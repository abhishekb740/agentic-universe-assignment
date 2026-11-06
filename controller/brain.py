"""Per-workflow decisions: what config do we want, and what do we do about what we saw.

Detect, hypothesise, propose, learn, explain. Governance decides how a change may reach production;
this module decides only what we would like and why. All of its state is one JSON row per workflow.
"""
from __future__ import annotations

import copy
import json
from collections import defaultdict

from . import policy as P
from .cohorts import PREFIX, compile_rules
from .experiments import TEST_DIMS, did_peer_test, did_stats, default_dist, look
from .governance import CUSTOMER_FACING, canon, changed_dims
from .stats import poisson_excess_z, rate_diff_z
from .store import FEATURES


def majority(dist: dict) -> str:
    return max(sorted(dist), key=lambda k: dist[k])


class WorkflowBrain:
    def __init__(self, store, client, gov, pub: dict, end_minute: int):
        self.s, self.c, self.gov, self.pub, self.end = store, client, gov, pub, end_minute
        self.wf = pub["workflow_id"]
        self.opts = pub["options"]
        self.st = store.wf_state(self.wf)

    def save(self):
        self.s.save_wf_state(self.wf, self.st)

    # ------------------------------------------------------------------ boot
    def boot(self, cfg: dict, now: int, ts: str):
        if self.st is not None:
            return
        db = self.s.db
        default = cfg["allocations"]["default"]
        ref = {d: majority(default[d]) for d in TEST_DIMS}
        horizon = (self.end - now) / 1440
        explore = horizon >= P.MIN_HORIZON_TO_EXPLORE_DAYS
        hist = dict(db.execute("SELECT did,SUM(dials) FROM hourly WHERE wf=? GROUP BY did", (self.wf,)))
        known = [d for d in self.opts["caller_id"] if hist.get(d, 0) >= P.DID_KNOWN_DIALS]
        unknown = [d for d in self.opts["caller_id"] if d not in known]
        p_hist = db.execute("SELECT SUM(connected),SUM(halluc) FROM hourly WHERE wf=? AND prompt=?",
                            (self.wf, cfg["prompt_version"])).fetchone()
        conn, hal = p_hist[0] or 0, p_hist[1] or 0
        self.st = {
            "ref": ref,
            "alive": {d: ([a for a in self.opts[d] if a != ref[d]] if explore else []) for d in TEST_DIMS},
            "blocked": {d: [] for d in TEST_DIMS},
            "rules": [],
            "locked": {},
            "last_look_day": None,
            "did": {"bad": [], "trusted": known or list(default["caller_id"]), "probing": unknown if explore else []},
            "prompt": {"good": cfg["prompt_version"], "seen": cfg["prompt_version"], "adopted": now,
                       "baseline_rate": (hal + 1) / (conn + 100), "incident": None, "screen": False, "checked": {}},
            "schedule": self._schedule(),
        }
        self.save()
        self.s.decide(now, ts, self.wf, "boot",
                      f"baseline read from history: {conn} connected, hallucination {hal}/{conn}; "
                      f"tested DIDs {known}; untested {unknown}",
                      {"state": self.st})

    # ------------------------------------------------------------------ schedule (history-based, once)
    def _schedule(self):
        """Pickup varies by hour differently across populations? Only if clearly so, weight dials to match."""
        lo, hi = self.pub["dialling_window"]
        segs = {r[0]: dict(zip(FEATURES, r[1:])) for r in self.s.db.execute(
            "SELECT seg,lang,city_tier,age_band,value_band FROM segments")}
        cells = defaultdict(lambda: [0, 0])
        for hour, seg, d, c in self.s.db.execute(
                "SELECT hour,seg,SUM(dials),SUM(connected) FROM hour_seg WHERE wf=? GROUP BY hour,seg", (self.wf,)):
            f = segs.get(seg)
            if not f:
                continue
            for k in FEATURES:
                cells[(k, f[k], hour // 2)][0] += d
                cells[(k, f[k], hour // 2)][1] += c
        best = None
        for feat in FEATURES:
            vals = sorted({v for (k, v, _) in cells if k == feat})
            buckets = sorted({b for (k, _, b) in cells if k == feat})
            if len(vals) < 2 or len(buckets) < 2:
                continue
            bt = {b: [sum(cells[(feat, v, b)][i] for v in vals) for i in (0, 1)] for b in buckets}
            vt = {v: [sum(cells[(feat, v, b)][i] for b in buckets) for i in (0, 1)] for v in vals}
            overall = sum(x[1] for x in bt.values()) / max(sum(x[0] for x in bt.values()), 1)
            if not overall or sum(x[0] for x in bt.values()) < P.SCHED_MIN_DIALS:
                continue
            chi2, df = 0.0, (len(vals) - 1) * (len(buckets) - 1)
            for v in vals:
                for b in buckets:
                    n, k_ = cells[(feat, v, b)]
                    if n == 0:
                        continue
                    # expected under "no interaction": value's rate x bucket's rate / overall
                    e = (vt[v][1] / vt[v][0]) * (bt[b][1] / bt[b][0]) / overall
                    chi2 += (k_ - n * e) ** 2 / max(n * e * (1 - e), 1e-9)
            if chi2 / df >= P.SCHED_CHI2_PER_DF and (best is None or chi2 / df > best[0]):
                best = (chi2 / df, feat, vals, buckets)
        if not best:
            return None
        score, feat, vals, buckets = best
        weights = {}
        for v in vals:
            n_v = sum(cells[(feat, v, b)][0] for b in buckets)
            mean = sum(cells[(feat, v, b)][1] for b in buckets) / max(n_v, 1)
            table = {}
            for h in range(lo, hi):
                n, k_ = cells.get((feat, v, h // 2), (0, 0))
                r = (k_ / n) / mean if n and mean else 1.0
                table[str(h)] = round(min(max(r ** 3, P.SCHED_MIN_WEIGHT), P.SCHED_MAX_WEIGHT), 3)
            weights[v] = table
        return {"feature": feat, "weights": weights, "chi2_per_df": round(score, 2)}

    # ------------------------------------------------------------------ supervisor edits
    def on_supervisor_edit(self, ev: dict, cfg: dict, now: int, ts: str):
        """A human changed production. Their change stands; we adapt to it and say so."""
        dims = ev.get("changed_dims") or []
        locked_now = []
        for dim in dims:
            if dim in TEST_DIMS:
                self.st["locked"][dim] = {"by": "supervisor", "version": ev.get("version"), "at": ts}
                self.st["alive"][dim] = []
                self.st["rules"] = [r for r in self.st["rules"] if not (r["dim"] == dim and not r["feat"])]
                self.st["ref"][dim] = majority(cfg["allocations"]["default"][dim])
                locked_now.append(dim)
            elif dim == "caller_id":
                self.st["locked"]["caller_id"] = {"by": "supervisor", "version": ev.get("version"), "at": ts}
                locked_now.append(dim)
            elif dim == "prompt_version":
                self.st["prompt"]["screen"] = True
        self.save()
        self.s.decide(now, ts, self.wf, "supervisor_edit",
                      f"supervisor changed {dims} at v{ev.get('version')}; adopted as the new base, locked {locked_now}",
                      ev)
        self.s.alert(now, ts, "info", self.wf, f"supervisor edit v{ev.get('version')} on {dims}",
                     "no action needed unless the controller raises a separate safety proposal")

    # ------------------------------------------------------------------ daily look
    def daily_look(self, now: int, ts: str):
        today = now // 1440
        if now % 1440 < 9 * 60 or self.st["last_look_day"] == today:
            return
        res = look(self.s.db, self.wf, self.st, self.opts, today)
        changed = False
        for d in res["drop"]:
            if d["arm"] in self.st["alive"][d["dim"]]:
                self.st["alive"][d["dim"]].remove(d["arm"])
                changed = True
                self.s.decide(now, ts, self.wf, "arm_dropped", f"{d['dim']}={d['arm']} ({d['why']})", d)
        for dec in res["decay"]:
            rule = dec["rule"]
            self.st["rules"].remove(rule)
            self.st["blocked"][rule["dim"]].append(rule["arm"])
            if rule["arm"] in self.st["alive"][rule["dim"]]:
                self.st["alive"][rule["dim"]].remove(rule["arm"])
            changed = True
            self.s.decide(now, ts, self.wf, "winner_decayed",
                          f"{rule['dim']}={rule['arm']} in {rule['feat'] or 'all'}={rule['val'] or ''}: {dec['why']}, rolling back",
                          dec["evidence"])
            self.s.alert(now, ts, "warn", self.wf, f"promoted {rule['dim']}={rule['arm']} {dec['why']}",
                         "rollback proposed; approver sees the evidence")
        decayed = {(d["rule"]["dim"], d["rule"]["arm"]) for d in res["decay"]}
        for pr in res["promote"]:
            if (pr["dim"], pr["arm"]) in decayed:
                continue
            rule = {k: pr[k] for k in ("dim", "feat", "val", "arm", "ref", "lift", "z")}
            rule["since_day"] = today
            self.st["rules"].append(rule)
            changed = True
            self.s.decide(now, ts, self.wf, "winner_selected",
                          f"{pr['dim']}={pr['arm']} in {pr['feat'] or 'all'}={pr['val'] or ''}: "
                          f"lift {pr['lift']:+.1%} z={pr['z']:.2f}, BH q={P.Q_FDR} over {res['tests']} tests",
                          pr["evidence"])
        self.st["last_look_day"] = today
        self.save()
        self.s.decide(now, ts, self.wf, "daily_look",
                      f"{res['tests']} tests; {len(res['promote'])} winners, {len(res['drop'])} drops, {len(res['decay'])} decays",
                      res["evidence"])
        return changed

    # ------------------------------------------------------------------ caller ID health
    def check_did(self, cfg: dict, now: int, ts: str) -> bool:
        """Returns True if a DID in live rotation has just been found unhealthy."""
        if "caller_id" in self.st["locked"]:
            return False
        did = self.st["did"]
        shares = cfg["allocations"]["default"]["caller_id"]
        live = [d for d in shares if d not in did["bad"]]
        window = did_stats(self.s.db, self.wf, now - P.DID_WINDOW_MIN)
        before = did_stats(self.s.db, self.wf, now - 8 * 1440, now - P.DID_WINDOW_MIN)
        since0 = did_stats(self.s.db, self.wf, 0) if did["probing"] else {}
        urgent = False
        for d in live:
            # Load depresses a busy DID, so a heavy DID is only compared with peers carrying as much.
            peers = [p for p in live if p != d and shares[p] >= shares[d] / 1.5]
            if not peers or len(live) < 2:
                continue
            probing = d in did["probing"]
            t = did_peer_test(since0 if probing else window, d, peers)
            if t["dials"] < 100 or t["peer_dials"] < 100:
                continue
            if t["z"] > P.DID_FLAG_Z or t["ratio"] > P.DID_FLAG_RATIO:
                continue
            if not probing:
                # An established DID must also have fallen against its own past. A DID recovering from heavy
                # load looks poor beside freshly rested peers without being flagged by anyone.
                wd, wc = window.get(d, (0, 0))
                bd, bc = before.get(d, (0, 0))
                self_z = rate_diff_z(wc, wd, bc, bd)
                self_ratio = (wc / wd) / (bc / bd) if wd and bd and bc else 1.0
                t.update({"self_z": round(self_z, 3), "self_ratio": round(self_ratio, 3)})
                if self_z > P.DID_FLAG_Z or self_ratio > P.DID_FLAG_RATIO:
                    continue
            did["bad"].append(d)
            for lst in ("trusted", "probing"):
                if d in did[lst]:
                    did[lst].remove(d)
            live = [x for x in live if x != d]
            urgent = True
            self.s.decide(now, ts, self.wf, "did_flagged", f"{d} pickup is {t['ratio']:.0%} of peers (z={t['z']})", t)
            self.s.alert(now, ts, "warn", self.wf, f"caller ID {d} looks spam-flagged: {t['ratio']:.0%} of peer pickup",
                         "traffic moving off it; ask telecom to check the number")
        for d in list(did["probing"]):
            if d not in shares or d in did["bad"]:
                continue
            peers = [p for p in shares if p != d and p not in did["bad"]]
            t = did_peer_test(since0, d, peers)
            if t["dials"] >= P.DID_TRUST_DIALS and t["z"] >= P.DID_TRUST_Z:
                did["probing"].remove(d)
                did["trusted"].append(d)
                self.s.decide(now, ts, self.wf, "did_trusted", f"{d} passed its probe (z={t['z']})", t)
        self.save()
        return urgent

    def did_target(self, cfg: dict) -> dict:
        cur = cfg["allocations"]["default"]["caller_id"]
        if "caller_id" in self.st["locked"]:
            return dict(cur)
        did = self.st["did"]
        trusted = [d for d in did["trusted"] if d not in did["bad"]]
        probing = [d for d in did["probing"] if d not in did["bad"]]
        if not trusted:
            return dict(cur)
        dials_day = self.pub["dials_per_hour"] * (self.pub["dialling_window"][1] - self.pub["dialling_window"][0])
        share = max(P.DID_PROBE_SHARE, min(0.12, 150 / max(dials_day, 1)))
        out = {d: share for d in probing}
        rest = 1 - share * len(probing)
        out.update({d: rest / len(trusted) for d in trusted})
        return out

    def did_step(self, cur: dict, target: dict, max_shift: float) -> dict:
        keys = set(cur) | set(target)
        tv = 0.5 * sum(abs(cur.get(k, 0) - target.get(k, 0)) for k in keys)
        if tv <= max_shift:
            return dict(target)
        f = (max_shift * 0.98) / tv
        return {k: v for k in keys if (v := cur.get(k, 0) + f * (target.get(k, 0) - cur.get(k, 0))) > 1e-6}

    # ------------------------------------------------------------------ prompt safety
    def check_prompt(self, cfg: dict, now: int, ts: str, credits: float):
        ps = self.st["prompt"]
        cur = cfg["prompt_version"]
        if cur != ps["seen"]:
            ps["seen"], ps["adopted"] = cur, now
        inc = ps["incident"]
        if inc and cur == inc.get("target"):
            ps["good"] = cur
            ps["incident"] = None
            self.s.decide(now, ts, self.wf, "prompt_recovered", f"{cur} live; monitoring hallucination and engagement", inc)
            inc = None
        conn, hal = self.s.db.execute(
            "SELECT COALESCE(SUM(connected),0),COALESCE(SUM(halluc),0) FROM hourly WHERE wf=? AND prompt=? AND minute>=?",
            (self.wf, cur, max(ps["adopted"], now - 1440))).fetchone()
        expected = conn * ps["baseline_rate"]
        z = poisson_excess_z(hal, expected)
        ratio = (hal / conn) / ps["baseline_rate"] if conn else 0.0
        reason = None
        if not inc and hal >= P.HALLUC_MIN_FLAGS and z >= P.HALLUC_Z and ratio >= P.HALLUC_RATIO:
            reason = {"source": "live_qa", "connected": conn, "flagged": hal, "z": round(z, 2), "ratio": round(ratio, 2),
                      "baseline_rate": round(ps["baseline_rate"], 5)}
        if not inc and not reason and ps["screen"]:
            ps["screen"] = False
            if credits >= P.VALIDATION_N_SCREEN / 100:
                pass_rate = self._validate(cur, P.VALIDATION_N_SCREEN, now, ts)
                if pass_rate is not None and pass_rate < P.STRICT_PASS_RATE:
                    reason = {"source": "validation_screen", "pass_rate": pass_rate, "threshold": P.STRICT_PASS_RATE}
        if reason:
            ps["incident"] = inc = {"prompt": cur, "at": now, "reason": reason, "target": None,
                                    "validation_run_id": None, "next_try": now}
            self.s.decide(now, ts, self.wf, "hallucination_incident", f"{cur} failing safety: {reason}", reason)
            self.s.alert(now, ts, "high", self.wf, f"prompt {cur} hallucination incident: {reason}",
                         "fix goes to a human approver with validation evidence; please review quickly")
        if inc and not inc["target"] and inc["next_try"] <= now:
            self._find_fix(cur, inc, now, ts, credits)
        self.save()
        return ps["incident"]

    def _validate(self, prompt: str, n: int, now: int, ts: str):
        code, res = self.c.validate(self.wf, prompt, n)
        if code != 200:
            self.s.alert(now, ts, "warn", self.wf, f"validation failed {code}: {res}", "will retry; check credits")
            return None
        self.st["prompt"]["checked"][prompt] = {"pass_rate": res["pass_rate"], "run": res["validation_run_id"], "at": now, "n": n}
        self.s.decide(now, ts, self.wf, "validation_run", f"{prompt}: pass rate {res['pass_rate']} on n={n}", res)
        return res["pass_rate"]

    def _find_fix(self, cur, inc, now, ts, credits):
        """Last known good first (it has real engagement history here). The LLM only widens the candidate list."""
        ps = self.st["prompt"]
        good = ps["good"]
        floor = max(P.STRICT_PASS_RATE, self.pub["governance"]["min_validation_pass_rate"])
        tried = {}
        if good != cur and credits >= P.VALIDATION_N_EVIDENCE / 100:
            r = self._validate(good, P.VALIDATION_N_EVIDENCE, now, ts)
            tried[good] = r
            if r is not None and r >= floor:
                inc["target"], inc["validation_run_id"] = good, ps["checked"][good]["run"]
                inc["how"] = "last_known_good"
                return
        code, res = self.c.suggest_fixes(self.wf, f"Prompt {cur} shows hallucination: {inc['reason']}")
        if code == 200:
            self.s.decide(now, ts, self.wf, "llm_suggestions", "candidates only, never decisions", res)
            for sg in res["suggestions"]:
                pv = sg["prompt_version"]
                if pv in tried or pv == cur:
                    continue
                if any(w in sg.get("summary", "").lower() for w in P.REJECT_SUMMARY_WORDS):
                    self.s.decide(now, ts, self.wf, "candidate_rejected", f"{pv}: {sg.get('summary')} (known to hurt the call)", sg)
                    continue
                tried[pv] = self._validate(pv, P.VALIDATION_N_EVIDENCE, now, ts)
        valid = {k: v for k, v in tried.items() if v is not None and v >= floor}
        if valid:
            best = max(sorted(valid), key=lambda k: valid[k])
            inc["target"], inc["validation_run_id"] = best, ps["checked"][best]["run"]
            inc["how"] = "validated_candidate"
        else:
            inc["next_try"] = now + 720
            self.s.alert(now, ts, "high", self.wf, f"no validated fix for prompt {cur}: {tried}",
                         "a human must choose; the controller will retry in 12 hours")

    # ------------------------------------------------------------------ the config we want
    def desired(self, cfg: dict) -> dict:
        c = copy.deepcopy(cfg)
        default = c["allocations"]["default"]
        for dim in TEST_DIMS:
            if dim not in self.st["locked"]:
                default[dim] = default_dist(self.st, dim)
        default["caller_id"] = self.did_target(cfg)
        rules = []
        for r in self.st["rules"]:
            if r["feat"]:
                rules.append({**r, "alloc": {r["arm"]: 1 - P.HOLDBACK_SHARE, self.st["ref"][r["dim"]]: P.HOLDBACK_SHARE}
                              if r["arm"] != self.st["ref"][r["dim"]] else {r["arm"]: 1.0}})
        cohorts, allocs, hw, dropped = compile_rules(rules, self.st["schedule"])
        # cohorts a human created keep their place, ahead of ours
        keep = [x for x in cfg["cohorts"] if not x["id"].startswith(PREFIX)]
        keep_ids = {x["id"] for x in keep}
        c["cohorts"] = keep + cohorts
        c["allocations"] = {k: v for k, v in c["allocations"].items() if k == "default" or k in keep_ids}
        c["allocations"].update(allocs)
        sched = c["schedule"]["hour_weights"]
        c["schedule"]["hour_weights"] = {k: v for k, v in sched.items() if k == "default" or k in keep_ids}
        c["schedule"]["hour_weights"].update(hw)
        return c

    def pop_share(self, where: dict) -> float:
        segs = {r[0]: dict(zip(FEATURES, r[1:])) for r in self.s.db.execute(
            "SELECT seg,lang,city_tier,age_band,value_band FROM segments")}
        tot = hit = 0
        for seg, d in self.s.db.execute("SELECT seg,SUM(dials) FROM hour_seg WHERE wf=? GROUP BY seg", (self.wf,)):
            f = segs.get(seg)
            if not f:
                continue
            tot += d
            if all(f[k] in v for k, v in where.items()):
                hit += d
        return hit / tot if tot else 0.0

    def describe(self, cur: dict, new: dict) -> dict:
        """Honest numbers for the approver: who is touched, how much traffic moves, what we expect."""
        dims = changed_dims(cur, new)
        ca, cb = canon(cur), canon(new)
        dials_day = self.pub["dials_per_hour"] * (self.pub["dialling_window"][1] - self.pub["dialling_window"][0])
        batch = self.pub["daily_batch"]["size"]
        dflt = cb["allocations"]["default"]
        unproven = {}
        for dim in TEST_DIMS:
            proven = {r["arm"] for r in self.st["rules"] if r["dim"] == dim} | {self.st["ref"][dim]}
            unproven[dim] = round(sum(v for k, v in dflt[dim].items() if k not in proven), 3)
        projected = {}
        for r in self.st["rules"]:
            where = {r["feat"]: [r["val"]]} if r["feat"] else {}
            sh = self.pop_share(where)
            projected[f"{r['dim']}={r['arm']}@{r['feat'] or 'all'}={r['val'] or ''}"] = {
                "population_share": round(sh, 3), "estimated_lift": r["lift"], "z": r["z"],
                "engagement_effect_on_workflow": round(sh * (1 - P.HOLDBACK_SHARE) * r["lift"], 4)}
        default_moved = any(ca["allocations"]["default"][x] != dflt[x] for x in ("welcome_message", "voice", "caller_id"))
        if default_moved or "prompt_version" in dims:
            touched = 1.0
        else:
            touched = min(1.0, sum(self.pop_share(c["where"]) for c in cb["cohorts"]))
        moved = 0.0
        for dim in ("welcome_message", "voice", "caller_id"):
            a, b = ca["allocations"]["default"][dim], dflt[dim]
            moved = max(moved, 0.5 * sum(abs(a.get(k, 0) - b.get(k, 0)) for k in set(a) | set(b)))
        impact = touched * max(moved, 0.1)
        if "prompt_version" in dims:
            impact = max(impact, 0.7)
        return {
            "changed_dims": dims, "affected_leads": int(batch * touched), "impact": impact,
            "dry_run": {
                "population_share_touched": round(touched, 3),
                "expected_daily_dials": dials_day,
                "default_allocation_after": {d: dflt[d] for d in dflt},
                "share_of_traffic_on_unproven_arms": unproven,
                "projected_effects_of_promoted_rules": projected,
                "cohorts_after": [x["id"] for x in cb["cohorts"]],
                "basis": "Lifts are stratified estimates from concurrent arms only; exploration arms have no estimate yet, "
                         "so exposure is bounded instead of a benefit being claimed.",
            },
        }

    def customer_changes(self, cur: dict, new: dict) -> list[str]:
        return [d for d in changed_dims(cur, new) if d in CUSTOMER_FACING]

    def rationale(self, cur: dict, new: dict, recent: list[str]) -> tuple[str, str]:
        dims = changed_dims(cur, new)
        rules = [f"{r['dim']}={r['arm']} for {r['feat'] or 'all'}{'=' + r['val'] if r['val'] else ''} "
                 f"({r['lift']:+.1%}, z={r['z']:.1f})" for r in self.st["rules"]]
        alive = {d: self.st["alive"][d] for d in TEST_DIMS if self.st["alive"][d]}
        parts = []
        if rules:
            parts.append("Promoted on evidence: " + "; ".join(rules) + ".")
        if alive:
            parts.append(f"Still testing, control stays the largest arm: {json.dumps(alive)}.")
        if "schedule" in dims and self.st["schedule"]:
            parts.append(f"Hour weights by {self.st['schedule']['feature']} from history "
                         f"(chi2/df {self.st['schedule']['chi2_per_df']}), capacity and dialling window unchanged.")
        if self.st["blocked"] and any(self.st["blocked"].values()):
            parts.append(f"Rolled back after decay: {json.dumps(self.st['blocked'])}.")
        if recent:
            parts.append("Since last request: " + " ".join(recent[-4:]))
        summary = f"Batched change to {', '.join(dims)}."
        if P.TENANT_PREFS.get(self.pub["tenant_id"]):
            summary += " Advance notice: this affects running campaigns where the workflow allows mid-flight changes."
        return summary, " ".join(parts) or "No change in evidence."
