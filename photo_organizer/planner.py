"""Build a Plan: scan -> cluster -> name -> destination paths (R-F5).

Producing a Plan writes nothing. It is the complete description of what a
later commit step WOULD do, and it is what the dry-run preview renders.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

from .cluster import cluster_photos
from .config import Config
from .geo import Geocoder
from .models import Event, Plan
from .naming import propose_names
from .scan import check_paths, scan_source

log = logging.getLogger(__name__)


def assign_dest_names(event: Event) -> None:
    """Decide the filename each photo would get inside its event folder.

    Filenames are preserved; collisions get a numeric suffix rather than an
    overwrite (R-F10). Comparison is case-insensitive because Windows
    treats IMG_01.JPG and img_01.jpg as the same file.
    """
    used: dict[str, int] = {}
    for photo in event.photos:
        name = photo.source_path.name
        key = name.lower()
        if key not in used:
            used[key] = 1
            photo.dest_name = name
            continue
        stem = photo.source_path.stem
        suffix = photo.source_path.suffix
        counter = used[key]
        while True:
            counter += 1
            candidate = f"{stem}_{counter}{suffix}"
            if candidate.lower() not in used:
                break
        used[key] = counter
        used[candidate.lower()] = 1
        photo.dest_name = candidate


def build_plan(
    source: Path,
    output: Path,
    config: Config,
    progress: Optional[Callable[[int, Path], None]] = None,
    on_step: Optional[Callable[[str, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> Plan:
    """Run the full read-only planning pipeline.

    `on_step(step_id, detail)` is called as each stage starts and finishes,
    so a UI can show which stage is running without parsing the log.
    """
    source, output = check_paths(source, output)

    def step(step_id: str, detail: str = "") -> None:
        if on_step is not None:
            on_step(step_id, detail)

    step("scan", f"Reading metadata from {source}")
    log.info("Scanning %s", source)
    photos, skipped = scan_source(
        source, config.scan, progress=progress, should_cancel=should_cancel
    )
    log.info("Found %d image(s), skipped %d file(s)", len(photos), len(skipped))
    step("scan_done", f"{len(photos)} image(s), {len(skipped)} skipped")

    step("cluster", "Grouping photos into events")
    events = cluster_photos(photos, config.cluster)
    log.info("Clustered into %d event(s)", len(events))
    step("cluster_done", f"{len(events)} event(s)")

    step("name", f"Proposing names via {config.geocode.provider} geocoding")
    geocoder = Geocoder(config.geocode) if config.geocode.provider != "none" else None
    propose_names(events, geocoder, config.naming)
    named = sum(1 for e in events if e.place_label)
    step("name_done", f"{named}/{len(events)} event(s) got a place name")

    step("plan", "Working out destination paths")
    for event in events:
        assign_dest_names(event)
    step("plan_done", f"{sum(len(e.photos) for e in events)} file(s) planned")

    return Plan(
        source_root=source,
        output_root=output,
        events=events,
        skipped=skipped,
        config_snapshot=config.to_dict(),
    )
