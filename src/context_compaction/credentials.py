"""Load API keys into the environment, never returning or printing their values

Order: an existing environment variable wins, else a one-line file in
CC_CREDENTIALS_DIR. Machine-specific settings such as CC_CREDENTIALS_DIR can
live in a gitignored .env at the repo root (see .env.example)
"""
import os
from pathlib import Path

from . import pins

KEYS = {"OPENAI_API_KEY": "openai.txt"}
DOTENV = pins.ROOT / ".env"


def load_dotenv(env=os.environ, path=None):
    """Fill unset variables from KEY=VALUE lines; existing values always win"""
    path = Path(path or DOTENV)
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip().removeprefix("export ").strip(), value.strip().strip("'\"")
        if key and key not in env:
            env[key] = os.path.expanduser(value)


def credentials_dir(env=os.environ):
    value = env.get("CC_CREDENTIALS_DIR")
    return Path(value) if value else None


def load(env=os.environ, dotenv=None):
    """Fill missing keys from files, return {name: 'env' | 'file' | None}

    dotenv defaults to the repo's .env; pass False to skip it
    """
    if dotenv is not False:
        load_dotenv(env, dotenv or DOTENV)
    directory = credentials_dir(env)
    sources = {}
    for name, filename in KEYS.items():
        if env.get(name):
            sources[name] = "env"
            continue
        path = directory / filename if directory else None
        value = path.read_text().strip() if path and path.is_file() else ""
        if value:
            env[name] = value
            sources[name] = "file"
        else:
            sources[name] = None
    return sources
