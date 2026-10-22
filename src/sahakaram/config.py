"""Validated, environment-only configuration for the foundation phase."""

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Read SAHAKARAM_* variables without modifying user or Codex configuration."""

    model_config = SettingsConfigDict(env_prefix="SAHAKARAM_")

    instance_name: str = Field(default="Sahakaram", min_length=1, max_length=64, pattern=r"\S")
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    state_dir: Path = Path(".state")
    database_url: SecretStr | None = None
    hub_url: str = "http://127.0.0.1:8788"
    room_token: SecretStr | None = None
    control_port: int = Field(default=8787, ge=1, le=65535)
    codex_bin: Path | None = None
    auto_start_control: bool = True
