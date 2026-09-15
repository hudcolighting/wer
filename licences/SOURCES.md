# Where these licence texts came from

Each text under `licences/` was fetched from the tagged upstream source of the
exact version Wer bundles, and saved byte for byte. Nothing here was typed out,
trimmed or reflowed by hand: a retyped licence is no longer the licence, and a
reflowed one can no longer be checked against anything.

All of it was fetched with `curl` on 14 September 2026. The SHA-256 of every
file is recorded below so the next person can confirm what is in the tree is
still what upstream served, rather than take it on trust:

    certutil -hashfile licences\qt\pcre2\LICENCE.md SHA256

Why these libraries and not the rest: `build.ps1` copies a licence out of the
package being bundled whenever the package carries one, which is the only way
to be sure the text and the binary describe each other. These libraries carry
none, because they are not packages -- they are compiled *into* the DLLs Wer
loads. PCRE2, HarfBuzz and libwebp are inside Qt's own libraries; Expat,
libmpdec and the Unicode character data are inside `python312.dll`. So the
PySide6 wheel and the embedded interpreter ship the code without the notices
that code requires, and there is nothing for `build.ps1` to copy. The texts
have to be kept here instead.

## Qt 6.9.3

PySide6 6.9.3 is built against Qt 6.9.3. The versions below are the ones Qt's
own `qt_attribution.json` states for the copy Qt bundles, which is not always
the newest upstream release -- Qt vendors a particular snapshot, and that
snapshot is what ends up in the DLL.

From **qt/qtbase**, tag `v6.9.3`, commit
`be09b211db70e1b0155d05c18668ad76e7e3df51`, paths relative to
<https://raw.githubusercontent.com/qt/qtbase/v6.9.3/>:

| File here | Upstream path | Version | Bytes | SHA-256 |
| --- | --- | --- | --- | --- |
| `qt/pcre2/LICENCE.md` | `src/3rdparty/pcre2/LICENCE.md` | PCRE2 10.46 | 3867 | `9cf7ac6976099a1d856826d3ef1b093bd6b84489dc6100628ac79e740cf9885a` |
| `qt/pcre2/LICENCE-SLJIT` | `src/3rdparty/pcre2/LICENCE-SLJIT` | PCRE2 10.46 | 1444 | `5f216505c0f6ea3273caec89e766eef93cdeb7bbb0c429f9360116d7c938feeb` |
| `qt/harfbuzz/COPYING` | `src/3rdparty/harfbuzz-ng/COPYING` | HarfBuzz-NG 11.5.0 | 1971 | `ba8f810f2455c2f08e2d56bb49b72f37fcf68f1f4fade38977cfd7372050ad64` |

From **qt/qtimageformats**, tag `v6.9.3`, commit
`7484973d606d212ae9b770b6a3be0dbce11f32e0`, path relative to
<https://raw.githubusercontent.com/qt/qtimageformats/v6.9.3/>:

| File here | Upstream path | Version | Bytes | SHA-256 |
| --- | --- | --- | --- | --- |
| `qt/libwebp/COPYING` | `src/3rdparty/libwebp/COPYING` | libwebp 1.6.0 | 1496 | `5aec868f669e384a22372a4e8a1a6cd7d44c64cd451f960ca69cc170d1e13acf` |

Two files for PCRE2 because `qt_attribution.json` declares two components in
that one directory, each with its own `LicenseFile`: PCRE2 itself, under a
BSD 3-clause licence with the binary-like-packages exception, and its
stack-less JIT compiler, under BSD 2-clause. Taking only `LICENCE.md` would
leave the JIT uncovered.

HarfBuzz's `COPYING` says that parts under other licences carry their own
`COPYING` in subdirectories. The qtbase tree at `v6.9.3` was listed in full
and has no such file: `COPYING`, `LICENCE.md` and `LICENCE-SLJIT` above are
the only licence files anywhere under `src/3rdparty/harfbuzz-ng/` and
`src/3rdparty/pcre2/`, so this one text covers the whole bundled copy.

## CPython 3.12.10

From **python/cpython**, tag `v3.12.10`, commit
`0cc81280367df838c4b199f8f0378837165071c2`, paths relative to
<https://raw.githubusercontent.com/python/cpython/v3.12.10/>. The venv
interpreter reports itself as `tags/v3.12.10:0cc8128`, which is the same
commit:

| File here | Upstream path | Version | Bytes | SHA-256 |
| --- | --- | --- | --- | --- |
| `python/expat/COPYING` | `Modules/expat/COPYING` | Expat 2.7.1 | 1144 | `122f2c27000472a201d337b9b31f7eb2b52d091b02857061a8880371612d9534` |
| `python/cpython/license.rst` | `Doc/license.rst` | libmpdec 2.5.1, among others | 54130 | `341832873fd316a37927e79385093fbbfd40a467428480835fe435a80cadf4e5` |

The versions are the ones the vendored headers declare: `XML_MAJOR_VERSION` /
`MINOR` / `MICRO` in `Modules/expat/expat.h` give 2.7.1, and `MPD_VERSION` in
`Modules/_decimal/libmpdec/mpdecimal.h` gives 2.5.1. `COPYING` itself states
no version.

**libmpdec has no licence file of its own in CPython 3.12.10.** The whole tree
at the tag was listed: the only files whose names contain "licen" or "copying"
are `Doc/license.rst`, `LICENSE`, `Mac/BuildScript/resources/License.rtf`,
`Modules/expat/COPYING`, `PC/crtlicense.txt` and
`Tools/msi/bundle/bootstrap/LICENSE.txt`. The
`Modules/_decimal/libmpdec/LICENSE.txt` that older CPython carried is gone;
the directory now holds source and a `README.txt` describing the build, and
nothing else. CPython states the libmpdec terms in `Doc/license.rst` instead,
under a heading of that name -- copyright 2008-2020 Stefan Krah, BSD 2-clause.
That document is saved here whole rather than cut down to the libmpdec
section, because cutting it down would mean editing a licence text by hand.
Keeping it whole costs 54 kB and gains the terms for most of the rest of the
interpreter's third-party code -- OpenSSL, libffi, Expat, the Mersenne
Twister and a dozen more. Not all of it: `Doc/license.rst` says nothing about
the Unicode character data or about liblzma.

## Unicode character data

| File here | Fetched from | Bytes | SHA-256 |
| --- | --- | --- | --- |
| `python/unicode/license.txt` | <https://www.unicode.org/license.txt> | 1995 | `e7a93b009565cfce55919a381437ac4db883e9da2126fa28b91d12732bc53d96` |

This one is not from a tag, because there is nothing in CPython to take it
from: `Doc/license.rst` at 3.12.10 does not mention Unicode at all, though the
interpreter carries the character database. So it comes from Unicode's own
site, which serves one current text and does not archive by version.

One mismatch worth knowing about rather than papering over. The text fetched
is **UNICODE LICENSE V3**, copyright 1991-2026. The interpreter Wer bundles
reports `unicodedata.unidata_version` as **15.0.0**, and the 15.0.0 data files
were published under the earlier wording, "UNICODE, INC. LICENSE AGREEMENT -
DATA FILES AND SOFTWARE". V3 is the licence Unicode publishes today for the
same data, and both are permissive copyright-and-permission notices, but they
are not word for word the same document. If the exact 15.0.0-era wording is
ever needed, it is in the UCD archive for that release.
