# Manual "How to do it" no longer stuck on preparing

Date: 2026-11-01
Plan: inline (diagnosed from production logs)

## What changed
- `knowledge_base/services/related_knowledge.py`: rank with plain `ts_rank` instead of cover density (`ts_rank_cd`).
- `knowledge_base/query_timeout.py`: `knowledge_search_timeout()` runs a search under a transaction-local `statement_timeout` and raises `KnowledgeSearchTimeoutError` when Postgres cancels it; used by `search_knowledge` and `related_knowledge`. The KB tab shows it as a search form error; agent tools and the guide / Ask AI excerpt lookups already handle `KnowledgeBaseError` (no excerpts, or a tool error).
- `projects/manual_step_guide_worker.py` (`run_manual_guide_chunk`) and `projects/person_step_question_services.py` (`answer_person_step_question_now`): close the DB connection before writing FAILED after an unexpected error or soft time limit.
- `docker-compose.yml`: `assist-celery` concurrency 2 → 8; `db` runs with `client_connection_check_interval=5s`.

## Why
On production (12 projects × 221k KB chunks) one `related_knowledge` call with a 30-token OR query took 138 s with `ts_rank_cd` versus 1.7 s with `ts_rank`. Every guide step chunk and Ask AI question runs it, so the two `assist` slots stayed busy and chunks hit the 150 s soft limit. The limit fired mid-query and left the psycopg connection busy ("another command is already in progress"), so the FAILED write failed too and guides stayed WRITING ("preparing"). Postgres kept running the abandoned queries.

## Behaviour and contract changes
- Setting `KNOWLEDGE_SEARCH_TIMEOUT_MS` (default 15000, 0 = no limit).
- Related-excerpt ordering changes slightly (same matches).
- Deploy recreates `db` (a few seconds of downtime) and `assist-celery`.

## Notes for future changes
- Run new KB search queries inside `knowledge_search_timeout()`, and evaluate the queryset inside the block (it is lazy).
- The timeout block restores the previous `statement_timeout`, so it is safe inside an outer transaction.
- Do not call the two FAILED-writing functions inside `transaction.atomic`: `connection.close()` there would only mark the connection for rollback.
