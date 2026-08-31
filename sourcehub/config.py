"""Settings (env) + crawl configuration (config.yaml).

Env holds secrets and machine-specific paths; config.yaml holds tunables you will
actually want to edit while the thing is running.
"""

from __future__ import annotations

import functools
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    """Where the editable YAML files live.

    In a container the code is baked into the image but config.yaml, providers.yaml,
    duty.yaml and freight.yaml must survive upgrades and be editable from the host,
    so they move to a mounted volume. SOURCEHUB_CONFIG_DIR points there; without it
    everything stays beside the source, which is what a local checkout wants.
    """
    raw = os.environ.get("SOURCEHUB_CONFIG_DIR", "").strip()
    return Path(raw) if raw else ROOT


def config_path(filename: str) -> Path:
    """Resolve one config file, preferring the mounted directory."""
    candidate = config_dir() / filename
    return candidate if candidate.exists() else ROOT / filename


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore"
    )

    # storage
    sourcehub_db_url: str = f"sqlite:///{(ROOT / 'data' / 'sourcehub.db').as_posix()}"
    sourcehub_media_dir: str = str(ROOT / "data" / "media")

    # translation
    translate_provider: str = "none"
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5"
    deepl_api_key: str = ""

    # networking
    sourcehub_proxy: str = ""
    sourcehub_user_agent: str = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )

    # official APIs
    aliexpress_app_key: str = ""
    aliexpress_app_secret: str = ""
    aliexpress_tracking_id: str = ""
    alibaba_app_key: str = ""
    alibaba_app_secret: str = ""
    ebay_client_id: str = ""
    ebay_client_secret: str = ""
    octopart_client_id: str = ""
    octopart_client_secret: str = ""
    cn_provider_preset: str = "otapi"   # a key from providers.yaml
    cn_provider_base_url: str = ""      # overrides the preset's base_url
    cn_provider_key: str = ""

    # browser
    sourcehub_browser_profile: str = str(ROOT / "data" / "browser_profile")
    sourcehub_headless: bool = True

    # agent affiliate ids
    agent_ref_superbuy: str = ""
    agent_ref_wegobuy: str = ""
    agent_ref_cssbuy: str = ""
    agent_ref_sugargoo: str = ""
    agent_ref_hagobuy: str = ""

    @property
    def media_path(self) -> Path:
        p = Path(self.sourcehub_media_dir)
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def browser_profile_path(self) -> Path:
        p = Path(self.sourcehub_browser_profile)
        if not p.is_absolute():
            p = ROOT / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_url(self) -> str:
        url = self.sourcehub_db_url
        if url.startswith("sqlite:///./"):
            url = "sqlite:///" + (ROOT / url[12:]).as_posix()
        if url.startswith("sqlite:"):
            # make sure the parent dir exists or SQLAlchemy blows up on first connect
            tail = url.split("sqlite:///", 1)[-1]
            Path(tail).parent.mkdir(parents=True, exist_ok=True)
        return url

    def agent_ref(self, agent_key: str) -> str:
        return getattr(self, f"agent_ref_{agent_key}", "") or ""


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


class CrawlConfig:
    """Thin dict wrapper over config.yaml with per-site fallback to defaults."""

    def __init__(self, data: dict[str, Any]):
        self._d = data

    @property
    def keywords(self) -> list[str]:
        return list(self._d.get("crawl", {}).get("keywords", []))

    @property
    def matching(self) -> dict[str, Any]:
        return self._d.get("matching", {})

    @property
    def translation(self) -> dict[str, Any]:
        return self._d.get("translation", {})

    @property
    def schedule(self) -> dict[str, str]:
        return self._d.get("schedule", {})

    def crawl(self, key: str, default: Any = None) -> Any:
        return self._d.get("crawl", {}).get(key, default)

    def site(self, site_key: str) -> dict[str, Any]:
        crawl = self._d.get("crawl", {})
        base = {
            "enabled": True,
            "delay_seconds": crawl.get("default_delay_seconds", 2.5),
            "concurrency": crawl.get("default_concurrency", 2),
            "max_pages_per_keyword": crawl.get("max_pages_per_keyword", 5),
            "request_timeout": crawl.get("request_timeout", 45),
            "retries": crawl.get("retries", 3),
            "driver": "http",
            "needs_agent": False,
            # How stale an enriched listing may get before its product page is
            # re-fetched. Was a hardcoded 7.
            "detail_refresh_days": crawl.get("default_detail_refresh_days", 7),
        }
        base.update(self._d.get("sites", {}).get(site_key, {}) or {})
        return base

    def enabled_sites(self) -> list[str]:
        return [k for k in self._d.get("sites", {}) if self.site(k)["enabled"]]


def load_crawl_config(path: str | os.PathLike[str] | None = None) -> CrawlConfig:
    """Read config.yaml fresh. Called per crawl run so edits take effect live."""
    p = Path(path) if path else config_path("config.yaml")
    if not p.exists():
        return CrawlConfig({})
    with p.open("r", encoding="utf-8") as fh:
        return CrawlConfig(yaml.safe_load(fh) or {})
