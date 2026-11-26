"""The Unraid deployment files must stay in step with the application they deploy."""
import re
from pathlib import Path

import yaml

from app.config import Settings

ROOT = Path(__file__).parent.parent
DEPLOY = ROOT / "deploy" / "unraid"
COMPOSE_TEXT = (DEPLOY / "docker-compose.yml").read_text()
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
ENV_TEXT = (DEPLOY / ".env.example").read_text()
ENV_KEYS = set(re.findall(r"^#?\s*([A-Z][A-Z0-9_]+)=", ENV_TEXT, re.MULTILINE))
ACTIVE = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", ENV_TEXT, re.MULTILINE))


def test_it_is_one_compose_file_and_one_env_file_with_a_guide():
    assert sorted(p.name for p in DEPLOY.iterdir()) == [".env.example", "README.md", "docker-compose.yml"]


def test_the_services_and_their_order_are_right():
    services = COMPOSE["services"]
    assert set(services) == {"init", "postgres", "api", "worker"}
    assert services["api"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    for name in ("api", "postgres"):
        assert services[name]["depends_on"]["init"]["condition"] == "service_completed_successfully"
    assert services["worker"]["depends_on"]["api"]["condition"] == "service_healthy"
    assert "app.worker" in services["worker"]["command"] and "app.worker" not in services["api"]["command"]
    assert "alembic upgrade head" in services["api"]["command"]
    assert "app.bootstrap" in services["api"]["command"]      # creates the admin and declared stores
    assert services["init"]["restart"] == "no"


def test_it_runs_as_unraids_user_keeps_data_under_appdata_and_survives_restarts():
    for name in ("api", "worker"):
        assert services_user(name) == "${PUID:-99}:${PGID:-100}"
    assert all(COMPOSE["services"][n]["restart"] == "unless-stopped" for n in ("postgres", "api", "worker"))
    assert COMPOSE_TEXT.count("/mnt/user/appdata/retail-monitor") >= 3
    assert "${DATA_DIR:-/mnt/user/appdata/retail-monitor}/postgres:/var/lib/postgresql/data" in COMPOSE_TEXT
    assert "/data/connectors" in COMPOSE_TEXT and "INSTALLED_CONNECTORS_DIR" not in COMPOSE_TEXT  # baked into the image


def services_user(name):
    return COMPOSE["x-app"]["user"] if name in ("api", "worker") else None


def test_logs_are_rotated_so_they_cannot_fill_unraids_docker_image():
    for name in ("postgres", "api", "worker"):
        logging = COMPOSE["services"][name].get("logging") or COMPOSE["x-app"]["logging"]
        assert logging["options"]["max-size"] and logging["options"]["max-file"]


def test_the_api_is_reachable_on_the_lan_and_has_unraid_labels():
    api = COMPOSE["services"]["api"]
    assert api["ports"] == ["${BIND_ADDRESS:-0.0.0.0}:${HTTP_PORT:-8085}:8000"]
    assert api["labels"]["net.unraid.docker.managed"] == "composeman"
    assert api["labels"]["net.unraid.docker.webui"].endswith("/admin")
    assert "/health/ready" in " ".join(api["healthcheck"]["test"])


def test_every_variable_the_compose_file_uses_is_documented_in_the_env_example():
    used = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", COMPOSE_TEXT))
    assert used <= ENV_KEYS, f"undocumented in .env.example: {sorted(used - ENV_KEYS)}"


def test_every_setting_in_the_env_example_is_one_the_application_reads():
    fields = {name.upper() for name in Settings.model_fields}
    compose_vars = set(re.findall(r"\$\{([A-Z][A-Z0-9_]+)", COMPOSE_TEXT))
    for key in ENV_KEYS:
        assert key in fields or key in compose_vars or key in ("POSTGRES_PASSWORD", "APP_SECRET"), \
            f"{key} in .env.example is not read by anything"
    assert "DATABASE_URL" not in ENV_KEYS    # derived from POSTGRES_PASSWORD, so the two cannot disagree


def test_required_values_ship_empty_so_a_placeholder_can_never_be_deployed():
    for key in ("POSTGRES_PASSWORD", "APP_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD"):
        assert ACTIVE[key] == "", key
        assert key in COMPOSE_TEXT or key in ("APP_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD")
    assert "${POSTGRES_PASSWORD:?" in COMPOSE_TEXT
    assert not re.search(r"change-me|replace-with", ENV_TEXT)


def test_the_image_the_compose_file_builds_contains_what_the_service_serves():
    dockerfile = (ROOT / "Dockerfile").read_text()
    ignored = (ROOT / ".dockerignore").read_text().split()
    for needed in ("connectors", "static", "migrations", "app", "docs/api.md"):
        assert f"COPY {needed} " in dockerfile, needed
    assert not {"connectors", "static", "migrations", "app"} & set(ignored)
    assert "!docs/api.md" in ignored and ".env" in ignored    # the guide ships; secrets never do
    assert COMPOSE["x-app"]["build"] == {"context": "../..", "dockerfile": "Dockerfile"}


def test_the_guide_names_every_file_and_command_it_relies_on():
    guide = (DEPLOY / "README.md").read_text()
    for needle in ("docker compose up -d --build", ".env.example", "/admin", "/demo", "/api-guide",
                   "pg_dump", "04:00-08:45", "APP_SECRET", "PUBLIC_BASE_URL", "BIND_ADDRESS"):
        assert needle in guide, needle
