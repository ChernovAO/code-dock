"""Настройки `core` из переменных окружения. Читаются один раз на старте."""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self


class SettingsError(RuntimeError):
    """Обязательная переменная окружения не задана."""


@dataclass(frozen=True)
class Settings:
    repos_dir: Path
    opencode_url: str
    opencode_password: str = field(repr=False)

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> Self:
        return cls(
            repos_dir=Path(_required(env, "REPOS_DIR")),
            opencode_url=_required(env, "OPENCODE_URL").rstrip("/"),
            opencode_password=_required(env, "OPENCODE_SERVER_PASSWORD"),
        )


def _required(env: Mapping[str, str], name: str) -> str:
    # Значение не обрезается: opencode берёт пароль из того же окружения как есть.
    value = env.get(name, "")
    if not value.strip():
        raise SettingsError(f"Не задана переменная окружения {name}")
    return value
