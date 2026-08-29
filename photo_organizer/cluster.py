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


def _same_trip(a, b) -> bool:
    """Is there evidence these two events are one outing?

    One of them must know where it was, and the other must not say
    something different. Two unnamed days are not evidence of anything and
    are left alone; two days naming different massifs are two trips.
    """
    places = {p for p in (a.place_name, b.place_name) if p}
    ranges = {r for r in (a.mountain_range, b.mountain_range) if r}

    # Different named places, or different massifs: separate trips.
    if len(places) > 1 or len(ranges) > 1:
        return False
    # At least one of them has to know something.
    return bool(places or ranges)


def merge_trips(
    events: list,
    gap_hours: float = 18.0,
    max_days: float = 3.0,
) -> tuple[list, int]:
    """Join consecutive events that are one multi-day trip.

    Runs AFTER naming, not during clustering, and that placement is the
    whole design. During clustering nothing is known about an event, so the
    only possible criterion is elapsed time -- and time alone merged 106
    events on this library, including a day of socialising with the next
    day's hike.

    Afterwards there is evidence, so a merge can require some. Two events
    join when the gap is no more than a night, the whole thing fits inside
    `max_days`, AND one of them actually knows where it was while the other
    does not contradict it. That is exactly the Aiguille Dibona case: the
    approach day was named correctly, the climbing day was not.

    The maximum length is what keeps this honest. Without it, a fortnight of
    consecutive climbing days would collapse into a single folder.

    Returns (events, merges_made). Names are cleared on anything merged, so
    the caller re-derives them from the pooled evidence -- which is the
    point: the correct answer was often in the day that got merged in.
    """
    if not events or gap_hours <= 0:
        return events, 0

    ordered = sorted(events, key=lambda e: e.start or datetime.max)
    merged: list = []
    merges = 0

    for event in ordered:
        if not merged:
            merged.append(event)
            continue
        previous = merged[-1]
        if previous.end is None or event.start is None or previous.start is None:
            merged.append(event)
            continue

        gap = (event.start - previous.end).total_seconds() / 3600.0
        span = ((event.end or event.start) - previous.start).total_seconds() / 86400.0
        if 0 <= gap <= gap_hours and span <= max_days and _same_trip(previous, event):
            previous.photos.extend(event.photos)
            previous.notes.append(
                f"joined with the photos of {event.start:%d %b} "
                f"({gap:.0f}h later): same trip"
            )
            # Clear what was derived, so it is worked out again from
            # everything now in the event rather than from the first day.
            previous.place_name = None
            previous.proposed_name = None
            previous.name_source = None
            previous.mountain_range = None
            previous.region = None
            previous.enriched_lat = previous.enriched_lon = None
            previous.evidence = []
            merges += 1
        else:
            merged.append(event)

    # Renumber so indices stay contiguous and stable for the UI.
    for position, event in enumerate(merged, start=1):
        event.index = position
    return merged, merges
