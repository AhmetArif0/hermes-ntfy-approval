"""Show one Hermes approval request as an ntfy notification and wait for the button the user taps.

The buttons are ntfy ``http`` actions: the phone itself POSTs a one-time answer to a reply topic
on the same server, and this side reads that topic. Nothing listens on the Hermes machine.
See docs/DESIGN.md.
"""

from __future__ import annotations

import hmac
import http.client
import logging
import secrets
import time
import urllib.error
import urllib.parse
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from .client import NtfyClient, NtfyError

logger = logging.getLogger(__name__)

DEFAULT_SERVER = "https://ntfy.sh"
REPLY_SUFFIX = "-reply"
# ntfy topics are 1-64 characters of [A-Za-z0-9_-]; the reply topic adds REPLY_SUFFIX.
MAX_TOPIC_LENGTH = 64 - len(REPLY_SUFFIX)
# Without an access token, knowing the topic is what lets someone read (and so answer) a request.
MIN_OPEN_TOPIC_LENGTH = 16
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5}
# At most three buttons fit on an ntfy notification. "always" is never offered from the phone.
BUTTONS = (("once", "Approve once"), ("session", "Approve for session"), ("deny", "Deny"))
REPLY_PREFIX = "hermes-approval v1"
MAX_DESCRIPTION_BYTES = 600
MAX_COMMAND_BYTES = 2000

READ_TIMEOUT = 55.0  # longer than ntfy's 45 s keepalive, so a healthy stream never times out
PUBLISH_TIMEOUT = 10.0
WITHDRAW_TIMEOUT = 5.0
RETRY_DELAY = 2.0
_TRANSIENT = (OSError, http.client.HTTPException, urllib.error.URLError)


class ConfigError(Exception):
    """The plugin is not set up well enough to send a request safely."""


@dataclass(frozen=True)
class Settings:
    server: str
    topic: str
    token: str = ""
    priority: int = PRIORITIES["high"]
    send_command: bool = True

    @property
    def reply_topic(self) -> str:
        return self.topic + REPLY_SUFFIX


def normalize_server(value: object) -> str:
    raw = str(value or "").strip() or DEFAULT_SERVER
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
        port = parts.port  # validates the port
    except ValueError:
        raise ConfigError(f"server {raw!r} is not a valid URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ConfigError(f"server {raw!r} must be an http(s) URL")
    if parts.username or parts.password:
        raise ConfigError("put the access token in NTFY_APPROVAL_TOKEN, not in the server URL")
    if parts.query or parts.fragment:
        raise ConfigError(f"server {raw!r} must not have a query or fragment")
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    netloc = host + (f":{port}" if port is not None else "")
    return f"{parts.scheme}://{netloc}{parts.path.rstrip('/')}"


def make_settings(*, server: object = DEFAULT_SERVER, topic: object = "", token: object = "",
                  priority: object = "high", send_command: object = True) -> Settings:
    topic = str(topic or "").strip()
    token = str(token or "").strip()
    if not topic:
        raise ConfigError("NTFY_APPROVAL_TOPIC is not set; run `hermes ntfy-approval setup`")
    if len(topic) > MAX_TOPIC_LENGTH or not all(c.isascii() and (c.isalnum() or c in "-_") for c in topic):
        raise ConfigError(f"NTFY_APPROVAL_TOPIC must be 1-{MAX_TOPIC_LENGTH} characters of "
                          "letters, digits, '-' and '_'")
    if not token and len(topic) < MIN_OPEN_TOPIC_LENGTH:
        raise ConfigError(f"without NTFY_APPROVAL_TOKEN the topic is the only secret, so it must be at "
                          f"least {MIN_OPEN_TOPIC_LENGTH} characters; run `hermes ntfy-approval setup`")
    if any(ord(c) < 33 or ord(c) > 126 for c in token):
        raise ConfigError("NTFY_APPROVAL_TOKEN contains characters a token cannot have")
    level = PRIORITIES.get(str(priority).strip().lower()) if priority is not None else None
    if level is None:
        raise ConfigError(f"priority must be one of {', '.join(PRIORITIES)}")
    if not isinstance(send_command, bool):
        raise ConfigError("send_command must be true or false")
    return Settings(server=normalize_server(server), topic=topic, token=token, priority=level,
                    send_command=send_command)


def offered_choices(request) -> tuple:
    allowed = set(getattr(request, "allowed_choices", ()) or ())
    return tuple(choice for choice, _label in BUTTONS if choice in allowed)


def reply_body(request_id: str, choice: str, token: str) -> str:
    return f"{REPLY_PREFIX} {request_id} {choice} {token}"


def match_reply(text: object, request_id: str, tokens: Dict[str, str]) -> Optional[str]:
    """The choice a reply message carries, if it is a valid answer to this request."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split(" ")
    if len(parts) != 5 or " ".join(parts[:2]) != REPLY_PREFIX:
        return None
    reply_id, choice, token = parts[2:]
    expected = tokens.get(choice)
    if expected is None:
        return None
    id_ok = hmac.compare_digest(reply_id.encode("utf-8"), request_id.encode("utf-8"))
    token_ok = hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))
    return choice if id_ok and token_ok else None


def _clip(text: str, limit: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit - 3].decode("utf-8", errors="ignore") + "…"


def _answer_window(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds} s"
    return f"{round(seconds / 60)} min"


def build_notification(request, settings: Settings, tokens: Dict[str, str], *, profile: str = "") -> dict:
    description = str(getattr(request, "description", "") or "").strip() or "Hermes flagged an action."
    parts = [_clip(description, MAX_DESCRIPTION_BYTES)]
    command = str(getattr(request, "command", "") or "").strip()
    if settings.send_command and command:
        parts.append(_clip(command, MAX_COMMAND_BYTES))
    parts.append(f"Answer within {_answer_window(request.timeout_seconds)}. No answer means deny.")
    title = "Hermes needs your approval"
    if profile and profile != "default":
        title += f" ({profile})"
    reply_url = f"{settings.server}/{settings.reply_topic}"
    actions = [
        {"action": "http", "label": label, "url": reply_url, "method": "POST",
         "body": reply_body(request.request_id, choice, tokens[choice]), "clear": True}
        for choice, label in BUTTONS if choice in tokens
    ]
    return {"topic": settings.topic, "title": title, "message": "\n\n".join(parts),
            "priority": settings.priority, "tags": ["warning"], "actions": actions}


class _ReplyWatch:
    """Reads the reply topic until a valid answer arrives or the deadline passes."""

    def __init__(self, client: NtfyClient, topic: str, deadline: float, clock: Callable[[], float],
                 sleep: Callable[[float], None]):
        self._client, self._topic, self._deadline = client, topic, deadline
        self._clock, self._sleep = clock, sleep
        self._events = None
        self._early: list = []  # events that arrived before ntfy's "open" event
        self._read_timeout = 0.0
        self.since: Optional[str] = None  # "<unix time>" at first, then the last reply message id

    def _remaining(self) -> float:
        return self._deadline - self._clock()

    def connect(self) -> None:
        """Open the stream and wait for ntfy's ``open`` event, so no later answer is missed."""
        self.close()
        self._read_timeout = max(0.1, min(READ_TIMEOUT, self._remaining()))
        self._events = self._client.subscribe(self._topic, since=self.since, timeout=self._read_timeout)
        for event in self._events:
            if event.get("event") == "open":
                return
            self._early.append(event)
        raise NtfyError("the ntfy stream closed before it opened")

    def _stream(self):
        while self._early:
            yield self._early.pop(0)
        yield from self._events

    def _note(self, event: dict) -> None:
        if event.get("event") == "message" and isinstance(event.get("id"), str):
            self.since = event["id"]

    def close(self) -> None:
        if self._events is not None:
            self._events.close()
            self._events = None
        self._early.clear()

    def wait(self, request_id: str, tokens: Dict[str, str]) -> Optional[str]:
        while self._remaining() > 0:
            try:
                if self._events is None:
                    self.connect()
                for event in self._stream():
                    self._note(event)
                    if event.get("event") == "message":
                        choice = match_reply(event.get("message"), request_id, tokens)
                        if choice is not None:
                            return choice
                    if self._remaining() < self._read_timeout:
                        break  # reconnect so a quiet stream cannot outlive the deadline (or stop past it)
                self.close()
            except NtfyError as exc:
                self.close()
                if exc.fatal:
                    raise
                logger.info("ntfy-approval: %s; reconnecting", exc)
                self._sleep(max(0.0, min(RETRY_DELAY, self._remaining())))
            except _TRANSIENT as exc:
                self.close()
                if isinstance(exc, TimeoutError):
                    continue
                logger.info("ntfy-approval: lost the ntfy stream (%s); reconnecting", type(exc).__name__)
                self._sleep(max(0.0, min(RETRY_DELAY, self._remaining())))
        return None


def present(request, settings: Settings, *, profile: str = "", client: Optional[NtfyClient] = None,
            clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
    """Send ``request`` to the phone and return ``request.respond(choice)`` for the tapped button.

    Raises when there is no answer before ``request.timeout_seconds`` or ntfy cannot be used;
    Hermes turns every raised error into a denial."""
    client = client or NtfyClient(settings.server, settings.token)
    deadline = clock() + float(request.timeout_seconds)
    tokens = {choice: secrets.token_urlsafe(24) for choice in offered_choices(request)}
    if not tokens:
        raise ConfigError("the request offers no choice this transport can show")
    watch = _ReplyWatch(client, settings.reply_topic, deadline, clock, sleep)
    published = None
    choice = None
    try:
        watch.connect()
        published = client.publish(build_notification(request, settings, tokens, profile=profile),
                                   timeout=max(0.1, min(PUBLISH_TIMEOUT, deadline - clock())))
        if watch.since is None:
            watch.since = str(published["time"])
        choice = watch.wait(request.request_id, tokens)
    finally:
        watch.close()
        if published is not None:
            _withdraw(client, settings.topic, published["id"],
                      WITHDRAW_TIMEOUT if choice is None else min(WITHDRAW_TIMEOUT, deadline - clock() - 1.0))
    if choice is None:
        raise TimeoutError("no answer from ntfy before the approval timed out")
    return request.respond(choice)


def _withdraw(client: NtfyClient, topic: str, message_id: str, timeout: float) -> None:
    """Remove the notification from every device; best effort (needs ntfy 2.16+)."""
    if timeout < 0.5:
        return
    try:
        client.delete(topic, message_id, timeout=timeout)
    except (NtfyError, *_TRANSIENT) as exc:
        logger.debug("ntfy-approval: could not withdraw the notification (%s)", exc)
