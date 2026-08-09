#!/usr/bin/env python3
"""
parse_power_report.py — Pulls the "Total" row out of OpenSTA's
report_power table from a log (stdout+stderr of the sta run).

The number of warnings before report_power varies from IP to IP
(different blackbox modules, SPEF quirks, etc.), so we don't try to
count warnings or detect where they "end". Instead we just search the
whole log for the line matching the Total row's numeric format:

Group                  Internal  Switching    Leakage      Total
                          Power      Power      Power      Power (Watts)
----------------------------------------------------------------
Sequential             1.91e-03   1.40e-06   9.65e-09   1.91e-03  60.3%
Combinational          3.23e-05   1.97e-05   3.04e-08   5.21e-05   1.6%
Clock                  6.60e-04   5.46e-04   2.41e-09   1.21e-03  38.1%
Macro                  0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
Pad                    0.00e+00   0.00e+00   0.00e+00   0.00e+00   0.0%
----------------------------------------------------------------
Total                  2.60e-03   5.67e-04   4.25e-08   3.17e-03 100.0%
                          82.1%      17.9%       0.0%

Works regardless of how many warnings (or which ones) came before it.

Usage:
    python3 parse_power_report.py log.txt
    cat log.txt | python3 parse_power_report.py -
    python3 parse_power_report.py log.txt --json

Default output (no --json): one headerless CSV line:
    internal_power_w,switching_power_w,leakage_power_w,total_power_w,sequential_power_w,combinational_power_w,clock_power_w

sequential_power_w, combinational_power_w and clock_power_w come from
the "Total Power (Watts)" column of the Sequential/Combinational/Clock
group rows (not the overall Total row). If any of those group rows
isn't found (different report_power layout, a differently-named group,
etc.) the corresponding value comes back as 0.0 and a warning is
printed to stderr — that's not fatal, only a missing overall Total row
is.

Exits with a non-zero status and a stderr message if the Total row
can't be found — that's the signal that OpenSTA failed before reaching
report_power (a fatal liberty/verilog/sdc/spef read error, for
instance), and it's what sweep_power.sh uses to decide whether to skip
that window.
"""
import argparse
import json
import re
import sys

# e.g. "Total                  2.60e-03   5.67e-04   4.25e-08   3.17e-03 100.0%"
TOTAL_RE = re.compile(
    r"^Total\s+"
    r"([\d.]+[eE][+-]?\d+)\s+"
    r"([\d.]+[eE][+-]?\d+)\s+"
    r"([\d.]+[eE][+-]?\d+)\s+"
    r"([\d.]+[eE][+-]?\d+)\s+"
    r"([\d.]+)%",
    re.MULTILINE,
)

# e.g. "Sequential             1.91e-03   1.40e-06   9.65e-09   1.91e-03  60.3%"
# Grabs the "Total Power" column (4th number) from a specific group row
# (Sequential, Combinational or Clock).
def _group_re(name):
    return re.compile(
        rf"^{name}\s+"
        r"[\d.]+[eE][+-]?\d+\s+"
        r"[\d.]+[eE][+-]?\d+\s+"
        r"[\d.]+[eE][+-]?\d+\s+"
        r"([\d.]+[eE][+-]?\d+)\s+"
        r"[\d.]+%",
        re.MULTILINE,
    )

SEQUENTIAL_RE = _group_re("Sequential")
COMBINATIONAL_RE = _group_re("Combinational")
CLOCK_RE = _group_re("Clock")

# e.g. "Warning: clock clk vcd period 39.931 differs from SDC clock period 25.000"
VCD_PERIOD_WARNING_RE = re.compile(
    r"clock\s+(\S+)\s+vcd period\s+([\d.]+)\s+differs from SDC clock period\s+([\d.]+)"
)


def parse_report(text):
    """Search the whole text for the Total row. Returns a dict, or None
    if not found (e.g. OpenSTA failed before report_power)."""
    m = TOTAL_RE.search(text)
    if not m:
        return None
    internal, switching, leakage, total, total_pct = m.groups()
    result = {
        "internal_power_w": float(internal),
        "switching_power_w": float(switching),
        "leakage_power_w": float(leakage),
        "total_power_w": float(total),
        "total_power_pct": float(total_pct),
    }

    for key, rx, label in (
        ("sequential_power_w", SEQUENTIAL_RE, "Sequential"),
        ("combinational_power_w", COMBINATIONAL_RE, "Combinational"),
        ("clock_power_w", CLOCK_RE, "Clock"),
    ):
        gm = rx.search(text)
        if gm:
            result[key] = float(gm.group(1))
        else:
            result[key] = 0.0
            print(
                f"[warn] couldn't find the '{label}' group row in "
                f"report_power — using 0.0 for {key}.",
                file=sys.stderr,
            )

    period_warn = VCD_PERIOD_WARNING_RE.search(text)
    if period_warn:
        result["vcd_clock_period"] = float(period_warn.group(2))
        result["sdc_clock_period"] = float(period_warn.group(3))

    return result


def main():
    ap = argparse.ArgumentParser(
        description="Extract power values (Total row) from an OpenSTA / "
                     "run_power_script.sh log."
    )
    ap.add_argument("logfile", help="Log file, or '-' for stdin")
    ap.add_argument("--json", action="store_true",
                     help="Print JSON instead of a headerless CSV line")
    args = ap.parse_args()

    if args.logfile == "-":
        text = sys.stdin.read()
    else:
        with open(args.logfile, "r", errors="replace") as f:
            text = f.read()

    result = parse_report(text)
    if result is None:
        print(
            "[error] couldn't find the 'Total' row from report_power in "
            "this log — OpenSTA probably failed before getting there "
            "(open the full log to see the actual error).",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.json:
        print(json.dumps(result))
    else:
        print(
            f"{result['internal_power_w']},{result['switching_power_w']},"
            f"{result['leakage_power_w']},{result['total_power_w']},"
            f"{result['sequential_power_w']},{result['combinational_power_w']},"
            f"{result['clock_power_w']}"
        )


if __name__ == "__main__":
    main()