#!/usr/bin/env python3
"""Run the full evaluation matrix (baseline vs SDN) and plot the results.

  sudo python3 tests/run_matrix.py --quick                      # ~10 min sanity pass
  sudo python3 tests/run_matrix.py --students 5 10 20 --bg 0 12 --reps 2 --failure   # full set

The script starts/stops its own controller for each mode (stop your manual one first).
Finished runs are skipped, so it can be restarted after an interruption (use --redo to repeat).
Labels: <mode>_n<students>_bg<Mbps>_r<rep>  and  <mode>_fail_n<students>_r<rep>
"""
import argparse, csv, os, subprocess, sys, time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEVNULL = subprocess.DEVNULL


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def cleanup():
    subprocess.run(["mn", "-c"], stdout=DEVNULL, stderr=DEVNULL)


def start_ctrl(mode, ryu):
    d = os.path.join(ROOT, "results", "ctrl_" + mode)
    os.makedirs(d, exist_ok=True)
    p = subprocess.Popen([ryu, "--observe-links", "controller/exam_controller.py"], cwd=ROOT,
                         env=dict(os.environ, EXAM_MODE=mode, EXAM_RESULTS_DIR=d),
                         stdout=open(os.path.join(d, "controller.log"), "w"), stderr=subprocess.STDOUT)
    time.sleep(6)
    if p.poll() is not None:
        sys.exit("controller did not start - see %s/controller.log" % d)
    return p


def stop_ctrl(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


def save_events(mode, label, t0):
    """Copy this run's controller events; drop the Mininet teardown noise after APP_END."""
    src = os.path.join(ROOT, "results", "ctrl_" + mode, "sdn_events.csv")
    if not os.path.exists(src):
        return
    rows = list(csv.reader(open(src)))
    out = [rows[0]]
    for r in rows[1:]:
        if len(r) >= 2 and float(r[0]) >= t0:
            out.append(r)
            if r[1] == "APP_END":
                break
    csv.writer(open(os.path.join(ROOT, "results", label, "sdn_events.csv"), "w", newline="")).writerows(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--students", type=int, nargs="+", default=[5, 10, 20])
    p.add_argument("--bg", type=int, nargs="+", default=[0, 12], help="UDP background load(s) in Mbps")
    p.add_argument("--reps", type=int, default=2)
    p.add_argument("--failure", action="store_true", help="also run the link-failure scenario (10 students)")
    p.add_argument("--quick", action="store_true", help="10 students, bg 0 and 12, 1 rep, with failure")
    p.add_argument("--redo", action="store_true", help="repeat runs that already have results")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--timeout", type=int, default=420, help="seconds allowed per run")
    default_ryu = os.path.join("/home", os.environ.get("SUDO_USER", ""), "ryu-env", "bin", "ryu-manager")
    p.add_argument("--ryu", default=default_ryu, help="path to ryu-manager (default: ~/ryu-env/bin/ryu-manager)")
    a = p.parse_args()
    if a.quick:
        a.students, a.bg, a.reps, a.failure = [10], [0, 12], 1, True
    if os.geteuid() != 0 and not a.dry_run:
        sys.exit("run with sudo (Mininet needs root)")

    jobs = []   # (suffix, extra args)
    for n in a.students:
        for bg in a.bg:
            jobs.append(("n%d_bg%d" % (n, bg), ["--students", str(n), "--bg", str(bg)]))
    if a.failure:
        jobs.append(("fail_n10", ["--students", "10", "--redundant", "--think", "4", "--fail-at", "10",
                                  "--restore-after", "20", "--duration", "90"]))
    total = 2 * len(jobs) * a.reps
    log("%d runs planned (%d conditions x %d reps x 2 modes)" % (total, len(jobs), a.reps))
    if a.dry_run:
        for mode in ("baseline", "sdn"):
            for suffix, extra in jobs:
                for rep in range(1, a.reps + 1):
                    print("%s_%s_r%d: run_experiment.py %s" % (mode, suffix, rep, " ".join(extra)))
        return

    subprocess.run(["pkill", "-f", "ryu-manager"], stdout=DEVNULL, stderr=DEVNULL)
    cleanup()
    done = 0
    for mode in ("baseline", "sdn"):
        ctrl = start_ctrl(mode, a.ryu)
        log("controller started in %s mode" % mode)
        for suffix, extra in jobs:
            for rep in range(1, a.reps + 1):
                label = "%s_%s_r%d" % (mode, suffix, rep)
                out = os.path.join(ROOT, "results", label)
                done += 1
                if os.path.exists(os.path.join(out, "results.csv")) and not a.redo:
                    log("[%d/%d] %s already done, skipping" % (done, total, label))
                    continue
                for attempt in (1, 2):
                    log("[%d/%d] %s (attempt %d)" % (done, total, label, attempt))
                    os.makedirs(out, exist_ok=True)
                    t0 = time.time()
                    try:
                        subprocess.run([sys.executable, "tests/run_experiment.py"] + extra + ["--label", label], cwd=ROOT,
                                       stdout=open(os.path.join(out, "run.log"), "w"), stderr=subprocess.STDOUT, timeout=a.timeout)
                    except subprocess.TimeoutExpired:
                        log("  timed out")
                    if os.path.exists(os.path.join(out, "results.csv")):
                        subprocess.run([sys.executable, "tests/analyze.py", "results/" + label], cwd=ROOT, stdout=DEVNULL)
                        save_events(mode, label, t0)
                        break
                    log("  run failed - cleaning up and restarting the controller")
                    stop_ctrl(ctrl); cleanup(); ctrl = start_ctrl(mode, a.ryu)
        stop_ctrl(ctrl)
        cleanup()
    user = os.environ.get("SUDO_USER")
    if user:
        subprocess.run(["chown", "-R", "%s:%s" % (user, user), os.path.join(ROOT, "results")])
    log("all runs finished. Now run:  python3 tests/plot.py")


if __name__ == "__main__":
    main()
