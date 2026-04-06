from __future__ import annotations

import configparser
import errno
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .fs import NOFOLLOW_OPEN_FLAG

DEFAULT_BASE_URL = "https://grugnotes.com"
CONFIG_PATH = Path.home() / ".grugnotes"
LOCALHOST_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


class ConfigError(ValueError):
    pass


@dataclass
class CLIConfig:
    base_url: str
    api_key: str | None
    path: Path
    allow_insecure_http: bool = False
    stored_base_url: str | None = None
    ignored_stored_api_key_reason: str | None = None
    api_key_source: str | None = None  # "override", "env", "config", or None


def config_path() -> Path:
    return CONFIG_PATH


def _ensure_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _env_truthy(name: str) -> bool:
    value = (os.getenv(name) or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _validate_config_path(path: Path) -> None:
    if path.is_symlink():
        raise ConfigError("Config file cannot be a symlink.")
    if path.exists() and not path.is_file():
        raise ConfigError("Config path must be a regular file.")


def validate_base_url(base_url: str, *, allow_insecure_http: bool = False) -> str:
    normalized = (base_url or "").strip().rstrip("/")
    if not normalized:
        raise ConfigError("Base URL is required.")

    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        raise ConfigError("Base URL must start with http:// or https://.")
    if not parsed.netloc:
        raise ConfigError("Base URL must include a host.")

    hostname = (parsed.hostname or "").strip().lower()
    if parsed.scheme == "http" and hostname not in LOCALHOST_HOSTNAMES and not allow_insecure_http:
        raise ConfigError(
            "Refusing insecure HTTP for non-localhost base URLs. "
            "Use HTTPS, or pass `--allow-insecure-http` / set `GRUGNOTES_ALLOW_INSECURE_HTTP=1`."
        )
    return normalized


def load_config(
    *,
    base_url_override: str | None = None,
    api_key_override: str | None = None,
    allow_insecure_http_override: bool | None = None,
) -> CLIConfig:
    parser = configparser.ConfigParser()

    path = config_path()
    if path.exists() or path.is_symlink():
        _validate_config_path(path)
        parser.read(path)
        _ensure_file_permissions(path)

    section = parser["default"] if parser.has_section("default") else {}
    stored_api_key = section.get("api_key") or None
    stored_base_url = None
    if stored_api_key is not None:
        raw_stored = (section.get("base_url") or DEFAULT_BASE_URL).strip().rstrip("/")
        parsed_stored = urlparse(raw_stored)
        if parsed_stored.scheme in {"http", "https"} and parsed_stored.netloc:
            stored_base_url = raw_stored

    base_url = (
        base_url_override
        or os.getenv("GRUGNOTES_BASE_URL")
        or section.get("base_url")
        or DEFAULT_BASE_URL
    )
    env_api_key = os.getenv("GRUGNOTES_API_KEY")
    api_key_source = None
    if api_key_override:
        api_key = api_key_override
        api_key_source = "override"
    elif env_api_key:
        api_key = env_api_key
        api_key_source = "env"
    else:
        api_key = stored_api_key
        api_key_source = "config" if stored_api_key else None
    allow_insecure_http = (
        allow_insecure_http_override
        if allow_insecure_http_override is not None
        else _env_truthy("GRUGNOTES_ALLOW_INSECURE_HTTP")
    )
    validated_base_url = validate_base_url(
        base_url,
        allow_insecure_http=allow_insecure_http,
    )
    ignored_stored_api_key_reason = None
    if (
        api_key_source == "config"
        and stored_base_url is not None
        and stored_base_url != validated_base_url
    ):
        api_key = None
        ignored_stored_api_key_reason = (
            f"Saved API key is bound to {stored_base_url}, but the effective base URL is "
            f"{validated_base_url}. Supply --api-key or GRUGNOTES_API_KEY for this host, "
            "or run `grugnotes auth`."
        )

    return CLIConfig(
        base_url=validated_base_url,
        api_key=api_key,
        path=path,
        allow_insecure_http=allow_insecure_http,
        stored_base_url=stored_base_url,
        ignored_stored_api_key_reason=ignored_stored_api_key_reason,
        api_key_source=api_key_source,
    )


def save_config(*, api_key: str, base_url: str, allow_insecure_http: bool = False) -> Path:
    path = config_path()
    parser = configparser.ConfigParser()
    _validate_config_path(path)
    validated_base_url = validate_base_url(
        base_url,
        allow_insecure_http=allow_insecure_http,
    )
    parser["default"] = {
        "api_key": api_key.strip(),
        "base_url": validated_base_url,
    }

    path.parent.mkdir(parents=True, exist_ok=True)

    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | NOFOLLOW_OPEN_FLAG
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        if NOFOLLOW_OPEN_FLAG and exc.errno == errno.ELOOP:
            raise ConfigError("Config file cannot be a symlink.") from exc
        raise
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        parser.write(handle)

    _ensure_file_permissions(path)
    return path
