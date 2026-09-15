"""Camera enumeration and format probing.

No Qt. numpy is not needed here either - this module is pure discovery, so it
can be exercised from a script or a test without opening a camera.

Why this is more than a one-liner
--------------------------------
OpenCV cannot report friendly device names on Windows; it only takes an integer
index. `pygrabber` reads the DirectShow graph and gives names. That split brings
a trap with it:

**DirectShow and Media Foundation enumerate devices in different orders.**
`pygrabber` names come from DirectShow. `cv2.CAP_DSHOW` indices match that
order. `cv2.CAP_MSMF` indices do not necessarily. So "device 1 called Logitech
BRIO" is only reliable under DSHOW, and opening index 1 under MSMF may open
something else entirely on a machine with several cameras.

This module therefore treats the DirectShow name+index pair as the identity and
records which backend an index is valid for, rather than pretending one integer
means the same thing to both backends.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from wer.paths import ffmpeg_path

log = logging.getLogger(__name__)

__all__ = [
    "CaptureDevice",
    "VideoFormat",
    "AudioDevice",
    "AudioFormat",
    "PREFERRED_AUDIO_RATE",
    "best_audio_format",
    "default_audio_input_name",
    "probe_audio_formats",
    "enumerate_video_devices",
    "enumerate_audio_devices",
    "probe_formats",
    "FOURCC_PREFERENCE",
    "UNREADABLE_FOURCCS",
]

#: Preferred capture encodings, best first.
#:
#: Why this matters: a camera negotiated to an uncompressed format often cannot
#: sustain 1080p30 over USB bandwidth and silently drops to 5-10 fps, which
#: reads as "the app is broken". MJPG is compressed on the camera and usually
#: the only way to get full rate.
#:
#: But MJPG is not always offered - this machine's integrated camera exposes
#: only nv12 and yuyv422 - so the UI must present what a device actually has
#: rather than assuming MJPG exists.
FOURCC_PREFERENCE = ("MJPG", "NV12", "YUY2", "UYVY")

#: Formats a device lists that OpenCV's DirectShow backend cannot read, ranked
#: below everything else so none of them is the default while a readable format
#: of the same size and rate exists.
#:
#: Measured 12 Sep 2026 on a Blackmagic UltraStudio Recorder 3G, at
#: 1920x1080@30, 2048x1080@30 and 1280x720@60: UYVY delivered every time, and
#: V210, R210 and BGR0 opened, accepted the format, reported fourcc 0000 and
#: never delivered a frame. Before this all four were unranked and so tied,
#: and min() took whichever sorted first -- BGR0, alphabetically -- so the
#: Blackmagic came up on a format that shows nothing, on every launch.
#:
#: Still offered in the Preview tab rather than hidden: another driver may read
#: them, and CameraCapture falls back to the driver's own format when a picked
#: one opens and delivers nothing.
UNREADABLE_FOURCCS = ("V210", "R210", "BGR0")

#: ffmpeg reports formats with its own pixel-format names. Map them to the
#: FOURCC codes OpenCV wants so the picker can speak one language.
_FFMPEG_PIXFMT_TO_FOURCC = {
    "yuyv422": "YUY2",
    "nv12": "NV12",
    "mjpeg": "MJPG",
    "bgr24": "BGR3",
    "rgb24": "RGB3",
}


@dataclass(frozen=True, slots=True)
class CaptureDevice:
    """A video capture device, identified by its DirectShow name and index."""

    index: int
    name: str

    def __str__(self) -> str:
        return f"[{self.index}] {self.name}"


@dataclass(frozen=True, slots=True)
class AudioDevice:
    """An audio capture device.

    ffmpeg takes these by name, not index (audio is piped straight from dshow),
    so the exact string matters and must not be prettified.
    """

    name: str

    def __str__(self) -> str:
        return self.name


@dataclass(frozen=True, slots=True, order=True)
class AudioFormat:
    """One sample format an audio input offers through DirectShow.

    Ordered by rate, then channels, then bit depth, so a sorted list reads the
    way DirectShow's own menu ought to.
    """

    rate: int
    channels: int
    bits: int

    def __str__(self) -> str:
        return f"{self.rate} Hz, {self.channels} ch, {self.bits}-bit"


@dataclass(frozen=True, slots=True)
class VideoFormat:
    """One resolution / rate / encoding a camera claims to support."""

    width: int
    height: int
    fps_min: float
    fps_max: float
    fourcc: str

    @property
    def resolution(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def megapixels(self) -> float:
        return (self.width * self.height) / 1_000_000

    def __str__(self) -> str:
        rate = (
            f"{self.fps_max:g}"
            if self.fps_min == self.fps_max
            else f"{self.fps_min:g}-{self.fps_max:g}"
        )
        return f"{self.resolution} @ {rate} fps ({self.fourcc})"


def enumerate_video_devices() -> list[CaptureDevice]:
    """List video capture devices in DirectShow order.

    Returns an empty list rather than raising when nothing is present or
    pygrabber is unavailable - a machine with no camera is a normal state to be
    in, and it must show as "no devices" in the UI, not as a crash.
    """
    try:
        from pygrabber.dshow_graph import FilterGraph
    except ImportError:
        log.exception("pygrabber is not installed; cannot enumerate cameras by name")
        return []

    graph = None
    try:
        graph = FilterGraph()
        names = graph.get_input_devices()
    except Exception:
        # pygrabber talks to COM. A busy or half-installed driver can throw
        # almost anything, and none of it should take down the app.
        log.exception("DirectShow enumeration failed")
        return []
    finally:
        # Release the COM objects on the thread that created them, before that
        # thread goes away. Leaving them to the garbage collector means they are
        # finalised at interpreter shutdown from a different apartment, which
        # segfaults. That is not hypothetical: moving this call onto a worker
        # thread to keep it off the UI thread introduced exactly that crash.
        del graph

    devices = [CaptureDevice(index=i, name=name) for i, name in enumerate(names)]
    log.info("Found %d video capture device(s): %s", len(devices),
             ", ".join(d.name for d in devices) or "none")
    return devices


def enumerate_audio_devices() -> list[AudioDevice]:
    """List audio capture devices, by the exact name ffmpeg needs."""
    output = _run_ffmpeg_devices()
    if output is None:
        return []

    devices: list[AudioDevice] = []
    for match in re.finditer(r'"([^"]+)"\s+\(audio\)', output):
        devices.append(AudioDevice(name=match.group(1)))
    log.info("Found %d audio capture device(s)", len(devices))
    return devices


#: Core Audio's names for what default_audio_input_name asks for: recording
#: devices (eCapture), the default device rather than the communications one
#: (eConsole), and a read-only property store (STGM_READ).
_E_CAPTURE = 1
_E_CONSOLE = 0
_STGM_READ = 0
#: A PROPVARIANT holding a string (VT_LPWSTR).
_VT_LPWSTR = 31
_core_audio = None


def default_audio_input_name() -> str | None:
    """The recording device Windows is set to use, by name, or None.

    Asked through Core Audio (IMMDeviceEnumerator.GetDefaultAudioEndpoint) for
    the device Sound settings calls the default, and named by its friendly name
    (PKEY_Device_FriendlyName). On the development laptop that default, a
    webcam's microphone, had character for character the name ffmpeg lists it
    under, so it can be looked up in the Audio box. Nothing is assumed where it
    is not: a name that is not listed is simply not chosen.

    None when there is no recording device, or when asking fails for any
    reason. This only ever picks a starting point, and is no reason to stop.

    Main thread only, like enumerate_video_devices and for the same reason:
    COM objects left to be finalised from another apartment at shutdown crash
    the process. Everything created here is released before it returns. It
    took about 30 ms the first time on the development laptop, and under a
    millisecond after that.
    """
    import ctypes

    try:
        import comtypes

        interfaces = _core_audio_interfaces()
    except Exception:  # noqa: BLE001 - a starting point, never a reason to stop
        log.exception("Cannot ask Windows for its default recording device")
        return None

    enumerator = endpoint = store = None
    try:
        enumerator = comtypes.CoCreateInstance(
            interfaces.CLSID_MMDeviceEnumerator,
            interface=interfaces.IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_INPROC_SERVER,
        )
        endpoint = enumerator.GetDefaultAudioEndpoint(_E_CAPTURE, _E_CONSOLE)
        store = endpoint.OpenPropertyStore(_STGM_READ)
        value = store.GetValue(interfaces.FRIENDLY_NAME)
        try:
            if value.vt != _VT_LPWSTR or not value.pwszVal:
                return None
            return ctypes.wstring_at(value.pwszVal)
        finally:
            # The string is Windows' to allocate and ours to free.
            ctypes.oledll.ole32.PropVariantClear(ctypes.byref(value))
    except Exception as exc:  # noqa: BLE001 - a starting point, never a reason to stop
        # A machine with no recording device at all lands here as well:
        # GetDefaultAudioEndpoint fails with E_NOTFOUND.
        log.info("Windows named no default recording device (%s)", exc)
        return None
    finally:
        del store, endpoint, enumerator


def _core_audio_interfaces():
    """Core Audio's interfaces, declared as far as default_audio_input_name calls them.

    comtypes needs each interface's methods in vtable order up to the last one
    called, and nothing after it. Declared on first use, so comtypes is only
    imported when something asks.
    """
    global _core_audio
    if _core_audio is not None:
        return _core_audio

    from ctypes import POINTER, Structure, c_uint, c_ulong, c_ushort, c_void_p
    from types import SimpleNamespace

    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    class PROPERTYKEY(Structure):
        _fields_ = [("fmtid", GUID), ("pid", c_ulong)]

    class PROPVARIANT(Structure):
        # Only the string member is read. The second pointer makes the union
        # the 16 bytes it is on 64-bit Windows, 24 bytes in all.
        _fields_ = [
            ("vt", c_ushort), ("reserved1", c_ushort), ("reserved2", c_ushort),
            ("reserved3", c_ushort), ("pwszVal", c_void_p), ("padding", c_void_p),
        ]

    class IPropertyStore(IUnknown):
        _iid_ = GUID("{886d8eeb-8cf2-4446-8d02-cdba1dbdcf99}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_ulong), "count")),
            COMMETHOD([], HRESULT, "GetAt", (["in"], c_ulong, "index"),
                      (["out"], POINTER(PROPERTYKEY), "key")),
            COMMETHOD([], HRESULT, "GetValue", (["in"], POINTER(PROPERTYKEY), "key"),
                      (["out"], POINTER(PROPVARIANT), "value")),
        ]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                      (["in"], c_uint, "context"), (["in"], c_void_p, "params"),
                      (["out"], POINTER(c_void_p), "interface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], c_uint, "access"),
                      (["out"], POINTER(POINTER(IPropertyStore)), "store")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_uint, "flow"),
                      (["in"], c_uint, "mask"), (["out"], POINTER(c_void_p), "devices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_uint, "flow"),
                      (["in"], c_uint, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "endpoint")),
        ]

    _core_audio = SimpleNamespace(
        CLSID_MMDeviceEnumerator=GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"),
        IMMDeviceEnumerator=IMMDeviceEnumerator,
        FRIENDLY_NAME=PROPERTYKEY(GUID("{a45c254e-df1c-4efd-8020-67d146a850e0}"), 14),
    )
    return _core_audio


def probe_formats(device: CaptureDevice) -> list[VideoFormat]:
    """Ask ffmpeg what formats a camera supports.

    ffmpeg is used rather than OpenCV because OpenCV has no format-enumeration
    API at all - the only way to discover a mode through OpenCV is to set it and
    see what you get back, which is slow and changes device state.

    Returns an empty list if ffmpeg is unavailable or the device does not
    answer. Callers should fall back to offering common resolutions rather than
    blocking the user.
    """
    output = _run_ffmpeg_list_options(device.name)
    if output is None:
        return []

    formats: list[VideoFormat] = []
    pattern = re.compile(
        r"(?:pixel_format=(?P<pix>\w+)|vcodec=(?P<vcodec>\w+))\s+"
        r"min s=(?P<minw>\d+)x(?P<minh>\d+) fps=(?P<minfps>[\d.]+)\s+"
        r"max s=(?P<maxw>\d+)x(?P<maxh>\d+) fps=(?P<maxfps>[\d.]+)"
    )
    for match in pattern.finditer(output):
        raw = match.group("pix") or match.group("vcodec") or ""
        fourcc = _FFMPEG_PIXFMT_TO_FOURCC.get(raw.lower(), raw.upper()[:4])
        formats.append(
            VideoFormat(
                width=int(match.group("maxw")),
                height=int(match.group("maxh")),
                fps_min=float(match.group("minfps")),
                fps_max=float(match.group("maxfps")),
                fourcc=fourcc,
            )
        )

    # Deduplicate: ffmpeg lists a line per discrete rate, and several collapse
    # to the same (resolution, fourcc, max rate) once parsed.
    unique = {(f.width, f.height, f.fourcc, f.fps_max): f for f in formats}
    result = sorted(
        unique.values(),
        key=lambda f: (-f.megapixels, -f.fps_max, _fourcc_rank(f.fourcc)),
    )
    log.info("Device %s reports %d distinct format(s)", device.name, len(result))
    return result


_AUDIO_OPTION = re.compile(r"ch=\s*(\d+),\s*bits=\s*(\d+),\s*rate=\s*(\d+)")

#: HDMI and SDI embed audio at 48 kHz, and so does nearly every interface a
#: booth is likely to plug in. Asking for it spares a resample in the driver.
PREFERRED_AUDIO_RATE = 48_000


def probe_audio_formats(device: AudioDevice) -> list[AudioFormat]:
    """Ask DirectShow which sample formats an audio input offers.

    Why this exists: with no rate specified, ffmpeg takes whichever format the
    capture pin lists first, and on both inputs measured -- a Blackmagic
    UltraStudio Recorder 3G's embedded HDMI audio and a laptop's Realtek
    microphone -- that is 44.1 kHz. HDMI carries 48 kHz, so every take with the
    Blackmagic was resampled from 48 to 44.1 kHz by the driver before ffmpeg
    saw it. Nothing about that was wrong enough to hear; it was a conversion
    nobody asked for, on the one stream a tech is meant to preserve.

    The rate cannot simply be forced. DirectShow refuses to open a format the
    pin does not list, so asking blind for 48 kHz would trade a silent resample
    for an audio input that does not open at all. Ask first.

    Distinct formats, sorted. Empty if ffmpeg is missing or the device does not
    answer -- which callers must treat as "leave the device alone", never as
    "offers nothing".
    """
    output = _run_ffmpeg_list_options(device.name, kind="audio")
    if not output:
        return []
    found = {
        AudioFormat(rate=int(rate), channels=int(channels), bits=int(bits))
        for channels, bits, rate in _AUDIO_OPTION.findall(output)
    }
    return sorted(found)


def best_audio_format(formats: list[AudioFormat]) -> AudioFormat | None:
    """The format to request from an audio input, or None to leave it be.

    48 kHz 16-bit, stereo in preference to mono, when the device lists it.
    Otherwise None rather than a guess: every other outcome is exactly what
    already happens with no format requested, so declining to choose can never
    make a recording worse than it was before this existed.
    """
    candidates = [f for f in formats if f.rate == PREFERRED_AUDIO_RATE and f.bits == 16]
    if not candidates:
        return None
    return max(candidates, key=lambda f: (f.channels == 2, f.channels))


def best_format(formats: list[VideoFormat], *, target_height: int = 1080,
                target_fps: float = 30.0) -> VideoFormat | None:
    """Pick a sensible default from a device's formats.

    Prefers the target resolution at the target rate in the best available
    encoding, then falls back to the closest thing below it. Deliberately never
    picks something larger than asked for: a camera that can do 1440p is not a
    reason to default to encoding 1440p during a four-hour tech.
    """
    if not formats:
        return None

    def score(fmt: VideoFormat) -> tuple[int, int, float, int]:
        return (
            0 if fmt.height <= target_height else 1,      # never overshoot
            abs(fmt.height - target_height),
            -min(fmt.fps_max, target_fps),
            _fourcc_rank(fmt.fourcc),
        )

    return min(formats, key=score)


def _fourcc_rank(fourcc: str) -> int:
    code = fourcc.upper()
    if code in UNREADABLE_FOURCCS:
        return len(FOURCC_PREFERENCE) + 1
    try:
        return FOURCC_PREFERENCE.index(code)
    except ValueError:
        return len(FOURCC_PREFERENCE)


# ---------------------------------------------------------------- ffmpeg shims


def _run_ffmpeg(args: list[str], *, timeout: float = 15.0) -> str | None:
    """Run bundled ffmpeg and return its combined output.

    Device enumeration writes to stderr and exits non-zero by design, so the
    exit code is ignored and only the absence of ffmpeg is treated as failure.
    """
    exe: Path | None = ffmpeg_path()
    if exe is None:
        log.warning("ffmpeg not found; cannot enumerate device capabilities")
        return None
    try:
        completed = subprocess.run(
            [str(exe), "-hide_banner", *args],
            capture_output=True,
            encoding="utf-8", errors="replace",
            timeout=timeout,
            # Without this a console window flashes up on every probe in a
            # windowed build, which looks like a crash.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        log.exception("ffmpeg device probe failed")
        return None
    return (completed.stderr or "") + (completed.stdout or "")


def _run_ffmpeg_devices() -> str | None:
    return _run_ffmpeg(["-list_devices", "true", "-f", "dshow", "-i", "dummy"])


def _run_ffmpeg_list_options(device_name: str, *, kind: str = "video") -> str | None:
    # kind is "video" or "audio". It was hardwired to video=, and asking for an
    # audio input under video= finds no such device and answers with nothing --
    # which would read as "this microphone offers no formats" rather than as
    # the wrong question.
    return _run_ffmpeg(
        ["-list_options", "true", "-f", "dshow", "-i", f"{kind}={device_name}"]
    )
