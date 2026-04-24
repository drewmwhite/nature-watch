import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

_RETRY_INTERVAL_S = 30
_QUEUE_FILE = "queue.json"


class Uploader:
    def __init__(self, bucket: str, prefix: str, region: str, buffer_dir: str):
        self._bucket = bucket
        self._prefix = prefix.rstrip("/")
        self._s3 = boto3.client("s3", region_name=region)
        self._buffer_dir = Path(buffer_dir)
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
        self._queue_path = self._buffer_dir / _QUEUE_FILE
        self._queue: list[str] = self._load_queue()
        self._lock = threading.Lock()
        self._last_upload_utc: str | None = None
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._upload_loop, daemon=True)
        self._thread.start()

    def enqueue(self, local_path: str) -> None:
        with self._lock:
            self._queue.append(local_path)
            self._save_queue()
        logger.debug("Enqueued for upload: %s", local_path)

    @property
    def queue_depth(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def last_upload_utc(self) -> str | None:
        return self._last_upload_utc

    def stop(self) -> None:
        self._stop_event.set()
        self._thread.join(timeout=5)

    def _s3_key(self, local_path: str) -> str:
        filename = os.path.basename(local_path)
        # Derive date/hour from the filename timestamp prefix (YYYYMMDD_HHMMSS_...)
        try:
            date_part = filename[:8]   # YYYYMMDD
            hour_part = filename[9:11]  # HH
            date_str = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]}"
        except (IndexError, ValueError):
            now = datetime.now(timezone.utc)
            date_str = now.strftime("%Y-%m-%d")
            hour_part = now.strftime("%H")
        return f"{self._prefix}/{date_str}/{hour_part}/{filename}"

    def _upload_one(self, local_path: str) -> bool:
        key = self._s3_key(local_path)
        try:
            self._s3.upload_file(local_path, self._bucket, key)
            logger.info("Uploaded s3://%s/%s", self._bucket, key)
            self._last_upload_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            return True
        except FileNotFoundError:
            logger.warning("Local file missing, dropping from queue: %s", local_path)
            return True  # remove from queue
        except (BotoCoreError, ClientError) as exc:
            logger.warning("Upload failed (%s): %s — will retry", exc, local_path)
            return False

    def _upload_loop(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                pending = list(self._queue)

            failed: list[str] = []
            for path in pending:
                if self._stop_event.is_set():
                    break
                if not self._upload_one(path):
                    failed.append(path)
                else:
                    try:
                        os.remove(path)
                    except OSError:
                        pass

            with self._lock:
                # Keep only items that weren't in this batch OR that failed
                uploaded = set(pending) - set(failed)
                self._queue = [p for p in self._queue if p not in uploaded]
                self._save_queue()

            self._stop_event.wait(timeout=_RETRY_INTERVAL_S)

    def _load_queue(self) -> list[str]:
        if self._queue_path.exists():
            try:
                data = json.loads(self._queue_path.read_text())
                return data if isinstance(data, list) else []
            except (json.JSONDecodeError, OSError):
                return []
        return []

    def _save_queue(self) -> None:
        try:
            self._queue_path.write_text(json.dumps(self._queue, indent=2))
        except OSError as exc:
            logger.warning("Could not persist upload queue: %s", exc)
