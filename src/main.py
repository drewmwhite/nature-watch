import logging
import signal
import sys
import time
from datetime import datetime, timezone

from . import config as cfg_module
from .camera import Camera
from .health import HealthServer
from .motion import MotionDetector
from .rate_limiter import RateLimiter
from .recorder import Recorder
from .uploader import Uploader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)


def main() -> None:
    cfg = cfg_module.load()

    start_time = time.monotonic()
    camera_ok = False
    last_clip_utc: str | None = None

    rate_limiter = RateLimiter(cap=cfg["clips_per_hour_cap"])
    uploader = Uploader(
        bucket=cfg["s3_bucket"],
        prefix=cfg["s3_prefix"],
        region=cfg["aws_region"],
        buffer_dir=cfg["local_buffer_dir"],
    )

    def get_status() -> dict:
        return {
            "uptime_s": int(time.monotonic() - start_time),
            "camera_ok": camera_ok,
            "clips_this_hour": rate_limiter.clips_this_hour(),
            "clips_per_hour_cap": cfg["clips_per_hour_cap"],
            "last_clip_utc": last_clip_utc,
            "upload_queue_depth": uploader.queue_depth,
            "last_upload_utc": uploader.last_upload_utc,
        }

    health = HealthServer(port=cfg["health_port"], get_status=get_status)
    health.start()

    camera: Camera | None = None
    try:
        camera = Camera(index=cfg["camera_index"])
        camera_ok = True
    except RuntimeError as exc:
        logger.error("Camera init failed: %s", exc)
        logger.warning("Running without camera — health endpoint still available.")

    detector = MotionDetector(
        threshold=cfg["motion_threshold"],
        blur_ksize=cfg["motion_blur_ksize"],
    )
    recorder: Recorder | None = None
    if camera:
        recorder = Recorder(
            camera=camera,
            buffer_dir=cfg["local_buffer_dir"],
            clip_duration_s=cfg["clip_duration_s"],
            cooldown_s=cfg["clip_cooldown_s"],
        )

    def _shutdown(signum, frame) -> None:
        logger.info("Shutting down (signal %d)…", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _shutdown)

    logger.info("Nature Watch started. Press Ctrl-C to stop.")

    try:
        while True:
            if camera is None or recorder is None:
                time.sleep(1)
                continue

            try:
                frame = camera.read_frame()
            except RuntimeError as exc:
                logger.error("Frame read error: %s", exc)
                camera_ok = False
                time.sleep(1)
                continue

            camera_ok = True

            if not detector.detect(frame):
                continue

            if recorder.in_cooldown:
                continue

            if not rate_limiter.is_allowed():
                rate_limiter.on_cap_hit()
                continue

            clip_path = recorder.record()
            if clip_path:
                last_clip_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                rate_limiter.record()
                uploader.enqueue(clip_path)

    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopping…")
    finally:
        if camera:
            camera.release()
        uploader.stop()
        health.stop()
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
