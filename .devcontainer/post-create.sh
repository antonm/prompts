#!/usr/bin/env bash
# Idempotent Codespaces / Dev Container setup for antonm/prompts.
set -euo pipefail

echo "[1/3] Installing lightweight CLI utilities (tmux, ripgrep, jq, fd-find, fzf)..."
sudo apt-get update -y
sudo apt-get install -y --no-install-recommends tmux ripgrep jq fd-find fzf
sudo rm -rf /var/lib/apt/lists/*

echo "[2/3] Installing Python dependencies for Anki builder..."
if [ -f "anki/requirements.txt" ]; then
  python3 -m pip install --user --no-cache-dir -r anki/requirements.txt
fi

echo "[3/3] Installing Claude Code CLI via npm..."
if ! command -v claude >/dev/null 2>&1; then
  npm install -g @anthropic-ai/claude-code || echo "WARN: Claude Code install skipped; run 'npm install -g @anthropic-ai/claude-code' manually."
fi

mkdir -p "$HOME/.local/bin"

cat <<'EOF'

Dev container ready!
  - GitHub CLI:   gh auth status
  - Claude Code:  claude
  - Antigravity:  See https://antigravity.google/docs/cli/reference to install `agy`
  - Anki check:   python3 anki/build_apkg.py --check
  - Anki build:   python3 anki/build_apkg.py
EOF
