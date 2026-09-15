"""Show files: persistence, migration and filename rendering.

No Qt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from wer import SHOW_SCHEMA_VERSION
from wer.core.showfile import (
    EosConfig,
    RecordingConfig,
    ShowFile,
    autosave_path,
    load_autosave,
    load_show,
    migrate,
    render_filename,
    sanitise_filename,
    save_show,
)
from wer.paths import DATA_DIR_ENV


# ------------------------------------------------------------------ round trip


def test_round_trip(tmp_path: Path) -> None:
    show = ShowFile(show_name="The Comedy of Errors", take=7)
    show.eos = EosConfig(enabled=True, host="10.101.90.101", port=3032, user=2)
    show.recording = RecordingConfig(directory=str(tmp_path), quality_preset="compact")
    show.manual_fields = {"act": "Two", "note": "spot cue late"}

    path = tmp_path / "test.wer"
    save_show(show, path)
    loaded = load_show(path)

    assert loaded.show_name == "The Comedy of Errors"
    assert loaded.take == 7
    assert loaded.eos.host == "10.101.90.101"
    assert loaded.eos.enabled is True
    assert loaded.recording.quality_preset == "compact"
    assert loaded.manual_fields["note"] == "spot cue late"


def test_the_file_is_human_readable_and_diffable(tmp_path: Path) -> None:
    """The format promises this explicitly, so it is worth asserting."""
    path = tmp_path / "test.wer"
    save_show(ShowFile(show_name="Hamlet"), path)
    text = path.read_text(encoding="utf-8")

    assert "\n" in text, "should be indented, not one line"
    assert '"show_name": "Hamlet"' in text
    # Sorted keys keep diffs stable between saves.
    data = json.loads(text)
    assert list(data) == sorted(data)


def test_saving_is_atomic(tmp_path: Path) -> None:
    """Autosave runs during a tech; it must never be what loses the setup."""
    path = tmp_path / "test.wer"
    save_show(ShowFile(show_name="First"), path)
    save_show(ShowFile(show_name="Second"), path)
    assert load_show(path).show_name == "Second"
    assert not list(tmp_path.glob("*.tmp")), "a temp file was left behind"


def test_schema_version_is_always_written(tmp_path: Path) -> None:
    path = tmp_path / "test.wer"
    save_show(ShowFile(), path)
    assert json.loads(path.read_text())["schema_version"] == SHOW_SCHEMA_VERSION


def test_the_picture_flips_round_trip(tmp_path: Path) -> None:
    """A camera rigged upside down is set once for the show, not at every launch."""
    show = ShowFile()
    show.camera.flip_horizontal = True
    show.camera.flip_vertical = True
    path = tmp_path / "test.wer"
    save_show(show, path)
    camera = load_show(path).camera
    assert (camera.flip_horizontal, camera.flip_vertical) == (True, True)


def test_a_show_file_from_before_the_flips_loads_with_both_off(tmp_path: Path) -> None:
    """A show saved before the Preview tab had the boxes has no such keys, and
    must come back recording the picture the way it always has, not turned."""
    path = tmp_path / "test.wer"
    path.write_text(
        json.dumps({
            "schema_version": SHOW_SCHEMA_VERSION,
            "camera": {
                "device_name": "Blackmagic WDM Capture", "width": 1920,
                "height": 1080, "fps": 30.0, "fourcc": "UYVY", "rotation": 0,
            },
        }),
        encoding="utf-8",
    )
    camera = load_show(path).camera
    assert camera.device_name == "Blackmagic WDM Capture"
    assert (camera.flip_horizontal, camera.flip_vertical) == (False, False)


# -------------------------------------------------------------------- robustness


def test_unknown_keys_are_ignored_not_fatal(tmp_path: Path) -> None:
    """The format invites hand-editing, so a stray key must not break loading."""
    path = tmp_path / "test.wer"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "show_name": "Hand edited",
            "eos": {"host": "10.0.0.1", "invented_key": True},
            "some_future_section": {"x": 1},
        }),
        encoding="utf-8",
    )
    show = load_show(path)
    assert show.show_name == "Hand edited"
    assert show.eos.host == "10.0.0.1"


def test_missing_sections_fall_back_to_defaults(tmp_path: Path) -> None:
    path = tmp_path / "test.wer"
    path.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
    show = load_show(path)
    assert show.eos.host == "127.0.0.1"
    assert show.recording.container == "mkv"


def test_editor_snap_and_grid_settings_round_trip(tmp_path: Path) -> None:
    """A snapping preference that reset on every launch would be set on every
    launch; a show file from before it existed opens with the defaults."""
    from wer.core.showfile import EditorConfig

    show = ShowFile()
    show.editor = EditorConfig(snap=False, snap_to_grid=True, grid_columns=64, grid_rows=36)
    path = tmp_path / "test.wer"
    save_show(show, path)
    loaded = load_show(path)
    assert loaded.editor == show.editor

    older = tmp_path / "older.wer"
    older.write_text(json.dumps({"schema_version": 2, "show_name": "x"}), encoding="utf-8")
    assert load_show(older).editor == EditorConfig()


def test_broken_json_says_where(tmp_path: Path) -> None:
    """A useful error beats a stack trace when it is a show file you edited."""
    path = tmp_path / "broken.wer"
    path.write_text('{"show_name": "unclosed', encoding="utf-8")
    with pytest.raises(ValueError, match="line"):
        load_show(path)


@pytest.mark.parametrize(
    "key",
    ["take", "consoles", "layouts", "manual_fields", "schema_version"],
)
def test_a_null_does_not_stop_the_show_file_loading(tmp_path: Path, key: str) -> None:
    """A JSON null used to mean the app never opened at all.

    Five of the top-level keys were coerced without a guard -- int(None) and
    dict(None) raise TypeError, which load_show does not promise and
    load_autosave does not catch, so the window never appeared and the .corrupt
    safety copy was never made either. An editor "clearing" a field writes
    null, not the default, and the format invites hand-editing.
    """
    path = tmp_path / f"null_{key}.wer"
    path.write_text(
        json.dumps({
            "schema_version": SHOW_SCHEMA_VERSION,
            "show_name": "Twelfth Night",
            key: None,
        }),
        encoding="utf-8",
    )
    show = load_show(path)
    assert show.show_name == "Twelfth Night", "the rest of the file must survive"
    assert show.take >= 1
    assert show.consoles == []
    assert show.layouts == []
    assert show.manual_fields == {}


def test_a_null_show_name_does_not_become_the_word_none(tmp_path: Path) -> None:
    """str(None) succeeds, which is the quiet half of the same bug: a show
    called None records None_2026-09-10_take001.mkv."""
    path = tmp_path / "null_name.wer"
    path.write_text(json.dumps({"show_name": None}), encoding="utf-8")
    show = load_show(path)
    assert show.show_name == ""
    assert render_filename("{show}", show_name=show.show_name).startswith("Untitled")


def test_an_unloadable_autosave_still_leaves_a_salvage_copy(
    tmp_path: Path, monkeypatch
) -> None:
    """The recovery net had a hole in exactly the case that needed it: a
    TypeError escaped the handler, so the app did not start AND the .corrupt
    copy that exists for the user to salvage was never written."""
    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))
    autosave_path().write_text(
        json.dumps({"take": {"not": "a number"}}), encoding="utf-8"
    )

    show = load_autosave()

    assert show.take == 1, "defaults, rather than no window at all"
    assert autosave_path().with_suffix(".corrupt").is_file()


def test_a_file_from_the_future_still_loads(tmp_path: Path) -> None:
    """Better to load what we understand than to refuse the file outright."""
    data = {"schema_version": SHOW_SCHEMA_VERSION + 5, "show_name": "From the future"}
    assert migrate(dict(data))["show_name"] == "From the future"


def test_migration_stamps_the_current_version() -> None:
    assert migrate({"schema_version": 1})["schema_version"] == SHOW_SCHEMA_VERSION


# --------------------------------------------------------------------- filenames


def test_filename_tokens_are_substituted() -> None:
    name = render_filename("{show}_{date}_take{take}", show_name="Hamlet", take=3)
    assert name.startswith("Hamlet_")
    assert name.endswith("_take003")
    assert len(name.split("_")[1]) == 10  # YYYY-MM-DD


def test_untitled_when_no_show_name() -> None:
    assert render_filename("{show}", show_name="").startswith("Untitled")


@pytest.mark.parametrize(
    ("raw", "banned"),
    [
        ("Act 1/2", "/"),
        ("Show: The Sequel", ":"),
        ('He said "hi"', '"'),
        ("What?", "?"),
        ("A|B", "|"),
    ],
)
def test_illegal_characters_are_replaced(raw: str, banned: str) -> None:
    """A show called "A Midsummer Night's Dream: Act 1/2" is a reasonable thing
    to type and an impossible thing to put in a filename."""
    assert banned not in sanitise_filename(raw)


def test_windows_reserved_names_are_escaped() -> None:
    """A show genuinely called "Aux" would otherwise be uncreatable."""
    assert sanitise_filename("AUX") != "AUX"
    assert sanitise_filename("con.mkv").startswith("_")


def test_sanitising_never_returns_nothing() -> None:
    assert sanitise_filename("...") == "recording"
    assert sanitise_filename("") == "recording"


def test_filenames_are_length_capped() -> None:
    assert len(sanitise_filename("x" * 400)) <= 180


# ---------------------------------------------------------------- directories


def test_recording_directory_defaults_when_blank() -> None:
    """A show file moved between machines must not carry a dead path."""
    config = RecordingConfig(directory="")
    assert config.resolved_directory().is_absolute()


def test_recording_directory_is_used_when_set(tmp_path: Path) -> None:
    config = RecordingConfig(directory=str(tmp_path))
    assert config.resolved_directory() == tmp_path


# ------------------------------------------------------------ marker settings


def test_marker_settings_default_to_useful() -> None:
    """Usable before anything has been configured."""
    config = RecordingConfig()
    assert config.auto_markers is True
    assert config.embed_markers is True
    assert config.write_bus_log is True
    # One file, not four -- the sidecars live inside the video by default.
    assert config.keep_sidecar_files is False


def test_marker_settings_round_trip(tmp_path: Path) -> None:
    show = ShowFile()
    show.recording = RecordingConfig(
        auto_markers=False, embed_markers=False,
        keep_sidecar_files=True, write_bus_log=False,
    )
    path = tmp_path / "s.wer"
    save_show(show, path)
    loaded = load_show(path)
    assert loaded.recording.auto_markers is False
    assert loaded.recording.embed_markers is False
    assert loaded.recording.keep_sidecar_files is True
    assert loaded.recording.write_bus_log is False


# ------------------------------------------- the recording folder is real


def test_an_existing_folder_has_no_problem(tmp_path) -> None:
    config = RecordingConfig(directory=str(tmp_path))
    assert config.directory_problem() == ""


def test_a_folder_that_does_not_exist_yet_says_it_will_be_made(tmp_path) -> None:
    """Normal on a first run, so it explains rather than alarms."""
    config = RecordingConfig(directory=str(tmp_path / "Tonight"))
    assert "will be created" in config.directory_problem()


def test_a_missing_drive_is_called_out(tmp_path) -> None:
    """The booth case: the external drive stayed at home. Nothing can be
    created there, and finding out after the tech is the expensive way."""
    config = RecordingConfig(directory="Q:/Techs/Tonight")
    problem = config.directory_problem()
    assert "not available" in problem
    assert "Q:" in problem


def test_a_file_where_a_folder_should_be(tmp_path) -> None:
    victim = tmp_path / "notafolder.txt"
    victim.write_text("x", encoding="utf-8")
    config = RecordingConfig(directory=str(victim))
    assert "not a folder" in config.directory_problem()


def test_the_default_folder_is_reported_as_fine_or_creatable() -> None:
    """Whatever else is true, the out-of-the-box path must never look broken."""
    problem = RecordingConfig().directory_problem()
    assert problem == "" or "will be created" in problem


def test_a_stale_saved_folder_does_not_masquerade_as_the_default(tmp_path) -> None:
    """The bug this came from: a saved folder that had stopped existing was
    recreated silently, so recordings went somewhere nobody chose."""
    gone = tmp_path / "vanished" / "deeper"
    config = RecordingConfig(directory=str(gone))
    assert config.resolved_directory() == gone
    assert config.directory_problem() != ""
