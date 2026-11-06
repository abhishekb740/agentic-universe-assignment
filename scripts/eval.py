"""Development harness: run the simulator, the controller and the grader for one scenario.

This file reads runs/ to produce grades. The controller package never does.
    python scripts/eval.py scenarios/public_01_dev.yaml --name a01 [--baseline runs/base01]
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

os.environ["PYTHONUTF8"] = "1"     # the grader writes non-ASCII; needed on Windows only


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario")
    ap.add_argument("--name", required=True)
    ap.add_argument("--baseline")
    ap.add_argument("--port", type=int, default=8720)
    ap.add_argument("--out")
    a = ap.parse_args()
    run = f"runs/{a.name}"
    db = f"state/{a.name}.db"
    shutil.rmtree(run, ignore_errors=True)
    for f in (db, db + "-wal", db + "-shm"):
        if os.path.exists(f):
            os.remove(f)
    srv = subprocess.Popen([sys.executable, "-m", "sim", "serve", "--scenario", a.scenario, "--run-dir", run,
                            "--port", str(a.port)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{a.port}"
    try:
        for _ in range(600):
            if srv.poll() is not None:
                raise SystemExit(f"simulator exited {srv.returncode}")
            try:
                httpx.get(url + "/sim/state", timeout=2)
                break
            except httpx.TransportError:
                time.sleep(0.5)
        t0 = time.time()
        log = open(f"runs/{a.name}.log", "w", encoding="utf-8")
        rc = subprocess.call([sys.executable, "-m", "controller", "--url", url, "--db", db], stdout=log, stderr=subprocess.STDOUT)
        print(f"controller exit {rc} in {time.time() - t0:.1f}s")
    finally:
        srv.terminate()
        srv.wait()
    if a.baseline:
        if not os.path.exists(a.baseline):
            subprocess.check_call([sys.executable, "-m", "sim", "baseline", "--scenario", a.scenario, "--run-dir", a.baseline],
                                  stdout=subprocess.DEVNULL)
        out = a.out or f"runs/{a.name}.md"
        subprocess.check_call([sys.executable, "-m", "sim", "grade", "--run", run, "--baseline", a.baseline, "--out", out])


if __name__ == "__main__":
    main()
