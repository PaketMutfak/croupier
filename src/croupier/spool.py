"""Sentry transport that spools envelopes to disk until Sentry confirms them.

Branch computers lose ethernet and power. The stock transport keeps events in
memory and drops them on a failed send, so anything captured during an outage
never reaches Sentry. This transport writes every envelope to disk *before*
sending it and deletes the file only once Sentry has answered definitively.
Leftovers are replayed at startup and periodically while the process runs.

Depends on private ``sentry-sdk`` internals: ``_worker``, ``_send_envelope``,
``_flush_client_reports`` and ``_request``. ``tests/test_spool.py`` fails when a
``sentry-sdk`` upgrade changes them. See ``docs/adr/0002``.
"""

import hashlib
import io
import logging
import os
import threading
import time
import uuid
from typing import TYPE_CHECKING
from typing import Any
from typing import ClassVar

from sentry_sdk.envelope import Envelope
from sentry_sdk.transport import HttpTransport
from sentry_sdk.utils import capture_internal_exceptions

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_SUFFIX = ".envelope"
_TEMP_SUFFIX = ".tmp"
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 299
_PERMANENT_REJECTIONS = frozenset({400, 401, 403, 413})
DEFAULT_MAX_FILES = 500
DEFAULT_REPLAY_INTERVAL = 60.0


def _is_retryable(status: int | None) -> bool:
    """Whether an envelope should stay on disk after a send attempt.

    ``None`` means no HTTP response arrived (dead link, or the SDK skipped the
    send while rate limited). Only a 2xx or a permanent rejection (400, 401,
    403, 413) is definitive; anything else, such as 408, 429 or 5xx, may be
    transient, so the file stays and the spool cap bounds the backlog.
    """
    if status is None:
        return True
    delivered = _HTTP_OK_MIN <= status <= _HTTP_OK_MAX
    return not (delivered or status in _PERMANENT_REJECTIONS)


def _is_client_report(envelope: Envelope) -> bool:
    return bool(envelope.items) and all(
        item.type == "client_report" for item in envelope.items
    )


class SpoolTransport(HttpTransport):
    spool_dir: ClassVar[Path]
    max_files: ClassVar[int] = DEFAULT_MAX_FILES
    replay_interval: ClassVar[float] = DEFAULT_REPLAY_INTERVAL

    def __init__(self, options: dict[str, Any]) -> None:
        super().__init__(options)
        self._in_flight: set[Path] = set()
        self._lock = threading.Lock()
        self._last_status: int | None = None
        self._last_ns = 0
        self._stopped = threading.Event()
        dsn_hash = hashlib.sha256(str(options["dsn"]).encode()).hexdigest()[:12]
        self._dir = self.spool_dir / dsn_hash
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            for leftover in self._dir.glob(f"*{_TEMP_SUFFIX}"):
                leftover.unlink(missing_ok=True)
            prefixes = (
                path.name.split("-", 1)[0] for path in self._dir.glob(f"*{_SUFFIX}")
            )
            self._last_ns = max((int(p) for p in prefixes if p.isdigit()), default=0)
        except OSError:
            # Observability must never stop the receipt printer from starting:
            # degrade to the stock in-memory transport.
            logger.warning(
                "Sentry spool %s is unusable; events will not survive outages",
                self._dir,
                exc_info=True,
            )
            self._spooling = False
            return
        self._spooling = True
        self.replay()
        threading.Thread(
            target=self._replay_forever, name="croupier-spool-replay", daemon=True
        ).start()

    def capture_envelope(self, envelope: Envelope) -> None:
        # Client reports describe losses the network itself caused; spooling
        # them would only echo an outage back at Sentry.
        if not self._spooling or _is_client_report(envelope):
            super().capture_envelope(envelope)
            return
        try:
            path = self._write(envelope)
        except OSError:
            # Disk full or unwritable: still try an ordinary in-memory send
            # rather than raise into the caller of ``capture_event``.
            logger.warning("Cannot spool envelope; sending without it", exc_info=True)
            super().capture_envelope(envelope)
            return
        self._evict()
        self._submit(path, envelope)

    def replay(self) -> None:
        """Re-submit spooled envelopes that are not already being sent."""
        for path in sorted(self._dir.glob(f"*{_SUFFIX}")):
            if self._worker.full():
                return
            if not self._claim(path):
                continue
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                # Delivered and deleted between the directory scan and now.
                self._release(path)
                continue
            except OSError:
                # Locked (antivirus, another handle) or a transient I/O error:
                # the file may be fine, so keep it for the next replay.
                logger.warning("Cannot read spooled envelope %s", path, exc_info=True)
                self._release(path)
                continue
            try:
                envelope = Envelope.deserialize_from(io.BytesIO(raw))
            except Exception:  # noqa: BLE001
                # A power cut can leave a truncated file; it is unrecoverable.
                logger.warning("Dropping unreadable spooled envelope %s", path)
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    # Read-only or locked: keep it and retry next replay
                    # rather than stop the service from starting.
                    logger.warning("Cannot delete %s", path, exc_info=True)
                self._release(path)
                continue
            self._submit(path, envelope)

    def kill(self) -> None:
        self._stopped.set()
        super().kill()

    def _request(self, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        response = super()._request(*args, **kwargs)
        self._last_status = response.status
        return response

    def _claim(self, path: Path) -> bool:
        """Mark ``path`` as being sent; False if another sender holds it."""
        with self._lock:
            if path in self._in_flight:
                return False
            self._in_flight.add(path)
            return True

    def _release(self, path: Path) -> None:
        with self._lock:
            self._in_flight.discard(path)

    def _submit(self, path: Path, envelope: Envelope) -> None:
        """Queue a claimed envelope; the claim is released once it is handled."""

        def deliver() -> None:
            try:
                with capture_internal_exceptions():
                    self._deliver(path, envelope)
                    self._flush_client_reports()
            finally:
                self._release(path)

        if not self._worker.submit(deliver):
            # Not lost: the file stays and a later replay picks it up.
            self._release(path)

    def _deliver(self, path: Path, envelope: Envelope) -> None:
        self._last_status = None
        # Raises on a dead link, which leaves the file in place.
        self._send_envelope(envelope)
        # A crash between a successful send and this delete replays a duplicate
        # after restart; Sentry dedups by event ID, so that is accepted.
        if not _is_retryable(self._last_status):
            path.unlink(missing_ok=True)

    def _write(self, envelope: Envelope) -> Path:
        # Names must sort by capture order even if the wall clock jumps back
        # (a branch PC that lost power may boot with a reset clock), or the
        # newest events would look oldest and be evicted first.
        with self._lock:
            self._last_ns = max(time.time_ns(), self._last_ns + 1)
            ns = self._last_ns
        name = f"{ns:020d}-{uuid.uuid4().hex}"
        final = self._dir / f"{name}{_SUFFIX}"
        temp = self._dir / f"{name}{_TEMP_SUFFIX}"
        try:
            with temp.open("wb") as handle:
                handle.write(envelope.serialize())
                handle.flush()
                os.fsync(handle.fileno())
            # Claim before the rename makes the file visible, so a concurrent
            # replay can never pick it up ahead of this capture.
            self._claim(final)
            temp.replace(final)
            self._fsync_dir()
        except OSError:
            self._release(final)
            temp.unlink(missing_ok=True)
            raise
        return final

    def _fsync_dir(self) -> None:
        # The rename is only durable once the directory entry is flushed.
        # Windows cannot open a directory, and NTFS journals the rename.
        if os.name == "nt":
            return
        fd = os.open(self._dir, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _evict(self) -> None:
        files = sorted(self._dir.glob(f"*{_SUFFIX}"))
        # May evict a file that is mid-send: the send still completes and the
        # later delete is a no-op.
        for stale in files[: max(0, len(files) - self.max_files)]:
            logger.warning("Spool full, evicting oldest envelope %s", stale)
            stale.unlink(missing_ok=True)

    def _replay_forever(self) -> None:
        while not self._stopped.wait(self.replay_interval):
            with capture_internal_exceptions():
                self.replay()


def make_spool_transport(
    spool_dir: Path,
    *,
    max_files: int = DEFAULT_MAX_FILES,
    replay_interval: float = DEFAULT_REPLAY_INTERVAL,
) -> type[SpoolTransport]:
    """Build a transport class for ``sentry_sdk.init(transport=...)``.

    The spool lives in a per-DSN subdirectory of ``spool_dir`` so instances
    pointed at different Sentry projects never replay each other's events.
    """

    class ConfiguredSpoolTransport(SpoolTransport):
        pass

    ConfiguredSpoolTransport.spool_dir = spool_dir
    ConfiguredSpoolTransport.max_files = max_files
    ConfiguredSpoolTransport.replay_interval = replay_interval
    return ConfiguredSpoolTransport
