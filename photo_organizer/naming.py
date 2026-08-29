"""Propose event folder names from GPS + date (R-F6, R-F7, R-F8).

Every name produced here is a PROPOSAL. Nothing in this module decides a
final name; the user reviews and edits before anything is copied.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

from .config import NamingConfig
from .geo import Geocoder, medoid
from .models import Event

log = logging.getLogger(__name__)

# Reserved device names on Windows; a folder called CON or NUL is unusable.
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_COLLAPSE = re.compile(r"[\s._-]+")


def sanitize_label(text: str, max_length: int = 60) -> str:
    """Make an arbitrary place name safe as a Windows folder name.

    Accents are folded to ASCII so the same place cannot produce two folders
    that differ only by encoding on different drives.
    """
    if not text:
        return ""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = _UNSAFE_CHARS.sub(" ", ascii_text)
    ascii_text = _COLLAPSE.sub("_", ascii_text).strip("_")
    if not ascii_text:
        return ""
    if ascii_text.upper() in _WINDOWS_RESERVED:
        ascii_text = f"{ascii_text}_place"
    # Windows silently strips a trailing dot or space from directory names.
    return ascii_text[:max_length].rstrip("_. ")


def _format_name(
    pattern: str, place: str, event: Event, config: NamingConfig
) -> str:
    start = event.start
    fields = {
        "place": place or config.unknown_place_label,
        "dd": f"{start.day:02d}" if start else "00",
        "mm": f"{start.month:02d}" if start else "00",
        "yyyy": f"{start.year:04d}" if start else "0000",
        "index": f"{event.index:03d}",
    }
    try:
        name = pattern.format(**fields)
    except (KeyError, IndexError) as exc:
        raise ValueError(
            f"Bad naming.pattern {pattern!r}: unknown field {exc}. "
            f"Available: {', '.join(sorted(fields))}"
        ) from None
    return name[: config.max_name_length].rstrip("_. ")


def propose_event_name(
    event: Event, geocoder: Optional[Geocoder], config: NamingConfig
) -> None:
    """Fill in `place_label` and `proposed_name` on one event, in place."""
    place = ""
    points = [p.coords for p in event.gps_photos]

    if points and geocoder is not None:
        # The medoid is a real photo location, unlike a centroid.
        reference = medoid(points)
        if reference is not None:
            result = geocoder.lookup(reference[0], reference[1])
            if result is not None and result.name:
                place = sanitize_label(result.name, config.max_name_length)
                event.place_label = result.name

    if not place:
        place = config.unknown_place_label
        if not points:
            event.notes.append("no GPS - name needs manual input")
        else:
            event.notes.append("GPS present but no place match - name needs manual input")

    event.proposed_name = _format_name(config.pattern, place, event, config)


def deduplicate_names(events: list[Event]) -> None:
    """Ensure no two events propose the same YEAR/name folder.

    Two hikes from the same valley on the same day would otherwise collide
    into one folder. Suffix the later ones rather than merge them.
    """
    seen: dict[str, int] = {}
    for event in events:
        key = f"{event.year}/{event.effective_name}".lower()
        if key not in seen:
            seen[key] = 1
            continue
        seen[key] += 1
        suffix = seen[key]
        event.proposed_name = f"{event.proposed_name}_{suffix}"
        event.notes.append(
            f"name collided with an earlier event; suffixed _{suffix}"
        )


def propose_names(
    events: list[Event], geocoder: Optional[Geocoder], config: NamingConfig
) -> None:
    for event in events:
        propose_event_name(event, geocoder, config)
    deduplicate_names(events)
    if geocoder is not None:
        geocoder.save_cache()
