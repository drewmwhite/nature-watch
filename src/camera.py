import logging

import cv2
import numpy as np

logger = logging.getLogger(__name__)


class Camera:
    def __init__(self, index: int = 0):
        self._cap = cv2.VideoCapture(index)
        if not self._cap.isOpened():
            raise RuntimeError(
                f"Cannot open camera at index {index}. "
                "Check that the device is connected and not in use."
            )
        self.width = int(self._cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.height = int(self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.fps = self._cap.get(cv2.CAP_PROP_FPS) or 30.0
        logger.info(
            "Camera opened: index=%d  %dx%d  %.1f fps", index, self.width, self.height, self.fps
        )

    def read_frame(self) -> np.ndarray:
        ok, frame = self._cap.read()
        if not ok:
            raise RuntimeError("Failed to read frame from camera.")
        return frame

    def release(self) -> None:
        self._cap.release()
        logger.info("Camera released.")
