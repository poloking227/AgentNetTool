"""Validate decoded video before publishing or reusing a processing result."""

import os
import math

import cv2


def read_video_timing(path):
    """Read the source timing used to calculate expected clip lengths."""
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open source video: {path}")
        fps = cap.get(cv2.CAP_PROP_FPS)
        total_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        if not all(math.isfinite(v) and v > 0 for v in (fps, total_frames)):
            raise ValueError(f"Invalid source video timing: {path}")
        return {"fps": fps, "total_frames": int(total_frames)}
    finally:
        cap.release()


def validate_video(path, expected_frames=None):
    """Raise on empty, unreadable, or truncated video; return decoded frame count.

    Decode sequentially rather than trusting a container header or a single
    thumbnail. A file interrupted during encoding can still contain both.
    """
    if not os.path.isfile(path) or os.path.getsize(path) == 0:
        raise ValueError(f"Video is missing or empty: {path}")
    cap = cv2.VideoCapture(str(path))
    try:
        if not cap.isOpened():
            raise ValueError(f"Cannot open video: {path}")
        declared_frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
        decoded_frames = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if frame is None or frame.size == 0:
                raise ValueError(f"Video contains an invalid frame: {path}")
            decoded_frames += 1
        if decoded_frames == 0:
            raise ValueError(f"Video has no decodable frames: {path}")
        if declared_frames > decoded_frames + 0.5:
            raise ValueError(f"Video is truncated: decoded {decoded_frames} of {declared_frames:g} frames: {path}")
        if expected_frames is not None and decoded_frames != expected_frames:
            raise ValueError(f"Incomplete video: expected {expected_frames} frames, decoded {decoded_frames}: {path}")
        return decoded_frames
    finally:
        cap.release()
