# Load test: scenarios/public_03_load.yaml

Measured on the owner's Windows 11 laptop (8 logical CPUs, 15 GB RAM), both processes pinned to 4 logical CPUs, while other work was also running on the machine. This is not a clean 4 vCPU / 8 GB Linux box: the Docker daemon was not running at the time.

Controller exit code 0. Simulator boot 72s. Total controller wall-clock 245s. Peak RSS: controller 0.47 GiB, simulator 0.31 GiB.

Day -1 is not a sim day: the first row includes the history bootstrap.

| sim day | wall s (total) | ingest s | decide s | sim advance s | events | controller events/s |
|---|---|---|---|---|---|---|
| 0 | 83 | 71.8 | 1.5 | 10.2 | 6377439 | 87071 |
| 1 | 34 | 20.9 | 1.4 | 12.0 | 840028 | 37642 |
| 2 | 42 | 26.1 | 1.8 | 13.6 | 840035 | 30066 |
| 3 | 40 | 26.1 | 1.5 | 12.5 | 840014 | 30364 |
| 4 | 45 | 29.2 | 1.7 | 13.7 | 840016 | 27195 |

Worst single tick of controller work (ingest + decide, excluding the sim's advance): 3.2s; worst decide phase 0.5s. A tick is one simulated hour.
