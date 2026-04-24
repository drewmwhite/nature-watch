import logging
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import cv2

from .camera import Camera

logger = logging.getLogger(__name__)

# fourcc for MP4 / H.264
_FOURCC = cv2.VideoWriter_fourcc(*"mp4v")


class Recorder:
    def __init__(self, camera: Camera, buffer_dir: str, clip_duration_s: int, cooldown_s: int):
        self._camera = camera
        self._buffer_dir = Path(buffer_dir)
        self._buffer_dir.mkdir(parents=True, exist_ok=True)
        self._clip_duration_s = clip_duration_s
        self._cooldown_s = cooldown_s
        self._cooldown_until: float = 0.0

    @property
    def in_cooldown(self) -> bool:
        return time.monotonic() < self._cooldown_until

    def record(self) -> str | None:
        if self.in_cooldown:
            return None

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        clip_id = str(uuid.uuid4())[:8]
        filename = f"{ts}_{clip_id}.mp4"
        dest = self._buffer_dir / filename

        writer = cv2.VideoWriter(
            str(dest),
            _FOURCC,
            self._camera.fps,
            (self._camera.width, self._camera.height),
        )

        logger.info("Recording clip: %s", filename)
        deadline = time.monotonic() + self._clip_duration_s
        frames_written = 0

        try:
            while time.monotonic() < deadline:
                frame = self._camera.read_frame()
                writer.write(frame)
                frames_written += 1
        finally:
            writer.release()

        logger.info("Clip saved (%d frames): %s", frames_written, dest)
        self._cooldown_until = time.monotonic() + self._cooldown_s
        return str(dest)
