#!/usr/bin/env python3
"""Summarise one experiment directory and append a row to results/summary.csv.

  python3 tests/analyze.py results/sdn_n10_bg12
Metrics: per-request latency (mean/p50/p95/max), submit latency, completion rate,
mean score, Jain's fairness index over per-student service rate (1 / mean latency).
"""
import csv
import glob
import os
import statistics
import sys


def jain(xs):
    sq = sum(x * x for x in xs)
    return (sum(xs) ** 2) / (len(xs) * sq) if xs and sq else 0.0


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p / 100 * len(xs)))] if xs else float("nan")


def main(run_dir):
    per_student, submit, done, students, alllat = {}, [], 0, 0, []
    for path in sorted(glob.glob(os.path.join(run_dir, "client_S*.csv"))):
        students += 1
        lat, finished = [], False
        with open(path) as f:
            for r in csv.DictReader(f):
                if r["event"] in ("question", "answer_ack") and r["latency_ms"]:
                    lat.append(float(r["latency_ms"]))
                    alllat.append(float(r["latency_ms"]))
                if r["event"] == "submit":
                    submit.append(float(r["latency_ms"]))
                if r["event"] in ("submit", "result_auto"):
                    finished = True
        done += finished
        if lat:
            per_student[os.path.basename(path)] = statistics.mean(lat)
    allv = list(per_student.values())
    scores = []
    rp = os.path.join(run_dir, "results.csv")
    if os.path.exists(rp):
        with open(rp) as f:
            scores = [int(r["score"]) for r in csv.DictReader(f)]
    row = {
        "label": os.path.basename(run_dir.rstrip("/")),
        "students": students,
        "completion_rate": round(done / students, 3) if students else 0,
        "mean_req_latency_ms": round(statistics.mean(allv), 2) if allv else "",
        "p95_student_mean_ms": round(pct(allv, 95), 2),
        "max_student_mean_ms": round(max(allv), 2) if allv else "",
        "p95_req_latency_ms": round(pct(alllat, 95), 2),
        "max_req_latency_ms": round(max(alllat), 2) if alllat else "",
        "mean_submit_ms": round(statistics.mean(submit), 2) if submit else "",
        "jain_fairness": round(jain([1.0 / x for x in allv]), 4),
        "mean_score": round(statistics.mean(scores), 2) if scores else "",
    }
    print(row)
    summary = os.path.join(os.path.dirname(os.path.abspath(run_dir)), "summary.csv")
    new = not os.path.exists(summary)
    with open(summary, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)


if __name__ == "__main__":
    main(sys.argv[1])
