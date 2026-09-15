"""Capture, compositing and encoding.

numpy and OpenCV only - no Qt imports at this level, so the pipeline can be
exercised headlessly. The QImage overlay produced by wer.overlay is handed in
as an array by the caller rather than imported here.
"""
