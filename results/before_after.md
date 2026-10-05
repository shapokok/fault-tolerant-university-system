| scenario | availability_baseline | detection_s_baseline | recovery_s_baseline | failed_baseline | recovered_baseline | consistency_baseline | availability_ft | detection_s_ft | recovery_s_ft | failed_ft | recovered_ft | consistency_ft |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| app_crash | 86.22% | 0.2 | not recovered | 248 | 0 | FAIL | 99.94% | 0.24 | 0.0 | 1 | 0 | PASS |
| db_failure | 66.33% | 0.1 | 30.5 | 606 | 0 | FAIL | 90.44% | 0.15 | 29.5 | 172 | 1 | PASS |
| network_timeout | 95.39% | not detected | 28.5 | 83 | 0 | FAIL | 92.00% | 3.71 | 41.0 | 144 | 0 | PASS |
| node_failure | 22.33% | 0.16 | not recovered | 1398 | 0 | FAIL | 100.00% | 0.23 | 0.0 | 0 | 0 | PASS |
| lost_transaction | 92.78% | not detected | 28.5 | 130 | 0 | FAIL | 92.78% | 2.16 | 33.0 | 130 | 0 | PASS |
| high_load | 100.00% | not detected | 0.0 | 0 | 0 | FAIL | 100.00% | not detected | 0.0 | 0 | 0 | PASS |

Consistency reasons:

- app_crash / baseline: FAIL, 7 payments completed more than once; 7 keys charged more than once by the bank
- app_crash / ft: PASS, all checks passed
- db_failure / baseline: FAIL, 14 payments completed more than once; 14 keys charged more than once by the bank; transcript job running, 0/50 transcripts
- db_failure / ft: PASS, all checks passed
- network_timeout / baseline: FAIL, 19 payments completed more than once; 26 keys charged more than once by the bank; 75 bank charges without a completed payment
- network_timeout / ft: PASS, all checks passed
- node_failure / baseline: FAIL, 7 payments completed more than once; 7 keys charged more than once by the bank; transcript job running, 0/50 transcripts
- node_failure / ft: PASS, all checks passed
- lost_transaction / baseline: FAIL, 19 payments completed more than once; 41 keys charged more than once by the bank; 105 bank charges without a completed payment
- lost_transaction / ft: PASS, all checks passed
- high_load / baseline: FAIL, 33 payments completed more than once; 33 keys charged more than once by the bank
- high_load / ft: PASS, all checks passed
