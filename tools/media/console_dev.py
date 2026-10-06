"""独立 schema 的本地 Console 预览；不修改正在使用的旧 Gateway 数据。"""

import argparse
import os
import secrets
from pathlib import Path

import psycopg
import uvicorn
from psycopg.conninfo import make_conninfo
from sensoryplex_api.app import create_app
from sensoryplex_api.auth import password_hash
from sensoryplex_api.contracts import audit
from sensoryplex_api.settings import Settings

from tools.migrate import migrate

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = "sensoryplex_console"
# 公开的本地演示凭据，与管理员随机密码相互独立。
DEMO_USERNAME = "demo"
DEMO_PASSWORD = "SensoryPlex-Demo-2026"


def settings():
    base = Settings()
    url = make_conninfo(base.database_url.get_secret_value(), options=f"-c search_path={SCHEMA}")
    return Settings(
        database_url=url,
        api_token=None,
        blob_root=ROOT / ".data/console-preview/blobs",
        demo_username=DEMO_USERNAME,
        demo_password=DEMO_PASSWORD,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["prepare", "serve"])
    arguments = parser.parse_args()
    config = settings()
    if arguments.command == "serve":
        uvicorn.run(
            create_app(config), host="127.0.0.1", port=8091, access_log=False, limit_concurrency=64
        )
        return
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS sensoryplex_console")
    migrate(config.database_url.get_secret_value())
    password_file = ROOT / ".data/console-preview/admin-password"
    with psycopg.connect(config.database_url.get_secret_value()) as conn:
        if not conn.execute("SELECT username FROM console_user WHERE username='admin'").fetchone():
            password_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if password_file.exists():
                if password_file.is_symlink() or password_file.stat().st_mode & 0o077:
                    raise ValueError("preview_password_file_permissions_invalid")
                password = password_file.read_text().strip()
                if not 12 <= len(password) <= 256:
                    raise ValueError("preview_password_file_invalid")
            else:
                fd = os.open(password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                password = secrets.token_urlsafe(24)
                with os.fdopen(fd, "w") as handle:
                    handle.write(password + "\n")
            conn.execute(
                "INSERT INTO console_user(username,display_name,password_hash,roles) "
                "VALUES ('admin','本地管理员',%s,ARRAY['admin','operator'])",
                (password_hash(password),),
            )
            audit(conn, "admin", "user.bootstrap", "admin")
        if not conn.execute(
            "SELECT username FROM console_user WHERE username=%s", (DEMO_USERNAME,)
        ).fetchone():
            conn.execute(
                "INSERT INTO console_user(username,display_name,password_hash,roles) "
                "VALUES (%s,'演示用户',%s,ARRAY['admin','operator'])",
                (DEMO_USERNAME, password_hash(DEMO_PASSWORD)),
            )
            audit(conn, DEMO_USERNAME, "user.bootstrap", DEMO_USERNAME)
    print("Console preview prepared in isolated schema sensoryplex_console.")
    print(f"Account: admin; password file: {password_file}")


if __name__ == "__main__":
    main()
