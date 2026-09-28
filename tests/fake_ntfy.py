"""A small in-process ntfy server with the parts of the API the plugin uses, plus fault switches.

Semantics follow ntfy 2.28 as observed on a real server: JSON publish on ``POST /``, raw publish
on ``POST /<topic>``, ``GET /<topic>/json`` sends ``open`` first, then cached messages from
``since`` (a unix time is inclusive, a message id exclusive), then live events and keepalives;
``DELETE /<topic>/<id>`` publishes a ``message_delete`` event. The same tests run against a real
server when NTFY_TEST_SERVER is set (see conftest.py).
"""

from __future__ import annotations

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class FakeNtfy:
    def __init__(self, *, keepalive: float = 1.0, token: str = "", redirect_to: str = ""):
        self.keepalive = keepalive
        self.token = token  # when set: everything needs it, except anonymous writes to "*-reply"
        self.redirect_to = redirect_to
        self.events: list = []
        self.requests: list = []  # (method, path, headers dict)
        self.refuse_streams_until = 0.0
        self.open_after_backlog = False  # send cached messages before the "open" event
        self._cond = threading.Condition()
        self._generation = 0
        self._stopping = False
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(self))
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self) -> "FakeNtfy":
        self._thread.start()
        return self

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify_all()
        self.httpd.shutdown()
        self.httpd.server_close()

    def add(self, event: dict) -> dict:
        event = {"id": secrets.token_urlsafe(9)[:12], "time": int(time.time()), **event}
        with self._cond:
            self.events.append(event)
            self._cond.notify_all()
        return event

    def drop_streams(self) -> None:
        """End every open subscription, as a proxy or server restart would."""
        with self._cond:
            self._generation += 1
            self._cond.notify_all()

    def topic_events(self, topic: str) -> list:
        with self._cond:
            return [e for e in self.events if e["topic"] == topic]


def _since_filter(events: list, since: str) -> list:
    if not since or since == "none":
        return []
    if since == "all":
        return events
    if since.isdigit():
        return [e for e in events if e["time"] >= int(since)]
    ids = [e["id"] for e in events]
    return events[ids.index(since) + 1:] if since in ids else events


def _handler(server: FakeNtfy):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def log_message(self, *_args):
            pass

        def _record(self) -> None:
            server.requests.append((self.command, self.path, dict(self.headers.items())))

        def _json(self, status: int, payload: dict) -> None:
            body = (json.dumps(payload) + "\n").encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _body(self) -> bytes:
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _denied(self, topic: str, write: bool) -> bool:
            if server.redirect_to:
                self.send_response(302)
                self.send_header("Location", server.redirect_to + self.path)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return True
            if not server.token or (write and topic.endswith("-reply")):
                return False
            auth = self.headers.get("Authorization")
            if auth is None:
                self._json(403, {"code": 40301, "http": 403, "error": "forbidden"})
                return True
            if auth != f"Bearer {server.token}":
                self._json(401, {"code": 40101, "http": 401, "error": "unauthorized"})
                return True
            return False

        def do_POST(self):
            self._record()
            path = urlsplit(self.path).path
            data = self._body()
            if path == "/":
                message = json.loads(data)
                topic = message.pop("topic")
                if self._denied(topic, write=True):
                    return
                for action in message.get("actions", []):
                    action["id"] = secrets.token_urlsafe(7)[:10]
                self._json(200, server.add({"event": "message", "topic": topic, **message}))
                return
            topic = path.strip("/")
            if self._denied(topic, write=True):
                return
            self._json(200, server.add({"event": "message", "topic": topic, "message": data.decode()}))

        def do_DELETE(self):
            self._record()
            topic, _, message_id = urlsplit(self.path).path.strip("/").partition("/")
            if self._denied(topic, write=True):
                return
            self._json(200, server.add({"event": "message_delete", "topic": topic, "sequence_id": message_id}))

        def do_GET(self):
            self._record()
            parts = urlsplit(self.path)
            topic, _, kind = parts.path.strip("/").partition("/")
            if kind != "json":
                self._json(404, {"http": 404, "error": "not found"})
                return
            if self._denied(topic, write=False):
                return
            if time.time() < server.refuse_streams_until:
                self._json(503, {"http": 503, "error": "unavailable"})
                return
            query = parse_qs(parts.query)
            since = query.get("since", [""])[0]
            poll = query.get("poll", [""])[0] in ("1", "true")
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.end_headers()
            with server._cond:
                generation = server._generation
                sent = len(server.events)
                backlog = _since_filter([e for e in server.events if e["topic"] == topic], since or ("all" if poll else ""))
            try:
                opening = [] if poll else [{"id": "open", "time": int(time.time()), "event": "open", "topic": topic}]
                for event in (backlog + opening if server.open_after_backlog else opening + backlog):
                    self._send(event)
                if poll:
                    return
                while True:
                    with server._cond:
                        server._cond.wait_for(
                            lambda: len(server.events) > sent or server._generation != generation
                            or server._stopping, timeout=server.keepalive)
                        if server._generation != generation or server._stopping:
                            return
                        fresh = [e for e in server.events[sent:] if e["topic"] == topic]
                        sent = len(server.events)
                    if not fresh:
                        fresh = [{"id": "keepalive", "time": int(time.time()), "event": "keepalive", "topic": topic}]
                    for event in fresh:
                        self._send(event)
            except (BrokenPipeError, ConnectionResetError):
                return

        def _send(self, event: dict) -> None:
            self.wfile.write((json.dumps(event) + "\n").encode())
            self.wfile.flush()

    return Handler
