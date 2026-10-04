The original pre/post cleanup scans checked the frozen scored executable names
(`rustytransfer-*`) and Croc. Local correctness uses the native example name
`shared_key_parallel`; its processes had already exited and were joined by the
smoke helper. The [expanded final scan](expanded-independent-connections-cleanup-20261004.json)
independently checks that example and `iroh_probe` name as well. Both endpoints
are clear, no Croc listeners or leases remain, and the original Oracle INPUT
hash still matches exactly. No firewall mutation was needed for this evaluation.
The original records remain unchanged; the exact expanded checker is retained
as [audit-independent-cleanup.py](../reproduction/audit-independent-cleanup.py).
