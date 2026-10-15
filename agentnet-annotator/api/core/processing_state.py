"""
Persistent processing state of a recording.

After a recording stops, the reducer turns the raw data (events.jsonl, the OBS
video, element/html data) into reduced events and video clips. That takes a long
time for long recordings, so its progress is kept in processing_state.json inside
the recording folder. If the machine crashes (e.g. a BSOD) in the middle, the
state file survives and the processing can be resumed on the next start.
"""

import json
import os
import time
from datetime import datetime

from .logger import logger

STATE_FILE = "processing_state.json"

# Processing status values stored in the state file
STATE_PENDING = "pending"
STATE_PROCESSING = "processing"
STATE_DONE = "done"
STATE_FAILED = "failed"

# Processing stages, in pipeline order
STAGE_QUEUED = "queued"
STAGE_REDUCE = "reduce"
STAGE_MATCH = "match"
STAGE_DUMP = "dump"
STAGE_CLIPS = "clips"
STAGE_DONE = "done"

# A recording whose processing was interrupted this many times is not resumed
# automatically on start any more (the processing itself may be what crashes the
# machine); the user has to resume it by hand.
MAX_AUTO_RESUME_ATTEMPTS = 2

# Raw files that must exist for a recording to be (re)processed
RAW_REQUIRED_FILES = ["events.jsonl", "metadata.json"]


def fsync_fp(fp):
    """Flush a Python file object all the way to disk."""
    fp.flush()
    os.fsync(fp.fileno())


def fsync_path(path):
    """Best-effort fsync of a file by path (e.g. the video written by OBS)."""
    try:
        fd = os.open(path, os.O_RDWR | getattr(os, "O_BINARY", 0))
    except OSError as e:
        logger.warning(f"fsync_path: cannot open {path}: {e}")
        return
    try:
        os.fsync(fd)
    except OSError as e:
        logger.warning(f"fsync_path: fsync failed for {path}: {e}")
    finally:
        os.close(fd)


def replace_file(src, dst, retries=5):
    """os.replace with retries: on Windows the target can be briefly locked
    (antivirus, indexer)."""
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def atomic_write_text(path, text):
    """Write a text file so that a crash leaves either the old or the new
    content, never a half-written file."""
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(text)
        fsync_fp(f)
    replace_file(tmp_path, path)


def read_state(recording_path):
    path = os.path.join(recording_path, STATE_FILE)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        logger.warning(f"read_state: invalid state file {path}: {e}")
        return None


def write_state(recording_path, **updates):
    """Merge updates into the state file and persist it atomically."""
    state = read_state(recording_path) or {}
    state.update(updates)
    state["updated_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_text(
        os.path.join(recording_path, STATE_FILE),
        json.dumps(state, indent=4, ensure_ascii=False),
    )
    return state


def has_video(recording_path):
    return any(f.endswith(".mp4") for f in os.listdir(recording_path))


def has_raw_data(recording_path):
    """Whether the raw recording data needed for processing is present."""
    if not os.path.isdir(recording_path):
        return False
    for file_name in RAW_REQUIRED_FILES:
        if not os.path.exists(os.path.join(recording_path, file_name)):
            return False
    return has_video(recording_path)


def has_processed_output(recording_path):
    return os.path.exists(
        os.path.join(recording_path, "reduced_events_vis.jsonl")
    ) and os.path.isdir(os.path.join(recording_path, "video_clips"))


def get_processing_status(recording_path):
    """
    Processing status of a recording as stored on disk:
    STATE_DONE, STATE_PENDING / STATE_PROCESSING (unfinished), STATE_FAILED,
    or None when the recording can't be processed at all.

    Recordings made before the state file existed are judged by their files.
    """
    state = read_state(recording_path)
    if state is not None:
        return state.get("status", STATE_PENDING)
    if has_processed_output(recording_path):
        return STATE_DONE
    if has_raw_data(recording_path):
        return STATE_PENDING
    return None
