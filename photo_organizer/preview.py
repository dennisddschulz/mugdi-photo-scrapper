"""Render the dry-run preview (R-F9).

Text only. This module must never touch the filesystem -- it is the thing
the user reads before deciding whether to trust the plan.
"""

from __future__ import annotations

from typing import Iterator

from .exif import backend_availability
from .models import Event, Plan, TimestampSource

RULE = "=" * 78
THIN = "-" * 78


def human_bytes(count: int) -> str:
    size = float(count)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _date_range(event: Event) -> str:
    start, end = event.start, event.end
    if start is None:
        return "no date"
    if end is None or start.date() == end.date():
        return f"{start:%Y-%m-%d %H:%M} - {end:%H:%M}" if end else f"{start:%Y-%m-%d %H:%M}"
    return f"{start:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M}"


def _event_lines(event: Event, verbose: bool) -> Iterator[str]:
    count = len(event.photos)
    yield f"  [{event.index:>3}] {event.rel_dir.as_posix()}"
    yield f"        {count:>5} photo(s), {human_bytes(sum(p.size_bytes for p in event.photos))}"
    yield f"        {_date_range(event)}"

    if event.place_label:
        yield f"        place: {event.place_label}"
    if event.missing_gps_count:
        yield f"        no GPS: {event.missing_gps_count}/{count}"
    if event.missing_time_count:
        yield f"        no EXIF time: {event.missing_time_count}/{count}"
    if event.heading_count:
        yield f"        compass heading on {event.heading_count}/{count} (summit naming possible)"
    if event.cameras:
        yield f"        camera: {', '.join(event.cameras[:3])}"
    for note in event.notes:
        yield f"        ! {note}"
    if verbose:
        for photo in event.photos:
            gps = (
                f"{photo.lat:.4f},{photo.lon:.4f}" if photo.has_gps else "no-gps"
            )
            stamp = f"{photo.timestamp:%Y-%m-%d %H:%M:%S}" if photo.timestamp else "no-time"
            yield f"          - {photo.dest_name}  [{stamp}] [{gps}]"
    yield ""


def render_preview(plan: Plan, verbose: bool = False, max_events: int = 0) -> str:
    """Build the full human-readable dry-run report."""
    lines: list[str] = []
    add = lines.append

    add(RULE)
    add("DRY RUN - PREVIEW ONLY. No files have been or will be written.")
    add(RULE)
    add(f"Source : {plan.source_root}")
    add(f"Output : {plan.output_root}  (would be created)")
    add("")

    backends = backend_availability()
    missing = [name for name, ok in backends.items() if not ok]
    if missing:
        add("METADATA BACKENDS")
        for name, ok in backends.items():
            add(f"  {'OK     ' if ok else 'MISSING'} {name}")
        add("  Missing backends mean less EXIF data and weaker clustering.")
        add("")

    add("SUMMARY")
    add(f"  Images found      : {plan.photo_count}")
    add(f"  Total size        : {human_bytes(plan.total_bytes)}")
    add(f"  Events proposed   : {len(plan.events)}")
    add(f"  Non-image skipped : {len(plan.skipped)}")
    add(f"  Photos w/o GPS    : {plan.missing_gps_count}")
    add(f"  Photos w/o EXIF ts: {plan.missing_time_count}")

    by_source: dict[str, int] = {}
    for photo in plan.photos:
        by_source[photo.timestamp_source] = by_source.get(photo.timestamp_source, 0) + 1
    if by_source:
        parts = [
            f"{src}={by_source[src]}"
            for src in (
                TimestampSource.EXIF,
                TimestampSource.FILENAME,
                TimestampSource.MTIME,
                TimestampSource.NONE,
            )
            if src in by_source
        ]
        add(f"  Timestamp sources : {', '.join(parts)}")
    add("")

    add("PROPOSED STRUCTURE")
    add(THIN)
    shown = plan.events if max_events <= 0 else plan.events[:max_events]
    for event in shown:
        lines.extend(_event_lines(event, verbose))
    if len(shown) < len(plan.events):
        add(f"  ... and {len(plan.events) - len(shown)} more event(s). Use --all to see them.")
        add("")

    warned = [p for p in plan.photos if p.warnings]
    if warned:
        add(THIN)
        add(f"FILES WITH WARNINGS ({len(warned)})")
        for photo in warned[:20]:
            add(f"  {photo.source_path.name}: {'; '.join(photo.warnings)}")
        if len(warned) > 20:
            add(f"  ... and {len(warned) - 20} more (see the manifest).")
        add("")

    if plan.skipped:
        reasons: dict[str, int] = {}
        for item in plan.skipped:
            reasons[item.reason] = reasons.get(item.reason, 0) + 1
        add(THIN)
        add("SKIPPED (not images)")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1])[:15]:
            add(f"  {count:>6}  {reason}")
        add("")

    add(RULE)
    add("NOTHING WAS WRITTEN. The source was opened read-only.")
    add("Review the proposed folders above. Names are editable before any copy:")
    add("  1. export the names   : --write-names names.toml")
    add("  2. edit that file     : change any proposed name you dislike")
    add("  3. re-run the preview : --names names.toml")
    add("Copying is not implemented yet (milestone 2) and requires --commit.")
    add(RULE)
    return "\n".join(lines)
