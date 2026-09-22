Known limitation. 

SeqScanCheck cannot distinguish "no index exists" from "an index exists but the planner chose a Seq Scan anyway." Both look identical in the plan. The check emits a neutral signal — "check whether an index exists on the filter column" — and relies on the LLM to call list\_indexes to disambiguate. In the rare case where the index exists but the planner ignored it, the LLM should investigate root causes (stale stats, correlation, random\_page\_cost) rather than blindly creating a duplicate index.

