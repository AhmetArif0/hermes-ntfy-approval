"""A small ntfy client on the standard library: publish, subscribe to a JSON stream, delete.

Redirects are never followed (an ``Authorization`` header must not travel to another host), and
every response is read with a size cap.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Iterator, Optional

MAX_RESPONSE_BYTES = 64 * 1024
USER_AGENT = "hermes-ntfy-approval"


class NtfyError(Exception):
    """A request the server answered with an error, or a response that is not ntfy's."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status

    @property
    def fatal(self) -> bool:
        """Retrying cannot help: bad credentials, no access, or no such server/topic."""
        return self.status in (400, 401, 403, 404)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError for the 3xx response


_OPENER = urllib.request.build_opener(_NoRedirect)


class NtfyClient:
    def __init__(self, server: str, token: str = ""):
        self.server = server.rstrip("/")
        self._token = token

    def _headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"User-Agent": USER_AGENT}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        headers.update(extra or {})
        return headers

    def _open(self, method: str, url: str, *, body: Optional[bytes] = None, timeout: float,
              headers: Optional[dict] = None):
        request = urllib.request.Request(url, data=body, method=method, headers=self._headers(headers))
        try:
            return _OPENER.open(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            if 300 <= status < 400:
                raise NtfyError(f"the ntfy server answered with a redirect (HTTP {status}); "
                                "set the server URL to the address it redirects to", status) from None
            raise NtfyError(f"the ntfy server answered HTTP {status}", status) from None

    def publish(self, message: dict, *, timeout: float = 10.0) -> dict:
        """Publish a JSON message (``POST /``); returns the stored message (``id``, ``time``)."""
        body = json.dumps(message, ensure_ascii=False).encode("utf-8")
        with self._open("POST", self.server + "/", body=body, timeout=timeout,
                        headers={"Content-Type": "application/json"}) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
        return _parse_message(data)

    def delete(self, topic: str, message_id: str, *, timeout: float = 5.0) -> None:
        """Withdraw a delivered notification from every subscribed device (ntfy 2.16+)."""
        with self._open("DELETE", f"{self.server}/{topic}/{message_id}", timeout=timeout) as response:
            response.read(MAX_RESPONSE_BYTES)

    def subscribe(self, topic: str, *, since: Optional[str], timeout: float) -> Iterator[dict]:
        """Yield the events of ``GET /<topic>/json``: an ``open`` event, cached messages from
        ``since`` on, then live messages and keepalives. ``timeout`` bounds the connect and each
        read; a quiet connection raises ``TimeoutError``."""
        url = f"{self.server}/{topic}/json"
        if since:
            url += "?since=" + since
        with self._open("GET", url, timeout=timeout) as response:
            while True:
                line = response.readline(MAX_RESPONSE_BYTES + 1)
                if not line:
                    return
                if len(line) > MAX_RESPONSE_BYTES:
                    raise NtfyError("the ntfy server sent an oversized event")
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    raise NtfyError("the ntfy server sent something that is not JSON") from None
                if isinstance(event, dict):
                    yield event


def _parse_message(data: bytes) -> dict:
    if len(data) > MAX_RESPONSE_BYTES:
        raise NtfyError("the ntfy server sent an oversized response")
    try:
        message = json.loads(data)
    except ValueError:
        raise NtfyError("the ntfy server answered with something that is not JSON") from None
    if not isinstance(message, dict) or not isinstance(message.get("id"), str):
        raise NtfyError("the ntfy server answered without a message id")
    if not isinstance(message.get("time"), int):
        message["time"] = int(time.time())
    return message
