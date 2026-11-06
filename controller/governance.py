"""C9: the governance boundary. Write-ahead proposals, reconcile-on-every-tick, no double apply.

Sequence for an approval: commit an intent row with a deterministic proposal_id, then POST. If we
die between the two, the next tick sees an intent, looks for that id on the server, and either
adopts what is there or posts it. The server's duplicate check backs this up. Apply is safe to
repeat for the same reason: an applied approval answers 409 not_approved and we re-read its status.
"""
from __future__ import annotations

import copy
import hashlib
import json

from . import policy as P

OPEN = ("intent", "pending", "approved")
CUSTOMER_FACING = ("cohorts", "welcome_message", "voice", "prompt_version", "schedule")


def canon(cfg: dict) -> dict:
    """Round weights, so that a config never differs from itself by float noise after normalisation."""
    c = copy.deepcopy(cfg)
    for dims in c["allocations"].values():
        for dim, w in dims.items():
            s = sum(w.values()) or 1
            dims[dim] = {k: round(v / s, 3) for k, v in sorted(w.items()) if v > 0}
    return c


def config_hash(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(canon(cfg), sort_keys=True).encode()).hexdigest()[:12]


def _dist(cfg, cohort, dim):
    return (cfg["allocations"].get(cohort) or {}).get(dim) or cfg["allocations"]["default"][dim]


def changed_dims(old: dict, new: dict) -> list[str]:
    a, b = canon(old), canon(new)
    out = []
    if a["cohorts"] != b["cohorts"]:
        out.append("cohorts")
    keys = set(a["allocations"]) | set(b["allocations"])
    for dim in ("welcome_message", "voice", "caller_id"):
        if any(_dist(a, k, dim) != _dist(b, k, dim) for k in keys
               if (k == "default" or k in {c["id"] for c in a["cohorts"]} | {c["id"] for c in b["cohorts"]})):
            out.append(dim)
    if a["prompt_version"] != b["prompt_version"]:
        out.append("prompt_version")
    if a["schedule"] != b["schedule"]:
        out.append("schedule")
    return out


def make_diff(old: dict, new: dict) -> dict:
    a, b = canon(old), canon(new)
    diff = {}
    for key in sorted(set(a["allocations"]) | set(b["allocations"])):
        for dim in ("welcome_message", "voice", "caller_id"):
            x, y = (a["allocations"].get(key) or {}).get(dim), (b["allocations"].get(key) or {}).get(dim)
            if x != y:
                diff[f"allocations.{key}.{dim}"] = {"from": x, "to": y}
    if a["prompt_version"] != b["prompt_version"]:
        diff["prompt_version"] = {"from": a["prompt_version"], "to": b["prompt_version"]}
    ca, cb = {c["id"] for c in a["cohorts"]}, {c["id"] for c in b["cohorts"]}
    if ca != cb:
        diff["cohorts"] = {"added": sorted(cb - ca), "removed": sorted(ca - cb)}
    ha, hb = a["schedule"]["hour_weights"], b["schedule"]["hour_weights"]
    changed_hw = sorted(k for k in set(ha) | set(hb) if ha.get(k) != hb.get(k))
    if changed_hw:
        diff["schedule.hour_weights"] = {"cohorts_changed": changed_hw}
    return diff


class Governance:
    def __init__(self, store, client, wf_pub: dict[str, dict]):
        self.s, self.c, self.pub = store, client, wf_pub

    # ---- bookkeeping
    def _rows(self, wf: str, statuses) -> list[dict]:
        q = ",".join("?" * len(statuses))
        cols = ("proposal_id", "wf", "kind", "route", "status", "base_version", "config_hash", "payload",
                "approval_id", "created_minute")
        cur = self.s.db.execute(
            f"SELECT {','.join(cols)} FROM proposals WHERE wf=? AND status IN ({q}) ORDER BY created_minute",
            (wf, *statuses))
        return [dict(zip(cols, r)) for r in cur]

    def open_proposals(self, wf: str) -> list[dict]:
        return self._rows(wf, OPEN)

    def _set(self, pid: str, now: int, status: str, **kw):
        sets = ["status=?", "updated_minute=?"] + [f"{k}=?" for k in kw]
        self.s.db.execute(f"UPDATE proposals SET {','.join(sets)} WHERE proposal_id=?",
                          (status, now, *kw.values(), pid))

    def requests_last_24h(self, wf: str, now: int) -> int:
        return self.s.db.execute(
            "SELECT COUNT(*) FROM proposals WHERE wf=? AND route='approval' AND status NOT IN ('intent','refused') "
            "AND created_minute>?", (wf, now - 1440)).fetchone()[0]

    def caps(self, wf: str) -> tuple[int, int]:
        """(hard cap from the contract, soft cap for routine work)."""
        pub = self.pub[wf]
        hard = pub["governance"]["max_requests_per_24h"]
        pref = P.TENANT_PREFS.get(pub["tenant_id"], {}).get("max_requests_per_24h", hard)
        # One slot a day stays free for incidents, so routine work can never lock a safety fix out.
        return hard, max(1, min(hard, pref) - 1)

    def routine_window(self, wf: str, hour: int) -> bool:
        pub = self.pub[wf]
        lo, hi = pub["governance"]["approver_hours"]
        lo = max(lo, P.TENANT_PREFS.get(pub["tenant_id"], {}).get("earliest_hour", lo))
        return lo <= hour < hi

    def cooling_down(self, wf: str, now: int) -> bool:
        row = self.s.db.execute(
            "SELECT MAX(updated_minute) FROM proposals WHERE wf=? AND status='rejected'", (wf,)).fetchone()
        return row[0] is not None and now - row[0] < P.REJECT_COOLDOWN_MIN

    def _new_id(self, wf: str, base: int, h: str) -> str:
        n = self.s.db.execute("SELECT COUNT(*) FROM proposals WHERE wf=? AND config_hash=?", (wf, h)).fetchone()[0]
        return f"{wf}-v{base}-{h}-{n}"

    # ---- submit
    def submit_approval(self, wf: str, kind: str, base: int, cur_cfg: dict, new_cfg: dict, now: int, ts: str,
                        summary: str, rationale: str, dry_run: dict, affected: int, impact: float, evidence: dict):
        h = config_hash(new_cfg)
        pid = self._new_id(wf, base, h)
        body = {"proposal_id": pid, "workflow_id": wf, "base_version": base, "proposed_config": canon(new_cfg),
                "change_summary": summary, "diff": make_diff(cur_cfg, new_cfg), "affected_leads": int(affected),
                "dry_run": dry_run, "impact_score": round(min(max(impact, 0.0), 1.0), 3),
                "rationale": rationale, "evidence": evidence}
        self.s.db.execute(
            "INSERT INTO proposals(proposal_id,wf,kind,route,status,base_version,config_hash,payload,created_minute,updated_minute)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, wf, kind, "approval", "intent", base, h, json.dumps(body), now, now))
        self.s.decide(now, ts, wf, "proposal_intent", summary,
                      {"kind": kind, "diff": body["diff"], "dry_run": dry_run, "affected_leads": affected,
                       "impact_score": body["impact_score"], "rationale": rationale, "evidence": evidence}, pid)
        self._post_intent(wf, pid, body, now, ts)
        return pid

    def _post_intent(self, wf, pid, body, now, ts):
        code, res = self.c.request_approval(body)
        if code == 200:
            self._set(pid, now, res["status"], approval_id=res["approval_id"])
            self.s.decide(now, ts, wf, "proposal_submitted", f"approval {res['approval_id']} requested", res, pid)
        elif code == 409 and res.get("error") == "duplicate_proposal_id":
            self._adopt(wf, pid, now, ts)
        elif code == 409:
            self._set(pid, now, "stale", reasons=json.dumps(res))
            self.s.decide(now, ts, wf, "proposal_stale", "base version moved before submission", res, pid)
        else:
            self._set(pid, now, "refused", reasons=json.dumps(res))
            self.s.alert(now, ts, "high", wf, f"approval request refused {code}: {res}", "fix contract bug; nothing changed")

    def _adopt(self, wf, pid, now, ts):
        for a in self.c.approvals(wf):
            if a["proposal_id"] == pid:
                self._set(pid, now, a["status"], approval_id=a["approval_id"])
                self.s.decide(now, ts, wf, "proposal_reconciled", f"found {a['approval_id']} ({a['status']})", a, pid)
                return True
        return False

    def auto_apply(self, wf: str, kind: str, base: int, cur_cfg, new_cfg, now: int, ts: str, rationale: str, evidence):
        h = config_hash(new_cfg)
        pid = self._new_id(wf, base, h)
        body = {"proposal_id": pid, "base_version": base, "proposed_config": canon(new_cfg), "rationale": rationale,
                "evidence": evidence}
        self.s.db.execute(
            "INSERT INTO proposals(proposal_id,wf,kind,route,status,base_version,config_hash,payload,created_minute,updated_minute)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)", (pid, wf, kind, "auto", "intent", base, h, json.dumps(body), now, now))
        self.s.decide(now, ts, wf, "auto_intent", rationale, {"diff": make_diff(cur_cfg, new_cfg), "evidence": evidence}, pid)
        self._put_auto(wf, pid, body, now, ts)

    def _put_auto(self, wf, pid, body, now, ts):
        code, res = self.c.auto_apply(wf, body)
        if code == 200:
            self._set(pid, now, "applied", applied_version=res["version"])
            self.s.decide(now, ts, wf, "auto_applied", f"applied as v{res['version']} under auto policy", res, pid)
        elif code == 409 and res.get("error") == "duplicate_proposal_id":
            self._adopt_auto(wf, pid, now, ts)
        elif code == 409:
            self._set(pid, now, "stale", reasons=json.dumps(res))
        else:
            self._set(pid, now, "refused", reasons=json.dumps(res))
            self.s.alert(now, ts, "high", wf, f"auto-apply refused {code}: {res}", "stays unchanged; check allowed_dims")

    def _adopt_auto(self, wf, pid, now, ts):
        for v in self.c.history(wf):
            if v["proposal_id"] == pid:
                self._set(pid, now, "applied", applied_version=v["version"])
                self.s.decide(now, ts, wf, "auto_reconciled", f"already applied as v{v['version']}", v, pid)
                return True
        self._set(pid, now, "stale", reasons=json.dumps({"lost": "not found on server after duplicate"}))
        return False

    # ---- reconcile: called every tick, and is the whole of crash recovery
    def reconcile(self, wf: str, now: int, ts: str) -> list[dict]:
        rows = self.open_proposals(wf)
        events = []
        if not rows:
            return events
        server = {a["proposal_id"]: a for a in self.c.approvals(wf)}
        for p in rows:
            pid = p["proposal_id"]
            body = json.loads(p["payload"])
            if p["route"] == "auto":
                seen = next((v for v in self.c.history(wf) if v["proposal_id"] == pid), None)
                if seen:
                    self._set(pid, now, "applied", applied_version=seen["version"])
                    self.s.decide(now, ts, wf, "auto_reconciled", f"applied as v{seen['version']} before restart", seen, pid)
                else:
                    self._put_auto(wf, pid, body, now, ts)
                continue
            a = server.get(pid)
            if a is None:
                # intent never reached the server: send it now, same id
                self._post_intent(wf, pid, body, now, ts)
                a = {x["proposal_id"]: x for x in self.c.approvals(wf)}.get(pid)
                if a is None:
                    continue
            status = a["status"]
            if p["approval_id"] is None:
                self._set(pid, now, status, approval_id=a["approval_id"])
            if status == "pending":
                continue
            if status == "approved":
                self._apply(wf, pid, a, p, now, ts, events)
            elif status == "applied":
                version = next((v["version"] for v in self.c.history(wf) if v["approval_id"] == a["approval_id"]), None)
                self._set(pid, now, "applied", applied_version=version)
                self.s.decide(now, ts, wf, "apply_reconciled", f"applied as v{version} before restart", a, pid)
                events.append({"proposal_id": pid, "kind": p["kind"], "status": "applied"})
            else:     # rejected, expired or lapsed: terminal. An expiry is treated as a rejection, with fresh data next time.
                self._set(pid, now, status, reasons=json.dumps(a.get("reasons")))
                self.s.decide(now, ts, wf, f"proposal_{status}", f"{p['kind']}: {a.get('reasons') or status}", a, pid)
                events.append({"proposal_id": pid, "kind": p["kind"], "status": status})
        return events

    def _apply(self, wf, pid, a, p, now, ts, events):
        code, res = self.c.apply(wf, a["approval_id"])
        if code == 200:
            self._set(pid, now, "applied", applied_version=res["version"])
            self.s.decide(now, ts, wf, "applied", f"{p['kind']} applied as v{res['version']} (approval {a['approval_id']})",
                          {"approval": a, "result": res}, pid)
            events.append({"proposal_id": pid, "kind": p["kind"], "status": "applied"})
        elif code == 410:
            self._set(pid, now, "lapsed", reasons=json.dumps(res))
            self.s.decide(now, ts, wf, "proposal_lapsed", "approved but missed apply window; will re-propose", res, pid)
            events.append({"proposal_id": pid, "kind": p["kind"], "status": "lapsed"})
        elif code == 409 and res.get("error") == "stale_base_version":
            # Someone (usually a supervisor) changed the config. Never force it: rebuild on the new base.
            self._set(pid, now, "stale", reasons=json.dumps(res))
            self.s.decide(now, ts, wf, "apply_conflict", "config moved after approval; dropped, will rebuild on new base", res, pid)
            events.append({"proposal_id": pid, "kind": p["kind"], "status": "stale"})
        elif code == 409:
            pass    # already applied by a previous life of this process: next tick sees status=applied
        else:
            self.s.alert(now, ts, "high", wf, f"apply failed {code}: {res}", "will retry next tick")
