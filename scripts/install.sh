#!/usr/bin/env bash
# AgentFeed installer for macOS and Linux.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
DOMAIN="${1:-generic}"

say() { printf "\n\033[1m%s\033[0m\n" "$*"; }

say "1/4  Python environment"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv venv --python 3.13 >/dev/null
uv pip install -q -e .

say "2/4  Local model runtime"
if command -v ollama >/dev/null; then
  echo "  ollama found: $(ollama --version 2>&1 | head -1)"
else
  case "$(uname -s)" in
    Darwin) echo "  installing ollama via brew…"
            command -v brew >/dev/null && brew install ollama \
              || echo "  ! install Homebrew, or get Ollama from https://ollama.com/download" ;;
    Linux)  echo "  installing ollama…"; curl -fsSL https://ollama.com/install.sh | sh ;;
    *)      echo "  ! install Ollama from https://ollama.com/download" ;;
  esac
fi

say "3/4  Models"
if command -v ollama >/dev/null; then
  # A small context is the single most common cause of confusing failures:
  # the runtime truncates the article and the model answers about a fragment.
  export OLLAMA_CONTEXT_LENGTH="${OLLAMA_CONTEXT_LENGTH:-32768}"
  pgrep -x ollama >/dev/null || { nohup ollama serve >/tmp/ollama.log 2>&1 & sleep 3; }
  ollama pull "${AGENTFEED_MODEL:-qwen3:8b}"
  ollama pull nomic-embed-text
fi

say "4/4  Database and domain pack"
.venv/bin/agentfeed init --domain "$DOMAIN"

cat <<'NEXT'

Ready.

  .venv/bin/agentfeed add techcrunch     add a source by name
  .venv/bin/agentfeed update             fetch and file
  .venv/bin/agentfeed serve              dashboard + agent protocol

  Dashboard  http://127.0.0.1:8770/
  Discovery  http://127.0.0.1:8770/.well-known/agent-feed

If a model server is running on a non-standard port, set AGENTFEED_LLM_BASE_URL.
NEXT
