#!/usr/bin/env python3
"""
annotate_vcd_power.py — Annotates a COPY of a source VCD with 4 new
'real' (floating point) signals, one per window from the
power_sweep.csv produced by sweep_power.sh / sweep_power_parallel.sh:

    power_total_w
    power_sequential_w
    power_combinational_w
    power_clock_w

Each signal stays constant through its window and steps to a new value
right at the start of the next window — so you can see the estimated
power alongside the original signals in any VCD viewer (GTKWave,
Surfer, etc.).

NEVER edits the source VCD. It always:
  1. copies the source VCD into --out-dir (byte-for-byte copy)
  2. reads that copy (not the source) and writes the annotated version
     to a temp file inside --out-dir
  3. replaces the copy with the annotated version (atomic rename)

At the end, --out-dir just holds the already-annotated copy — the file
passed in --vcd is never opened for writing.

Usage:
    python3 annotate_vcd_power.py source.vcd power_sweep.csv \
        --out-dir power_sweep_results

    # custom output name inside --out-dir:
    python3 annotate_vcd_power.py source.vcd power_sweep.csv \
        --out-dir power_sweep_results -o dump_with_power.vcd

Known limitation: if the CSV windows overlap (--window-step <
--window in the sweep), these signals just step to a new value at each
window's "start", in chronological order — there's no way to represent
two overlapping windows at once in a single scalar signal per metric.
For --window-step >= --window (consecutive windows or with a gap, the
common case) this isn't an issue.
"""
import argparse
import csv
import os
import re
import shutil
import sys
import tempfile

VAR_RE = re.compile(r"^\$var\s+\S+\s+\d+\s+(\S+)\s")
ENDDEF_RE = re.compile(r"^\$enddefinitions\b")
DUMPVARS_RE = re.compile(r"^\$dumpvars\b")
TIME_RE = re.compile(r"^#\s*(\d+)\s*$")

METRICS = [
    ("power_total_w", "total_power_w"),
    ("power_sequential_w", "sequential_power_w"),
    ("power_combinational_w", "combinational_power_w"),
    ("power_clock_w", "clock_power_w"),
]

# valid VCD identifier characters: printable ASCII from '!' (33) to '~'
# (126), no whitespace. We generate short IDs that don't collide with
# whatever's already used in the source file.
_ID_CHARS = [chr(c) for c in range(33, 127)]


def make_id_generator(existing_ids):
    existing = set(existing_ids)

    def gen():
        # try 1-character IDs first, then 2, etc. — never collides with
        # an ID already used in the source file.
        length = 1
        while True:
            from itertools import product
            for combo in product(_ID_CHARS, repeat=length):
                cand = "".join(combo)
                if cand not in existing:
                    existing.add(cand)
                    yield cand
            length += 1

    return gen()


def load_windows(csv_path):
    """Read power_sweep.csv, return a list of windows sorted by start:
    [{"start": int, "values": {var_name: float, ...}}, ...]
    Skips/warns about malformed rows or rows missing a metric.
    """
    windows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                start = int(row["start"])
                values = {}
                for var_name, col in METRICS:
                    values[var_name] = float(row[col])
                windows.append({"start": start, "values": values})
            except (KeyError, ValueError) as e:
                print(f"[warn] skipping malformed row in {csv_path}: "
                      f"{row} ({e})", file=sys.stderr)
    windows.sort(key=lambda w: w["start"])
    if not windows:
        print(f"[error] no valid windows found in {csv_path}",
              file=sys.stderr)
        sys.exit(1)
    return windows


def format_real(v):
    # VCD real-value format: 'r<number> <id>'. Compact scientific
    # notation, with enough precision to not lose a significant digit
    # on the typically small (Watts) values here.
    return f"{v:.9g}"


def annotate(input_path, output_path, windows):
    ids = {}  # var_name -> generated id

    with open(input_path, "r", errors="replace") as fin, \
         open(output_path, "w") as fout:

        # ---------- first pass: collect the IDs already in use in the
        # header, without writing anything yet (we need to know the
        # existing IDs before generating new ones, and we need to walk
        # the header anyway to insert the new $scope right before
        # $enddefinitions) ----------
        header_lines = []
        existing_ids = set()
        while True:
            line = fin.readline()
            if not line:
                raise ValueError("hit EOF before $enddefinitions — "
                                  "malformed VCD?")
            header_lines.append(line)
            m = VAR_RE.match(line)
            if m:
                existing_ids.add(m.group(1))
            if ENDDEF_RE.match(line.strip()):
                break

        id_gen = make_id_generator(existing_ids)
        for var_name, _ in METRICS:
            ids[var_name] = next(id_gen)

        # write the original header, but insert our new $scope right
        # BEFORE the $enddefinitions line (the last line collected)
        for line in header_lines[:-1]:
            fout.write(line)
        fout.write("$scope module power_sweep $end\n")
        for var_name, _ in METRICS:
            fout.write(f"$var real 64 {ids[var_name]} {var_name} $end\n")
        fout.write("$upscope $end\n")
        fout.write(header_lines[-1])  # the original $enddefinitions line

        # ---------- body: copy line by line, injecting power steps at
        # each window boundary ----------
        pending_idx = 0
        n_windows = len(windows)
        seen_dumpvars = False
        cur_time = 0

        def active_window_for(t):
            """Index of the last window whose start <= t, or None."""
            idx = None
            for i, w in enumerate(windows):
                if w["start"] <= t:
                    idx = i
                else:
                    break
            return idx

        def write_metric_lines(idx):
            if idx is None:
                for var_name, _ in METRICS:
                    fout.write(f"x{ids[var_name]}\n")
            else:
                vals = windows[idx]["values"]
                for var_name, col in METRICS:
                    fout.write(f"r{format_real(vals[var_name])} "
                               f"{ids[var_name]}\n")

        for line in fin:
            stripped = line.rstrip("\n")

            if not seen_dumpvars and DUMPVARS_RE.match(stripped):
                # initial-values block (t=0, usually right after the
                # first "#0" line in the body) — inject the starting
                # value of the 4 signals here (whatever window is active
                # at cur_time, or 'x' if no window covers the start)
                fout.write(line)
                idx0 = active_window_for(cur_time)
                write_metric_lines(idx0)
                if idx0 is not None:
                    pending_idx = idx0 + 1
                seen_dumpvars = True
                continue

            m = TIME_RE.match(stripped)
            if m:
                t = int(m.group(1))
                # inject any window whose start falls STRICTLY before
                # this timestamp (boundary doesn't line up with an
                # existing event — needs its own '#' line)
                while (pending_idx < n_windows
                       and windows[pending_idx]["start"] < t):
                    fout.write(f"#{windows[pending_idx]['start']}\n")
                    write_metric_lines(pending_idx)
                    pending_idx += 1

                cur_time = t
                fout.write(line)

                # if a window starts EXACTLY at this timestamp, fold the
                # value change into the same instant (avoids a
                # duplicate timestamp)
                while (pending_idx < n_windows
                       and windows[pending_idx]["start"] == t):
                    write_metric_lines(pending_idx)
                    pending_idx += 1
                continue

            fout.write(line)

        # windows whose start is past the last event in the source VCD:
        # still record their steps at the end of the file, so nothing
        # gets dropped (they end up "hanging" after the last real
        # timestamp, but stay valid/visible in the viewer)
        while pending_idx < n_windows:
            fout.write(f"#{windows[pending_idx]['start']}\n")
            write_metric_lines(pending_idx)
            pending_idx += 1

    return ids


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("vcd", help="Source VCD (NEVER modified)")
    ap.add_argument("csv", help="power_sweep.csv from sweep_power.sh")
    ap.add_argument("--out-dir", default="power_sweep_results",
                     help="folder to write the annotated copy into "
                          "(default: power_sweep_results)")
    ap.add_argument("-o", "--output-name", default=None,
                     help="output filename inside --out-dir (default: "
                          "same name as the source VCD)")
    args = ap.parse_args()

    if not os.path.isfile(args.vcd):
        print(f"[error] VCD not found: {args.vcd}", file=sys.stderr)
        sys.exit(1)
    if not os.path.isfile(args.csv):
        print(f"[error] CSV not found: {args.csv}", file=sys.stderr)
        sys.exit(1)

    os.makedirs(args.out_dir, exist_ok=True)

    out_name = args.output_name or os.path.basename(args.vcd)
    out_path = os.path.join(args.out_dir, out_name)

    # 1) copy the source into out-dir, untouched — from here on we
    #    never write to the path passed in args.vcd
    print(f"[1/3] copying {args.vcd} -> {out_path} ...")
    shutil.copy2(args.vcd, out_path)

    windows = load_windows(args.csv)
    print(f"[2/3] loaded {len(windows)} window(s) from {args.csv} "
          f"(start from {windows[0]['start']} to {windows[-1]['start']})")

    # 2) read the COPY (not the source) and write the annotated version
    #    to a temp file in the same out-dir, then replace the copy
    fd, tmp_path = tempfile.mkstemp(prefix=".annotate_", suffix=".vcd",
                                     dir=args.out_dir)
    os.close(fd)
    try:
        ids = annotate(out_path, tmp_path, windows)
        os.replace(tmp_path, out_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

    print(f"[3/3] done: {out_path}")
    print("      new signals (scope 'power_sweep'):")
    for var_name, col in METRICS:
        print(f"        {var_name}  (VCD id: {ids[var_name]!r}, "
              f"from {col})")
    print(f"      source file ({args.vcd}) was not modified.")


if __name__ == "__main__":
    main()