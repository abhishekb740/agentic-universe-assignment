"""Compact view of a graded run, for development. Reads runs/ and state/, so it lives outside controller/."""
import json, sqlite3, subprocess, sys
name, base = sys.argv[1], sys.argv[2]
sys.path.insert(0, ".")
from sim.grader import grade
g = grade(f"runs/{name}", base)
for w, m in g["kpis"].items():
    d = g["delta_vs_baseline"][w]; c = m["engagement_gap_captured"]
    print(f"{w:12} concl {m['conclusion_rate']:.3f} {d['conclusion_rate']*100:+.1f}% pick {m['pickup_rate']:.3f} {d['pickup_rate']*100:+.1f}% eng {m['engagement_rate']:.3f} {d['engagement_rate']*100:+.1f}% gap {c*100 if c is not None else 0:.0f}% halluc {m['hallucinated_calls']} ({g['baseline_kpis'][w]['hallucinated_calls']})")
print(dict(g["controller_changes"]["verdicts"]), {k: v for k, v in g["governance"].items() if k != "peak_requests_per_24h" and v}, "peak", g["governance"]["peak_requests_per_24h"], "credits", g["credits_used"])
for j in g["controller_changes"]["detail"]:
    if j["verdict"] == "harmful": print("HARM", j["workflow_id"], j["version"], j["actor"], j["dims"], j["engage_delta"], j["did_health_delta"])
print(json.dumps(g["incidents"])[:900])
print(json.dumps({k: list(v.values()) for k, v in g["decaying_variant_share_after_onset"].items()}))
db = sqlite3.connect(f"state/{name}.db")
for r in db.execute("select substr(ts,6,11),wf,kind,substr(summary,1,110) from decisions where kind in('winner_selected','winner_decayed','did_flagged','hallucination_incident','prompt_recovered','apply_conflict','supervisor_edit','proposal_expired') order by id"): print(*r)
