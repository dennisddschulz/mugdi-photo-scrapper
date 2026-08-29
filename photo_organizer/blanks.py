"""Detecting frames with nothing in them: pocket shots, all-black, all-white.

A phone in a pocket takes photographs of the inside of a pocket. They are
black, or a smear of sensor noise, or a blown-out white rectangle from a
lens against fabric. They are worth nothing, they clutter the library, and
they cost money to analyse.

NOTHING HERE DELETES ANYTHING. This module classifies; the copier routes what
it flags into `_rejected_review/` so you can look before anything goes. That
is the same rule duplicates follow, and for the same reason: a wrong call by
this code must never be the reason a photograph stops existing.

The measurements it uses, all from the EXIF thumbnail so no full image has to
be decoded:

    mean        how bright the frame is on average
    stddev      how much variation there is; a blank frame has almost none
    edges       mean absolute difference between neighbouring pixels, kept
                because it is diagnostic even though nothing keys off it

Deliberately conservative. A dark photograph of a bivouac at night is a
photograph; a frame of pure black is not. The thresholds below sit far enough
apart to keep the first and catch the second, and every one of them is
checked against the real library in the tests rather than guessed.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# 8-bit luminance. A frame whose spread of brightness is below this has
# essentially no content: measured, real photographs sit far above it, and
# even a dim night shot of a hut has a lamp, a horizon or a star field.
FLAT_STDDEV = 8.0

# Where "flat" becomes "flat and black" or "flat and white".
BLACK_MEAN = 24.0
WHITE_MEAN = 232.0

# There is no "noise" rule, and that is a finding rather than an omission.
#
# A first attempt flagged dark, low-edge frames as pocket shots. Checked
# against 1,500 real photos it caught 11, of which TEN were photographs: a
# night food truck, a campfire, a snowy road at dusk, a climb inside a cave,
# somebody eating cake. Only one was an actual lens-against-fabric frame.
#
# The reason is simple and not fixable by moving the numbers: a pocket shot
# and a photograph taken at night have the same statistics. Brightness,
# spread and edge energy cannot separate them, and the thresholds that catch
# the pocket shot delete the campfire.
#
# Pocket shots are instead caught after analysis, where the model has
# actually looked at the picture -- see analyze.rejected_by_analysis. That
# costs nothing extra because every photo is analysed anyway.
REASONS = ("black", "white", "blank")


@dataclass
class FrameStats:
    """What one frame looks like, numerically."""

    mean: float = 0.0
    stddev: float = 0.0
    edges: float = 0.0
    reason: Optional[str] = None

    @property
    def is_empty(self) -> bool:
        return self.reason is not None

    def describe(self) -> str:
        return (
            f"{self.reason or 'ok'} "
            f"(mean {self.mean:.0f}, spread {self.stddev:.1f}, edges {self.edges:.1f})"
        )


def _measure(img) -> FrameStats:
    """Brightness, spread and edge energy of a PIL image."""
    from PIL import ImageStat

    grey = img.convert("L")
    # Small enough to be quick, large enough that a horizon still shows.
    grey.thumbnail((64, 64))
    stat = ImageStat.Stat(grey)
    mean = float(stat.mean[0])
    stddev = float(stat.stddev[0])

    # Mean absolute difference between horizontally adjacent pixels. Flat
    # frames score near zero; noise scores low because the differences
    # cancel over a blur; real detail scores high.
    pixels = list(grey.getdata())
    width = grey.width
    if width > 1 and len(pixels) >= width:
        deltas = [
            abs(pixels[i] - pixels[i + 1])
            for row in range(grey.height)
            for i in range(row * width, row * width + width - 1)
        ]
        edges = sum(deltas) / len(deltas) if deltas else 0.0
    else:
        edges = 0.0
    return FrameStats(mean=mean, stddev=stddev, edges=edges)


def classify(stats: FrameStats) -> FrameStats:
    """Decide whether a frame is empty, and why. Order matters."""
    if stats.stddev <= FLAT_STDDEV:
        if stats.mean <= BLACK_MEAN:
            stats.reason = "black"
        elif stats.mean >= WHITE_MEAN:
            stats.reason = "white"
        else:
            stats.reason = "blank"
    return stats


def inspect(path: Path, head: Optional[bytes] = None) -> Optional[FrameStats]:
    """Measure one photo, preferring its embedded thumbnail.

    `head` is the first chunk of the file, which the duplicate pass has
    already read. Using the thumbnail inside it means this costs no extra
    disk access at all -- decoding every full image instead was measured at
    45 minutes for this library.
    """
    from PIL import Image

    from .dedupe import _exif_thumbnail

    if head:
        thumb = _exif_thumbnail(head)
        if thumb:
            try:
                with Image.open(io.BytesIO(thumb)) as img:
                    return classify(_measure(img))
            except Exception:
                pass  # fall through to a full decode

    try:
        with Image.open(path) as img:
            img.draft("L", (128, 128))
            return classify(_measure(img))
    except Exception as exc:
        log.debug("Could not inspect %s: %s", path, exc)
        return None
