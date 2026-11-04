"""Load API keys into the environment, never returning or printing their values

Order: an existing environment variable wins, else a one-line file under
CC_CREDENTIALS_DIR (default ~/.config/context-compaction)
"""
import os
from pathlib import Path

DEFAULT_DIR = Path.home() / ".config" / "context-compaction"
KEYS = {"OPENAI_API_KEY": "openai.txt"}


def credentials_dir(env=os.environ):
    return Path(env.get("CC_CREDENTIALS_DIR", DEFAULT_DIR))


def load(env=os.environ):
    """Fill missing keys from files, return {name: 'env' | 'file' | None}"""
    sources = {}
    for name, filename in KEYS.items():
        if env.get(name):
            sources[name] = "env"
            continue
        path = credentials_dir(env) / filename
        value = path.read_text().strip() if path.is_file() else ""
        if value:
            env[name] = value
            sources[name] = "file"
        else:
            sources[name] = None
    return sources
