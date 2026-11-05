#!/usr/bin/env bash
# Manage the x86 VM from the Mac: create, push code and key, run jobs, pull results
# Usage: scripts/cloud/gcp.sh <create|setup|push|run|logs|pull|ssh|stop|start|delete> [args]
# Settings come from the environment or the repo's gitignored .env (see .env.example)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
if [ -f "$ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$ROOT/.env"
  set +a
fi

# a dedicated project keeps the experiment's billing separate and cleanup to one delete
PROJECT=${CC_PROJECT:?set CC_PROJECT to your GCP project id, e.g. in .env}
ZONE=${CC_ZONE:-us-central1-b}
VM=${CC_VM:-cc-x86}
MACHINE=${CC_MACHINE:-n2-standard-16}
DISK_GB=${CC_DISK_GB:-500}
KEY_FILE=${CC_KEY_FILE:-${CC_CREDENTIALS_DIR:+$CC_CREDENTIALS_DIR/openai.txt}}

HOST="$VM.$ZONE.$PROJECT"
REMOTE=context-compaction
# where the VM keeps its copy of the key, outside the repo
VM_KEY_DIR='$HOME/.config/context-compaction'
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
    if [ -z "$KEY_FILE" ] || [ ! -f "$KEY_FILE" ]; then
      echo "no API key file: set CC_KEY_FILE or CC_CREDENTIALS_DIR (see .env.example)" >&2
      exit 1
    fi
    # local-only files (.env, CLAUDE.md) stay on this machine
    rsync -az --delete \
      --exclude .venv/ --exclude data/ --exclude results/ --exclude logs/ \
      --exclude '__pycache__/' --exclude '*.pdf' --exclude .DS_Store \
      --exclude .env --exclude CLAUDE.md \
      "$ROOT/" "$HOST:$REMOTE/"
    # the key travels via stdin, so it never appears in argv, and lands readable only by the VM user;
    # the VM's own .env points the code at it
    ssh "$HOST" "umask 077 && mkdir -p $VM_KEY_DIR && cat > $VM_KEY_DIR/openai.txt && chmod 600 $VM_KEY_DIR/openai.txt \
      && echo CC_CREDENTIALS_DIR=$VM_KEY_DIR > $REMOTE/.env" < "$KEY_FILE"
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
