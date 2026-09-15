"""The little picture of the layout on the Overlay tab.

Hudson asked for "a preview of the widget you are making when making a widget
on that page", and the danger in giving him one is that a preview is a second
renderer looking at the same widgets. These are mostly about the two ways that
goes wrong: the picture must be drawn from copies, so a small preview cannot
resize the cached image the capture thread is blending into the recording, and
it must cost nothing at all while the operator is on another tab.

The preset dialog's thumbnails are in test_layout_presets.py; the editor around
this in test_layout_editor.py.
"""

from __future__ import annotations

import logging
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QImage

from test_window_height import LAPTOP_WINDOW_HEIGHT
from wer.overlay.layout import default_layouts
from wer.overlay.sample import sample_frame
from wer.ui.widget_preview import RENDER_SIZE, SELECTION_COLOUR, frame_to_image

#: The outline colour as an RGB triple, to find in a rendered picture.
_GREEN = (0x27, 0xAE, 0x60)


@pytest.fixture()
def editor(qt_app):
    """A LayoutEditor showing the Tech layout, on screen but off the display.

    WA_DontShowOnScreen so the widget is genuinely visible as far as Qt is
    concerned -- which is what the preview decides whether to draw from --
    without a window appearing over whoever is running the tests.
    """
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.ui.layout_editor import LayoutEditor

    panel = LayoutEditor(Compositor(DataBus()))
    tech = next(layout for layout in default_layouts() if layout.name == "Tech")
    panel.compositor.apply_layout(tech.widgets)
    panel.refresh()
    panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
    panel.resize(900, 600)
    panel.show()
    qt_app.processEvents()
    yield panel
    panel.close()
    panel.deleteLater()


def _settle(qt_app, preview, timeout: float = 5.0) -> None:
    """Wait out the debounce, then let the render land."""
    deadline = time.monotonic() + timeout
    while preview.render_pending and time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)
    qt_app.processEvents()
    assert not preview.render_pending, "the preview never redrew"


def _pixels(image: QImage) -> np.ndarray:
    """A QImage as an (h, w, 3) RGB array, for counting pixels."""
    rgb = image.convertToFormat(QImage.Format.Format_RGB888)
    width, height = rgb.width(), rgb.height()
    flat = np.frombuffer(
        rgb.constBits(), dtype=np.uint8, count=height * rgb.bytesPerLine()
    )
    return flat.reshape(height, rgb.bytesPerLine() // 3, 3)[:, :width, :].copy()


def _green_bbox(image: QImage) -> tuple[int, int, int, int] | None:
    """Where the outline colour is, as (left, top, right, bottom)."""
    mask = (_pixels(image) == _GREEN).all(axis=2)
    if not mask.any():
        return None
    rows = np.flatnonzero(mask.any(axis=1))
    columns = np.flatnonzero(mask.any(axis=0))
    return (
        int(columns[0]), int(rows[0]), int(columns[-1]), int(rows[-1])
    )


# ------------------------------------------------------------------ drawing


def test_it_draws_the_layout_on_the_sample_stage(editor, qt_app) -> None:
    """A preview of an empty stage would say nothing about the layout."""
    preview = editor.preview
    _settle(qt_app, preview)

    drawn = preview.rendered_image()
    assert drawn is not None
    assert (drawn.width(), drawn.height()) == RENDER_SIZE
    bare = _pixels(frame_to_image(sample_frame(*RENDER_SIZE)))
    changed = int((_pixels(drawn) != bare).any(axis=2).sum())
    assert changed > 2000, f"only {changed} pixels differ from the bare stage"


def test_the_selected_widget_is_outlined_where_it_is(editor, qt_app) -> None:
    """The same green the Preview tab uses, around the rectangle the widget
    actually resolved to -- so the outline names the right widget."""
    editor.select("clock_date")
    _settle(qt_app, editor.preview)

    rect = editor.preview.selection_rect()
    assert rect is not None, "nothing was outlined"
    live = editor.compositor.get("clock_date")
    expected = live.rect(editor.preview._compositor.bus, *RENDER_SIZE)
    assert (rect.x, rect.y, rect.width, rect.height) == (
        expected.x, expected.y, expected.width, expected.height
    )

    box = _green_bbox(editor.preview.rendered_image())
    assert box is not None, f"no {SELECTION_COLOUR} pixels in the picture"
    left, top, right, bottom = box
    # Within a pixel or two: a two-pixel pen straddles the path it is drawn on.
    assert abs(left - rect.x) <= 2 and abs(top - rect.y) <= 2
    assert abs(right - (rect.x + rect.width)) <= 2
    assert abs(bottom - (rect.y + rect.height)) <= 2


def test_with_nothing_selected_the_layout_is_still_drawn(editor, qt_app) -> None:
    """Deselecting must not blank the picture: the layout as a whole is worth
    looking at, and an empty box beside the caption reads as a failure."""
    editor.select("clock_date")
    _settle(qt_app, editor.preview)
    editor.select("")
    _settle(qt_app, editor.preview)

    assert editor.preview.selection_rect() is None
    assert _green_bbox(editor.preview.rendered_image()) is None
    bare = _pixels(frame_to_image(sample_frame(*RENDER_SIZE)))
    changed = int((_pixels(editor.preview.rendered_image()) != bare).any(axis=2).sum())
    assert changed > 2000, "the layout went with the selection"


def test_an_edit_reaches_the_picture(editor, qt_app) -> None:
    """The whole point: change what a widget says and see it, without going to
    the Preview tab to look."""
    editor.select("show_name")
    _settle(qt_app, editor.preview)
    before = _pixels(editor.preview.rendered_image())

    editor._content.setText("MARKED FOR THE PREVIEW")
    assert editor.preview.render_pending, "the edit never asked for a redraw"
    _settle(qt_app, editor.preview)

    after = _pixels(editor.preview.rendered_image())
    assert (before != after).any(), "the picture did not follow the edit"


def test_a_burst_of_keystrokes_is_one_redraw(editor, qt_app) -> None:
    """Rendering per keystroke would put a full composite between the key and
    the letter appearing. The debounce gathers them up -- but it must not be a
    restart, or fast typing holds the picture off for as long as it lasts."""
    editor.select("show_name")
    _settle(qt_app, editor.preview)

    for index in range(10):
        editor._content.setText("Quiet Neighbours" + "!" * index)
    assert editor.preview.render_pending

    _settle(qt_app, editor.preview)
    assert editor.preview.rendered_image() is not None


# ----------------------------------------------------------- costing nothing


def test_it_draws_nothing_while_the_tab_is_away(qt_app) -> None:
    """The Overlay tab is one page of a tab widget, so this is the usual state.
    A preview redrawing behind a hidden tab is pure cost during a take."""
    from wer.core.databus import DataBus
    from wer.overlay.compositor import Compositor
    from wer.ui.layout_editor import LayoutEditor

    panel = LayoutEditor(Compositor(DataBus()))
    try:
        tech = next(layout for layout in default_layouts() if layout.name == "Tech")
        panel.compositor.apply_layout(tech.widgets)
        panel.refresh()
        panel.request_preview()

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            qt_app.processEvents()
            time.sleep(0.005)

        assert panel.preview.rendered_image() is None, "it drew while hidden"
        assert panel.preview.render_pending, "the request was thrown away"

        panel.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        panel.show()
        qt_app.processEvents()
        # At once on being shown, not after the debounce: coming back to the
        # tab must not show the picture from before the change.
        assert panel.preview.rendered_image() is not None
        assert not panel.preview.render_pending
    finally:
        panel.close()
        panel.deleteLater()


def test_hiding_the_tab_stops_a_redraw_already_asked_for(editor, qt_app) -> None:
    editor.hide()
    qt_app.processEvents()
    editor.request_preview()
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        qt_app.processEvents()
        time.sleep(0.005)

    assert editor.preview.render_pending, "the timer was still running"


# ------------------------------------------------------- copies, not the live ones


def test_the_preview_draws_copies_of_the_live_widgets(editor, qt_app) -> None:
    """Sharing the objects would mean the preview and the recording fighting
    over one cached image."""
    _settle(qt_app, editor.preview)

    live = editor.compositor.get("show_name")
    copy = editor.preview._compositor.get("show_name")
    assert copy is not None
    assert copy is not live
    assert copy.template == live.template

    copy.template = "EDITED ON THE COPY"
    assert live.template != "EDITED ON THE COPY"


def test_rendering_the_preview_leaves_the_recorded_image_alone(editor, qt_app) -> None:
    """The failure this guards against is silent and it is on the recording:
    a widget rendered for a 640x360 preview hands the capture thread back a
    third-size cached image to blend into a 1080p frame."""
    _settle(qt_app, editor.preview)
    live = editor.compositor.get("show_name")
    recorded = live.image(editor.compositor.bus, 1080, 1920)
    assert recorded is not None
    size = (recorded.width(), recorded.height())

    editor.preview.render_now()

    again = live.image(editor.compositor.bus, 1080, 1920)
    assert (again.width(), again.height()) == size
    assert again is recorded, "the live widget was re-rendered for the preview"


def test_a_copy_outlives_a_redraw_that_did_not_change_it(editor, qt_app) -> None:
    """Rebuilding every copy on every render is the obvious way to write this
    and the wrong one. A copy carries the decoded picture file, so a rebuild
    twelve times a second reads the logo off the disk that often -- and, when
    the logo has gone missing, writes its warning into the log beside the exe
    that often too."""
    _settle(qt_app, editor.preview)
    drawn = editor.preview._compositor.get("show_name")

    editor.preview.render_now()

    assert editor.preview._compositor.get("show_name") is drawn

    editor.select("show_name")
    editor._content.setText("A different show entirely")
    _settle(qt_app, editor.preview)
    assert editor.preview._compositor.get("show_name") is not drawn, (
        "an edited widget was not rebuilt"
    )


def test_a_capture_thread_composites_right_through_a_burst_of_previews(
    editor, qt_app
) -> None:
    """The same guarantee as the test above, made against a thread that is
    actually compositing while the preview draws -- which is the only shape
    the failure ever takes in the booth. A widget handed back at a third of
    the size it was blending at does not raise anything; it is just on the
    recording, and only afterwards.
    """
    compositor = editor.compositor
    errors: list[BaseException] = []
    frames = {"n": 0}
    stop = threading.Event()
    frame = np.zeros((1080, 1920, 3), dtype=np.uint8)

    def capture() -> None:
        try:
            while not stop.is_set():
                compositor.composite(frame, in_place=True)
                frames["n"] += 1
                time.sleep(0.001)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            errors.append(exc)

    thread = threading.Thread(target=capture, daemon=True)
    thread.start()
    try:
        editor.select("show_name")
        for index in range(20):
            editor._content.setText("The Quiet Neighbours " + "!" * index)
            editor.preview.render_now()
            qt_app.processEvents()
    finally:
        stop.set()
        thread.join(timeout=5)

    assert not errors, f"the capture thread failed: {errors[0]!r}"
    assert frames["n"] > 0, "the capture thread never ran, so this proves nothing"
    # Asked of the compositor rather than of every widget: one whose condition
    # is false on an empty bus is never drawn at all, and has no size to check.
    blended = [w for w in compositor.widgets if compositor.widget_size(w.id)]
    assert blended, "nothing was blended, so this proves nothing"
    for widget in compositor.widgets:
        assert widget._rendered_for != (360, 640), (
            f"{widget.id} was re-rendered at the preview's size"
        )
    for widget in blended:
        assert widget._rendered_for == (1080, 1920), (
            f"{widget.id} is cached for {widget._rendered_for}, not the frame "
            "being recorded"
        )
    for copy in editor.preview._compositor.widgets:
        assert copy._rendered_for == (360, 640), (
            f"the preview's {copy.id} is cached for {copy._rendered_for}"
        )


def _watermark(editor, path: str):
    """Put a picture widget in the layout and select it."""
    from wer.overlay.widgets import ImageWidget

    widget = ImageWidget("mark", path, height=0.10)
    editor.compositor.add(widget)
    editor.refresh()
    editor.select("mark")
    return widget


def _a_png(tmp_path, name: str, colour: str, size: tuple[int, int]):
    file = tmp_path / name
    image = QImage(*size, QImage.Format.Format_ARGB32)
    image.fill(QColor(colour))
    assert image.save(str(file))
    return file


def test_moving_a_watermark_does_not_go_back_to_the_disk(
    editor, qt_app, tmp_path
) -> None:
    """Keeping a copy until its settings change is only half of it, because
    moving a widget IS a change. Twenty arrow keys down one side of the stage
    used to be twenty decodes of the logo, once per copy rebuilt."""
    logo = _a_png(tmp_path, "logo.png", "#ff8800", (64, 32))
    _watermark(editor, str(logo))
    _settle(qt_app, editor.preview)

    decoded = editor.preview._compositor.get("mark")._source
    assert decoded is not None, "the logo never loaded in the first place"

    for _ in range(20):
        editor.nudge("mark", 0.004, 0.0)
        editor.preview.render_now()

    assert editor.preview._compositor.get("mark")._source is decoded, (
        "the logo was read off the disk again while the widget was moved"
    )


def test_moving_a_watermark_that_has_gone_missing_warns_once(
    editor, qt_app, tmp_path, caplog
) -> None:
    """The one that matters. A logo left behind on a show server that is not
    there tonight writes "Watermark ...: No file at ..." once -- not once per
    mouse move, into the log beside the exe that gets read after a bad take."""
    _watermark(editor, str(tmp_path / "not-here.png"))
    _settle(qt_app, editor.preview)

    with caplog.at_level(logging.WARNING, logger="wer.overlay.widgets"):
        caplog.clear()
        for _ in range(20):
            editor.nudge("mark", 0.004, 0.0)
            editor.preview.render_now()
        complaints = [r for r in caplog.records if "Watermark" in r.getMessage()]

    assert len(complaints) <= 1, (
        f"{len(complaints)} warnings for one missing logo being moved"
    )


def test_pointing_a_watermark_somewhere_else_does_read_the_new_file(
    editor, qt_app, tmp_path
) -> None:
    """The other half: carrying the decoded file across must not mean a widget
    that can never be pointed at a different picture."""
    first = _a_png(tmp_path, "one.png", "#ff8800", (64, 32))
    second = _a_png(tmp_path, "two.png", "#0088ff", (40, 20))
    _watermark(editor, str(first))
    _settle(qt_app, editor.preview)
    assert editor.preview._compositor.get("mark")._source.size() == QSize(64, 32)

    editor._image_path.setText(str(second))
    editor._image_path_typed()
    _settle(qt_app, editor.preview)

    assert editor.preview._compositor.get("mark")._source.size() == QSize(40, 20), (
        "the preview went on drawing the picture that was there before"
    )


def test_a_removed_widget_leaves_the_picture(editor, qt_app) -> None:
    """Nothing about the widgets that are left has changed, so a preview that
    only watched for edits would go on drawing the one that has gone."""
    editor.select("clock_date")
    editor._remove.click()
    _settle(qt_app, editor.preview)

    assert editor.preview._compositor.get("clock_date") is None
    assert [w.id for w in editor.preview._compositor.widgets] == [
        w.id for w in editor.compositor.widgets
    ]


def test_the_preview_has_a_bus_of_its_own(editor) -> None:
    """A made-up cue number must have no way of reaching a recording."""
    assert editor.preview._compositor.bus is not editor.compositor.bus
    assert editor.preview._compositor.bus.value("eos.cue.active.number") == "58"
    assert "eos.cue.active.number" not in editor.compositor.bus


# ------------------------------------------------------------------ the tab fits


def test_the_overlay_tab_still_fits_a_laptop_screen(editor) -> None:
    """A picture at the top of the property sheet is the obvious way to push
    the window past the height of the screen it has to run on; see
    tests/test_window_height.py, which checks the window as a whole."""
    needed = editor.minimumSizeHint().height()
    assert needed <= LAPTOP_WINDOW_HEIGHT, (
        f"the Overlay tab cannot be made shorter than {needed} px"
    )
