"""C6/C7: experiment analysis, winner selection, elimination, decay. Deterministic: the LLM never decides.

KPIs are never blended. Welcome and voice are judged on engaged/connected, stratified by
(day, config version, cohort, prompt) so that only arms that ran side by side are compared.
Pickup is reported separately and can only veto, never promote.
"""
from __future__ import annotations

from collections import defaultdict

from . import policy as P
from .stats import bh_select, cmh, norm_sf, rate_diff_z
from .store import FEATURES

DIM_COL = {"welcome_message": "welcome", "voice": "voice"}
TEST_DIMS = tuple(DIM_COL)


def load_cells(db, wf: str, from_day: int = 0):
    segs = {r[0]: dict(zip(FEATURES, r[1:])) for r in db.execute(
        "SELECT seg,lang,city_tier,age_band,value_band FROM segments")}
    rows = db.execute(
        "SELECT day,version,cohort,prompt,seg,welcome,voice,SUM(dials),SUM(connected),SUM(engaged) "
        "FROM arm_day WHERE wf=? AND day>=? GROUP BY day,version,cohort,prompt,seg,welcome,voice", (wf, from_day)).fetchall()
    return rows, segs


class Cells:
    """dim -> population -> arm -> stratum -> [dials, connected, engaged]. Population None is everyone."""

    def __init__(self, rows, segs):
        self.t = defaultdict(lambda: [0, 0, 0])
        for day, ver, coh, prm, seg, w, v, dials, conn, eng in rows:
            st = (day, ver, coh, prm)
            f = segs.get(seg, {})
            pops = [None] + [(k, f[k]) for k in FEATURES if k in f]
            for dim, arm in (("welcome_message", w), ("voice", v)):
                for pop in pops:
                    c = self.t[(dim, pop, arm, st)]
                    c[0] += dials; c[1] += conn; c[2] += eng
        self.index = defaultdict(list)
        for (dim, pop, arm, st) in self.t:
            self.index[(dim, pop, arm)].append(st)

    def compare(self, dim, pop, a, b, kpi="engaged", min_day=None, max_day=None) -> dict:
        strata = []
        for st in self.index.get((dim, pop, a), ()):
            if (min_day is not None and st[0] < min_day) or (max_day is not None and st[0] > max_day):
                continue
            ca, cb = self.t.get((dim, pop, a, st)), self.t.get((dim, pop, b, st))
            if ca is None or cb is None:
                continue
            if kpi == "engaged":
                strata.append((ca[1], ca[2], cb[1], cb[2]))
            else:
                strata.append((ca[0], ca[1], cb[0], cb[1]))
        return cmh(strata)


def populations(segs: dict) -> list:
    vals = sorted({(k, f[k]) for f in segs.values() for k in FEATURES})
    return [None] + vals


def look(db, wf: str, st: dict, opts: dict, today: int) -> dict:
    """One daily analysis. Returns the new rules, the eliminated arms, decayed winners and the evidence."""
    rows, segs = load_cells(db, wf)
    cells = Cells(rows, segs)
    pops = populations(segs)
    out = {"promote": [], "drop": [], "decay": [], "tests": 0, "evidence": {}}

    tests = []
    for dim in TEST_DIMS:
        if dim in st["locked"]:
            continue
        ref = st["ref"][dim]
        for arm in opts[dim]:
            if arm == ref or arm in st["blocked"][dim]:
                continue
            for pop in pops:
                r = cells.compare(dim, pop, arm, ref)
                if r["n_a"] == 0:
                    continue
                tests.append({"dim": dim, "arm": arm, "pop": pop, **r})
    out["tests"] = len(tests)
    if not tests:
        return out
    # One-sided BH across the entire look: the correction spans every arm, dim and population.
    sig = bh_select([norm_sf(t["z"]) for t in tests], P.Q_FDR)
    by = defaultdict(list)
    for t, s in zip(tests, sig):
        t["bh"] = s
        by[(t["dim"], t["arm"])].append(t)

    def qualifies(t):
        floor = P.MIN_N_GLOBAL if t["pop"] is None else P.MIN_N_SEGMENT
        if not (t["bh"] and t["z"] >= P.Z_MIN and t["lift"] >= P.MIN_LIFT and t["n_a"] >= floor):
            return False
        # guardrail on the other KPI, kept separate
        g = cells.compare(t["dim"], t["pop"], t["arm"], st["ref"][t["dim"]], kpi="connected")
        t["pickup_guard"] = {k: g[k] for k in ("z", "lift", "n_a", "n_b")}
        if g["z"] <= P.GUARD_PICKUP_Z:
            return False
        # a winner that has already stopped winning is not a winner (novelty that faded mid test)
        recent = cells.compare(t["dim"], t["pop"], t["arm"], st["ref"][t["dim"]], min_day=today - P.DECAY_RECENT_DAYS + 1)
        t["recent"] = {k: recent[k] for k in ("z", "lift", "n_a", "n_b")}
        return recent["n_a"] < 50 or recent["z"] > -1.0

    existing = {(r["dim"], r["feat"], r["val"]): r for r in st["rules"]}
    for dim in TEST_DIMS:
        if dim in st["locked"]:
            continue
        ref = st["ref"][dim]
        dim_tests = [t for t in tests if t["dim"] == dim]
        glob_rule = existing.get((dim, None, None))
        if not glob_rule:
            cands = [t for t in dim_tests if t["pop"] is None and qualifies(t)]
            if cands:
                best = max(cands, key=lambda t: t["lift"])
                out["promote"].append({"dim": dim, "feat": None, "val": None, "arm": best["arm"], "ref": ref,
                                       "lift": best["lift"], "z": best["z"], "evidence": best})
                glob_rule = {"arm": best["arm"], "lift": best["lift"]}
        added = sum(1 for r in st["rules"] if r["dim"] == dim and r["feat"])
        seg_cands = sorted([t for t in dim_tests if t["pop"] is not None and qualifies(t)], key=lambda t: -t["z"])
        seen_pop = set()
        for t in seg_cands:
            if t["pop"] in seen_pop or added >= P.MAX_SEGMENT_RULES_PER_DIM:
                continue
            feat, val = t["pop"]
            if (dim, feat, val) in existing:
                continue
            if glob_rule:
                if t["arm"] == glob_rule["arm"]:
                    continue
                vs = cells.compare(dim, t["pop"], t["arm"], glob_rule["arm"])
                t["vs_global"] = {k: vs[k] for k in ("z", "lift", "n_a", "n_b")}
                # must beat the workflow-wide winner in this population, on concurrent data
                if vs["n_b"] > 0 and (vs["z"] < 2.0 or vs["lift"] < P.MIN_LIFT):
                    continue
                if vs["n_b"] == 0 and t["lift"] < glob_rule["lift"] + P.MIN_LIFT:
                    continue
            seen_pop.add(t["pop"])
            added += 1
            out["promote"].append({"dim": dim, "feat": feat, "val": val, "arm": t["arm"], "ref": ref,
                                   "lift": t["lift"], "z": t["z"], "evidence": t})

        # successive elimination: move scarce traffic off arms with no sign of a win anywhere
        promoted = {r["arm"] for r in st["rules"] if r["dim"] == dim} | {
            p["arm"] for p in out["promote"] if p["dim"] == dim}
        for arm in st["alive"][dim]:
            if arm in promoted:
                continue
            at = [t for t in dim_tests if t["arm"] == arm]
            glob = next((t for t in at if t["pop"] is None), None)
            if not glob:
                continue
            if glob["z"] <= P.HARM_Z and glob["lift"] <= P.HARM_LIFT:
                out["drop"].append({"dim": dim, "arm": arm, "why": "harm", "z": glob["z"], "lift": glob["lift"]})
                continue
            seg_max = max((t["z"] for t in at if t["pop"] is not None and t["n_a"] >= 30), default=0)
            if glob["n_a"] >= P.DROP_AFTER_N and glob["z"] < P.DROP_MAX_ALL_Z and seg_max < P.DROP_MAX_SEG_Z:
                out["drop"].append({"dim": dim, "arm": arm, "why": "no_signal", "z": glob["z"],
                                    "lift": glob["lift"], "best_segment_z": seg_max})

    # decay of what we already promoted, against its concurrent holdback
    for rule in st["rules"]:
        pop = (rule["feat"], rule["val"]) if rule["feat"] else None
        dim, arm, ref = rule["dim"], rule["arm"], rule["ref"]
        # the recent window is whole days after the promotion was in force; today is still partial at look time
        cut = max(today - P.DECAY_RECENT_DAYS, rule["since_day"] + 1)
        age = today - rule["since_day"]
        rec = cells.compare(dim, pop, arm, ref, min_day=cut)
        old = cells.compare(dim, pop, arm, ref, max_day=rule["since_day"])
        ev = {"recent": rec, "earlier": old, "age_days": age}
        if rec["n_b"] < 40 or age < 2:
            continue
        if rec["z"] <= P.DECAY_REVERSAL_Z and rec["lift"] < 0:
            out["decay"].append({"rule": rule, "why": "reversal", "evidence": ev})
        elif old["n_b"] >= 40 and age >= P.DECAY_MIN_AGE_DAYS:
            d_rec = rec["rate_a"] - rec["rate_b"]
            d_old = old["rate_a"] - old["rate_b"]
            pooled = 0.25 * (1 / rec["n_a"] + 1 / rec["n_b"] + 1 / old["n_a"] + 1 / old["n_b"])
            zdrop = (d_old - d_rec) / pooled ** 0.5
            ev["zdrop"] = round(zdrop, 3)
            if zdrop >= P.DECAY_DROP_Z and d_old - d_rec >= P.DECAY_MIN_DROP and rec["lift"] < P.DECAY_KEEP_FRACTION * old["lift"]:
                out["decay"].append({"rule": rule, "why": "faded", "evidence": ev})
    out["evidence"]["top_tests"] = sorted(
        [{k: t.get(k) for k in ("dim", "arm", "pop", "z", "lift", "n_a", "n_b", "bh")} for t in tests],
        key=lambda t: -abs(t["z"]))[:12]
    return out


def default_dist(st: dict, dim: str) -> dict:
    """Default allocation for a tested dim, purely from state."""
    ref = st["ref"][dim]
    alive = [a for a in st["alive"][dim] if a not in st["blocked"][dim]]
    glob = next((r for r in st["rules"] if r["dim"] == dim and not r["feat"]), None)
    if glob:
        others = [a for a in alive if a not in (glob["arm"], ref)]
        if others:
            d = {glob["arm"]: P.PROMOTE_SHARE, ref: P.HOLDBACK_SHARE}
            for a in others:
                d[a] = (1 - P.PROMOTE_SHARE - P.HOLDBACK_SHARE) / len(others)
            return d
        return {glob["arm"]: 1 - P.HOLDBACK_SHARE, ref: P.HOLDBACK_SHARE}
    if not alive:
        return {ref: 1.0}
    each = P.ARM_SINGLE_SHARE if len(alive) == 1 else min(P.ARM_MAX_SHARE, 0.6 / len(alive))
    d = {ref: 1 - each * len(alive)}
    for a in alive:
        d[a] = each
    return d


def did_stats(db, wf: str, start: int, end: int = 10**9) -> dict:
    """Pickup per caller ID over [start, end). Peer comparison is applied by the caller."""
    out = defaultdict(lambda: [0, 0])
    for did, dials, conn in db.execute(
            "SELECT did,SUM(dials),SUM(connected) FROM hourly WHERE wf=? AND minute>=? AND minute<? GROUP BY did",
            (wf, start, end)):
        out[did] = [dials, conn]
    return out


def did_peer_test(stats: dict, did: str, peers: list[str]) -> dict:
    d, c = stats.get(did, (0, 0))
    pd = sum(stats[p][0] for p in peers if p in stats)
    pc = sum(stats[p][1] for p in peers if p in stats)
    z = rate_diff_z(c, d, pc, pd)
    ratio = (c / d) / (pc / pd) if d and pd and pc else 1.0
    return {"did": did, "dials": d, "connected": c, "peer_dials": pd, "peer_connected": pc,
            "z": round(z, 3), "ratio": round(ratio, 3)}
