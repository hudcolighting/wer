# licences/

Licence texts that have to be kept here because upstream does not ship one.

Everything else Wer bundles carries its own licence text inside its package, and
`build.ps1` copies those into the build from the very package being bundled --
so the text that ships can never describe a different version from the binary
next to it. That list is in `build.ps1`, and what ends up where is described in
`THIRD-PARTY-NOTICES.md`.

What is here:

    qt/LGPL-3.0.txt     Qt, under the LGPL v3
    qt/pcre2/           PCRE2 and its JIT compiler, compiled into Qt
    qt/harfbuzz/        HarfBuzz, compiled into Qt
    qt/libwebp/         libwebp, compiled into Qt's WebP image plugin
    python/expat/       Expat, compiled into the embedded CPython
    python/cpython/     CPython's own third-party notices, which is where
                        the libmpdec terms live -- libmpdec has no separate
                        licence file in 3.12.10
    python/unicode/     the terms for the Unicode character data
    SOURCES.md          where each of the above came from, and its SHA-256

**`qt/LGPL-3.0.txt`** is the exception. Wer uses Qt under the LGPL v3, and LGPL
v3 section 4(b) requires a copy of that licence to accompany the program. The
PySide6 wheel ships no copy of it: its `licenses/` directory contains a single
470-byte file pointing at Qt's *commercial* terms. So this copy is kept in the
repository, taken verbatim from <https://www.gnu.org/licenses/lgpl-3.0.txt>.

Do not edit it.

The rest are here for the same reason at one remove. Those libraries are not
packages Wer installs -- they are compiled into the Qt DLLs and into
`python312.dll`, so the wheels deliver the code without the notices that code
requires, and `build.ps1` has no package to copy a text out of. Each was
fetched from the tagged upstream source of the exact version Wer bundles --
except the Unicode terms, which are not in CPython's tree at any tag and come
from Unicode's own site. `SOURCES.md` records the URL, the size, the SHA-256
and, where there is one, the tag, the commit and the stated version of every
one, so they can be checked instead of trusted.

Do not edit those by hand either. If one ever needs to change, fetch it again
and update `SOURCES.md` to match.

All of them ship. `build.ps1` names every licence text here in its `$licences`
table and copies it into the build under `_internal\licences\`, laid out
exactly as it is in this directory, and a test fails if a text is added here
and not added there. This file and `SOURCES.md` stay behind: they are the
record of where the texts came from, not the notices themselves. A licence
text kept in the repository and left out of the build is the worst of both:
the obligation acknowledged, and discharged in nothing anybody is handed.
