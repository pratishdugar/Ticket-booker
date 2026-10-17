"""Configuration loading (YAML with ${ENV_VAR} interpolation)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from .matching import Preferences
from .models import Category, Contact, Passenger

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

CONFIG_SEARCH_PATHS = [
    Path("ticketbooker.yaml"),
    Path("ticketbooker.yml"),
    Path.home() / ".config" / "ticketbooker" / "config.yaml",
]


class AgentSettings(BaseModel):
    model: str = "claude-opus-5-5"
    effort: str = "medium"  # low | medium | high | xhigh | max
    max_tokens: int = 16000
    max_steps: int = 25  # tool-call rounds per user message
    require_confirmation: bool = True  # ask the human before every booking
    server_side_fallback: bool = True  # retry safety-declined requests on a fallback model


class Traveller(Passenger):
    aliases: list[str] = Field(default_factory=list)


class AppConfig(BaseModel):
    agent: AgentSettings = Field(default_factory=AgentSettings)
    travellers: list[Traveller] = Field(default_factory=list)
    contact: Contact = Field(default_factory=Contact)
    preferences: Preferences = Field(default_factory=Preferences)
    category_preferences: dict[Category, dict[str, Any]] = Field(default_factory=dict)
    providers: list[dict[str, Any]] = Field(default_factory=lambda: [{"name": "demo", "type": "mock"}])
    storage_path: str = "~/.ticketbooker/bookings.json"

    def preferences_for(self, category: Category, overrides: dict[str, Any] | None = None) -> Preferences:
        return self.preferences.merged(self.category_preferences.get(category)).merged(overrides)

    def find_travellers(self, names: list[str]) -> list[Traveller]:
        found: list[Traveller] = []
        for wanted in names:
            key = wanted.strip().lower()
            match = next(
                (t for t in self.travellers if t.name.lower() == key or key in (a.lower() for a in t.aliases)),
                None,
            )
            if match is None:
                known = ", ".join(t.name for t in self.travellers) or "none configured"
                raise KeyError(f"Unknown traveller '{wanted}'. Known travellers: {known}")
            found.append(match)
        return found


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""), value)
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def find_config(explicit: str | None = None) -> Path | None:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("TICKETBOOKER_CONFIG")
    if env:
        return Path(env).expanduser()
    return next((p for p in CONFIG_SEARCH_PATHS if p.exists()), None)


def load_config(path: str | Path | None = None) -> AppConfig:
    resolved = find_config(str(path) if path else None)
    if resolved is None:
        return AppConfig()
    if not resolved.exists():
        raise FileNotFoundError(f"Config file not found: {resolved}")
    raw = yaml.safe_load(resolved.read_text()) or {}
    data = _interpolate(raw)
    data["providers"] = [p for p in data.get("providers", []) if p.pop("enabled", True)] or AppConfig().providers
    return AppConfig.model_validate(data)
