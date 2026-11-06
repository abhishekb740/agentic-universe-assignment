"""The always-on loop. Everything it needs to resume lives in SQLite, so start and restart are one path."""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path

from . import policy as P
from .brain import WorkflowBrain
from .client import SimClient
from .governance import Governance, canon, changed_dims
from .ingest import Ingestor
from .store import Store


def open_store(path: Path, sim_state: dict) -> Store:
    """A store belongs to one simulator run. A clock that went backwards means a new run: keep the old file aside."""
    if path.exists():
        s = Store(path)
        same = s.kv_get("scenario") == sim_state["scenario"] and s.kv_get("last_minute", 0) <= sim_state["sim_minute"]
        if same:
            return s
        s.db.close()
        os.replace(path, f"{path}.{int(time.time())}.old")
        for ext in ("-wal", "-shm"):
            if Path(f"{path}{ext}").exists():
                os.remove(f"{path}{ext}")
    s = Store(path)
    s.kv_set("scenario", sim_state["scenario"])
    return s


class Controller:
    def __init__(self, url: str, db_path: str):
        self.c = SimClient(url)
        sim = self.c.state()
        self.s = open_store(Path(db_path), sim)
        origin = self.s.kv_get("origin_date")
        if origin is None:
            origin = (date.fromisoformat(sim["ts"][:10]) - timedelta(days=sim["sim_minute"] // 1440)).isoformat()
            self.s.kv_set("origin_date", origin)
        self.ing = Ingestor(self.s, self.c, origin)
        self.pub = {w["workflow_id"]: w for w in self.c.workflows()}
        self.gov = Governance(self.s, self.c, self.pub)
        self.end = sim["end_minute"]
        self.brains: dict[str, WorkflowBrain] = {}

    def run(self):
        print(f"controller up; store={self.s.path}", flush=True)
        while True:
            t0 = time.perf_counter()
            sim = self.c.state()
            now, ts = sim["sim_minute"], sim["ts"]
            self.s.kv_set("last_minute", now)
            n_chunks, n_events = self.ing.run()
            t1 = time.perf_counter()
            self.tick(now, ts, sim["credits_remaining"])
            t2 = time.perf_counter()
            if sim["finished"]:
                self.s.decide(now, ts, None, "finished", "scenario finished; final state reconciled", sim)
                print(f"finished at {ts}; credits left {sim['credits_remaining']}", flush=True)
                return
            code, res = self.c.advance(min(P.STEP_MINUTES, sim["end_minute"] - now))
            t3 = time.perf_counter()
            if code not in (200, 409):
                self.s.alert(now, ts, "high", None, f"advance failed {code}: {res}", "will retry")
            self.s.db.execute(
                "INSERT INTO metrics(minute,tick_ms,ingest_ms,decide_ms,advance_ms,events,credits) VALUES(?,?,?,?,?,?,?)",
                (now, int((t3 - t0) * 1e3), int((t1 - t0) * 1e3), int((t2 - t1) * 1e3), int((t3 - t2) * 1e3), n_events,
                 sim["credits_remaining"]))
            if now % 1440 == 0:
                print(f"{ts} events={n_events} tick_ms={int((t3 - t0) * 1e3)} credits={sim['credits_remaining']}", flush=True)

    def tick(self, now: int, ts: str, credits: float):
        cfgs = {wf: self.c.config(wf) for wf in self.pub}
        for wf, pub in self.pub.items():
            brain = self.brains.get(wf)
            if brain is None:
                brain = self.brains[wf] = WorkflowBrain(self.s, self.c, self.gov, pub, self.end)
                brain.boot(cfgs[wf]["config"], now, ts)
        self._inbox(cfgs, now, ts)
        for wf in self.pub:
            self._workflow(wf, now, ts, credits)

    def _inbox(self, cfgs, now, ts):
        rows = self.s.db.execute("SELECT event_id,body FROM inbox WHERE done=0 ORDER BY event_id").fetchall()
        for event_id, body in rows:
            ev = json.loads(body)
            wf = ev.get("workflow_id")
            if ev["type"] == "config_changed" and ev.get("actor") == "supervisor" and wf in self.brains:
                self.brains[wf].on_supervisor_edit(ev, cfgs[wf]["config"], now, ts)
            self.s.db.execute("UPDATE inbox SET done=1 WHERE event_id=?", (event_id,))

    def _workflow(self, wf: str, now: int, ts: str, credits: float):
        brain, gov, pub = self.brains[wf], self.gov, self.pub[wf]
        gov.reconcile(wf, now, ts)
        cur_full = self.c.config(wf)
        base, cfg = cur_full["version"], cur_full["config"]
        hour = (now % 1440) // 60
        remaining_days = (self.end - now) / 1440

        brain.daily_look(now, ts)
        did_urgent = brain.check_did(cfg, now, ts)
        incident = brain.check_prompt(cfg, now, ts, credits)

        open_ = gov.open_proposals(wf)
        sent, hard_cap = gov.requests_last_24h(wf, now), gov.caps(wf)[0]
        routine_cap = gov.caps(wf)[1]

        # 1. safety first: a bad prompt goes to a human at once, whatever else is queued
        if incident and incident.get("target") and not any(p["kind"] == "prompt_fix" for p in open_):
            if sent < hard_cap:
                new = copy.deepcopy(cfg)
                new["prompt_version"] = incident["target"]
                d = brain.describe(cfg, new)
                gov.submit_approval(
                    wf, "prompt_fix", base, cfg, new, now, ts,
                    f"SAFETY: replace prompt {cfg['prompt_version']} with {incident['target']} (hallucination incident).",
                    f"Prompt {cfg['prompt_version']} is failing: {incident['reason']}. {incident['target']} chosen as "
                    f"{incident.get('how')}, validated above the stricter internal bar of {P.STRICT_PASS_RATE}. This reverses a "
                    "supervisor's edit, so it needs a named human decision and is not applied automatically.",
                    d["dry_run"], d["affected_leads"], d["impact"],
                    {"validation_run_id": incident["validation_run_id"], "incident": incident["reason"]})
                sent += 1
                open_ = gov.open_proposals(wf)
            else:
                self.s.alert(now, ts, "high", wf, "prompt fix blocked by request cap", "call the approver; fix is validated and ready")

        # 2. caller IDs: auto-apply where policy allows it, and never collide with a pending approval unless urgent
        pol = pub["governance"]["auto_apply"]
        target = brain.did_target(cfg)
        cur_did = canon(cfg)["allocations"]["default"]["caller_id"]
        wanted = canon({"allocations": {"default": {"caller_id": target}}})["allocations"]["default"]["caller_id"]
        did_auto = pol.get("enabled") and "caller_id" in pol.get("allowed_dims", [])
        has_flagged = any(d in cur_did for d in brain.st["did"]["bad"])
        if did_auto and wanted != cur_did and remaining_days > 0.5 and (not open_ or has_flagged or did_urgent):
            step = brain.did_step(cfg["allocations"]["default"]["caller_id"], target, pol["max_shift"])
            new = copy.deepcopy(cfg)
            new["allocations"]["default"]["caller_id"] = step
            if canon(new) != canon(cfg) and not any(p["route"] == "auto" for p in open_):
                gov.auto_apply(wf, "did_rotation", base, cfg, new, now, ts,
                               f"Caller rotation toward {wanted}: flagged {brain.st['did']['bad']}, probing "
                               f"{brain.st['did']['probing']}. Within auto policy (max_shift {pol['max_shift']}); "
                               "reported in the weekly summary.",
                               {"did_state": brain.st["did"]})
                cur_full = self.c.config(wf)
                base, cfg = cur_full["version"], cur_full["config"]

        # 3. routine work: one batched request, in the approver's day, one slot always left free for incidents
        open_ = gov.open_proposals(wf)
        desired = brain.desired(cfg)
        if did_auto:
            desired["allocations"]["default"]["caller_id"] = copy.deepcopy(cfg["allocations"]["default"]["caller_id"])
        wants = changed_dims(cfg, desired)
        if not wants or open_ or incident:
            return
        urgent_did = did_urgent or (not did_auto and has_flagged)
        allowed = sent < (hard_cap if urgent_did else routine_cap)
        if not allowed or not gov.routine_window(wf, hour) or gov.cooling_down(wf, now) or remaining_days < 0.5:
            return
        d = brain.describe(cfg, desired)
        summary, why = brain.rationale(cfg, desired, [])
        gov.submit_approval(wf, "batched", base, cfg, desired, now, ts, summary, why, d["dry_run"], d["affected_leads"],
                            d["impact"], {"rules": brain.st["rules"], "alive": brain.st["alive"], "did": brain.st["did"],
                                          "schedule_basis": (brain.st["schedule"] or {}).get("chi2_per_df")})


def main():
    ap = argparse.ArgumentParser(description="Outcome-first agent loop controller")
    ap.add_argument("--url", required=True)
    ap.add_argument("--db", default=os.environ.get("CONTROLLER_DB", "state/controller.db"))
    a = ap.parse_args()
    Controller(a.url, a.db).run()


if __name__ == "__main__":
    sys.exit(main())
