"""Group photos into events (R-F4).

A new event starts when the time gap to the previous photo exceeds the
threshold, or when the location jumps further than the distance threshold.
Photos without GPS contribute time gaps only.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Sequence

from .config import ClusterConfig
from .geo import haversine_km
from .models import Event, Photo

log = logging.getLogger(__name__)


def sort_photos(photos: Sequence[Photo]) -> list[Photo]:
    """Chronological order. Undated photos sort last, by path, so they end
    up in one predictable bucket rather than scattered at the epoch."""
    dated = [p for p in photos if p.timestamp is not None]
    undated = [p for p in photos if p.timestamp is None]
    dated.sort(key=lambda p: (p.timestamp, str(p.source_path)))
    undated.sort(key=lambda p: str(p.source_path))
    return dated + undated


def _format_gap(seconds: float) -> str:
    """Render an elapsed gap readably. '311 days' beats '7482.0h'."""
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.1f}h"
    days = hours / 24
    if days < 60:
        return f"{days:.1f} days"
    return f"{days:.0f} days"


def _split_reason(
    photo: Photo,
    prev_time: Optional[datetime],
    last_fix: Optional[tuple[float, float]],
    config: ClusterConfig,
) -> Optional[str]:
    """Why this photo should start a new event, or None to keep going."""
    if photo.timestamp is None:
        # Undated photos always form their own trailing group.
        return "no timestamp" if prev_time is not None else None
    if prev_time is None:
        return "first dated photo after undated run"

    gap = (photo.timestamp - prev_time).total_seconds()
    if gap > config.time_gap_hours * 3600:
        return f"time gap {_format_gap(gap)}"

    if photo.has_gps and last_fix is not None:
        distance = haversine_km(last_fix[0], last_fix[1], photo.lat, photo.lon)
        if distance > config.distance_km:
            # A lone bad GPS fix mid-hike should not shatter an event, so a
            # distance split also needs a meaningful time gap behind it.
            if gap >= config.min_gap_minutes_for_distance_split * 60:
                return f"location jump {distance:.0f}km"
            log.debug(
                "Ignoring %0.f km jump at %s: only %.0f min elapsed",
                distance,
                photo.source_path.name,
                gap / 60,
            )
    return None


def cluster_photos(
    photos: Sequence[Photo], config: ClusterConfig
) -> list[Event]:
    """Split a photo list into chronological events."""
    ordered = sort_photos(photos)
    if not ordered:
        return []

    events: list[Event] = []
    current = Event(index=1)
    prev_time: Optional[datetime] = None
    last_fix: Optional[tuple[float, float]] = None

    for photo in ordered:
        if current.photos:
            reason = _split_reason(photo, prev_time, last_fix, config)
            if reason is not None:
                events.append(current)
                current = Event(index=len(events) + 1)
                current.notes.append(f"split: {reason}")
                last_fix = None

        current.photos.append(photo)
        if photo.timestamp is not None:
            prev_time = photo.timestamp
        if photo.has_gps:
            last_fix = (photo.lat, photo.lon)

    if current.photos:
        events.append(current)

    for event in events:
        if len(event.photos) < config.small_event_threshold:
            event.notes.append(
                f"small event ({len(event.photos)} photo(s)) - may belong with a neighbour"
            )
        if all(p.timestamp is None for p in event.photos):
            event.notes.append("no timestamps - needs manual placement")

    return events
