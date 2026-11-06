"""C11: one screen for a supervisor, and the "why did this change?" query. Reads the controller's store only.

    python -m controller.view --db state/controller.db [--workflow ks_cancel]
    python -m controller.view --db state/controller.db --why ks_cancel --version 4
    python -m controller.view --db state/controller.db --weekly
    python -m controller.view --db state/controller.db --customer nw_kyc:p0000298
"""
from __future__ import annotations

import argparse
import json
import sqlite3


def screen(db, only=None):
    now = db.execute("SELECT MAX(minute) FROM decisions").fetchone()[0] or 0
    print(f"Controller status at sim minute {now}\n" + "=" * 72)
    for wf, raw in db.execute("SELECT wf,state FROM wf_state ORDER BY wf"):
        if only and wf != only:
            continue
        st = json.loads(raw)
        d7 = db.execute("SELECT COALESCE(SUM(dials),0),COALESCE(SUM(connected),0),COALESCE(SUM(engaged),0),COALESCE(SUM(halluc),0) "
                        "FROM hourly WHERE wf=? AND minute>=?", (wf, now - 1440)).fetchone()
        pick = d7[1] / d7[0] if d7[0] else 0
        eng = d7[2] / d7[1] if d7[1] else 0
        hal = d7[3] / d7[1] if d7[1] else 0
        print(f"\n{wf}\n  last 24h   pickup {pick:.1%} | engagement {eng:.1%} | hallucination flags {hal:.2%} ({d7[0]} dials)")
        for r in st["rules"]:
            pop = f"{r['feat']}={r['val']}" if r["feat"] else "everyone"
            print(f"  live       {r['dim']} {r['arm']} for {pop}: {r['lift']:+.1%} v {r['ref']} (z={r['z']:.1f}), since day {r['since_day']}")
        for dim, arms in st["alive"].items():
            if arms:
                print(f"  testing    {dim}: {', '.join(arms)} against {st['ref'][dim]}")
        if st["did"]["bad"]:
            print(f"  caller IDs off rotation: {', '.join(st['did']['bad'])}")
        if st["locked"]:
            print("  hands off  " + "; ".join(f"{k} (changed by supervisor, v{v['version']})" for k, v in st["locked"].items()))
        inc = st["prompt"]["incident"]
        if inc:
            print(f"  INCIDENT   prompt {inc['prompt']}: {inc['reason']['source']}; proposed fix {inc.get('target') or 'none yet'}")
        pend = db.execute("SELECT kind,status,proposal_id FROM proposals WHERE wf=? AND status IN ('intent','pending','approved')", (wf,)).fetchall()
        for kind, status, pid in pend:
            print(f"  waiting    {kind} is {status}: {pid}")
        row = db.execute("SELECT ts,summary FROM decisions WHERE wf=? AND kind IN ('applied','auto_applied') ORDER BY id DESC LIMIT 1", (wf,)).fetchone()
        if row:
            print(f"  last change {row[0][:16]}: {row[1]}")
    m = db.execute("SELECT MAX(minute),AVG(ingest_ms+decide_ms),MAX(ingest_ms+decide_ms),MIN(credits) FROM metrics").fetchone()
    if m[0] is not None:
        dec = db.execute("SELECT COUNT(*) FROM decisions WHERE minute>=?", (m[0] - 60,)).fetchone()[0]
        burn = db.execute("SELECT COUNT(*) FROM decisions WHERE kind IN ('validation_run','llm_suggestions')").fetchone()[0]
        print()
        print(f"Loop health: last tick at minute {m[0]} | work per tick avg {m[1]:.0f} ms, max {m[2]} ms | decisions last hour {dec} | "
              f"credits left {m[3]} ({burn} paid calls)")
    alerts = db.execute("SELECT ts,severity,wf,message,action FROM alerts WHERE severity!='info' ORDER BY id DESC LIMIT 5").fetchall()
    if alerts:
        print("\nRecent alerts")
        for ts, sev, wf, msg, action in alerts:
            print(f"  [{sev}] {ts[:16]} {wf}: {msg} -> {action}")


def why(db, wf, version):
    """Answers from this store alone: which proposal made this version, on what evidence, who decided."""
    row = db.execute("SELECT proposal_id,kind,route,status,approval_id,payload,created_minute FROM proposals "
                     "WHERE wf=? AND applied_version=?", (wf, version)).fetchone()
    if not row:
        ev = db.execute("SELECT ts,summary,evidence FROM decisions WHERE wf=? AND kind='supervisor_edit'", (wf,)).fetchall()
        for ts, summary, raw in ev:
            if json.loads(raw).get("version") == version:
                print(f"{wf} v{version} came from a supervisor at {ts}.\n{summary}")
                return
        print(f"No record of {wf} v{version} from this controller (version 1 is the business default).")
        return
    pid, kind, route, status, aid, payload, _ = row
    p = json.loads(payload)
    print(f"{wf} v{version}: {kind} via {route}, proposal {pid}, approval {aid or 'not required (auto policy)'}")
    print(f"Summary:   {p.get('change_summary', '')}")
    print(f"Rationale: {p.get('rationale', '')}")
    print(f"Diff:      {json.dumps(p.get('diff', {}), indent=1)}")
    print("Timeline:")
    for ts, k, summary in db.execute("SELECT ts,kind,summary FROM decisions WHERE proposal_id=? ORDER BY id", (pid,)):
        print(f"  {ts[:16]} {k}: {summary}")
    print("Evidence behind the rules in force:")
    for ts, k, summary in db.execute("SELECT ts,kind,summary FROM decisions WHERE wf=? AND kind IN "
                                     "('winner_selected','arm_dropped','winner_decayed','did_flagged','did_trusted',"
                                     "'hallucination_incident','validation_run') ORDER BY id", (wf,)):
        print(f"  {ts[:16]} {k}: {summary}")


def customer(db, person):
    """stakeholders/02: why did this customer hear what they heard on this day?"""
    rows = db.execute("SELECT ts,wf,version,cohort,welcome,voice,did,prompt,disposition FROM call_log "
                      "WHERE person_id=? ORDER BY ts", (person,)).fetchall()
    if not rows:
        print(f"No calls recorded for {person} since the controller started.")
    for ts, wf, version, cohort, welcome, voice, did, prompt, disp in rows:
        print(f"{ts[:16]} {wf} v{version} cohort {cohort}: welcome {welcome}, voice {voice}, caller {did}, prompt {prompt} -> {disp}")
        row = db.execute("SELECT proposal_id,route,approval_id,payload FROM proposals WHERE wf=? AND applied_version=?",
                         (wf, version)).fetchone()
        if row:
            print(f"    live because: {json.loads(row[3]).get('rationale', '')}")
            print(f"    proposal {row[0]}, approval {row[2] or 'auto policy'}")
        else:
            print("    live because: business default or a supervisor edit; see --why for this version")


def weekly(db):
    """stakeholders/02 asks that caller ID and timing changes made without approval are reported weekly."""
    print("Changes made under auto-apply latitude (caller ID only)")
    for wf, pid, version, payload in db.execute("SELECT wf,proposal_id,applied_version,payload FROM proposals "
                                                "WHERE route='auto' AND status='applied' ORDER BY updated_minute"):
        print(f"  {wf} v{version} {pid}: {json.loads(payload)['rationale']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="state/controller.db")
    ap.add_argument("--workflow")
    ap.add_argument("--why")
    ap.add_argument("--version", type=int)
    ap.add_argument("--weekly", action="store_true")
    ap.add_argument("--customer", help="person_id: what they heard, and why it was live")
    a = ap.parse_args()
    db = sqlite3.connect(f"file:{a.db}?mode=ro", uri=True)
    if a.customer:
        customer(db, a.customer)
    elif a.why:
        why(db, a.why, a.version)
    elif a.weekly:
        weekly(db)
    else:
        screen(db, a.workflow)


if __name__ == "__main__":
    main()
