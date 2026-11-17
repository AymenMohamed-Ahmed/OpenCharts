"""The network rules in docker-compose.yml, as assertions.

A change that weakens the isolation of the execution layer, or leaks a secret
into a container that should not hold it, fails here.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
SERVICES = COMPOSE["services"]
NETWORKS = COMPOSE["networks"]

BROKER_SECRETS = {"HL_AGENT_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS"}
LLM_SECRETS = {"ANTHROPIC_API_KEY", "OPENAI_API_KEY"}
DATA_SECRETS = {"DATABASE_URL", "REDIS_URL", "POSTGRES_PASSWORD", "REDIS_PASSWORD"}


def networks_of(service: str) -> set[str]:
    return set(SERVICES[service].get("networks", []))


def env_of(service: str) -> set[str]:
    return set(SERVICES[service].get("environment", {}))


def is_internal(network: str) -> bool:
    return bool((NETWORKS[network] or {}).get("internal"))


def test_only_web_publishes_a_port():
    assert [name for name, svc in SERVICES.items() if svc.get("ports")] == ["web"]


def test_web_binds_loopback_by_default():
    (port,) = SERVICES["web"]["ports"]
    assert port.startswith("${WEB_BIND_ADDR:-127.0.0.1}:")


def test_execution_is_only_on_an_internal_network():
    assert networks_of("execution") == {"exec_net"}
    assert is_internal("exec_net")


def test_only_the_gateway_and_the_proxy_can_reach_execution():
    peers = {name for name in SERVICES if "exec_net" in networks_of(name)} - {"execution"}
    assert peers == {"api", "egress-proxy"}


def test_execution_egress_goes_through_the_proxy():
    assert SERVICES["execution"]["environment"]["HTTPS_PROXY"] == "http://egress-proxy:3128"
    assert networks_of("egress-proxy") == {"exec_net", "broker_egress"}
    assert not is_internal("broker_egress")


def test_proxy_allowlist_is_hyperliquid_only():
    hosts = (ROOT / "infra/egress-proxy/allowlist.txt").read_text().split()
    assert sorted(hosts) == ["api.hyperliquid-testnet.xyz", "api.hyperliquid.xyz"]
    conf = (ROOT / "infra/egress-proxy/squid.conf").read_text()
    assert "http_access deny all" in conf


def test_agents_cannot_reach_execution_or_data_stores():
    for other in ("execution", "postgres", "redis", "egress-proxy"):
        assert not networks_of("agents") & networks_of(other), other


def test_data_stores_have_no_route_out():
    for store in ("postgres", "redis"):
        assert networks_of(store) == {"data_net"}
    assert is_internal("data_net")


def test_web_reaches_only_the_gateway():
    assert networks_of("web") == {"edge", "web_net"}
    assert {n for n in SERVICES if "web_net" in networks_of(n)} == {"web", "api"}
    assert {n for n in SERVICES if "edge" in networks_of(n)} == {"web"}


@pytest.mark.parametrize(
    ("service", "forbidden"),
    [
        ("agents", BROKER_SECRETS | DATA_SECRETS | {"API_AUTH_TOKEN", "EXECUTION_INTERNAL_TOKEN"}),
        ("execution", LLM_SECRETS | DATA_SECRETS | {"API_AUTH_TOKEN"}),
        (
            "ingestion",
            BROKER_SECRETS | LLM_SECRETS | {"API_AUTH_TOKEN", "EXECUTION_INTERNAL_TOKEN"},
        ),
        ("api", BROKER_SECRETS | LLM_SECRETS),
        (
            "migrate",
            BROKER_SECRETS
            | LLM_SECRETS
            | {"API_AUTH_TOKEN", "EXECUTION_INTERNAL_TOKEN", "REDIS_URL"},
        ),
        ("web", BROKER_SECRETS | LLM_SECRETS | DATA_SECRETS | {"EXECUTION_INTERNAL_TOKEN"}),
    ],
)
def test_secrets_stay_in_their_own_container(service, forbidden):
    assert not env_of(service) & forbidden


def test_broker_key_is_held_by_execution_only():
    holders = [name for name in SERVICES if "HL_AGENT_PRIVATE_KEY" in env_of(name)]
    assert holders == ["execution"]


def test_no_service_uses_env_file_or_host_networking():
    for name, svc in SERVICES.items():
        assert "env_file" not in svc, name
        assert svc.get("network_mode") is None, name
        assert not svc.get("privileged"), name


@pytest.mark.parametrize("service", ["api", "agents", "ingestion", "execution", "migrate", "web"])
def test_app_containers_are_hardened(service):
    svc = SERVICES[service]
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]


def test_web_build_args_carry_no_secrets():
    assert set(SERVICES["web"]["build"]["args"]) == {"VITE_ADAPTER", "VITE_HL_NETWORK"}


def test_migrate_is_a_one_shot_on_the_data_network_only():
    assert networks_of("migrate") == {"data_net"}
    assert SERVICES["migrate"]["restart"] == "no"
    assert SERVICES["migrate"]["command"] == ["python", "-m", "app.common.migrate"]


@pytest.mark.parametrize("service", ["api", "ingestion"])
def test_database_users_wait_for_migrations(service):
    dep = SERVICES[service]["depends_on"]["migrate"]
    assert dep == {"condition": "service_completed_successfully"}
