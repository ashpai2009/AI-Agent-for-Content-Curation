"""Regression tests for operator-facing scripts that sit outside the package."""

from __future__ import annotations

import hashlib

from conftest import problem, step
from scripts import preflight, shadow_run


def test_shadow_runner_tracks_the_current_batched_agent_contracts(
    make_workbook, monkeypatch
):
    """The runner had drifted to single-block schemas and crashed before its first call.

    Two blocks force the default batched audit and sweep paths. This remains offline: the
    scripted observer records logical calls but never starts Claude or uses credentials.
    """
    source = make_workbook(
        [
            problem("p1", title="Add", oer_src="source", license="CC BY"),
            step("p1", answer="2", answer_type="numeric"),
            problem("p2", title="Subtract", oer_src="source", license="CC BY"),
            step("p2", answer="1", answer_type="numeric"),
        ]
    )
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    monkeypatch.setattr("sys.argv", ["shadow_run.py", str(source)])

    assert shadow_run.main() == 0
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_preflight_reports_a_configuration_failure_without_a_traceback(
    monkeypatch, capsys
):
    """The launch path should tell a curator what to do before either server starts."""
    from oatutor_council.config import ConfigurationError

    def fail(_settings):
        raise ConfigurationError("sign in first")

    monkeypatch.setattr(preflight, "require_authentication", fail)
    assert preflight.main() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "not ready: sign in first\n"


def test_preflight_accepts_a_ready_subscription(monkeypatch, capsys):
    monkeypatch.setattr(preflight, "require_authentication", lambda _settings: None)
    assert preflight.main() == 0
    assert capsys.readouterr().out == "ready: Claude subscription login is available\n"
