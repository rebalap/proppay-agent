"""Runtime settings (.env) and policy config (config/*.yaml)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, time
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str
    ollama_model: str
    embed_model: str
    db_path: Path
    mock_bank_url: str
    max_agent_steps: int
    llm_num_ctx: int
    llm_temperature: float
    demo_run_date: date  # "today" for Monday's AP run; fixed so the demo is reproducible
    demo_run_time: time  # local bank time the run starts (matters for rail cutoffs)


@lru_cache
def get_settings() -> Settings:
    db_path = Path(os.getenv("DB_PATH", "data/proppay.db"))
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    return Settings(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen3:8b"),
        embed_model=os.getenv("EMBED_MODEL", "nomic-embed-text"),
        db_path=db_path,
        mock_bank_url=os.getenv("MOCK_BANK_URL", "http://localhost:8001"),
        max_agent_steps=int(os.getenv("MAX_AGENT_STEPS", "5")),
        llm_num_ctx=int(os.getenv("LLM_NUM_CTX", "8192")),
        llm_temperature=float(os.getenv("LLM_TEMPERATURE", "0")),
        demo_run_date=date.fromisoformat(os.getenv("DEMO_RUN_DATE", "2026-10-19")),
        demo_run_time=time.fromisoformat(os.getenv("DEMO_RUN_TIME", "09:00")),
    )


@lru_cache
def load_config(name: str) -> dict[str, Any]:
    """Load config/<name>.yaml. Cached; treat the result as read-only."""
    path = CONFIG_DIR / f"{name}.yaml"
    with path.open() as f:
        return yaml.safe_load(f)


def rails_config() -> dict[str, Any]:
    return load_config("rails")


def policy_config() -> dict[str, Any]:
    return load_config("policy")


def fraud_config() -> dict[str, Any]:
    return load_config("fraud")
