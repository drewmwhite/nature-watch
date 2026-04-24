import logging
import time
from collections import deque

logger = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, cap: int):
        self._cap = cap
        self._timestamps: deque[float] = deque()
        self._last_alert_at: float = 0.0

    def _prune(self) -> None:
        cutoff = time.time() - 3600
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()

    def is_allowed(self) -> bool:
        self._prune()
        return len(self._timestamps) < self._cap

    def record(self) -> None:
        self._timestamps.append(time.time())

    def clips_this_hour(self) -> int:
        self._prune()
        return len(self._timestamps)

    def on_cap_hit(self) -> None:
        now = time.time()
        # Suppress repeated alerts within the same hour window
        if now - self._last_alert_at < 3600:
            return
        self._last_alert_at = now
        logger.warning(
            "Rate cap of %d clips/hour reached. Dropping further clips until the next hour. "
            # TODO: publish to SNS topic for external alerting
            "Configure sns_topic_arn in config.yaml to enable SNS alerts.",
            self._cap,
        )
