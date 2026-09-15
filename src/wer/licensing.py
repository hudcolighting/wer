"""Who owns Wer, what it is licensed under, and how to reach whoever made it.

Copyright (C) 2026 Hudco Lighting LLC. Licensed under the GNU General Public
License v3 or later; see LICENSE at the root of the repository.

One module, because the same handful of facts belong in the About box, the
licence dialog, the README and THIRD-PARTY-NOTICES.md at once, and four
hand-written copies drift apart -- which for a licence notice is not a
cosmetic problem.

No Qt, deliberately: this is read by the UI, by the tests, and by anything that
needs to state the terms without a window open.

Two things here are obligations rather than decoration. GPL v3 section 5 asks
an interactive program to show its legal notices, which is what NOTICE is for.
Section 6 asks anyone handed a binary to be able to get the source that built
it, which is what SOURCE_OFFER is for. It names the public repository, which is
section 6(d) -- source from the same place, at no charge -- and is the simplest
of the routes section 6 allows. It replaced a written offer to email an address,
which was not a valid route as worded: an offer is option 6(b), for object code
in a physical product, and has to be good for three years and state its terms.
"""

from __future__ import annotations

__all__ = [
    "COPYRIGHT_HOLDER",
    "SUMMARY",
    "summary_html",
    "COPYRIGHT",
    "LICENCE_SPDX",
    "LICENCE_NAME",
    "CONTACT_EMAIL",
    "PROJECT_URL",
    "SOURCE_REPOSITORY",
    "SOURCE_OFFER",
    "NOTICE",
]

#: The New York limited liability company that owns the copyright.
COPYRIGHT_HOLDER = "Hudco Lighting LLC"
COPYRIGHT_YEARS = "2026"
COPYRIGHT = f"Copyright (C) {COPYRIGHT_YEARS} {COPYRIGHT_HOLDER}"

#: GPL v3 "or later", which is what the notice below grants. The bundled ffmpeg
#: is GPL v3 as well (it contains libx264), so the whole distribution is one
#: licence rather than an argument about which parts are which.
LICENCE_SPDX = "GPL-3.0-or-later"
LICENCE_NAME = "GNU General Public License, version 3 or later"

CONTACT_EMAIL = "wer@hudco.lighting"
PROJECT_URL = "https://hudco.lighting"

#: Where Wer's source lives. Section 6(d): whoever holds a build can get the
#: source it was built from, from the same place, at no charge. Keep this a
#: working URL -- it is the whole of Wer's section 6 compliance, and a dead link
#: is a licence breach rather than a broken hyperlink.
SOURCE_REPOSITORY = "https://github.com/hudcolighting/wer"

SOURCE_OFFER = f"Source code: {SOURCE_REPOSITORY}"

#: The notice itself, in the Free Software Foundation's own wording from the
#: GPL's "How to Apply These Terms" appendix. Changing the middle three
#: paragraphs is not a style decision -- leave them as they are.
NOTICE = f"""Wer - Windows Eos Recorder
{COPYRIGHT}

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU General Public License as published by the Free Software
Foundation, either version 3 of the License, or (at your option) any later
version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.

You should have received a copy of the GNU General Public License along with
this program. If not, see <https://www.gnu.org/licenses/>.

{SOURCE_OFFER}
Contact: {CONTACT_EMAIL}  -  {PROJECT_URL}"""


#: The short form, for a box nobody should have to scroll: copyright, the
#: freedom granted, the warranty disclaimed, and where the full text is. That
#: is what section 5 asks an interactive program to put in front of someone.
SUMMARY = (
    f"{COPYRIGHT}. Free software under the "
    f"{LICENCE_NAME}, with ABSOLUTELY NO WARRANTY. "
    f"{SOURCE_OFFER}"
)


def summary_html() -> str:
    """The short notice, for the About box. The addresses are links: a
    QMessageBox opens them in the browser or mail client by itself."""
    return (
        f"<p>{COPYRIGHT}<br>"
        f"Free software under the {LICENCE_NAME}, with "
        f"<b>absolutely no warranty</b>. See Help &rarr; Licences for the full "
        f"text.</p>"
        f"<p style='color:gray'>Source code: "
        f"<a href='{SOURCE_REPOSITORY}'>{SOURCE_REPOSITORY}</a><br>"
        f"<a href='mailto:{CONTACT_EMAIL}'>{CONTACT_EMAIL}</a> "
        f"&nbsp;&middot;&nbsp; <a href='{PROJECT_URL}'>{PROJECT_URL}</a></p>"
    )
