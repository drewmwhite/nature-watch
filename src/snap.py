import logging
import sys
import uuid
from datetime import datetime, timezone

import boto3
import cv2
from botocore.exceptions import BotoCoreError, ClientError

from . import config as cfg_module

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

_SNAPSHOTS_PREFIX = "snapshots"


def snap() -> None:
    cfg = cfg_module.load()

    cap = cv2.VideoCapture(cfg["camera_index"])
    if not cap.isOpened():
        logger.error("Cannot open camera at index %d.", cfg["camera_index"])
        sys.exit(1)

    # Discard a few frames so the sensor has time to adjust exposure
    for _ in range(5):
        cap.read()

    ok, frame = cap.read()
    cap.release()

    if not ok:
        logger.error("Failed to read frame from camera.")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    filename = f"{now.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}.jpg"
    key = f"{_SNAPSHOTS_PREFIX}/{filename}"

    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])

    s3 = boto3.client("s3", region_name=cfg["aws_region"])
    try:
        s3.put_object(
            Bucket=cfg["s3_bucket"],
            Key=key,
            Body=buf.tobytes(),
            ContentType="image/jpeg",
        )
    except (BotoCoreError, ClientError) as exc:
        logger.error("Upload failed: %s", exc)
        sys.exit(1)

    logger.info("Snapshot saved to s3://%s/%s", cfg["s3_bucket"], key)


if __name__ == "__main__":
    snap()
