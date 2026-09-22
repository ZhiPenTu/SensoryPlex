from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SENSORYPLEX_", env_file=".env", extra="ignore")
    database_url: SecretStr
    api_token: SecretStr
    principal: str = "local-developer"
    pool_min_size: int = 1
    pool_max_size: int = 5

    @field_validator("api_token")
    @classmethod
    def validate_token(cls, value):
        if len(value.get_secret_value()) < 32:
            raise ValueError("api_token_must_have_at_least_32_characters")
        return value
