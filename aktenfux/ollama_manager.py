"""Ollama availability and model management helpers."""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 10.0  # seconds for quick checks


def _get(url: str, timeout: float = _TIMEOUT) -> Any:
    """Perform a GET request and return the parsed JSON or raise."""
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def is_ollama_installed() -> bool:
    """Return True if the `ollama` binary is on PATH."""
    # Command is hardcoded and not user-controlled; safe to call without shell=True.
    return bool(subprocess.run(  # noqa: S603
        ["ollama", "--version"],
        capture_output=True,
        check=False,
    ).returncode == 0)


def is_ollama_running(base_url: str = "http://localhost:11434") -> bool:
    """Return True if Ollama's HTTP API responds."""
    logger.debug("Checking Ollama availability: url=%s timeout=%.0fs", base_url, _TIMEOUT)
    try:
        with httpx.Client(timeout=_TIMEOUT) as client:
            r = client.get(base_url)
            running = r.status_code < 500
            logger.debug("Ollama reachability check: status=%d running=%s", r.status_code, running)
            return running
    except Exception as exc:  # noqa: BLE001
        logger.debug("Ollama not reachable: %s", exc)
        return False


def list_models(base_url: str = "http://localhost:11434") -> list[str] | None:
    """Return installed model names, or ``None`` when discovery failed.

    An empty list means discovery succeeded and no models were returned.  It
    must remain distinct from an unavailable, unauthorized, timed-out, or
    malformed discovery response so callers do not offer a download based on
    unknown state.
    """
    logger.debug("Listing Ollama models: url=%s timeout=%.0fs", base_url, _TIMEOUT)
    try:
        data = _get(f"{base_url}/api/tags")
        if not isinstance(data, dict) or not isinstance(data.get("models"), list):
            raise ValueError("Ollama model response must contain a models list")
        models: list[str] = []
        for item in data["models"]:
            if not isinstance(item, dict):
                raise ValueError("Ollama model entry must be an object")
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("Ollama model name must be a non-empty string")
            models.append(name)
        logger.debug("Ollama models available: %s", models)
        return models
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not list Ollama models: %s", exc)
        return None


def ensure_model(model_name: str, base_url: str = "http://localhost:11434") -> bool:
    """Return True if *model_name* is available locally.

    If the model is not installed, prompt the user for confirmation before
    downloading it (models can be several GB).
    """
    installed = list_models(base_url)
    if installed is None:
        logger.error(
            "Could not determine whether model '%s' is installed; refusing to offer a pull.",
            model_name,
        )
        return False

    # Ollama model names may include a tag; match on name prefix too.
    if _model_is_listed(installed, model_name):
        logger.debug("Model '%s' is already installed locally.", model_name)
        return True

    print(
        f"\nThe model '{model_name}' is not installed locally.\n"
        "It must be downloaded once. This can be several GB.\n"
        "Continue? [y/N] ",
        end="",
        flush=True,
    )
    answer = sys.stdin.readline().strip().lower()
    if answer != "y":
        logger.info("Model download cancelled by user.")
        return False

    if not pull_model(model_name, base_url):
        return False

    refreshed = list_models(base_url)
    if refreshed is None:
        logger.error(
            "Model pull completed, but installed models could not be verified; "
            "refusing to continue."
        )
        return False
    if not _model_is_listed(refreshed, model_name):
        logger.error(
            "Model pull completed, but '%s' was not reported as installed; "
            "refusing to continue.",
            model_name,
        )
        return False
    return True


def _model_is_listed(installed: list[str], model_name: str) -> bool:
    return any(m == model_name or m.startswith(model_name + ":") for m in installed)


def pull_model(model_name: str, base_url: str = "http://localhost:11434") -> bool:
    """Pull *model_name* via the Ollama HTTP API. Returns True on success."""
    _pull_timeout = 600.0
    logger.info("Pulling model %s …", model_name)
    logger.debug("Pull request: url=%s model=%s timeout=%.0fs", base_url, model_name, _pull_timeout)
    try:
        with httpx.Client(timeout=_pull_timeout) as client:
            with client.stream(
                "POST",
                f"{base_url}/api/pull",
                json={"name": model_name},
            ) as response:
                response.raise_for_status()
                completed = False
                for line in response.iter_lines():
                    if line:
                        print(line)
                        try:
                            event = json.loads(line)
                        except (TypeError, json.JSONDecodeError) as exc:
                            raise RuntimeError(
                                "Ollama pull stream returned malformed progress data"
                            ) from exc
                        if not isinstance(event, dict):
                            raise RuntimeError(
                                "Ollama pull stream returned a non-object event"
                            )
                        if "error" in event:
                            raise RuntimeError("Ollama pull stream reported an error")
                        if event.get("status") == "success":
                            completed = True
                if not completed:
                    raise RuntimeError(
                        "Ollama pull stream ended without a success event"
                    )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "Failed to pull model %s: %s. Ollama may retain partial model data; "
            "inspect the configured endpoint before retrying.",
            model_name,
            exc,
        )
        return False


def test_model(model_name: str, base_url: str = "http://localhost:11434") -> bool:
    """Send a small test prompt to *model_name*. Returns True on success."""
    _test_timeout = 120.0
    logger.info("Testing model %s …", model_name)
    logger.debug("Test request: url=%s model=%s timeout=%.0fs", base_url, model_name, _test_timeout)
    try:
        with httpx.Client(timeout=_test_timeout) as client:
            response = client.post(
                f"{base_url}/api/generate",
                json={
                    "model": model_name,
                    "prompt": "Reply with the word: OK",
                    "stream": False,
                },
            )
            response.raise_for_status()
            result = response.json()
            text = result.get("response", "").strip()
            logger.info("Model test response: %s", text)
            return bool(text)
    except Exception as exc:  # noqa: BLE001
        logger.error("Model test failed for %s: %s", model_name, exc)
        return False
