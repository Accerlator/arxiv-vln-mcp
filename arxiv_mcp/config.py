"""Environment-backed configuration for the Arxiv MCP server."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _integer(name: str, default: int, minimum: int = 1) -> int:
    raw = os.getenv(name, str(default))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return value


def _floating(name: str, default: float, minimum: float = 0.0) -> float:
    raw = os.getenv(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if value <= minimum:
        raise ValueError(f"{name} must be > {minimum}")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    cache_dir: Path
    max_results: int
    request_interval: float
    request_timeout: float
    cache_ttl: int
    max_concurrent: int
    log_level: str

    @classmethod
    def from_env(cls) -> "Settings":
        cache_dir_str = os.getenv("ARXIV_CACHE_DIR", "./arxiv_mcp/cache")
        settings = cls(
            cache_dir=Path(cache_dir_str),
            max_results=_integer("ARXIV_MAX_RESULTS", 100),
            request_interval=_floating("ARXIV_REQUEST_INTERVAL", 3.0, minimum=0.0),
            request_timeout=_floating("ARXIV_REQUEST_TIMEOUT", 30.0, minimum=0.0),
            cache_ttl=_integer("ARXIV_CACHE_TTL", 300, minimum=0),
            max_concurrent=_integer("ARXIV_MAX_CONCURRENT", 5),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        if self.log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
        if self.max_results < 1 or self.max_results > 100:
            raise ValueError("ARXIV_MAX_RESULTS must be between 1 and 100")
