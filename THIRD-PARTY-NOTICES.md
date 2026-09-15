# Third-party notices

Wer (Windows Eos Recorder) is Copyright (C) 2026 Hudco Lighting LLC and is free
software under the GNU General Public License v3 or later. See `LICENSE`.

It is built on other people's work. This file names every third-party component
that ships inside a Wer build and says what it is licensed under. Nothing here
is modified: every component is the upstream binary as published.

Most of their licence texts ship as well, in `_internal\licences\`, and each
entry below says where its own travels. Some do not, and those say so: either
where the text can be read, or why that licence asks for none to be
reproduced. The rule is the same either way: a text ships when there is a
copy of it to ship -- out of the package being bundled, or out of the tagged
source of the exact version compiled in -- and this file never reproduces a
licence by retyping it. **Help -> Licences** shows this file, and the GPL and
the LGPL in full.

## Qt, used under the LGPL

**Qt 6.9.3**, through **PySide6-Essentials 6.9.3** and **shiboken6 6.9.3**.
Copyright (C) The Qt Company Ltd. and other contributors.

These are offered under a choice of licences. Wer uses them under the **GNU
Lesser General Public License version 3**, and under no other. Qt is used
unmodified and is dynamically linked.

The LGPL requires that you be able to replace the Qt libraries with your own.
A Wer build is laid out so that you can: the Qt DLLs sit loose in
`_internal\PySide6\`, with Qt's plugins in `_internal\PySide6\plugins\`, and
replacing them with interface-compatible builds of your own is what the
licence entitles you to do.

- Licence text: `_internal\licences\qt\LGPL-3.0.txt`, and the GPL v3 it builds
  on, at `LICENSE` beside the application -- the installer lays it there and
  the zip carries it at its root, and either way there is a second copy at
  `_internal\LICENSE`.
- Source: <https://download.qt.io/official_releases/qt/> and
  <https://download.qt.io/official_releases/QtForPython/>

Qt carries other people's code inside its own binaries, and that ships too.
A Wer build carries these, each under its own licence. A version is given
where the binary itself states one; otherwise it is whatever Qt 6.9.3 bundles.

- **FreeType**, in `Qt6Gui.dll`, as bundled in Qt 6.9.3. Copyright (C) The
  FreeType Project (David Turner, Robert Wilhelm and Werner Lemberg).
  **FreeType License**, which requires a binary distribution's documentation
  to say that the software is based in part on the work of the FreeType Team,
  and offers this as its preferred wording: Portions of this software are
  copyright (C) The FreeType Project (www.freetype.org). All rights reserved.
- **HarfBuzz-NG 11.5.0**, in `Qt6Gui.dll`, as bundled in Qt 6.9.3. Copyright
  (C) Google, Inc., Red Hat, Inc., Behdad Esfahbod and other contributors.
  **MIT-style licence** (the "Old MIT" wording).
  Text: `_internal\licences\qt\harfbuzz\COPYING`
- **libpng 1.6.50**, in `Qt6Gui.dll`. Copyright (C) The PNG Reference Library
  Authors, Cosmin Truta and Glenn Randers-Pehrson. **PNG Reference Library
  License v2**.
- **PCRE2 10.46**, in `Qt6Core.dll`. Copyright (C) University of Cambridge
  (Philip Hazel) and Zoltan Herczeg. **BSD 3-Clause**, with an exception for
  binary-like packages.
  Text: `_internal\licences\qt\pcre2\LICENCE.md`, and beside it
  `_internal\licences\qt\pcre2\LICENCE-SLJIT` for the stack-less JIT
  compiler, which Qt declares as a component of its own under **BSD 2-Clause**.
- **zlib 1.3.1**, in `Qt6Core.dll`, which reports itself as `1.3.1 (Qt)`.
  Copyright (C) Jean-loup Gailly and Mark Adler. **zlib licence**.
- **libjpeg-turbo 3.0.3**, in `plugins\imageformats\qjpeg.dll`. The binary
  carries its own line: Copyright (C) 1991-2025 The libjpeg-turbo Project and
  many others. **IJG licence**. (The TurboJPEG API and SIMD code in
  libjpeg-turbo carry **BSD 3-Clause** and **zlib** terms besides, but neither
  is in this plugin, which carries none of their strings.) The IJG licence
  asks that this be said: this software is based in part on the work of the
  Independent JPEG Group.
- **libwebp 1.6.0**, in `plugins\imageformats\qwebp.dll`, as bundled in
  Qt 6.9.3. Copyright (C) Google Inc. **BSD 3-Clause**.
  Text: `_internal\licences\qt\libwebp\COPYING`

Those three texts are in the build because they were fetched and kept, not
because anything shipped them. PCRE2, HarfBuzz and libwebp are compiled into
Qt's binaries rather than installed as packages, so there is nothing for
`build.ps1` to copy a text out of the way it does for OpenCV or comtypes: the
PySide6 wheel delivers the code and none of the notices that code requires.
They were taken byte for byte from Qt's own tagged source for 6.9.3 on
14 Sep 2026 and live in `licences/` in the source repository, where
`licences/SOURCES.md` records the URL, tag, commit, stated version, size and
SHA-256 of each. That file stays in the source rather than the build, and it
is what lets the copies here be checked rather than taken on trust.

No text ships for FreeType, libpng, zlib or libjpeg-turbo. What those licences
ask of a binary distribution is the credit and the two sentences given above,
and those are here. `Qt6Core.dll` and `Qt6Gui.dll` build in further small
permissively-licensed pieces besides, which Qt's list names. The texts of all
of them are at <https://doc.qt.io/qt-6/licenses-used-in-qt.html>; this file
does not reproduce a licence text it cannot copy from a file on disk.

Qt used to bring more than that, and since 13 Sep 2026 `build.ps1` deletes
the rest after packaging. Gone from the one-folder build: `opengl32sw.dll`, a
19.7 MB software OpenGL built from **Mesa** (llvmpipe) and **LLVM**, which Qt
loads only when something asks for an OpenGL context and nothing in Wer does;
`Qt6Network.dll` and the TUIO touch-input plugin that was the only thing
linking it; `Qt6Svg.dll` with the SVG icon-engine and image-format plugins;
the TIFF (**LibTIFF**), ICNS, TGA and WBMP image-format plugins, none of which
the picture widget offers; the Direct2D, minimal and offscreen platform
plugins, when nothing ever selects any but `qwindows`; and 6.3 MB of Qt's own
translations, which Wer never installs. Together 30 MB, and the LibTIFF, Mesa
and LLVM terms then apply to nothing Wer distributes. `build.ps1` lists what
may remain under `_internal\PySide6\` and refuses to build if anything else
turns up there, so a Qt binary this file does not describe cannot ship.

## FFmpeg

**FFmpeg 8.1.2** at `_internal\vendor\ffmpeg\ffmpeg.exe` is what Wer records
with. It is gyan.dev's `ffmpeg-8.1.2-essentials_build`: a 64-bit static
Windows build, bundled byte-for-byte as published, made from FFmpeg at commit
`38b88335f9`. It is a **GPL v3** build (`--enable-gpl --enable-version3`, and
it contains libx264) and is run as a separate program rather than linked into
Wer.

- Licence text: `_internal\vendor\ffmpeg\LICENSE`
- Build record: `_internal\vendor\ffmpeg\README.txt`, gyan's own, which states
  the version, the source commit, the configuration FFmpeg reports, and every
  external library linked into the exe -- 42 of them, 28 with a version given.
  It ships because a static binary is otherwise a list nobody can read.
- Corresponding Source: FFmpeg at
  <https://github.com/FFmpeg/FFmpeg/commit/38b88335f9>, which is the exact
  tree this binary was built from, together with gyan.dev's published build
  scripts at <https://www.gyan.dev/ffmpeg/builds/>, which are how it was
  configured and compiled. The 8.1.2 release tarball is at
  <https://ffmpeg.org/releases/ffmpeg-8.1.2.tar.xz>.

Being static, that one exe has its external libraries linked inside it -- 42 of
them, all named in `README.txt`. Five are GPL-licensed, and they are what makes
the whole binary GPL rather than LGPL: **libx264**,
**libx265**, **libxvid**, **libvidstab** and **librubberband**. The rest are
each under their own terms, permissive or LGPL or otherwise GPL-compatible, as
FFmpeg's own listing of them records. The build is version 3 rather than
version 2 because some of them -- the Apache-2.0 AMR codecs among them -- are
compatible only with GPL v3, which is part of why Wer is GPL v3 too.

No per-library licence texts ship for those. What ships is what gyan's build
came with: its `LICENSE`, which is the GPL v3 above, and its `README.txt`.
That is the line drawn deliberately. The binary is unmodified, the
Corresponding Source for everything inside it is the commit named above, and a
licence text typed out here by hand rather than copied from a file would be
neither the build's nor checkable against it.

This is the only FFmpeg in a Wer build. opencv-python ships a second, unrelated
one -- FFmpeg 7.1 under LGPL v2.1, in `opencv_videoio_ffmpeg*.dll` -- and
`build.ps1` deletes it after packaging. Wer never called it: capture goes
through DirectShow and Media Foundation by explicit backend. Removing it saves
29 MB, and its terms then apply to nothing Wer distributes.

## OpenCV

**opencv-python 4.14.0.94**. Two licences apply to different parts:

- The OpenCV library itself (`_internal\cv2\cv2.pyd`) is under the
  **Apache License 2.0**. Copyright (C) OpenCV contributors.
  Text: `_internal\licences\opencv\LICENSE-3RD-PARTY.txt`
- The opencv-python packaging layer (`__init__.py`, `config.py`, `config-3.py`,
  `load_config_py3.py`) is **MIT**, Copyright (c) Olli-Pekka Heinisuo.
  Text: `_internal\licences\opencv\LICENSE.txt`
- Source: <https://github.com/opencv/opencv> and
  <https://github.com/opencv/opencv-python>

## Python and the libraries it brings

**CPython 3.12.10**, unmodified, under the **Python Software Foundation License
Version 2**. Copyright (C) 2001-2023 Python Software Foundation; All Rights
Reserved.

- Licence text: `_internal\licences\python\LICENSE.txt`
- Source: <https://www.python.org/downloads/release/python-31210/>

That file is a stack of documents rather than one, and it is worth saying what
is actually in it. The **PSF License Version 2** above; the **BeOpen**, **CNRI**
and **CWI** agreements for the older Pythons this code descends from; the
**Zero-Clause BSD** covering code in the documentation; **Microsoft's
Distributable Code** terms for the C runtime the interpreter links; **bzip2**
(in `_bz2.pyd`); **libffi** (MIT, in `_ctypes.pyd`); an unlabelled copy of the
**Apache License 2.0**, which is OpenSSL's, though the file nowhere says so;
and at the end **Tcl/Tk** and **Tix** terms pointing at files a Windows build
does not contain.

What it does not carry matters as much. It names neither Expat, libmpdec, the
Unicode character data nor XZ Utils anywhere, and the interpreter bundles all
four. So those travel separately:

- **Expat 2.7.1**, in `pyexpat.pyd`. Copyright (c) 1998-2000 Thai Open Source
  Software Center Ltd and Clark Cooper, and (c) 2001-2022 Expat maintainers --
  the years the shipped text itself carries; CPython vendors the source ahead
  of the notice. **MIT**. Text: `_internal\licences\python\expat\COPYING`
- **libmpdec 2.5.1**, the arbitrary-precision decimal arithmetic in
  `_decimal.pyd`. Copyright (c) 2008-2020 Stefan Krah. **BSD 2-Clause**.
  CPython 3.12 dropped the separate licence file libmpdec used to carry and
  states its terms in `Doc/license.rst` instead, so that whole document ships:
  `_internal\licences\python\cpython\license.rst`. It carries the terms for
  most of the rest of the interpreter's third-party code as well -- OpenSSL,
  libffi, Expat, the Mersenne Twister and a dozen more -- though not, as this
  list shows, for the Unicode data or for liblzma.
- The **Unicode character database**, version 15.0.0, compiled into
  `python312.dll`. Copyright (c) Unicode, Inc. **Unicode licence**. Text:
  `_internal\licences\python\unicode\license.txt`. That is the V3 wording
  Unicode publishes today; the 15.0.0 data files were released under the
  earlier "UNICODE, INC. LICENSE AGREEMENT - DATA FILES AND SOFTWARE".
  Both are permissive copyright-and-permission notices, and the difference is
  recorded in `licences/SOURCES.md` rather than smoothed over.
- **XZ Utils**' `liblzma`, in `_lzma.pyd`. **0BSD** as XZ publishes it now, and
  a public-domain dedication in the releases before 5.6. Either wording is the
  one licence here that asks for nothing at all to be reproduced, so nothing
  is -- which is why which of the two applies does not have to be pinned down.

Like Qt's, none of these are packages Wer installs: they are compiled into the
interpreter's own binaries, so `build.ps1` has no package to copy a text out
of. They were fetched from CPython's tagged source for 3.12.10, or from
Unicode's site in the one case CPython's tree has nothing to take, and
`licences/SOURCES.md` records where each came from and its SHA-256.

**OpenSSL 3.0.16** (`libcrypto-3.dll`, `libssl-3.dll`) is the build CPython
links against, under the **Apache License 2.0**, Copyright 1998-2025 The OpenSSL
Authors. Source: <https://github.com/openssl/openssl>

A second OpenSSL used to ship beside it. PyInstaller's Qt hook resolves OpenSSL
by searching the build machine's PATH, and on the machine Wer is built on it
found Git for Windows' copy and bundled it -- so the payload depended on what
happened to be installed. PySide6's QtNetwork module is excluded from the build
now (nothing in Wer imports it), and the build drops Git's directories from PATH
while PyInstaller runs, so only CPython's OpenSSL ships. `Qt6Network.dll`
itself stayed a day longer, because Qt's touch-input plugin
(`qtuiotouchplugin.dll`) links it; since 13 Sep 2026 `build.ps1` deletes
both after packaging, as described under Qt above.

## NumPy, and what it carries

**numpy 2.5.3** under the **BSD 3-Clause** licence, Copyright (c) 2005-2025
NumPy Developers.

NumPy ships its own notices, and they travel in the build at
`_internal\numpy-2.5.3.dist-info\licenses\`. They cover numpy itself and the
components vendored into it -- pocketfft, LAPACK, the random-number generators
(MT19937, PCG64, Philox, SFC64, SplitMix64), libdivide, Highway, dragon4,
x86-simd-sort and Intel SVML.

Alongside it in `_internal\numpy.libs\`:

- **OpenBLAS** (`libscipy_openblas64_*.dll`), **BSD 3-Clause**, which itself
  includes LAPACK and reference BLAS. Source:
  <https://github.com/OpenMathLib/OpenBLAS>
- Statically linked into that DLL, **libgfortran** and the GCC runtime, under
  **GPL v3 or later with the GCC Runtime Library Exception 3.1**. The exception
  is what permits its distribution here. Source:
  <https://gcc.gnu.org/git/?p=gcc.git;a=tree;f=libgfortran>
- A private copy of **msvcp140.dll** -- see below.

## Device enumeration

- **pygrabber 0.2**, **MIT**, Copyright (c) Andrea Schiavinato.
  Text: `_internal\licences\pygrabber\LICENSE`
  Source: <https://github.com/andreaschiavinato/python_grabber>
- **comtypes 1.4.16**, **MIT**, which pygrabber depends on.
  Text: `_internal\licences\comtypes\LICENSE.txt`
  Source: <https://github.com/enthought/comtypes>

## Microsoft Visual C++ runtime

`VCRUNTIME140.dll` and `VCRUNTIME140_1.dll` in `_internal\`; `MSVCP140.dll`,
`MSVCP140_1.dll`, `MSVCP140_2.dll`, `VCRUNTIME140.dll` and `VCRUNTIME140_1.dll`
in `_internal\PySide6\`, and `MSVCP140.dll`, `VCRUNTIME140.dll` and
`VCRUNTIME140_1.dll` in `_internal\shiboken6\`; and a private `msvcp140` inside
`numpy.libs\`. All are under Microsoft's Distributable Code terms for Visual
Studio. Copyright (C) Microsoft Corporation. These are the C++ runtime the
interpreter and the extension modules were compiled against, and are
redistributed under Microsoft's grant for distributable code.

## Application icon

`assets\wer.ico` and `assets\wer-icon.svg` are original work,
Copyright (C) 2026 Hudco Lighting LLC, under the same GPL v3 as the rest.

Wer bundles no fonts. It uses the fonts already on the system.

---

If something here is wrong, incomplete, or credits you incorrectly, write to
wer@hudco.lighting and it will be fixed.
