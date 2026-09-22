"""需要真实数据库，使用独立 schema，绝不静默跳过验收。"""

import os
import subprocess

from sensoryplex_gateway.settings import Settings

environment = os.environ.copy()
if not environment.get("SENSORYPLEX_TEST_DATABASE_URL"):
    environment["SENSORYPLEX_TEST_DATABASE_URL"] = Settings().database_url.get_secret_value()
raise SystemExit(
    subprocess.call(
        ["uv", "run", "python", "-m", "pytest", "tests/integration", "-q"], env=environment
    )
)
