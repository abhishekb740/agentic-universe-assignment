# Runbook: the engineer paged when the author is on leave

**What it is.** One Python process, one SQLite file (`state/controller.db`). It reads events from the platform and proposes config changes through approvals, or auto-apply where policy allows.

**Start or restart.** `./run.sh <SIM_URL>`. Same command in both cases. It is always safe to kill it and run it again.

**See what's going on.**
```
python -m controller.view --db state/controller.db                  # status, last-24h KPIs, loop health, alerts
python -m controller.view --db state/controller.db --why WF --version N
python -m controller.view --db state/controller.db --customer PERSON_ID
python -m controller.view --db state/controller.db --weekly         # auto changes, for the client's weekly summary
```

**Alerts and what to do**
| Alert text | Meaning | Do |
|---|---|---|
| prompt fix blocked by request cap | Validated fix ready, the 24h cap is used up | Phone the client's approver |
| no validated fix for prompt | No candidate passed validation | A human picks a prompt. It retries after 12 hours |
| approval or auto-apply refused | 4xx from the platform: our payload or a policy changed | Check `proposals.reasons` for the row. Nothing was changed |
| validation failed | Usually out of credits | Check the credit balance on the status screen |
| caller ID looks spam-flagged | Traffic is already moving off it | Morning: ask telecom to check the number |
| promoted X faded | Rollback proposed | No action unless the approver asks |

**The process exited.** After 8 failed retries to the platform it exits on purpose. Fix the platform or network, then restart. No data is at risk.

**Don't**
- Edit `wf_state` or `proposals` by hand while it's running.
- Delete the store to "fix" something: it would forget experiments and re-propose. If you must, move it aside.
- Change `policy.py` to make a change go through faster without reading why each value is set.

**Tuning.** All thresholds are in `controller/policy.py` with reasons. After changing any, run all of `scenarios_own/` through `scripts/eval.py`, as well as `pytest tests`, and compare harmful changes and false winners.
