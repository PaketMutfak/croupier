"""Sentry transport that spools envelopes to disk until Sentry confirms them.

Branch computers lose ethernet and power. The stock transport keeps events in
memory and drops them on a failed send, so anything captured during an outage
never reaches Sentry. This transport writes every envelope to disk *before*
sending it and deletes the file only once Sentry has answered definitively.
Leftovers are replayed at startup and periodically while the process runs.

Depends on private ``sentry-sdk`` internals: ``_worker``, ``_send_envelope``,
``_flush_client_reports``, ``_check_disabled`` and ``_request``.
``tests/test_spool.py`` fails when a ``sentry-sdk`` upgrade changes them. See
``docs/adr/0002``.
"""

import contextlib
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
from sentry_sdk.utils import Dsn
from sentry_sdk.utils import capture_internal_exceptions

if TYPE_CHECKING:
    from pathlib import Path

    from sentry_sdk._types import EventDataCategory
    from sentry_sdk.envelope import Item

type _Loss = tuple[str, EventDataCategory | None, Item | None, int]

logger = logging.getLogger(__name__)

_SUFFIX = ".envelope"
_TEMP_SUFFIX = ".tmp"
_HTTP_OK_MIN = 200
_HTTP_OK_MAX = 299
_PERMANENT_REJECTIONS = frozenset({400, 401, 403, 413})
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_FILE_MODE = 0o600
# Windows ``os.open`` defaults to text mode, which would rewrite newlines.
_O_BINARY = getattr(os, "O_BINARY", 0)
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


def _fsync_directory(path: Path) -> None:
    """Flush ``path``'s entries, so a rename or mkdir inside it survives a power cut.

    Windows cannot open a directory, and NTFS commits a rename lazily: a power
    cut can lose it and leave only the fsynced ``.tmp`` file, which startup
    recovers (``_recover_temp``).
    """
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def project_spool_dir(spool_dir: Path, dsn: str) -> Path:
    """The subdirectory of ``spool_dir`` for the Sentry project ``dsn`` points at.

    Keyed by the project's endpoint, not the full DSN: rotating the client key
    keeps the backlog, which is then sent with the new key to the same project.
    Instances pointed at different projects never replay each other's events.
    """
    parsed = Dsn(dsn)
    project = f"{parsed.scheme}://{parsed.netloc}{parsed.path}{parsed.project_id}"
    return spool_dir / hashlib.sha256(project.encode()).hexdigest()[:12]


def _recover_temp(path: Path) -> None:
    """Promote a complete leftover ``.tmp`` file, or delete a partial one.

    The temp file is fsynced before its rename, so a leftover that parses
    is a whole envelope whose rename was lost (Windows) or never ran. A
    crash before that fsync leaves a partial file, which is deleted.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        # Locked by antivirus: one stale file must not disable the spool;
        # the next start retries it.
        logger.warning("Cannot read %s", path, exc_info=True)
        return
    try:
        complete = bool(Envelope.deserialize_from(io.BytesIO(raw)).items)
    except Exception:  # noqa: BLE001
        complete = False
    try:
        if complete:
            # Same name, so it keeps its place in capture order.
            path.replace(path.with_suffix(_SUFFIX))
        else:
            path.unlink(missing_ok=True)
    except OSError:
        logger.warning("Cannot recover %s", path, exc_info=True)


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
        # Envelopes Sentry already answered for good whose file could not be
        # deleted (locked by antivirus, read-only): replays retry the delete
        # and never send them again.
        self._answered: set[Path] = set()
        self._evict_lock = threading.Lock()
        self._lock = threading.Lock()
        self._last_status: int | None = None
        self._last_ns = 0
        self._stopped = threading.Event()
        # Losses the SDK records while a spooled envelope is being sent; they
        # are kept per thread so captures on other threads report as usual.
        self._held_losses = threading.local()
        self._dir = project_spool_dir(self.spool_dir, str(options["dsn"]))
        try:
            missing = [p for p in (self._dir, *self._dir.parents) if not p.exists()]
            # Envelopes carry exception payloads and tags: keep every level
            # created here private to the service account. ``mkdir(parents=True)``
            # would apply ``mode`` to the last level only.
            for created in reversed(missing):
                created.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
            # Persist only the levels ``mkdir`` created: a pre-existing
            # ancestor may be search-only (``/home`` at 0711), and opening it
            # to fsync would fail and disable the spool for no gain.
            for created in missing:
                _fsync_directory(created.parent)
            for leftover in self._dir.glob(f"*{_TEMP_SUFFIX}"):
                _recover_temp(leftover)
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
        paths = sorted(self._dir.glob(f"*{_SUFFIX}"))
        with self._lock:
            # Forget files that are gone, evicted or deleted by hand.
            self._answered.intersection_update(paths)
            answered = set(self._answered)
        for path in paths:
            if path in answered:
                self._forget(path)
                continue
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

    def record_lost_event(
        self,
        reason: str,
        data_category: EventDataCategory | None = None,
        item: Item | None = None,
        *,
        quantity: int = 1,
    ) -> None:
        held: list[_Loss] | None = getattr(self._held_losses, "losses", None)
        if held is None:
            super().record_lost_event(reason, data_category, item, quantity=quantity)
        else:
            held.append((reason, data_category, item, quantity))

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
        # ``_send_envelope`` silently drops items in a rate-limited category, so
        # a 2xx would only confirm the reduced envelope. Keep the file; once
        # the limit lapses the replay sends the rest (Sentry dedups the items
        # that already went through).
        partial = any(
            self._check_disabled(item.data_category) for item in envelope.items
        )
        # The SDK reports every failed attempt as a lost event, but a kept file
        # is retried, so those losses are held back and reported only if the
        # envelope is dropped. Otherwise each replay during an outage would
        # count the same events as lost again in Sentry's client reports.
        held: list[_Loss] = []
        self._held_losses.losses = held
        try:
            # Raises on a dead link, which leaves the file in place.
            self._send_envelope(envelope)
        finally:
            self._held_losses.losses = None
        # A crash between a successful send and this delete, or a restart while
        # the file is still locked, replays a duplicate once; Sentry dedups by
        # event ID, so that is accepted.
        if not partial and not _is_retryable(self._last_status):
            for reason, data_category, item, quantity in held:
                self.record_lost_event(reason, data_category, item, quantity=quantity)
            self._forget(path)

    def _forget(self, path: Path) -> None:
        """Delete an envelope Sentry has answered for good."""
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Cannot delete answered envelope %s", path, exc_info=True)
            with self._lock:
                self._answered.add(path)
            return
        with self._lock:
            self._answered.discard(path)

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
            fd = os.open(
                temp,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_BINARY,
                _PRIVATE_FILE_MODE,
            )
            with open(fd, "wb") as handle:  # noqa: PTH123
                handle.write(envelope.serialize())
                handle.flush()
                os.fsync(handle.fileno())
            # Claim before the rename makes the file visible, so a concurrent
            # replay can never pick it up ahead of this capture.
            self._claim(final)
            temp.replace(final)
            self._fsync_dir()
        except OSError:
            # The caller falls back to an in-memory send, so a file that was
            # already renamed must go too, or the replay would send it again.
            # A cleanup failure must neither hide the original error nor keep
            # the claim: a file left behind is then replayed, a duplicate
            # Sentry dedups, instead of being skipped until a restart.
            for leftover in (temp, final):
                with contextlib.suppress(OSError):
                    leftover.unlink(missing_ok=True)
            self._release(final)
            raise
        return final

    def _fsync_dir(self) -> None:
        _fsync_directory(self._dir)

    def _evict(self) -> None:
        # One eviction at a time, or concurrent captures would each count the
        # same excess and evict twice as many files as needed.
        with self._evict_lock:
            files = sorted(self._dir.glob(f"*{_SUFFIX}"))
            excess = len(files) - self.max_files
            for stale in files:
                if excess <= 0:
                    return
                # Claim before reading or deleting, so a replay cannot start
                # sending the file meanwhile. A file already being sent is
                # skipped: its send decides whether it is lost. The cap is
                # soft by at most the worker's queue size.
                if not self._claim(stale):
                    continue
                excess -= 1
                try:
                    with self._lock:
                        answered = stale in self._answered
                    if answered:
                        # Already accepted or rejected by Sentry: nothing lost.
                        self._forget(stale)
                    else:
                        self._drop_evicted(stale)
                finally:
                    self._release(stale)

    def _drop_evicted(self, path: Path) -> None:
        logger.warning("Spool full, evicting oldest envelope %s", path)
        try:
            raw: bytes | None = path.read_bytes()
        except OSError:
            raw = None
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # Locked or read-only: the cap is soft, and the capture that
            # triggered this must still be submitted.
            logger.warning("Cannot evict %s", path, exc_info=True)
            return
        if raw is None:
            return
        try:
            envelope = Envelope.deserialize_from(io.BytesIO(raw))
        except Exception:  # noqa: BLE001
            # A truncated file holds no event that could still be delivered.
            return
        # Overflow is a real loss; report it so the client reports show it.
        for item in envelope.items:
            self.record_lost_event("queue_overflow", item=item)

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

    The spool lives in a per-project subdirectory of ``spool_dir`` so instances
    pointed at different Sentry projects never replay each other's events.
    """

    class ConfiguredSpoolTransport(SpoolTransport):
        pass

    ConfiguredSpoolTransport.spool_dir = spool_dir
    ConfiguredSpoolTransport.max_files = max_files
    ConfiguredSpoolTransport.replay_interval = replay_interval
    return ConfiguredSpoolTransport
