"""The log banner describes the machine without slowing the launch.

platform.platform() answers from WMI on Python 3.12: 109-186 ms measured in
fresh processes on the development laptop, spent before the window appears, to
write one line of the log banner.
"""

from __future__ import annotations

import logging
import sys

import pytest

from wer import logging_setup


@pytest.mark.skipif(sys.platform != "win32", reason="Windows version lookup")
def test_the_banner_names_the_windows_build_without_asking_wmi(monkeypatch, caplog) -> None:
    import platform

    def refuse(*_args, **_kwargs):
        raise AssertionError("the banner went through platform.uname / WMI")

    for name in ("platform", "uname", "win32_ver", "machine", "version", "release"):
        monkeypatch.setattr(platform, name, refuse)

    with caplog.at_level(logging.INFO, logger="wer.logging_setup"):
        logging_setup._log_banner()

    line = next(r.getMessage() for r in caplog.records if r.getMessage().startswith("Platform"))
    assert str(sys.getwindowsversion().build) in line
    assert line.startswith("Platform Windows")
