"""``hermes ntfy-approval setup|test``. Neither command writes a file: ``setup`` prints the
``hermes config set`` commands to run, ``test`` sends one sample request and waits for the tap."""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Callable

from .client import NtfyError
from .transport import DEFAULT_SERVER, ConfigError, Settings, present

LABELS = {"once": "Approve once", "session": "Approve for session", "deny": "Deny"}


@dataclass(frozen=True)
class SampleRequest:
    """Shaped like Hermes' ApprovalRequest, for ``test`` only; ``respond`` returns the choice."""

    request_id: str
    command: str
    description: str
    timeout_seconds: float
    allowed_choices: tuple = ("once", "session", "deny")

    def respond(self, choice: str) -> str:
        return choice


def setup_parser(parser) -> None:
    actions = parser.add_subparsers(dest="ntfy_approval_action")
    setup = actions.add_parser("setup", help="Create a private topic and print the steps to finish setup")
    setup.add_argument("--new-topic", action="store_true", help="Print a new topic even if one is set")
    test = actions.add_parser("test", help="Send a test approval to your phone and wait for your answer")
    test.add_argument("--timeout", type=int, default=120, help="Seconds to wait for a tap (default 120)")


def new_topic() -> str:
    return "hermes-" + secrets.token_urlsafe(24)


def _shown(topic: str) -> str:
    return topic[:10] + "…" if len(topic) > 12 else topic


def run(args, load_settings: Callable[[], Settings], profile: str = "",
        load_server: Callable[[], str] = lambda: DEFAULT_SERVER) -> int:
    action = getattr(args, "ntfy_approval_action", None)
    if action == "setup":
        return _setup(load_settings, load_server, bool(getattr(args, "new_topic", False)))
    if action == "test":
        return _test(load_settings, max(10, int(getattr(args, "timeout", 120))), profile)
    print("usage: hermes ntfy-approval {setup,test}")
    return 1


def _setup(load_settings: Callable[[], Settings], load_server: Callable[[], str], rotate: bool) -> int:
    try:
        server = load_server()
    except ConfigError as exc:
        print(f"Fix the server setting first: {exc}")
        return 1
    try:
        current = load_settings()
    except ConfigError:
        current = None
    if current and not rotate:
        print(f"Already set up: topic {_shown(current.topic)} on {server}.")
        print("Run `hermes ntfy-approval test` to check it, or `hermes ntfy-approval setup --new-topic` "
              "to move to a new topic.")
        return 0
    topic = new_topic()
    print(f"""ntfy-approval setup

1. Install the ntfy app (https://ntfy.sh/app) and subscribe to this topic on {server}:

       {topic}

   Anyone who knows this name can read your approval requests and answer them. Keep it private.

2. Save it for Hermes (this writes your profile's .env, not config.yaml):

       hermes config set NTFY_APPROVAL_TOPIC {topic}

3. Check that a tap on your phone reaches Hermes:

       hermes ntfy-approval test

4. Send Hermes' approval prompts to your phone:

       hermes config set security.approval.transport ntfy

   To go back to the terminal and chat prompts: hermes config set security.approval.transport builtin
""")
    return 0


def _test(load_settings: Callable[[], Settings], timeout: int, profile: str) -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"Not set up: {exc}")
        return 1
    request = SampleRequest(
        request_id=uuid.uuid4().hex, command="echo 'hello from hermes ntfy-approval test'",
        description="Test from `hermes ntfy-approval test`. Tap any button; nothing will run.",
        timeout_seconds=timeout)
    print(f"Sent a test approval to topic {_shown(settings.topic)} on {settings.server}. "
          f"Waiting up to {timeout} s for a tap…")
    try:
        choice = present(request, settings, profile=profile)
    except TimeoutError:
        print(f"No answer within {timeout} s. Is the phone subscribed to the topic on {settings.server}?")
        return 1
    except NtfyError as exc:
        print(f"ntfy refused the request: {exc}.{_hint(exc.status)}")
        return 1
    except OSError as exc:
        print(f"Could not reach {settings.server}: {exc}")
        return 1
    print(f"Your phone answered: {LABELS.get(choice, choice)}. Phone approvals work.")
    return 0


def _hint(status) -> str:
    if status in (401, 403):
        return (" Check NTFY_APPROVAL_TOKEN, and that the reply topic (your topic + '-reply') "
                "accepts anonymous writes; see the README's self-hosting section.")
    if status == 429:
        return " The server is rate limiting this machine; try again in a minute."
    return ""
