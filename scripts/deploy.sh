#!/usr/bin/env bash
# Deploys Abonix Social on the server. Run it there from anywhere; the GitHub
# Deploy workflow runs the same thing.
#
#   scripts/deploy.sh [ref]      (default: main)
#
# It touches only this Compose project (abonix-social): one container, built
# from this folder. Abonix's own containers, network and volumes are never
# stopped, rebuilt or pruned.
set -euo pipefail

PROJECT=abonix-social

# Everything runs from inside main, which bash reads whole before starting:
# the git reset below rewrites this very file.
main() {
  local ref="${1:-main}"
  local root
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$root"

  # Refuse to run anywhere but the social project's folder.
  if ! grep -q "^name: ${PROJECT}\$" docker-compose.yml 2>/dev/null; then
    echo "$root is not the ${PROJECT} folder (docker-compose.yml must say 'name: ${PROJECT}'). Stopping."
    exit 1
  fi
  if [ ! -f .env ]; then
    echo "Missing .env in $root — copy .env.example and fill it in first."
    exit 1
  fi

  if [ -z "${DEPLOY_FETCHED:-}" ]; then
    echo ">>> Fetching ${ref}..."
    git fetch origin "$ref"
    git reset --hard "origin/${ref}"  # data/ and .env are git-ignored and stay as they are
  fi
  echo ">>> Code at $(git log -1 --format='%h %s')"

  echo ">>> Building ${PROJECT}..."
  docker compose -p "$PROJECT" build --pull

  echo ">>> Starting ${PROJECT} (recreated, so .env changes apply)..."
  local since
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  docker compose -p "$PROJECT" up -d --force-recreate

  echo ">>> Waiting for the agent to start..."
  for _ in $(seq 1 45); do
    if docker compose -p "$PROJECT" logs --since "$since" 2>/dev/null | grep -q "abonix-social: started:"; then
      sleep 3  # give the Telegram bot a moment to connect
      if [ "$(docker inspect -f '{{.State.Running}}' "$PROJECT" 2>/dev/null)" = "true" ]; then
        docker compose -p "$PROJECT" logs --since "$since" | grep -E "started:|telegram bot polling|not configured" || true
        echo ">>> Deployed."
        return 0
      fi
    fi
    sleep 2
  done

  echo "!!! ${PROJECT} did not start. Last log lines:"
  docker compose -p "$PROJECT" ps
  docker compose -p "$PROJECT" logs --tail=80
  exit 1
}

main "$@"
