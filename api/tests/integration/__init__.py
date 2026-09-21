"""KB / RAG T12 — integration test suite.

Each module wires together two or more layers (route + repo +
dbos task body, route + RLS, engine + service mock, etc.) to
exercise the same flows the manual G-gates check, but as automated
pytest cases under the shared ``pg_engine`` fixture.

Fixture pattern (re-applied across every module) — mirrors T4-T8:
inline ``_seed_tenant_and_user`` over the RLS-bypassing ``pg_engine``,
then drive route handlers / repos through ``session_scope(tenant_id=...)``.
The plan's ``client`` / ``auth_headers`` / ``dbos_worker`` fixtures
do not exist in this repo; instead the upload-flow test invokes the
``kb.index_file`` DBOS task body directly via its ``.apply(kwargs=...)``
synchronous entry — equivalent to ``task_always_eager`` but scoped to
exactly the one ``send_task`` call we want to intercept.
"""
