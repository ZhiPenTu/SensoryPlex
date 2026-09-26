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
    # 受控制品仓（ADR-030）：由 tools/plugin_release.py 在构建机写出；API 只读，
    # 导入时重新计算落盘 bundle 的字节摘要，因此客户端无法伪造 release 摘要。
    release_repository: Path = ROOT / ".data/releases"
    max_upload_bytes: int = Field(default=1024**3, ge=1, le=10 * 1024**3)
    upload_timeout_s: int = Field(default=900, ge=10, le=3600)
    session_hours: int = Field(default=12, ge=1, le=24)
    cookie_secure: bool = False
    demo_username: str = ""
    demo_password: SecretStr | None = None
    # 语义检索的检索面（ADR-023）：索引持有者是常驻 index-worker，API 只转发。
    # 默认空 = **显式不可用**（`semantic_search_unavailable`），不是"曾经 501"。
    index_search_endpoint: str = ""
    index_search_token: SecretStr | None = None
    index_search_timeout_s: float = Field(default=5.0, gt=0, le=60)
    # collection 名 = vector_index_key。本切片交付的 BGE 模型（bge-small-zh-v1.5，512 维）
    # 对应的就是这一个；换模型或换维度必须改这里，且检索面会与它**逐字对账**（不等即拒绝，
    # 不会"尽力找另一个 collection"）。
    index_vector_index_key: str = "material_text_bge_small_zh_v1_5_d512_v1"
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

    @model_validator(mode="after")
    def validate_index_search(self):
        """终结点与令牌要么都不配，要么都配齐：半配置的检索面不允许启动。

        **空白等于没配**：容器编排只能表达"变量是空串"（compose 的 `${VAR:-}` 无法表达"这个变量
        不存在"，`docker compose exec -e VAR=` 同理）。把空白令牌读成缺省值，`up.sh` 才不会因为
        一个老 `.env` 少了这一项就整个栈起不来。**半配置仍然被挡住**——只给一处（任一处为空）照样
        落 `*_required`，所以"配了一半"依旧不会被当成"配好了"。
        """
        endpoint = self.index_search_endpoint.strip()
        token = self.index_search_token
        if token is not None and not token.get_secret_value().strip():
            token = None
        if not endpoint:
            if token is not None:
                raise ValueError("index_search_endpoint_required")
            self.index_search_endpoint = ""
            self.index_search_token = None
            return self
        if "://" in endpoint:
            # 这里要的是 gRPC target（`host:port`）；写成 URL 会被静默当成域名。
            raise ValueError("index_search_endpoint_invalid")
        if not self.index_vector_index_key:
            raise ValueError("index_vector_index_key_required")
        if token is None or len(token.get_secret_value()) < 32:
            raise ValueError("index_search_token_required")
        self.index_search_endpoint = endpoint
        self.index_search_token = token
        return self
