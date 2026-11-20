"""MQ dispatch + publisher behaviour.

The real broker and the worker consuming it are covered by a docker smoke test
(see KAN-74 PR body). These are the unit tests that run in CI without a broker:
publisher.enabled() reads the env; dispatch() picks the right handoff; publish
failure falls back to in-process.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.mq import publisher as mq_publisher
from app.mq.dispatch import dispatch


@pytest.fixture(autouse=True)
def _reset_publisher(monkeypatch):
    """Each test starts with no RABBITMQ_URL and a closed publisher connection."""
    monkeypatch.delenv("RABBITMQ_URL", raising=False)
    mq_publisher.close()
    yield
    mq_publisher.close()


def test_publisher_is_disabled_without_the_env_var():
    assert mq_publisher.enabled() is False


def test_publisher_is_enabled_when_the_env_var_is_set(monkeypatch):
    monkeypatch.setenv("RABBITMQ_URL", "amqp://u:p@rabbitmq:5672/")
    assert mq_publisher.enabled() is True


def test_dispatch_in_fallback_mode_with_a_request_uses_background_tasks():
    """A request context (BackgroundTasks given) + no broker: hand off via background.add_task."""
    background = MagicMock()
    graph = MagicMock()

    dispatch("case_abc", graph, background=background)

    # The task was scheduled: runner.run_case will be called with (graph, case_id).
    background.add_task.assert_called_once()
    args = background.add_task.call_args.args
    assert args[1:] == (graph, "case_abc")


def test_dispatch_in_fallback_mode_without_a_request_runs_inline(monkeypatch):
    """No request context (startup, retry loop) + no broker: runner.run_case is called inline."""
    called_with = []
    from app import runner  # late import so the test fixture's env reset wins
    monkeypatch.setattr(runner, "run_case", lambda graph, case_id: called_with.append((graph, case_id)))
    graph = MagicMock()

    dispatch("case_xyz", graph, background=None)

    assert called_with == [(graph, "case_xyz")]


def test_dispatch_in_mq_mode_publishes_and_does_not_fall_back(monkeypatch):
    """With the broker enabled and publish succeeding: BackgroundTasks is not touched."""
    monkeypatch.setenv("RABBITMQ_URL", "amqp://u:p@rabbitmq:5672/")
    published = []
    monkeypatch.setattr(mq_publisher, "enqueue", lambda case_id: published.append(case_id))
    background = MagicMock()

    dispatch("case_from_mq", MagicMock(), background=background)

    assert published == ["case_from_mq"]
    background.add_task.assert_not_called()


def test_a_publish_failure_falls_back_to_in_process(monkeypatch):
    """A broker hiccup must not stall a case: dispatch catches and uses the fallback path."""
    monkeypatch.setenv("RABBITMQ_URL", "amqp://u:p@rabbitmq:5672/")

    def _bang(_case_id):
        raise RuntimeError("broker is unreachable")

    monkeypatch.setattr(mq_publisher, "enqueue", _bang)
    background = MagicMock()
    graph = MagicMock()

    dispatch("case_whoops", graph, background=background)

    # Fallback used BackgroundTasks because we handed one in.
    background.add_task.assert_called_once()
    assert background.add_task.call_args.args[1:] == (graph, "case_whoops")


def test_a_publish_failure_with_no_graph_leaves_the_case_queued(monkeypatch, caplog):
    """MQ mode on an API process with no model: publish fails and there's no graph
    to fall back to. The case must stay QUEUED and the operator must notice —
    not silently crash inside a BackgroundTask."""
    monkeypatch.setenv("RABBITMQ_URL", "amqp://u:p@rabbitmq:5672/")

    def _bang(_case_id):
        raise RuntimeError("broker is unreachable")

    monkeypatch.setattr(mq_publisher, "enqueue", _bang)
    background = MagicMock()

    with caplog.at_level("ERROR", logger="app.mq.dispatch"):
        dispatch("case_stuck", graph=None, background=background)

    background.add_task.assert_not_called()
    assert any("stays queued" in record.message for record in caplog.records)
