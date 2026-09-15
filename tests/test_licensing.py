"""Wer says what it is licensed under, to whoever is holding a copy.

Not paperwork. Wer is GPL v3 or later and bundles an ffmpeg that is the same,
so every copy handed to anyone carries two obligations: the terms have to be
visible in the application (section 5), and the source that built it has to be
gettable (section 6). Both are easy to satisfy and easy to break silently --
a renamed menu, a tidied import, a licence file left out of a build -- which is
what these guard.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

from wer import licensing

REPO = Path(__file__).resolve().parents[1]


def test_the_repository_ships_the_licence_it_claims() -> None:
    """A GPL program without its licence text is not distributable."""
    licence = REPO / "LICENSE"
    assert licence.is_file(), "no LICENSE at the root of the repository"

    text = licence.read_text(encoding="utf-8")
    assert "GNU GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text


def test_wer_and_the_ffmpeg_it_bundles_are_under_one_licence() -> None:
    """The point of choosing GPL v3 for Wer itself: one licence over the whole
    distribution, so nothing rests on which part is which."""
    ours = (REPO / "LICENSE").read_text(encoding="utf-8")
    theirs = (REPO / "vendor" / "ffmpeg" / "LICENSE").read_text(encoding="utf-8")

    assert ours == theirs, (
        "Wer's licence text and the bundled ffmpeg's have diverged; one of them "
        "is no longer the GNU GPL v3 as published"
    )


def test_the_packaging_metadata_agrees_with_the_notice() -> None:
    """pip, the wheel and anything reading the project's metadata should say
    the same thing the application says."""
    with (REPO / "pyproject.toml").open("rb") as handle:
        metadata = tomllib.load(handle)["project"]

    assert metadata["license"]["text"] == licensing.LICENCE_SPDX
    assert licensing.COPYRIGHT_HOLDER in metadata["authors"][0]["name"]
    assert metadata["authors"][0]["email"] == licensing.CONTACT_EMAIL


def test_the_notice_carries_what_section_5_asks_for() -> None:
    """Copyright, the freedom granted, the warranty disclaimed, and where the
    licence can be read -- in the Free Software Foundation's own wording.

    Read with the line breaks flattened: the wording is fixed, where it wraps
    is not, and a test that pins the wrapping would fail on a reflow that
    changed nothing that matters.
    """
    notice = " ".join(licensing.NOTICE.split())

    assert licensing.COPYRIGHT_HOLDER in notice
    assert "GNU General Public License" in notice
    assert "either version 3 of the License" in notice
    assert "WITHOUT ANY WARRANTY" in notice
    assert "https://www.gnu.org/licenses/" in notice


def test_anyone_handed_a_build_can_find_the_source() -> None:
    """Section 6(d): the source comes from the same place, at no charge.

    This replaced a written offer to email an address, which was not a route
    section 6 actually allows -- an offer is 6(b), for object code in a physical
    product, good for three years and stating its terms. A repository URL is
    both simpler and valid, and it has to appear everywhere the terms appear.
    """
    assert licensing.SOURCE_REPOSITORY.startswith("https://")
    assert licensing.SOURCE_REPOSITORY in licensing.SOURCE_OFFER
    assert licensing.SOURCE_OFFER in licensing.NOTICE
    assert licensing.SOURCE_OFFER in licensing.SUMMARY


def test_the_source_link_is_not_quietly_dropped() -> None:
    """A dead or missing link here is a licence breach, not a broken hyperlink.

    Wer is GPL v3 and ships as a binary, so the repository URL is the whole of
    its section 6 compliance. If someone replaces it with an email address
    again, or empties it, this fails.
    """
    assert "github.com/hudcolighting/wer" in licensing.SOURCE_REPOSITORY
    assert licensing.SOURCE_REPOSITORY in licensing.summary_html()


def test_the_short_form_says_it_too() -> None:
    """The About box gets the short form; it still has to state the terms."""
    summary = licensing.SUMMARY

    assert licensing.COPYRIGHT_HOLDER in summary
    assert "General Public License" in summary
    assert "NO WARRANTY" in summary


# --------------------------------------------------- what the application shows


@pytest.fixture()
def window(qt_app):
    """A real MainWindow. The device fakes come from conftest."""
    from wer.ui.main_window import MainWindow

    win = MainWindow()
    yield win
    win.close()
    win.deleteLater()


def test_the_about_box_states_the_terms(window) -> None:
    """The one place most people will ever look."""
    about = window._about_html()

    assert licensing.COPYRIGHT_HOLDER in about
    assert "General Public License" in about
    assert "no warranty" in about.lower()
    assert licensing.CONTACT_EMAIL in about


def test_the_licence_dialog_points_at_the_repository_not_an_inbox(
    window
) -> None:
    """Section 6(d) again, at the top of the dialog, where somebody who never
    scrolls will read it.

    Until 13 Sep 2026 the heading said "Source for Wer:" and gave the contact
    address. An address is not a route section 6 allows: an offer to send
    source on request is 6(b), for object code in a physical product, good for
    three years and stating its terms, and a bare email states none of that.
    The repository is the route Wer actually uses, so it is the one this line
    has to hand over. The address stays in the About box, where it is contact
    and not compliance.

    Read as links rather than as text, because the URL turning up somewhere
    in the string is also what a stray f-string would give. The href has to be
    the constant itself, and the words the link is shown as have to be the
    host and path, so they can be read off the screen of a machine that cannot
    open a browser to them.
    """
    heading = window._licence_heading_html()
    hrefs = re.findall(r"<a href=['\"]([^'\"]*)['\"]>", heading)

    assert licensing.SOURCE_REPOSITORY in hrefs, hrefs
    assert ">github.com/hudcolighting/wer</a>" in heading, (
        "the link text should be the host and path, readable off the screen"
    )
    assert licensing.CONTACT_EMAIL not in heading
    assert "https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz" in hrefs, (
        "the ffmpeg source link went with the email"
    )


def test_the_licence_dialog_carries_both_notices_and_the_licence_itself(
    window
) -> None:
    """Wer's notice, what it bundles, and the text of the licence -- read from
    the bundle at runtime so it cannot drift from the binary shipped."""
    text = window._licence_text()

    assert licensing.NOTICE in text
    assert "FFmpeg 8.1.2" in text
    assert "ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz" in text
    assert "GNU GENERAL PUBLIC LICENSE" in text, "the licence text itself is missing"
    assert "LGPL" in text, "Qt's terms are not stated"


def test_the_dialog_carries_qts_own_copyright_notice(window) -> None:
    r"""LGPL v3 section 4(c).

    Because Wer displays copyright notices while it runs, Qt's has to be among
    them, together with a pointer to where the licence copies can be read.
    Naming Qt is not the same as crediting whoever owns it.

    The pointer has to be a path that exists. Until 14 Sep 2026 it read
    "licences/qt/LGPL-3.0.txt", which is where the file sits in the repository
    and not where it sits in a build -- somebody following it in an installed
    copy would look beside the exe and find nothing, because PyInstaller puts
    it under _internal\. A pointer to the wrong place is worse than none: it
    reads as a licence text that was promised and left out.
    """
    text = window._licence_text()

    assert "The Qt Company Ltd. and other contributors" in text
    assert "_internal\\licences\\qt\\LGPL-3.0.txt" in text, (
        "no pointer to where the LGPL copy actually lives in a build"
    )


def test_the_dialog_reproduces_the_lgpl_in_full(window) -> None:
    """LGPL v3 section 4(b) asks for a copy of the GNU GPL *and* this licence.

    Wer shipped only the GPL until 11 Sep 2026, which is half of 4(b). The
    PySide6 wheel ships no LGPL text at all -- only a pointer to Qt's
    commercial terms -- so the copy in licences/qt/ is doing real work.
    """
    text = window._licence_text()

    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text


def test_the_dialog_names_everything_else_bundled(window) -> None:
    """The dialog once named three components out of a dozen. Everything that
    ships gets credited."""
    text = window._licence_text()

    for component in ("OpenCV", "CPython", "OpenSSL", "NumPy", "pygrabber", "comtypes"):
        assert component in text, f"{component} ships but is credited nowhere"


def test_the_dialog_accounts_for_opencvs_removed_ffmpeg(window) -> None:
    """One FFmpeg ships, and the dialog says what happened to the other.

    opencv-python bundles a second FFmpeg under LGPL v2.1 that Wer never
    called; build.ps1 deletes it after packaging. The dialog is read by people
    checking compliance, so the absence is worth stating rather than leaving
    them to wonder.
    """
    text = window._licence_text()

    assert "FFmpeg 8.1.2" in text
    assert "opencv_videoio_ffmpeg" in text


def test_the_environment_tab_says_which_commit_built_it(window) -> None:
    """Section 6 gives a URL; this says which commit at that URL.

    "The source is on GitHub" is only half an answer to somebody holding a
    binary: two builds of 0.9.2 a day apart are not the same program, and the
    one they have is the one they need the source for. build.ps1 stamps
    `git describe --always --dirty` into the payload as build-id.txt and the
    Environment tab shows it beside the version.

    A source checkout has no stamp and should not pretend to one -- git is
    right there, and a file left behind by an earlier build would be worse than
    nothing. That is the case this runs in, so it is the case pinned here.
    """
    from PySide6.QtWidgets import QFormLayout, QGroupBox

    from wer import __version__
    from wer.paths import build_id

    assert build_id() is None, "a source checkout has no build stamp to read"

    box = next(
        child
        for child in window.findChildren(QGroupBox)
        if child.title() == "Environment"
    )
    form = box.layout()
    rows = {
        form.itemAt(i, QFormLayout.ItemRole.LabelRole).widget().text():
        form.itemAt(i, QFormLayout.ItemRole.FieldRole).widget().text()
        for i in range(form.rowCount())
    }

    assert rows["Version:"] == __version__, rows
    assert rows["Built from:"] == "source checkout", rows


def test_the_licence_is_reachable_from_the_help_menu(window) -> None:
    """A notice nobody can open is not displayed. The item was called
    "Third-Party Licences" when only ffmpeg's applied; it covers Wer now."""
    labels = [
        action.text()
        for menu_action in window.menuBar().actions()
        if menu_action.menu() is not None
        for action in menu_action.menu().actions()
    ]

    assert any("Licences" in label for label in labels), labels
    assert not any("Third-Party" in label for label in labels), (
        "the menu still says Third-Party, but Wer's own licence is in there now"
    )
