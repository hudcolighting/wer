"""The notices have to be right, and they have to keep shipping.

The licence audit on 11 Sep 2026 found that most of what Wer bundles was
neither declared nor attributed, and that the one thing keeping numpy's
notices in the build was PyInstaller happening to collect its dist-info. These
tests exist so that stays fixed: an upgrade that renames a licence file, or an
edit that drops a component from the notices, fails here rather than in
somebody's hands.

The Qt trim of 13 Sep 2026 added a second way for the two to drift. build.ps1
now deletes the Qt binaries Wer never loads and keeps an allowlist of what may
remain, and the notices describe exactly that set. So the tests at the end
check the three against each other: the build still drops what the notices
say is gone, keeps what the source needs, and the notices do not present as
shipping anything the build removes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
NOTICES = REPO / "THIRD-PARTY-NOTICES.md"
BUILD = REPO / "build.ps1"


@pytest.fixture(scope="module")
def notices() -> str:
    return NOTICES.read_text(encoding="utf-8")


def test_notices_file_ships_in_the_repo() -> None:
    assert NOTICES.is_file(), "THIRD-PARTY-NOTICES.md is what discharges the attribution"


@pytest.mark.parametrize(
    "component",
    [
        "Qt 6.9.3",
        "PySide6-Essentials",
        "shiboken6",
        "CPython 3.12.10",
        "OpenSSL",
        "opencv-python",
        "numpy",
        "OpenBLAS",
        "libgfortran",
        "pygrabber",
        "comtypes",
        "FFmpeg 8.1.2",
        "Visual C++",
    ],
)
def test_every_bundled_component_is_named(notices: str, component: str) -> None:
    """Each thing that ships is named. Silence is the failure mode here."""
    assert component in notices


@pytest.mark.parametrize(
    ("licence", "why"),
    [
        ("GNU General Public License v3", "Wer itself and the ffmpeg it records with"),
        ("Lesser General Public License version 3", "Qt, and the reason the DLLs stay loose"),
        ("Apache License 2.0", "OpenCV and OpenSSL"),
        ("Python Software Foundation License", "CPython"),
        ("BSD 3-Clause", "numpy and OpenBLAS"),
        ("MIT", "pygrabber, comtypes, and the opencv-python shim"),
    ],
)
def test_every_licence_in_play_is_named(notices: str, licence: str, why: str) -> None:
    assert licence in notices, f"{licence} must be named: {why}"


def test_the_notices_explain_the_ffmpeg_that_was_removed(notices: str) -> None:
    """One FFmpeg ships; the notices say why the other one does not.

    opencv-python bundles a second, unrelated FFmpeg under LGPL v2.1. Wer never
    called it, and build.ps1 deletes it after packaging. Saying so is what stops
    the next person re-adding it, or believing the payload still carries LGPL
    terms it does not.
    """
    assert "FFmpeg 8.1.2" in notices, "the ffmpeg Wer records with"
    assert "opencv_videoio_ffmpeg" in notices, "the removal is not explained"
    assert "deletes it after packaging" in notices


def test_the_build_removes_opencvs_ffmpeg() -> None:
    """The removal has to survive somebody tidying build.ps1."""
    build = BUILD.read_text(encoding="utf-8")
    assert "opencv_videoio_ffmpeg*.dll" in build, "the stray ffmpeg is no longer removed"
    assert "-Recurse" in build[build.index("$strayFfmpeg"):][:400], (
        "it lives in _internal\\cv2\\, so a non-recursive search silently does nothing"
    )


def test_qt_licence_text_is_kept_because_upstream_ships_none() -> None:
    """PySide6's wheel carries no LGPL text, so this copy is load-bearing."""
    lgpl = REPO / "licences" / "qt" / "LGPL-3.0.txt"
    assert lgpl.is_file(), "LGPL v3 section 4(b) needs this copy to accompany the build"
    text = lgpl.read_text(encoding="utf-8")
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 29 June 2007" in text


def test_gpl_licence_text_is_present() -> None:
    assert (REPO / "LICENSE").is_file()


def test_the_build_collects_every_licence_it_promises() -> None:
    r"""build.ps1's licence table must cover what the notices claim ships.

    The notices say the texts live in _internal\licences\. This checks the
    build is actually told to put them there.
    """
    build = BUILD.read_text(encoding="utf-8")
    table = re.search(r"\$licences = @\((.*?)\n\)", build, re.S)
    assert table, "build.ps1 no longer has a $licences table"
    body = table.group(1)
    for destination in (
        "licences/opencv",
        "licences/pygrabber",
        "licences/comtypes",
        "licences/python",
        "licences/qt",
    ):
        assert destination in body, f"{destination} is promised in the notices but not collected"


def test_a_missing_licence_text_fails_the_build_rather_than_warning() -> None:
    """A build that drops a licence text must not be shippable."""
    build = BUILD.read_text(encoding="utf-8")
    assert "Licence text missing:" in build
    guard = build[build.index("foreach ($l in $licences)"):]
    assert "throw" in guard[:600], "a warning is not enough; it has to stop the build"


def _licence_table(build: str) -> str:
    table = re.search(r"\$licences = @\((.*?)\n\)", build, re.S)
    assert table, "build.ps1 no longer has a $licences table"
    return table.group(1)


def test_every_licence_text_kept_in_the_repository_is_shipped() -> None:
    r"""Nothing sits in licences/ without reaching the build.

    The six texts fetched on 14 Sep 2026 are for code compiled into the Qt DLLs
    and into the interpreter's binaries. There is no package for build.ps1 to
    copy them out of, so the only thing that puts them in a payload is the
    $licences table naming them one by one -- and a file added to the tree and
    not to the table is the worst of both: the obligation acknowledged in the
    repository and discharged in nothing anybody is handed. It is also
    invisible, because the tree looks right.

    Destinations are checked too, not just names. Two files called COPYING land
    in the same folder if the To is wrong, and the second one wins.
    """
    body = _licence_table(BUILD.read_text(encoding="utf-8"))
    root = REPO / "licences"

    texts = [
        path
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name not in {"README.md", "SOURCES.md"}
    ]
    assert texts, "licences/ is empty, which it has never been"

    for path in texts:
        relative = path.relative_to(root)
        source = "licences\\" + str(relative).replace("/", "\\")
        destination = "licences/" + relative.parent.as_posix()

        lines = [line for line in body.splitlines() if f"'{source}'" in line]
        assert lines, f"{source} is kept in the repository but never shipped"
        assert f"To = '{destination}'" in lines[0], (
            f"{source} ships to the wrong place: {lines[0].strip()}"
        )


def test_the_dist_info_licence_paths_are_matched_not_written_out() -> None:
    """Wheel versions live in the directory name, so a pinned path goes stale.

    pygrabber-0.2.dist-info and comtypes-1.4.16.dist-info were written out in
    full until 14 Sep 2026. The failure that produces is a build throwing
    "licence text missing" at a directory nobody has deleted -- it was renamed
    by a pip install, and the message sends whoever reads it looking for the
    wrong thing. A pattern survives the upgrade. It still has to match exactly
    one file: none means the text is not there to ship, and two means nobody
    can say which of them describes what is being shipped.
    """
    build = BUILD.read_text(encoding="utf-8")

    assert "pygrabber-0.2.dist-info" not in build, "the version is pinned into the path again"
    assert "comtypes-1.4.16.dist-info" not in build

    body = _licence_table(build)
    assert "pygrabber-*.dist-info" in body
    assert "comtypes-*.dist-info" in body

    resolver = build[build.index("function Resolve-BundledFile"):]
    assert resolver.count("throw") >= 2, (
        "zero matches and more than one must both stop the build"
    )


def test_the_ffmpeg_build_record_ships_with_the_binary() -> None:
    """A static ffmpeg is a list of libraries nobody can read out of the exe.

    gyan's README.txt is that list: the version, the source commit, the
    configuration FFmpeg reports and all 42 external libraries. The
    notices point at it rather than copying it out, which only works if it is
    there -- and only means anything if the two still agree. A swapped ffmpeg
    brings a new README with a new commit in it, and the notices would go on
    naming the old one, which is the Corresponding Source somebody is told to
    go and fetch.
    """
    record = REPO / "vendor" / "ffmpeg" / "README.txt"
    assert record.is_file()
    published = record.read_text(encoding="utf-8", errors="replace")

    build = BUILD.read_text(encoding="utf-8")
    assert "vendor\\ffmpeg\\README.txt" in build, "the build record is not shipped"

    notices = NOTICES.read_text(encoding="utf-8")
    assert "_internal\\vendor\\ffmpeg\\README.txt" in notices
    assert "38b88335f9" in notices, "the commit the binary was built from"
    assert "38b88335f9" in published, (
        "the notices name a commit the shipped build record does not"
    )
    assert "8.1.2-essentials_build" in published

    # The five that make the whole binary GPL rather than LGPL. The notices
    # name them; the build record is where that can be checked.
    for gpl in ("libx264", "libx265", "libxvid", "libvidstab", "librubberband"):
        assert gpl in notices, f"{gpl} is why this ffmpeg is GPL, and is not named"
        assert gpl in published, f"{gpl} is named but is not in this build"


def test_numpys_notices_are_checked_after_the_build() -> None:
    """The one set of notices that ships by habit rather than by instruction.

    Nothing in build.ps1 copies them: PyInstaller collects numpy's dist-info
    and they come along inside it. That was the audit's finding on 11 Sep 2026,
    and a hook change is all it would take to end it silently -- taking with it
    the only notice covering numpy and everything vendored into it. So the
    build looks for them afterwards and stops if they are gone, which is the
    same treatment a missing licence text gets, for the same reason.
    """
    build = BUILD.read_text(encoding="utf-8")
    assert "numpy-*.dist-info\\licenses\\LICENSE.txt" in build, (
        "nothing checks that numpy's notices survived the build"
    )
    guard = build[build.index("$numpyNotices ="):]
    assert "throw" in guard[:900], "a warning is not enough; it has to stop the build"
    assert "Licence text missing:" in guard[:900]


def test_the_zip_carries_the_licence_and_the_notices_at_its_root() -> None:
    r"""The two ways of handing Wer over must not differ in what is visible.

    The installer lays LICENSE and THIRD-PARTY-NOTICES.md beside the exe --
    LGPL v3 section 4(a) asks for prominent notice with each copy, not notice
    inside the running program. PyInstaller puts them in _internal\, which is
    the folder a recipient is told to leave alone, so -Package adds them at the
    root of the zip as well.
    """
    build = BUILD.read_text(encoding="utf-8")
    package = build[build.index("if ($Package)"):]
    zipped = package[: package.index("Compress-Archive")]

    assert "'LICENSE'" in zipped, "the zip carries no licence at its root"
    assert "'THIRD-PARTY-NOTICES.md'" in zipped, "the zip carries no notices at its root"

    iss = (REPO / "installer" / "wer.iss").read_text(encoding="utf-8")
    assert 'Source: "..\\LICENSE"; DestDir: "{app}"' in iss
    assert 'Source: "..\\THIRD-PARTY-NOTICES.md"; DestDir: "{app}"' in iss


# --- The Qt trim -----------------------------------------------------------


def _powershell_list(build: str, name: str) -> str:
    """The body of the `$name = @( ... )` list in build.ps1.

    The closing paren is the first line that holds nothing but one, which is
    how PowerShell's own parser would find it too; the entries end in quotes
    or braces, never in a bare paren.
    """
    found = re.search(r"\$" + name + r" = @\((.*?)\n\s*\)", build, re.S)
    assert found, f"build.ps1 no longer has a ${name} list"
    return found.group(1)


def _dropped(build: str) -> set[str]:
    """The name of each $qtDrop entry: the quoted Path values, not the comments.

    The comments beside the entries name the same DLLs, and a comment outlives
    the entry it explained. Searching the whole list body kept these tests
    green with the Qt6Network.dll entry deleted and its comment left standing
    (tried on 13 Sep 2026), which is exactly the tidying they exist to catch.
    The folder is dropped so a test can ask for qtiff.dll without caring
    which plugin folder it sits in.
    """
    paths = re.findall(r"Path\s*=\s*'([^']+)'", _powershell_list(build, "qtDrop"))
    return {p.rsplit("\\", 1)[-1] for p in paths}


def _kept(build: str) -> set[str]:
    """The name of each $qtKeep entry: the quoted lines, comments left out."""
    paths = re.findall(r"^\s*'([^']+)'\s*$", _powershell_list(build, "qtKeep"), re.M)
    return {p.rsplit("\\", 1)[-1] for p in paths}


@pytest.mark.parametrize(
    "binary",
    ["opengl32sw.dll", "Qt6Network.dll", "Qt6Svg.dll", "qtiff.dll", "translations"],
)
def test_the_build_drops_the_qt_binaries_wer_never_loads(binary: str) -> None:
    """The notices say these are gone; the build has to keep them gone.

    Each is one that carries a licence of its own -- Mesa and LLVM, LibTIFF --
    or is the size of the whole rest of Qt's plugins put together. Tidying
    one out of $qtDrop would put a binary back that the notices describe as
    removed, which is the drift these tests are for.
    """
    assert binary in _dropped(BUILD.read_text(encoding="utf-8"))


def test_a_qt_binary_the_notices_do_not_describe_fails_the_build() -> None:
    """The allowlist is what makes the notices a description and not a hope.

    A PySide6 upgrade that adds a DLL has to land as a build failure naming
    the file, for the same reason a missing licence text does: a build that
    ships a Qt binary the notices do not describe cannot be handed to anyone.
    """
    build = BUILD.read_text(encoding="utf-8")
    assert "$qtKeep = @(" in build, "the allowlist is gone"
    guard = build[build.index("$qtUnexpected"):]
    assert "throw" in guard[:1200], "a warning is not enough; it has to stop the build"
    assert "not described by THIRD-PARTY-NOTICES.md" in guard[:1200]


# Qt reads PNG, BMP and the netpbm/X bitmap family with code built into
# Qt6Gui; everything else is a plugin. Only the formats the picker can offer
# are listed, so a new one has to be classified here on purpose.
_BUILT_INTO_QTGUI = {"png", "bmp", "pbm", "pgm", "ppm", "xbm", "xpm"}
_NEEDS_PLUGIN = {
    "jpg": "qjpeg.dll",
    "jpeg": "qjpeg.dll",
    "gif": "qgif.dll",
    "webp": "qwebp.dll",
    "ico": "qico.dll",
    "svg": "qsvg.dll",
    "tif": "qtiff.dll",
    "tiff": "qtiff.dll",
    "icns": "qicns.dll",
    "tga": "qtga.dll",
    "wbmp": "qwbmp.dll",
}


def test_the_keep_list_covers_every_format_the_picture_picker_offers() -> None:
    """The picker is the promise; $qtKeep is what makes it true in a build.

    Read from the source rather than pinned here, so that adding *.tiff to the
    picker without keeping qtiff.dll fails in this test and not in the layout
    editor of somebody who chose a TIFF and was told it is not an image Wer
    can read.
    """
    source = (REPO / "src" / "wer" / "ui" / "layout_editor.py").read_text(encoding="utf-8")
    picker = re.search(r'"Images \(([^)]*)\)', source)
    assert picker, "the picture widget's file picker filter has moved"
    extensions = {e.strip().lower().removeprefix("*.") for e in picker.group(1).split()}
    assert extensions, "the picker offers nothing"

    keep = _kept(BUILD.read_text(encoding="utf-8"))
    for extension in sorted(extensions):
        if extension in _BUILT_INTO_QTGUI:
            continue
        plugin = _NEEDS_PLUGIN.get(extension)
        assert plugin, f"*.{extension} is offered but not classified in this test"
        assert plugin in keep, (
            f"the picker offers *.{extension}, which needs {plugin}, "
            f"and build.ps1 no longer keeps it"
        )


def test_the_window_icon_is_an_ico_so_qico_is_kept() -> None:
    """Qt reads .ico through a plugin, and the app icon is one."""
    source = (REPO / "src" / "wer" / "paths.py").read_text(encoding="utf-8")
    icon = source[source.index("def icon_path"):]
    icon = icon[: icon.index("\ndef ")]
    names = re.findall(r'"([^"]+\.[A-Za-z]+)"', icon)
    assert names, "icon_path() no longer names its candidate files"
    assert all(n.endswith(".ico") for n in names), names
    assert "qico.dll" in _kept(BUILD.read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "library",
    ["libjpeg-turbo", "libwebp", "FreeType", "HarfBuzz", "libpng", "PCRE2", "zlib"],
)
def test_the_code_built_into_the_qt_that_ships_is_named(notices: str, library: str) -> None:
    """The seven libraries inside the Qt binaries that survive the trim."""
    assert library in notices


@pytest.mark.parametrize(
    ("statement", "licence"),
    [
        (
            "Portions of this software are copyright (C) The FreeType Project "
            "(www.freetype.org). All rights reserved.",
            "the FreeType License",
        ),
        (
            "this software is based in part on the work of the Independent JPEG Group",
            "the IJG licence",
        ),
    ],
)
def test_the_statements_the_licences_prescribe_are_carried(
    notices: str, statement: str, licence: str
) -> None:
    """Two of Qt's bundled licences dictate a sentence for binary distributions.

    Compared with line breaks folded, because the notices wrap at 80 columns
    and the sentence does not.
    """
    folded = " ".join(notices.split())
    assert statement in folded, f"{licence} requires this sentence verbatim"


def _paragraphs(notices: str) -> list[str]:
    return [" ".join(p.split()) for p in notices.split("\n\n") if p.strip()]


def test_the_notices_explain_the_qt_that_was_removed(notices: str) -> None:
    """Same pattern as the ffmpeg: say what build.ps1 deletes, and why."""
    removal = [p for p in _paragraphs(notices) if "opengl32sw.dll" in p]
    assert len(removal) == 1, "opengl32sw.dll should be named once, in the removal paragraph"
    assert "deletes" in removal[0] and "after packaging" in removal[0]
    for gone in ("Qt6Network.dll", "Qt6Svg.dll", "translations"):
        assert gone in removal[0], f"the removal paragraph no longer names {gone}"


@pytest.mark.parametrize(
    "removed",
    ["LibTIFF", "Mesa", "LLVM", "opengl32sw.dll", "Qt6Network.dll", "Qt6Svg.dll"],
)
def test_the_notices_do_not_present_removed_code_as_shipping(notices: str, removed: str) -> None:
    """What the build deletes may be named only to say it is gone.

    A paragraph that names one of them has to be a paragraph about deletion.
    Otherwise the notices claim obligations the payload no longer carries,
    which is the mirror image of the drift the rest of this file guards
    against, and just as misleading to whoever reads them.
    """
    mentions = [p for p in _paragraphs(notices) if removed in p]
    assert mentions, f"{removed} should still be named, as removed"
    for paragraph in mentions:
        assert "delet" in paragraph, f"{removed} is presented as shipping: {paragraph[:120]}"
