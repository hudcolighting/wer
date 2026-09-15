"""Save a still frame from the feed.

No Qt. Works whether or not a recording is running, because the two are
unrelated: the camera is live from the moment the app opens.

Writing the file
----------------
``cv2.imwrite`` is not used, and that is deliberate rather than fussy. On
Windows it passes the path to the C runtime as bytes in the system code page, so
it **fails silently** on any path containing a character outside that page --
returning False rather than raising. A show called *Rosencrantz och Gyldenstern*
or a Windows user account with an accent in it would produce no file and no
error. Encoding to memory and writing the bytes ourselves sidesteps the whole
problem.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import cv2
import numpy as np

log = logging.getLogger(__name__)

__all__ = ["SnapshotFormat", "SnapshotResult", "save_snapshot"]


class SnapshotFormat(str, Enum):
    #: Lossless. The right default for a reference still of a lighting state,
    #: which is a thing you may want to look at closely.
    PNG = "png"
    #: Much smaller, and fine for a quick record of a moment.
    JPEG = "jpg"

    @property
    def label(self) -> str:
        return {
            SnapshotFormat.PNG: "PNG (lossless, larger)",
            SnapshotFormat.JPEG: "JPEG (smaller)",
        }[self]


@dataclass(frozen=True)
class SnapshotResult:
    ok: bool
    path: Path
    detail: str = ""
    size_bytes: int = 0

    def __bool__(self) -> bool:
        return self.ok


def save_snapshot(
    image: np.ndarray,
    path: Path,
    *,
    image_format: SnapshotFormat = SnapshotFormat.PNG,
    jpeg_quality: int = 92,
) -> SnapshotResult:
    """Write a BGR frame to disk.

    The array is encoded as it is given, so if the caller hands over a frame
    that already has the overlay drawn on it, the overlay is in the still.
    """
    if image is None or image.size == 0:
        return SnapshotResult(False, path, "there was no frame to save")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return SnapshotResult(False, path, f"cannot create {path.parent}: {exc}")

    if image_format is SnapshotFormat.JPEG:
        params = [int(cv2.IMWRITE_JPEG_QUALITY), max(1, min(100, jpeg_quality))]
    else:
        # 3 is a reasonable middle: PNG compression is lossless at every level,
        # so this only trades encode time against file size. At 1080p level 9
        # takes noticeably longer for a few percent, and a snapshot should feel
        # instant.
        params = [int(cv2.IMWRITE_PNG_COMPRESSION), 3]

    try:
        success, buffer = cv2.imencode(f".{image_format.value}", image, params)
    except cv2.error as exc:
        log.exception("Could not encode snapshot")
        return SnapshotResult(False, path, f"could not encode the image: {exc}")

    if not success:
        return SnapshotResult(False, path, "the image could not be encoded")

    try:
        path.write_bytes(buffer.tobytes())
    except OSError as exc:
        log.exception("Could not write snapshot %s", path)
        return SnapshotResult(False, path, f"could not write the file: {exc}")

    size = path.stat().st_size
    log.info("Snapshot saved: %s (%.1f KB)", path.name, size / 1024)
    return SnapshotResult(True, path, size_bytes=size)
