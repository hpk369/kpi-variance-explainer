"""Loads config/constants.yaml and the environment (.env locally, Space secrets on Hugging Face)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]

try:  # .env is optional (the HF Space passes secrets as environment variables)
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # pragma: no cover
    pass


@lru_cache(maxsize=1)
def constants() -> dict:
    with open(ROOT / "config" / "constants.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None, required: bool = True) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing setting {name}. Add it to .env (local) or the Space secrets.")
    return value


def host() -> str:
    h = env("DATABRICKS_HOST").strip().rstrip("/")
    return h if h.startswith("http") else f"https://{h}"


def catalog() -> str:
    return constants()["databricks"]["catalog"]


def model(kind: str) -> str:
    """kind: llm | llm_fallback | draft | verifier | embedding. Environment wins over constants.yaml."""
    env_names = {"llm": "LLM_ENDPOINT", "llm_fallback": "LLM_FALLBACK_ENDPOINT", "draft": "DRAFT_ENDPOINT",
                 "verifier": "VERIFIER_ENDPOINT", "embedding": "EMBEDDING_ENDPOINT"}
    return env(env_names[kind], constants()["models"].get(kind), required=False)
