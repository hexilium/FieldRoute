"""Validate deployment exposure without a running Docker daemon."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def production_config(tmp_path):
    if not shutil.which("docker"):
        pytest.skip("Docker Compose CLI is required; no daemon is needed")
    version = subprocess.run(["docker", "compose", "version"], capture_output=True, check=False)
    if version.returncode:
        pytest.skip("Docker Compose plugin is unavailable")
    env = os.environ.copy()
    env.pop("FIELDROUTE_DOMAIN", None)
    env.pop("FIELDROUTE_PASSWORD_HASH", None)
    # Single-quoted env values preserve the dollars in a bcrypt hash.
    credentials = tmp_path / "production.env"
    credentials.write_text(
        "FIELDROUTE_DOMAIN='demo.example.org'\n"
        "FIELDROUTE_PASSWORD_HASH='$2a$14$testplaceholder'\n"
    )
    result = subprocess.run(
        ["docker", "compose", "--env-file", str(credentials), "-f",
         str(ROOT / "compose.production.yml"), "config", "--format", "json"],
        check=True, capture_output=True, text=True, env=env,
    )
    return json.loads(result.stdout)


def test_only_authenticated_proxy_publishes_ports(production_config):
    services = production_config["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {"caddy"}
    assert {port["published"] for port in services["caddy"]["ports"]} == {"80", "443"}
    caddy_env = services["caddy"]["environment"]
    assert caddy_env["FIELDROUTE_DOMAIN"] == "demo.example.org"
    # `compose config` escapes dollars to make its output reusable as Compose input.
    assert caddy_env["FIELDROUTE_PASSWORD_HASH"].replace("$$", "$") == "$2a$14$testplaceholder"


def test_dependencies_and_persistent_services(production_config):
    services = production_config["services"]
    assert services["caddy"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert services["api"]["depends_on"]["geodata-prepare"]["condition"] == "service_completed_successfully"
    for name in ("api", "caddy", "roads-car", "roads-foot", "roads-bicycle"):
        assert services[name]["restart"] == "unless-stopped"
        assert services[name]["logging"]["options"]["max-file"] == "3"
    for name in ("map-download", "map-prepare", "geodata-prepare"):
        assert services[name].get("restart", "no") == "no"
    data = next(volume for volume in services["api"]["volumes"] if volume["target"] == "/data")
    assert data["read_only"]


def test_deployment_shell_syntax():
    subprocess.run(["bash", "-n", str(ROOT / "deploy-ubuntu.sh")], check=True)


@pytest.mark.parametrize("mode,failure", [("local", ""), ("local", "exit"), ("local", "timeout"), ("local", "interrupted"), ("external", "")])
def test_preparation_stops_on_failure_and_prints_diagnostics(tmp_path, mode, failure):
    script = (ROOT / "deploy-ubuntu.sh").read_text()
    stages = script.split("show_diagnostics() {", 1)[1].split(
        "printf 'Проверка HTTPS", 1,
    )[0]
    trace = tmp_path / "commands"
    harness = r'''
set -Eeuo pipefail
die() { printf '%s\n' "$*" >&2; exit 1; }
prepare_timeout=14400
routing_mode=$MODE
fake_compose() {
    printf '%s\n' "$*" >> "$TRACE"
    case "$*" in
        'up --no-deps --exit-code-from map-download map-download')
            case "$FAILURE" in
                exit) return 23;;
                timeout) return 124;;
            esac;;
        'ps -aq'*) printf 'test-container\n';;
    esac
}
docker() {
    if [[ $* == *'{{.State.Status}}:{{.State.ExitCode}}'* ]]; then
        if [[ $FAILURE == interrupted ]]; then printf 'running:0\n';
        else printf 'exited:0\n'; fi
    else
        printf 'test-container status=exited exit=23 OOMKilled=false\n'
    fi
}
timeout() { shift 3; "$@"; }
compose=(fake_compose)
'''
    result = subprocess.run(
        ["bash"], input=harness + "show_diagnostics() {" + stages,
        env={**os.environ, "TRACE": str(trace), "FAILURE": failure, "MODE": mode},
        text=True, capture_output=True, check=False,
    )
    commands = trace.read_text()
    if failure:
        assert result.returncode != 0
        assert "logs --no-color --tail 40" in commands
        assert "OOMKilled=false" in result.stderr
        assert "--exit-code-from map-prepare" not in commands
        assert "up -d" not in commands
    else:
        assert result.returncode == 0, result.stderr
        starts = [line for line in commands.splitlines() if line.startswith("up ")]
        expected = (["map-download", "map-prepare", "geodata-prepare", "roads-bicycle"]
                    if mode == "local" else []) + ["api", "caddy"]
        assert [line.split()[-1] for line in starts] == expected
        if mode == "external":
            assert "geodata-prepare" not in commands
            assert "map-download" not in commands
