#!/usr/bin/env python3
"""
plot_power_sweep.py — Reads the CSV produced by sweep_power.sh and
builds an INTERACTIVE (HTML) grouped bar chart of Total power plus the
Sequential/Combinational/Clock group powers, as a function of the VCD
window used in each run.

Usage:
    python3 plot_power_sweep.py power_sweep_results/power_sweep.csv
    python3 plot_power_sweep.py power_sweep.csv -o chart.html --x-axis duration
    python3 plot_power_sweep.py power_sweep.csv --timescale "1 ps"
    python3 plot_power_sweep.py power_sweep.csv --autoscale-skip 3
    python3 plot_power_sweep.py power_sweep.csv --y-min 0.0030 --y-max 0.0032

Produces a self-contained .html file (opens in any browser, no server
needed). Interactivity available:
  - hover a bar to see the exact value and window
  - click a legend entry to toggle that series (e.g. hide "Total" to
    compare Sequential/Combinational/Clock against each other)
  - double-click a legend entry to isolate just that series
  - drag to zoom into a region; the toolbar in the top-right has
    zoom/pan/reset/export-as-PNG
  - buttons above the chart switch the Y-axis scale between "zoom"
    (skips the initial windows, useful when a reset transient dominates
    the range and flattens the real variation in the rest) and "full
    view", plus linear/log scale toggle

Y-axis scale control (keeps an initial spike — e.g. a reset transient —
from flattening the visible variation across the rest of the windows):
  --autoscale-skip N   how many initial windows to ignore when computing
                        the default Y scale (default: 1). A button in
                        the chart always lets you switch back to the
                        full view.
  --y-min / --y-max    fix the scale manually (in Watts); disables the
                        scale-toggle buttons.

Needs a CSV from the current parse_power_report.py / sweep_power.sh
(with sequential_power_w, combinational_power_w, clock_power_w
columns). Older CSVs without those columns still work, but the
Sequential/Combinational/Clock bars show up as 0.
"""
import argparse
import csv
import sys


def load_rows(csv_path):
    rows = []
    with open(csv_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                rows.append({
                    "index": int(row["index"]),
                    "start": int(row["start"]),
                    "end": int(row["end"]),
                    "duration": int(row["duration"]),
                    "events": int(row["events"]) if row.get("events") else None,
                    "internal_power_w": float(row["internal_power_w"]),
                    "switching_power_w": float(row["switching_power_w"]),
                    "leakage_power_w": float(row["leakage_power_w"]),
                    "total_power_w": float(row["total_power_w"]),
                    "sequential_power_w": float(row.get("sequential_power_w") or 0.0),
                    "combinational_power_w": float(row.get("combinational_power_w") or 0.0),
                    "clock_power_w": float(row.get("clock_power_w") or 0.0),
                })
            except (ValueError, KeyError) as e:
                print(f"[warn] skipping malformed row: {row} ({e})",
                      file=sys.stderr)
    return rows


_UNIT_TO_SECONDS = {
    "s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15,
}


def parse_timescale(ts):
    """'1 ps' -> multiplier (in seconds) per raw VCD tick."""
    parts = ts.strip().split()
    if len(parts) != 2:
        raise ValueError(f"invalid timescale: {ts!r} (expected e.g. '1 ps')")
    mult = float(parts[0])
    unit = parts[1]
    if unit not in _UNIT_TO_SECONDS:
        raise ValueError(f"unknown timescale unit: {unit!r}")
    return mult * _UNIT_TO_SECONDS[unit]


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("csv", help="CSV produced by sweep_power.sh")
    ap.add_argument("-o", "--output", default=None,
                     help="Output HTML path (default: <csv>.html)")
    ap.add_argument("--x-axis", choices=["end", "duration", "index"],
                     default="end",
                     help="What to label the X axis with (default: end)")
    ap.add_argument("--timescale", default=None,
                     help="e.g. '1 ps' — converts X-axis labels from raw "
                          "VCD units to nanoseconds (same value shown by "
                          "'vcd_extract.py --info')")
    ap.add_argument("--no-open", action="store_true",
                     help="Don't try to auto-open the HTML in the "
                          "default browser after generating it")
    ap.add_argument("--y-min", type=float, default=None,
                     help="Manually fix the Y-axis minimum (W). If "
                          "omitted, the chart opens with an automatic "
                          "scale that skips the first --autoscale-skip "
                          "windows, with a button for 'full view'.")
    ap.add_argument("--y-max", type=float, default=None,
                     help="Manually fix the Y-axis maximum (W). See "
                          "--y-min.")
    ap.add_argument("--autoscale-skip", type=int, default=1,
                     help="How many initial windows to ignore when "
                          "computing the default Y scale (default: 1 — "
                          "skips the 1st window, typically the reset "
                          "transient, so it doesn't flatten the "
                          "variation in the rest). Use 0 to include all "
                          "windows in the default scale. A button in "
                          "the chart always lets you switch to the full "
                          "view.")
    args = ap.parse_args()

    rows = load_rows(args.csv)
    if not rows:
        print("[error] no valid rows in the CSV.", file=sys.stderr)
        sys.exit(1)
    rows.sort(key=lambda r: r["index"])

    import plotly.graph_objects as go

    x_raw = [r[args.x_axis] for r in rows]
    x_label = {"end": "Window end time",
               "duration": "Window duration",
               "index": "Iteration index"}[args.x_axis]

    x_display = x_raw
    if args.timescale and args.x_axis in ("end", "duration"):
        factor = parse_timescale(args.timescale)
        x_display = [v * factor * 1e9 for v in x_raw]  # -> ns
        x_label += " (ns)"
    elif args.x_axis in ("end", "duration"):
        x_label += " (raw VCD units)"

    # categorical: each window gets its own category on the X axis, so
    # bars stay evenly spaced even when windows aren't uniform (e.g.
    # overlap or gaps via --window-step)
    x_categories = [str(v) for v in x_display]

    windows = [f"[{r['start']}, {r['end']}]" for r in rows]
    events = [r["events"] if r["events"] is not None else "?" for r in rows]

    series = [
        ("Total", "total_power_w", "#d62728"),
        ("Sequential", "sequential_power_w", "#1f77b4"),
        ("Combinational", "combinational_power_w", "#2ca02c"),
        ("Clock", "clock_power_w", "#ff7f0e"),
    ]

    fig = go.Figure()
    for label, key, color in series:
        values = [r[key] for r in rows]
        fig.add_trace(go.Bar(
            x=x_categories,
            y=values,
            name=label,
            marker_color=color,
            customdata=list(zip(windows, events)),
            hovertemplate=(
                f"<b>{label}</b><br>"
                "Power: %{y:.6g} W<br>"
                "Window: %{customdata[0]}<br>"
                "Events: %{customdata[1]}"
                "<extra></extra>"
            ),
        ))

    fig.update_layout(
        barmode="group",
        title="Estimated power (OpenSTA) per VCD window — Total vs. groups",
        xaxis_title=x_label,
        yaxis_title="Power (W)",
        legend_title="Series (click to toggle)",
        template="plotly_white",
        hovermode="closest",
        bargap=0.15,
        bargroupgap=0.1,
    )
    fig.update_xaxes(type="category", tickangle=-45)

    # ---- Y-axis scale ----
    # Without this, a single spike (e.g. a reset transient in the first
    # window) dominates the auto range and flattens the real variation
    # in the rest of the windows. We compute two views and add buttons
    # to switch between them without regenerating the file.
    all_values = [v for _, key, _ in series for v in (r[key] for r in rows)]
    full_max = max(all_values) if all_values else 1.0
    full_range = [0, full_max * 1.08]

    skip_n = max(0, min(args.autoscale_skip, len(rows) - 1))
    zoomed_values = [
        r[key] for _, key, _ in series for r in rows[skip_n:]
    ] if skip_n < len(rows) else all_values
    if zoomed_values:
        z_min, z_max = min(zoomed_values), max(zoomed_values)
        pad = (z_max - z_min) * 0.15 or z_max * 0.05 or 1e-6
        zoomed_range = [max(0, z_min - pad), z_max + pad]
    else:
        zoomed_range = full_range

    if args.y_min is not None or args.y_max is not None:
        # explicit manual range: use it for both "states" (no toggle
        # button, since the user already picked what to see)
        y_lo = args.y_min if args.y_min is not None else 0
        y_hi = args.y_max if args.y_max is not None else full_max * 1.08
        fig.update_yaxes(range=[y_lo, y_hi])
    else:
        # open already zoomed in (skipping the first windows), with
        # buttons to switch to the full view or back
        fig.update_yaxes(range=zoomed_range)
        fig.update_layout(
            updatemenus=[
                dict(
                    type="buttons",
                    direction="right",
                    x=1.0, xanchor="right",
                    y=1.15, yanchor="top",
                    showactive=True,
                    buttons=[
                        dict(
                            label=f"Zoom (skips {skip_n} initial window(s))",
                            method="relayout",
                            args=[{"yaxis.range": zoomed_range}],
                        ),
                        dict(
                            label="Full view (includes initial spike)",
                            method="relayout",
                            args=[{"yaxis.range": full_range}],
                        ),
                        dict(
                            label="Log scale",
                            method="relayout",
                            args=[{"yaxis.type": "log"}],
                        ),
                        dict(
                            label="Linear scale",
                            method="relayout",
                            args=[{"yaxis.type": "linear", "yaxis.range": zoomed_range}],
                        ),
                    ],
                )
            ]
        )

    out = args.output or (args.csv.rsplit(".", 1)[0] + ".html")
    fig.write_html(out, include_plotlyjs="cdn", full_html=True)
    print(f"[ok] interactive chart saved to: {out}")
    print("     open it in a browser — hover the bars, click the legend "
          "to toggle series, drag to zoom.")
    if args.y_min is None and args.y_max is None:
        print("     Y scale opened in 'zoom' mode (skipping "
              f"{skip_n} initial window(s)) — use the buttons above the "
              "chart to switch to the full view, log scale, etc.")


if __name__ == "__main__":
    main()