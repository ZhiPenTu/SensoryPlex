"""应用配置；本地凭据和媒体目录不进入公开响应。"""

from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = next(
    (p for p in Path(__file__).resolve().parents if (p / "services/api/pyproject.toml").is_file()),
    Path.cwd(),
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENSORYPLEX_", env_file=".env", extra="ignore")
    database_url: SecretStr
    api_token: SecretStr | None = None
    principal: str = "local-developer"
    pool_min_size: int = Field(default=1, ge=1, le=8)
    pool_max_size: int = Field(default=8, ge=1, le=32)
    blob_root: Path = ROOT / ".data/console-media"
    console_dist: Path = ROOT / "apps/console/dist"
    plugin_root: Path = ROOT / "plugins/python/processors"
    max_upload_bytes: int = Field(default=1024**3, ge=1, le=10 * 1024**3)
    upload_timeout_s: int = Field(default=900, ge=10, le=3600)
    session_hours: int = Field(default=12, ge=1, le=24)
    cookie_secure: bool = False
    demo_username: str = ""
    demo_password: SecretStr | None = None
    allowed_origins: str = (
        "http://127.0.0.1:8091,http://localhost:8091,"
        "http://127.0.0.1:5173,http://localhost:5173,"
        "http://127.0.0.1:8090,http://localhost:8090"
    )

    @field_validator("api_token")
    @classmethod
    def validate_token(cls, value):
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("api_token_must_have_at_least_32_characters")
        return value

    @model_validator(mode="after")
    def validate_pool(self):
        if self.pool_min_size > self.pool_max_size:
            raise ValueError("pool_min_size_exceeds_max_size")
        return self
