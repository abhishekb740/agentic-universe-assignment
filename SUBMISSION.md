# Outcome-First Agent Loop — Submission

**Name:** Abhishek Bhagat · **Date:** OWNER_DATE · **Hours actually spent (be honest):** OWNER_HOURS · **Language / storage:** Python 3.12, SQLite (WAL)

> **How this was built.** The README allows and expects AI tools. This controller and this document were written with Claude Code doing most of the typing, directed and reviewed by me. Every number below comes from a run in this repository and can be reproduced with the commands given.

---

## 0. TL;DR
An always-on controller that ingests events exactly once, tests welcome and voice in concurrent arms, promotes winners per population through human approval, retires winners that decay, moves traffic off unhealthy caller IDs, and sends prompt fixes to a human with validation evidence.
public_01: pickup +10.3%, engagement +5.2%, 24% of the engagement gap, 0 harmful changes. public_02: pickup +23.7%, engagement +2.5%, 29% of the gap, 2 harmful of 21, bad prompt fixed in 90 minutes, hallucinated calls 157 against 416.
Deliberately not built: per-person call time, credit and attention fairness across tenants, prompt optimisation for engagement.

## 1. Assumptions
1. The API contract and workflow policies are the truth. Where a memo disagrees, the contract wins (the CTO asks for this).
2. Allocation sampling is independent per dimension and per call within a config version, so arms inside one version are effectively randomised. Stratified analysis relies on this.
3. Feature effects are mostly main effects within one feature. I do not test two-feature interactions directly, because there isn't the traffic for it (section 5).
4. Tenant preferences in memos are real constraints when they are stricter than policy. Kestrel's "two a day, not before 11:00" is honoured even though policy allows four.
5. A prompt-safety fix may exceed a client's soft preference for request count, but never the contract's cap, and it still goes to a human.
6. Approval decisions are not influenced by how good the proposal looks. The simulator rejects at a base rate, so a rejection tells me nothing about the evidence, and I re-propose on fresh data after a cooldown.
7. Hour-of-day pickup patterns in 14 days of history stay valid for the scenario. Nothing re-estimates them (a known weakness).
8. Compliance's "named human approval" is satisfied by the `/approvals` flow, since a controller has no other route to a person.
9. The grader's oracle includes things I cannot reach in the time available, so engagement gap capture will never approach 100% within 8 to 10 days.

## 2. Scope decisions
| # | Capability | Built / Partial / Cut | Why | What it would take to finish |
|---|---|---|---|---|
| C1 | Event ingestion | Built | Everything rests on it | Streaming parse for very large chunks |
| C2 | One cohort object | Built | Product non-negotiable. Deterministic IDs from the definition | Nothing for this scope |
| C3 | Bootstrap from history | Built | Free information: prompt baseline, DID track record, hour patterns | Nothing |
| C4 | Call time | Partial: per cohort. Per-person cut | Cohort weights gave most of the conclusion lift at a fraction of the cost. Each person has 1 to 2 connected calls in history, too few to infer an hour | Person-level posterior with cohort prior, and a test that it beats the cohort rule |
| C5 | Caller ID health | Built | Largest pickup effect, and low risk | Re-probing retired DIDs after a cooldown |
| C6 | Variant experiments | Built | Core of the role | Direct two-feature interaction tests |
| C7 | Promotion, decay, rollback | Built | A winner that fades is a live harm | Faster detection needs a larger holdback |
| C8 | Hallucination response | Built | Regulated client, highest severity | Engagement check after a fix (section 13) |
| C9 | Governance client | Built | Absolute constraint | Nothing for this scope |
| C10 | Impact, leads, dry run | Partial | Real population shares and bounded exposure, but no replay against historical cohorts | Counterfactual replay |
| C11 | Supervisor view | Built, minimal CLI | Anita asked for one screen, not a product | Web view per tenant |
| C12 | Audit store | Built | Floor requirement, IRDAI question answered per customer | Retention and export |
| C13 | Crash recovery | Built and demonstrated | SRE non-negotiable | Nothing |
| C14 | Loop observability | Partial | Metrics table and status line. No external exporter | Prometheus endpoint |
| C15 | Fairness across tenants | Cut | Only reachable problem is credits, and total use is far below the limit (8 of 200 on public_02) | Per-tenant credit budgets and an approver-attention queue |

**Principle.** First anything that can hurt a client or get us disqualified (governance, crash safety, audit, prompt safety). Then whatever moves a primary KPI per unit of risk. Things that look good in a demo but need data we don't have (per-person timing) lost out, even though Product and Sales both wanted them.

## 3. Stakeholders

### 01 Nikhil Rao, VP Sales
- **Accepted:** speed where evidence allows. Daily looks, successive elimination, batched requests rather than many.
- **Declined:** a blended score (Product and the README forbid it), "winning by lunch, live by evening" (one morning of data on 20% of traffic is noise), and rolling out the persuasive prompt (see below).
- **Wrong claims:** "Kestrel has turned on auto-apply for everything": `ks_cancel` allows only schedule, caller ID, welcome and voice, with `max_shift` 0.2, not prompts or cohorts. "Northwind is fine with auto-apply on KYC": `nw_kyc` has auto-apply disabled, and Northwind's compliance head requires human approval. The persuasive prompt's success was in another account, and promoting across clients is out of scope; its summary alone also says nothing about hallucination, and it would need validation here.
- **Reply:** Nikhil, you'll have honest per-client numbers for the summit: pickup and engagement, shown separately, because Product and our clients need them separate. I can't push winners by lunch: half a day of data picks noise, and Northwind requires a human approval on anything a customer hears. Kestrel's auto-apply covers limited dimensions, not everything, and KYC has it off. "It improved itself, and can explain every change to your regulator" is a stronger BFSI story than a single score. Happy to build the slide with you.

### 02 Meera Iyer, Compliance, Northwind
- **Accepted, all of it:** everything customer-facing goes through approval, even on `nw_renewal`. Script changes carry a validation run. Requests go out only in approver hours (safety fixes wait for opening too). Auto latitude is used for caller ID only and is listed by `controller.view --weekly`. `--customer <person_id>` answers the IRDAI question.
- **Pushed back on:** only that "slow and right" cannot mean no exposure at all. Testing an unknown variant has a risk, which I bound (at most 20% of calls per untested arm, or 30% when a dimension has one alternative) and disclose in each dry run. On public_02 one exploration start on `nw_kyc` was judged harmful, at -1.2%.
- **Wrong claims:** none found. Hours and policy match the contract.
- **Reply:** Meera, your conditions are built in. Nothing a customer hears changes without your approver; script changes carry validation evidence; requests arrive in working hours. Caller ID rotation uses the signed latitude and appears in your weekly summary. For any customer and day we can produce the variant and why it was live, from our own records, well inside 24 hours. One thing to be open about: testing an untried variant carries some risk before we have data. We cap its exposure and show that cap in every request, so your approver can refuse it.

### 03 Kabir Menon, Platform and SRE
- **Accepted:** kill test (section 9), idempotency everywhere, actionable alerts carrying an action, health in `controller.view`.
- **Pushed back on:** SQLite is new state, though embedded, with no server to run. At 2 a.m. it's a file: back it up, and restart resumes. For production with many workers it should move to our existing Postgres; schema and transactions are plain SQL.
- **Wrong claims:** none.
- **Reply:** Kabir, kill test is in `scripts/kill_test.py`: eight unannounced kills, then checks against the sim's log and an independent recount. No double apply, no lost approval, no lost event. No new server: state is one SQLite file on one node, resumable by rerunning the same command. For production it should move to your Postgres. Load numbers and what breaks at 10x are in section 8. Pages only fire for things needing a person, and each says what to do.

### 04 Anita Desai, Kestrel
- **Accepted:** at most two requests a day and none before 11:00 (`TENANT_PREFS`), one batched request, one screen, her edits become the new base and that dimension is locked.
- **Pushed back on:** one exception. A hallucination fix may be requested before 11:00 and may be a third request, and it can propose reversing her prompt. It never applies without an approver. Also, "tell me before anything changes" is met by the approval itself plus a notice on her screen for caller rotation, not a separate message channel, which isn't built.
- **Wrong claims:** none on policy; note that policy would allow four requests, and I use her lower number.
- **Reply:** Anita, your prompt edits stand. I'll never undo one behind your back. If an edit fails safety checks, you'll get a request with the evidence, and you decide. Otherwise: one batched request a day, after 11:00, with a second slot kept for safety problems. One screen shows what's being tested, what's winning, what I need from you and what changed. And I won't put you on a word-for-word compliance script: fixes described that way are rejected before validation even runs.

### 05 Tara Kulkarni, Product
- **Accepted:** separate KPIs, one cohort object, proposals with impact, diff, leads and dry run, expiry treated as rejection and re-queued fresh under a new ID, evidence only from arms that ran (strata where either arm is absent contribute nothing), winners stay in their workflow.
- **Pushed back on:** "all P0". Caller health and the view are built. Per-person call time is cut, with cohort-level time instead (section 5).
- **Wrong claims:** none on contract. "Per-person is the headline" is a wish, and data doesn't support it yet.
- **Decision she asked for:** minimum 120 connected calls per arm in a segment, 250 for the whole workflow, evaluated daily. Cost: on public_01 most promotions came on days 3 to 6. Lowering it would be faster, with more false winners. Measured rate in section 5.
- **Reply:** Tara, all your definitions hold, including one cohort object with one ID everywhere. My calls on your open questions: 120 connected calls per arm per segment, daily evaluation, false discovery controlled at 10%, costing about three to six days to a segment winner. I'm saying no to per-person call time for now: most people have one or two connected calls in history, too few to learn an hour from. Cohort-level timing shipped and already lifted pickup. Impact score means traffic moved times population touched, so your ranking is meaningful, though not a forecast of value.

### 06 Aryamann, CTO
- **Accepted:** choose and explain (section 2), spend credits only where they change a decision (8 of 200 on public_02), docs for whoever gets paged (`docs/RUNBOOK.md`).
- **Pushed back on:** nothing.
- **Wrong claims:** none found. His note that memos contain errors was right: two false claims in Sales' memo above.
- **Reply:** Aryamann, I said no to Sales on blended scores, lunchtime rollouts and blanket auto-apply, and no to Product on per-person timing this round. Biggest weakness: it learns slowly on small segments, capturing 24 to 29% of the engagement gap, and it can miss a real 8 to 10% segment winner. What breaks first at scale is one process doing everything, covered with a partition plan in section 8. Harmful changes and false winners are stated in full rather than tuned away.

## 4. Architecture
```
 simulator (HTTP only)                       controller process
 ------------------                         ------------------------------------------
 /events/chunks  --------------------------> ingest ---(one transaction per chunk)---+
 /sim/state, /workflows, /config                                                     v
                                                                  SQLite WAL: cursor, leads, arm_day,
                                                                  hourly, hour_seg, call_log, inbox,
                                                                  wf_state, proposals, decisions,
                                                                  alerts, metrics
                                                                                     |
                  brain (per workflow): daily look <-- experiments/stats (CMH + BH)  |
                                        caller health, prompt safety, schedule  <----+
                                        desired config  --> cohorts compiler
                                                 |
 /approvals, /apply, PUT /config  <-------- governance: intent row -> send -> reconcile each tick
 /validation, /llm/suggest-fixes  <-------- prompt safety only (LLM offers candidates, never decides)

 controller.view (read-only on SQLite): supervisor screen, --why, --customer, --weekly
```
All state is in one SQLite file. On restart there is no special path: `run()` reads the cursor, ingests what's new, reconciles open proposals against the server, and continues. The store is tied to a run by scenario name and a clock that must not go backwards; otherwise the old file is set aside.

## 5. The learning loop
**Cohorts.** One rule form, `(dimension, feature, value, arm)`, statistics at single-feature level. `cohorts.py` compiles rules into first-match cohorts with intersections, most specific first, and IDs derived from the definition such as `c:age_band=60+&lang=hi`. Minimum 120 connected per arm in a segment: at base 0.5, a 20% lift (0.10) with 120 against about 500 has standard error about 0.05, so z about 2. Fragmentation is limited by at most 3 segment rules per dimension and 20 cohorts, dropping the weakest rule first.

**Experiment design.** Deterministic allocation: control stays largest, each untested arm gets 20% (30% if it's the only alternative). Welcome and voice run factorially via independent sampling, so each arm's marginal comparison stays balanced across the other dimension. I don't estimate welcome × voice interactions: at about 950 connected calls per day per workflow split across up to 12 cells, there isn't power in 8 to 10 days. Successive elimination moves traffic to arms still alive: an arm goes once it has 600 connected, with no global signal (z < 0.5) and no segment hint (best z < 1.5), or immediately on harm (z <= -2.5 and lift <= -3%).

**Deciding a winner.** Cochran-Mantel-Haenszel over strata of (day, config version, cohort, prompt). Benjamini-Hochberg at q = 0.10 across all tests in a look, plus floors z >= 2.4 and lift >= 3%, sample minimums, a pickup veto, and "recent two days not negative". BH doesn't correct for daily peeking, hence the z floor, and the empirical check below.
**False-winner rate, measured.** Four worlds with every effect removed and new seeds (12 workflow runs, 4,518 tests): 3 false winners, all in one seed. That's 0.25 per workflow run, or about 1 per 1,500 tests. It's not negligible. It's tolerable because holdback and decay checks stay on, and the arms tested there are by construction no worse than control. Against truly bad arms a false winner would cost more. Lowering q would cut this at the cost of slower real wins.

**Decay and rollback.** A winner keeps a 15% control holdback. From age 2 days: reversal (z <= -2 and negative). From age 3: fade (the recent two whole days' lift below 40% of the promotion-time lift, with z >= 2.5 on the fall and an absolute drop of at least 5 points). Then a rollback goes through approval, and the arm is blocked in that workflow. On public_01 W3 share went 0.71, 0.71, 0.69, 0.11, 0 by day after onset; about 3 days to detect. A real fade from +15% to -7% cannot be seen much faster with 15% holdback at this volume.

**Call time and caller ID.** History gives hour-by-feature pickup. A chi-square per degree of freedom >= 3 chooses the one feature with the strongest interaction, then cohort weights are `(rate/mean)^3` clipped to [0.25, 3], with no zero weights and no change to window or capacity. It is observational and not randomised, so it could be wrong, but it's verified by pickup against baseline in the grade. Caller IDs with no history are probed at 5% (more on low-volume workflows); a probe becomes trusted after 350 dials with z >= -1.5, or retires at z <= -3. Trusted DIDs share evenly because load measurably depresses pickup. An established DID needs a fall against both peers and its own past. I added this after `nw_kyc`'s D1, overloaded by history, was wrongly flagged in an early run.

**Guardrail.** Pickup and engagement are computed and tested separately. A variant win is vetoed if the arm's own pickup z <= -2.5. A side effect: cohort scheduling changes who connects, so workflow-level engagement can drift without any variant being worse (`nw_kyc` -0.6% on public_02, with pickup +20.5%). `nw_kyc`'s primary is pickup, and the guardrail there is a 2% engagement drop, so that stays within bounds. On `own_c`, a private world, `nw_kyc` fell -2.3% and `nw_renewal` -3.8% in engagement, which would breach or come close to the guardrail. No code currently detects workflow-level guardrail drift (section 13).

## 6. Governance
```
intent -> pending -> approved -> applied
   |         |          |-> lapsed  (window missed: re-propose)
   |         |          |-> stale   (config moved: rebuild on new base, never force)
   |         |-> rejected (cooldown 4h, then fresh data under a new id)
   |         |-> expired  (treated as rejected, re-queued fresh)
   |-> stale / refused (refused = contract bug, raises an alert)
```
**Batching and timing.** One open request per workflow. `desired()` always folds everything currently wanted into one config. Routine requests go in the approver window, with one daily slot reserved for incidents. Kestrel: 1 routine plus 1 reserved, after 11:00. Peak requests per 24h on public_02 stayed at 2 per workflow against limits of 2 to 4. Requests made late in the day can spill past a short TTL: on `own_c`, a prompt fix asked at 18:00 expired overnight and was re-sent at opening.

**Auto-apply.** Only for caller ID, on workflows that allow it, stepped within `max_shift`. Voluntarily not used for welcome and voice on Kestrel though policy allows it, since customers hear those. A routine auto change waits while an approval is pending, as it would make that approval stale. Flagged DIDs don't wait.

**Impact, leads, dry run.** `affected_leads` = daily batch × population share touched, using real shares. `impact_score` = share touched × largest default allocation move (minimum 0.1), floor 0.7 for prompts. It measures how much changes, not expected value. The dry run lists post-change allocation, the share of traffic on unproven arms, and projected effect of each promoted rule (population share × 85% × estimated lift). Those lifts carry winner's curse and are optimistic. It's not a replay against history (C10 is partial).

**Supervisor edit mid-experiment.** The `config_changed` event marks the edit. The new default becomes the reference, tests on that dimension stop, the dimension locks for the rest of the run, and any approved change on the old base fails at apply and is rebuilt. Versions are strata, so pre-edit data isn't mixed in. Public_02 shows it: voice locked on `nw_renewal` at v8, default untouched. One nuance: a segment rule's holdback follows the new reference, so v9 changed voice inside one cohort, and that went through approval. Locking until the end is deliberately conservative, and loses upside. Prompt edits get a screen rather than a lock.

## 7. Incidents
**Detection.** Live: flags >= 5, Poisson excess z >= 3.5 and rate >= 2.5× baseline, in the trailing 24h since adoption. Triggered: a supervisor prompt change buys a 3-credit screen (n=300) and fails below pass rate 0.985, stricter than policy's 0.97, because 0.97 would pass a prompt several times worse than production. False positives: none in 9 runs, 7 of which had no bad prompt; that bounds it only roughly. Caller ID: z <= -3 and ratio <= 0.85, and on established DIDs the same again against their own past. Across the same 9 runs every flag was a truly unhealthy DID (D4 from history, or the injected one), and no healthy DID was flagged.

**Fix choice.** Last known good first, since it has real engagement history here: 5 credits to validate for evidence. Only if that fails do I pay 3 credits for LLM suggestions, then validate them. Candidates described as strict or verbatim are rejected first (Anita's memo). Validation can't see engagement, so "lowest hallucination" alone would choose the script that kills calls. That fallback path has not been exercised in any run, since last known good passed each time.

**Verify.** After apply, the prompt is watched by the same live detector, so a fix that fails raises a new incident. I do not check engagement after a fix.

**Timeline, public_02, `ks_cancel`:**
| Sim time | What happened |
|---|---|
| D2 10:00 | D2 spam-flagged (hidden) |
| D2 14:00 | Detected at 71% of peers, z=-5.8; auto-rotation starts, finishing at 15:00 (max_shift 0.2) |
| D3 | D2 share 0, from 0.144 on the incident day |
| D3 11:00 | Routine batched request submitted |
| D3 11:30 | Supervisor ships prompt P2 |
| D3 12:00 | Seen on tick. Screen: 0.9367 pass, fail. P1 validated at 0.99. Fix requested |
| D3 13:00 | Fix approved and applied as v9. MTTR 90 min (grader). Older batch approval now stale, dropped, rebuilt |

Hallucinated calls on `ks_cancel`: 60 against 330 baseline. Credits: 8 of 200.

## 8. Scale
Measured on `public_03_load` (12 workflows, 840,000 events a simulated day). Full table in `reports/public_03_load.md`. **Caveat:** this was my Windows laptop with both processes pinned to 4 logical CPUs, 15 GB RAM available, and other work running. It is not a clean 4 vCPU / 8 GB Linux box, as the Docker daemon wasn't running. Treat it as indicative.

| Measure | Result |
|---|---|
| Wall-clock per simulated day, including the sim's own advance | 34 to 45 s, against the 10 minute target |
| Ingest throughput, live days | about 27,000 to 38,000 events/s |
| History bootstrap | 5.5M events inside day 0's 72 s of ingest |
| End-to-end decision lag | worst tick 3.2 s of controller work per simulated hour; worst decide phase 0.5 s |
| Peak memory | controller 0.47 GiB, simulator 0.31 GiB |
| Store after the run | 566 MB, 3.24M rows in `call_log` |

**What breaks first at 10x** (estimates from those numbers, not measured). The in-memory lead map grows with every lead and is never evicted: 2.3M leads now, 23M at 10x over the same 12 days, roughly 4 to 5 GiB, which gets close to the 8 GB limit, and worse the longer it runs. Fix: evict concluded leads, and look up misses in SQLite. Next is the single writer: about 280 s of ingest a simulated day still fits the budget, but at 100x it wouldn't. Then the store size, since `call_log` has no retention.

**Partitioning.** Workflows share no statistics or governance state, so partition by workflow, with tenant as the grouping unit for approver limits. One worker per partition, each owning its cursor and state in Postgres, and a shared consumer writing to a queue so that chunks are processed once per partition. Credits become a shared budget per tenant (C15), which is the one thing that has to coordinate.

## 9. Reliability
**Method.** `scripts/kill_test.py` starts public_02, kills the controller without warning (TerminateProcess on Windows, SIGKILL on Linux) at 8 random points, restarts with the same command, then runs to the end. It checks the sim's log and independently recounts every chunk. `reports/crash_recovery.md`:

| Check | Result |
|---|---|
| No approval applied twice | PASS |
| No proposal ID requested twice | PASS |
| No auto proposal applied twice | PASS |
| No lost approval | PASS |
| No auto-apply refused by policy | PASS |
| Store counters equal independent recount | PASS |

Kills land wherever the process is, usually waiting on `advance`, so narrow windows are also unit-tested: crash after POST before commit, approval decided while down, crash after apply before commit (`tests/test_controller.py`).

**What can be lost.** Nothing durable. A validation run in flight when killed can be paid for twice. A `client_notice` or audit row can be duplicated. Wall-clock time passes in production, so an approval whose window ends during downtime lapses and gets re-proposed.

**Idempotency.** Chunk cursor and deltas commit together. `proposal_id` is `workflow-vBase-configHash-n`, committed before sending; retries reuse it, and the server's duplicate check backs it up.

**4xx/5xx.** Transport errors and 5xx retry with backoff up to 8 times, then the process exits so a supervisor can restart it. 409 stale drops and rebuilds. 409 duplicate adopts. 410 lapsed re-proposes. 403 or 422 records "refused" and alerts: it's a bug on our side, and retrying would not help.

## 10. Audit & observability
```
$ python -m controller.view --db state/b02.db --why ks_cancel --version 9
ks_cancel v9: prompt_fix via approval, proposal ks_cancel-v8-7264815b8980-1, approval apr_00006
Summary:   SAFETY: replace prompt P2 with P1 (hallucination incident).
Diff:      {"prompt_version": {"from": "P2", "to": "P1"}}
Timeline:
  2027-01-07T12:00 proposal_intent: SAFETY: replace prompt P2 with P1 (hallucination incident).
  2027-01-07T12:00 proposal_submitted: approval apr_00006 requested
  2027-01-07T13:00 applied: prompt_fix applied as v9 (approval apr_00006)
Evidence behind the rules in force:
  2027-01-07T12:00 validation_run: P2: pass rate 0.9367 on n=300
  2027-01-07T12:00 hallucination_incident: P2 failing safety: ...
  2027-01-07T12:00 validation_run: P1: pass rate 0.99 on n=500
```
Output shortened. `--customer <person_id>` gives each call with its variant, version, and why that version was live. "What happened after" comes from the status screen's last-24h KPIs and later decisions. The approver's name isn't available: the API provides an approval ID only.

**Page:** prompt fix blocked by the request cap, no validated fix for a bad prompt, approval or auto-apply refused with 4xx (our bug), the process exiting after retries. **Don't page, show on screen:** DID flagged and rotated, winner rolled back, supervisor edits, ordinary rejections. Today these are severities in the `alerts` table; nothing actually delivers a page.

## 11. Results
**public_01** (`reports/public_01.md`)
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | gap captured | hallucinated (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.392 | +17.8% | 0.265 | +11.0% | 0.514 | +4.8% | 27% | 58 (47) |
| nw_kyc | 0.448 | +23.3% | 0.273 | +12.8% | 0.632 | +7.0% | 26% | 23 (21) |
| nw_renewal | 0.476 | +13.4% | 0.285 | +8.1% | 0.571 | +4.6% | 21% | 36 (42) |
| ALL | 0.432 | +17.3% | 0.273 | +10.3% | 0.557 | +5.2% | 24% | 117 (110) |

18 changes: 12 beneficial, 6 neutral, 0 harmful. 13 requests, 12 approved, 1 rejected, none lapsed or expired. Credits 0.

**public_02** (`reports/public_02.md`)
| workflow | conclusion rate | Δ | pickup | Δ | engagement | Δ | gap captured | hallucinated (base) |
|---|---|---|---|---|---|---|---|---|
| ks_cancel | 0.385 | +55.3% | 0.254 | +38.3% | 0.515 | +8.1% | 54% | 60 (330) |
| nw_kyc | 0.456 | +21.6% | 0.291 | +20.5% | 0.588 | -0.6% | -6% | 36 (31) |
| nw_renewal | 0.484 | +13.5% | 0.293 | +11.8% | 0.550 | +0.4% | -5% | 61 (55) |
| ALL | 0.433 | +29.5% | 0.275 | +23.7% | 0.543 | +2.5% | 29% | 157 (416) |

21 changes: 15 beneficial, 4 neutral, 2 harmful. Credits 8.

**Where it helped.** Pickup, mostly through caller spread and hour weights. Note `ks_cancel`'s +55% is largely avoiding the baseline's damage from the flagged DID and the bad prompt, not new lift. **Where it was harmful:**
- `nw_kyc` v2, -1.2% expected engagement: the exploration start. The price of an untested arm at 30%. DID health in the same change rose +7.5%.
- `nw_renewal` v7, -1.6%: a routine change that dropped W2 and raised W3 from 70% to 85% when W3 was already decaying. Fade detection was about a day behind, and the rollback (v10) was then judged +9.2%. Elimination acting while a winner fades is the flaw: it should wait until the winner has held up in recent data.
- Negative gap capture on `nw_kyc` and `nw_renewal` in public_02 means exploration and timing cost about what the variants gained there. That's a real miss: it missed the true `nw_renewal` W2 segment effect, dropping W2 as "no signal".

**Private worlds** (permuted truth): `own_a` pickup +9.9%, engagement +0.6%, 1 harmful; `own_c` pickup +20.8%, engagement -1.3%, 1 harmful; null world `own_b` pickup +12.5%, engagement +1.0%, no winners. Public results are at the better end. Expect hidden-scenario results closer to these.

**Disclosure.** The public truth sections were visible while developing. They were used as a sanity check, and tuning changes such as the elimination threshold came from seeing real effects get dropped. No variant, segment, or scenario name is hard-coded, and a test enforces the no-`sim`, no-`runs/` rule. Some fitting to public results by a person looking at them can't be excluded, which is why the private worlds are reported.

## 12. Trade-offs & rejected alternatives
| Option | Why considered | Why rejected / what instead |
|---|---|---|
| Thompson sampling | Standard, captures more during the test | Allocation changes need approval and are rate limited, so it can't update quickly. Also harder to explain to an auditor. Fixed arms plus elimination |
| Bayesian posterior for winners | No peeking problem in name | Needs priors I'd have to defend. CMH + BH is explainable to compliance, with its false-winner rate measured |
| Auto-apply welcome and voice on Kestrel | Allowed by policy, faster | Customers hear those, and Anita asked to know first. Trading speed for trust |
| Postgres | SRE already runs it | Needs a server in `run.sh` and adds no value on one node. Move there for production |
| Two-feature tests | Truth might be interactions | Multiplies tests and splits traffic. Intersections arise only when rules overlap |
| Per-person call time | Product's headline | Too little data per person |
| Validation-led prompt upgrades | Reduce hallucination everywhere | Validation can't see engagement and would favour a strict script. Prompts can't be split within a workflow, so there's no concurrent control |
| Larger holdback | Faster decay detection | Costs lift every day to speed up a rare event |

## 13. Known weaknesses
1. **Slow on small segments.** Real 8 to 10% segment effects can be eliminated (W2 in `nw_renewal`). Gap capture of 24 to 29%.
2. **No workflow-level guardrail check.** The veto is per arm. Engagement drift from scheduling mix isn't detected, as `own_c` showed (-3.8% on `nw_renewal`).
3. **False winners exist:** 0.25 per workflow run on null worlds, clustered in one seed.
4. **Exploration can be judged harmful**, at about one change per run in worlds with bad arms.
5. **Peeking is handled by a floor and measured,** not corrected formally. No always-valid sequential test.
6. **No engagement check after a prompt fix.** Strict or verbatim filtering depends on catalogue wording, which is brittle.
7. **Supervisor lock never expires** within a run, which loses upside after one edit.
8. **Schedule weights are observational,** set once from history, and never re-estimated.
9. **Single process,** decisions in series. Retired DIDs are never re-probed.
10. **Fade rollback is both late and final:** about a day behind on public_02, and it blocks that arm for good in that workflow, even if the fall was noise. Elimination can also add traffic to a winner that's fading.
11. **Alerts are not delivered;** they sit in a table and on the screen. The approver's name isn't recorded because the API doesn't give it.
12. **`call_log` has no retention policy,** and grows about linearly with dials.

## 14. If this went to production at Formi next month
1. **Shadow mode, days 1 to 3.** Real events, with proposals logged but not sent. Compare with what supervisors did.
2. **Move storage to Postgres, and wire alerts to on-call,** days 3 to 5, and rerun the kill test against it.
3. **Workflow-level guardrail monitor,** before any client exposure. It's the worst known gap for a regulated client.
4. **One workflow live, with Meera's approvers walked through a real proposal,** days 6 to 8. Their feedback on the proposal format matters more than my guesses.
5. **Sequential testing review with a statistician,** then false-winner measurement on real traffic using A/A arms.
6. **Rollout to Kestrel, with Anita's screen in her hands,** by the end of week two, followed by a partition by tenant once there are more than a few dozen workflows.
