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
