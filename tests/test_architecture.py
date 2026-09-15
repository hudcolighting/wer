"""Guards on the package layering.

These are not style checks. "I want to be able to test a protocol parser with
no Qt in the process" is a hard requirement, and the way it gets broken is
someone adding a convenience import at the top of a parser months from now.
Catching that here costs nothing; catching it when the parser needs a display
server costs an afternoon.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Packages that must stay free of Qt.
QT_FREE_PACKAGES = ["wer.core", "wer.connections", "wer.video"]

#: Packages that must not be reachable from the Qt-free ones.
UI_PACKAGES = ["wer.overlay", "wer.ui"]


@pytest.mark.parametrize("package", QT_FREE_PACKAGES)
def test_package_imports_without_qt(package: str) -> None:
    """Importing these must not pull PySide6 into the process.

    Run in a subprocess: once Qt is imported by any other test in the session,
    an in-process check would pass no matter what.
    """
    script = (
        "import sys, importlib\n"
        f"importlib.import_module({package!r})\n"
        "leaked = sorted(m for m in sys.modules if m.split('.')[0] "
        "in {'PySide6', 'shiboken6'})\n"
        "print(';'.join(leaked))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, f"{package} failed to import:\n{result.stderr}"
    leaked = result.stdout.strip()
    assert not leaked, f"{package} pulled Qt into the process: {leaked}"


@pytest.mark.parametrize("package", QT_FREE_PACKAGES)
def test_package_does_not_reach_ui(package: str) -> None:
    """No import path from the Qt-free packages up into overlay or ui."""
    script = (
        "import sys, importlib\n"
        f"importlib.import_module({package!r})\n"
        f"bad = sorted(m for m in sys.modules if any("
        f"m == p or m.startswith(p + '.') for p in {UI_PACKAGES!r}))\n"
        "print(';'.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    reached = result.stdout.strip()
    assert not reached, f"{package} imported UI code: {reached}"


def test_the_version_is_declared_once_as_far_as_anyone_can_tell() -> None:
    """pyproject and the package must agree.

    They are read by different things: pip and the wheel take the metadata,
    while build.ps1 -Package names the handover zip from wer.__version__. Two
    numbers that disagree means a zip whose name is not the version inside it,
    which is the sort of thing nobody notices until somebody quotes the wrong
    one back at you.
    """
    import tomllib

    import wer

    root = Path(__file__).resolve().parents[1]
    with (root / "pyproject.toml").open("rb") as handle:
        declared = tomllib.load(handle)["project"]["version"]

    assert declared == wer.__version__


def test_the_readme_describes_the_version_it_ships_with() -> None:
    """The README said "M0 - environment and packaging skeleton ... No features
    yet" for months after every feature had shipped, which is what a new reader
    met first. Two claims are worth pinning: the version it names, and that it
    no longer says the app does nothing.
    """
    import wer

    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
        encoding="utf-8"
    )

    assert wer.__version__ in readme, (
        f"the README does not mention version {wer.__version__}"
    )
    assert "No features yet" not in readme



def test_the_build_stamps_in_the_commit_it_was_made_from() -> None:
    r"""Three pieces that only work together, in three different files.

    build.ps1 runs `git describe --always --dirty`, writes it under build\,
    and ships it as build-id.txt; wer.paths.build_id() reads it back; the
    Environment tab shows it. Any one of them dropped leaves the other two
    looking fine -- the build still succeeds, the tab still renders, and the
    row quietly says "source checkout" in a frozen exe, which is the one
    reading it must never give. So the chain is pinned rather than each link.
    """
    root = Path(__file__).resolve().parent.parent
    build = (root / "build.ps1").read_text(encoding="utf-8")

    assert "describe --always --dirty" in build, "the build no longer records a commit"
    assert "rev-parse --abbrev-ref HEAD" in build, "and no longer records the branch"
    assert "'--add-data', \"$buildIdFile;.\"" in build, (
        "the stamp is written but never reaches the payload"
    )

    paths = (root / "src" / "wer" / "paths.py").read_text(encoding="utf-8")
    assert '"build-id.txt"' in paths, "nothing reads the stamp back"

    window = (root / "src" / "wer" / "ui" / "main_window.py").read_text(encoding="utf-8")
    assert '("Built from", built_from, True)' in window, (
        "the Environment tab no longer shows which commit built this"
    )


def test_the_app_user_model_id_matches_everywhere_it_is_written() -> None:
    r"""Windows resolves a taskbar button's icon and name through this identity.

    It is written in three places -- the app claims it at startup, and both the
    installer's shortcuts and build.ps1's Wer.lnk have to claim the same thing.
    If they drift, the shell looks up an identity nothing registers, finds no
    icon, and the taskbar falls back to a generic one while the title bar and
    Explorer still look right. That is a confusing half-broken symptom to
    diagnose from, so it is pinned here instead.
    """
    root = Path(__file__).resolve().parent.parent

    app_py = (root / "src" / "wer" / "app.py").read_text(encoding="utf-8")
    match = re.search(r'APP_USER_MODEL_ID\s*=\s*"([^"]+)"', app_py)
    assert match, "APP_USER_MODEL_ID is gone from app.py"
    app_id = match.group(1)

    iss = (root / "installer" / "wer.iss").read_text(encoding="utf-8")
    assert f'#define AppUserModelID "{app_id}"' in iss, (
        "installer/wer.iss declares a different AppUserModelID"
    )
    # And it must actually be applied to the shortcuts, not merely defined.
    assert iss.count("AppUserModelID:") >= 2, (
        "the installer defines the id but does not put it on the shortcuts"
    )

    build = (root / "build.ps1").read_text(encoding="utf-8")
    assert f"$appUserModelId = '{app_id}'" in build, (
        "build.ps1 stamps Wer.lnk with a different AppUserModelID"
    )


def test_dialog_answers_are_compared_by_value_not_identity() -> None:
    """QMessageBox.question hands back a plain int in this PySide6, not the
    enum member, so ``answer is QMessageBox.StandardButton.Yes`` is never true.
    Four confirmations were written that way and every one of them silently
    declined: Delete on the Overlay tab, Delete on the saved consoles, Reset to
    default, and the low-disk "Record anyway?" prompt. The tests stubbed the
    dialog with the enum and never saw it."""
    offenders = []
    for path in sorted((Path(__file__).resolve().parents[1] / "src").rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"\bis (not )?QMessageBox\.StandardButton\.", line):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert not offenders, "compare dialog answers with == or !=:\n" + "\n".join(offenders)
