import subprocess  # noqa: S404
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
import sentry_sdk
from sentry_sdk.client import NonRecordingClient
from sentry_sdk.envelope import Envelope
from sentry_sdk.envelope import Item
from sentry_sdk.envelope import PayloadRef
from sentry_sdk.transport import HttpTransport

from croupier.spool import SpoolTransport
from croupier.spool import make_spool_transport

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

DSN = "https://public@example.com/1"
OTHER_DSN = "https://public@example.com/2"


class _Response:
    def __init__(self, status: int) -> None:
        self.status = status
        self.headers: dict[str, str] = {}
        self.data = b""

    def close(self) -> None:
        pass


class _Network:
    """Stands in for the HTTP layer; ``status=None`` simulates a dead link."""

    def __init__(self) -> None:
        self.status: int | None = 200
        self.requests = 0
        self.on_request: Callable[[], None] | None = None

    def request(self, *_args: object, **_kwargs: object) -> _Response:
        self.requests += 1
        if self.on_request is not None:
            self.on_request()
        if self.status is None:
            msg = "network is unreachable"
            raise OSError(msg)
        return _Response(self.status)


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> _Network:
    fake = _Network()
    monkeypatch.setattr(
        HttpTransport,
        "_request",
        lambda _self, *args, **kwargs: fake.request(*args, **kwargs),
    )
    return fake


@pytest.fixture
def spool_dir(tmp_path: Path) -> Path:
    return tmp_path / "spool"


@pytest.fixture
def start(
    spool_dir: Path, network: _Network
) -> Iterator[Callable[..., SpoolTransport]]:
    _ = network
    started: list[SpoolTransport] = []

    def _start(
        *, dsn: str = DSN, max_files: int = 100, directory: Path | None = None
    ) -> SpoolTransport:
        transport_cls = make_spool_transport(
            directory or spool_dir, max_files=max_files, replay_interval=3600
        )
        sentry_sdk.init(
            dsn=dsn,
            transport=transport_cls,
            default_integrations=False,
            auto_enabling_integrations=False,
        )
        transport = sentry_sdk.get_client().transport
        assert isinstance(transport, SpoolTransport)
        started.append(transport)
        return transport

    yield _start
    for transport in started:
        transport.kill()
    sentry_sdk.get_global_scope().set_client(NonRecordingClient())


def _spooled(spool_dir: Path) -> list[Path]:
    return sorted(spool_dir.rglob("*.envelope"))


def _capture(transport: SpoolTransport, text: str = "boom") -> None:
    sentry_sdk.capture_message(text)
    transport.flush(timeout=5)


class TestSpooling:
    def test_delivered_event_leaves_nothing_on_disk(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        _capture(start())

        assert network.requests == 1
        assert _spooled(spool_dir) == []

    def test_event_is_on_disk_before_the_send_attempt(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        seen: list[int] = []
        network.on_request = lambda: seen.append(len(_spooled(spool_dir)))

        _capture(start())

        assert seen == [1]

    def test_network_failure_keeps_the_event(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None

        _capture(start())

        assert len(_spooled(spool_dir)) == 1

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503])
    def test_transient_http_status_keeps_the_event(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        status: int,
    ) -> None:
        network.status = status

        _capture(start())

        assert len(_spooled(spool_dir)) == 1

    @pytest.mark.parametrize("status", [400, 401, 403, 413])
    def test_permanent_http_status_drops_the_event(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        status: int,
    ) -> None:
        network.status = status

        _capture(start())

        assert _spooled(spool_dir) == []

    def test_client_reports_are_not_spooled(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        transport = start()
        report = Item(PayloadRef(json={"discarded_events": []}), type="client_report")

        transport.capture_envelope(Envelope(items=[report]))
        transport.flush(timeout=5)

        assert _spooled(spool_dir) == []


class TestReplay:
    def test_replay_delivers_spooled_events_once_network_returns(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        transport = start()
        _capture(transport)
        network.status = 200
        before = network.requests

        transport.replay()
        transport.flush(timeout=5)

        # The SDK may also flush a client report about the earlier loss.
        assert network.requests > before
        assert _spooled(spool_dir) == []

    def test_replay_keeps_events_while_network_is_still_down(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        transport = start()
        _capture(transport)

        transport.replay()
        transport.flush(timeout=5)

        assert len(_spooled(spool_dir)) == 1

    def test_events_left_by_a_previous_run_are_sent_on_startup(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        first = start()
        _capture(first)
        first.kill()
        network.status = 200
        before = network.requests

        second = start()
        second.flush(timeout=5)

        assert network.requests > before
        assert _spooled(spool_dir) == []

    def test_corrupt_files_are_discarded(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        transport = start()
        _capture(transport)
        (corrupt,) = _spooled(spool_dir)
        corrupt.write_bytes(b"\x00 truncated by a power cut")
        network.status = 200

        transport.replay()
        transport.flush(timeout=5)

        assert _spooled(spool_dir) == []

    def test_corrupt_file_that_cannot_be_deleted_does_not_break_replay(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        network.status = None
        transport = start()
        _capture(transport)
        (corrupt,) = _spooled(spool_dir)
        corrupt.write_bytes(b"\x00 truncated by a power cut")
        original = Path.unlink

        def locked(self: Path, *, missing_ok: bool = False) -> None:
            if self.suffix == ".envelope":
                msg = "read-only"
                raise PermissionError(msg)
            original(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", locked)
        transport.replay()

        assert _spooled(spool_dir) == [corrupt]


class TestBounds:
    def test_oldest_events_are_evicted_over_the_cap(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        transport = start(max_files=2)

        for index in range(3):
            _capture(transport, f"event-{index}")

        files = _spooled(spool_dir)
        assert len(files) == 2
        contents = [path.read_bytes() for path in files]
        assert not any(b"event-0" in content for content in contents)
        assert any(b"event-2" in content for content in contents)

    def test_eviction_failure_still_delivers_the_new_event(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        network.status = None
        transport = start(max_files=1)
        _capture(transport, "event-0")
        network.status = 200
        original = Path.unlink

        def locked(self: Path, *, missing_ok: bool = False) -> None:
            if "event-0" in self.read_text(encoding="utf-8", errors="ignore"):
                msg = "locked by another process"
                raise PermissionError(msg)
            original(self, missing_ok=missing_ok)

        monkeypatch.setattr(Path, "unlink", locked)
        _capture(transport, "event-1")
        monkeypatch.setattr(Path, "unlink", original)

        remaining = [path.read_bytes() for path in _spooled(spool_dir)]
        assert all(b"event-1" not in content for content in remaining)

    def test_each_dsn_gets_its_own_spool(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        network.status = None
        _capture(start(dsn=DSN))
        _capture(start(dsn=OTHER_DSN))

        directories = {path.parent for path in _spooled(spool_dir)}
        assert len(directories) == 2


class TestConcurrency:
    def test_replay_racing_a_fresh_capture_does_not_send_twice(
        self, start: Callable[..., SpoolTransport], network: _Network
    ) -> None:
        transport = start()
        original_evict = transport._evict

        def evict_then_replay_midway() -> None:
            # The file is already visible on disk here, before the capture
            # path has handed it to the worker: the window a replay thread
            # scanning the directory could hit.
            transport.replay()
            original_evict()

        transport._evict = evict_then_replay_midway  # type: ignore[method-assign]

        _capture(transport)

        assert network.requests == 1


class TestProcessDeath:
    def test_event_survives_a_hard_kill_and_is_sent_exactly_once(
        self, spool_dir: Path
    ) -> None:
        child = Path(__file__).with_name("_spool_child.py")

        def run(mode: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(  # noqa: S603
                [sys.executable, str(child), str(spool_dir), mode],
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )

        died = run("dead")
        assert died.returncode == 1
        assert len(_spooled(spool_dir)) == 1

        recovered = run("alive")
        assert recovered.returncode == 0, recovered.stderr
        assert recovered.stdout.strip() == "1"
        assert _spooled(spool_dir) == []


class TestStress:
    def test_parallel_capture_and_replay_send_every_event_exactly_once(
        self, start: Callable[..., SpoolTransport], spool_dir: Path, network: _Network
    ) -> None:
        threads_count, events_per_thread = 8, 25
        total = threads_count * events_per_thread
        transport = start()
        stop = threading.Event()

        def replay_loop() -> None:
            while not stop.is_set():
                transport.replay()

        def capture_burst(thread: int) -> None:
            for index in range(events_per_thread):
                sentry_sdk.capture_message(f"event-{thread}-{index}")

        replayer = threading.Thread(target=replay_loop)
        replayer.start()
        captors = [
            threading.Thread(target=capture_burst, args=(n,))
            for n in range(threads_count)
        ]
        for captor in captors:
            captor.start()
        for captor in captors:
            captor.join()
        stop.set()
        replayer.join()

        # The worker queue holds 100; overflow stays spooled for a later replay.
        for _ in range(50):
            transport.flush(timeout=5)
            if not _spooled(spool_dir):
                break
            transport.replay()

        assert _spooled(spool_dir) == []
        assert network.requests == total


class TestDegradation:
    """Observability must never take the receipt printer down with it."""

    def test_unwritable_spool_dir_still_delivers_events(
        self, start: Callable[..., SpoolTransport], tmp_path: Path, network: _Network
    ) -> None:
        blocker = tmp_path / "a-file"
        blocker.write_text("not a directory")

        _capture(start(directory=blocker / "spool"))

        assert network.requests == 1

    def test_write_failure_falls_back_to_a_plain_send(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        def disk_full(_fd: int) -> None:
            msg = "no space left on device"
            raise OSError(msg)

        transport = start()
        monkeypatch.setattr("croupier.spool.os.fsync", disk_full)

        _capture(transport)

        assert network.requests == 1
        assert _spooled(spool_dir) == []

    def test_unreadable_file_is_kept_not_deleted(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        network.status = None
        transport = start()
        _capture(transport)
        network.status = 200
        original = Path.read_bytes

        def locked(self: Path) -> bytes:
            if self.suffix == ".envelope":
                msg = "locked by another process"
                raise PermissionError(msg)
            return original(self)

        monkeypatch.setattr(Path, "read_bytes", locked)
        transport.replay()
        transport.flush(timeout=5)
        assert len(_spooled(spool_dir)) == 1

        monkeypatch.setattr(Path, "read_bytes", original)
        transport.replay()
        transport.flush(timeout=5)
        assert _spooled(spool_dir) == []

    def test_clock_reset_does_not_make_new_events_look_oldest(
        self,
        start: Callable[..., SpoolTransport],
        spool_dir: Path,
        network: _Network,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        network.status = None
        transport = start(max_files=2)
        clock = iter([2_000_000_000_000_000_000, 1_000_000_000_000_000_000] * 5)
        monkeypatch.setattr(
            "croupier.spool.time", SimpleNamespace(time_ns=lambda: next(clock))
        )

        for index in range(3):
            _capture(transport, f"event-{index}")

        contents = [path.read_bytes() for path in _spooled(spool_dir)]
        assert not any(b"event-0" in content for content in contents)
        assert any(b"event-1" in content for content in contents)
        assert any(b"event-2" in content for content in contents)
