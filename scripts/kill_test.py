"""C13: kill -9 the controller repeatedly during a run, restart it each time, and prove nothing broke.

Verifies from the simulator's own log and chunks, which is why this reads runs/ (the controller never does):
  1. no approval applied twice, no proposal id used twice;
  2. every approval the sim approved was applied, or has a recorded terminal outcome in the store (no lost approvals);
  3. counters in the store equal an independent recount of every chunk (no lost or doubled events);
  4. the governance boundary held: nothing outside allowed dims was auto-applied.

    python scripts/kill_test.py scenarios/public_02_incidents.yaml --kills 6 --out reports/crash_recovery.md
"""
import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

import httpx


def wait_up(url):
    for _ in range(600):
        try:
            return httpx.get(url + "/sim/state", timeout=2).json()
        except httpx.TransportError:
            time.sleep(0.5)
    raise SystemExit("simulator did not start")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--kills", type=int, default=6)
    ap.add_argument("--port", type=int, default=8740)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="reports/crash_recovery.md")
    a = ap.parse_args()
    rng = random.Random(a.seed)
    run, db = "runs/kill", "state/kill.db"
    shutil.rmtree(run, ignore_errors=True)
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    srv = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", run,
                            "--port", str(a.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{a.port}"
    kills = []
    try:
        st = wait_up(url)
        end = st["end_minute"]
        # kill points spread across the run
        points = sorted(rng.randrange(1, end) for _ in range(a.kills))
        cmd = [sys.executable, "-m", "controller", "--url", url, "--db", db]
        log = open("runs/kill.log", "w", encoding="utf-8")
        for target in points:
            ctl = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
            while True:
                m = httpx.get(url + "/sim/state", timeout=30).json()["sim_minute"]
                if m >= target or ctl.poll() is not None:
                    break
                time.sleep(0.01)
            ctl.kill()     # SIGKILL on POSIX, TerminateProcess on Windows: no handler runs
            ctl.wait()
            kills.append(m)
            print(f"killed at sim minute {m}", flush=True)
        rc = subprocess.call(cmd, stdout=log, stderr=subprocess.STDOUT)
        if rc != 0:
            raise SystemExit(f"final controller run exited {rc}")
    finally:
        srv.terminate()
        srv.wait()

    logrows = [json.loads(l) for l in open(f"{run}/sim_log.jsonl", encoding="utf-8") if l.strip()]
    applied = Counter(r["approval_id"] for r in logrows if r["kind"] == "approval_applied")
    requested = Counter(r["proposal_id"] for r in logrows if r["kind"] == "approval_requested")
    autos = Counter(r["proposal_id"] for r in logrows if r["kind"] == "auto_applied")
    approved = [r["approval_id"] for r in logrows if r["kind"] == "approval_decided" and r["status"] == "approved"]
    s = sqlite3.connect(db)
    outcomes = dict(s.execute("SELECT approval_id,status FROM proposals WHERE approval_id IS NOT NULL"))
    lost = [x for x in approved if x not in applied and outcomes.get(x) not in ("lapsed", "stale", "applied")]

    # independent recount of every chunk
    truth = Counter()
    for sub in ("history", "events"):
        for f in sorted(Path(run, sub).glob("*.jsonl")):
            for line in open(f, encoding="utf-8"):
                e = json.loads(line)
                if e["type"] == "call":
                    truth["dials"] += 1
                    truth["connected"] += bool(e["connected"])
                    truth["engaged"] += bool(e["engaged"])
                    truth["halluc"] += bool((e.get("qa") or {}).get("hallucination_flag"))
    mine = dict(zip(("dials", "connected", "engaged", "halluc"),
                    s.execute("SELECT SUM(dials),SUM(connected),SUM(engaged),SUM(halluc) FROM arm_day").fetchone()))
    checks = {
        "no approval applied twice": all(v == 1 for v in applied.values()),
        "no proposal id requested twice": all(v == 1 for v in requested.values()),
        "no auto proposal applied twice": all(v == 1 for v in autos.values()),
        "no lost approval": not lost,
        "no auto-apply refused by policy": not [r for r in logrows if r["kind"] == "auto_apply_rejected"],
        "store counters equal independent recount": mine == dict(truth),
    }
    Path(a.out).parent.mkdir(exist_ok=True)
    with open(a.out, "w", encoding="utf-8") as f:
        f.write(f"# Crash recovery evidence\n\nScenario `{a.scenario}`. Controller killed without warning at sim minutes "
                f"{kills}, then restarted with the same command and the same store. Run to scenario end.\n\n")
        f.write("| check | result |\n|---|---|\n")
        for k, v in checks.items():
            f.write(f"| {k} | {'PASS' if v else 'FAIL'} |\n")
        f.write(f"\nApprovals requested {sum(requested.values())}, approved {len(approved)}, applied {sum(applied.values())}; "
                f"auto-applied {sum(autos.values())}.\n\n")
        f.write(f"Counters in store: {mine}\n\nIndependent recount: {dict(truth)}\n\nReproduce: `python scripts/kill_test.py {a.scenario} --kills {a.kills}`\n")
    print(json.dumps(checks, indent=1))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
