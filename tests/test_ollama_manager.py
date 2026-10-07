"""Focused tests for model discovery and acquisition outcomes."""
from __future__ import annotations

import logging
from contextlib import nullcontext
from unittest.mock import patch

import aktenfux.ollama_manager as om
import pytest


def test_list_models_returns_none_when_discovery_fails():
    with patch("aktenfux.ollama_manager._get", side_effect=TimeoutError("timeout")):
        assert om.list_models() is None


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"models": None},
        {"models": {}},
        {"models": ["qwen3:8b"]},
        {"models": [{}]},
        {"models": [{"name": 123}]},
        {"models": [{"name": ""}]},
    ],
)
def test_list_models_returns_none_for_malformed_responses(payload):
    with patch("aktenfux.ollama_manager._get", return_value=payload):
        assert om.list_models() is None


def test_list_models_distinguishes_a_valid_empty_list():
    with patch("aktenfux.ollama_manager._get", return_value={"models": []}):
        assert om.list_models() == []


def test_ensure_model_stops_when_discovery_is_unknown():
    with (
        patch("aktenfux.ollama_manager.list_models", return_value=None),
        patch("aktenfux.ollama_manager.pull_model") as pull,
        patch("aktenfux.ollama_manager.sys.stdin.readline") as read_input,
    ):
        assert om.ensure_model("qwen3:8b") is False

    pull.assert_not_called()
    read_input.assert_not_called()


def test_ensure_model_returns_true_when_model_is_installed():
    with (
        patch("aktenfux.ollama_manager.list_models", return_value=["qwen3:8b"]),
        patch("aktenfux.ollama_manager.pull_model") as pull,
        patch("aktenfux.ollama_manager.sys.stdin.readline") as read_input,
    ):
        assert om.ensure_model("qwen3:8b") is True

    pull.assert_not_called()
    read_input.assert_not_called()


def test_ensure_model_stops_when_pull_is_declined():
    with (
        patch("aktenfux.ollama_manager.list_models", return_value=[]),
        patch("aktenfux.ollama_manager.sys.stdin.readline", return_value="n\n"),
        patch("aktenfux.ollama_manager.pull_model") as pull,
    ):
        assert om.ensure_model("qwen3:8b") is False

    pull.assert_not_called()


def test_ensure_model_returns_true_after_successful_pull():
    with (
        patch(
            "aktenfux.ollama_manager.list_models",
            side_effect=[[], ["qwen3:8b"]],
        ) as list_models,
        patch("aktenfux.ollama_manager.sys.stdin.readline", return_value="y\n"),
        patch("aktenfux.ollama_manager.pull_model", return_value=True) as pull,
    ):
        assert om.ensure_model("qwen3:8b") is True

    pull.assert_called_once_with("qwen3:8b", "http://localhost:11434")
    assert list_models.call_count == 2


def test_ensure_model_stops_when_successful_pull_cannot_be_verified():
    with (
        patch("aktenfux.ollama_manager.list_models", side_effect=[[], None]),
        patch("aktenfux.ollama_manager.sys.stdin.readline", return_value="y\n"),
        patch("aktenfux.ollama_manager.pull_model", return_value=True),
    ):
        assert om.ensure_model("qwen3:8b") is False


def test_ensure_model_stops_when_pulled_model_is_not_listed():
    with (
        patch("aktenfux.ollama_manager.list_models", side_effect=[[], []]),
        patch("aktenfux.ollama_manager.sys.stdin.readline", return_value="y\n"),
        patch("aktenfux.ollama_manager.pull_model", return_value=True),
    ):
        assert om.ensure_model("qwen3:8b") is False


def test_ensure_model_returns_false_after_failed_pull():
    with (
        patch("aktenfux.ollama_manager.list_models", return_value=[]),
        patch("aktenfux.ollama_manager.sys.stdin.readline", return_value="y\n"),
        patch("aktenfux.ollama_manager.pull_model", return_value=False) as pull,
    ):
        assert om.ensure_model("qwen3:8b") is False

    pull.assert_called_once_with("qwen3:8b", "http://localhost:11434")


def test_failed_pull_warns_about_possible_partial_state(caplog):
    response = type("Response", (), {"raise_for_status": lambda self: None})()
    stream = type(
        "Stream",
        (),
        {
            "__enter__": lambda self: response,
            "__exit__": lambda self, *args: False,
        },
    )()

    with (
        patch("aktenfux.ollama_manager.httpx.Client") as client_type,
        caplog.at_level(logging.ERROR),
    ):
        client_type.return_value.__enter__.return_value.stream.return_value = stream
        assert om.pull_model("qwen3:8b") is False

    assert "may retain partial model data" in caplog.text


def _pull_client_with_lines(lines):
    response = type(
        "Response",
        (),
        {
            "raise_for_status": lambda self: None,
            "iter_lines": lambda self: iter(lines),
        },
    )()
    client = type(
        "Client",
        (),
        {"stream": lambda self, *args, **kwargs: nullcontext(response)},
    )()
    return nullcontext(client)


def test_pull_model_requires_an_explicit_success_event():
    client = _pull_client_with_lines(['{"status":"pulling manifest"}'])
    with (
        patch("aktenfux.ollama_manager.httpx.Client", return_value=client),
        patch("builtins.print"),
    ):
        assert om.pull_model("qwen3:8b") is False


def test_pull_model_rejects_an_error_event():
    client = _pull_client_with_lines(['{"error":"remote detail"}'])
    with (
        patch("aktenfux.ollama_manager.httpx.Client", return_value=client),
        patch("builtins.print"),
    ):
        assert om.pull_model("qwen3:8b") is False


def test_pull_model_accepts_only_a_completed_stream():
    client = _pull_client_with_lines(
        ['{"status":"pulling manifest"}', '{"status":"success"}']
    )
    with (
        patch("aktenfux.ollama_manager.httpx.Client", return_value=client),
        patch("builtins.print"),
    ):
        assert om.pull_model("qwen3:8b") is True
