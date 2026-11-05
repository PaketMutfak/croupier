"""Child process for the kill-and-recover spool test (not collected by pytest).

``python _spool_child.py <spool_dir> <dead|alive>``: starts Sentry with the
spool transport over a fake network, captures one event, then either exits
without any cleanup (``dead``: the network is down) or waits for delivery and
prints how many HTTP requests were made (``alive``).
"""

import os
import sys
from pathlib import Path

import sentry_sdk
from sentry_sdk.transport import HttpTransport

from croupier.spool import make_spool_transport

requests = 0
network_up = sys.argv[2] == "alive"


class _Response:
    status = 200
    headers: dict[str, str] = {}  # noqa: RUF012
    data = b""

    def close(self) -> None:
        pass


def _fake_request(_self: object, *_args: object, **_kwargs: object) -> _Response:
    global requests  # noqa: PLW0603
    requests += 1
    if not network_up:
        msg = "network is unreachable"
        raise OSError(msg)
    return _Response()


HttpTransport._request = _fake_request  # type: ignore[assignment]
sentry_sdk.init(
    dsn="https://public@example.com/1",
    transport=make_spool_transport(Path(sys.argv[1]), replay_interval=3600),
    default_integrations=False,
    auto_enabling_integrations=False,
)
if network_up:
    sentry_sdk.get_client().flush(timeout=5)
    sys.stdout.write(f"{requests}\n")
else:
    sentry_sdk.capture_message("lost to a power cut")
    # Like a power cut: no flush, no atexit, no cleanup.
    os._exit(1)
