# Interrupted alert test series (do not use)

First full run of `experiments/alert_test.py`, started 2026-11-04 about 19:45.
The Mac went to sleep many times during the series (`pmset -g log`: "Entering Sleep state"
from 19:41 on), and Docker Desktop was not running afterwards.

Signs the data is invalid: firing delays of 967 to 999 s in 150 s runs, resolution after
3066 s, two baseline cases only 19 s apart (each should take about 3 min), baseline cases
"did not fire" because the stack was not running.

The old script still wrote PASS for some of these rows, because it did not check whether
the run itself was continuous. It now marks such runs INVALID (alert poll gap > 3 s, run much
longer than planned, or fewer than 95% of the planned requests). All files are kept here
only as a record. The valid series is in `results/endterm/alert_test*.csv`.
