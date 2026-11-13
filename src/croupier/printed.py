import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

# RabbitMQ redelivers an unacked message soon after the channel comes back, so
# a day is far longer than the redelivery window while keeping the set small.
DEFAULT_RETENTION_SECONDS = 24 * 60 * 60


class PrintedReceipts:
    """Correlation ids of receipts this process has already printed.

    Thread-safe: ids are added from the worker thread that printed the receipt.
    In memory only: a restart forgets them. Ids older than ``retention_seconds``
    are dropped on every access, so the set stays bounded by receipt volume.
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

    def _prune(self) -> None:
        cutoff = self._clock() - self._retention_seconds
        for correlation_id, printed_at in list(self._printed_at.items()):
            if printed_at > cutoff:
                break
            del self._printed_at[correlation_id]
