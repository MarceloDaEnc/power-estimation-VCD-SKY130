#!/usr/bin/env bash
#
# sweep_power_parallel.sh — Same job as sweep_power.sh (generate
# consecutive/overlapping VCD windows, run OpenSTA's power estimate via
# run_power_script.sh for each one, combine everything into a CSV), but
# spreads the windows across N cores in parallel.
#
# OpenSTA itself still runs single-core within each run — what gets
# parallelized here is the NUMBER of simultaneous run_power_script.sh
# executions, one per core.
#
# -----------------------------------------------------------------------
# WHY YOU CAN'T JUST RUN THE ORIGINAL sweep_power.sh IN PARALLEL AS-IS:
#
#   1. power_vcd.tcl always reads a fixed "dump.vcd" file from the
#      current directory. If two workers run against the SAME --workdir
#      at once, one overwrites the other's dump.vcd mid-run — wrong or
#      corrupted result, silently.
#
#   2. run_power_script.sh mounts $(pwd) as /openlane INSIDE THE
#      CONTAINER. That means the container only sees files that live
#      INSIDE the mounted directory. So each worker needs its own,
#      self-contained directory — you can't use a symlink pointing
#      outside it (e.g. to your original --workdir), because that path
#      doesn't exist inside the container and reading the .lib/.v/.spef
#      would fail.
#
# SOLUTION: each "slot" (one per core) gets its own real directory,
# built once and reused across windows:
#   - files from the original workdir (power_vcd.tcl,
#     run_power_script.sh, .sdc, .v, .spef, .lib, etc.) go in as
#     HARDLINKS (same data on disk, no duplication, but they show up as
#     regular files inside the slot — the container sees them fine) and
#     get made read-only (chmod a-w) as a safety net, so nothing can
#     ever corrupt the shared original file if something tries to write
#     to it by mistake.
#   - any SUBDIRECTORIES in the workdir get copied for real (cp -a),
#     since you can't hardlink a directory and a symlink would break
#     inside the container for the same reason as point 2.
#   - dump.vcd is NEVER hardlinked — each slot generates its own for
#     every window it processes.
#
# Hardlinking requires the slot and the original file to be on the SAME
# filesystem. That's why slot directories live under --workdir (not
# --out-dir), which is where the original files already are. If that's
# not possible for some reason (e.g. workdir is read-only), use
# --copy-workdir to do real copies instead of hardlinks.
#
# Usage:
#   ./sweep_power_parallel.sh \
#       --vcd source.vcd \
#       --workdir /path/to/project \
#       --start 0 --window 2000000 --steps 40 \
#       --jobs 8
#
# (same options as sweep_power.sh, plus --jobs and --copy-workdir)
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
COPY_WORKDIR=0
SHIFT_TO_ZERO=0
JOBS="$(nproc 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"

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
                          iteration (default: same as --window)
  --workdir DIR           Project directory (where power_vcd.tcl,
                          run_power_script.sh, .sdc, .v, .spef, .lib
                          live) (default: current directory: $WORKDIR)
  --out-dir DIR           Where to save the CSV and logs (default:
                          ./power_sweep_results)
  --jobs N                How many windows to process in parallel, one
                          per core (default: $JOBS, detected via nproc).
                          OpenSTA itself still runs single-core WITHIN
                          each run — this controls how many
                          run_power_script.sh executions happen at once.
  --copy-workdir          Instead of hardlinking, do a real copy of the
                          ENTIRE --workdir into each slot. Slower and
                          uses more disk, but avoids hardlinks (useful
                          if --workdir is on a read-only filesystem or a
                          different disk than --out-dir).
  --vcd-extract-script F  Path to vcd_extract.py (default: next to this
                          script)
  --keep-vcds             Keep a copy of each generated dump.vcd under
                          out-dir/vcds/
  --shift-to-zero         Generate each dump.vcd with timestamps shifted
                          to start at #0 (passes --shift-to-zero through
                          to vcd_extract.py) instead of keeping the
                          window's absolute timestamp
  -h, --help              Show this help

Example (40 2us windows, 8 in parallel):
  $0 --vcd source.vcd --workdir ~/my_project \\
     --start 0 --window 2000000 --steps 40 --jobs 8
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
        --jobs) JOBS="$2"; shift 2 ;;
        --copy-workdir) COPY_WORKDIR=1; shift ;;
        --vcd-extract-script) VCD_EXTRACT="$2"; shift 2 ;;
        --keep-vcds) KEEP_VCDS=1; shift ;;
        --shift-to-zero) SHIFT_TO_ZERO=1; shift ;;
        -h|--help) usage ;;
        *) echo "Unknown option: $1" >&2; usage ;;
    esac
done

[[ -z "$VCD" || -z "$WINDOW" || -z "$STEPS" ]] && usage
[[ -z "$WINDOW_STEP" ]] && WINDOW_STEP="$WINDOW"

if ! [[ "$JOBS" =~ ^[0-9]+$ ]] || [[ "$JOBS" -lt 1 ]]; then
    echo "Error: --jobs must be an integer >= 1 (got: $JOBS)" >&2
    exit 1
fi
if ! [[ "$STEPS" =~ ^[0-9]+$ ]] || [[ "$STEPS" -lt 1 ]]; then
    echo "Error: --steps must be an integer >= 1" >&2
    exit 1
fi
# no point creating more slots than windows to process
if [[ "$JOBS" -gt "$STEPS" ]]; then
    JOBS="$STEPS"
fi

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

mkdir -p "$OUT_DIR/logs" "$OUT_DIR/results"
[[ "$KEEP_VCDS" -eq 1 ]] && mkdir -p "$OUT_DIR/vcds"

CSV="$OUT_DIR/power_sweep.csv"
CSV_HEADER="index,start,end,duration,events,internal_power_w,switching_power_w,leakage_power_w,total_power_w,sequential_power_w,combinational_power_w,clock_power_w"

# the slots' parent directory lives INSIDE --workdir (not --out-dir) on
# purpose: hardlinking only works if both files are on the same
# filesystem, and that's where power_vcd.tcl/.lib/.v/.spef/.sdc already are.
WORKERS_DIR="$WORKDIR/.sweep_parallel_workers"
mkdir -p "$WORKERS_DIR"

echo "== sweep_power_parallel.sh: $STEPS window(s), duration $WINDOW, start=$START, step=$WINDOW_STEP =="
echo "== source workdir: $WORKDIR =="
echo "== parallelism: $JOBS slot(s) (1 run_power_script.sh execution per slot at a time) =="
echo "== results: $OUT_DIR =="
echo

# ---------- set up slot directories (once, reused across windows) ----------
setup_slot() {
    local slot="$1"
    local slot_dir="$WORKERS_DIR/slot_$slot"
    mkdir -p "$slot_dir"

    if [[ "$COPY_WORKDIR" -eq 1 ]]; then
        echo "[setup] slot $slot: copying the whole workdir (--copy-workdir)..."
        cp -a "$WORKDIR"/. "$slot_dir"/
        rm -rf "$slot_dir/.sweep_parallel_workers"  # don't recurse into the slots themselves
        rm -f "$slot_dir/dump.vcd"
        return
    fi

    while IFS= read -r -d '' entry; do
        local base
        base="$(basename "$entry")"
        [[ "$base" == "dump.vcd" ]] && continue
        [[ "$base" == ".sweep_parallel_workers" ]] && continue
        local dest="$slot_dir/$base"
        [[ -e "$dest" || -L "$dest" ]] && continue  # slot already set up

        if [[ -d "$entry" && ! -L "$entry" ]]; then
            # real subdirectory: no such thing as a directory hardlink,
            # and a symlink would break inside the container -> real copy
            cp -a "$entry" "$dest"
        else
            # a file (or a pre-existing symlink in the original workdir):
            # try a hardlink; if the filesystem won't allow it
            # (cross-device, or it's a symlink), fall back to a real copy
            if ! ln "$entry" "$dest" 2>/dev/null; then
                cp -a "$entry" "$dest"
            else
                chmod a-w "$dest" 2>/dev/null || true
            fi
        fi
    done < <(find "$WORKDIR" -mindepth 1 -maxdepth 1 -print0)
}

for ((s = 0; s < JOBS; s++)); do
    setup_slot "$s"
done
echo "== $JOBS slot(s) ready in $WORKERS_DIR =="
echo

# ---------- FIFO-based semaphore for free slots ----------
SLOTS_FIFO="$(mktemp -u "${TMPDIR:-/tmp}/sweep_slots.XXXXXX")"
mkfifo "$SLOTS_FIFO"
exec 8<>"$SLOTS_FIFO"
rm -f "$SLOTS_FIFO"
for ((s = 0; s < JOBS; s++)); do printf '%d\n' "$s" >&8; done

cleanup() {
    exec 8>&- 2>/dev/null || true
}
trap cleanup EXIT

export WORKERS_DIR VCD VCD_EXTRACT PARSE_SCRIPT OUT_DIR START WINDOW_STEP WINDOW KEEP_VCDS STEPS SHIFT_TO_ZERO

run_window() {
    local i="$1"
    local slot
    read -r -u 8 slot

    local slot_dir="$WORKERS_DIR/slot_$slot"
    local wstart=$((START + i * WINDOW_STEP))
    local wend=$((wstart + WINDOW))
    local duration=$((wend - wstart))
    local dump_vcd="$slot_dir/dump.vcd"
    local extract_log="$OUT_DIR/logs/extract_${i}.log"
    local power_log="$OUT_DIR/logs/power_${i}.log"
    local row_file="$OUT_DIR/results/row_${i}.csv"

    echo "---- [slot $slot] [$((i + 1))/$STEPS] window [$wstart, $wend] (duration $duration) ----"

    local extract_args=("$VCD" --start "$wstart" --end "$wend" -o "$dump_vcd")
    [[ "$SHIFT_TO_ZERO" -eq 1 ]] && extract_args+=(--shift-to-zero)
    if ! python3 "$VCD_EXTRACT" "${extract_args[@]}" \
            > "$extract_log" 2>&1; then
        echo "[warn][slot $slot] vcd_extract.py failed for [$wstart, $wend] — skipping." >&2
        printf '%d\n' "$slot" >&8
        return 0
    fi

    if [[ "$KEEP_VCDS" -eq 1 ]]; then
        cp "$dump_vcd" "$OUT_DIR/vcds/dump_${wstart}_${wend}.vcd"
    fi

    if ! ( cd "$slot_dir" && bash run_power_script.sh ) > "$power_log" 2>&1; then
        echo "[warn][slot $slot] run_power_script.sh failed for [$wstart, $wend] — see $power_log — skipping." >&2
        printf '%d\n' "$slot" >&8
        return 0
    fi

    local result_csv
    if ! result_csv=$(python3 "$PARSE_SCRIPT" "$power_log"); then
        echo "[warn][slot $slot] couldn't extract total power from $power_log — skipping." >&2
        printf '%d\n' "$slot" >&8
        return 0
    fi

    local events
    events=$(grep -oE '\[ok\][[:space:]]+[0-9]+' "$extract_log" | grep -oE '[0-9]+' || true)

    echo "$i,$wstart,$wend,$duration,$events,$result_csv" > "$row_file"
    local total_w
    total_w=$(echo "$result_csv" | awk -F, '{print $4}')
    echo "[ok][slot $slot] window $i: total power ${total_w} W"

    printf '%d\n' "$slot" >&8
}
export -f run_window

# ---------- fire off the STEPS windows, at most JOBS at a time ----------
seq 0 "$((STEPS - 1))" | xargs -P "$JOBS" -I{} bash -c 'run_window "$@"' _ {}

exec 8>&-
trap - EXIT

# ---------- stitch the results back together in the original order ----------
echo
echo "$CSV_HEADER" > "$CSV"
missing=0
for ((i = 0; i < STEPS; i++)); do
    row_file="$OUT_DIR/results/row_${i}.csv"
    if [[ -f "$row_file" ]]; then
        cat "$row_file" >> "$CSV"
    else
        missing=$((missing + 1))
    fi
done

echo "== Done. Results in: $CSV =="
if [[ "$missing" -gt 0 ]]; then
    echo "[warn] $missing window(s) failed and were skipped (see warnings above / logs in $OUT_DIR/logs)." >&2
fi
echo "To generate the chart:"
echo "  python3 $SCRIPT_DIR/plot_power_sweep.py $CSV"
echo
echo "Tip: the slot directories in $WORKERS_DIR were kept around (reusable"
echo "on the next run, avoiding redoing the hardlinks/copies). Delete them"
echo "by hand if you want to free up space or force a clean setup."