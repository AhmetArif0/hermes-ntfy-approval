"""ntfy-approval: answer Hermes' approval prompts from your phone through ntfy.

``register`` registers one approval transport (``ntfy``), one CLI command
(``hermes ntfy-approval``) and one observer hook (``post_approval_response``, to withdraw a
notification Hermes stopped waiting for). The transport does nothing until the user selects it
with ``security.approval.transport: ntfy``. See README.md and docs/DESIGN.md.
"""

from __future__ import annotations

import logging
import os

from .transport import ConfigError, make_settings, normalize_server, present as _present_request, stop_waiting

logger = logging.getLogger(__name__)

TRANSPORT_NAME = "ntfy"
TOPIC_ENV = "NTFY_APPROVAL_TOPIC"
TOKEN_ENV = "NTFY_APPROVAL_TOKEN"


class ProfileMismatch(Exception):
    """The request reached this profile's transport outside this profile's context."""


def _home() -> str:
    from hermes_constants import get_hermes_home

    return os.path.realpath(str(get_hermes_home()))


def read_secret(name: str) -> str:
    """A ``.env`` value for the active profile. Under a multi-profile gateway Hermes' secret scope
    answers (and refuses when no profile is in scope); otherwise it is the process environment."""
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return (os.environ.get(name) or "").strip()
    return (get_secret(name) or "").strip()


def load_settings(ctx):
    return make_settings(
        server=ctx.get_config("server"),
        topic=read_secret(TOPIC_ENV),
        token=read_secret(TOKEN_ENV),
        priority=ctx.get_config("priority", "high"),
        send_command=ctx.get_config("send_command", True),
    )


def make_present(ctx, home: str, profile: str):
    def present(request):
        try:
            if _home() != home:
                # Hermes runs transports on a worker thread; until it carries the routed profile
                # into that thread (hermes-agent #114580) a request can arrive under the launch
                # profile's home. Refuse rather than use another profile's settings.
                raise ProfileMismatch("the approval arrived outside its profile; refusing it")
            return _present_request(request, load_settings(ctx), profile=profile)
        except TimeoutError as exc:
            logger.info("ntfy-approval: %s", exc)
            raise
        except ConfigError as exc:
            logger.warning("ntfy-approval: not configured: %s", exc)
            raise
        except Exception as exc:
            logger.warning("ntfy-approval: could not ask through ntfy: %s", exc)
            raise

    return present


def on_approval_answered(request_id=None, **_kwargs) -> None:
    """``post_approval_response``: Hermes has its answer or gave up (timeout, /stop, Ctrl-C).
    Withdraw that request's notification if it is still on the phone. Never raises."""
    try:
        stop_waiting(request_id)
    except Exception:
        logger.debug("ntfy-approval: could not withdraw after the approval ended", exc_info=True)


def register(ctx) -> None:
    home = _home()
    ctx.register_approval_transport(TRANSPORT_NAME, make_present(ctx, home, ctx.profile_name))
    ctx.register_hook("post_approval_response", on_approval_answered)

    from . import cli

    ctx.register_cli_command(
        name="ntfy-approval", help="Answer Hermes approval prompts from your phone (ntfy)",
        setup_fn=cli.setup_parser,
        handler_fn=lambda args: cli.run(args, lambda: load_settings(ctx), ctx.profile_name,
                                        lambda: normalize_server(ctx.get_config("server"))),
        description="Set up and test phone approvals through ntfy. Start with: hermes ntfy-approval setup",
    )
