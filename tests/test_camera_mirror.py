"""Mirror left to right and Flip top to bottom, for a camera mounted upside down.

A camera rigged upside down on a boom arm, or looking at the stage through a
mirror, records a picture nobody wants to scrub through in the notes session,
and the booth cannot always move the camera. The two boxes on the Preview tab
turn the picture in the capture loop, before the overlay is drawn, so the
preview, the recording and snapshots all get the same picture and the overlay's
text still reads the right way round.

The failures worth pinning are the quiet ones, where the screen being watched
looks right: a recording the other way up from the preview, an overlay mirrored
along with the picture, a box showing ticked over a camera that opened without
it, or a picture that turned over partway through a take with nothing in the
log to say why.

Nothing here opens a camera. Frames go through the real CameraCapture loop on
this thread, handed to it by a stand-in for cv2.VideoCapture, and the panel
runs on a stand-in CameraCapture that opens nothing, over a fake device list.
"""

from __future__ import annotations

import logging

import cv2
import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

from wer.core.databus import DataBus
from wer.video.capture import CameraCapture, CaptureSettings
from wer.video.devices import CaptureDevice, VideoFormat

HEIGHT, WIDTH = 36, 64
#: (row, column). Off both centre lines, so every flip moves it somewhere else.
MARK = (3, 10)
MIRRORED = (MARK[0], WIDTH - 1 - MARK[1])
FLIPPED = (HEIGHT - 1 - MARK[0], MARK[1])
BOTH = (HEIGHT - 1 - MARK[0], WIDTH - 1 - MARK[1])


def marked_frame() -> np.ndarray:
    """A black BGR frame with one red pixel at MARK."""
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    image[MARK] = (0, 0, 255)
    return image


def where_the_mark_is(image: np.ndarray) -> tuple[int, int]:
    found = np.argwhere(image[:, :, 2] == 255)
    assert len(found) == 1, f"expected one marked pixel, found {len(found)}"
    return int(found[0][0]), int(found[0][1])


class OneFrame:
    """Enough of cv2.VideoCapture to hand the capture loop one picture."""

    def __init__(self, camera: CameraCapture, image: np.ndarray) -> None:
        self.camera = camera
        self.image = image

    def read(self):
        # Asked to stop as the frame is handed over, so the loop takes this
        # one frame all the way through and then ends.
        self.camera._stop.set()
        return True, self.image.copy()

    def release(self) -> None:
        pass


def through_the_capture_loop(camera: CameraCapture, image: np.ndarray) -> np.ndarray:
    """What the preview gets when the camera's real capture loop reads ``image``.

    Played on this thread through CameraCapture._run, so the flips and the
    overlay run in the order the capture thread runs them, with nothing racing.
    """
    held = camera._capture
    camera._capture = OneFrame(camera, image)
    camera._stop.clear()
    try:
        camera._run()
    finally:
        camera._capture = held
    return camera.preview_queue.get_nowait().image


# ------------------------------------------------------------------ the picture


@pytest.mark.parametrize(
    ("horizontal", "vertical", "lands_at"),
    [
        (False, False, MARK),
        (True, False, MIRRORED),
        (False, True, FLIPPED),
        (True, True, BOTH),
    ],
    ids=["neither", "mirror left to right", "flip top to bottom", "both"],
)
def test_each_flip_moves_a_marked_pixel_to_where_it_belongs(
    horizontal: bool, vertical: bool, lands_at: tuple[int, int]
) -> None:
    """Mirror left to right swaps the columns and nothing else, and Flip top to
    bottom the rows. The other way about, a camera seen in a mirror would come
    out upside down instead. The size is checked too: a flip that changed it
    could not be allowed during a take."""
    camera = CameraCapture(
        CaptureSettings(flip_horizontal=horizontal, flip_vertical=vertical)
    )
    picture = through_the_capture_loop(camera, marked_frame())
    assert picture.shape == (HEIGHT, WIDTH, 3), "a flip changed the frame's size"
    assert where_the_mark_is(picture) == lands_at


def test_both_flips_are_the_same_picture_as_a_camera_turned_half_a_turn() -> None:
    """What the Preview tab and the Help tell the operator: tick both for a
    camera mounted upside down. Compared on every pixel of a picture with no
    symmetry in it, not on one pixel."""
    picture = (
        np.arange(HEIGHT * WIDTH * 3, dtype=np.uint32).reshape(HEIGHT, WIDTH, 3) % 251
    ).astype(np.uint8)
    flipped = through_the_capture_loop(
        CameraCapture(CaptureSettings(flip_horizontal=True, flip_vertical=True)),
        picture,
    )
    turned = through_the_capture_loop(
        CameraCapture(CaptureSettings(rotation=180)), picture
    )
    assert not np.array_equal(flipped, picture), "nothing was turned at all"
    assert np.array_equal(flipped, turned)


@pytest.mark.parametrize(
    ("rotation", "horizontal", "vertical", "lands_at"),
    [
        # A quarter turn clockwise takes (row, column) to (column, HEIGHT-1-row)
        # in a frame WIDTH rows tall and HEIGHT columns wide. Mirrored after
        # that, it is (column, row).
        (90, True, False, (MARK[1], MARK[0])),
        # Flipped after it instead: (WIDTH-1-column, HEIGHT-1-row).
        (90, False, True, (WIDTH - 1 - MARK[1], HEIGHT - 1 - MARK[0])),
        # A quarter turn anticlockwise takes it to (WIDTH-1-column, row), and
        # mirrored after that, (WIDTH-1-column, HEIGHT-1-row).
        (270, True, False, (WIDTH - 1 - MARK[1], HEIGHT - 1 - MARK[0])),
    ],
    ids=["90 then mirror", "90 then flip", "270 then mirror"],
)
def test_the_flips_apply_to_the_picture_as_rotated(
    rotation: int, horizontal: bool, vertical: bool, lands_at: tuple[int, int]
) -> None:
    """CaptureSettings documents the flips as applied after the rotation, so
    "left to right" is left to right on the picture as turned. Applied before
    it, the mark lands somewhere else in every case here."""
    camera = CameraCapture(
        CaptureSettings(
            rotation=rotation, flip_horizontal=horizontal, flip_vertical=vertical
        )
    )
    picture = through_the_capture_loop(camera, marked_frame())
    assert picture.shape == (WIDTH, HEIGHT, 3)
    assert where_the_mark_is(picture) == lands_at


# ------------------------------------------------------------------ the overlay


def test_the_overlay_is_drawn_on_the_mirrored_picture_and_is_not_mirrored_with_it(
    qt_app,
) -> None:
    """The overlay is burnt into the recording, and a cue number mirrored
    along with the picture is no use to anyone reading it back. So the flips
    have to run before the compositor in the capture loop, not after it."""
    from wer.overlay.compositor import Compositor
    from wer.overlay.widgets import TextWidget

    bus = DataBus()
    bus.publish("eos.cue.active.number", "58", source_connection_id="eos")
    compositor = Compositor(bus)
    compositor.add(TextWidget("cue", "Cue {eos.cue.active.number}"))  # top left
    camera = CameraCapture(CaptureSettings(flip_horizontal=True))
    camera.frame_processor = compositor.composite

    stage = np.zeros((360, 640, 3), dtype=np.uint8)
    stage[:40, :60] = (0, 0, 255)  # something in the top left of the stage

    picture = through_the_capture_loop(camera, stage)

    assert not np.array_equal(picture, cv2.flip(stage, 1)), (
        "the overlay drew nothing, so this proves nothing"
    )
    assert np.array_equal(
        picture, compositor.composite(cv2.flip(stage, 1), in_place=False)
    ), "the overlay was not drawn on the mirrored picture"
    assert not np.array_equal(
        picture, cv2.flip(compositor.composite(stage, in_place=False), 1)
    ), "the overlay was mirrored along with the picture"


# -------------------------------------------------------------- the Preview tab

STAGE_CAMERA = CaptureDevice(0, "Blackmagic WDM Capture")
FORMATS = [
    VideoFormat(1920, 1080, 30.0, 30.0, "UYVY"),
    VideoFormat(1280, 720, 30.0, 30.0, "UYVY"),
]


class StandInCamera(CameraCapture):
    """A CameraCapture that opens nothing and starts no thread.

    Frames reach its real capture loop only when a test hands one over
    (through_the_capture_loop), so what a tick does to the picture is seen
    frame by frame, with nothing racing it.
    """

    #: Every camera opened, across every instance, in order.
    opened: list[StandInCamera] = []

    def __init__(self, settings: CaptureSettings, *, on_error=None) -> None:
        super().__init__(settings, on_error=on_error)
        self.running = False

    def open(self) -> bool:
        StandInCamera.opened.append(self)
        self._backend_name = "STAND-IN"
        self._actual = (self.settings.width, self.settings.height, self.settings.fps)
        return True

    def start(self) -> None:
        self.running = True

    def stop(self, timeout: float = 3.0) -> bool:
        self.running = False
        return True

    @property
    def is_running(self) -> bool:
        return self.running


class SyncProbe(QObject):
    """FormatProbe without the thread, so nothing has to pump events."""

    finished = Signal(object)

    def probe(self, devices) -> None:
        self.finished.emit({device.index: list(FORMATS) for device in devices})


@pytest.fixture
def preview_module(qt_app, monkeypatch):
    """wer.ui.preview over a stand-in camera and a fake device list."""
    from wer.ui import preview

    StandInCamera.opened = []
    monkeypatch.setattr(preview, "enumerate_video_devices", lambda: [STAGE_CAMERA])
    monkeypatch.setattr(preview, "probe_formats", lambda device: list(FORMATS))
    monkeypatch.setattr(preview, "FormatProbe", SyncProbe)
    monkeypatch.setattr(preview, "CameraCapture", StandInCamera)
    return preview


@pytest.fixture
def panel(preview_module):
    """A real PreviewPanel, live on the stand-in camera."""
    widget = preview_module.PreviewPanel()
    try:
        assert widget.capture is not None and widget.capture.is_running
        yield widget
    finally:
        widget.stop_capture()
        widget.deleteLater()


def test_ticking_a_box_turns_the_live_picture_without_reopening_the_camera(
    panel,
) -> None:
    """Opening a camera takes seconds with no picture meanwhile, so a tick
    changes the capture that is running, from its next frame on."""
    camera = panel.capture
    opens = len(StandInCamera.opened)
    assert where_the_mark_is(through_the_capture_loop(camera, marked_frame())) == MARK

    panel._mirror_box.setChecked(True)
    assert panel.capture is camera and len(StandInCamera.opened) == opens, (
        "reopened the camera to mirror it"
    )
    assert where_the_mark_is(through_the_capture_loop(camera, marked_frame())) == MIRRORED

    panel._flip_box.setChecked(True)
    assert where_the_mark_is(through_the_capture_loop(camera, marked_frame())) == BOTH

    panel._mirror_box.setChecked(False)
    panel._flip_box.setChecked(False)
    assert where_the_mark_is(through_the_capture_loop(camera, marked_frame())) == MARK
    assert panel.capture is camera and len(StandInCamera.opened) == opens


def test_a_flip_during_a_take_reaches_the_recording_and_the_log_says_so(
    panel, caplog
) -> None:
    """Device, Format and Refresh are locked while recording because each ends
    the take's video. A flip keeps the frame's size and ends nothing, so it
    stays available; the log is how a picture that turns over partway through a
    take is explained afterwards."""
    camera = panel.capture
    panel.set_recording(True)
    camera.feed_encoder.set()
    assert panel._mirror_box.isEnabled() and panel._flip_box.isEnabled(), (
        "the flips are locked during a take"
    )

    with caplog.at_level(logging.INFO, logger="wer.ui.preview"):
        panel._flip_box.setChecked(True)
    preview = through_the_capture_loop(camera, marked_frame())
    recorded = camera.encoder_queue.get_nowait().image

    assert where_the_mark_is(preview) == FLIPPED
    assert where_the_mark_is(recorded) == FLIPPED, (
        "the recording is the other way up from the preview"
    )
    lines = [r.getMessage() for r in caplog.records if r.name == "wer.ui.preview"]
    assert len(lines) == 1, lines
    assert "during a take" in lines[0] and "flipped top to bottom" in lines[0], lines[0]


def test_the_camera_opens_with_the_flips_it_was_given_and_keeps_them_on_another_format(
    preview_module, caplog
) -> None:
    """The show file's flips come in with the panel and have to be on the first
    frame. A box that stays ticked over a camera restarted on another format
    has to go on being what the picture does."""
    with caplog.at_level(logging.INFO, logger="wer.ui.preview"):
        panel = preview_module.PreviewPanel(flip_horizontal=True, flip_vertical=True)
    try:
        first = panel.capture
        assert panel._mirror_box.isChecked() and panel._flip_box.isChecked()
        assert where_the_mark_is(through_the_capture_loop(first, marked_frame())) == BOTH
        messages = [r.getMessage() for r in caplog.records]
        assert not any("flips changed" in m for m in messages), (
            "the show file's flips were logged as a change someone made"
        )

        other = 1 - panel._format_box.currentIndex()
        panel._format_box.setCurrentIndex(other)
        second = panel.capture
        assert second is not first, "choosing a format did not restart the camera"
        assert (second.settings.width, second.settings.height) == (
            FORMATS[other].width, FORMATS[other].height,
        )
        assert where_the_mark_is(through_the_capture_loop(second, marked_frame())) == BOTH, (
            "the restarted camera lost the flips its boxes still show"
        )
    finally:
        panel.stop_capture()
        panel.deleteLater()


# ------------------------------------------------------------------ the show file


def test_the_show_files_flips_reach_the_preview_tab_and_a_tick_is_saved_at_once(
    preview_module, monkeypatch
) -> None:
    """Saved with the show, so a camera that hangs upside down for the run is
    set once rather than at every launch. Saved at the tick, rather than at
    whatever next happens to autosave, which a crash could get to first."""
    from wer.core.showfile import ShowFile, autosave_path, load_show
    from wer.ui import main_window as main_window_module

    show = ShowFile()
    show.camera.flip_vertical = True
    monkeypatch.setattr(main_window_module, "load_autosave", lambda: show)

    window = main_window_module.MainWindow()
    try:
        preview = window.preview
        assert preview._flip_box.isChecked(), (
            "the show file's flip never reached the Preview tab"
        )
        assert not preview._mirror_box.isChecked()

        window._autosave_timer.stop()
        preview._mirror_box.setChecked(True)
        assert window._autosave_timer.isActive(), "a tick did not ask for a save"
        window._autosave()

        saved = load_show(autosave_path()).camera
        assert (saved.flip_horizontal, saved.flip_vertical) == (True, True)
    finally:
        window.close()


# ------------------------------------------------------- preview frame cost


def test_the_preview_shrinks_before_converting(qt_app) -> None:
    """The expensive passes happen at preview size, not at capture size.

    This runs on the main thread for every frame shown. It used to do three
    full-frame passes at capture size -- a QImage copy, a conversion to
    QPixmap, then a smooth scale. Measured on 12 Sep 2026 into a 960x540 pane:
    3.34 ms a frame at 1080p and 12.54 ms at 4K, the latter being 37.6% of a
    30 fps frame interval spent on the thread that also answers Stop. Shrinking
    first with INTER_AREA took those to 0.63 ms and 1.40 ms.

    Pinned by the pixmap's size: a frame larger than the pane must not produce
    a pixmap at capture size.
    """
    import numpy as np
    from wer.ui.preview import VideoSurface

    surface = VideoSurface()
    surface.resize(320, 180)

    surface.show_frame(np.zeros((2160, 3840, 3), dtype=np.uint8))

    pixmap = surface._pixmap
    assert pixmap is not None
    assert pixmap.width() <= 320 and pixmap.height() <= 180, (
        f"a 4K frame became a {pixmap.width()}x{pixmap.height()} pixmap; "
        "the conversion is still happening at capture size"
    )
    # The frame's own size is still reported, because the overlay editor places
    # widgets in frame pixels.
    assert surface._frame_size == (3840, 2160)


def test_a_frame_smaller_than_the_pane_is_not_enlarged(qt_app) -> None:
    """Shrinking only. Blowing a small frame up before Qt scales it would cost
    more than it saves and lose nothing but sharpness."""
    import numpy as np
    from wer.ui.preview import VideoSurface

    surface = VideoSurface()
    surface.resize(1920, 1080)
    surface.show_frame(np.zeros((240, 320, 3), dtype=np.uint8))

    assert surface._pixmap is not None
    assert surface._pixmap.width() == 320, "a small frame was resized on the way in"
