#!/usr/bin/env bash
# Initialise and start the OpenBroker platform (docker compose).
#
#   ./setup.sh              validate, generate missing secrets, build, start
#   ./setup.sh --check      validate only; change nothing, start nothing
#   ./setup.sh --auto-port  if WEB_PORT is taken, use the next free port
#   ./setup.sh --public     allow WEB_BIND_ADDR to be a non-loopback address
#   ./setup.sh --no-start   validate and build, but do not start
#
# Safe to re-run.

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

CHECK_ONLY=0
AUTO_PORT=0
ALLOW_PUBLIC=0
NO_START=0
MIN_DOCKER_MAJOR=24
MIN_COMPOSE_MAJOR=2
GENERATED_SECRETS=(API_AUTH_TOKEN EXECUTION_INTERNAL_TOKEN ADMIN_TOKEN POSTGRES_PASSWORD REDIS_PASSWORD)

usage() { sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

for arg in "$@"; do
  case "$arg" in
    --check) CHECK_ONLY=1 ;;
    --auto-port) AUTO_PORT=1 ;;
    --public) ALLOW_PUBLIC=1 ;;
    --no-start) NO_START=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "unknown option: $arg" >&2; usage >&2; exit 2 ;;
  esac
done

info() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

# --- dependencies ------------------------------------------------------------

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "$1 is not installed. $2"; }

major() { printf '%s' "${1#v}" | cut -d. -f1; }

check_dependencies() {
  require_cmd docker "Install Docker Engine: https://docs.docker.com/engine/install/"
  docker compose version >/dev/null 2>&1 \
    || die "the Docker Compose v2 plugin is missing: https://docs.docker.com/compose/install/"
  docker info >/dev/null 2>&1 \
    || die "the Docker daemon is not reachable. Start it, or add this user to the docker group."

  local docker_version compose_version
  docker_version="$(docker version --format '{{.Server.Version}}')"
  compose_version="$(docker compose version --short)"
  [[ "$(major "$docker_version")" -ge "$MIN_DOCKER_MAJOR" ]] \
    || die "Docker $docker_version is too old; need $MIN_DOCKER_MAJOR or newer."
  [[ "$(major "$compose_version")" -ge "$MIN_COMPOSE_MAJOR" ]] \
    || die "Docker Compose $compose_version is too old; need v$MIN_COMPOSE_MAJOR or newer."
  info "Docker $docker_version, Compose $compose_version"
}

# --- .env --------------------------------------------------------------------

# Value of KEY in .env (last assignment wins), without surrounding quotes.
env_get() {
  local line
  line="$(grep -E "^$1=" .env | tail -n 1 || true)"
  line="${line#*=}"
  line="${line%\"}"; line="${line#\"}"
  line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}

# Set KEY=VALUE in .env, replacing an existing assignment or appending one.
env_set() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp .env.XXXXXX)"
  chmod 600 "$tmp"
  if grep -qE "^${key}=" .env; then
    awk -v k="$key" -v v="$value" 'BEGIN { FS = OFS = "=" } $1 == k { print k "=" v; next } { print }' \
      .env >"$tmp"
  else
    cat .env >"$tmp"
    printf '%s=%s\n' "$key" "$value" >>"$tmp"
  fi
  mv "$tmp" .env
}

random_hex() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -hex 32
  else
    head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'
  fi
}

prepare_env() {
  if [[ ! -f .env ]]; then
    [[ "$CHECK_ONLY" -eq 1 ]] && die ".env does not exist. Run ./setup.sh without --check to create it."
    [[ -f .env.example ]] || die ".env.example is missing; is this the repository root?"
    (umask 077 && cp .env.example .env)
    info "created .env from .env.example"
  fi
  chmod 600 .env 2>/dev/null || warn "could not restrict permissions on .env"

  local key
  for key in "${GENERATED_SECRETS[@]}"; do
    [[ -n "$(env_get "$key")" ]] && continue
    [[ "$CHECK_ONLY" -eq 1 ]] && die "$key is empty in .env. Run ./setup.sh without --check to generate it."
    env_set "$key" "$(random_hex)"
    info "generated $key"
  done

  # These end up inside connection URLs, so they must not need escaping.
  for key in POSTGRES_PASSWORD REDIS_PASSWORD POSTGRES_USER POSTGRES_DB; do
    [[ "$(env_get "$key")" =~ ^[A-Za-z0-9._~-]+$ ]] \
      || die "$key may only contain letters, digits and . _ ~ - (it is embedded in a URL)."
  done
}

validate_trading_config() {
  local broker network live
  broker="$(env_get BROKER)"; broker="${broker:-paper}"
  network="$(env_get PLATFORM_HL_NETWORK)"; network="${network:-testnet}"
  live="$(env_get LIVE_TRADING)"; live="${live:-false}"

  case "$broker" in
    paper) ;;
    hyperliquid)
      [[ "$(env_get HL_ACCOUNT_ADDRESS)" =~ ^0x[0-9a-fA-F]{40}$ ]] \
        || die "BROKER=hyperliquid needs HL_ACCOUNT_ADDRESS (0x followed by 40 hex characters)."
      [[ -n "$(env_get HL_AGENT_PRIVATE_KEY)" ]] \
        || die "BROKER=hyperliquid needs HL_AGENT_PRIVATE_KEY."
      if [[ "$network" == "mainnet" && "${live,,}" != "true" ]]; then
        die "BROKER=hyperliquid on mainnet needs LIVE_TRADING=true."
      fi
      ;;
    *) die "BROKER must be 'paper' or 'hyperliquid', not '$broker'." ;;
  esac
  [[ "$network" == "testnet" || "$network" == "mainnet" ]] \
    || die "PLATFORM_HL_NETWORK must be 'testnet' or 'mainnet', not '$network'."

  if [[ "$broker" == "hyperliquid" && "$network" == "mainnet" ]]; then
    warn "LIVE TRADING: orders will be sent to Hyperliquid mainnet with real funds."
  fi
  info "broker=$broker network=$network"
}

# --- port binding ------------------------------------------------------------

port_in_use() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -Hltn "sport = :$port" 2>/dev/null | grep -q .
  elif command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null
  fi
}

# True when the port is held by this project's own web container (a re-run).
port_is_ours() {
  local published
  published="$(docker compose port web 8080 2>/dev/null || true)"
  [[ -n "$published" && "${published##*:}" == "$1" ]]
}

check_port() {
  local addr port candidate
  addr="$(env_get WEB_BIND_ADDR)"; addr="${addr:-127.0.0.1}"
  port="$(env_get WEB_PORT)"; port="${port:-8080}"

  if ! [[ "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    die "WEB_PORT must be a number from 1 to 65535, not '$port'."
  fi
  if ((port < 1024)) && [[ "$(id -u)" -ne 0 ]]; then
    warn "WEB_PORT=$port is a privileged port; rootless Docker cannot bind it."
  fi

  case "$addr" in
    127.* | localhost | ::1) ;;
    *)
      [[ "$ALLOW_PUBLIC" -eq 1 ]] \
        || die "WEB_BIND_ADDR=$addr exposes the terminal beyond this host. Re-run with --public if that is intended."
      warn "the terminal will be reachable on $addr:$port. It serves plain HTTP; put TLS in front."
      ;;
  esac

  if port_in_use "$port" && ! port_is_ours "$port"; then
    [[ "$AUTO_PORT" -eq 1 ]] || die "port $port is already in use. Change WEB_PORT in .env or pass --auto-port."
    [[ "$CHECK_ONLY" -eq 1 ]] && die "port $port is already in use (--check does not change .env)."
    candidate=$((port + 1))
    while port_in_use "$candidate"; do
      candidate=$((candidate + 1))
      ((candidate <= 65535)) || die "no free port found above $port."
    done
    env_set WEB_PORT "$candidate"
    info "port $port is taken; WEB_PORT set to $candidate"
    port="$candidate"
  fi
  WEB_URL="http://${addr}:${port}"
  info "web will bind $addr:$port"
}

# --- run ---------------------------------------------------------------------

check_dependencies
prepare_env
validate_trading_config
check_port
docker compose config --quiet || die "docker-compose.yml did not validate."
info "compose file is valid"

if [[ "$CHECK_ONLY" -eq 1 ]]; then
  info "all checks passed"
  exit 0
fi

info "building images"
docker compose build

if [[ "$NO_START" -eq 1 ]]; then
  info "build finished (--no-start)"
  exit 0
fi

info "starting services"
if ! docker compose up --detach --wait --wait-timeout 180; then
  docker compose ps >&2 || true
  die "services did not become healthy. Inspect with: docker compose logs"
fi

docker compose ps
info "terminal: $WEB_URL"
info "gateway health: $WEB_URL/api/healthz"
