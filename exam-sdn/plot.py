#!/usr/bin/env python3
"""Average the repeated runs in results/summary.csv and draw baseline-vs-SDN charts.

  python3 tests/plot.py
Needs matplotlib:  sudo apt install -y python3-matplotlib
Outputs: results/plots/*.png, results/summary_table.csv, results/summary_table.md
"""
import csv, os, re, statistics, sys
from collections import defaultdict

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RES = os.path.join(ROOT, "results")
PAT = re.compile(r"^(baseline|sdn)_(fail_)?n(\d+)(?:_bg(\d+))?_r(\d+)$")
METRICS = ["mean_req_latency_ms", "p95_req_latency_ms", "max_req_latency_ms", "jain_fairness", "completion_rate"]
COLORS = {"baseline": "#c0504d", "sdn": "#1f5fa8"}


def load():
    last = {}
    with open(os.path.join(RES, "summary.csv")) as f:
        for r in csv.DictReader(f):
            last[r["label"]] = r                     # a re-run replaces the earlier row
    groups = defaultdict(lambda: defaultdict(list))  # (scenario, n, bg) -> mode -> [rows]
    for label, r in last.items():
        m = PAT.match(label)
        if m and r.get("p95_req_latency_ms") not in (None, ""):
            mode, fail, n, bg, _ = m.groups()
            groups[("fail" if fail else "load", int(n), int(bg or 0))][mode].append(r)
    return groups


def avg(rows, key):
    v = [float(r[key]) for r in rows if r.get(key) not in (None, "")]
    return statistics.mean(v) if v else float("nan")


def bars(ax, cats, series, ylabel, log=False, ylim=None):
    w, xs = 0.38, range(len(cats))
    for i, (mode, vals) in enumerate(series.items()):
        pos = [x + (i - 0.5) * w for x in xs]
        b = ax.bar(pos, vals, w, label=mode, color=COLORS[mode])
        for rect, v in zip(b, vals):
            if v == v:
                ax.annotate("%.3g" % v, (rect.get_x() + w / 2, rect.get_height()), ha="center", va="bottom", fontsize=8)
    ax.set_xticks(list(xs)); ax.set_xticklabels(cats); ax.set_ylabel(ylabel); ax.legend()
    if log: ax.set_yscale("log")
    if ylim: ax.set_ylim(*ylim)


def main():
    try:
        import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    except ImportError:
        sys.exit("matplotlib missing: sudo apt install -y python3-matplotlib")
    if not os.path.exists(os.path.join(RES, "summary.csv")):
        sys.exit("results/summary.csv not found - run tests/analyze.py or tests/run_matrix.py first")
    g = load()
    os.makedirs(os.path.join(RES, "plots"), exist_ok=True)

    # ---- table of averages ----
    table = []
    for (sc, n, bg), modes in sorted(g.items()):
        for mode, rows in sorted(modes.items()):
            table.append([sc, n, bg, mode, len(rows)] + [round(avg(rows, k), 3) for k in METRICS])
    hdr = ["scenario", "students", "bg_mbps", "mode", "runs"] + METRICS
    with open(os.path.join(RES, "summary_table.csv"), "w", newline="") as f:
        w = csv.writer(f); w.writerow(hdr); w.writerows(table)
    with open(os.path.join(RES, "summary_table.md"), "w") as f:
        f.write("| " + " | ".join(hdr) + " |\n|" + "---|" * len(hdr) + "\n")
        for r in table:
            f.write("| " + " | ".join(str(x) for x in r) + " |\n")

    # ---- load scenarios: one chart per metric, panels per background load ----
    loads = sorted({bg for (sc, n, bg) in g if sc == "load"})
    studs = sorted({n for (sc, n, bg) in g if sc == "load"})
    specs = [("mean_req_latency_ms", "Mean request latency (ms, log scale)", True, None),
             ("p95_req_latency_ms", "95th-percentile request latency (ms, log scale)", True, None),
             ("jain_fairness", "Jain's fairness index (1 = perfectly fair)", False, (0, 1.1))]
    for key, ylabel, log, ylim in specs:
        if not loads:
            break
        fig, axes = plt.subplots(1, len(loads), figsize=(5.2 * len(loads), 4.2), squeeze=False, sharey=True)
        for ax, bg in zip(axes[0], loads):
            series = {m: [avg(g.get(("load", n, bg), {}).get(m, []), key) for n in studs] for m in ("baseline", "sdn")}
            bars(ax, ["%d students" % n for n in studs], series, ylabel, log, ylim)
            ax.set_title("Background load: %d Mbps" % bg if bg else "No background load")
        fig.tight_layout(); fig.savefig(os.path.join(RES, "plots", key + ".png"), dpi=150); plt.close(fig)

    # ---- link-failure scenario ----
    fail = [(k, v) for k, v in g.items() if k[0] == "fail"]
    if fail:
        (_, n, _), modes = fail[0]
        keys = ["mean_req_latency_ms", "p95_req_latency_ms", "max_req_latency_ms"]
        fig, ax = plt.subplots(figsize=(6.5, 4.2))
        bars(ax, ["mean", "95th percentile", "worst request"],
             {m: [avg(modes.get(m, []), k) for k in keys] for m in ("baseline", "sdn")}, "Request latency (ms, log scale)", True)
        ax.set_title("Link s1-s4 fails mid-exam (%d students)" % n)
        fig.tight_layout(); fig.savefig(os.path.join(RES, "plots", "link_failure.png"), dpi=150); plt.close(fig)
    print("wrote", os.path.join(RES, "plots"), "and summary_table.csv / summary_table.md")
    print(open(os.path.join(RES, "summary_table.md")).read())


if __name__ == "__main__":
    main()
