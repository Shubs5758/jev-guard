import pytest

from jevguard import Guard, MemorySink
from jevguard.env import env_names


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    """Keep the suite independent of the machine it runs on.

    Clears every variable jevguard reads (monkeypatch restores them afterwards, which also
    cleans up anything a test loads from a .env) and disables .env discovery by default, so a
    stray .env in a parent directory cannot change what the tests see. The .env tests opt back in.
    """
    for name in [*env_names(), "TYPESAFE_API_KEY", "AI_GATEWAY_API_KEY", "JEVGUARD_ENV_FILE",
                 "JEVGUARD_DB", "JEVGUARD_API_KEY", "JEVGUARD_POLICY"]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("JEVGUARD_NO_DOTENV", "1")


@pytest.fixture
def sink() -> MemorySink:
    return MemorySink()


@pytest.fixture
def guard(sink: MemorySink) -> Guard:
    # The offline simulator keeps tests deterministic and free of network calls.
    return Guard(backend="simulated", sinks=[sink])
