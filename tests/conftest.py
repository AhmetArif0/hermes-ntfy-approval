"""Shared fixtures: the plugin package, an ntfy server, and a phone that taps notification buttons.

By default the tests use the in-process FakeNtfy. With NTFY_TEST_SERVER=http://host:port they
run against a real ntfy server instead; tests that need a fault switch only the fake has are
skipped there.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import threading
import time
import urllib.request
import uuid
from concurrent.futures import Future
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_ntfy import FakeNtfy  # noqa: E402

PLUGIN_DIR = Path(__file__).resolve().parents[1] / "ntfy-approval"
REAL_SERVER = os.environ.get("NTFY_TEST_SERVER", "").rstrip("/")


def _load_plugin_package():
    name = "ntfy_approval"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin_package()
transport = importlib.import_module("ntfy_approval.transport")
client_mod = importlib.import_module("ntfy_approval.client")
cli = importlib.import_module("ntfy_approval.cli")

fake_only = pytest.mark.skipif(bool(REAL_SERVER), reason="needs a FakeNtfy fault switch")


@pytest.fixture
def fake():
    server = FakeNtfy().start()
    yield server
    server.stop()


@pytest.fixture
def server_url(request):
    if REAL_SERVER:
        return REAL_SERVER
    return request.getfixturevalue("fake").url


@pytest.fixture
def topic():
    return "t" + uuid.uuid4().hex  # 33 characters, unique per test


class Request:
    """Shaped like Hermes' ApprovalRequest; ``respond`` returns what the host would receive."""

    def __init__(self, *, command="rm -rf /tmp/build", description="recursive delete",
                 timeout_seconds=10.0, allowed_choices=("once", "session", "always", "deny")):
        self.request_id = uuid.uuid4().hex
        self.digest = uuid.uuid4().hex
        self.command = command
        self.description = description
        self.timeout_seconds = timeout_seconds
        self.allowed_choices = tuple(allowed_choices)

    def respond(self, choice):
        return ("decision", self.request_id, self.digest, choice)


class Phone:
    """What the ntfy app does: read the topic, show a notification, run a button's http action."""

    def __init__(self, server: str, topic: str, token: str = ""):
        self.server, self.topic, self.token = server, topic, token
        self._seen: set = set()

    def _get(self, topic: str) -> list:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        req = urllib.request.Request(f"{self.server}/{topic}/json?poll=1&since=all", headers=headers)
        with urllib.request.urlopen(req, timeout=5) as response:
            return [json.loads(line) for line in response if line.strip()]

    def events(self, topic: str = "") -> list:
        return self._get(topic or self.topic)

    def notification(self, request_id: str = "", timeout: float = 10.0) -> dict:
        """The next unseen notification, or the one for ``request_id`` (a topic may hold old ones)."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for event in self._get(self.topic):
                if event["event"] != "message" or event["id"] in self._seen:
                    continue
                if request_id and not any(request_id in a.get("body", "") for a in event.get("actions", [])):
                    continue
                self._seen.add(event["id"])
                return event
            time.sleep(0.05)
        raise AssertionError("no notification arrived")

    def tap(self, note: dict, label: str, *, body: str = "") -> None:
        action = next(a for a in note["actions"] if a["label"] == label)
        assert action["action"] == "http"
        req = urllib.request.Request(action["url"], data=(body or action["body"]).encode(),
                                     method=action.get("method", "POST"), headers=action.get("headers") or {})
        urllib.request.urlopen(req, timeout=5).read()

    def post(self, topic: str, text: str) -> None:
        req = urllib.request.Request(f"{self.server}/{topic}", data=text.encode(), method="POST")
        urllib.request.urlopen(req, timeout=5).read()


@pytest.fixture
def phone(server_url, topic):
    return Phone(server_url, topic)


def run_async(fn, *args, **kwargs) -> Future:
    future: Future = Future()

    def target():
        try:
            future.set_result(fn(*args, **kwargs))
        except BaseException as exc:  # noqa: BLE001 - handed to the test
            future.set_exception(exc)

    threading.Thread(target=target, daemon=True).start()
    return future
