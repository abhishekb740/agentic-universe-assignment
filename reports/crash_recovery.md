# Crash recovery evidence

Scenario `scenarios/public_02_incidents.yaml`. Controller killed without warning at sim minutes [900, 1200, 2520, 5400, 6540, 8940, 10800, 13500], then restarted with the same command and the same store. Run to scenario end.

| check | result |
|---|---|
| no approval applied twice | PASS |
| no proposal id requested twice | PASS |
| no auto proposal applied twice | PASS |
| no lost approval | PASS |
| no auto-apply refused by policy | PASS |
| store counters equal independent recount | PASS |

Approvals requested 17, approved 15, applied 14; auto-applied 7.

Counters in store: {'dials': 245032, 'connected': 65529, 'engaged': 35506, 'halluc': 923}

Independent recount: {'dials': 245032, 'connected': 65529, 'engaged': 35506, 'halluc': 923}

Reproduce: `python scripts/kill_test.py scenarios/public_02_incidents.yaml --kills 8`
