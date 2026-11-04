#!/usr/bin/env bash
# Manage the x86 VM from the Mac: create, push code and key, run jobs, pull results
# Usage: scripts/cloud/gcp.sh <create|setup|push|run|logs|pull|ssh|stop|start|delete> [args]
set -euo pipefail

# dedicated experiment project, independent of gcloud's default; override with CC_PROJECT
PROJECT=${CC_PROJECT:-your-gcp-project-id}
ZONE=${CC_ZONE:-us-central1-b}
VM=${CC_VM:-cc-x86}
MACHINE=${CC_MACHINE:-n2-standard-16}
DISK_GB=${CC_DISK_GB:-500}
KEY_FILE=${CC_KEY_FILE:-$HOME/.config/context-compaction/openai.txt}

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HOST="$VM.$ZONE.$PROJECT"
REMOTE=context-compaction
G=(gcloud --project "$PROJECT")

ssh_config() {
  # writes Host entries named <vm>.<zone>.<project> into ~/.ssh/config
  "${G[@]}" compute config-ssh --quiet >/dev/null
}

cmd=${1:-}
shift || true
case "$cmd" in
  create)
    "${G[@]}" compute instances create "$VM" --zone "$ZONE" \
      --machine-type "$MACHINE" \
      --image-family ubuntu-2404-lts-amd64 --image-project ubuntu-os-cloud \
      --boot-disk-size "${DISK_GB}GB" --boot-disk-type pd-balanced
    ;;
  push)
    ssh_config
    rsync -az --delete \
      --exclude .venv/ --exclude data/ --exclude results/ --exclude logs/ \
      --exclude '__pycache__/' --exclude '*.pdf' --exclude .DS_Store \
      "$ROOT/" "$HOST:$REMOTE/"
    # key goes outside the repo, readable only by the VM user
    # via stdin, so it works with macOS openrsync and never appears in argv
    ssh "$HOST" 'umask 077 && d=~/.config/context-compaction && mkdir -p $d && cat > $d/openai.txt && chmod 600 $d/openai.txt' < "$KEY_FILE"
    ;;
  setup)
    "$0" push
    ssh "$HOST" "bash $REMOTE/scripts/cloud/vm_setup.sh"
    ;;
  run)
    # detached in tmux so it survives disconnects, output in logs/<name>.log
    name=${CC_JOB:-job}
    ssh "$HOST" "mkdir -p $REMOTE/logs && tmux new -d -s $name \
      'cd $REMOTE && source .venv/bin/activate && $* 2>&1 | tee -a logs/$name.log'"
    echo "started tmux session $name, follow with: $0 logs $name"
    ;;
  logs)
    ssh "$HOST" "tail -n ${CC_LINES:-50} -f $REMOTE/logs/${1:-job}.log"
    ;;
  pull)
    ssh_config
    rsync -az "$HOST:$REMOTE/results/" "$ROOT/results/"
    ;;
  ssh)
    ssh_config
    ssh "$HOST" "$@"
    ;;
  stop | start | delete)
    "${G[@]}" compute instances "$cmd" "$VM" --zone "$ZONE"
    ;;
  *)
    sed -n 2,3p "$0"
    exit 1
    ;;
esac
