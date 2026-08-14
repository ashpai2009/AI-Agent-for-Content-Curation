#!/usr/bin/env bash
#
# Run the whole thing on this machine: the council service and the web page.
#
#   ./scripts/serve.sh              both, then open http://localhost:3000
#   ./scripts/serve.sh --api-only   just the council service on :8000
#   ./scripts/serve.sh --no-open    both, without opening a browser
#
# **Local is not a limitation here, it is the architecture.** The council shells out to
# the Claude Code CLI, which authenticates against your Claude subscription through this
# machine's keychain. That login cannot be moved to a server without turning the
# subscription into metered API billing, so the compute stays where the login is -- and
# once the compute is here, there is nothing left for a remote host to do.
#
# The page still talks to the service through its own server-side route handlers rather
# than from the browser. On localhost that is not about secrecy so much as keeping one
# shape: the token stays out of the JavaScript bundle, and there is no CORS to configure.
#
# Ctrl-C stops everything it started.

set -euo pipefail

cd "$(dirname "$0")/.."

API_PORT="${API_PORT:-8000}"
WEB_PORT="${WEB_PORT:-3000}"
WITH_WEB=1
OPEN_BROWSER=1

for arg in "$@"; do
  case "$arg" in
    --api-only) WITH_WEB=0 ;;
    --no-open)  OPEN_BROWSER=0 ;;
    -h|--help)  sed -n '3,8p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

LOG_DIR="$(mktemp -d -t council-serve)"
API_LOG="$LOG_DIR/service.log"
WEB_LOG="$LOG_DIR/web.log"
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do
    [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
  done
  wait 2>/dev/null || true
  echo ""
  echo "stopped. logs: $LOG_DIR"
}
trap cleanup EXIT INT TERM

wait_for() {
  # $1 url, $2 pid, $3 what it is
  for _ in $(seq 1 120); do
    curl -sf "$1" >/dev/null 2>&1 && return 0
    if ! kill -0 "$2" 2>/dev/null; then
      echo "the $3 exited before it came up:" >&2
      tail -25 "$4" >&2
      return 1
    fi
    sleep 0.5
  done
  echo "the $3 did not come up within 60s:" >&2
  tail -25 "$4" >&2
  return 1
}

# -- the council service ---------------------------------------------------------------

if [ ! -x .venv/bin/uvicorn ]; then
  echo "error: .venv/bin/uvicorn is missing. Run:" >&2
  echo "  uv pip install --python .venv -e '.[dev]'" >&2
  exit 1
fi

echo "starting the council service on :$API_PORT"
# One worker, deliberately: SQLite in WAL over a single file wants a single writer, and
# the working copies are on a local filesystem.
.venv/bin/uvicorn "oatutor_council.api:create_app" \
  --factory --app-dir src --port "$API_PORT" --workers 1 \
  >"$API_LOG" 2>&1 &
API_PID=$!
PIDS+=("$API_PID")

# `create_app` checks the CLI login at startup and refuses to boot without it, rather than
# accepting a workbook it could never process. So a service that never comes up has almost
# always said why in its log.
wait_for "http://127.0.0.1:$API_PORT/health" "$API_PID" "council service" "$API_LOG" || exit 1

if ! grep -qE '^API_TOKEN=..*' .env 2>/dev/null; then
  echo "note: API_TOKEN is unset, so every route is open. Fine while this is on localhost."
fi

echo "  service ready — http://127.0.0.1:$API_PORT/readyz"

if [ "$WITH_WEB" = "0" ]; then
  echo ""
  echo "running. Ctrl-C to stop."
  wait
fi

# -- the web page ----------------------------------------------------------------------

if ! command -v npm >/dev/null 2>&1; then
  echo "error: npm is not installed, so the page cannot be served. Install Node 20+," >&2
  echo "       or use --api-only and talk to the service with curl." >&2
  exit 1
fi

if [ ! -d web/node_modules ]; then
  echo "installing the page's dependencies (first run only)"
  (cd web && npm install --no-audit --no-fund >"$WEB_LOG" 2>&1)
fi

# Rewritten whenever it disagrees with `.env`, not only when it is missing. The page holds
# a *copy* of the service's token, so rotating `API_TOKEN` leaves the copy stale -- and the
# symptom is a 401 on every call from a page that looks correctly configured, which is a
# bad afternoon. Compared rather than trusted; neither value is ever printed.
WANT_TOKEN="$(grep '^API_TOKEN=' .env 2>/dev/null | cut -d= -f2- || true)"
HAVE_TOKEN="$(grep '^COUNCIL_API_TOKEN=' web/.env.local 2>/dev/null | cut -d= -f2- || true)"
WANT_URL="http://127.0.0.1:$API_PORT"
HAVE_URL="$(grep '^COUNCIL_API_URL=' web/.env.local 2>/dev/null | cut -d= -f2- || true)"

if [ ! -f web/.env.local ] || [ "$WANT_TOKEN" != "$HAVE_TOKEN" ] || [ "$WANT_URL" != "$HAVE_URL" ]; then
  [ -f web/.env.local ] && echo "web/.env.local disagreed with .env — rewriting it" \
                        || echo "writing web/.env.local"
  {
    echo "COUNCIL_API_URL=$WANT_URL"
    echo "COUNCIL_API_TOKEN=$WANT_TOKEN"
  } > web/.env.local
fi

# Built rather than run in dev mode: this is a tool being used, not a page being edited,
# and `next dev` recompiles on every request for no benefit here.
if [ ! -d web/.next ] || [ -n "$(find web/app web/lib -newer web/.next -type f 2>/dev/null | head -1)" ]; then
  echo "building the page"
  (cd web && npm run build >"$WEB_LOG" 2>&1) || { tail -25 "$WEB_LOG" >&2; exit 1; }
fi

echo "starting the page on :$WEB_PORT"
(cd web && npx next start -p "$WEB_PORT" >"$WEB_LOG" 2>&1) &
WEB_PID=$!
PIDS+=("$WEB_PID")

wait_for "http://127.0.0.1:$WEB_PORT/api/health" "$WEB_PID" "web page" "$WEB_LOG" || exit 1

READY="$(curl -s "http://127.0.0.1:$WEB_PORT/api/health" || true)"
echo ""
echo "  →  http://localhost:$WEB_PORT"
echo ""
case "$READY" in
  *'"ready":true'*) echo "  the page can reach the council and a job would run." ;;
  *) echo "  warning: the page reached its own API but the council is not ready:"
     echo "  $READY" ;;
esac

if [ "$OPEN_BROWSER" = "1" ] && command -v open >/dev/null 2>&1; then
  open "http://localhost:$WEB_PORT" >/dev/null 2>&1 || true
fi

echo ""
echo "running. Ctrl-C to stop both."
wait
