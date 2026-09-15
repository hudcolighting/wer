"""Only one copy of Wer writes the settings file.

Wer rewrites the whole settings file on every autosave. Two copies open at
once therefore do not merge: whichever writes last silently discards
everything the other did. That is not hypothetical -- a layout built during a
session was lost exactly this way.
"""

from __future__ import annotations

import os
from pathlib import Path

from wer.core.instance import LOCK_NAME, claim_settings


def test_the_first_copy_gets_the_claim(tmp_path: Path) -> None:
    claim = claim_settings(tmp_path)
    assert claim.granted is True
    assert claim.may_save is True
    assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8").strip() == str(os.getpid())


def test_a_second_copy_runs_but_does_not_write(tmp_path: Path) -> None:
    """Deliberately not a refusal to start. A recorder that will not open, in
    a booth, ten minutes before a tech, is worse than a confusing settings
    file -- so the second copy is a passenger, not a casualty."""
    (tmp_path / LOCK_NAME).write_text("999999999", encoding="utf-8")

    # Pretend that pid is alive, which is what another running Wer looks like.
    import wer.core.instance as instance

    original = instance._process_alive
    instance._process_alive = lambda pid: True
    try:
        claim = claim_settings(tmp_path)
    finally:
        instance._process_alive = original

    assert claim.granted is False
    assert claim.may_save is False
    assert claim.held_by == 999999999


def test_a_claim_left_by_a_crash_is_taken_over(tmp_path: Path) -> None:
    """A force-kill leaves the file behind with nothing to protect. Demanding
    someone delete it by hand is not a thing to ask during a tech."""
    (tmp_path / LOCK_NAME).write_text("999999999", encoding="utf-8")

    import wer.core.instance as instance

    original = instance._process_alive
    instance._process_alive = lambda pid: False
    try:
        claim = claim_settings(tmp_path)
    finally:
        instance._process_alive = original

    assert claim.granted is True
    assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8").strip() == str(os.getpid())


def test_releasing_lets_the_next_copy_in(tmp_path: Path) -> None:
    first = claim_settings(tmp_path)
    assert first.granted
    first.release()
    assert not (tmp_path / LOCK_NAME).exists()
    assert claim_settings(tmp_path).granted is True


def test_releasing_a_claim_we_never_had_does_nothing(tmp_path: Path) -> None:
    """It must not delete the live holder's claim on the way out."""
    (tmp_path / LOCK_NAME).write_text("999999999", encoding="utf-8")

    import wer.core.instance as instance

    original = instance._process_alive
    instance._process_alive = lambda pid: True
    try:
        claim = claim_settings(tmp_path)
        claim.release()
    finally:
        instance._process_alive = original

    assert (tmp_path / LOCK_NAME).read_text(encoding="utf-8").strip() == "999999999"


def test_a_garbled_claim_file_is_not_fatal(tmp_path: Path) -> None:
    (tmp_path / LOCK_NAME).write_text("not a pid", encoding="utf-8")
    assert claim_settings(tmp_path).granted is True


def test_different_data_directories_never_contend(tmp_path: Path) -> None:
    """Scoped to the directory, so a test harness or a portable install
    pointed elsewhere is not blocked by the copy running normally."""
    one = claim_settings(tmp_path / "a")
    two = claim_settings(tmp_path / "b")
    assert one.granted and two.granted


def test_this_process_is_reported_alive() -> None:
    """The check has to be right in the direction that matters: mistaking a
    live instance for a dead one is what loses work."""
    from wer.core.instance import _process_alive

    assert _process_alive(os.getpid()) is True
