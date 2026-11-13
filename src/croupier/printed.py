import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from dataclasses import field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

# RabbitMQ redelivers an unacked message soon after the channel comes back, so
# a day is far longer than the redelivery window while keeping the set small.
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60


@dataclass
class _Claim:
    lock: threading.Lock = field(default_factory=threading.Lock)
    holders: int = 0


class PrintedReceipts:
    """Correlation ids of receipts this process has already printed.

    Thread-safe: ``print_once`` runs in a worker thread. In memory only: a
    restart forgets them. Ids older than ``retention_seconds`` are dropped on
    every access, so the set stays bounded by receipt volume.
    """

    def __init__(
        self,
        retention_seconds: float = DEFAULT_RETENTION_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retention_seconds = retention_seconds
        self._clock = clock
        # Insertion order is print order, so the oldest ids are at the front.
        self._printed_at: dict[str, float] = {}
        self._claims: dict[str, _Claim] = {}
        self._lock = threading.Lock()

    def __contains__(self, correlation_id: str) -> bool:
        with self._lock:
            self._prune()
            return correlation_id in self._printed_at

    def add(self, correlation_id: str) -> None:
        with self._lock:
            self._prune()
            self._printed_at.pop(correlation_id, None)
            self._printed_at[correlation_id] = self._clock()

    def print_once(self, correlation_id: str, print_: Callable[[], None]) -> bool:
        """Call ``print_`` unless ``correlation_id`` was already printed.

        Deliveries are handled concurrently, so a redelivery can arrive while
        the first copy is still printing. A second call with the same id waits
        for the first: it skips if that print succeeded and prints if it
        failed. The id is recorded only after ``print_`` returns; its errors
        propagate. Returns whether this call printed.
        """
        with self._claim(correlation_id):
            if correlation_id in self:
                return False
            print_()
            self.add(correlation_id)
            return True

    @contextmanager
    def _claim(self, correlation_id: str) -> Iterator[None]:
        with self._lock:
            claim = self._claims.setdefault(correlation_id, _Claim())
            claim.holders += 1
        try:
            with claim.lock:
                yield
        finally:
            with self._lock:
                claim.holders -= 1
                if not claim.holders:
                    del self._claims[correlation_id]

    def _prune(self) -> None:
        cutoff = self._clock() - self._retention_seconds
        for correlation_id, printed_at in list(self._printed_at.items()):
            if printed_at > cutoff:
                break
            del self._printed_at[correlation_id]
