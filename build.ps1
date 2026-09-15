<#
.SYNOPSIS
    Builds Wer.exe with PyInstaller.
.DESCRIPTION
    One-folder during development for speed, one-file for release.
    Packaging is where Python GUI projects die, so this script exists from day
    one and is re-run at the end of every milestone.

    The app's logs live beside the exe and PyInstaller removes the whole
    output folder before writing the new one, so they are set aside and put
    back around the build.
.PARAMETER Release
    Build a single-file exe (slower to build, slower to start, one artifact).
    Without this you get dist/Wer/Wer.exe, which builds in a fraction of the time.
.PARAMETER Clean
    Delete build/ and dist/ first, and discard PyInstaller's cache.
.PARAMETER Package
    Zip a one-folder build into dist\Wer-<version>.zip. This is the recommended
    way to hand Wer to another machine: one artifact to copy, but ~1s startup
    instead of the ~5s a one-file exe pays on every single launch.
.PARAMETER Installer
    Build dist\Wer-<version>-setup.exe: one file to hand someone, which lays
    out the ordinary one-folder installation on their machine. Needs Inno Setup
    (winget install JRSoftware.InnoSetup). Per-user by default, so no UAC prompt
    and no administrator needed.
.PARAMETER Force
    Stop a running Wer.exe rather than refusing to build.
.PARAMETER Run
    Launch the result when the build succeeds.
.EXAMPLE
    .\build.ps1                  # fast one-folder dev build
.EXAMPLE
    .\build.ps1 -Installer       # the setup.exe to hand to someone else
.EXAMPLE
    .\build.ps1 -Release -Clean  # the single-file release artifact
#>
[CmdletBinding()]
param(
    [switch]$Release,
    [switch]$Clean,
    [switch]$Package,
    [switch]$Installer,
    [switch]$Force,
    [switch]$Run
)

$ErrorActionPreference = 'Stop'
$repoRoot = $PSScriptRoot
Set-Location $repoRoot

$python = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    throw "No virtualenv at .venv. Create it with:`n  py -3.12 -m venv .venv`n  .\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt"
}

#: Must stay identical to APP_USER_MODEL_ID in src\wer\app.py and to
#: AppUserModelID in installer\wer.iss. A test pins all three together.
$appUserModelId = 'HudCo.Wer.WindowsEosRecorder'

function Set-ShortcutAppId {
    <#
        .SYNOPSIS
        Write System.AppUserModel.ID into a .lnk's property store.

        Best effort by design: a shortcut without the property still launches
        the app, it just gets the shell's guess at a taskbar identity. Worth
        doing, never worth failing a build over.
    #>
    param([string]$Path, [string]$AppId)

    try {
        $code = @'
using System;
using System.Runtime.InteropServices;

public static class WerShortcut
{
    [ComImport, Guid("00021401-0000-0000-C000-000000000046")]
    private class ShellLink { }

    [ComImport, Guid("0000010b-0000-0000-C000-000000000046"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPersistFile
    {
        void GetClassID(out Guid pClassID);
        [PreserveSig] int IsDirty();
        void Load([MarshalAs(UnmanagedType.LPWStr)] string pszFileName, int dwMode);
        void Save([MarshalAs(UnmanagedType.LPWStr)] string pszFileName,
                  [MarshalAs(UnmanagedType.Bool)] bool fRemember);
        void SaveCompleted([MarshalAs(UnmanagedType.LPWStr)] string pszFileName);
        void GetCurFile([MarshalAs(UnmanagedType.LPWStr)] out string ppszFileName);
    }

    [ComImport, Guid("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        void GetCount(out uint cProps);
        void GetAt(uint iProp, out PROPERTYKEY pkey);
        void GetValue(ref PROPERTYKEY key, [In, Out] PROPVARIANT pv);
        void SetValue(ref PROPERTYKEY key, [In] PROPVARIANT pv);
        void Commit();
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct PROPERTYKEY
    {
        public Guid fmtid;
        public uint pid;
    }

    [StructLayout(LayoutKind.Sequential)]
    private class PROPVARIANT : IDisposable
    {
        public ushort vt;
        public ushort r1, r2, r3;
        public IntPtr p;
        public int p2;

        public void SetString(string value)
        {
            vt = 31;                       // VT_LPWSTR
            p = Marshal.StringToCoTaskMemUni(value);
        }

        public void Dispose()
        {
            if (p != IntPtr.Zero) { Marshal.FreeCoTaskMem(p); p = IntPtr.Zero; }
        }
    }

    public static void SetAppId(string path, string appId)
    {
        var link = new ShellLink();
        ((IPersistFile)link).Load(path, 2);          // STGM_READWRITE
        var store = (IPropertyStore)link;
        var key = new PROPERTYKEY {
            fmtid = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"),
            pid = 5                                  // System.AppUserModel.ID
        };
        using (var value = new PROPVARIANT())
        {
            value.SetString(appId);
            store.SetValue(ref key, value);
            store.Commit();
        }
        ((IPersistFile)link).Save(path, true);
    }
}
'@
        if (-not ([System.Management.Automation.PSTypeName]'WerShortcut').Type) {
            Add-Type -TypeDefinition $code -Language CSharp | Out-Null
        }
        [WerShortcut]::SetAppId($Path, $AppId)
    } catch {
        Write-Warning "Could not set the AppUserModelID on $Path (the taskbar may show a generic icon): $_"
    }
}

function Resolve-BundledFile {
    <#
        .SYNOPSIS
        The one file a pattern matches, or a throw that names what went wrong.

        Wheels put their version in the dist-info directory name, so a path
        written out in full goes stale the moment a package is upgraded -- and
        the way it went stale was a build failing with "licence text missing"
        naming a directory that had simply been renamed by a pip install. A
        pattern survives that. Zero matches and more than one both still stop
        the build: zero means the text is not there to ship, and two means
        nobody can say which of them describes the binary being shipped.
    #>
    param([string]$Pattern, [string]$What)

    $found = @(Get-ChildItem -Path $Pattern -File -ErrorAction SilentlyContinue)
    if ($found.Count -eq 0) {
        throw ("Licence text missing: nothing matches {0} ({1})`nIt is required to ship. If a package was upgraded or removed, update the pattern in build.ps1." -f $Pattern, $What)
    }
    if ($found.Count -gt 1) {
        throw ("Licence text ambiguous: {0} files match {1} ({2})`n  {3}`nOne of them is the text for what ships and the build cannot tell which. Narrow the pattern in build.ps1, or clean out the stale install." -f `
               $found.Count, $Pattern, $What, (($found.FullName) -join "`n  "))
    }
    return $found[0].FullName
}

function Find-InnoCompiler {
    $inPath = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($inPath) { return $inPath.Source }
    # winget installs Inno per-user by default, which is where it landed here;
    # the machine-wide paths are checked too in case it was installed as admin.
    $candidates = @(
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    foreach ($c in $candidates) { if (Test-Path $c) { return $c } }
    return $null
}

$ffmpeg = Join-Path $repoRoot 'vendor\ffmpeg\ffmpeg.exe'
if (-not (Test-Path $ffmpeg)) {
    throw "vendor\ffmpeg\ffmpeg.exe is missing. Run:`n  .\tools\fetch-ffmpeg.ps1"
}

# Checked here rather than after the build, because a build that runs for
# twenty seconds and only then says it cannot make the installer has wasted the
# time -- and the thing that was asked for is the installer, not the folder.
if ($Installer) {
    if ($Release) {
        throw ("-Installer and -Release are different answers to the same " +
               "question. The installer packs the one-folder build, which is " +
               "the shape that lets a user replace the LGPL Qt libraries and " +
               "starts in about a second. Drop -Release.")
    }
    $iscc = Find-InnoCompiler
    if (-not $iscc) {
        throw ("-Installer needs Inno Setup's compiler (ISCC.exe) and it was " +
               "not found. Install it with:`n" +
               "  winget install JRSoftware.InnoSetup")
    }
}

# A running Wer.exe holds dist\ open, and PyInstaller then fails with a
# permission error buried in a hundred lines of INFO. Catch it up front and say
# so plainly, because the message it produces otherwise is not obvious.
$running = Get-Process -Name 'Wer' -ErrorAction SilentlyContinue
if ($running) {
    if ($Force) {
        Write-Host 'Stopping the running Wer.exe...' -ForegroundColor Yellow
        $running | Stop-Process -Force
        Start-Sleep -Seconds 2
    } else {
        throw ("Wer.exe is running (pid $($running.Id -join ', ')) and is holding " +
               "dist\ open.`nClose it, or re-run with -Force to stop it automatically.")
    }
}

# PyInstaller's COLLECT step removes the whole output folder before it writes
# the new one, and the app's log lives beside the exe -- so every rebuild
# deleted the logs of every run since the last one, which is the opposite of
# what a log is for: the run worth reading about is usually the one just before
# somebody rebuilt to fix it. Moved aside here and put back below. Safe to move
# at this point and not earlier, because a running Wer.exe holds its log open
# and has already been dealt with above.
$logHome = if ($Release) { Join-Path $repoRoot 'dist' } else { Join-Path $repoRoot 'dist\Wer' }
$logsDir = Join-Path $logHome 'logs'
$logStash = $null
if (Test-Path $logsDir) {
    try {
        $logStash = Join-Path ([System.IO.Path]::GetTempPath()) ('wer-logs-' + [guid]::NewGuid().ToString('n'))
        Move-Item -LiteralPath $logsDir -Destination $logStash
    } catch {
        # Never fail a build over this. Never be quiet about it either: the
        # logs are about to be removed and the operator should know why.
        $logStash = $null
        Write-Warning "Could not set $logsDir aside, so this build will remove it: $_"
    }
}

try {

if ($Clean) {
    Write-Host 'Cleaning build/ and dist/...' -ForegroundColor Cyan
    foreach ($d in @('build', 'dist')) {
        $p = Join-Path $repoRoot $d
        if (Test-Path $p) { Remove-Item -Recurse -Force $p }
    }
}

# Qt modules we know we do not use. PySide6-Essentials is already far smaller
# than full PySide6, but QML/Quick alone is tens of megabytes and this app is
# pure QtWidgets. The browser-source overlay is stdlib http.server, so
# QtWebSockets is not needed either.
$excludes = @(
    'PySide6.QtQml'
    'PySide6.QtQuick'
    'PySide6.QtQuickWidgets'
    'PySide6.QtQuickControls2'
    'PySide6.QtWebSockets'
    'PySide6.QtWebChannel'
    'PySide6.QtSql'
    'PySide6.QtTest'
    'PySide6.QtDesigner'
    'PySide6.Qt3DCore'
    'PySide6.QtCharts'
    'PySide6.QtDataVisualization'
    # Not "we do not use QtNetwork" -- nothing imports it at all. PySide6's own
    # __init__.py has a `from . import QtNetwork` that PyInstaller's static
    # module graph always sees, which runs hook-PySide6.QtNetwork.py, which
    # calls collect_qtnetwork_files(). That resolves OpenSSL by searching the
    # Qt package dir and then falling back to the build machine's PATH -- and
    # on this machine it found Git for Windows' libssl-3-x64.dll and shipped
    # it. Wer's networking is hand-written sockets, so none of QtNetwork is
    # reachable.
    #
    # If that ever changes -- QNetworkAccessManager, QSslSocket, QTcpSocket,
    # QUdpSocket -- this exclusion has to come out FIRST, because the import
    # would work from source and fail only in the packaged build, which is the
    # worst shape a bug can take here.
    'PySide6.QtNetwork'
    'tkinter'
)

# Licence texts for everything bundled. Resolved from the very packages being
# built in, not vendored copies, so the text that ships can never describe a
# different version from the binary beside it.
#
# The files under licences\ are the exceptions, and they are exceptions for one
# reason: there is no package to copy a text out of. PySide6's wheel ships no
# LGPL text at all -- only a 470-byte pointer to Qt's COMMERCIAL terms -- and
# PCRE2, HarfBuzz, libwebp, Expat, libmpdec and the Unicode character data are
# not packages either. They are compiled into the Qt DLLs and into
# python312.dll, so the wheels deliver the code and none of the notices that
# code requires. Those texts were fetched from the tagged upstream source of
# the exact version bundled -- except the Unicode one, which has no tag to
# fetch -- and kept in the repository; licences\SOURCES.md records the URL,
# the size, the SHA-256 and, where there is one, the tag and commit of every
# one, and a test fails if a file is added there and not added here.
$sitePackages = Join-Path $repoRoot '.venv\Lib\site-packages'
$pythonHome = & $python -c "import sys; print(sys.base_prefix)"
$licences = @(
    @{ From = Join-Path $sitePackages 'cv2\LICENSE.txt';              To = 'licences/opencv' }
    @{ From = Join-Path $sitePackages 'cv2\LICENSE-3RD-PARTY.txt';    To = 'licences/opencv' }
    @{ From = (Resolve-BundledFile `
                 -Pattern (Join-Path $sitePackages 'pygrabber-*.dist-info\LICENSE') `
                 -What 'pygrabber, MIT');                             To = 'licences/pygrabber' }
    @{ From = (Resolve-BundledFile `
                 -Pattern (Join-Path $sitePackages 'comtypes-*.dist-info\licenses\LICENSE.txt') `
                 -What 'comtypes, MIT');                              To = 'licences/comtypes' }
    @{ From = Join-Path $pythonHome 'LICENSE.txt';                    To = 'licences/python' }
    @{ From = Join-Path $repoRoot 'licences\qt\LGPL-3.0.txt';         To = 'licences/qt' }
    # Compiled into Qt's binaries; fetched from qtbase/qtimageformats v6.9.3.
    @{ From = Join-Path $repoRoot 'licences\qt\pcre2\LICENCE.md';     To = 'licences/qt/pcre2' }
    @{ From = Join-Path $repoRoot 'licences\qt\pcre2\LICENCE-SLJIT';  To = 'licences/qt/pcre2' }
    @{ From = Join-Path $repoRoot 'licences\qt\harfbuzz\COPYING';     To = 'licences/qt/harfbuzz' }
    @{ From = Join-Path $repoRoot 'licences\qt\libwebp\COPYING';      To = 'licences/qt/libwebp' }
    # Compiled into python312.dll; fetched from cpython v3.12.10, except the
    # Unicode terms, which CPython's tree does not carry at all.
    @{ From = Join-Path $repoRoot 'licences\python\expat\COPYING';    To = 'licences/python/expat' }
    @{ From = Join-Path $repoRoot 'licences\python\cpython\license.rst'; To = 'licences/python/cpython' }
    @{ From = Join-Path $repoRoot 'licences\python\unicode\license.txt'; To = 'licences/python/unicode' }
)
foreach ($l in $licences) {
    if (-not (Test-Path $l.From)) {
        # Fatal, not a warning. A build that silently drops a licence text is a
        # build that cannot lawfully be handed to anyone, and the whole point of
        # resolving these from the packages is that a version bump shows up here
        # rather than in somebody's inbox.
        throw ("Licence text missing: {0}`nIt is required to ship. If a package was upgraded, update the path in build.ps1." -f $l.From)
    }
}

# Which commit this exe was built from. The version says what was intended;
# this says what was actually compiled, dirty working tree and all. A build
# handed to someone has no repository to ask, and "which build is this?" is
# the first question of every bug report that matters -- so it is stamped in
# here and shown on the Environment tab (wer.paths.build_id). It is also what
# makes the source link in Help -> Licences answerable: the repository is the
# GPL section 6 route, and this names the commit to check out.
$buildIdFile = Join-Path $repoRoot 'build\build-id.txt'
New-Item -ItemType Directory -Path (Split-Path -Parent $buildIdFile) -Force | Out-Null
$buildId = 'unknown - not built from a git checkout'
try {
    $described = & git -C $repoRoot describe --always --dirty 2>$null
    $branch = & git -C $repoRoot rev-parse --abbrev-ref HEAD 2>$null
    if ($described) {
        $buildId = if ($branch) { "$described on $branch" } else { "$described" }
    }
} catch {
    # No git, or an exported tree with no .git in it. Never fail a build over
    # a label: an unlabelled build is still the build, and saying so beats
    # stamping in something that is not true.
    Write-Warning "Could not read the commit for build-id.txt, so this build will not name one: $_"
}
Set-Content -LiteralPath $buildIdFile -Value $buildId -Encoding utf8
Write-Host ("Built from {0}" -f $buildId) -ForegroundColor DarkGray

$pyiArgs = @(
    '-m', 'PyInstaller'
    '--noconfirm'
    '--name', 'Wer'
    '--windowed'                       # no console window
    '--paths', 'src'
    # ffmpeg goes in as DATA, not as a binary. --add-binary would put this
    # 97 MB static exe through PyInstaller's dependency scanner for no reason.
    # The destination matches what wer.paths.ffmpeg_path() looks for first.
    '--add-data', "$ffmpeg;vendor/ffmpeg"
    '--add-data', "$(Join-Path $repoRoot 'vendor\ffmpeg\LICENSE');vendor/ffmpeg"
    # gyan's own build record for that exe: the version, the source commit, the
    # configuration report, and all 42 external libraries linked into it, 28 of
    # them with a version. (Not the configure command line -- gyan's README
    # does not carry one; the build scripts the notices point at are where that
    # lives.) It is the only list of what is inside a static ffmpeg, and
    # THIRD-PARTY-NOTICES.md points at it rather than copying it out.
    '--add-data', "$(Join-Path $repoRoot 'vendor\ffmpeg\README.txt');vendor/ffmpeg"
    # The index that names everything bundled and what it is used under.
    '--add-data', "$(Join-Path $repoRoot 'THIRD-PARTY-NOTICES.md');."
    '--add-data', "$(Join-Path $repoRoot 'LICENSE');."
    '--add-data', "$buildIdFile;."
)

foreach ($l in $licences) { $pyiArgs += @('--add-data', "$($l.From);$($l.To)") }

# The icon goes in twice on purpose: --icon stamps it into the exe so Explorer
# and the taskbar show it, and --add-data ships the file so the running app can
# set it on the QApplication too. Neither covers the other.
$icon = Join-Path $repoRoot 'assets\wer.ico'
if (Test-Path $icon) {
    $pyiArgs += @('--icon', $icon)
    $pyiArgs += @('--add-data', "$icon;assets")
} else {
    Write-Warning 'assets\wer.ico is missing - building without an icon.'
}
foreach ($e in $excludes) { $pyiArgs += @('--exclude-module', $e) }
if ($Clean)   { $pyiArgs += '--clean' }
if ($Release) { $pyiArgs += '--onefile' } else { $pyiArgs += '--onedir' }
$pyiArgs += 'wer_launcher.py'

$mode = if ($Release) { 'one-file (release)' } else { 'one-folder (dev)' }
Write-Host "Building Wer.exe - $mode" -ForegroundColor Cyan
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# PyInstaller resolves some libraries by searching PATH, so what is installed on
# this machine leaks into the payload: Git for Windows' OpenSSL was being
# shipped inside Wer purely because Git is on PATH. Excluding QtNetwork closes
# that one hole; this closes the shape of it, so the next hook that scavenges
# off PATH cannot quietly do the same. Not a wholesale scrub -- PyInstaller
# shells out and needs the system paths.
$savedPath = $env:PATH
# -notlike, not -notmatch: wildcards need no escaping, and a regex with
# backslashes in it is the kind of thing that silently stops matching.
$env:PATH = ($savedPath -split ';' | Where-Object {
    $_ -and $_ -notlike '*\Git\mingw64\bin*' -and $_ -notlike '*\Git\usr\bin*'
}) -join ';'
try {
    & $python @pyiArgs
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
}
finally {
    $env:PATH = $savedPath
}

$sw.Stop()

$exe = if ($Release) {
    Join-Path $repoRoot 'dist\Wer.exe'
} else {
    Join-Path $repoRoot 'dist\Wer\Wer.exe'
}
if (-not (Test-Path $exe)) { throw "Build reported success but $exe does not exist" }

}
finally {
    if ($logStash) {
        try {
            if (-not (Test-Path $logsDir)) {
                New-Item -ItemType Directory -Path $logsDir -Force | Out-Null
            }
            foreach ($old in Get-ChildItem -LiteralPath $logStash -Recurse -File) {
                $target = Join-Path $logsDir $old.Name
                if (Test-Path $target) {
                    # A run since the stash has written its own. Keep both:
                    # the older one is the one that explains the rebuild.
                    $target = Join-Path $logsDir "$($old.BaseName)-before-build$($old.Extension)"
                }
                Move-Item -LiteralPath $old.FullName -Destination $target
            }
            Remove-Item -LiteralPath $logStash -Recurse -Force
        } catch {
            Write-Warning "The logs from before this build are in $logStash and could not be put back: $_"
        }
    }
}

$payloadRoot = Split-Path -Parent $exe

# OpenCV bundles its own FFmpeg, which is a different build from the one in
# vendor\ffmpeg: FFmpeg 7.1 under LGPL v2.1, where ours is 8.1.2 under GPL v3.
# Wer never calls it. Capture goes through DirectShow and Media Foundation by
# explicit backend (wer.video.capture.CAPTURE_BACKENDS) and CAP_FFMPEG appears
# nowhere in the source; proved on 11 Sep 2026 by renaming it away and running a
# real 1080p30 capture off the Blackmagic, which was unaffected, and by asking
# Windows whether the DLL was ever mapped into the process, which it never was.
#
# So it is 30 MB of payload nobody uses, carrying a third set of licence
# obligations. OpenCV's own documentation says to drop it if you would rather
# not ship it. PyInstaller has no flag to exclude one collected binary, so it
# goes after the build.
# Recursive: in a one-folder build it sits in _internal\cv2\, and looking for
# it at the payload root is how this silently did nothing the first time.
$strayFfmpeg = Get-ChildItem -Path $payloadRoot -Recurse -File `
    -Filter 'opencv_videoio_ffmpeg*.dll' -ErrorAction SilentlyContinue
foreach ($stray in $strayFfmpeg) {
    $freedMb = [math]::Round($stray.Length / 1MB, 1)
    Remove-Item -LiteralPath $stray.FullName -Force
    Write-Host ("Removed {0} ({1} MB): OpenCV's LGPL ffmpeg, which Wer never calls." -f `
        $stray.Name, $freedMb) -ForegroundColor DarkGray
}

# Qt arrives as the whole of what PySide6-Essentials ships for a QtWidgets
# program, and a good part of that is for programs Wer is not: a software
# OpenGL, SVG, image formats nothing reads, spare platform plugins, Qt's own
# dialogs translated into some thirty languages. Each of those is a binary
# with other people's code and licences inside it, which the notices then have
# to describe. So what Wer never loads goes here, after the build (PyInstaller
# has no per-binary exclude, the same reason the ffmpeg above goes here) and
# before -Package and -Installer pick the folder up.
#
# One-folder builds only. A one-file exe has been packed by the time it
# exists, so there is nothing to trim -- and it is not the copy that goes to
# anyone: the README says to hand out the installer or the zip, both of which
# are made from the trimmed folder.
#
# "Nothing in Wer uses it" is checked, not assumed. On 13 Sep 2026 a search of
# src\ found no QOpenGL, QT_OPENGL, QTranslator, QLibraryInfo, QT_QPA_PLATFORM
# and nothing SVG: Wer is pure QtWidgets on the raster paint engine, its window
# icon is a .ico (wer.paths.icon_path), and the only image files it opens are
# the ones the picture widget's picker offers (wer.ui.layout_editor:
# png, jpg, jpeg, bmp, gif, webp). A path to some other format can still be
# typed into the widget, or picked under "All files"; that now gets the
# editor's "is not an image Wer can read" caption, where before the trim a
# .tif or .svg would have drawn. The same day every DLL under PySide6\ was
# byte-searched for the names of the libraries being dropped: the TUIO plugin
# is the only thing that links Qt6Network.dll, the two SVG plugins the only
# things that link Qt6Svg.dll, and qwindows.dll names opengl32sw.dll only as
# the fallback it would load if an OpenGL context were ever asked for.
$qtRoot = Join-Path $payloadRoot '_internal\PySide6'
if (-not $Release) {
    $qtDrop = @(
        # Mesa's llvmpipe with LLVM inside it: a software OpenGL that Qt loads
        # only when something asks for an OpenGL context and the desktop driver
        # cannot provide one. Nothing in Wer asks for a context. 19.7 MB.
        @{ Path = 'opengl32sw.dll'
           Why  = 'software OpenGL (Mesa llvmpipe + LLVM) that nothing in Wer asks for' }
        # Wer's networking is hand-written sockets and PySide6.QtNetwork is
        # excluded above. The only binary left that links Qt6Network.dll is
        # the TUIO touch-input plugin, which listens on a UDP port for a
        # tabletop protocol nobody in a lighting booth speaks. They go
        # together: the plugin would fail to load without the DLL, and the DLL
        # has no other reason to be there.
        @{ Path = 'Qt6Network.dll'
           Why  = 'linked only by the TUIO touch plugin, which goes with it' }
        @{ Path = 'plugins\generic\qtuiotouchplugin.dll'
           Why  = 'TUIO touch input over UDP, which Wer does not offer' }
        # Nothing in src draws, loads or shows an SVG. The window icon is a
        # .ico, and the picture widget's picker does not offer .svg.
        @{ Path = 'Qt6Svg.dll'
           Why  = 'SVG rendering, which nothing in Wer uses' }
        @{ Path = 'plugins\iconengines\qsvgicon.dll'
           Why  = 'SVG icons, and the window icon is a .ico' }
        @{ Path = 'plugins\imageformats\qsvg.dll'
           Why  = 'SVG as an image format, which the picture widget does not offer' }
        # Image formats the picture widget's picker does not offer and nothing
        # else reads. qtiff is the one that matters for the notices: it is
        # LibTIFF, a further licence for a format Wer cannot open.
        @{ Path = 'plugins\imageformats\qtiff.dll'
           Why  = 'TIFF (LibTIFF), which the picture widget does not offer' }
        @{ Path = 'plugins\imageformats\qicns.dll'
           Why  = 'Apple ICNS icons, which the picture widget does not offer' }
        @{ Path = 'plugins\imageformats\qtga.dll'
           Why  = 'TGA, which the picture widget does not offer' }
        @{ Path = 'plugins\imageformats\qwbmp.dll'
           Why  = 'WBMP, which the picture widget does not offer' }
        # Platform plugins Wer never selects. Nothing sets QT_QPA_PLATFORM, so
        # Qt picks qwindows every time; qdirect2d is the experimental Direct2D
        # backend, and qminimal and qoffscreen exist for headless test runs.
        @{ Path = 'plugins\platforms\qdirect2d.dll'
           Why  = 'the Direct2D platform plugin, which nothing selects' }
        @{ Path = 'plugins\platforms\qminimal.dll'
           Why  = 'the headless platform plugin, which nothing selects' }
        @{ Path = 'plugins\platforms\qoffscreen.dll'
           Why  = 'the offscreen platform plugin, which nothing selects' }
        # Qt's own dialogs translated into other languages. Wer installs no
        # QTranslator, so Qt's built-in English is what shows and the .qm
        # files are never opened. 6.3 MB.
        @{ Path = 'translations'
           Why  = "Qt's translations of its own dialogs, which Wer never installs" }
    )

    # Everything that may remain under PySide6\, and nothing else may.
    # THIRD-PARTY-NOTICES.md describes exactly this set of binaries and the
    # other people's code built into each, so a build that ships a Qt binary
    # the notices do not describe cannot be handed to anyone -- the same
    # reasoning as the licence-text throw above. A PySide6 upgrade that adds a
    # binary lands here as a build failure with the file named, which is where
    # it should be decided: drop it above, or describe it in the notices and
    # add it here. Never widen this list to make the build pass.
    $qtKeep = @(
        # The C++ runtime PySide6 was compiled against; in the notices under
        # Microsoft Visual C++ runtime.
        'MSVCP140.dll'
        'MSVCP140_1.dll'
        'MSVCP140_2.dll'
        'VCRUNTIME140.dll'
        'VCRUNTIME140_1.dll'
        # PySide6's binding layer and the three modules Wer imports.
        'pyside6.abi3.dll'
        'QtCore.pyd'
        'QtGui.pyd'
        'QtWidgets.pyd'
        # Qt itself. A QtWidgets program needs exactly these three; the code
        # built into them (FreeType, HarfBuzz, libpng, PCRE2, zlib) is what
        # the notices describe.
        'Qt6Core.dll'
        'Qt6Gui.dll'
        'Qt6Widgets.dll'
        # The Windows platform plugin, and the one style plugin Qt picks on
        # Windows: it registers both windows11 and windowsvista, so a Windows
        # 10 venue laptop needs it as much as an 11 does.
        'plugins\platforms\qwindows.dll'
        'plugins\styles\qmodernwindowsstyle.dll'
        # The window icon is assets\wer.ico, which needs the ICO reader. The
        # picture widget's picker offers png, jpg, jpeg, bmp, gif and webp:
        # PNG and BMP readers are built into Qt6Gui, the other three are these
        # plugins (qjpeg carries libjpeg-turbo, qwebp carries libwebp).
        'plugins\imageformats\qico.dll'
        'plugins\imageformats\qjpeg.dll'
        'plugins\imageformats\qgif.dll'
        'plugins\imageformats\qwebp.dll'
    )

    $qtFreed = 0
    foreach ($drop in $qtDrop) {
        $target = Join-Path $qtRoot $drop.Path
        if (-not (Test-Path -LiteralPath $target)) { continue }
        $item = Get-Item -LiteralPath $target
        $bytes = if ($item.PSIsContainer) {
            (Get-ChildItem -LiteralPath $target -Recurse -File | Measure-Object Length -Sum).Sum
        } else {
            $item.Length
        }
        Remove-Item -LiteralPath $target -Recurse -Force
        $qtFreed += $bytes
        Write-Host ("Removed {0} ({1} MB): {2}." -f `
            $drop.Path, [math]::Round($bytes / 1MB, 1), $drop.Why) -ForegroundColor DarkGray
    }
    # A plugin folder with nothing left in it is not a plugin folder, and an
    # empty generic\ or iconengines\ would only invite the question of what
    # used to be there.
    foreach ($dir in Get-ChildItem -LiteralPath (Join-Path $qtRoot 'plugins') -Directory) {
        if (-not (Get-ChildItem -LiteralPath $dir.FullName -Recurse -File)) {
            Remove-Item -LiteralPath $dir.FullName -Recurse -Force
        }
    }
    Write-Host ("Qt trimmed to what Wer loads: {0} MB removed." -f `
        [math]::Round($qtFreed / 1MB, 1)) -ForegroundColor DarkGray

    $qtLeft = Get-ChildItem -LiteralPath $qtRoot -Recurse -File | ForEach-Object {
        $_.FullName.Substring($qtRoot.Length + 1)
    }
    $qtUnexpected = @($qtLeft | Where-Object { $qtKeep -notcontains $_ })
    if ($qtUnexpected.Count -gt 0) {
        throw ("Qt binary not described by THIRD-PARTY-NOTICES.md: {0}`n" +
               "Every file under _internal\PySide6\ has to be in `$qtKeep in build.ps1, " +
               "and the notices have to describe what is built into it. Either add it to " +
               "`$qtDrop with a reason, or describe it in the notices and add it to `$qtKeep. " +
               "A build that ships a Qt binary the notices do not describe cannot be handed " +
               "to anyone.") -f ($qtUnexpected -join ', ')
    }

    # numpy's notices are the one set that ships by habit rather than by
    # instruction: nothing in this script copies them, PyInstaller simply
    # collects the dist-info and they come along inside it. Found on
    # 11 Sep 2026 when the payload was checked, and a hook change or a
    # `--exclude` is all it would take to end it silently. They cover numpy itself and everything
    # vendored into it -- pocketfft, LAPACK, the random-number generators,
    # libdivide, Highway, dragon4, x86-simd-sort, Intel SVML -- so their
    # absence is the same fault as a missing licence text, and stops the build
    # for the same reason. Globbed on the version, which moves with pip.
    $numpyNotices = @(Get-ChildItem -Path (Join-Path $payloadRoot '_internal\numpy-*.dist-info\licenses\LICENSE.txt') `
        -File -ErrorAction SilentlyContinue)
    if ($numpyNotices.Count -ne 1) {
        throw ("Licence text missing: {0} files match _internal\numpy-*.dist-info\licenses\LICENSE.txt in the payload`n" +
               "It is required to ship, and it is the only notice covering numpy and everything vendored into it. " +
               "PyInstaller collects it with numpy's dist-info; if that stopped happening, collect it here.") -f $numpyNotices.Count
    }
} else {
    Write-Host ('Qt not trimmed: a one-file build is already packed. ' +
                'It is not the copy that goes to anyone; hand out the installer or the zip.') `
        -ForegroundColor DarkGray
}

# Report the whole payload, not just the launcher stub: for a one-folder build
# the exe is a rounding error next to _internal/.
# Logs kept across the build are in there too, and they are not payload.
$totalMb = [math]::Round(
    ((Get-ChildItem $payloadRoot -Recurse -File |
        Where-Object { $_.DirectoryName -ne $logsDir } |
        Measure-Object Length -Sum).Sum / 1MB), 1)
$exeMb = [math]::Round((Get-Item $exe).Length / 1MB, 1)

Write-Host ''
Write-Host 'Build OK' -ForegroundColor Green
Write-Host ("  exe        {0}" -f $exe)
Write-Host ("  exe size   {0} MB" -f $exeMb)
Write-Host ("  total      {0} MB" -f $totalMb)
Write-Host ("  duration   {0:mm\:ss}" -f $sw.Elapsed)
# Wer is not code-signed, so Windows shows a SmartScreen panel naming an unknown
# publisher and hides Run behind "More info". Said on every build rather than
# left to be discovered: whoever passes a build on should know before the person
# receiving it does.
Write-Host '  signature  none - Windows will warn about an unknown publisher' -ForegroundColor Yellow

# Point the shortcut at whatever was just built. Two exes and no sign of which
# is current is how an outdated build gets launched by mistake, which happened
# for real: a day-old one-file exe in dist\ sat next to the current one-folder
# build and was the easier of the two to find.
$shortcut = Join-Path $repoRoot 'Wer.lnk'
try {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($shortcut)
    $link.TargetPath = $exe
    $link.WorkingDirectory = $payloadRoot
    $link.IconLocation = "$exe,0"
    $link.Description = 'Windows Eos Recorder - the current build'
    $link.Save()

    # Stamp the shortcut with the same AppUserModelID the app claims at
    # startup (APP_USER_MODEL_ID in src\wer\app.py). Windows resolves a taskbar
    # button's icon and name through that identity; with no shortcut claiming
    # it, the shell has nothing to resolve, and the title bar shows the icon
    # while the taskbar does not. WScript.Shell cannot write it -- it lives in
    # the shortcut's property store -- so this goes through IPropertyStore.
    Set-ShortcutAppId -Path $shortcut -AppId $appUserModelId

    Write-Host ("  shortcut   {0}" -f $shortcut)
} catch {
    # Never fail a good build over a convenience file.
    Write-Warning "Could not update $shortcut : $_"
}

$version = (Select-String -Path (Join-Path $repoRoot 'src\wer\__init__.py') `
    -Pattern '__version__\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value

if ($Installer) {
    $iss = Join-Path $repoRoot 'installer\wer.iss'
    Write-Host ''
    Write-Host "Compiling installer for $version ..." -ForegroundColor Cyan
    # /Q so the compiler's own per-file chatter stays out of the build output;
    # errors still come through.
    & $iscc /Q "/DAppVersion=$version" $iss
    if ($LASTEXITCODE -ne 0) {
        throw "Inno Setup failed with exit code $LASTEXITCODE"
    }
    $setup = Join-Path $repoRoot "dist\Wer-$version-setup.exe"
    if (-not (Test-Path $setup)) {
        throw "Inno Setup reported success but $setup does not exist"
    }
    $setupMb = [math]::Round((Get-Item $setup).Length / 1MB, 1)
    Write-Host ("  {0}  ({1} MB)" -f $setup, $setupMb) -ForegroundColor Green
    Write-Host '  One file to send. Installs per-user, no administrator needed.'
}

if ($Package) {
    if ($Release) {
        Write-Host ''
        Write-Host 'Skipping -Package: a one-file build is already a single artifact.' -ForegroundColor Yellow
    } else {
        $zip = Join-Path $repoRoot "dist\Wer-$version.zip"
        if (Test-Path $zip) { Remove-Item $zip -Force }
        Write-Host ''
        Write-Host "Packaging dist\Wer -> Wer-$version.zip ..." -ForegroundColor Cyan
        # Everything except logs\: those belong to whoever ran the build, not
        # to the person being handed the zip, and the app makes its own.
        $payload = Get-ChildItem (Join-Path $repoRoot 'dist\Wer') |
            Where-Object { $_.Name -ne 'logs' }
        # The licence and the notices again, at the root of the zip. They are
        # already in the payload, but PyInstaller puts them in _internal\,
        # which is the folder a person is told not to go into. The installer
        # lays both beside the exe for the same reason -- LGPL v3 section 4(a)
        # asks for prominent notice with each copy, not notice inside the
        # running program -- and the two ways of handing Wer over should not
        # differ in what the recipient can see without unpacking anything.
        $zipItems = @($payload.FullName) + @(
            (Join-Path $repoRoot 'LICENSE')
            (Join-Path $repoRoot 'THIRD-PARTY-NOTICES.md')
        )
        Compress-Archive -Path $zipItems -DestinationPath $zip -CompressionLevel Optimal
        $zipMb = [math]::Round((Get-Item $zip).Length / 1MB, 1)
        Write-Host ("  {0}  ({1} MB)" -f $zip, $zipMb) -ForegroundColor Green
        Write-Host '  Unzip anywhere and run Wer.exe. No install, nothing to set up.'
    }
}

if ($Run) {
    Write-Host ''
    Write-Host 'Launching...' -ForegroundColor Cyan
    Start-Process -FilePath $exe
}
