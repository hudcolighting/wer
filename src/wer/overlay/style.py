"""Text and box styling for overlay widgets.

Every size here is a **fraction of frame height**, not a pixel count, so a
layout survives being recorded at a different resolution than it was built at.

Put plainly: legibility over a bright stage is the whole game.
That is not a stylistic preference. A tech recording is shot into a lit stage
with a black-and-white contrast range that swings wildly cue to cue, and white
text over a followspot is invisible without help. So the defaults here are
deliberately heavy: a dark translucent backing box, plus an outline, plus a
shadow. Any one of the three can be switched off, but the defaults assume the
worst case, because the worst case is a Tuesday.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum

__all__ = [
    "Align",
    "Colour",
    "TextStyle",
    "BoxStyle",
    "WHITE",
    "BLACK",
    "AMBER",
    "RED",
    "GREEN",
]


class Align(str, Enum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"


@dataclass(frozen=True, slots=True)
class Colour:
    """RGBA, 0-255. Kept Qt-free so styles can be defined and tested anywhere."""

    r: int = 255
    g: int = 255
    b: int = 255
    a: int = 255

    @property
    def rgba(self) -> tuple[int, int, int, int]:
        return self.r, self.g, self.b, self.a

    def with_alpha(self, a: int) -> "Colour":
        return replace(self, a=max(0, min(255, a)))

    @classmethod
    def from_hex(cls, value: str, alpha: int = 255) -> "Colour":
        text = value.lstrip("#")
        if len(text) == 3:
            text = "".join(c * 2 for c in text)
        if len(text) == 8:
            return cls(
                int(text[0:2], 16), int(text[2:4], 16),
                int(text[4:6], 16), int(text[6:8], 16),
            )
        if len(text) != 6:
            raise ValueError(f"not a colour: {value!r}")
        return cls(int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16), alpha)

    def to_hex(self) -> str:
        return f"#{self.r:02x}{self.g:02x}{self.b:02x}"


WHITE = Colour(255, 255, 255)
BLACK = Colour(0, 0, 0)
#: The colour a lighting console uses for "attention". Reads as meaningful to
#: this audience rather than decorative.
AMBER = Colour(243, 156, 18)
RED = Colour(192, 57, 43)
GREEN = Colour(39, 174, 96)


@dataclass(frozen=True, slots=True)
class BoxStyle:
    """The backing box behind text.

    On by default. Text alone over a stage picture is unreadable the moment
    anything bright happens behind it, and a recording of a tech is nothing but
    bright things happening.
    """

    enabled: bool = True
    fill: Colour = Colour(0, 0, 0, 160)
    #: Padding and corner radius as fractions of frame height.
    padding: float = 0.010
    corner_radius: float = 0.006
    border_width: float = 0.0
    border: Colour = Colour(255, 255, 255, 90)


@dataclass(frozen=True, slots=True)
class TextStyle:
    """Font, colour, and the two legibility aids."""

    family: str = "Segoe UI"
    #: Size as a fraction of frame height. 0.045 is ~49 px at 1080p, ~97 px at
    #: 4K, and the same apparent size in both.
    size: float = 0.045
    weight: int = 700          # QFont.Weight values; 700 is Bold
    italic: bool = False
    letter_spacing: float = 0.0
    line_spacing: float = 1.15

    colour: Colour = field(default_factory=lambda: WHITE)
    align: Align = Align.LEFT
    opacity: float = 1.0

    #: Outline, as a fraction of font size. Survives a light background where a
    #: shadow alone would not.
    outline_width: float = 0.10
    outline: Colour = field(default_factory=lambda: Colour(0, 0, 0, 220))

    #: Drop shadow, offset as a fraction of font size.
    shadow: bool = True
    shadow_offset: float = 0.06
    shadow_blur: float = 0.0
    shadow_colour: Colour = field(default_factory=lambda: Colour(0, 0, 0, 180))

    box: BoxStyle = field(default_factory=BoxStyle)

    def __post_init__(self) -> None:
        # Align subclasses str; a QComboBox and a show file both hand back a
        # bare string, and `.value` on that is an AttributeError at save time.
        object.__setattr__(self, "align", Align(self.align))

    def scaled_size(self, frame_height: int) -> int:
        from wer.overlay.geometry import scale_to_height

        return scale_to_height(self.size, frame_height)

    def with_colour(self, colour: Colour) -> "TextStyle":
        return replace(self, colour=colour)


#: A style with every legibility aid off. Not a default -- offered because an
#: archive layout burned over clean footage sometimes wants the minimum.
PLAIN = TextStyle(
    outline_width=0.0,
    shadow=False,
    box=BoxStyle(enabled=False),
)
