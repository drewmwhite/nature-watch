import cv2
import numpy as np


class MotionDetector:
    def __init__(self, threshold: int = 500, blur_ksize: int = 21):
        self._threshold = threshold
        self._blur_ksize = blur_ksize
        self._prev_gray: np.ndarray | None = None

    def detect(self, frame: np.ndarray) -> bool:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        blurred = cv2.GaussianBlur(gray, (self._blur_ksize, self._blur_ksize), 0)

        if self._prev_gray is None:
            self._prev_gray = blurred
            return False

        delta = cv2.absdiff(self._prev_gray, blurred)
        self._prev_gray = blurred

        _, thresh = cv2.threshold(delta, 25, 255, cv2.THRESH_BINARY)
        dilated = cv2.dilate(thresh, None, iterations=2)
        contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        return any(cv2.contourArea(c) >= self._threshold for c in contours)
