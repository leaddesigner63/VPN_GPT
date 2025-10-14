from __future__ import annotations

import threading
from typing import Any, Callable, Sequence

from api.utils import db, xray
from api.utils.logging import get_logger

logger = get_logger("expired_key_monitor")


ExpiredKeyRecord = dict[str, Any]


class ExpiredKeyMonitor:
    """Background helper that deactivates expired VPN keys and syncs Xray."""

    def __init__(
        self,
        *,
        interval_seconds: float = 60.0,
        fetch_expired: Callable[[], Sequence[ExpiredKeyRecord]] | None = None,
        deactivate_key: Callable[[str], None] | None = None,
        remove_client: Callable[[str], Any] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            logger.warning(
                "Invalid expired key poll interval supplied; using default",
                extra={"interval_seconds": interval_seconds},
            )
            interval_seconds = 60.0

        self.interval_seconds = float(interval_seconds)
        self._fetch_expired = fetch_expired or db.list_expired_keys
        self._deactivate_key = deactivate_key or db.deactivate_key
        self._remove_client = remove_client or xray.remove_client
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the background monitoring thread if it is not already running."""

        if self._thread and self._thread.is_alive():
            logger.debug("Expired key monitor already running")
            return

        self._stop_event.clear()
        thread = threading.Thread(target=self._run_loop, name="expired-key-monitor", daemon=True)
        thread.start()
        self._thread = thread
        logger.info(
            "Expired key monitor thread started", extra={"interval_seconds": self.interval_seconds}
        )

    def stop(self) -> None:
        """Signal the monitoring thread to stop and wait for it to finish."""

        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=self.interval_seconds + 1.0)
        self._thread = None
        logger.info("Expired key monitor thread stopped")

    def run_once(self) -> int:
        """Run a single sweep of expired keys.

        Returns the number of keys that were marked inactive during this run.
        """

        try:
            expired_keys = list(self._fetch_expired())
        except Exception:
            logger.exception("Failed to fetch expired VPN keys")
            return 0

        processed = 0

        for record in expired_keys:
            uuid_value = record.get("uuid")
            username = record.get("username")
            if not uuid_value:
                logger.warning(
                    "Skipping expired key without UUID", extra={"username": username}
                )
                continue

            try:
                self._deactivate_key(uuid_value)
            except Exception:
                logger.exception(
                    "Failed to deactivate expired VPN key",
                    extra={"uuid": uuid_value, "username": username},
                )
                continue

            try:
                self._remove_client(uuid_value)
            except Exception:
                logger.exception(
                    "Failed to remove VPN client from Xray",
                    extra={"uuid": uuid_value, "username": username},
                )

            processed += 1

        if processed or expired_keys:
            logger.info(
                "Expired key sweep completed",
                extra={"processed": processed, "candidates": len(expired_keys)},
            )

        return processed

    def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("Unexpected error during expired key sweep")

            if self._stop_event.wait(self.interval_seconds):
                break


__all__ = ["ExpiredKeyMonitor"]
