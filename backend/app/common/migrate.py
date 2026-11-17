"""Apply numbered SQL migrations (app/migrations/NNNN_*.sql) once each.

Run as a one-shot container: `python -m app.common.migrate`. Each file runs in
its own transaction together with its `schema_migrations` row, so a failure
leaves the database at the previous version. An advisory lock serialises
concurrent runners.
"""

import logging
import re
import sys
from pathlib import Path

import psycopg

from app.common.config import MigrateSettings
from app.common.logging import configure_logging

log = logging.getLogger("migrate")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_NAME = re.compile(r"^\d{4}_[a-z0-9_]+\.sql$")
_LOCK_ID = 727_001


def discover(directory: Path = MIGRATIONS_DIR) -> list[Path]:
    files = sorted(directory.glob("*.sql"))
    for f in files:
        if not _NAME.match(f.name):
            raise ValueError(f"migration file name must look like 0001_name.sql: {f.name}")
    return files


def apply_migrations(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the names applied by this call."""
    files = discover(directory)
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_ID,))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
            done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
            for f in files:
                if f.name in done:
                    continue
                log.info("applying %s", f.name)
                with conn.transaction():
                    conn.execute(f.read_text())  # type: ignore[arg-type]
                    conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (f.name,))
                applied_now.append(f.name)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_ID,))
    return applied_now


def main() -> int:
    settings = MigrateSettings()
    configure_logging(settings.log_level)
    applied = apply_migrations(settings.database_url.get_secret_value())
    log.info("migrations done: %s", ", ".join(applied) if applied else "nothing to apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
