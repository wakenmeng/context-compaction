#!/usr/bin/env bash
# Runs on the VM (Ubuntu 24.04 x86_64): Docker, uv, locked env, dataset snapshot
# Safe to re-run
set -euo pipefail

UV_VERSION=0.11.21

if [ "$(uname -m)" != "x86_64" ]; then
  echo "expected x86_64, got $(uname -m)" >&2
  exit 1
fi

if ! command -v docker >/dev/null; then
  sudo apt-get update -q
  sudo apt-get install -yq docker.io tmux rsync
fi
# Docker Hub pull-through cache on GCP, falls back to Docker Hub on a miss
if [ ! -f /etc/docker/daemon.json ]; then
  echo '{"registry-mirrors": ["https://mirror.gcr.io"]}' | sudo tee /etc/docker/daemon.json >/dev/null
  sudo systemctl restart docker
fi
sudo usermod -aG docker "$USER"

if ! command -v uv >/dev/null && [ ! -x "$HOME/.local/bin/uv" ]; then
  curl -LsSf "https://astral.sh/uv/$UV_VERSION/install.sh" | sh
fi
export PATH="$HOME/.local/bin:$PATH"

cd "$HOME/context-compaction"
uv sync --frozen --extra dev
.venv/bin/pytest -q
.venv/bin/python scripts/fetch_dataset.py
echo "setup done; docker group applies to new logins"
