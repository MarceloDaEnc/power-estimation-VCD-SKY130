#!/usr/bin/env bash
#
# sweep_power.sh — Generates a sequence of VCDs covering CONSECUTIVE
# (non-overlapping) time windows from a source VCD (using your own
# vcd_extract.py), runs OpenSTA's power estimate (via
# run_power_script.sh) for each one, and combines the results into a
# single CSV ready for plotting.
#
# Each iteration 'i' (0-indexed) generates a VCD covering:
#     [START + i * WINDOW_STEP, START + i * WINDOW_STEP + WINDOW]
#
# With --window-step equal to --window (the default), windows sit
# back-to-back with no overlap, e.g. with --start 0 --window 2000000
# --steps 10:
#     [0, 2000000], [2000000, 4000000], [4000000, 6000000], ...,
#     [18000000, 20000000]
#
# Pass a different --window-step if you want overlap or gaps between
# windows (smaller = overlap, larger = gap).
#
# How this fits your existing scripts:
#   - power_vcd.tcl always reads a fixed file named "dump.vcd" in the
#     current directory (not a variable path), and run_power_script.sh
#     mounts $(pwd) as /openlane inside the container. That's why this
#     script OVERWRITES <workdir>/dump.vcd on every iteration and runs
#     run_power_script.sh from inside the workdir, via
#     "bash run_power_script.sh" (not "./run_power_script.sh") so it
#     doesn't depend on the file's exec bit.
#   - Nothing in your original scripts gets touched.
#
# Usage:
#   ./sweep_power.sh \
#       --vcd source.vcd \
#       --workdir /path/to/project \
#       --start 0 --window 2000000 --steps 10
#
# Prerequisites in --workdir: power_vcd.tcl, run_power_script.sh, the
# .sdc, the .v, the .spef and the .lib (the same files
# run_power_script.sh already expects to find today). vcd_extract.py
# and parse_power_report.py need to sit next to this script.
set -euo pipefail

# ---------- defaults ----------
START=0
WINDOW=""
WINDOW_STEP=""
STEPS=""
WORKDIR="$(pwd)"
VCD=""
OUT_DIR="power_sweep_results"
KEEP_VCDS=0
SHIFT_TO_ZERO=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VCD_EXTRACT="$SCRIPT_DIR/vcd_extract.py"
PARSE_SCRIPT="$SCRIPT_DIR/parse_power_report.py"

usage() {
    cat <<EOF
Usage: $0 --vcd SOURCE.vcd --window N --steps N [options]

Required:
  --vcd FILE              Source (large) VCD to slice windows from
  --window N              Duration (VCD units) of each window
  --steps N               How many windows to generate

Optional:
  --start N               Start time of the 1st window (default: 0)
  --window-step N         How much the window START advances each
                          iteration (default: same as --window, i.e.
                          consecutive non-overlapping windows; use a
                          smaller value to overlap windows, or a larger
                          one to leave gaps)
  --workdir DIR           Project directory (where power_vcd.tcl,
                          run_power_script.sh, .sdc, .v, .spef, .lib
                          live) (default: current directory: $WORKDIR)
  --out-dir DIR           Where to save the CSV, logs, and (optionally)
                          the VCDs (default: ./power_sweep_results)
  --vcd-extract-script F  Path to vcd_extract.py (default: next to this
                          script)
  --keep-vcds             Keep a copy of each generated dump.vcd under
                          out-dir/vcds/
  --shift-to-zero         Generate each dump.vcd with timestamps shifted
                          to start at #0 (passes --shift-to-zero through
                          to vcd_extract.py) instead of keeping the
                          window's absolute timestamp
  -h, --help              Show this help

Example (10 consecutive 2us windows):
  $0 --vcd source.vcd --workdir ~/my_project \\
     --start 0 --window 2000000 --steps 10
  # generates [0,2000000], [2000000,4000000], [4000000,6000000], ...

Example (a 2us window sliding 1us at a time, with overlap):
  $0 --vcd source.vcd --workdir ~/my_project \\
     --start 0 --window 2000000 --window-step 1000000 --steps 10
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --vcd) VCD="$2"; shift 2 ;;
        --start) START="$2"; shift 2 ;;
        --window) WINDOW="$2"; shift 2 ;;
        --window-step) WINDOW_STEP="$2"; shift 2 ;;
        --steps) STEPS="$2"; shift 2 ;;
        --workdir) WORKDIR="$2"; shift 2 ;;
        --out-dir) OUT_DIR="$2"; shift 2 ;;
        --vcd-extract-script) VCD_EXTRACT="$2"; shift 2 ;;
        --keep-vcds) KEEP_VCDS=1; shift ;;
        --shift-to-zero) SHIFT_TO_ZERO=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

[[ -z "$VCD" || -z "$WINDOW" || -z "$STEPS" ]] && usage
[[ -z "$WINDOW_STEP" ]] && WINDOW_STEP="$WINDOW"

# strip trailing slash from workdir (avoids "calculation//dump.vcd" in logs)
WORKDIR="${WORKDIR%/}"

if [[ ! -f "$VCD" ]]; then
    echo "Error: source VCD not found: $VCD" >&2
    exit 1
fi
if [[ ! -f "$VCD_EXTRACT" ]]; then
    echo "Error: vcd_extract.py not found at: $VCD_EXTRACT" >&2
    echo "       (use --vcd-extract-script to point at the right path)" >&2
    exit 1
fi
if [[ ! -f "$PARSE_SCRIPT" ]]; then
    echo "Error: parse_power_report.py not found next to this script ($SCRIPT_DIR)" >&2
    exit 1
fi
if [[ ! -f "$WORKDIR/power_vcd.tcl" || ! -f "$WORKDIR/run_power_script.sh" ]]; then
    echo "Error: $WORKDIR doesn't seem to have power_vcd.tcl / run_power_script.sh" >&2
    exit 1
fi

mkdir -p "$OUT_DIR/logs"
[[ "$KEEP_VCDS" -eq 1 ]] && mkdir -p "$OUT_DIR/vcds"

CSV="$OUT_DIR/power_sweep.csv"
echo "index,start,end,duration,events,internal_power_w,switching_power_w,leakage_power_w,total_power_w,sequential_power_w,combinational_power_w,clock_power_w" > "$CSV"

DUMP_VCD="$WORKDIR/dump.vcd"

echo "== sweep_power.sh: $STEPS window(s) of duration $WINDOW, start=$START, step=$WINDOW_STEP =="
echo "== workdir: $WORKDIR =="
echo "== results: $OUT_DIR =="

for ((i = 0; i < STEPS; i++)); do
    WSTART=$((START + i * WINDOW_STEP))
    WEND=$((WSTART + WINDOW))
    DURATION=$((WEND - WSTART))

    echo
    echo "---- [$((i + 1))/$STEPS] window [$WSTART, $WEND] (duration $DURATION) ----"

    EXTRACT_LOG="$OUT_DIR/logs/extract_${i}.log"
    EXTRACT_ARGS=("$VCD" --start "$WSTART" --end "$WEND" -o "$DUMP_VCD")
    [[ "$SHIFT_TO_ZERO" -eq 1 ]] && EXTRACT_ARGS+=(--shift-to-zero)
    if ! python3 "$VCD_EXTRACT" "${EXTRACT_ARGS[@]}" \
            > "$EXTRACT_LOG" 2>&1; then
        echo "[warn] vcd_extract.py failed for window [$WSTART, $WEND] — skipping." >&2
        cat "$EXTRACT_LOG" >&2
        continue
    fi
    cat "$EXTRACT_LOG"

    if [[ "$KEEP_VCDS" -eq 1 ]]; then
        cp "$DUMP_VCD" "$OUT_DIR/vcds/dump_${WSTART}_${WEND}.vcd"
    fi

    POWER_LOG="$OUT_DIR/logs/power_${i}.log"
    if ! ( cd "$WORKDIR" && bash run_power_script.sh ) > "$POWER_LOG" 2>&1; then
        echo "[warn] run_power_script.sh returned an error for window [$WSTART, $WEND]." >&2
        echo "       See $POWER_LOG for details — skipping this window." >&2
        continue
    fi

    if ! RESULT_CSV=$(python3 "$PARSE_SCRIPT" "$POWER_LOG"); then
        echo "[warn] couldn't extract total power from log $POWER_LOG — skipping." >&2
        continue
    fi

    EVENTS=$(grep -oE '\[ok\][[:space:]]+[0-9]+' "$EXTRACT_LOG" | grep -oE '[0-9]+' || true)

    echo "$i,$WSTART,$WEND,$DURATION,$EVENTS,$RESULT_CSV" >> "$CSV"
    TOTAL_W=$(echo "$RESULT_CSV" | awk -F, '{print $4}')
    echo "[ok] total power recorded: ${TOTAL_W} W"
done

echo
echo "== Done. Results in: $CSV =="
echo "To generate the chart:"
echo "  python3 $SCRIPT_DIR/plot_power_sweep.py $CSV"