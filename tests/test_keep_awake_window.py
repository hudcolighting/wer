"""The main window keeps Windows awake for exactly as long as a take is in progress.

An unattended take on a laptop running on battery ended when the power plan
put the machine to sleep -- after 30 minutes on the development laptop -- and
Wer never asked it not to. See wer.core.power.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_a_take_in_progress_holds_windows_awake_and_ending_it_lets_go(qt_app) -> None:
    from wer.core.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake
    from wer.ui.main_window import MainWindow

    asked: list[int] = []
    window = MainWindow()
    try:
        window._keep_awake = KeepAwake(lambda flags: asked.append(flags) or 1)
        assert not window._take_in_progress

        window._take_in_progress = True
        assert window._keep_awake.held
        window._take_in_progress = True  # armed again: still one request
        window._take_in_progress = False
        assert not window._keep_awake.held

        assert asked == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]
    finally:
        window.close()


def test_nothing_ends_a_take_without_going_through_the_flag() -> None:
    """The request is only as good as the flag. Anything that armed or ended a
    take through another attribute would hold the machine awake for ever, or
    let it sleep mid-take, so the flag's backing store is written in exactly
    one place: the property setter."""
    source = Path("src/wer/ui/main_window.py").read_text(encoding="utf-8")
    writes = re.findall(r"self\._take_live\s*=", source)
    assert len(writes) == 1, f"_take_live is written {len(writes)} times"
