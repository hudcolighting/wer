"""Keeping Windows awake while recording (wer.core.power).

No Qt. The fake setter records what would have been asked of Windows; one test
makes the real call, and releases it straight away.
"""

from __future__ import annotations

import logging
import sys

import pytest

from wer.core.power import ES_CONTINUOUS, ES_SYSTEM_REQUIRED, KeepAwake


class FakeSetter:
    def __init__(self, answer: int = 0x80000000) -> None:
        self.calls: list[int] = []
        self.answer = answer

    def __call__(self, flags: int) -> int:
        self.calls.append(flags)
        return self.answer


def test_holding_asks_for_the_system_and_releasing_gives_it_back() -> None:
    setter = FakeSetter()
    awake = KeepAwake(setter)

    assert awake.hold() is True
    assert awake.held
    assert setter.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]

    awake.release()
    assert not awake.held
    assert setter.calls[-1] == ES_CONTINUOUS


def test_the_display_is_left_to_its_own_timeout() -> None:
    """ES_DISPLAY_REQUIRED (0x2) is deliberately not asked for."""
    setter = FakeSetter()
    KeepAwake(setter).hold()
    assert not setter.calls[0] & 0x00000002


def test_holding_twice_asks_once_and_releasing_unheld_asks_nothing() -> None:
    setter = FakeSetter()
    awake = KeepAwake(setter)
    awake.release()
    assert setter.calls == []
    awake.hold()
    awake.hold()
    assert setter.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED]
    awake.release()
    awake.release()
    assert setter.calls == [ES_CONTINUOUS | ES_SYSTEM_REQUIRED, ES_CONTINUOUS]


def test_a_refusal_is_logged_and_does_not_count_as_held(caplog) -> None:
    awake = KeepAwake(FakeSetter(answer=0))
    with caplog.at_level(logging.WARNING, logger="wer.core.power"):
        assert awake.hold() is False
    assert not awake.held
    assert "refused" in caplog.text


def test_with_nothing_to_call_it_does_nothing() -> None:
    awake = KeepAwake(setter=None)
    awake._setter = None  # as off Windows
    assert awake.hold() is False
    awake.release()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_the_real_call_is_accepted_on_windows() -> None:
    awake = KeepAwake()
    try:
        assert awake.hold() is True
    finally:
        awake.release()
    assert not awake.held
