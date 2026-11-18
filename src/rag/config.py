"""Load configs/config.yaml and resolve paths relative to the project root."""

import os
from functools import lru_cache
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "configs" / "config.yaml"
PROMPTS_DIR = PROJECT_ROOT / "configs" / "prompts"


@lru_cache
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    # Environment overrides, e.g. CI builds a separate index: RAG_CHROMA_DIR=data/chroma_ci
    for key in cfg["paths"]:
        cfg["paths"][key] = os.environ.get(f"RAG_{key.upper()}", cfg["paths"][key])
    # Turn relative paths into absolute ones so commands work from any folder.
    for key, value in cfg["paths"].items():
        cfg["paths"][key] = PROJECT_ROOT / value
    return cfg
