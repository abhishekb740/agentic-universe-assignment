"""Tests where they matter: statistics, cohort compilation, exactly-once ingest, governance recovery, the disqualifiers."""
import ast
import json
from pathlib import Path

from controller import policy as P
from controller.cohorts import compile_rules, cohort_id
from controller.experiments import default_dist
from controller.governance import Governance, canon, changed_dims, config_hash
from controller.ingest import Ingestor
from controller.stats import bh_select, cmh, poisson_excess_z
from controller.store import Store


def rule(dim, feat, val, arm, lift=0.1):
    return {"dim": dim, "feat": feat, "val": val, "arm": arm, "lift": lift, "alloc": {arm: 0.85, "REF": 0.15}}


# ------------------------------------------------------------------ statistics
def test_cmh_resists_simpsons_paradox():
    # A beats B inside each stratum, while B looks better pooled because it ran mostly in the easy stratum.
    strata = [(900, 180, 100, 15), (100, 90, 900, 765)]
    pooled_a, pooled_b = 270 / 1000, 780 / 1000
    assert pooled_a < pooled_b
    r = cmh(strata)
    assert r["z"] > 0 and r["lift"] > 0


def test_cmh_ignores_strata_where_one_arm_did_not_run():
    assert cmh([(500, 400, 0, 0), (0, 0, 500, 100)])["n_a"] == 0


def test_bh_controls_and_keeps_order():
    assert bh_select([0.001, 0.2, 0.9], 0.05) == [True, False, False]
    assert bh_select([0.04, 0.04, 0.04], 0.05) == [True, True, True]
    assert bh_select([0.04, 0.5, 0.5, 0.5], 0.05) == [False] * 4


def test_poisson_excess():
    assert poisson_excess_z(1, 1.0) < 1
    assert poisson_excess_z(20, 2.0) > 3.5


# ------------------------------------------------------------------ cohorts
def test_overlapping_rules_become_most_specific_first():
    rules = [rule("voice", "age_band", "60+", "V3", 0.2), rule("welcome_message", "lang", "hi", "W2", 0.1)]
    cohorts, allocs, _, dropped = compile_rules(rules, None)
    ids = [c["id"] for c in cohorts]
    both = cohort_id({"age_band": "60+", "lang": "hi"})
    assert ids[0] == both and not dropped
    assert set(allocs[both]) == {"voice", "welcome_message"}
    assert cohorts[0]["where"] == {"age_band": ["60+"], "lang": ["hi"]}


def test_same_feature_values_do_not_intersect():
    rules = [rule("voice", "age_band", "18-30", "V4"), rule("voice", "age_band", "31-45", "V4")]
    cohorts, _, _, _ = compile_rules(rules, None)
    assert len(cohorts) == 2


def test_cohort_cap_drops_weakest_rule_first():
    feats = [("lang", "hi"), ("lang", "en"), ("age_band", "60+"), ("age_band", "18-30"), ("city_tier", "1"),
             ("city_tier", "2"), ("value_band", "low"), ("value_band", "mid")]
    rules = [rule("voice", f, v, "V2", 0.3 - i * 0.01) for i, (f, v) in enumerate(feats)]
    cohorts, _, _, dropped = compile_rules(rules, None)
    assert len(cohorts) <= P.MAX_COHORTS and dropped
    assert dropped[0]["val"] == "mid"


def test_default_dist_keeps_control_largest_or_equal_and_bounds_exposure():
    st = {"ref": {"voice": "V1"}, "alive": {"voice": ["V2", "V3", "V4"]}, "blocked": {"voice": []}, "rules": []}
    d = default_dist(st, "voice")
    assert abs(sum(d.values()) - 1) < 1e-9
    assert d["V1"] == max(d.values())
    assert max(v for k, v in d.items() if k != "V1") <= P.ARM_MAX_SHARE + 1e-9


def test_decayed_arm_cannot_come_back():
    st = {"ref": {"voice": "V1"}, "alive": {"voice": ["V2"]}, "blocked": {"voice": ["V2"]}, "rules": []}
    assert default_dist(st, "voice") == {"V1": 1.0}


# ------------------------------------------------------------------ ingest
class FakeChunks:
    def __init__(self, chunks):
        self.c = chunks

    def chunks(self, kind, after):
        return [{"chunk": n} for n in sorted(self.c) if n > after] if kind == "events" else []

    def chunk_text(self, n):
        return self.c[n]


LEAD = {"type": "lead", "lead_id": "l1", "features": {"lang": "hi", "city_tier": "1", "age_band": "60+", "value_band": "low"}}
CALL = {"type": "call", "ts": "2027-01-04T10:00:00+05:30", "workflow_id": "w", "lead_id": "l1", "person_id": "p1", "config_version": 1, "cohort_id": "default",
        "variant": {"welcome_message": "W1", "voice": "V1", "caller_id": "D1", "prompt_version": "P1"},
        "connected": True, "engaged": True, "qa": {"hallucination_flag": False}}


def test_ingest_exactly_once_across_a_crash(tmp_path):
    good = "\n".join(json.dumps(x) for x in (LEAD, CALL))
    store = Store(tmp_path / "s.db")
    Ingestor(store, FakeChunks({0: good}), "2027-01-04").run()
    # second chunk dies halfway through parsing: nothing from it may persist
    bad = json.dumps(CALL) + "\n{not json"
    ing = Ingestor(store, FakeChunks({0: good, 1: bad}), "2027-01-04")
    try:
        ing.run()
        raise AssertionError("expected a parse failure")
    except json.JSONDecodeError:
        pass
    assert store.db.execute("SELECT SUM(dials) FROM arm_day").fetchone()[0] == 1
    assert store.db.execute("SELECT last_chunk FROM cursor").fetchone()[0] == 0
    # restart with the chunk repaired: counted once, and re-running changes nothing
    fixed = Ingestor(store, FakeChunks({0: good, 1: json.dumps(CALL)}), "2027-01-04")
    fixed.run()
    fixed.run()
    assert store.db.execute("SELECT SUM(dials),SUM(connected),SUM(engaged) FROM arm_day").fetchone() == (2, 2, 2)
    assert store.db.execute("SELECT COUNT(*) FROM call_log").fetchone()[0] == 2


# ------------------------------------------------------------------ governance
CFG = {"cohorts": [], "allocations": {"default": {"welcome_message": {"W1": 1.0}, "voice": {"V1": 1.0}, "caller_id": {"D1": 1.0}}},
       "prompt_version": "P1", "schedule": {"hour_weights": {}, "lead_preferred_hour": {}}}
NEW = {**CFG, "prompt_version": "P3"}
PUB = {"w": {"tenant_id": "t", "governance": {"max_requests_per_24h": 3, "approver_hours": [10, 19]}}}


class FakeGov:
    """Server that can lose the response to a request, as a crash between POST and commit would."""

    def __init__(self):
        self.apps, self.versions, self.applies = [], [], 0

    def approvals(self, wf):
        return self.apps

    def history(self, wf):
        return self.versions

    def request_approval(self, body):
        if any(a["proposal_id"] == body["proposal_id"] for a in self.apps):
            return 409, {"error": "duplicate_proposal_id"}
        a = {"approval_id": f"a{len(self.apps)}", "proposal_id": body["proposal_id"], "status": "pending"}
        self.apps.append(a)
        return 200, a

    def apply(self, wf, aid):
        a = next(x for x in self.apps if x["approval_id"] == aid)
        if a["status"] != "approved":
            return 409, {"error": "not_approved"}
        a["status"] = "applied"
        self.applies += 1
        self.versions.append({"version": 2, "approval_id": aid, "proposal_id": a["proposal_id"]})
        return 200, {"version": 2}


def _submit(gov):
    return gov.submit_approval("w", "t", 1, CFG, NEW, 600, "ts", "s", "r", {"x": 1}, 1, 0.5, {})


def test_crash_after_post_before_commit_is_adopted_not_resent(tmp_path):
    store, srv = Store(tmp_path / "s.db"), FakeGov()
    gov = Governance(store, srv, PUB)
    pid = _submit(gov)
    # simulate losing the commit: the row goes back to intent, while the server has the request
    store.db.execute("UPDATE proposals SET status='intent',approval_id=NULL WHERE proposal_id=?", (pid,))
    gov.reconcile("w", 660, "ts")
    assert len(srv.apps) == 1
    assert store.db.execute("SELECT status,approval_id FROM proposals").fetchone() == ("pending", "a0")


def test_approval_decided_while_down_is_applied_once(tmp_path):
    store, srv = Store(tmp_path / "s.db"), FakeGov()
    gov = Governance(store, srv, PUB)
    _submit(gov)
    srv.apps[0]["status"] = "approved"
    gov.reconcile("w", 660, "ts")
    gov.reconcile("w", 720, "ts")
    assert srv.applies == 1
    assert store.db.execute("SELECT status,applied_version FROM proposals").fetchone() == ("applied", 2)


def test_crash_after_apply_before_commit_does_not_apply_again(tmp_path):
    store, srv = Store(tmp_path / "s.db"), FakeGov()
    gov = Governance(store, srv, PUB)
    pid = _submit(gov)
    srv.apps[0]["status"] = "approved"
    srv.apply("w", "a0")     # applied on the server, but we died before recording it
    store.db.execute("UPDATE proposals SET status='approved' WHERE proposal_id=?", (pid,))
    gov.reconcile("w", 660, "ts")
    assert srv.applies == 1
    assert store.db.execute("SELECT status,applied_version FROM proposals").fetchone() == ("applied", 2)


def test_reproposal_after_rejection_gets_a_new_id(tmp_path):
    store, srv = Store(tmp_path / "s.db"), FakeGov()
    gov = Governance(store, srv, PUB)
    first = _submit(gov)
    srv.apps[0]["status"] = "rejected"
    gov.reconcile("w", 660, "ts")
    assert _submit(gov) != first and len(srv.apps) == 2


def test_float_noise_is_not_a_change():
    a = json.loads(json.dumps(CFG))
    b = json.loads(json.dumps(CFG))
    b["allocations"]["default"]["caller_id"] = {"D1": 0.50000001, "D2": 0.49999999}
    a["allocations"]["default"]["caller_id"] = {"D1": 0.5, "D2": 0.5}
    assert changed_dims(a, b) == [] and config_hash(a) == config_hash(b) and canon(a) == canon(b)


def test_tenant_preference_tightens_and_one_slot_stays_for_incidents(tmp_path):
    pub = {"k": {"tenant_id": "kestrel_finance", "governance": {"max_requests_per_24h": 4, "approver_hours": [10, 19]}}}
    gov = Governance(Store(tmp_path / "s.db"), None, pub)
    assert gov.caps("k") == (4, 1)
    assert not gov.routine_window("k", 10) and gov.routine_window("k", 11)


# ------------------------------------------------------------------ the disqualifiers
def test_controller_never_imports_sim_or_reads_runs_or_scenarios():
    for f in Path("controller").glob("*.py"):
        src = f.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "sim" for a in node.names), f
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "sim" or node.level > 0, f
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value
                assert "runs/" not in v and "scenarios/" not in v and "_truth" not in v, (f, v)
