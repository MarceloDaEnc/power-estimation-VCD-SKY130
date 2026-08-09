#!/usr/bin/env python3
"""
vcd_extract.py — Pull a single time window out of a large VCD and write
it out as a standalone, valid VCD file.

Unlike vcd_split.py (which chops the whole file into N pieces), this one
streams through the file once (never loads it all into memory) and only
writes the slice you asked for to disk. That makes it fine on huge VCDs
since the pass is O(file size) in time, not memory, and the output is
just the requested window.

Usage:
    python3 vcd_extract.py input.vcd --start 10000 --end 20000 -o output.vcd

    # Worth checking the file's real time range and unit ($timescale)
    # before extracting, so you don't ask for a window outside the file
    # or mix up units:
    python3 vcd_extract.py input.vcd --info

--start/--end use the VCD's own time units (whatever follows # on
timestamp lines), not necessarily ns — that depends on the $timescale
declared in the header. Use --info to check the $timescale and the real
[min, max] time range in the file before picking --start/--end.
"""

import argparse
import re
import sys


def parse_header(f):
    """Read the VCD header (everything up to $enddefinitions $end).
    Returns (header_text, signal_ids).
    """
    header_lines = []
    signal_ids = set()
    var_re = re.compile(r"^\$var\s+\S+\s+\d+\s+(\S+)\s")

    while True:
        line = f.readline()
        if not line:
            raise ValueError("hit EOF before $enddefinitions — malformed VCD?")
        header_lines.append(line)
        m = var_re.match(line)
        if m:
            signal_ids.add(m.group(1))
        if line.strip().startswith("$enddefinitions"):
            break

    return "".join(header_lines), signal_ids


VALUE_CHANGE_SCALAR_RE = re.compile(r"^([01xXzZ])(\S+)\s*$")
VALUE_CHANGE_VECTOR_RE = re.compile(r"^([bBrR])(\S+)\s+(\S+)\s*$")
TIME_RE = re.compile(r"^#\s*(\d+)\s*$")


def parse_time_line(line_stripped, line_no, warned_set):
    """Return the integer value of a '#<time>' line, or None if it
    doesn't match the expected format. Warns once per unrecognized
    format on stderr instead of silently swallowing it, so parsing bugs
    don't go unnoticed.
    """
    m = TIME_RE.match(line_stripped)
    if m:
        return int(m.group(1))
    if line_stripped not in warned_set:
        warned_set.add(line_stripped)
        print(
            f"[warn] line {line_no}: didn't recognize as a timestamp: "
            f"{line_stripped!r} — skipping (current time stays at the "
            f"last valid value)",
            file=sys.stderr,
        )
    return None


def parse_value_change(line):
    """Return (signal_id, full_line) for a value-change line, or None
    if it's not recognized.
    """
    line = line.rstrip("\n")
    if not line:
        return None
    m = VALUE_CHANGE_VECTOR_RE.match(line)
    if m:
        return m.group(3), line
    m = VALUE_CHANGE_SCALAR_RE.match(line)
    if m:
        return m.group(2), line
    return None


def extract_range(input_path, output_path, start, end, shift_to_zero=False):
    """Extract [start, end] (inclusive on both ends) from input_path
    into output_path. Single streaming pass over the file.

    If shift_to_zero=True, every timestamp written to the output is
    shifted by subtracting `start`, so the extracted window starts at
    #0 (and ends at #(end-start)) — same duration and same gaps between
    events, just re-anchored at the origin. Useful for testing whether
    an absolute timestamp value affects OpenSTA's report_power in any
    way (it shouldn't, since the calculation is based on toggle rate /
    duty cycle — i.e. Δt between changes, not the absolute time value —
    but worth confirming experimentally rather than assuming).
    """
    def out_time(t):
        return (t - start) if shift_to_zero else t

    with open(input_path, "r", errors="replace") as f, \
         open(output_path, "w") as out_f:

        header_text, all_signal_ids = parse_header(f)

        # We need each signal's value exactly at `start`, even if its
        # last change happened before the window. So we track state as
        # we scan forward, and only start writing once cur_time >= start.
        current_values = {}
        cur_time = 0
        in_range = False
        header_written = False
        pending_time = None
        wrote_any_change_in_current_time = False
        events_in_range = 0
        warned_formats = set()

        def open_output_at(t):
            nonlocal header_written
            out_f.write(header_text)
            out_f.write(f"#{out_time(t)}\n")
            out_f.write("$dumpvars\n")
            for sig_id, val_line in current_values.items():
                out_f.write(val_line + "\n")
            out_f.write("$end\n")
            header_written = True

        for line_no, line in enumerate(f, start=1):
            line_stripped = line.rstrip("\n")
            if not line_stripped:
                continue

            if line_stripped.startswith("#"):
                t = parse_time_line(line_stripped, line_no, warned_formats)
                if t is None:
                    continue
                cur_time = t

                if cur_time > end:
                    break  # past the window, stop reading

                if not in_range and cur_time >= start:
                    in_range = True
                    open_output_at(cur_time if cur_time >= start else start)

                pending_time = cur_time
                wrote_any_change_in_current_time = False
                continue

            if line_stripped.startswith("$"):
                continue  # stray control markers in the body

            parsed = parse_value_change(line_stripped)
            if parsed is None:
                continue
            sig_id, val_line = parsed
            if sig_id not in all_signal_ids:
                continue

            current_values[sig_id] = val_line

            if in_range:
                if pending_time is not None and not wrote_any_change_in_current_time:
                    out_f.write(f"#{out_time(pending_time)}\n")
                    wrote_any_change_in_current_time = True
                out_f.write(val_line + "\n")
                events_in_range += 1

        if not header_written:
            # Never entered the window (start is past the end of the
            # file, or the file is shorter than start): still emit a
            # valid VCD with the last known state, just no events.
            open_output_at(start)

    return events_in_range


TIMESCALE_RE = re.compile(r"\$timescale\s+(.+?)\s*\$end", re.DOTALL)

_UNIT_TO_SECONDS = {
    "s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15,
}


def _format_real_time(raw_ticks, timescale_str):
    """Convert raw_ticks * timescale (e.g. '1 ps') into a readable
    string in the most natural unit (ns/us/ms/s). Returns None if the
    timescale can't be parsed.
    """
    m = re.match(r"(\d+)\s*([a-z]+)", timescale_str.strip())
    if not m:
        return None
    mult = int(m.group(1))
    unit = m.group(2)
    if unit not in _UNIT_TO_SECONDS:
        return None
    total_seconds = raw_ticks * mult * _UNIT_TO_SECONDS[unit]
    for label, factor in (("s", 1), ("ms", 1e-3), ("us", 1e-6),
                           ("ns", 1e-9), ("ps", 1e-12), ("fs", 1e-15)):
        if total_seconds >= factor or label == "fs":
            return f"{total_seconds / factor:.3f} {label}"
    return None


def show_info(input_path):
    """Quick scan: prints $timescale, min/max time, and counts of
    value-change and timestamp lines found — writes nothing. Meant for
    sanity-checking before picking --start/--end.
    """
    timescale = None
    min_t = None
    max_t = None
    n_time_lines = 0
    n_unparsed_time_lines = 0
    n_value_changes = 0
    warned_formats = set()

    with open(input_path, "r", errors="replace") as f:
        header_text, signal_ids = parse_header(f)
        m = TIMESCALE_RE.search(header_text)
        if m:
            timescale = " ".join(m.group(1).split())

        for line_no, line in enumerate(f, start=1):
            line_stripped = line.rstrip("\n")
            if not line_stripped:
                continue
            if line_stripped.startswith("#"):
                t = parse_time_line(line_stripped, line_no, warned_formats)
                if t is None:
                    n_unparsed_time_lines += 1
                    continue
                n_time_lines += 1
                if min_t is None or t < min_t:
                    min_t = t
                if max_t is None or t > max_t:
                    max_t = t
                continue
            if line_stripped.startswith("$"):
                continue
            if parse_value_change(line_stripped) is not None:
                n_value_changes += 1

    print(f"File:                 {input_path}")
    print(f"$timescale:           {timescale!r}")
    print(f"Signals in header:    {len(signal_ids)}")
    print(f"Timestamp lines:      {n_time_lines} recognized, "
          f"{n_unparsed_time_lines} NOT recognized")
    print(f"Value changes:        {n_value_changes}")
    print(f"Real time range in file: [{min_t}, {max_t}]")
    if timescale and max_t is not None:
        real = _format_real_time(max_t, timescale)
        if real:
            print(f"  (roughly {real} of simulated time)")
    if n_unparsed_time_lines > 0:
        print(
            "\n[heads up] Some lines starting with '#' didn't match the "
            "expected numeric format — see the warnings above. That "
            "points to a timestamp format outside the simple "
            "'#<integer>' pattern (mixed units, or something unusual "
            "from your simulator). Send me a sample of those lines if "
            "the parser needs adjusting."
        )
    elif min_t is not None and max_t is not None:
        print(
            "\nUse --start/--end within that range (same raw unit "
            f"above, scale = {timescale!r})."
        )


def main():
    ap = argparse.ArgumentParser(
        description="Extract a single time window from a large VCD."
    )
    ap.add_argument("input", help="Input VCD file")
    ap.add_argument("--info", action="store_true",
                     help="Just print timescale and the file's real time "
                          "range (doesn't extract anything); run this first")
    ap.add_argument("--start", type=int, default=None,
                     help="Start time (VCD units, inclusive)")
    ap.add_argument("--end", type=int, default=None,
                     help="End time (VCD units, inclusive)")
    ap.add_argument("-o", "--output", default=None,
                     help="Output VCD path")
    ap.add_argument("--shift-to-zero", action="store_true",
                     help="Shift output timestamps by subtracting "
                          "--start, so the extracted window starts at "
                          "#0 (same duration/same Δt between events). "
                          "Only useful for testing whether the absolute "
                          "timestamp value affects OpenSTA's "
                          "report_power; shouldn't be needed for normal "
                          "use.")
    args = ap.parse_args()

    if args.info:
        show_info(args.input)
        return

    if args.start is None or args.end is None or args.output is None:
        ap.error("--start, --end and -o/--output are required "
                  "(or use --info on its own for diagnostics)")
    if args.end < args.start:
        ap.error("--end must be >= --start")

    n = extract_range(args.input, args.output, args.start, args.end,
                       shift_to_zero=args.shift_to_zero)
    shift_note = " — shifted to start at #0" if args.shift_to_zero else ""
    print(f"[ok] {n} event(s) written to "
          f"'{args.output}' (window [{args.start}, {args.end}]{shift_note})")


if __name__ == "__main__":
    main()