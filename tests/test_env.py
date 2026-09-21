"""`.env` loading and JEVGUARD_* policy overrides."""

import pytest

from jevguard import Guard, GuardPolicy
from jevguard.env import applied_overrides, env_names, env_overrides, find_dotenv, load_dotenv, parse_dotenv


@pytest.fixture(autouse=True)
def enable_dotenv(monkeypatch):
    """These tests exercise .env discovery, which the suite disables by default."""
    monkeypatch.delenv("JEVGUARD_NO_DOTENV", raising=False)


def test_parse_dotenv_handles_real_world_lines():
    parsed = parse_dotenv(
        "# a comment\n"
        "\n"
        "TYPESAFE_API_KEY=sk-123\n"
        "export JEVGUARD_MODE=shadow\n"
        'JEVGUARD_SCOPE="billing support, refunds"\n'
        "JEVGUARD_JEV_HEADERS='x-a=1,x-b=2'\n"
        "JEVGUARD_INPUT_BLOCK=0.7   # trailing comment\n"
        "MALFORMED LINE\n"
    )
    assert parsed["TYPESAFE_API_KEY"] == "sk-123"
    assert parsed["JEVGUARD_MODE"] == "shadow"
    assert parsed["JEVGUARD_SCOPE"] == "billing support, refunds"
    assert parsed["JEVGUARD_JEV_HEADERS"] == "x-a=1,x-b=2"
    assert parsed["JEVGUARD_INPUT_BLOCK"] == "0.7"
    assert "MALFORMED LINE" not in parsed


def test_real_environment_beats_the_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TYPESAFE_API_KEY=from-file\nJEVGUARD_MODE=shadow\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "from-shell")
    load_dotenv()
    import os

    assert os.environ["TYPESAFE_API_KEY"] == "from-shell"   # shell wins
    assert os.environ["JEVGUARD_MODE"] == "shadow"          # file fills the gap


def test_dotenv_is_found_from_a_subdirectory(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JEVGUARD_SCOPE=from-parent\n", encoding="utf-8")
    nested = tmp_path / "services" / "agent"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert find_dotenv() == tmp_path / ".env"


def test_env_file_variable_and_opt_out(tmp_path, monkeypatch):
    env_file = tmp_path / "custom.env"
    env_file.write_text("JEVGUARD_MODE=shadow\n", encoding="utf-8")
    monkeypatch.setenv("JEVGUARD_ENV_FILE", str(env_file))
    assert load_dotenv() == {"JEVGUARD_MODE": "shadow"}
    monkeypatch.setenv("JEVGUARD_NO_DOTENV", "1")
    assert load_dotenv() == {}


def test_overrides_cover_every_kind_of_field(monkeypatch):
    monkeypatch.setenv("JEVGUARD_MODE", "shadow")
    monkeypatch.setenv("JEVGUARD_CANARIES", "CANARY-1, CANARY-2")
    monkeypatch.setenv("JEVGUARD_REDACT_LOGGED_TEXT", "true")
    monkeypatch.setenv("JEVGUARD_TOOLS_DENY", "drop_db,rm")
    monkeypatch.setenv("JEVGUARD_INPUT_BLOCK", "0.7")
    monkeypatch.setenv("JEVGUARD_OUTPUT_PII", "redact")
    monkeypatch.setenv("JEVGUARD_SESSION_MAX_IDENTICAL_TOOL_CALLS", "2")
    monkeypatch.setenv("JEVGUARD_JEV_HEADERS", "x-a=1,x-b=2")

    policy = GuardPolicy.load()
    assert policy.mode == "shadow"
    assert policy.canaries == ["CANARY-1", "CANARY-2"]
    assert policy.redact_logged_text is True
    assert policy.tools.deny == ["drop_db", "rm"]
    assert policy.stages["input"].thresholds.block == 0.7
    assert policy.stages["input"].thresholds.flag == 0.5          # untouched default
    assert policy.stages["input"].checks                          # stage defaults survive the merge
    assert policy.stages["output"].pii == "redact"
    assert policy.session.max_identical_tool_calls == 2
    assert policy.jev.headers == {"x-a": "1", "x-b": "2"}
    assert ("JEVGUARD_MODE", "mode") in applied_overrides()


def test_env_wins_over_the_yaml_file(tmp_path, monkeypatch):
    policy_file = tmp_path / "policy.yaml"
    policy_file.write_text("name: from-yaml\nmode: enforce\ntools:\n  deny: [from_yaml]\n", encoding="utf-8")
    monkeypatch.setenv("JEVGUARD_MODE", "shadow")
    policy = GuardPolicy.load(policy_file)
    assert policy.name == "from-yaml"        # yaml keeps what env does not set
    assert policy.mode == "shadow"           # env wins
    assert policy.tools.deny == ["from_yaml"]
    assert GuardPolicy.load(policy_file, env=False).mode == "enforce"   # opt out


def test_guard_picks_up_a_dotenv_file(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("JEVGUARD_MODE=shadow\nJEVGUARD_TOOLS_DENY=nuke\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    guard = Guard(backend="simulated")
    assert guard.policy.mode == "shadow"
    d = guard.check_tool_call("nuke", {})
    assert d.action.value == "block" and not d.blocked   # shadow mode: recorded, not enforced


def test_gateway_settings_from_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "AI_GATEWAY_API_KEY=vck_test\n"
        "JEVGUARD_JEV_API_KEY_ENV=AI_GATEWAY_API_KEY\n"
        "JEVGUARD_JEV_BASE_URL=https://gw.example/v1/systemone\n"
        "JEVGUARD_JEV_HEADERS=x-route=typesafe/jev\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    guard = Guard()
    assert guard.status()["backend"] == "jev-api"
    assert guard.backend.base_url == "https://gw.example/v1/systemone"
    assert guard.backend.api_key == "vck_test"
    assert guard.backend.headers["x-route"] == "typesafe/jev"


def test_bad_value_is_reported_with_its_variable(monkeypatch):
    monkeypatch.setenv("JEVGUARD_INPUT_BLOCK", "very-strict")
    with pytest.raises(ValueError, match="JEVGUARD_INPUT_BLOCK"):
        env_overrides()


def test_env_example_documents_every_variable():
    """Every supported variable should appear in .env.example, so the file stays the reference."""
    from pathlib import Path

    text = Path(__file__).resolve().parents[1].joinpath(".env.example").read_text(encoding="utf-8")
    documented = {line.lstrip("# ").split("=")[0].strip() for line in text.splitlines() if "=" in line}
    undocumented = [n for n in env_names() if n not in documented]
    # the per-stage matrix is documented by example, not one line per stage
    undocumented = [n for n in undocumented if not any(n.startswith(f"JEVGUARD_{s}_") for s in
                                                       ("INPUT", "TOOL_CALL", "TOOL_RESULT", "RETRIEVAL", "OUTPUT"))]
    assert undocumented == []


def test_dashboard_server_honours_dotenv(tmp_path, monkeypatch):
    """Regression: the server used to build its default policy without env overrides."""
    from fastapi.testclient import TestClient

    from jevguard.server.app import create_app

    (tmp_path / ".env").write_text("JEVGUARD_MODE=shadow\nJEVGUARD_TOOLS_DENY=drop_database\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(str(tmp_path / "s.db")))
    policy = client.get("/api/policy").json()
    assert policy["policy"]["mode"] == "shadow"
    assert policy["policy"]["tools"]["deny"] == ["drop_database"]
    assert {o["var"] for o in policy["env_overrides"]} >= {"JEVGUARD_MODE", "JEVGUARD_TOOLS_DENY"}
    # and the running guard actually uses it
    d = client.post("/api/guard", json={"stage": "tool_call", "context": {"tool_name": "drop_database"}}).json()
    assert d["action"] == "block" and d["blocked"] is False   # shadow
