#!/usr/bin/env bash
# Start a safe local oc8 evaluation instance on macOS or Linux.
# It creates only missing local secrets and never resets containers or volumes.
#
# Interactive by default: asks which operating mode to run (Community, Demo,
# or Dev) and, for Community/Demo, whether you already run your own reverse
# proxy (binds oc8 to loopback, OC8_QUICKSTART_OWN_PROXY/_PROXY_PORT/
# _EXTERNAL_URL) or want oc8's own Caddy to handle it (an optional custom
# domain for automatic HTTPS, OC8_QUICKSTART_DOMAIN). All can be preset for
# scripted/non-interactive runs via OC8_QUICKSTART_MODE (community|demo|dev)
# and the vars above -- when stdin isn't a terminal and none are set, it
# falls back to the previous non-interactive default: Community mode, no
# proxy, no domain.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

fail() {
  printf 'Quickstart stopped: %s\n' "$1" >&2
  exit 1
}

case "$(uname -s)" in
  Darwin|Linux) ;;
  MINGW*|MSYS*|CYGWIN*)
    fail "Use PowerShell instead: .\\scripts\\quickstart.ps1"
    ;;
  *) fail "Unsupported platform. Use macOS, Linux, or scripts/quickstart.ps1 on Windows." ;;
esac

command -v openssl >/dev/null 2>&1 || fail "openssl is required to generate local secrets."

# Resolution order: an explicit env var wins, then a value already persisted
# in .env from a previous run, then auto-detect from what's on PATH.
resolve_container_runtime() {
  if [[ -n "${OC8_CONTAINER_RUNTIME:-}" ]]; then
    printf '%s\n' "$OC8_CONTAINER_RUNTIME"
    return
  fi
  if [[ -f .env ]]; then
    local from_env
    from_env="$(awk -F= '$1 == "OC8_CONTAINER_RUNTIME" { sub(/^[^=]*=/, ""); print; exit }' .env)"
    if [[ -n "$from_env" ]]; then
      printf '%s\n' "$from_env"
      return
    fi
  fi
  if command -v docker >/dev/null 2>&1; then
    printf 'docker\n'
  elif command -v podman >/dev/null 2>&1; then
    printf 'podman\n'
  else
    fail "Neither Docker nor Podman was found. Install one of them first."
  fi
}

CONTAINER_RUNTIME="$(resolve_container_runtime)"
case "$CONTAINER_RUNTIME" in
  docker|podman) ;;
  *) fail "OC8_CONTAINER_RUNTIME must be 'docker' or 'podman', got '$CONTAINER_RUNTIME'." ;;
esac

if [[ "$CONTAINER_RUNTIME" == "docker" ]]; then
  command -v docker >/dev/null 2>&1 || fail "Docker is required. Install Docker Desktop or Docker Engine first."
  docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required."
  docker info >/dev/null 2>&1 || fail "Docker is installed but its daemon is not running. Start Docker first."
  COMPOSE_CMD=(docker compose)
else
  command -v podman >/dev/null 2>&1 || fail "Podman is required (OC8_CONTAINER_RUNTIME=podman). Install it first."
  if podman compose version >/dev/null 2>&1; then
    COMPOSE_CMD=(podman compose)
  elif command -v podman-compose >/dev/null 2>&1; then
    COMPOSE_CMD=(podman-compose)
  else
    fail "Podman Compose is required: install the 'podman compose' plugin (Podman v4+) or podman-compose."
  fi
  podman info >/dev/null 2>&1 || fail "Podman is installed but not ready. Run 'podman machine start' (macOS) or 'systemctl --user enable --now podman.socket' (Linux) first."
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env 2>/dev/null || true
  printf 'Created .env from .env.example.\n'
else
  printf 'Using existing .env; non-empty values will not be changed.\n'
fi

env_value() {
  awk -F= -v key="$1" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' .env
}

# Unconditional overwrite (or append if the key is missing entirely). Only
# used below for values the user just typed at a prompt -- everything else
# in this script goes through set_env_value_if_missing so a rerun never
# clobbers what's already in .env.
set_env_value() {
  local key="$1"
  local value="$2"

  local temp_env
  temp_env="$(mktemp "${TMPDIR:-/tmp}/oc8-env.XXXXXX")"
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    $0 ~ "^" key "=" { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' .env > "$temp_env"
  mv "$temp_env" .env
  chmod 600 .env 2>/dev/null || true
}

set_env_value_if_missing() {
  local key="$1"
  local value="$2"
  local current
  current="$(env_value "$key")"
  if [[ -n "$current" ]]; then
    return
  fi
  set_env_value "$key" "$value"
  printf 'Generated %s in .env.\n' "$key"
}

is_interactive() {
  [[ -t 0 && -t 1 ]]
}

# --- Operating mode: Community (empty, real password setup), Demo (seeded
# ACME showcase behind real password login), or Dev (seeded ACME + instant
# unauthenticated login, localhost only). Each maps to a docker-compose.yml
# + override combination -- see docker-compose.demo.yml/docker-compose.dev.yml
# for exactly what each one changes.
MODE="${OC8_QUICKSTART_MODE:-}"
if [[ -z "$MODE" ]]; then
  if is_interactive; then
    printf '\nWhich operating mode do you want to run?\n'
    printf '  1) Prod      — empty instance, real password setup (recommended)\n'
    printf '  2) Demo      — seeded bilingual ACME showcase data behind a real password login\n'
    printf '  3) Dev       — seeded ACME data + instant unauthenticated login (localhost only, never expose)\n'
    read -r -p 'Choice [1]: ' mode_choice
    case "${mode_choice:-1}" in
      1|"") MODE="community" ;;
      2) MODE="demo" ;;
      3) MODE="dev" ;;
      *) fail "Invalid choice: ${mode_choice}" ;;
    esac
  else
    MODE="community"
  fi
fi
case "$MODE" in
  community|demo|dev) ;;
  *) fail "OC8_QUICKSTART_MODE must be 'community', 'demo', or 'dev', got '$MODE'." ;;
esac

COMPOSE_FILES=(-f docker-compose.yml)
case "$MODE" in
  demo)
    COMPOSE_FILES+=(-f docker-compose.demo.yml)
    printf 'Demo mode: seeded ACME showcase data behind a real password login.\n'
    ;;
  dev)
    COMPOSE_FILES+=(-f docker-compose.dev.yml)
    printf 'Dev mode: instant unauthenticated admin login. LOCALHOST ONLY -- never expose this to a network.\n'
    ;;
esac

# --- Own reverse proxy, or built-in Caddy? Skipped in Dev mode: that mode's
# login has no password, so it must never be reachable off localhost anyway.
# Always asks interactively, with no default -- a checkout reused for a
# different host/mode must not silently inherit an old answer.
if [[ "$MODE" != "dev" ]]; then
  own_proxy="${OC8_QUICKSTART_OWN_PROXY:-}"
  if [[ -z "$own_proxy" ]] && is_interactive; then
    read -r -p 'Already running your own reverse proxy on this host (nginx, Traefik, another Caddy)? [y/N] ' own_proxy
  fi
  if [[ "$own_proxy" =~ ^[Yy] ]]; then
    # Your proxy owns the domain and the TLS certificate; oc8's own Caddy
    # must not also try to grab :80/:443 or provision one. Bind it to
    # loopback on a plain port instead, and hand back what to point at.
    proxy_port="${OC8_QUICKSTART_PROXY_PORT:-}"
    if [[ -z "$proxy_port" ]] && is_interactive; then
      read -r -p 'Localhost port for your proxy to reach oc8 on (leave empty for 8080): ' proxy_port
    fi
    proxy_port="${proxy_port:-8080}"
    set_env_value "OC8_HTTP_PORT" "127.0.0.1:${proxy_port}"
    set_env_value "OC8_DOMAIN" ""
    external_url="${OC8_QUICKSTART_EXTERNAL_URL:-}"
    if [[ -z "$external_url" ]] && is_interactive; then
      read -r -p 'Externally visible URL your proxy serves this under (e.g. https://oc8.example.com): ' external_url
    fi
    if [[ -n "$external_url" ]]; then
      set_env_value "OC8_FRONTEND_BASE_URL" "$external_url"
    fi
    printf 'Point your reverse proxy at 127.0.0.1:%s (plain HTTP) -- oc8'"'"'s own Caddy will not attempt to obtain a certificate.\n' "$proxy_port"
  else
    existing_domain="$(env_value OC8_DOMAIN)"
    domain="${OC8_QUICKSTART_DOMAIN:-}"
    if [[ -z "$domain" ]] && is_interactive; then
      read -r -p 'Custom domain for automatic HTTPS (leave empty for plain HTTP): ' domain
    elif [[ -z "$domain" ]] && ! is_interactive; then
      domain="$existing_domain"
    fi
    if [[ -n "$domain" ]]; then
      if [[ "$domain" != "$existing_domain" ]]; then
        printf 'Point DNS for %s at this host before continuing, or certificate issuance will fail.\n' "$domain"
      fi
      set_env_value "OC8_DOMAIN" "$domain"
      current_base_url="$(env_value OC8_FRONTEND_BASE_URL)"
      if [[ -z "$current_base_url" || "$current_base_url" == "http://localhost" ]]; then
        set_env_value "OC8_FRONTEND_BASE_URL" "https://${domain}"
      else
        printf 'Note: OC8_FRONTEND_BASE_URL is already set to %s -- leaving it, but it should probably be https://%s.\n' "$current_base_url" "$domain"
      fi
    elif [[ -n "$existing_domain" ]]; then
      set_env_value "OC8_DOMAIN" ""
      printf 'Cleared OC8_DOMAIN -- plain HTTP.\n'
    fi
  fi
fi

# --- Demo mode's password login (compose.demo.yml requires OC8_DEMO_PASSWORD).
if [[ "$MODE" == "demo" ]]; then
  existing_demo_password="$(env_value OC8_DEMO_PASSWORD)"
  if [[ -z "$existing_demo_password" ]]; then
    demo_password="${OC8_QUICKSTART_DEMO_PASSWORD:-}"
    if [[ -z "$demo_password" ]] && is_interactive; then
      read -r -s -p 'Demo login password (min 8 chars, leave empty to auto-generate): ' demo_password
      printf '\n'
    fi
    if [[ -z "$demo_password" ]]; then
      demo_password="$(openssl rand -base64 18)"
      printf 'Generated demo password: %s\n' "$demo_password"
      printf '(save this now -- it is only shown this once)\n'
    elif [[ "${#demo_password}" -lt 8 ]]; then
      fail "Demo password must be at least 8 characters."
    fi
    set_env_value "OC8_DEMO_PASSWORD" "$demo_password"
  fi
  demo_email="$(env_value OC8_DEMO_EMAIL)"
  printf 'Demo sign-in address: %s\n' "${demo_email:-demo@oc8.ai}"
fi

set_env_value_if_missing "OC8_JWT_SECRET" "$(openssl rand -hex 32)"
set_env_value_if_missing "OC8_SECRET_KEK" "$(openssl rand -base64 32 | tr -d '\n')"
set_env_value_if_missing "POSTGRES_PASSWORD" "$(openssl rand -hex 24)"
set_env_value_if_missing "OC8_SANDBOX_PROVISIONER_TOKEN" "$(openssl rand -hex 32)"
set_env_value_if_missing "OC8_CONTAINER_RUNTIME" "$CONTAINER_RUNTIME"

if [[ "$CONTAINER_RUNTIME" == "podman" ]]; then
  podman_socket="$(podman info --format '{{.Host.RemoteSocket.Path}}' 2>/dev/null || true)"
  [[ -n "$podman_socket" ]] || fail "Could not determine the Podman API socket path (podman info --format failed)."
  set_env_value_if_missing "OC8_CONTAINER_SOCKET" "$podman_socket"
fi

printf '\nBuilding and starting oc8 (%s mode)…\n' "$MODE"
if [[ "$CONTAINER_RUNTIME" == "docker" ]]; then
  docker compose "${COMPOSE_FILES[@]}" up -d --build
else
  "${COMPOSE_CMD[@]}" "${COMPOSE_FILES[@]}" up -d --build
fi

domain_in_env="$(env_value OC8_DOMAIN)"
if [[ -n "$domain_in_env" ]]; then
  url="https://${domain_in_env}"
else
  port_mapping="$(env_value OC8_HTTP_PORT)"
  port_mapping="${port_mapping:-80}"
  if [[ "$port_mapping" == *:* ]]; then
    host="${port_mapping%:*}"
    port="${port_mapping##*:}"
    # `0.0.0.0` is a listen address, not an address a local browser can request.
    [[ "$host" == "0.0.0.0" || "$host" == "::" ]] && host="127.0.0.1"
    url="http://${host}:${port}"
  else
    url="http://localhost"
    [[ "$port_mapping" != "80" ]] && url="${url}:${port_mapping}"
  fi
fi

printf '\nWaiting for %s/health …\n' "$url"
if command -v curl >/dev/null 2>&1; then
  ready=0
  for _ in $(seq 1 60); do
    if curl --silent --fail --max-time 3 "${url}/health" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 2
  done
  if [[ "$ready" -ne 1 ]]; then
    "${COMPOSE_CMD[@]}" "${COMPOSE_FILES[@]}" ps
    fail "oc8 did not become healthy in time. Inspect: ${COMPOSE_CMD[*]} ${COMPOSE_FILES[*]} logs -f backend"
  fi
fi

printf '\n✓ oc8 (%s mode) is running at %s\n' "$MODE" "$url"
case "$MODE" in
  community) printf 'Next: open the URL and create the local administrator account.\n' ;;
  demo) printf 'Next: sign in as %s with the password shown above (or already in .env).\n' "${demo_email:-demo@oc8.ai}" ;;
  dev) printf 'Next: open the URL -- dev-login signs you in as org_admin with no password.\n' ;;
esac
printf 'Logs: %s logs -f backend\n' "${COMPOSE_CMD[*]}"
printf 'Stop later (keeps data): %s stop\n' "${COMPOSE_CMD[*]}"
if [[ "$MODE" != "community" ]]; then
  printf 'Restart later in the same mode: %s %s up -d\n' "${COMPOSE_CMD[*]}" "${COMPOSE_FILES[*]}"
fi
