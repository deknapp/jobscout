"""Test-wide guards.

The suite must not reach the network. jobscout probes ATS APIs to find careers
boards without paying a model, which is the right behaviour in production and
the wrong behaviour in a test: it makes the suite slow, flaky, dependent on
someone else's uptime, and rude to the boards being probed. Wiring board
discovery into resolve_board turned six pipeline tests into real HTTP clients
and took the suite from 0.3s to 37s, which is how this was noticed.

Blocked here rather than in each test, because the next test to call into the
pipeline would not know it had to opt out.
"""
import pytest

from jobscout import discover


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Every probe fails, as if the employer had no ATS. Tests that want a
    specific answer pass their own getter, which is what the discover tests do.
    """
    def unreachable(url: str):
        # Inert rather than fatal: a probe that fails is a normal outcome
        # (most employers are not on the ATS being tried), so the pipeline
        # falls through to its model path exactly as it did before discovery
        # existed. Raising here would instead make every pipeline test assert
        # on an implementation detail of board lookup.
        attempted.append(url)
        return 0, ""

    attempted: list[str] = []
    monkeypatch.setattr(discover, "_http_get", unreachable)
    return attempted
