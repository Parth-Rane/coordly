import os
import math
from dataclasses import dataclass
from pathlib import Path

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError
from dotenv import load_dotenv


load_dotenv()


def _setting(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    try:
        secret = st.secrets.get(name)
    except StreamlitSecretNotFoundError:
        return default
    return str(secret) if secret else default


@dataclass(frozen=True)
class Settings:
    database_backend: str
    database_path: str
    supabase_db_url: str | None
    supabase_url: str | None
    supabase_anon_key: str | None
    telegram_bot_token: str | None
    llm_provider: str
    llm_api_url: str | None
    llm_api_key: str | None
    llm_model: str | None
    llm_timeout_seconds: float
    follow_up_cooldown_hours: float
    reminder_interval_minutes: float

    @property
    def follow_up_cooldown_minutes(self) -> float:
        return self.follow_up_cooldown_hours * 60.0


def _positive_float_value(name: str, value_str: str, *, allow_zero: bool = False) -> float:
    try:
        parsed = float(value_str)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0 or (parsed == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be {qualifier}")
    return parsed


def _positive_float(name: str, default: float, *, allow_zero: bool = False) -> float:
    value = _setting(name, str(default))
    return _positive_float_value(name, value, allow_zero=allow_zero)


def _parse_cooldown_hours() -> float:
    # 1. FOLLOW_UP_COOLDOWN (e.g. '5 mins', '5m', '5', '1 hr', '0')
    cooldown_val = _setting("FOLLOW_UP_COOLDOWN")
    if cooldown_val is not None and cooldown_val.strip():
        val = cooldown_val.strip().lower()
        for suffix in ("minutes", "minute", "mins", "min", "m"):
            if val.endswith(suffix):
                return _positive_float_value(
                    "FOLLOW_UP_COOLDOWN",
                    val[:-len(suffix)].strip(),
                    allow_zero=True,
                ) / 60.0
        for suffix in ("hours", "hour", "hrs", "hr", "h"):
            if val.endswith(suffix):
                return _positive_float_value(
                    "FOLLOW_UP_COOLDOWN",
                    val[:-len(suffix)].strip(),
                    allow_zero=True,
                )
        num = _positive_float_value("FOLLOW_UP_COOLDOWN", val, allow_zero=True)
        return num / 60.0 if num >= 1 else num

    # 2. FOLLOW_UP_COOLDOWN_MINUTES (e.g. 5 or 0)
    cooldown_minutes = _setting("FOLLOW_UP_COOLDOWN_MINUTES")
    if cooldown_minutes and cooldown_minutes.strip():
        return _positive_float("FOLLOW_UP_COOLDOWN_MINUTES", 5, allow_zero=True) / 60.0

    # 3. FOLLOW_UP_COOLDOWN_HOURS (e.g. 24, 0.0833, or 0)
    cooldown_hours = _setting("FOLLOW_UP_COOLDOWN_HOURS")
    if cooldown_hours and cooldown_hours.strip():
        return _positive_float("FOLLOW_UP_COOLDOWN_HOURS", 24, allow_zero=True)

    # 4. Default: 5 minutes (5 / 60 hours)
    return 5.0 / 60.0


def get_settings() -> Settings:
    return Settings(
        database_backend=(_setting("DATABASE_BACKEND", "supabase") or "supabase").strip().lower(),
        database_path=(
            _setting("DATABASE_PATH", str(Path(__file__).with_name("coordinator.db")))
            or str(Path(__file__).with_name("coordinator.db"))
        ),
        supabase_db_url=_setting("SUPABASE_DB_URL") or None,
        supabase_url=_setting("SUPABASE_URL") or None,
        supabase_anon_key=_setting("SUPABASE_ANON_KEY") or None,
        telegram_bot_token=_setting("TELEGRAM_BOT_TOKEN") or None,
        llm_provider=(_setting("LLM_PROVIDER", "disabled") or "disabled").strip().lower(),
        llm_api_url=_setting("LLM_API_URL") or None,
        llm_api_key=_setting("LLM_API_KEY") or None,
        llm_model=_setting("LLM_MODEL") or None,
        llm_timeout_seconds=_positive_float("LLM_TIMEOUT_SECONDS", 20),
        follow_up_cooldown_hours=_parse_cooldown_hours(),
        reminder_interval_minutes=_positive_float(
            "REMINDER_INTERVAL_MINUTES", 0, allow_zero=True
        ),
    )
