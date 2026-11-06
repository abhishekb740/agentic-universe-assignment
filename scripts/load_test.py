"""Measure the controller on public_03_load, using the controller's own metrics table.

Starts the simulator and the controller, samples their memory, then reports wall-clock per simulated day.
    python scripts/load_test.py --out reports/public_03_load.md
"""
import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import psutil  # measurement only; the controller itself does not depend on it


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="scenarios/public_03_load.yaml")
    ap.add_argument("--port", type=int, default=8750)
    ap.add_argument("--out", default="reports/public_03_load.md")
    ap.add_argument("--note", default="")
    a = ap.parse_args()
    run, db = "runs/load", "state/load.db"
    shutil.rmtree(run, ignore_errors=True)
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    t_boot = time.time()
    srv = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", run,
                            "--port", str(a.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{a.port}"
    for _ in range(2400):
        try:
            httpx.get(url + "/sim/state", timeout=5)
            break
        except httpx.TransportError:
            time.sleep(0.5)
    sim_boot = time.time() - t_boot
    t0 = time.time()
    ctl = subprocess.Popen([sys.executable, "-m", "controller", "--url", url, "--db", db],
                           stdout=open("runs/load.log", "w", encoding="utf-8"), stderr=subprocess.STDOUT)
    peak = {"controller": 0, "simulator": 0}
    # Pin both to 4 logical CPUs, to match the 4 vCPU budget as closely as a shared host allows.
    for pid in (ctl.pid, srv.pid):
        psutil.Process(pid).cpu_affinity(list(range(4)))

    def sample():
        pc, ps = psutil.Process(ctl.pid), psutil.Process(srv.pid)
        while ctl.poll() is None:
            try:
                peak["controller"] = max(peak["controller"], pc.memory_info().rss)
                peak["simulator"] = max(peak["simulator"], ps.memory_info().rss)
            except psutil.Error:
                pass
            time.sleep(1)

    threading.Thread(target=sample, daemon=True).start()
    rc = ctl.wait()
    wall = time.time() - t0
    srv.terminate()
    srv.wait()

    s = sqlite3.connect(db)
    rows = s.execute("SELECT minute/1440,SUM(tick_ms),SUM(ingest_ms),SUM(decide_ms),SUM(advance_ms),SUM(events) "
                     "FROM metrics GROUP BY 1 ORDER BY 1").fetchall()
    worst = s.execute("SELECT MAX(tick_ms-advance_ms),MAX(decide_ms) FROM metrics WHERE minute>0").fetchone()
    Path(a.out).parent.mkdir(exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(f"# Load test: {a.scenario}\n\n{a.note}\n\nController exit code {rc}. Simulator boot {sim_boot:.0f}s. "
                f"Total controller wall-clock {wall:.0f}s. Peak RSS: controller {peak['controller'] / 2**30:.2f} GiB, "
                f"simulator {peak['simulator'] / 2**30:.2f} GiB.\n\n")
        f.write("Day -1 is not a sim day: the first row includes the history bootstrap.\n\n")
        f.write("| sim day | wall s (total) | ingest s | decide s | sim advance s | events | controller events/s |\n|---|---|---|---|---|---|---|\n")
        for d, tick, ing, dec, adv, ev in rows:
            own = (ing + dec) / 1000
            f.write(f"| {d} | {tick / 1000:.0f} | {ing / 1000:.1f} | {dec / 1000:.1f} | {adv / 1000:.1f} | {ev} | "
                    f"{ev / own if own else 0:.0f} |\n")
        f.write(f"\nWorst single tick of controller work (ingest + decide, excluding the sim's advance): {worst[0] / 1000:.1f}s; "
                f"worst decide phase {worst[1] / 1000:.1f}s. A tick is one simulated hour.\n")
    print(Path(a.out).read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
