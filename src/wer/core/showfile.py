"""Show files: persistence for connections, recording settings and layouts.

JSON, human-readable and diffable, with a ``schema_version`` from day one and
the migration hook written **before** it is needed -- because the version you
wish you had bumped is always the one that shipped last month.

No Qt. A show file is data; the UI reads and writes it but does not define it.

Autosave
--------
The app keeps an autosaved show at ``%LOCALAPPDATA%/Wer/autosave.wer`` and loads
it at startup. That is what makes Wer reconnect to the console you used last
without being asked: your last session's settings are simply still there.
"""

from __future__ import annotations

import json
import logging
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from wer import SHOW_FILE_EXT, SHOW_SCHEMA_VERSION
from wer.paths import default_recording_dir, user_data_dir

log = logging.getLogger(__name__)

__all__ = [
    "ShowFile",
    "EosConfig",
    "ConsoleProfile",
    "RecordingConfig",
    "SacnConfig",
    "load_show",
    "save_show",
    "autosave_path",
    "migrate",
]


@dataclass
class ConsoleProfile:
    """One saved console: a venue, a rig, a desk.

    Anyone working across several venues has several of these -- a house Ion XE,
    a rehearsal room Nomad, a laptop running offline -- and typing an IP address
    into a settings box before every tech is exactly the sort of thing to get
    wrong under pressure.
    """

    name: str = "Console"
    host: str = "127.0.0.1"
    transport: str = "TCP"
    port: int = 3032
    framing: str = "OSC 1.0 (length prefix)"
    user: int = 0
    subscribe: bool = True
    #: Free text -- "booth, house left" or "needs OSC TX turning on".
    notes: str = ""
    #: Wall-clock time this console last actually delivered data, or 0.0 if it
    #: never has. Ordering by this is what makes "the one I used last" work
    #: without anyone having to mark a favourite.
    last_connected: float = 0.0
    #: False once a console has proved itself unreachable and the user says so.
    enabled: bool = True

    @property
    def has_connected(self) -> bool:
        return self.last_connected > 0.0

    @property
    def summary(self) -> str:
        return f"{self.host}:{self.port} ({self.transport})"


@dataclass
class EosConfig:
    """Global Eos behaviour. The consoles themselves are ConsoleProfiles."""

    enabled: bool = False
    host: str = "127.0.0.1"
    transport: str = "TCP"
    port: int = 3032
    framing: str = "OSC 1.0 (length prefix)"
    user: int = 0
    subscribe: bool = True
    #: Reconnect on startup. On by default once a console has successfully
    #: connected once -- walking into a tech and having the app already talking
    #: to the desk is the whole point.
    auto_connect: bool = True
    #: If the most recent console does not answer, work down the rest of the
    #: list. A venue laptop that moves between rigs finds the right one by
    #: itself rather than needing settings changed on arrival.
    try_all_on_startup: bool = True
    #: Seconds to wait for a console to actually SEND something before moving
    #: on. Not the socket timeout: Eos accepts a connection whether or not its
    #: OSC output is enabled, so only data proves a console is usable. Eos
    #: dumps its whole state within about a second of connecting, so this is
    #: generous.
    probe_seconds: float = 4.0


@dataclass
class AvOffset:
    """The A/V offset for one camera and audio input, and where it came from.

    See wer.core.avsync for how these are chosen and kept.
    """

    #: The camera and audio input, by the names DirectShow lists them under.
    camera: str = ""
    audio: str = ""
    offset_ms: int = 0
    #: "typed" on the Recording tab, or "clap" when applied from a clap test.
    source: str = "typed"
    #: When it was set, as a local ISO 8601 date and time. Empty when not known,
    #: which is the case for one carried over from a v2 show file.
    set_at: str = ""
    #: A clap test's reading: the lag it measured, the offset its take was
    #: recorded with, how many claps it used, and the take's frame period.
    #: None for a typed value.
    measured_lag_ms: float | None = None
    recorded_offset_ms: int | None = None
    claps: int | None = None
    frame_period_ms: float | None = None


@dataclass
class RecordingConfig:
    """Where recordings go and how they are encoded."""

    #: Empty means "use the default", resolved at load time so that a show file
    #: moved between machines does not carry a dead path.
    directory: str = ""

    #: Filename template with tokens.
    #: {show} {date} {time} {take} {cue} are substituted at record time.
    filename_template: str = "{show}_{date}_take{take}"

    container: str = "mkv"
    #: "auto" means the best encoder this machine actually has. Hardware
    #: encoding keeps the camera fed measurably better -- see
    #: resolve_encoder_name -- and falls back to software on a machine with
    #: none, which is every machine Wer promises to work on.
    encoder: str = "auto"
    quality_preset: str = "standard"
    quality: int | None = None
    speed_preset: str | None = None

    #: None means "match the camera".
    width: int | None = None
    height: int | None = None
    fps: float | None = None

    audio_device: str | None = None
    audio_bitrate_kbps: int = 192
    #: The offset last set for the camera and audio input in use, or 0 when
    #: that pairing is on the automatic value. Kept for an older Wer, which
    #: reads only this; this build reads it only when upgrading a v2 show file.
    #: See wer.core.avsync.sync_legacy_offset.
    av_offset_ms: int = 0
    #: One entry per camera and audio input with an offset set by the operator
    #: or a clap test (Hudson, 12 Sep 2026). A pairing with no entry uses the
    #: automatic value.
    av_offsets: list[AvOffset] = field(default_factory=list)

    #: Stop the recording at or below this many gigabytes free. Checked
    #: before arming and every ten seconds while recording.
    #:
    #: Stopping with room to spare is the point: a disk that fills mid-write
    #: does not fail politely, and ten gigabytes is enough headroom for ffmpeg
    #: to close the file, for the chapters to be embedded (which rewrites the
    #: file, and so briefly needs its size again), and for Windows to keep
    #: functioning.
    low_disk_stop_gb: float = 10.0
    #: Warn at this level, well before stopping, so there is time to react.
    low_disk_warning_gb: float = 20.0
    #: If ffmpeg dies mid-recording, continue into a numbered part rather than
    #: ending the session. Losing two acts to a hiccup is worse than a short
    #: gap and an extra file.
    continue_after_failure: bool = True

    #: Drop a marker automatically whenever the console fires a cue. Off
    #: means only the manual Ctrl+M markers are kept.
    auto_markers: bool = True
    #: Put chapters and the sidecars INSIDE the .mkv when a take finishes, so
    #: the recording is self-contained and navigable in an ordinary player.
    #: Costs a stream-copy rewrite of the file, which is I/O-bound and lossless.
    embed_markers: bool = True
    #: Keep the sidecar files next to the video as well as inside it. Off by
    #: default once embedding is on -- the whole point is one file, not four.
    keep_sidecar_files: bool = False
    #: Log every bus change for the duration of a take.
    write_bus_log: bool = True

    # --- Snapshots. Independent of recording; the camera is always live. ---
    #: Empty means "next to the recordings".
    snapshot_directory: str = ""
    #: {cue} is the active cue number, which is usually the whole point of
    #: taking a still during a tech.
    snapshot_template: str = "{show}_{date}_{time}_cue{cue}"
    snapshot_format: str = "png"
    snapshot_jpeg_quality: int = 92
    #: Drop a marker when a snapshot is taken during a recording, so the still
    #: and the moment in the video can be found from each other.
    snapshot_marks_recording: bool = True

    def resolved_snapshot_directory(self) -> Path:
        if self.snapshot_directory:
            return Path(self.snapshot_directory)
        return self.resolved_directory() / "Snapshots"

    def resolved_directory(self) -> Path:
        if self.directory:
            return Path(self.directory)
        return default_recording_dir()

    def directory_problem(self) -> str:
        """Why the recording folder is not usable, or "" if it is fine.

        A saved folder can stop existing between shows -- an external drive
        that is not plugged in, a letter that moved, a path that was only ever
        temporary. Creating it silently is the wrong answer: the recording then
        goes somewhere nobody chose and nobody looks until afterwards.
        """
        directory = self.resolved_directory()
        if directory.is_dir():
            return ""
        # Walk up to the first ancestor that exists. If that is the drive root
        # itself, the drive is missing and nothing can be created.
        parent = directory.parent
        while parent != parent.parent and not parent.exists():
            parent = parent.parent
        if not parent.exists():
            return f"the drive {parent} is not available"
        if directory.exists():
            return "that path is a file, not a folder"
        return "it does not exist yet and will be created"


@dataclass
class CameraConfig:
    """Last-used camera, so the app comes back up the way you left it."""

    device_name: str = ""
    width: int = 1920
    height: int = 1080
    fps: float = 30.0
    fourcc: str = "MJPG"
    #: Kept for the schema; nothing reads it. See CaptureSettings.rotation.
    rotation: int = 0
    #: The Preview tab's Mirror left to right and Flip top to bottom. Off by
    #: default, and off for a show file saved before those boxes existed:
    #: _build leaves a missing key at its default, so an older show comes back
    #: with the picture the way it has always recorded it.
    flip_horizontal: bool = False
    flip_vertical: bool = False


@dataclass
class EditorConfig:
    """How the overlay editor behaves while widgets are being moved.

    Preferences about the tool rather than the show, kept here all the same:
    the show file is the one place Wer remembers anything, and a snapping
    setting that resets on every launch is one you set on every launch.
    """

    #: Pull dragged widgets onto the margins, the centre lines and each other.
    snap: bool = True
    #: Pull them onto the grid lines as well. Off by default: with it on,
    #: every widget lands on a line, which is tidy but fights fine placement.
    snap_to_grid: bool = False
    #: The grid drawn while a widget moves. 32 x 18 gives square cells on a
    #: 16:9 picture, 60 px each at 1080p.
    grid_columns: int = 32
    grid_rows: int = 18


@dataclass
class SacnConfig:
    """Wer's recording status sent over sACN (Connections > sACN output).

    Off by default. Universe 101, address 1 to start with: Hudson's defaults,
    13 Sep 2026. See wer.connections.sacn_sender.
    """

    enabled: bool = False
    #: 1-63999.
    universe: int = 101
    #: 1-512, as a console numbers them.
    address: int = 1
    #: 1-200. 100 is sACN's default, and what a console sends unless told
    #: otherwise.
    priority: int = 100
    #: On by default. With it off, a receiver that merges by priority takes
    #: the whole universe from Wer at this priority, 511 addresses of 0 and all.
    per_address_priority: bool = True
    #: The Windows key of the network to send on; "" for Automatic.
    adapter: str = ""
    #: What that network was called when it was chosen, to name it when it is
    #: missing.
    adapter_name: str = ""
    #: What receivers list Wer as; "" for "Wer on <computer name>".
    source_name: str = ""
    #: This Wer's sACN source identity, made once and kept: E1.31 asks a source
    #: to keep one CID, and receivers tell sources apart by it alone.
    cid: str = ""


@dataclass
class ShowFile:
    """Everything that persists between sessions."""

    schema_version: int = SHOW_SCHEMA_VERSION
    show_name: str = ""
    eos: EosConfig = field(default_factory=EosConfig)
    consoles: list[ConsoleProfile] = field(default_factory=list)
    #: Name of the console currently selected.
    active_console: str = ""
    recording: RecordingConfig = field(default_factory=RecordingConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    editor: EditorConfig = field(default_factory=EditorConfig)
    sacn: SacnConfig = field(default_factory=SacnConfig)
    #: Operator-typed fields (the Manual connection).
    manual_fields: dict[str, str] = field(default_factory=dict)
    #: Reserved for M7. Present now so the schema does not change shape later.
    layouts: list[dict[str, Any]] = field(default_factory=list)
    active_layout: str = "Tech"

    take: int = 1

    def to_json(self) -> str:
        """Serialise. Sorted keys and real indentation, so diffs are readable."""
        return json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False)


# --------------------------------------------------------------------- loading


def autosave_path() -> Path:
    return user_data_dir() / f"autosave{SHOW_FILE_EXT}"


def migrate(data: dict[str, Any]) -> dict[str, Any]:
    """Bring an older show file up to the current schema.

    Written before it is needed. Each step upgrades one version, so a file from
    any past version reaches the present by running through them in order.

    Adding a version means adding a branch here, not inventing a mechanism
    under pressure halfway through a tech.
    """
    version = _as_int(data.get("schema_version"), 1)

    if version > SHOW_SCHEMA_VERSION:
        # A file from a newer Wer. Loading it would silently drop whatever the
        # newer version added, so say so and carry on with what we understand.
        log.warning(
            "Show file is schema v%d but this build understands v%d. "
            "Anything newer will be ignored, and saving will downgrade the file.",
            version, SHOW_SCHEMA_VERSION,
        )
        return data

    while version < SHOW_SCHEMA_VERSION:
        if version == 1:
            # v2 introduced named console profiles. A v1 file holds exactly one
            # console under "eos"; carry it across so nobody loses the address
            # they had working, and name it after its host so it is recognisable
            # rather than called "Console 1".
            eos = data.get("eos") if isinstance(data.get("eos"), dict) else {}
            if eos and not data.get("consoles"):
                host = str(eos.get("host", "127.0.0.1"))
                name = "This computer" if host.startswith("127.") else host
                data["consoles"] = [{
                    "name": name,
                    "host": host,
                    "transport": eos.get("transport", "TCP"),
                    "port": eos.get("port", 3032),
                    "framing": eos.get("framing", "OSC 1.0 (length prefix)"),
                    "user": eos.get("user", 1),
                    "subscribe": eos.get("subscribe", True),
                    # If it was marked enabled in v1 it had connected at least
                    # once, so give it a plausible last-used time rather than
                    # zero -- otherwise it would sort below consoles added later.
                    "last_connected": time.time() if eos.get("enabled") else 0.0,
                }]
                data.setdefault("active_console", name)
                log.info("Migrated the v1 console %r into a named profile", host)
            version = 2
        elif version == 2:
            # v3 keeps an A/V offset for each camera and audio input. A v2 file
            # has one offset for everything. The Recording tab's spin box was
            # the only thing that ever wrote it, so a non-zero one was the
            # operator's: it becomes the typed offset for the camera and audio
            # input the file was saved with. A zero is left out. It cannot say
            # whether anyone chose it, and never having chosen is what the
            # automatic value now stands for.
            recording = data.get("recording") if isinstance(data.get("recording"), dict) else None
            camera = data.get("camera") if isinstance(data.get("camera"), dict) else {}
            if recording is not None and not recording.get("av_offsets"):
                try:
                    offset = int(recording.get("av_offset_ms") or 0)
                except (TypeError, ValueError):
                    offset = 0
                if offset:
                    recording["av_offsets"] = [{
                        "camera": _as_text(camera.get("device_name"), ""),
                        "audio": _as_text(recording.get("audio_device"), ""),
                        "offset_ms": offset,
                        "source": "typed",
                        "set_at": "",
                    }]
                    log.info(
                        "Kept the v2 A/V offset of %d ms for the saved camera and audio input",
                        offset,
                    )
            version = 3
        else:
            # Unknown gap: stop rather than loop forever.
            break
        data["schema_version"] = version

    data["schema_version"] = SHOW_SCHEMA_VERSION
    return data


def _as_int(value: Any, default: int) -> int:
    """A show-file integer, or the default when the field is null."""
    return default if value is None else int(value)


def _as_text(value: Any, default: str) -> str:
    """A show-file string, or the default when the field is null.

    Not `str(value or default)`: an empty string is a real answer that the user
    may have chosen, and only null means "not set".
    """
    return default if value is None else str(value)


def _build(data: dict[str, Any]) -> ShowFile:
    """Construct a ShowFile, ignoring unknown keys rather than exploding.

    A show file edited by hand -- which a human-readable format explicitly
    invites -- should not become unloadable because of a stray key or a missing
    one.
    """
    show = ShowFile()
    # Every coercion below treats a JSON null as "not set". An editor clearing
    # a field writes null rather than removing the key, and null used to be
    # fatal in two different ways. int(None) and dict(None) raise TypeError,
    # which load_show does not promise and load_autosave did not catch, so one
    # null in take, consoles, layouts, manual_fields or schema_version meant the
    # window never opened at all -- no dialog, and no .corrupt salvage copy
    # either, because that copy is made inside the handler the TypeError sailed
    # past. str(None) is the quieter half: it succeeds, and a show whose name
    # was nulled records as None_2026-09-10_take001.mkv.
    show.schema_version = _as_int(data.get("schema_version"), SHOW_SCHEMA_VERSION)
    show.show_name = _as_text(data.get("show_name"), "")
    show.active_layout = _as_text(data.get("active_layout"), "Tech")
    show.take = _as_int(data.get("take"), 1)
    show.manual_fields = dict(data.get("manual_fields") or {})
    show.layouts = list(data.get("layouts") or [])
    show.active_console = _as_text(data.get("active_console"), "")

    known_console = set(ConsoleProfile.__dataclass_fields__)
    for entry in data.get("consoles") or []:
        if isinstance(entry, dict):
            show.consoles.append(
                ConsoleProfile(**{k: v for k, v in entry.items() if k in known_console})
            )

    # No "osc_control" any more. It held the settings of a UDP listener the
    # console never reached, removed on 13 Sep 2026 when commands moved onto
    # the Eos connection. A show file that still has one loads without it.
    for name, cls in (("eos", EosConfig), ("recording", RecordingConfig),
                      ("camera", CameraConfig),
                      ("editor", EditorConfig), ("sacn", SacnConfig)):
        section = data.get(name)
        if isinstance(section, dict):
            known = {f for f in cls.__dataclass_fields__}
            filtered = {k: v for k, v in section.items() if k in known}
            unknown = set(section) - known
            if unknown:
                log.info("Ignoring unknown %s keys in show file: %s",
                         name, ", ".join(sorted(unknown)))
            setattr(show, name, cls(**filtered))
    # The loop above leaves the offsets as the plain dicts JSON gave it, and
    # a null av_offset_ms would reach the Recording tab's spin box, whose
    # setValue(None) raises.
    show.recording.av_offset_ms = _as_int(show.recording.av_offset_ms, 0)
    show.recording.av_offsets = _av_offsets(show.recording.av_offsets)
    show.sacn = _sacn_config(show.sacn)
    return show


def _sacn_config(config: SacnConfig) -> SacnConfig:
    """The sACN section with every value a sender can use, or its default.

    The loop in _build passes a section's values through as JSON gave them. A
    universe out of range goes back to the default rather than being clamped
    to the nearest real one: clamping 70000 to 63999 would send on a universe
    nobody picked, where 101 is at least the one Wer starts on everywhere.
    """
    defaults = SacnConfig()

    def whole(value: Any, default: int, low: int, high: int) -> int:
        if isinstance(value, bool):
            return default
        try:
            number = default if value is None else int(value)
        except (TypeError, ValueError):
            return default
        return number if low <= number <= high else default

    def flag(value: Any, default: bool) -> bool:
        return value if isinstance(value, bool) else default

    def text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    cid = text(config.cid)
    try:
        uuid.UUID(cid)
    except ValueError:
        cid = ""
    return SacnConfig(
        enabled=flag(config.enabled, defaults.enabled),
        universe=whole(config.universe, defaults.universe, 1, 63999),
        address=whole(config.address, defaults.address, 1, 512),
        priority=whole(config.priority, 100, 1, 200),
        per_address_priority=flag(config.per_address_priority, True),
        adapter=text(config.adapter),
        adapter_name=text(config.adapter_name),
        source_name=text(config.source_name),
        cid=cid,
    )


def _av_offsets(raw: Any) -> list[AvOffset]:
    """The A/V offsets a show file holds, skipping any entry that cannot be read.

    Skipped rather than fatal: one bad entry, typed by hand, is no reason to
    lose the rest of the setup, and the pairing it was for falls back to the
    automatic value, which the Recording tab shows.
    """
    known = set(AvOffset.__dataclass_fields__)
    entries: list[AvOffset] = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, AvOffset):
            entries.append(item)
            continue
        if not isinstance(item, dict):
            continue
        try:
            entry = AvOffset(**{k: v for k, v in item.items() if k in known})
            entry.camera = _as_text(entry.camera, "")
            entry.audio = _as_text(entry.audio, "")
            entry.offset_ms = _as_int(entry.offset_ms, 0)
            entry.source = _as_text(entry.source, "typed")
            entry.set_at = _as_text(entry.set_at, "")
        except (TypeError, ValueError):
            log.warning("Ignoring an A/V offset in the show file that could not be read: %r", item)
            continue
        entries.append(entry)
    return entries


def load_show(path: Path) -> ShowFile:
    """Load a show file. Raises OSError or ValueError with something readable."""
    text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{path.name} is not valid JSON (line {exc.lineno}, column {exc.colno}): "
            f"{exc.msg}"
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} does not contain a show object")

    show = _build(migrate(data))
    log.info("Loaded show file %s (schema v%d)", path, show.schema_version)
    return show


def save_show(show: ShowFile, path: Path) -> None:
    """Write a show file atomically.

    Via a temporary file and a replace, so an interruption mid-write cannot
    leave a truncated show file where a working one used to be. Autosave runs
    while a tech is happening; it must never be the thing that loses the setup.
    """
    show.schema_version = SHOW_SCHEMA_VERSION
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(show.to_json(), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    log.debug("Saved show file %s", path)


def load_autosave() -> ShowFile:
    """Load the autosaved show, or return defaults if there is not one yet."""
    path = autosave_path()
    if not path.is_file():
        log.info("No autosave at %s; starting with defaults", path)
        return ShowFile()
    try:
        return load_show(path)
    except (OSError, TypeError, ValueError):
        # A corrupt autosave must not stop the app from opening. Keep the bad
        # file for inspection rather than overwriting it.
        #
        # TypeError is here because it escaped once, for real: a hand-edited
        # file with a null where a number belonged raised one out of _build,
        # and because the salvage copy below is made inside this handler, the
        # one failure that stopped the app from starting was also the one that
        # left the user nothing to recover from. _build no longer raises on
        # null, but the net is only worth having if it catches the shapes
        # nobody has thought of yet.
        log.exception("Could not load autosave; starting with defaults")
        try:
            shutil.copy2(path, path.with_suffix(".corrupt"))
        except OSError:
            pass
        return ShowFile()


def save_autosave(show: ShowFile) -> None:
    try:
        save_show(show, autosave_path())
    except OSError:
        log.exception("Autosave failed")


# ------------------------------------------------------------------- filenames


def render_filename(
    template: str,
    *,
    show_name: str = "",
    take: int = 1,
    cue: str = "",
    when: float | None = None,
) -> str:
    """Substitute the filename tokens.

    Anything that would upset Windows is replaced rather than rejected: a show
    called "A Midsummer Night's Dream: Act 1/2" is a perfectly reasonable thing
    to type and an impossible thing to put in a filename.
    """
    moment = time.localtime(when if when is not None else time.time())
    values = {
        "show": show_name or "Untitled",
        "date": time.strftime("%Y-%m-%d", moment),
        "time": time.strftime("%H-%M-%S", moment),
        "take": f"{take:03d}",
        "cue": cue or "",
    }
    rendered = template
    for key, value in values.items():
        rendered = rendered.replace("{" + key + "}", str(value))

    return sanitise_filename(rendered)


#: Characters Windows will not accept in a filename.
_ILLEGAL = '<>:"/\\|?*'

#: Names Windows reserves regardless of extension. A show genuinely called
#: "Aux" would otherwise produce a file that cannot be created.
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitise_filename(name: str) -> str:
    cleaned = "".join("-" if c in _ILLEGAL else c for c in name)
    cleaned = "".join(c for c in cleaned if ord(c) >= 32)
    cleaned = cleaned.strip().strip(".")
    # Collapse runs of separators introduced by substitution.
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    if not cleaned:
        return "recording"
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:180]
