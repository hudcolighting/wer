"""The text of the in-app help.

No Qt: this is content, and keeping it separate means it can be spell-checked,
diffed and exported without a window.

Three of the sections are **generated from the code** rather than written out --
the widget catalogue from ``wer.overlay.presets``, the layout catalogue from
``wer.overlay.layout_presets`` and the remote control command table from
``wer.connections.osc_control``. The shortcut table and the bus-key reference
are built from lists kept here, ``SHORTCUTS`` and ``BUS_KEY_GROUPS``, and
tests/test_help.py checks them against the window's key bindings, real console
captures and the clock. Documentation that restates a list by hand is
documentation that is wrong within a month.

Everything here describes behaviour that was actually verified against a real
ETCnomad and a real camera, not behaviour that ought to work. Where something is
unconfirmed it says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from wer.licensing import CONTACT_EMAIL

__all__ = ["BUS_KEY_GROUPS", "HelpTopic", "topics", "SHORTCUTS"]


@dataclass(frozen=True)
class HelpTopic:
    key: str
    title: str
    #: Rich text (Qt's HTML subset).
    body: str
    section: str = ""
    #: Words that should find this topic even if they are not in the body.
    keywords: tuple[str, ...] = field(default_factory=tuple)


#: (keys, what it does, where it works)
SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("Ctrl+R", "Start or stop recording"),
    ("Ctrl+M", "Drop a marker, with an optional note"),
    ("F12", "Take a snapshot of the picture, recording or not"),
    ("Ctrl+H", "Panic-hide the overlay (everything stays configured)"),
    ("Ctrl+1 … Ctrl+9", "Switch to that layout"),
    ("Ctrl+L", "Next layout"),
    ("F1", "Open this help"),
    ("Ctrl+Q", "Quit"),
)


def _shortcut_table() -> str:
    rows = "".join(
        f"<tr><td><b><code>{keys}</code></b></td><td>{what}</td></tr>"
        for keys, what in SHORTCUTS
    )
    return f"<table cellpadding=6>{rows}</table>"


def _widget_catalogue() -> str:
    """Built from the actual preset list, so it cannot go stale."""
    from wer.overlay.presets import preset_groups

    parts: list[str] = []
    for group, presets in preset_groups().items():
        parts.append(f"<h3>{group}</h3><table cellpadding=6 width='100%'>")
        for preset in presets:
            reads = (
                "<br><span style='color:gray'>Reads: <code>"
                + "</code>, <code>".join(preset.reads)
                + "</code></span>"
                if preset.reads
                else ""
            )
            parts.append(
                f"<tr><td width='28%' valign='top'><b>{preset.label}</b></td>"
                f"<td valign='top'>{preset.description}{reads}</td></tr>"
            )
        parts.append("</table>")
    return "".join(parts)


def _layout_catalogue() -> str:
    """Built from the ready-made layouts themselves, so it cannot go stale."""
    from wer.overlay.layout_presets import LAYOUT_PRESETS

    rows = "".join(
        f"<tr><td width='22%' valign='top'><b>{preset.name}</b></td>"
        f"<td valign='top'>{preset.description}</td></tr>"
        for preset in LAYOUT_PRESETS
    )
    return f"<table cellpadding=6 width='100%'>{rows}</table>"


def _osc_command_table() -> str:
    """Generated from the command list, so the page cannot drift from the code."""
    from wer.connections.osc_control import COMMANDS

    rows = "".join(
        f"<tr><td valign='top'><code>{command.address}</code></td>"
        f"<td valign='top'>{command.summary}"
        + (
            f"<br><span style='color:gray'>{command.arguments}</span>"
            if command.arguments
            else ""
        )
        + "</td></tr>"
        for command in COMMANDS
    )
    return f"<table cellpadding=6 width='100%'>{rows}</table>"


#: Every bus key worth naming, grouped by where it comes from, as (group,
#: ((key, what it is), ...)). The Bus keys help topic is built from this and
#: so is the overlay editor's Insert key menu -- one list, so the page and the
#: menu cannot disagree. A key with <angle brackets> in it is a pattern for a
#: family of keys, not a key that works as written.
BUS_KEY_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    ("From the console (Eos)", (
        ("eos.cue.active.number", "Active cue number, e.g. 58"),
        ("eos.cue.active.list", "Cue list the active cue is in"),
        ("eos.cue.active.label", "The cue's name as recorded on the desk"),
        ("eos.cue.active.duration", "The cue's fade time in seconds"),
        ("eos.cue.active.remaining", "Seconds left in a running fade"),
        ("eos.cue.active.progress", "Fade progress, 0.0 to 1.0"),
        ("eos.cue.active.fading", "True while a fade is running"),
        ("eos.cue.active.text", "The console's own one-line summary"),
        ("eos.cue.pending.number", "The cue standing by"),
        ("eos.cue.pending.label", "Its name"),
        ("eos.cue.previous.number", "The cue you came from"),
        ("eos.cue.previous.label", "Its name"),
        ("eos.cmdline.text", "Live command line, updating per keystroke"),
        ("eos.cmdline.error", "True when the command line holds an error"),
        ("eos.cmdline.user.<n>.text", "A specific operator's command line"),
        ("eos.show.name", "Show file loaded on the desk"),
        ("eos.chan.active", "Channels currently selected"),
        ("eos.connected", "True while data is arriving"),
        ("eos.softkey.<1-12>", "Softkey labels"),
    )),
    ("From the clock", (
        ("clock.wall", "Time of day, 24 hour"),
        ("clock.wall12", "Time of day, 12 hour with am/pm"),
        ("clock.date", "Date as 2026-09-08"),
        ("clock.date_long", "Date as Tuesday 08 September 2026"),
        ("clock.elapsed", "How long this take has been running"),
        ("clock.elapsed_seconds", "The same, as a number"),
        ("clock.recording", "True while recording"),
        ("clock.take", "Take number, counting up on each stop"),
    )),
    ("Typed by you (Manual tab)", (
        ("manual.show", "Show title"),
        ("manual.act", "Act"),
        ("manual.scene", "Scene"),
        ("manual.note", "A free line, editable mid-take"),
        ("manual.<anything>", "Any field you add on the Manual tab"),
    )),
)


def _bus_reference() -> str:
    parts: list[str] = []
    for group, keys in BUS_KEY_GROUPS:
        rows = "".join(
            "<tr><td valign='top'><code>"
            + key.replace("<", "&lt;").replace(">", "&gt;")
            + f"</code></td><td valign='top'>{what}</td></tr>"
            for key, what in keys
        )
        parts.append(
            f"<h3>{group}</h3><table cellpadding=5 width='100%'>{rows}</table>"
        )
    return "".join(parts)


def topics() -> list[HelpTopic]:
    """Every help topic, in the order they should appear."""
    return [
        HelpTopic(
            "start", "Getting started", section="Basics",
            keywords=("quick", "first", "begin", "setup"),
            body="""
<h2>Getting started</h2>
<p>Wer puts your lighting console's data on top of a camera feed and records the
result, so a recording of a tech shows the cue number, the label and the command
line rather than leaving you to guess which cue you are looking at.</p>

<h3>The short version</h3>
<ol>
<li><b>Open Wer.</b> The camera starts on its own and appears on the
    <b>Preview</b> tab. It is not recording.</li>
<li><b>Connect the console.</b> Go to <b>Connections &rarr; Eos</b>, type the
    console's IP address, press <b>Connect</b>. See
    <i>Connecting to the console</i> if nothing arrives.</li>
<li><b>Type your show name</b> on <b>Connections &rarr; Manual</b>. It appears
    on the overlay and is used in the recording's filename.</li>
<li><b>Check where recordings go</b> on the <b>Recording</b> tab, and that
    <b>Audio</b> names the input the sound should come from. A first launch
    picks Windows' default recording device, which is often the laptop's own
    microphone rather than the capture device's HDMI audio.</li>
<li><b>Press Record</b> on the Preview tab, or <code>Ctrl+R</code>.</li>
</ol>

<p>To grab a still at any point, press <b>F12</b>. That works whether or not you are recording.</p>

<h3>What you get</h3>
<p>One <code>.mkv</code> file with the overlay burnt in, the audio, and every
cue you fired stored inside it as a chapter. Open it in VLC and you can jump
from cue to cue.</p>

<h3>Installed or unzipped</h3>
<p>Run <code>Wer-&lt;version&gt;-setup.exe</code> and Wer installs for you alone
in <code>%LOCALAPPDATA%\\Programs\\Wer</code>, with no administrator prompt, and
can be uninstalled from Add/Remove Programs. Wer is not code-signed, so Windows
may warn before it first runs, and with Smart App Control on, Windows can
refuse to run it at all. From a zip, unzip it anywhere and run <code>Wer.exe</code>;
nothing is installed and there is nothing to uninstall. Either way it writes its
settings to <code>%LOCALAPPDATA%\\Wer</code> and its log next to the program, in
<code>logs</code>. Uninstalling removes the program and its log, and leaves your
settings and recordings alone.</p>
""",
        ),
        HelpTopic(
            "eos", "Connecting to the console", section="Basics",
            keywords=("osc", "eos", "nomad", "console", "network", "ip", "3032",
                      "connect", "no data"),
            body="""
<h2>Connecting to the console</h2>

<h3>On the console</h3>
<p>Eos does not send anything until you tell it to. The settings are in the OSC
section of <b>Setup &rarr; System &rarr; Show Control</b> (on some versions
<b>Setup &rarr; Show &rarr; Show Control</b>). Those menu paths have not been
checked on a desk and move between Eos versions, so look for what each setting
does:</p>
<ul>
<li><b>Enable OSC TX.</b> This is the one that matters. TX is the console
    transmitting, which is what Wer listens to.</li>
<li><b>Enable OSC RX</b> as well, so the handshake Wer sends on connect is
    accepted.</li>
<li><b>OSC TCP Server Port</b> &mdash; the port Eos listens on for a TCP
    connection, and therefore the port Wer must dial. It defaults to 3032, but
    it is freely settable: put whatever is here into <b>Port</b> on the Eos
    tab. If those two numbers disagree, nothing connects.</li>
<li><b>OSC TCP Format</b> &mdash; OSC 1.0 or OSC 1.1. This must match
    <b>TCP framing</b> in Wer, and it is not implied by the port number.
    Getting it wrong looks like a console that connects and then says nothing;
    Wer reports <b>"answered, but not in this framing"</b> when it spots it.</li>
<li><b>Macros that drive Wer</b> need nothing extra over TCP: a macro's
    command came down the same link as the cue data when that was measured.
    UDP has not been tested. See below.</li>
</ul>

<h3>The command line, and whose it is</h3>
<p>The command-line widget shows what is being typed on the desk, and it is one
of the most useful things in a tech recording. It starts on <b>Any user</b>,
which follows whichever operator typed most recently.</p>
<p>Select the widget on the <b>Overlay</b> tab and use <b>Command line
from</b> to pin it to a single operator instead. That is worth doing when a
programmer and a designer are both on the desk and you only want to see one of
them; the rest of the time, Any user is right.</p>
<p>Note that this is a <i>display</i> choice and is separate from the
<b>OSC user</b> setting under <i>In Wer</i> below, which decides what Wer
receives at all. Leave that on 0 and pick the operator here.</p>

<h3>Cue data and macros share one link</h3>
<p>Wer takes the <code>/wer/</code> commands a macro sends from the <b>Eos
connection</b> set up on <b>Connections &rarr; Eos</b>, the same link the cue
numbers, labels and command line arrive on. There is no second port in Wer to
set up.</p>
<p>Measured on Eos running on the same computer, over TCP 3032: each press of
the macro sent the console's own <code>/eos/out/event/macro/1</code>, then the
command, both down the link Wer had dialled. <b>Over UDP this has not been
tested</b>, and nor has a hardware desk, so whether a desk sends a macro's
command to its UDP destination is not known. Either way the Monitor on the Eos
tab shows what arrives, and is the place to look when a macro seems to do
nothing. See <b>Remote control from the console</b>.</p>
<p style='color:#e67e22'><b>OSC TX switched off is the single most common
reason Wer looks broken when it is not.</b> Eos accepts the network connection
whether or not OSC output is enabled, and then sends nothing. Wer shows
<b>"Waiting for data"</b> rather than "Live" in that situation, which is your
cue to go and look at the desk.</p>

<h3>In Wer</h3>
<p><b>Connections &rarr; Eos</b>. The defaults are correct for a
default-configured console:</p>
<table cellpadding=6>
<tr><td><b>Host</b></td><td>The console's IP address. If Eos is running on this
    same computer, <code>127.0.0.1</code> works.</td></tr>
<tr><td><b>Transport</b></td><td>TCP.</td></tr>
<tr><td><b>Port</b></td><td>3032. This is what a default Eos actually talks —
    verified, not assumed.</td></tr>
<tr><td><b>TCP framing</b></td><td><b>OSC 1.0 (length prefix)</b>; the other
    choice is <b>OSC 1.1 (SLIP)</b>. Leave it unless you have changed the
    console's OSC TCP format.</td></tr>
<tr><td><b>OSC user</b></td><td>Whose traffic to receive. <b>0</b> means any
    user, and it is the default because it is what a recording wants.
    <b>This setting does far more than the command line.</b> Eos sends the
    ongoing cue-state stream only to the bound user &mdash; measured on a real
    desk, announcing user 1 to a console being run by user 2 gave <b>zero</b>
    cue updates in 25 seconds, where user 0 gave 26. Cue <i>fire</i> events
    arrive regardless, so the symptom is peculiar and easy to misread: the live
    cue lurches forward on fires alone while the next cue and the fade time
    never move at all. When OSC user is not 0, and two or
    more different cues fire with no cue state arriving for 20 seconds, Wer
    writes a warning to the log and <code>eos.user.mismatch</code> appears in
    the Data Monitor; nothing pops up on screen. 0 avoids the question.</td></tr>
<tr><td><b>Send /eos/subscribe on connect</b></td><td>On. It asks the console to
    send parameter changes unprompted. It costs a second copy of the console's
    state when Wer connects, which is harmless. Leave it ticked.</td></tr>
</table>

<h3>Status meanings</h3>
<table cellpadding=6>
<tr><td><b>Disconnected</b></td><td>Not trying.</td></tr>
<tr><td><b>Connecting</b></td><td>Dialling.</td></tr>
<tr><td><b>Waiting for data</b></td><td>Connected, but the console has said
    nothing. Almost always OSC TX switched off.</td></tr>
<tr><td><b>Live</b></td><td>Data is arriving.</td></tr>
<tr><td><b>Stale</b></td><td>Was live, nothing for two minutes. Normal during a
    quiet stretch; a worry if you are pushing cues.</td></tr>
<tr><td><b>Error</b></td><td>The message says what to check.</td></tr>
</table>

<h3>It reconnects by itself</h3>
<p>A dropped link does not require restarting Wer. It retries with a backoff,
and Eos sends its full state within about a second of any connection, so
nothing needs resyncing by hand. A desk rebooted mid-show has not yet been
watched coming back; check the status reads <b>Live</b> again afterwards.</p>

<h3>It remembers</h3>
<p>Once a console has actually delivered data, Wer reconnects to it
automatically next time you open the program. A host you typed that never
worked is not dialled forever. To stop Wer connecting by itself, untick
<b>Connect to a console when Wer opens</b> under <b>Saved consoles</b>.</p>
<p>If you work in more than one venue, save each desk under its own name and
let Wer pick — see <b>Saved consoles</b>.</p>
""",
        ),
        HelpTopic(
            "consoles", "Saved consoles", section="Basics",
            keywords=("profile", "profiles", "saved", "console", "venue", "switch",
                      "multiple", "find", "search", "ip", "address", "tour",
                      "which console", "second desk"),
            body="""
<h2>Saved consoles</h2>
<p>A laptop that goes between venues should not need its settings retyped on
arrival. Save each desk once, under a name you will recognise, and switch
between them from the dropdown.</p>

<p><b>Connections &rarr; Eos &rarr; Saved consoles.</b></p>

<h3>The list</h3>
<table cellpadding=6>
<tr><td><b>New...</b></td><td>Add another console. It starts on the defaults; fill
    in the address below and press <b>Connect</b>, pressing <b>Disconnect</b>
    first if Wer is already connected. The settings are kept for that console
    when Connect is pressed, not as you type.</td></tr>
<tr><td><b>Duplicate...</b></td><td>Copy the selected one under a new name — handy
    for a second desk on the same rig, where only the last number of the address
    differs. The copy starts as "never connected", because it has not.</td></tr>
<tr><td><b>Rename...</b></td><td>Change the name. Nothing else moves.</td></tr>
<tr><td><b>Delete</b></td><td>Forget a console. The last one cannot be deleted —
    there is always something to edit.</td></tr>
</table>
<p>Everything under <b>Settings for this console</b> — host, transport, port,
framing, OSC user, <b>Send /eos/subscribe on connect</b> — belongs to
whichever console is selected above. Switching the dropdown switches those
fields, and reconnects to the new console if Wer was connected. Anything typed
in them and not yet used with <b>Connect</b> is dropped.</p>
<p>The <b>notes</b> box is free text and is only ever shown to you.
<i>"booth, house left"</i>, <i>"ask Dave to turn OSC on"</i>, <i>"the one with
the broken fader"</i>.</p>

<h3>Which one Wer tries first</h3>
<p>The one you used most recently. There is no favourite to set and nothing to
remember: whichever console last actually sent data goes to the top of the
running order by itself. A console you have saved but never connected to is
tried last — but it <i>is</i> tried, because it may be a rig set up in advance
for tonight.</p>

<h3>If the desk has moved</h3>
<p>When none of the saved consoles answers on its own settings, Wer tries the
other places an Eos is known to transmit before giving up &mdash; TCP 3032 in
both OSC 1.0 and 1.1 framing, TCP 3033, and UDP on 8000 and 8123. If one of
them answers, that console is updated to the settings that actually worked.</p>
<p>This is a last resort, deliberately. Nothing is guessed until every saved
console has been tried honestly and failed, so a profile that names a specific
desk is never quietly redirected to a different one.</p>
<p style='color:#e67e22'>It is worth knowing that the port does not tell you
the framing. A real Ion XE was found transmitting <b>UDP to 8123</b>, and on
TCP 3032 it answered in OSC 1.1, not the 1.0 that port usually implies, with
only a state dump. If Wer says <b>"answered, but
not in this framing"</b>, that is what happened &mdash; the desk is fine, the
setting is wrong.</p>

<h3>Finding one on startup</h3>
<p>When you open Wer it goes down that list and connects to the first console
that answers. In a venue you were in last night that is instant, because the
right one is already first. In a new venue it takes a few seconds per dud
address and then lands on the right desk without anyone touching the
settings.</p>
<p>Progress appears next to the button and in the status bar, so a search never
looks like a hang. <b>Find a console</b> runs the same search on demand — use it
when you have plugged into a different network and would rather not think about
which entry is which.</p>
<p>If you would rather Wer did not try every saved console on startup, and only
ever dialled the one it used last, untick <b>...and try every saved console, not
just the last one</b> under <b>Saved consoles</b>. Unticking <b>Connect to a
console when Wer opens</b>, just above it, turns the startup connection off
altogether and greys that box out. Nothing is dialled on startup until a console
has connected at least once.</p>

<h3>What counts as "found"</h3>
<p style='color:#e67e22'>A console that accepts the connection but has OSC
output switched off does <b>not</b> count as found, and the search moves on to
the next one.</p>
<p>This is deliberate, and it is the whole reason the search is worth having.
Eos accepts a network connection whether or not OSC TX is enabled, and then
sends nothing — so "the socket opened" proves nothing at all. A search that
stopped there would happily attach to the wrong desk and sit silent all night.
Wer waits to hear the console speak first. That is a fast test rather than a
patient one, because Eos volunteers its whole state within about a second of a
client connecting.</p>
<p>The result line tells you which it was:</p>
<table cellpadding=6>
<tr><td><b>talking</b></td><td>Found. This is the one it connects to.</td></tr>
<tr><td><b>connected but silent</b></td><td>The desk is there and reachable, but
    OSC output is off. Go and enable OSC TX — see
    <b>Connecting to the console</b>.</td></tr>
<tr><td><b>no answer</b></td><td>Nothing at that address. Wrong IP, wrong
    subnet, or the console is not running.</td></tr>
<tr><td><b>refused</b></td><td>The computer is there but nothing is taking
    connections on that port. Eos may not be running, or its OSC TCP may be
    off.</td></tr>
<tr><td><b>unreachable</b></td><td>No route to that address. Check the
    network.</td></tr>
<tr><td><b>answered, but not in this framing</b></td><td>The desk is sending,
    in the other OSC version. See <i>If the desk has moved</i> above.</td></tr>
<tr><td><b>nothing arrived on this port</b></td><td>A UDP setting, and nothing
    arrived while Wer listened.</td></tr>
<tr><td><b>something is sending to this port, but it is not Eos</b></td><td>A
    UDP setting with another device on the port. It does not count as
    found.</td></tr>
</table>
<p>If nothing at all answers, Wer says so and lists what it found at each
address rather than leaving you guessing.</p>

<h3>Where they are kept</h3>
<p>In the show file, with everything else. Show files from before this feature
existed are upgraded on load: the single console they held becomes a saved
console named after its address, or <b>This computer</b> for Eos running on this
computer, so nothing is lost.</p>
""",
        ),
        HelpTopic(
            "camera", "Camera and picture", section="Basics",
            keywords=("camera", "webcam", "usb", "resolution", "fps", "mjpg",
                      "blackmagic", "device", "format", "mirror", "flip",
                      "upside down", "rotate"),
            body="""
<h2>Camera and picture</h2>
<p>The camera runs whenever Wer is open — before, during and after a take. There
is no separate "start camera" step, and stopping a recording never stops the
picture.</p>

<h3>Choosing a camera</h3>
<p><b>Preview</b> tab, <b>Device</b>. <b>Refresh</b> looks again if you plugged
something in after opening Wer. Changing camera restarts the picture on its own.</p>
<p><b>Device</b>, <b>Format</b> and <b>Refresh</b> are locked while recording,
because changing the camera would end the take's video. Stop the take first.
Wer remembers the camera by its name rather than its number, because Windows can
renumber devices when one is unplugged and plugged in again, and picks that
camera again the next time Wer opens.</p>

<h3>A camera mounted upside down, or seen in a mirror</h3>
<p>In the same box, under <b>Device</b>: <b>Mirror left to right</b> and
<b>Flip top to bottom</b>. Tick both for a camera mounted upside down; together
they turn the picture half a turn. Mirror alone is for a camera that sees the
stage in a mirror.</p>
<p>A change shows at once, without the camera being reopened, and the preview,
the recording and snapshots all get the same picture. The overlay is drawn on
afterwards, so its text always reads the right way round. Both stay available
while recording: each change is written to the log, so a picture that turns
over partway through a take can be explained afterwards. They are saved with
your other settings, and are off until you tick them.</p>

<h3>Format matters more than it looks</h3>
<p>The <b>Format</b> box lists what your camera actually reports, including its
pixel format. This is worth understanding:</p>
<p>Many USB cameras cannot sustain 1080p30 in an uncompressed format and quietly
drop to a few frames a second instead of refusing. If the picture looks stuttery,
check the <b>fps measured</b> figure under the preview. If it is far below what
you asked for, choose an <b>MJPG</b> format if your camera offers one.</p>
<p>Not every camera has MJPG. Some offer only NV12 and YUY2, and manage full
rate anyway.</p>

<h3>What the numbers under the picture mean</h3>
<table cellpadding=6>
<tr><td><b>fps measured</b></td><td>What you are actually getting, not what was
    requested. The number that reveals a struggling camera.</td></tr>
<tr><td><b>backend</b></td><td>MSMF or DSHOW, whichever delivered closest to the
    rate asked for. With more than one camera attached only DSHOW is used,
    because its device numbers are the ones the <b>Device</b> list
    uses.</td></tr>
<tr><td><b>MB/s raw</b></td><td>How much uncompressed video is moving through
    the pipeline. Explains why 4K behaves differently from 1080p.</td></tr>
<tr><td><b>preview drops</b></td><td>Frames the on-screen preview skipped.
    Harmless — the preview is allowed to drop, the recording is not.</td></tr>
<tr><td><b>ENCODER DROPS</b></td><td>Frames missing from the <i>recording</i>.
    Never harmless. See Troubleshooting.</td></tr>
<tr><td><b>Starting…</b></td><td>The camera has just been opened and no frame
    has arrived yet. Normal for a moment after opening Wer or changing
    camera.</td></tr>
<tr><td><b>NO SIGNAL</b></td><td>No frames have arrived for the time shown, or
    <i>no frames yet</i> if none ever came: the camera has stopped delivering,
    or never started, for example because it was unplugged.</td></tr>
<tr><td><b>CAMERA LOST</b></td><td>During a take the camera delivered nothing for
    5 seconds, so Wer closed it and is opening it again, waiting longer between
    attempts, up to 30 seconds. The take carries on and the gap is in the
    recording. If the take ends with the camera still lost, press
    <b>Refresh</b>.</td></tr>
</table>

<h3>Capture cards and other cameras</h3>
<p>Anything that presents itself to Windows as a camera should work. A capture
card or box appears in the <b>Device</b> list under its own name, once its
maker's drivers are installed, and then behaves like a camera with a few habits
of its own:</p>
<ul>
<li><b>What it can take is a property of the unit</b>, not of Wer: which inputs,
    which resolutions and which rates vary from model to model, and a signal a
    unit cannot take gives no picture at all. Check what the device itself
    accepts.</li>
<li>Of the formats it lists, often only one actually delivers. <b>V210</b>,
    <b>R210</b> and <b>BGR0</b> open, accept the setting and then never send a
    frame, so Wer ranks them below every format it can read and starts on one
    of those instead &mdash; <b>UYVY</b> on a Blackmagic. All three stay in the
    list; pick one and Wer falls back to the driver's own format.</li>
<li><b>If its input signal is lost it sends black frames</b> at full rate rather
    than stopping, so the numbers under the picture stay normal and nothing
    says so. A blackout on stage is dark too. Watch the picture, not the
    numbers.</li>
<li>Its sound is a separate input, named after the device &mdash; something of
    the shape <b>Line In (… Audio)</b> &mdash; chosen under <b>Audio</b> on
    the Recording tab.</li>
</ul>
""",
        ),
        HelpTopic(
            "recording", "Recording", section="Basics",
            keywords=("record", "quality", "crf", "encoder", "nvenc", "mkv",
                      "mp4", "audio", "sync", "offset", "disk", "filename"),
            body="""
<h2>Recording</h2>
<p>Press <b>Record</b> on the Preview tab or <code>Ctrl+R</code>. Press it again
to stop. The picture keeps running either way.</p>

<h3>Where recordings go</h3>
<p><b>Recording</b> tab. <b>Browse</b> to pick a folder, <b>Open</b> to see it in
Explorer. The <b>Filename</b> box accepts tokens:</p>
<table cellpadding=6>
<tr><td><code>{show}</code></td><td>Show name from the Manual tab</td></tr>
<tr><td><code>{date}</code></td><td>2026-09-08</td></tr>
<tr><td><code>{time}</code></td><td>21-57-31</td></tr>
<tr><td><code>{take}</code></td><td>Take number, 001, 002…</td></tr>
</table>
<p>Characters Windows will not accept in a filename are replaced automatically,
so a show called <i>Act 1/2: The Sequel</i> is fine. An existing file is never
overwritten — a number is added instead.</p>

<h3>Quality</h3>
<p>Four levels, and the underlying number is shown so you can see what changed:</p>
<table cellpadding=6>
<tr><td><b>Archive</b></td><td>Near-transparent, largest files. For a recording
    you intend to keep or edit from.</td></tr>
<tr><td><b>Standard</b></td><td>Good quality at a sane size. The right default
    for documenting a tech.</td></tr>
<tr><td><b>Compact</b></td><td>Visibly compressed on fine detail, but cue text
    stays legible. For long sessions or a full disk.</td></tr>
<tr><td><b>Tiny</b></td><td>Heavily compressed. For sending a run to someone on
    a connection that will not carry more.</td></tr>
</table>

<h3>Record at a different size than you capture</h3>
<p><b>Record at</b> is independent of the camera. Capture 4K and record 1080p if
you want a manageable file, or record 4K at a lower quality. This is the main
lever for making a 4K source comfortable.</p>

<h3>Encoder</h3>
<p>Wer tests each encoder on your machine rather than trusting a list, so what
you see offered actually works here. An encoder that is present but unusable
stays visible and says why — for example a graphics driver too old for NVENC,
which a driver update fixes.</p>
<p>The default is <b>Automatic</b>: the best hardware encoder the check at launch
found working on this machine, and software if there is none, so it follows the
machine rather than needing to be set per venue. The box names the encoder it
picked. <b>Software (x264)</b> works on every machine and is the safe choice.
Hardware encoders are faster and lighter on the processor.</p>

<h3>Container</h3>
<p><b>MKV</b> by default, and it is the right default: a crash or a power cut
four hours into a tech does not destroy an MKV. MP4 is offered for compatibility
and is written fragmented so an interrupted file is still playable. An MP4 gets
its chapters but cannot hold the marker list and bus log inside it, so they stay
beside it; see <i>Markers and what is in the file</i>.</p>

<h3>Audio and A/V sync</h3>
<p>Pick the audio input to record under <b>Audio</b>, or <b>None — video
only</b>. The usual source is the capture device's HDMI audio, which appears as
an input of its own, named after the device &mdash; something of the shape
<i>Line In (… Audio)</i>. On a first launch Wer starts on Windows' default
recording device, or the first input listed if that is not there, which may be
a laptop's own microphone rather than the input the show needs, so check it.
Each input is listed with the format it will be recorded at, or <i>default
format</i> when it did not offer 48&nbsp;kHz 16-bit; a saved input that is not
plugged in stays listed as <i>not connected</i>. Wer checks the device exists
before starting, so a missing input is caught before you think you are
recording sound.</p>
<p>While a take records, Wer checks the sound in two separate ways. The first is
<b>how much of it arrives</b>. It looks at the last half minute of sound, and if
less than 90% of it reached the recording &mdash; or none of it did, because the
input stopped &mdash; the status bar says so, about half a minute in, and the
message stays there until the next take is started here. The end-of-take message
repeats it when the take as a whole came up short. Some USB webcam microphones
deliver only about half their sound, in short gaps, whatever format they are set
to. The picture is unaffected; record sound from another input.</p>
<p>That first check counts sound, not level: it is about samples going missing,
so an input that is simply quiet passes it, and so does one handing over nothing
but silence. The second check is therefore <b>whether there is anything in the
sound at all</b>. Wer reads the level of each second as it is recorded, and a
second whose loudest moment sits at the very bottom of the scale &mdash; exact
zeros, or a hair above them, which no room reaches however quiet the house is
&mdash; came from an input delivering nothing. After fifteen unbroken seconds of
that the status bar says <b>No sound from <i>the input</i>: only digital silence
for 15&nbsp;s</b>. The take carries on recording and nothing is thrown away; it
is a warning, not a stop. A laptop's own microphone with its voice effects
switched on does this, and so does a capture device whose HDMI carries no audio.
See <i>The recording has no sound</i> under <i>Troubleshooting</i>.</p>
<p><b>A/V offset</b> lines the sound up with the picture. Positive delays the
sound, negative delays the picture. Wer keeps one for each camera and audio
input, and switches to it when you switch devices. Until you set one, the
<b>automatic</b> value is used, which is 0&nbsp;ms at present, and the line
under the box says so. Change the number and it sticks for that camera and audio
input; <b>Use automatic</b>, which appears once one is set, goes back. The offset
is fixed when a take starts, so the box is locked while recording, and an offset
applied from a clap test during a take is used from the next take.</p>
<h3>Clap test</h3>
<p>To measure the offset rather than guess it: press <b>Record</b> with the
camera and audio input the show will use, have someone on stage, in view of the
camera, clap sharply six to ten times about a second apart, hands side-on to the
camera, and stop. Then press <b>Clap test…</b> on the Recording tab. It measures
the last take finished since Wer was opened, so do it before closing Wer. Press
<b>Measure this take</b> and Wer finds each clap in the sound; you drag a box
around the hands and press <b>Next</b>. Then, for each clap, pick the first frame
where the hands meet and press <b>Use this frame</b> (or double-click it); pick
the closest if none shows them together, or press <b>Skip this clap</b> if the
hands cannot be seen. It works out how far the sound is from the picture and
offers the offset that cancels it: press <b>Use <i>n</i> ms for this camera and
audio input</b> and the next take uses it. It is kept for the camera and audio
input the claps were recorded with, even if you have switched since. Clap on
stage rather than at the microphone: the sound's travel time from the stage is
then measured and taken out too.</p>

<h3>Disk space</h3>
<p>The Recording tab shows free space in gigabytes and, beside it, roughly how
many <i>hours</i> that is at 1080p Standard, because hours is the useful unit.</p>
<p>Free space is checked before arming and <b>every ten seconds while
recording</b>. The low-space warning during a take gives the minutes left from
the rate the file is actually growing, rather than a guess.</p>
<table cellpadding=6>
<tr><td><b>Warning</b></td><td>At 20 GB, so there is time to react.</td></tr>
<tr><td><b>Stop</b></td><td>At 10 GB, adjustable on the Recording tab.
    <b>Recording stops itself.</b></td></tr>
</table>
<p style='color:#e67e22'>Stopping with room in hand is deliberate. The remaining
space is not spare: the file has to be closed, and embedding the markers
rewrites it, which briefly needs its size again. Running to the last byte would
risk all of that, and a disk that fills mid-write does not fail politely.</p>
<p>Wer also refuses to start a recording that is already below the stop level,
rather than recording for a few seconds and stopping itself.</p>

<h3>If the encoder fails mid-tech</h3>
<p>If ffmpeg dies partway through — it should not, but three hours is a long
time — Wer starts a fresh one writing <code>...-part2.mkv</code> and carries on.
What is lost is about a second, rather than the rest of the session. You are
told, and the status message names every file produced so nothing is hidden.</p>
<p>A single failure costs about that second. If ffmpeg keeps failing, each
restart after the first waits longer than the one before, up to half a minute,
and the picture in those pauses is lost. If the audio input looks to be the
cause, the rest of the take carries on <b>without sound</b> rather than losing
the picture, and the status message says so. After five restarts in a row
without a minute of good recording in between, the next failure ends the take;
if the take still has its sound, one last try is made without it first.</p>
<p>There is no switch for this on the Recording tab. It is on unless the show
file's <code>continue_after_failure</code> is set to <code>false</code>.</p>
""",
        ),
        HelpTopic(
            "widgets", "The widgets", section="The overlay",
            keywords=("widget", "cue", "clock", "date", "elapsed", "duration",
                      "lamp", "text", "catalogue", "add"),
            body="<h2>The widgets</h2>"
            "<p>Everything you can put on the picture. Add them from "
            "<b>Overlay &rarr; Add widget</b>.</p>"
            "<p>Most are text with a template, and you can edit that template to "
            "say anything you like — see <i>Writing your own text</i>.</p>"
            + _widget_catalogue(),
        ),
        HelpTopic(
            "editing", "Editing the overlay", section="The overlay",
            keywords=("edit", "move", "drag", "resize", "colour", "property",
                      "position", "anchor", "snap", "grid", "align", "nudge",
                      "arrow", "lock", "style", "copy", "paste", "font",
                      "preview", "sample"),
            body="""
<h2>Editing the overlay</h2>
<p>The <b>Overlay</b> tab. Everything you change appears on the picture
immediately, which is the only way to judge whether something is legible over an
actual lit stage.</p>

<h3>The picture beside the settings</h3>
<p>Above the widget's settings is a small picture of the whole layout, redrawn
as you type, with the selected widget ringed in green. It is there so that
making a widget does not mean changing a size, going to the Preview tab to look,
and coming back.</p>
<p>It is drawn over a stand-in stage with made-up show data &mdash; a cue
number, a command line, a show name &mdash; so a layout can be built at a desk
in the afternoon with no console and no camera, and the text still comes out a
believable length. Those values are never the console's own, and they can never
reach a recording. The <b>Preview</b> tab is the live picture.</p>

<h3>Moving things</h3>
<p>Tick <b>Edit on preview</b>, then go to the <b>Preview</b> tab. Every widget
gets an outline. Click one to select it and drag it where you want. The
selection is shown in green with its name and, while you drag, the corner it
will hang from.</p>
<p>While a widget moves, a grid is drawn over the picture with the centre lines
and the edge margin, and a magenta line shows anything it has snapped to.</p>
<table cellpadding=6>
<tr><td><b>Alt</b> while dragging</td><td>No snapping, to place something just so</td></tr>
<tr><td><b>Shift</b> while dragging</td><td>Keep the drag straight across or straight down</td></tr>
<tr><td><b>Esc</b> while dragging</td><td>Put the widget back where it started</td></tr>
<tr><td><b>Arrow keys</b></td><td>Move the selected widget one pixel. Never snaps.</td></tr>
<tr><td><b>Shift + arrow keys</b></td><td>Move it one grid square</td></tr>
</table>
<p>The <b>Position</b> tab of the widget's settings sets the same thing in
numbers.</p>

<h3>Snapping</h3>
<p>With <b>Snap</b> ticked, a dragged widget is pulled onto the frame's edge
margins and centre lines, and into line with the other widgets &mdash; their
edges, their centres, and just below or beside them, so a date can sit under a
clock instead of landing on top of it. Across and down snap separately, so a
widget can be centred across the picture and still sit anywhere up and down.</p>
<p>It only catches within a few pixels of a line, and lets go as soon as you
move past it.</p>
<p><b>Snap to grid</b> pulls widgets onto the grid lines as well; it starts off.
<b>Grid</b> sets how fine the grid is. All three are saved with the show.</p>

<h3>Corners, not coordinates</h3>
<p>A widget is anchored to one of nine points — a corner, an edge or the centre —
and then nudged from there. This is deliberate and it matters: it means a layout
you build here looks the same when recorded at a different resolution, and a
widget anchored bottom-right stays in its corner as its text grows.</p>
<p>That last point is why the command line does not slide off frame when you type
a long command, which is exactly when you want to read it. Dragging a widget
onto a margin or a centre line sets its corner for you.</p>

<h3>A widget's settings</h3>
<p>Select a widget and its settings appear in four tabs. Each kind of widget
shows only the settings it has.</p>
<ul>
<li><b>Content</b> &mdash; what it says. Text is a template that can run to
    several lines (press Enter); <b>Insert key</b> picks a bus key from a list,
    including everything on the bus right now. A cue block chooses its lines:
    the cue list, the label, the cue it came from, the next cue and the fade
    bar. A lamp sets the key it follows, its on and off words and colours, and
    whether it disappears when off. Pictures, shapes and fade bars set their
    size here.</li>
<li><b>Position</b> &mdash; the corner, the nudges, the edge margin, the
    <b>widest</b> it may draw (longer text is cut with an ellipsis, so a long
    command line cannot run into the show name), its layer, and <b>Lock in
    place</b>, which stops it being dragged.</li>
<li><b>Style</b> &mdash; font, size, weight, colour, alignment, opacity and the
    legibility aids. <b>Copy style</b> and <b>Paste style</b> give one widget
    the look of another, keeping its own text size.</li>
<li><b>Visibility</b> &mdash; when it appears, how long after its last change it
    hides, and how long it takes to fade.</li>
</ul>

<h3>Text size</h3>
<p>Size is a fraction of picture height, not a pixel count. <code>0.045</code> is
about 49&nbsp;px at 1080p and 97&nbsp;px at 4K — the same apparent size in both.
That is what makes a layout survive a change of recording resolution.</p>

<h3>Legibility</h3>
<p><b>Dark box</b>, <b>Outline</b> and <b>Drop shadow</b> are all on by default,
and that is not decoration. A tech is shot into a lit stage whose brightness
swings wildly cue to cue, and white text over a followspot is invisible without
help. Turn them off only if you can see the result is still readable. The box's
colour, opacity, padding and corner radius can all be changed.</p>

<h3>Showing a widget only sometimes</h3>
<p><b>Appear</b> on the Visibility tab, with a bus key. <i>While a key has a
value</i> shows the widget whenever the key holds anything &mdash;
<code>manual.note</code>, so a note appears only when you have typed one.
<i>While a key is true</i> shows it only while the key is true &mdash;
<code>eos.cue.active.fading</code>, so a countdown appears only during a
fade.</p>
<p><b>Hide after</b> fades a widget out a set time after the last change to
anything it shows, and brings it back on the next.</p>

<h3>Writing your own text</h3>
<p>The <b>Content</b> box is a template. Anything in braces is replaced live:</p>
<p><code>Cue {eos.cue.active.number} &mdash; {eos.cue.active.label}</code></p>
<p>becomes</p>
<p><code>Cue 58 &mdash; Beatrix Xs Center</code></p>
<p>A template can have several lines. Tick <b>Lines after the first are
smaller</b> for the time large with the date beneath it: one widget, so the two
can never overlap or drift apart.</p>
<p>The <b>Data Monitor</b> tab lists every key currently available, with its
value, so you never have to guess a name. <i>Bus keys</i> in this help lists the
ones most worth using.</p>
<p>A key with no data shows as <code>--</code> rather than going blank, so you
can tell "the console has not said" from "the value is empty".</p>

<h3>Layers</h3>
<p><b>Bring forward</b> and <b>Send back</b>, or set <b>Layer</b> directly.
Higher numbers draw on top. Give a backing strip or box a lower layer than the
widgets sitting on it.</p>
""",
        ),
        HelpTopic(
            "layouts", "Layouts", section="The overlay",
            keywords=("layout", "tech", "minimal", "none", "archive", "switch",
                      "preset", "hotkey", "ready-made", "programming",
                      "performance", "lower third", "notes", "documentation",
                      "system check", "clean", "no overlay"),
            body="""
<h2>Layouts</h2>
<p>A layout is a named set of widgets. A new show starts with <b>Tech</b>,
<b>Minimal</b> and <b>None</b>, live on Tech. <b>None</b> has nothing on it: it
is there so the overlay can come off for a stretch that has to be recorded
clean, without taking anything apart. <b>New...</b> on the Overlay tab offers
every ready-made layout below, with a picture of each over a sample stage:</p>
"""
            + _layout_catalogue()
            + """
<h3>Switching</h3>
<p><code>Ctrl+1</code>, <code>Ctrl+2</code> and <code>Ctrl+3</code> for the
three a new show starts with, or <code>Ctrl+L</code> to cycle, or the
<b>Layout</b> menu. <b>You can switch while recording</b> — that is the point of
them. The change appears in the recording from that frame on.</p>

<h3>Making your own</h3>
<p>On the <b>Overlay</b> tab:</p>
<ul>
<li><b>New</b> starts from any of the layouts above, or from nothing. Give it
    a name and it is yours to change.</li>
<li><b>Duplicate</b> copies the layout you are on under a new name. The copy is
    fully independent — editing it does not touch the original.</li>
<li><b>Rename</b> and <b>Delete</b> do what they say. The last layout cannot be
    deleted; there would be no way back.</li>
<li><b>Reset to default</b> puts a layout named after a ready-made one back the
    way it ships, if you have edited it and want to start again. Your own
    layouts have no default to return to.</li>
</ul>
<p>The first nine layouts, in the order they are listed, get <code>Ctrl+1</code>
to <code>Ctrl+9</code> and a place on the <b>Layout</b> menu. A new layout goes to
the end of the list and takes the next number; deleting one moves every layout
after it down a number. A tenth layout has neither, but <code>Ctrl+L</code> still
cycles to it.</p>

<h3>They are saved for you</h3>
<p>Layouts, widget positions and everything else are saved automatically. There
is no Save button and nothing to remember.</p>
""",
        ),
        HelpTopic(
            "markers", "Markers and what is in the file", section="Recording",
            keywords=("marker", "chapter", "vlc", "csv", "note", "jump",
                      "attachment", "sidecar", "bus log"),
            body="""
<h2>Markers and what is in the file</h2>
<p>A recording you have to scrub through is only half useful. Wer marks every
cue as it fires, so a four-hour tech opens with every cue already labelled.</p>

<h3>How markers get there</h3>
<ul>
<li><b>Automatically</b>, every time the console fires a cue. The marker carries
    the cue number and its label. <b>Go To Cue</b> counts: the console sends the
    same fire event for it as for <b>GO</b>, so scrubbing back and forth during a
    tech drops a marker for every jump, and a tech that has run cue 42 six times
    has six markers on it. There is nothing to filter that with. The only control
    is the single <b>Drop a marker on every cue fire</b> tickbox on the Recording
    tab, and it is all or nothing: leave it on and live with the extra marks, or
    turn it off and mark the moments you want by hand on
    <code>Ctrl+M</code>.</li>
<li><b>By hand</b>, on <code>Ctrl+M</code>, while recording. You are asked for a
    note, which is optional — press Enter to drop an unlabelled mark. Outside a
    take the status bar says markers are only dropped while recording.</li>
<li><b>From the console</b>, while recording, when a macro sends
    <code>/wer/marker</code>, with an optional note and no dialog. See
    <i>Remote control from the console</i>.</li>
<li><b>With a snapshot</b> taken during a take, naming the still, unless that is
    turned off on the Recording tab. See <i>Snapshots</i>.</li>
</ul>
<p>Markers appear in the list on the Recording tab as they are dropped, so you
can see they are working.</p>

<h3>Opening the result</h3>
<p>The cues are written into the video file itself as <b>chapters</b>. Open the
recording in <b>VLC</b> and they are under <b>Playback &rarr; Chapter</b>; you
can jump between them with <code>Shift+N</code> for the next and
<code>Shift+P</code> for the previous. mpv and most other players show them too.
No special software and no import step.</p>

<h3>What else is inside the file</h3>
<p>Two more things are tucked inside the <code>.mkv</code> as attachments:</p>
<table cellpadding=6>
<tr><td><b>markers.csv</b></td><td>Every marker with its timecode, cue number,
    label and note. Opens in Excel.</td></tr>
<tr><td><b>bus.jsonl</b></td><td>Everything the console sent, timestamped
    against the video. It is what would let a different overlay be put on this
    footage later without re-running the tech.</td></tr>
</table>
<p>This means one file carries everything. Copy it to a stick and nothing is
left behind.</p>
<p><b>An MP4 cannot hold attachments.</b> An MP4 recording still gets its
chapters, but <b>markers.csv</b> and <b>bus.jsonl</b> stay beside it as ordinary
files, whatever the switches below say, and the message when the take is saved
says so. Copy all three together. For one file that carries everything, record
MKV.</p>

<h3>The switches</h3>
<p>On the <b>Recording</b> tab, under <b>Markers</b>:</p>
<table cellpadding=6>
<tr><td><b>Drop a marker on every cue fire</b></td><td>Off keeps only your
    manual markers.</td></tr>
<tr><td><b>Put markers inside the video file</b></td><td>Off leaves the marker
    files beside the video instead, and skips the rewrite.</td></tr>
<tr><td><b>Also leave the .csv and .jsonl beside it</b></td><td>On keeps copies
    outside the video as well. An MP4 recording keeps them beside it either
    way.</td></tr>
<tr><td><b>Log all console data alongside the recording</b></td><td>Off skips
    the bus log.</td></tr>
</table>

<h3>Why stopping takes a moment</h3>
<p>Putting the markers inside the file means writing the file again. Nothing is
re-encoded, so no quality is lost, but every byte is copied — a 20&nbsp;GB tech
takes a couple of minutes and needs that much free space while it runs.</p>
<p>Wer stays responsive throughout and shows <b>Finishing</b>. Your original
recording is never replaced until the new file is complete and checked, so if
anything goes wrong the recording is exactly as it was.</p>
""",
        ),
        HelpTopic(
            "snapshots", "Snapshots", section="Recording",
            keywords=("snapshot", "still", "photo", "picture", "screenshot",
                      "f12", "png", "jpeg", "grab"),
            body="""
<h2>Snapshots</h2>
<p>Press <b>F12</b>, or the <b>Snapshot</b> button on the Preview tab, to save a
still of what you can see. <b>It works whether or not you are recording</b> —
the camera is live from the moment Wer opens, and snapshots have nothing to do
with a take.</p>

<h3>What gets saved</h3>
<p>Exactly the picture on screen, overlay included. If the cue number and label
are visible in the preview, they are in the still.</p>

<h3>Where they go</h3>
<p>A <b>Snapshots</b> folder beside your recordings, unless you choose somewhere
else on the <b>Recording</b> tab. <b>File &rarr; Open Snapshots Folder</b> takes
you there.</p>

<h3>The filename includes the cue</h3>
<p>The default is
<code>{show}_{date}_{time}_cue{cue}</code>, which gives you something like:</p>
<p><code>The Quiet Neighbours_2026-09-08_21-57-31_cue58.png</code></p>
<p>That is usually the whole point of taking a still during a tech — the cue it
was showing is in the name, so a folder of them is browsable without opening
anything. The same tokens as the recording filename are available, plus
<code>{cue}</code>.</p>
<p>Two snapshots in the same second do not overwrite each other; a number is
added.</p>

<h3>PNG or JPEG</h3>
<table cellpadding=6>
<tr><td><b>PNG</b></td><td>Lossless and the default. A still of a lighting state
    is a thing you may want to look at closely, and losing detail to compression
    would defeat the purpose. Roughly 1–3&nbsp;MB at 1080p.</td></tr>
<tr><td><b>JPEG</b></td><td>Much smaller, and fine for a quick record of a
    moment or for sending to someone.</td></tr>
</table>

<h3>Snapshots during a recording</h3>
<p>Taking one while recording also drops a <b>marker</b> naming the file, so the
still and the moment in the video can be found from each other afterwards. Turn
that off on the Recording tab if you would rather not.</p>
""",
        ),
        HelpTopic(
            "monitor", "The Data Monitor", section="Reference",
            keywords=("monitor", "debug", "keys", "data", "stale", "diagnose"),
            body="""
<h2>The Data Monitor</h2>
<p>A live table of every piece of data Wer currently holds. It is the fastest
way to answer the question every other problem reduces to: <i>is the data
arriving?</i></p>

<p>If a widget is blank, look here first. It separates "the console is not
talking" from "the widget is set up wrong", which are very different problems
with very different fixes.</p>

<h3>Reading it</h3>
<table cellpadding=6>
<tr><td><b>Key</b></td><td>The name you use in a widget template.</td></tr>
<tr><td><b>Value</b></td><td>What it is right now.</td></tr>
<tr><td><b>Type</b></td><td>The kind of value: <code>str</code> for text,
    <code>int</code> or <code>float</code> for a number, <code>bool</code> for
    true or false.</td></tr>
<tr><td><b>Age</b></td><td>How long since it last arrived, changed or not. The
    console re-sends values that have not changed, and each one starts this again
    from 0.</td></tr>
<tr><td><b>Source</b></td><td>Which connection published it.</td></tr>
</table>
<p><span style='color:#2ecc71'><b>Green</b></span> means it arrived in the last
second. <span style='color:#c0392b'><b>Red</b></span> means the value has gone
stale and should not be trusted: it has not been sent again within the time that
kind of value is good for. Some values never go stale, the date and the take
number among them.</p>

<h3>Filtering</h3>
<p>Type in the filter box to narrow the list by key name — <code>eos.cue</code>
for cue data, <code>clock</code> for the clock. <b>Pause</b> freezes the view
without affecting anything else. <b>Clear</b> empties the table; a key comes back
when its value next changes, or straight away when <b>Pause</b> is ticked and
unticked.</p>

<h3>The Eos tab has a monitor of its own</h3>
<p><b>Connections &rarr; Eos</b> shows raw traffic exactly as it arrives from the
console. That is the place to look if the Data Monitor is empty but you believe
the console is sending.</p>
""",
        ),
        HelpTopic(
            "keys", "Bus keys", section="Reference",
            keywords=("key", "template", "variable", "reference", "bus"),
            body="<h2>Bus keys</h2>"
            "<p>The values most worth putting in a widget template. Use them in"
            " braces: <code>Cue {eos.cue.active.number}</code></p>"
            "<p>The <b>Data Monitor</b> tab shows every key live with its current"
            " value: these, a few more the console sends that are rarely worth"
            " showing, and any extra fields you add on the Manual tab.</p>"
            + _bus_reference(),
        ),
        HelpTopic(
            "shortcuts", "Keyboard shortcuts", section="Reference",
            keywords=("hotkey", "shortcut", "keyboard", "key"),
            body="<h2>Keyboard shortcuts</h2>"
            "<p>These work from anywhere in the program, so you do not have to be"
            " on the right tab during a tech.</p>" + _shortcut_table()
            + "<p><b>Panic-hide</b> removes the whole overlay from the picture"
            " immediately, without losing any of your setup. Press it again to"
            " bring it back. It affects the recording as well as the preview —"
            " it is for when something on screen should not be in the"
            " recording.</p>",
        ),
        HelpTopic(
            "osccontrol", "Remote control from the console", section="Reference",
            keywords=("osc", "remote", "macro", "control", "trigger", "automate",
                      "desk", "tap"),
            body="<h2>Remote control from the console</h2>"
            "<p>A macro on the Eos desk can tell Wer to start and stop recording, "
            "drop a marker, take a snapshot or switch layouts, so a recording can "
            "be armed at the top of a session and stopped at the end without "
            "anyone walking to the computer.</p>"
            "<p>Wer takes the commands from the <b>Eos connection</b> it already "
            "has to the desk, alongside the cue data. There is nothing to switch "
            "on and no port to choose in Wer. Over TCP that was measured working: "
            "the commands came down the same link as the cues. Over UDP, or from "
            "a hardware desk, it has not been tested yet, so watch the Monitor "
            "the first time.</p>"
            "<p>This is control coming <i>in</i>. Wer sends a console no "
            "playback or control commands of any kind — the only thing it ever "
            "transmits to a desk is the user binding and subscribe request it "
            "makes when it connects.</p>"

            "<h3>The commands</h3>" + _osc_command_table() +

            "<h3>Setting up a macro</h3>"
            "<p>On the console, make a macro that sends one of the addresses "
            "above as an OSC string, for example <code>/wer/record/toggle</code>. "
            "Then one macro key starts the recording and the same key stops "
            "it.</p>"
            "<p>If a press seems to do nothing, open <b>Connections &rarr; "
            "Eos</b> and watch the Monitor. When this was measured, each run of "
            "the macro showed as <code>/eos/out/event/macro/N</code> followed by "
            "the command. If the command is not in the Monitor, it did not reach "
            "Wer.</p>"
            "<p>If it is, Wer received it, and the log (<b>Help &rarr; Open Log "
            "Folder</b>) says what became of it. A command Wer acted on is "
            "logged. So is one it ignored, with the reason: the first repeat in "
            "a burst, an address that is not a Wer command, a start while already "
            "recording, a stop or marker with nothing recording, a layout with no "
            "name or a name no layout has. A start, marker or snapshot that "
            "cannot happen says why on screen too.</p>"
            "<p>One kind is not logged: a command whose first argument is the "
            "number 0, or false, is taken as a button being let go, and ignored "
            "without a word. Send the command without a number after it, as Eos "
            "did when this was measured.</p>"

            "<h3>One press, however fast you tap</h3>"
            "<p>A start, stop or toggle that arrives again within a second of the "
            "same command is ignored, and every ignored repeat restarts that "
            "second. So a double tap, or a burst of taps, starts or stops the "
            "recording once rather than switching it on and off. Leave a second "
            "between presses that are meant separately.</p>"
            "<p>Only the <i>same</i> command is folded together: a macro that "
            "sends stop and then start does both. Everything else acts on every "
            "press: markers, snapshots, layouts, and hiding or showing the "
            "overlay.</p>"

            "<h3>A note on safety</h3>"
            "<p>Anything that can send to Wer's console connection can use these "
            "commands. Over TCP that is only the desk Wer dialled. Over UDP it is "
            "anything on the network that can reach that port. A command never "
            "counts as the console being connected, so something else sending "
            "them cannot hide a desk that has gone quiet.</p>",
        ),
        HelpTopic(
            "files", "Files and folders", section="Reference",
            keywords=("file", "folder", "settings", "log", "where", "autosave"),
            body="""
<h2>Files and folders</h2>
<table cellpadding=6 width='100%'>
<tr><td width='30%'><b>Recordings</b></td><td>Wherever you set on the Recording
    tab; <code>Videos\\Wer</code> in your user profile until you do.
    <b>File &rarr; Open Recordings Folder</b> takes you there.</td></tr>
<tr><td><b>Snapshots</b></td><td>A <code>Snapshots</code> folder inside the
    recordings folder, unless you choose another on the Recording tab.
    <b>File &rarr; Open Snapshots Folder</b> takes you there.</td></tr>
<tr><td><b>Settings</b></td><td><code>%LOCALAPPDATA%\\Wer\\autosave.wer</code> —
    your saved consoles, camera, recording settings, A/V offsets, sACN output and
    layouts. Plain JSON; you can read and edit it, but close Wer first, because
    Wer rewrites the whole file as you work. If it cannot be read, Wer opens on
    defaults and keeps the bad file beside it as <code>autosave.corrupt</code>.
    The same folder holds <code>camera-backends.json</code>, which remembers how
    each camera was opened, and <code>settings.lock</code> while Wer is
    running.</td></tr>
<tr><td><b>Log</b></td><td><code>wer.log</code>, in a <code>logs</code> folder
    next to the program, or in <code>%LOCALAPPDATA%\\Wer\\logs</code> if that
    folder cannot be written to. <b>Help &rarr; Open Log Folder</b> goes to
    whichever is in use, and the <b>Environment</b> tab names it. Everything that
    goes wrong is written here, whether or not it appeared on screen. It starts a
    new file at 5&nbsp;MB and keeps five older ones beside it. Uninstalling
    deletes a <code>logs</code> folder next to the program.</td></tr>
</table>

<h3>Moving Wer to another machine</h3>
<p>Run the same installer there, <code>Wer-&lt;version&gt;-setup.exe</code>. It
installs to <code>%LOCALAPPDATA%\\Programs\\Wer</code> for that Windows user, so
it needs no administrator, and it can be removed again from Add/Remove Programs.
A copy handed over as a zip needs no installing and registers nothing: unzip it
anywhere and run <code>Wer.exe</code>.</p>
<p>Your settings live in your user profile, not with the program, so a fresh
machine starts with defaults — which are chosen to be usable straight away. To
take yours with you, copy <code>autosave.wer</code> into
<code>%LOCALAPPDATA%\\Wer</code> on the new machine while Wer is closed there.
The file carries this Wer's sACN source identity too, so do not copy it to a
computer that will send sACN status on the same network as this one: receivers
would take the two for a single source. Uninstalling leaves your settings,
recordings and snapshots where they are.</p>
""",
        ),
        HelpTopic(
            "sacn", "sACN status output", section="Reference",
            keywords=("sacn", "e1.31", "universe", "priority", "per-address",
                      "status", "cue light", "tally", "network"),
            body="""
<h2>sACN status output</h2>
<p>Wer can put its recording status on the lighting network as sACN: one address
in one universe that sits at <b>0</b> while Wer is not recording, and fades
smoothly from 0 to full and back every <b>2 seconds</b> while it is. Point a
spare channel, a magic sheet or a cue light at it, and anyone on the lighting
network can see whether Wer is rolling.</p>

<h3>Setting it up</h3>
<p><b>Connections &rarr; sACN output</b>. It is off by default, and starts on
<b>universe 101, address 1</b>. Change those first if anything else uses that
address, then tick <b>Send Wer's recording status</b>.</p>
<table cellpadding=6>
<tr><td><b>Universe</b></td><td>1-63999. Sent by multicast to that universe's
    standard sACN address, on the network below.</td></tr>
<tr><td><b>Address</b></td><td>1-512 in that universe.</td></tr>
<tr><td><b>Priority</b></td><td>1-200. 100 by default, which is also what a
    console sends unless told otherwise.</td></tr>
<tr><td><b>Per-address priority</b></td><td>On by default. Wer also sends ETC
    per-address priorities: its priority at the chosen address and 0 everywhere
    else, so a receiver that understands them gives Wer that one address and
    nothing more.</td></tr>
<tr><td><b>Network</b></td><td>The network card to send on. <b>Automatic</b> is
    whichever Windows prefers, and the tab names it. On a laptop with Wi-Fi and a
    show network both connected that is often the Wi-Fi, which reaches no
    lighting at all, so the tab warns when Automatic lands there or when more
    than one network is connected. A network chosen by name and then unplugged is
    shown as missing, never swapped for another.</td></tr>
</table>

<h3>Take care with the universe</h3>
<p><b>With per-address priority off</b>, a receiver that merges by priority
treats all 512 addresses of the universe as Wer's, at Wer's priority, the 511
that are 0 included. If Wer's priority is higher than the console's on that
universe, everything else on it goes out. At an equal priority the levels merge
highest-takes-precedence, so the pulse is added to whatever the console puts on
that address.</p>
<p><b>Even with it on</b>, a receiver that ignores per-address priority still
merges Wer as a whole universe. Some gateways ship with it switched off, and at
least one make of node has been reported flickering on it. The safe choice is a
universe nothing else is sent on.</p>

<h3>What counts as recording</h3>
<p>The address pulses only while picture is actually going into the file. It
stays at 0 while a take is still starting, and goes back to 0 within about 3
seconds if the camera stalls or the recorder is waiting to restart: the same test
Wer uses to say nothing is being written. It goes to 0 the moment Stop is
pressed, even while the last frames are still being saved. A capture device that
keeps sending black frames when it loses its signal still counts as recording; a
device that stops sending frames instead takes the address to 0 within about 3
seconds.</p>

<h3>When it stops</h3>
<p>Switching it off, changing the universe or network, and closing Wer all send
the address at 0 first and then end the stream, so a receiver that holds its last
look when a source goes holds 0, not wherever the pulse had got to. What a
receiver does once the stream has ended is its own setting.</p>
<p>If the network it sends on goes away and sending has not come back within 10
seconds, the status bar says so, and keeps saying so until Record is next pressed
at this computer. Wer starts sending again by itself when the network comes back.
With no network connected at all, Windows would send it only to this computer,
and Wer counts that as no network. If Automatic moves a running stream to another
network -- the cable knocked out with Wi-Fi still up, say -- the status bar says
that too, because receivers on the lighting network have lost it.</p>
<p>While the level moves it is sent about thirty times a second, and while it
sits still about once a second, as sACN expects. Receivers list it as <b>Wer
on</b> the computer's name.</p>
<p><b>Checked so far</b> by receiving Wer's own packets on the same computer,
with the packet layout matched field by field to ETC's own sACN library, and by
seeing Wer's output arrive in ETCnomad. It has not yet been tried through a
hardware gateway or node, or on a universe a console is also sending on, so
watch it arrive on the rig before a show depends on it.</p>
""",
        ),
        HelpTopic(
            "trouble", "Troubleshooting", section="Reference",
            keywords=("problem", "broken", "help", "not working", "blank",
                      "dropped", "error", "fix", "silent", "silence",
                      "no sound"),
            body="""
<h2>Troubleshooting</h2>

<h3>The console connects but no data arrives</h3>
<p>Status says <b>Waiting for data</b>. Eos accepts the connection whether or not
its OSC output is on, so this nearly always means <b>OSC TX is not enabled</b> on
the desk. See <i>Connecting to the console</i>.</p>

<h3>Cue widgets are blank</h3>
<p>Check the <b>Data Monitor</b>. If <code>eos.cue.active.number</code> is not
there, it is the console. If it is there with a value, the widget's template is
wrong — check spelling on the Overlay tab.</p>

<h3>The command line stays empty</h3>
<p>Check <b>Command line from</b> on the Overlay tab. <b>Any user</b> follows
whichever command line changed last; a widget pinned to one user shows only
theirs, so it stays still while someone else is driving. <b>OSC user</b> is not
usually the cause: Eos was seen sending every user's command line whichever user
Wer was bound to. If the cue widgets are blank as well, the console data is not
arriving at all; see <i>Cue widgets are blank</i> above.</p>

<h3>A console macro does nothing</h3>
<p>Open <b>Connections &rarr; Eos</b> and watch its Monitor while the macro runs.
If the <code>/wer/</code> command does not appear there, it did not reach Wer:
look at the macro, and at <b>OSC TX</b> on the desk. It was measured working over
TCP; over UDP, or from a hardware desk, it has not been tested yet. If the command
does appear, the log says what Wer did with it. A start, stop or toggle pressed
again within a second is ignored on purpose. See <i>Remote control from the
console</i>.</p>

<h3>The picture is stuttery</h3>
<p>Look at <b>fps measured</b> under the preview. If it is far below what you
asked for, the camera cannot sustain that mode — choose an <b>MJPG</b> format if
it offers one, or a lower resolution.</p>

<h3>ENCODER DROPS appears while recording</h3>
<p>Frames are missing from the recording. The encoder is not keeping up. In
order of what to try: switch to a hardware encoder if one is offered, lower the
<b>Record at</b> resolution, or choose a lower quality. A slow disk can also
cause it — check what else is writing to that drive.</p>

<h3>Recording will not start</h3>
<p>The message says why; for a start sent from the console it is in the status
bar and the log. Common causes: the camera is not running, the audio device is no
longer there (Wer lists the ones that are), the folder cannot be written to, or
the disk is full.</p>

<h3>The recording has no sound</h3>
<p>Mid-take the status bar says <b>No sound from <i>the input</i>: only digital
silence for 15&nbsp;s</b>. The take goes on recording; it is the sound that is
empty. What Wer is reporting is that the level stayed at the bottom of the scale
for fifteen seconds together &mdash; exact zeros, or within a count or three of
them &mdash; which a microphone in a room never manages on its own: something
upstream is delivering nothing.</p>
<p>The usual culprit is a laptop's own built-in microphone array, sitting behind
the maker's voice effects &mdash; noise suppression, "voice clarity" and the
like. An array like that passes someone talking into the machine and gates
everything else, and what it hands over in between is not a quiet room, it is
digital silence. Even the speech it does pass can arrive at the very bottom of
the scale. It strips a clap as well, so a take of claps recorded from one can
have nothing in it for the clap test to measure. The switch to try is in
Windows: <b>Settings &rarr; System &rarr; Sound</b>, click the input, and turn
<b>Audio enhancements</b> off. Then record a few seconds and see whether the
warning comes back. Wer has not yet been watched recording from such an array
with the effects off, so expect to check it rather than assume it.</p>
<p>A capture device whose HDMI carries no audio looks exactly the same from
here, and no setting in Wer changes it &mdash; check that the source is putting
sound down the cable, and that it is the device's audio input you have chosen on
the Recording tab. What a show should be recording is the capture device's own
audio input, the one named after the device, or a USB audio interface. A laptop
microphone is for checking that the path works at all, not for the recording.
See <i>Recording</i>.</p>

<h3>The console rebooted</h3>
<p>Wer retries a dropped link by itself, and Eos sends its full state within
about a second of any connection, so there is nothing to resync by hand. A desk
rebooted mid-show has not yet been watched coming back, though, so check the Eos
status reads <b>Live</b> again once the desk is up, and press <b>Connect</b> on
<b>Connections &rarr; Eos</b> if it does not. A take running through it keeps
recording either way; only the console data is missing from the stretch the desk
was away. See <i>Connecting to the console</i>.</p>

<h3>Sound and picture are out of step</h3>
<p>Measure it rather than guessing: record someone on stage clapping sharply six
to ten times, stop, and press <b>Clap test…</b> on the Recording tab, which
measures the take just finished. See <i>Recording</i>. The offset is kept for the
camera and audio input together, so after changing either, read the line under
<b>A/V offset</b>: a pairing with nothing set yet says <b>Automatic</b> there. A
change applies from the next take, not the one running.</p>

<h3>The camera will not open</h3>
<p>Something else has it, or it has been unplugged. Teams, Zoom and the Windows
Camera app all hold a camera exclusively. Close them, or plug the camera back in,
and press <b>Refresh</b>. Refresh is locked during a take; Wer keeps trying to
open a camera that drops out of a take on its own, and the preview says so.</p>

<h3>A capture device records a black picture</h3>
<p>A capture device that loses its input signal goes on sending black frames,
and Wer records them without a warning: to Wer it is still a working camera, and
the sACN status goes on pulsing. Opened with nothing reaching its input, a
Blackmagic sends about one frame a second instead, which shows under <b>fps
measured</b>. Check the cable, and that the source is on and sending a mode the
unit accepts: what a capture device can take is a property of the unit, so
check what that one takes rather than assuming. Leave the <b>Format</b> alone:
Wer already picks the best one it can read, which on a Blackmagic is
<b>UYVY</b> &mdash; the only format such a unit lists that has been seen to
deliver. See <i>Camera and picture</i>.</p>

<h3>The picture is upside down or back to front</h3>
<p><b>Preview</b> tab: tick <b>Mirror left to right</b> and <b>Flip top to
bottom</b> together for a camera mounted upside down, or <b>Mirror left to
right</b> alone for one looking through a mirror. See <i>Camera and
picture</i>.</p>

<h3>An encoder is greyed out</h3>
<p>It is present but does not work on this machine, and the tooltip says why. A
common one is a graphics driver too old for NVENC — updating the driver fixes
it. Software (x264) always works.</p>

<h3>sACN status does not arrive</h3>
<p>Look at <b>sACN</b> in the status bar. <b>off</b> means it is not switched on
(<b>Connections &rarr; sACN output</b>). <b>not sending</b> says why when you
hold the pointer over it. <b>sending</b> with nothing arriving is often the wrong
network: with Wi-Fi connected, <b>Automatic</b> often sends there, and the tab
warns about it. Choose the lighting network by name on the tab, or turn the Wi-Fi
off. Then check the receiver is on the same universe and address, and look during
a take: the address sits at 0 until picture is going into the file. See <i>sACN
status output</i>.</p>

<h3>Windows blocks Wer from opening</h3>
<p>Wer is not code-signed. On a computer with <b>Smart App Control</b> switched
on, Windows judges each build of Wer by its exact files rather than by its name
or version, so one build can open where a rebuild of the same version is blocked.
If it is blocked, ask for an installer that has already been seen to open on a
computer with Smart App Control on, and use it as it came rather than a new
build. A warning offering <b>More info</b> and <b>Run anyway</b> is SmartScreen
instead, and Run anyway opens Wer.</p>

<h3>Something else</h3>
<p><b>Help &rarr; Open Log Folder</b>. Every failure is written there, including
ones that did not appear on screen.</p>

<h3>Reporting it</h3>
<p>Problems go to <b>"""
            + CONTACT_EMAIL
            + """</b>. There is no issue tracker; that
address is read by the person who wrote this.</p>
<p>What makes a report answerable, in order of usefulness:</p>
<ol>
<li><b>The log.</b> <b>Help &rarr; Open Log Folder</b>, then attach
    <code>wer.log</code>. It holds what the app was doing, what ffmpeg said and
    what the console sent, and it is the difference between a fix and a guess.</li>
<li><b>What you were doing</b>, and what you expected instead.</li>
<li><b>The version and the machine</b>, both on the <b>Environment</b> tab —
    take a photograph of it if that is quicker than typing.</li>
</ol>
<p>If the recording itself came out wrong, say which file and keep it until you
have heard back.</p>
""",
        ),
    ]
