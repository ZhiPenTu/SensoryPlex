"""Require a real DB, use isolated schemas, and never silently skip acceptance."""

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
