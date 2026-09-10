from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 出厂默认密钥。任何部署都必须替换，否则会话令牌可被任意伪造。
DEFAULT_SECRET_KEY = "dev-only-change-me-please-32-characters"
MIN_SECRET_KEY_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "星河智能企业制度 AI 助手"
    app_host: str = "127.0.0.1"
    app_port: int = 8000
    app_secret_key: str = "dev-only-change-me-please-32-characters"
    session_expire_minutes: int = 480

    handbook_path: Path = Field(default=Path("员工守则_美化版.docx"))
    database_path: Path = Field(default=Path("data/enterprise_ai.db"))
    rag_index_path: Path = Field(default=Path("data/index.json"))
    rag_top_k: int = 4
    rag_min_score: float = 0.08
    rag_min_coverage: float = 0.20
    rag_chunk_size: int = 800
    rag_chunk_overlap: int = 120

    deepseek_enabled: bool = True
    deepseek_api_key: SecretStr | None = None
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-chat"
    deepseek_timeout_seconds: float = 30.0
    deepseek_max_retries: int = 1

    @field_validator("handbook_path", "database_path", "rag_index_path", mode="after")
    @classmethod
    def resolve_project_path(cls, value: Path) -> Path:
        return value if value.is_absolute() else PROJECT_ROOT / value

    @field_validator("deepseek_api_key", mode="before")
    @classmethod
    def blank_api_key_to_none(cls, value: object) -> object:
        """环境变量写成 DEEPSEEK_API_KEY= 时应视为未配置，而非空密钥。

        空字符串会被包装成 SecretStr("")，导致 enabled 判定为真并拿着空凭证
        初始化客户端，最终在启动阶段抛出 Missing credentials。
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    settings.rag_index_path.parent.mkdir(parents=True, exist_ok=True)
    _ensure_secret_key(settings.app_secret_key)
    return settings


def _ensure_secret_key(secret_key: str) -> None:
    """拒绝使用默认或过短的会话密钥启动，避免令牌被伪造。

    会话令牌由该密钥做 HMAC 签名，密钥一旦泄露或保持默认值，
    RBAC 权限体系可以被绕过。因此启动阶段直接失败而不是仅告警。
    """
    if secret_key == DEFAULT_SECRET_KEY or len(secret_key) < MIN_SECRET_KEY_LENGTH:
        raise RuntimeError(
            "检测到无效会话密钥，已拒绝启动。\n"
            "会话令牌使用该密钥签名，使用默认值或过短密钥可被伪造任意角色身份。\n"
            "请生成随机密钥：python -c \"import secrets; print(secrets.token_urlsafe(32))\"\n"
            "并将结果写入项目根目录 .env 的 APP_SECRET_KEY。"
        )
