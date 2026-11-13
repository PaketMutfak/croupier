import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from croupier.printed import PrintedReceipts


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TestPrintedReceipts:
    def test_unknown_id_is_not_printed(self) -> None:
        assert "req-1" not in PrintedReceipts()

    def test_added_id_is_printed(self) -> None:
        printed = PrintedReceipts()
        printed.add("req-1")

        assert "req-1" in printed
        assert "req-2" not in printed

    def test_id_is_kept_until_retention_ends(self) -> None:
        clock = _Clock()
        printed = PrintedReceipts(retention_seconds=60, clock=clock)
        printed.add("req-1")

        clock.now += 59
        assert "req-1" in printed

        clock.now += 1
        assert "req-1" not in printed

    def test_expired_ids_are_dropped_and_recent_ones_kept(self) -> None:
        clock = _Clock()
        printed = PrintedReceipts(retention_seconds=60, clock=clock)
        printed.add("old")
        clock.now += 30
        printed.add("recent")

        clock.now += 30
        assert "old" not in printed
        assert "recent" in printed

    def test_adding_again_restarts_retention(self) -> None:
        clock = _Clock()
        printed = PrintedReceipts(retention_seconds=60, clock=clock)
        printed.add("req-1")
        clock.now += 10
        printed.add("other")
        clock.now += 20
        printed.add("req-1")

        # 65 s after the first add, 35 s after the second.
        clock.now += 35
        assert "req-1" in printed
        assert "other" in printed

        clock.now += 10
        assert "other" not in printed
        assert "req-1" in printed


class TestPrintOnce:
    def test_prints_and_records(self) -> None:
        printed = PrintedReceipts()
        calls: list[str] = []

        assert printed.print_once("req-1", lambda: calls.append("req-1")) is True
        assert printed.print_once("req-1", lambda: calls.append("req-1")) is False
        assert calls == ["req-1"]

    def test_failed_print_is_not_recorded(self) -> None:
        printed = PrintedReceipts()

        def fail() -> None:
            msg = "printer offline"
            raise OSError(msg)

        with pytest.raises(OSError, match="printer offline"):
            printed.print_once("req-1", fail)

        assert "req-1" not in printed
        assert printed.print_once("req-1", lambda: None) is True

    def test_concurrent_duplicate_waits_and_skips(self) -> None:
        printed = PrintedReceipts()
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def slow_print() -> None:
            calls.append("first")
            started.set()
            release.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(printed.print_once, "req-1", slow_print)
            assert started.wait(timeout=5)
            second = pool.submit(
                printed.print_once, "req-1", lambda: calls.append("second")
            )
            # The duplicate must not print while the first is still printing.
            assert not second.done()
            release.set()

            assert first.result(timeout=5) is True
            assert second.result(timeout=5) is False
        assert calls == ["first"]

    def test_concurrent_duplicate_prints_when_first_fails(self) -> None:
        printed = PrintedReceipts()
        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def failing_print() -> None:
            started.set()
            release.wait(timeout=5)
            msg = "printer offline"
            raise OSError(msg)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(printed.print_once, "req-1", failing_print)
            assert started.wait(timeout=5)
            second = pool.submit(
                printed.print_once, "req-1", lambda: calls.append("second")
            )
            release.set()

            with pytest.raises(OSError, match="printer offline"):
                first.result(timeout=5)
            assert second.result(timeout=5) is True
        assert calls == ["second"]

    def test_other_ids_are_not_blocked(self) -> None:
        printed = PrintedReceipts()
        started = threading.Event()
        release = threading.Event()

        def slow_print() -> None:
            started.set()
            release.wait(timeout=5)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(printed.print_once, "req-1", slow_print)
            assert started.wait(timeout=5)
            assert printed.print_once("req-2", lambda: None) is True
            release.set()
            assert first.result(timeout=5) is True
