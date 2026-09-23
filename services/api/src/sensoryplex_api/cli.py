"""通过本机命令创建账户；密码只经交互或权限受限的文件读取。"""

import argparse
import getpass
import os
import secrets
from pathlib import Path

import psycopg

from .auth import password_hash, validate_user
from .contracts import audit
from .settings import Settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("username")
    parser.add_argument("--display-name", default="本地管理员")
    parser.add_argument("--roles", default="admin,operator")
    parser.add_argument("--password-file", type=Path)
    parser.add_argument("--generate-password", action="store_true")
    args = parser.parse_args()
    if args.generate_password:
        if not args.password_file:
            parser.error("--generate-password requires --password-file")
        args.password_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(args.password_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        password = secrets.token_urlsafe(24)
        with os.fdopen(descriptor, "w") as handle:
            handle.write(password + "\n")
    elif args.password_file:
        if args.password_file.stat().st_mode & 0o077:
            parser.error("password file must only be accessible to its owner")
        password = args.password_file.read_text().strip()
    else:
        password = getpass.getpass("密码（至少 12 位）：")
    roles = args.roles.split(",")
    validate_user(args.username, password, roles)
    with psycopg.connect(Settings().database_url.get_secret_value()) as conn:
        conn.execute(
            (
                "INSERT INTO "
                "console_user(username,display_name,password_hash,roles) VALUES "
                "(%s,%s,%s,%s)"
            ),
            (args.username, args.display_name, password_hash(password), roles),
        )
        audit(conn, args.username, "user.bootstrap", args.username)
    print(f"Created account {args.username}; password is not printed.")


if __name__ == "__main__":
    main()
