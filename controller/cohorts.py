"""C2: one cohort abstraction. Statistics live at feature level; config needs first-match cohorts.

A rule says "for feature=value, on this dim, use this arm". Rules can overlap (age=60+ for voice,
lang=hi for welcome), and a lead only ever gets its first matching cohort, so overlaps must become
explicit intersection cohorts ordered most specific first. IDs derive from the definition, so the
same population has the same ID in stats, config, proposals and audit.
"""
from __future__ import annotations

from itertools import combinations

from .policy import MAX_COHORTS

PREFIX = "c:"


def cohort_id(conds: dict[str, str]) -> str:
    return PREFIX + "&".join(f"{k}={conds[k]}" for k in sorted(conds))


def _subsets(conds: list[tuple[str, str]], max_size: int):
    for size in range(min(max_size, len(conds)), 0, -1):
        for combo in combinations(conds, size):
            if len({f for f, _ in combo}) == size:
                yield dict(combo)


def _enumerate(rules: list[dict], sched_feature: str | None, sched_values: list[str]) -> list[dict]:
    conds = sorted({(r["feat"], r["val"]) for r in rules if r["feat"]})
    out: dict[str, dict] = {}
    if sched_feature:
        base = list(_subsets(conds, 3)) + [{}]
        for sub in base:
            if sched_feature in sub:
                out[cohort_id(sub)] = sub
            else:
                for v in sched_values:
                    c = {**sub, sched_feature: v}
                    out[cohort_id(c)] = c
    else:
        for sub in _subsets(conds, 3):
            out[cohort_id(sub)] = sub
    return sorted(out.values(), key=lambda c: (-len(c), cohort_id(c)))


def compile_rules(rules: list[dict], schedule: dict | None) -> tuple[list[dict], dict, dict, list[dict]]:
    """Returns (cohorts, allocations, hour_weights, rules_dropped_for_capacity). Global rules are not here."""
    rules = sorted([r for r in rules if r["feat"]], key=lambda r: -r["lift"])
    dropped: list[dict] = []
    sched = schedule
    while True:
        sf = sched["feature"] if sched else None
        svals = sorted(sched["weights"]) if sched else []
        cs = _enumerate(rules, sf, svals)
        if len(cs) <= MAX_COHORTS:
            break
        if rules:
            dropped.append(rules.pop())     # weakest rule goes first
        else:
            sched = None
    cohorts, allocs, hw = [], {}, {}
    for c in cs:
        cid = cohort_id(c)
        cohorts.append({"id": cid, "where": {k: [v] for k, v in sorted(c.items())}})
        per_dim: dict[str, dict] = {}
        for r in rules:     # strongest rule first, so it wins a dim when two apply
            if c.get(r["feat"]) == r["val"] and r["dim"] not in per_dim:
                per_dim[r["dim"]] = r["alloc"]
        if per_dim:
            allocs[cid] = per_dim
        if sched and sf in c:
            hw[cid] = sched["weights"][c[sf]]
    return cohorts, allocs, hw, dropped
