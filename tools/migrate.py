"""显式 PostgreSQL 迁移，采用事务锁与校验和检查。"""

import hashlib
import os
from pathlib import Path

import psycopg

ROOT = Path(__file__).resolve().parents[1]


def migrate(database_url: str):
    with psycopg.connect(database_url) as conn:
        conn.execute("SELECT pg_advisory_xact_lock(826504120)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS schema_migration (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            )
        """)
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migration").fetchall())
        migrations = sorted((ROOT / "db/migrations").glob("[0-9]*.sql"))
        if set(applied) - {path.stem for path in migrations}:
            raise RuntimeError("database_has_unknown_migrations")
        for path in migrations:
            checksum = hashlib.sha256(path.read_bytes()).hexdigest()
            if path.stem in applied:
                if applied[path.stem] != checksum:
                    raise RuntimeError(f"migration_checksum_mismatch: {path.stem}")
                continue
            conn.execute(path.read_text())
            conn.execute(
                "INSERT INTO schema_migration(version, checksum) VALUES (%s, %s)",
                (path.stem, checksum),
            )
            print(f"Applied {path.stem}")


if __name__ == "__main__":
    from sensoryplex_gateway.settings import Settings

    url = os.environ.get("SENSORYPLEX_DATABASE_URL")
    if not url:
        url = Settings().database_url.get_secret_value()
    migrate(url)
