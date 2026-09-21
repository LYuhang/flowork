#!/usr/bin/env python3
"""Grant DBOS data-plane access to Flowork's long-lived database roles.

``dbos migrate --app-role`` applies these grants when it creates the schema,
but does not re-apply them when an existing DBOS schema is already current.
Flowork's cross-tenant scheduler atomically writes its domain row and enqueues
the DBOS workflow through ``vibecanvas_maintenance``, so both runtime roles
need the DBOS-recommended DML/function privileges after every migration.
"""
from __future__ import annotations

import os

import psycopg


_ROLES = ("vibecanvas_app", "vibecanvas_maintenance")


def _admin_url() -> str:
    value = os.environ.get("DBOS_ADMIN_DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DBOS_ADMIN_DATABASE_URL is required")
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def provision() -> None:
    with psycopg.connect(_admin_url(), autocommit=True) as connection:
        if connection.execute(
            "SELECT to_regnamespace('dbos') IS NOT NULL"
        ).fetchone() != (True,):
            raise RuntimeError("DBOS schema is missing; run dbos migrate first")

        for role in _ROLES:
            # Role names are a closed source-code constant, never deployment
            # input. The statements intentionally mirror DBOS's
            # ``migrate --print-user-role`` output.
            connection.execute(f'GRANT USAGE ON SCHEMA "dbos" TO "{role}"')
            connection.execute(
                f'GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA "dbos" '
                f'TO "{role}"'
            )
            connection.execute(
                f'GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA "dbos" '
                f'TO "{role}"'
            )
            connection.execute(
                f'GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA "dbos" '
                f'TO "{role}"'
            )
            connection.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "dbos" '
                f'GRANT ALL ON TABLES TO "{role}"'
            )
            connection.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "dbos" '
                f'GRANT ALL ON SEQUENCES TO "{role}"'
            )
            connection.execute(
                f'ALTER DEFAULT PRIVILEGES IN SCHEMA "dbos" '
                f'GRANT EXECUTE ON FUNCTIONS TO "{role}"'
            )


if __name__ == "__main__":
    provision()
