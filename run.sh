#!/usr/bin/env bash
# Run OCR + Translate locally, without Docker.
#
#   ./run.sh            build the SPA and serve everything on :8000 (one URL)
#   ./run.sh dev        backend :8000 (reload) + Vite dev server :5173
#   ./run.sh api        backend only (:8000)
#   ./run.sh --help
#
# Env (optional): HOST, PORT, STORAGE_DIR, DS_KEY, DEFAULT_MODEL, API_BASE.
# Reads .env from the repo root if present.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
MODE="${1:-serve}"
[[ "${1:-}" == "--help" || "${1:-}" == "-h" ]] && MODE=help

c()  { printf '\033[1;36m%s\033[0m\n' "$*"; }   # cyan
ok() { printf '\033[1;32m%s\033[0m\n' "$*"; }    # green
warn(){ printf '\033[1;33m%s\033[0m\n' "$*" >&2; }

usage() {
  sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}
[[ "$MODE" == help ]] && usage

# ---- load .env ----------------------------------------------------------
if [[ -f .env ]]; then
  c "Loading .env"
  set -a; # shellcheck disable=SC1091
  . ./.env; set +a
fi
export STORAGE_DIR="${STORAGE_DIR:-$ROOT/data}"

# ---- sanity checks ------------------------------------------------------
need() { command -v "$1" >/dev/null 2>&1 || { warn "missing required tool: $1"; exit 1; }; }
need python3
python3 - <<'PY' || { warn "python deps missing: pip install pymupdf requests pillow"; exit 1; }
import importlib, sys
for m in ("fitz", "requests", "PIL"):
    importlib.import_module(m)
PY
if ! command -v xelatex >/dev/null 2>&1; then
  warn "xelatex not found — typesetting (build step) will fail. Install TeX Live + fontspec."
fi

# ---- local dependency override (FastAPI/Starlette clash) ----------------
# Some systems ship an incompatible fastapi/starlette pair. We detect it by
# trying to import the app, and if it fails install a matching pair into
# backend/.deps (git-ignored) and use it via PYTHONPATH.
PYPATH="."
if ! ( cd backend && python3 -c "import app.main" >/dev/null 2>&1 ); then
  if [[ ! -d backend/.deps ]]; then
    warn "Backend import failed — installing a matching FastAPI/Starlette into backend/.deps"
    python3 -m pip install --target backend/.deps --no-deps -q 'fastapi==0.115.0' 'starlette==0.41.3' \
      || { warn "pip install failed; fix your environment manually (see AGENTS.md)"; exit 1; }
  fi
  PYPATH=".deps:."
  ( cd backend && PYTHONPATH="$PYPATH" python3 -c "import app.main" >/dev/null 2>&1 ) \
    || { warn "backend still fails to import"; exit 1; }
fi
ok "backend imports OK (PYTHONPATH=$PYPATH from backend/)"

PYBIN() { ( cd backend && PYTHONPATH="$PYPATH" python3 "$@" ); }

# ---- frontend helpers ---------------------------------------------------
have_node() { command -v npm >/dev/null 2>&1; }
ensure_node_modules() {
  have_node || { warn "npm not found: install Node.js to build/serve the SPA"; exit 1; }
  [[ -d frontend/node_modules ]] || { c "npm install"; ( cd frontend && npm install --no-audit --no-fund ); }
}
build_frontend() {
  ensure_node_modules
  c "Building frontend"; ( cd frontend && npm run build )
}

# ---- run modes ----------------------------------------------------------
run_api() { # $1 = extra uvicorn args
  c "API  -> http://$HOST:$PORT   (docs at /docs, SPA at / if built)"
  echo "     STORAGE_DIR=$STORAGE_DIR${DS_KEY:+  (server key set)}${DS_KEY:-  (BYOK: paste a key in the UI)}"
  ( cd backend && PYTHONPATH="$PYPATH" exec python3 -m uvicorn app.main:app \
      --host "$HOST" --port "$PORT" ${1:-} )
}

case "$MODE" in
  api)
    run_api "--reload"
    ;;

  serve)
    [[ -d frontend/dist ]] || build_frontend
    run_api ""
    ;;

  dev)
    ensure_node_modules
    VITE_PORT=5173
    c "Starting Vite dev server on :$VITE_PORT (proxies /api -> :$PORT)"
    ( cd frontend && VITE_API_TARGET="http://$HOST:$PORT" npm run dev -- --host "$HOST" --port "$VITE_PORT" ) &
    VITE_PID=$!
    trap 'kill "$VITE_PID" 2>/dev/null || true' EXIT INT TERM
    c "Open http://$HOST:$VITE_PORT"
    run_api "--reload"
    ;;

  *)
    warn "unknown mode: $MODE"; usage
    ;;
esac
