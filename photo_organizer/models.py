"""Core data structures for the photo organizer.

Everything here is plain-stdlib and side-effect free: constructing these
objects never touches the filesystem. That keeps the planning stages
(scan -> cluster -> name -> preview) trivially testable and, more
importantly, incapable of writing anything.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

MANIFEST_SCHEMA_VERSION = 1


class TimestampSource:
    """Where a photo's timestamp came from. Affects clustering confidence."""

    EXIF = "exif"
    FILENAME = "filename"
    MTIME = "mtime"
    NONE = "none"


@dataclass
class Photo:
    """One source image, as read. Never mutated to point at a new location.

    `source_path` is the only path that refers to the read-only source tree.
    The planned destination lives on the owning Event plus `dest_name`.
    """

    source_path: Path
    size_bytes: int = 0
    timestamp: Optional[datetime] = None
    timestamp_source: str = TimestampSource.NONE
    lat: Optional[float] = None
    lon: Optional[float] = None
    altitude: Optional[float] = None
    heading: Optional[float] = None
    camera_make: Optional[str] = None
    camera_model: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    # Non-fatal problems encountered while reading this file.
    warnings: list[str] = field(default_factory=list)
    # Filled in during planning; the file name it would be copied to.
    dest_name: Optional[str] = None
    # Set by the duplicate detector. "keep" marks the representative of a
    # group; "exact"/"near" mark suspected duplicates of it. Marking only --
    # nothing is ever deleted on the strength of this.
    duplicate_role: Optional[str] = None
    duplicate_of: Optional[str] = None
    # Why this frame was set aside as empty, if it was: "black", "white" or
    # "blank" from pixel statistics, or "pocket" from the analysis. Marking
    # only -- the copier routes these to _rejected_review/, and nothing is
    # ever deleted.
    reject_reason: Optional[str] = None
    # The analysis cache key for this file, filled in by the duplicate pass.
    # Kept so the analysis stage does not have to read and hash every file a
    # second time, and so the cost of a run can be quoted without doing so.
    content_key: Optional[str] = None

    @property
    def has_gps(self) -> bool:
        return self.lat is not None and self.lon is not None

    @property
    def has_exif_time(self) -> bool:
        return self.timestamp_source == TimestampSource.EXIF

    @property
    def coords(self) -> Optional[tuple[float, float]]:
        return (self.lat, self.lon) if self.has_gps else None

    @property
    def camera(self) -> Optional[str]:
        parts = [p for p in (self.camera_make, self.camera_model) if p]
        if not parts:
            return None
        # Many phones repeat the make inside the model ("Apple iPhone 15 Pro").
        if len(parts) == 2 and parts[1].lower().startswith(parts[0].lower()):
            return parts[1]
        return " ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["source_path"] = str(self.source_path)
        d["timestamp"] = self.timestamp.isoformat() if self.timestamp else None
        return d


@dataclass
class Event:
    """A cluster of photos judged to belong to the same outing."""

    index: int
    photos: list[Photo] = field(default_factory=list)
    # Proposed, human-editable. Never treated as final by the tool itself.
    place_label: Optional[str] = None
    proposed_name: Optional[str] = None
    # Set when the user overrides the proposal via the names file.
    user_name: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    # --- analysis results (filled in by the Gemini analysis stage) --------
    # Kept separate from `proposed_name` so the evidence behind a name stays
    # visible and the user can judge it, rather than seeing a bare string.
    activity: Optional[str] = None          # Ice_climbing, Ski_touring, ...
    place_name: Optional[str] = None        # mountain, hut or crag, from OCR
    route_name: Optional[str] = None        # route, when one was identified
    mountain_range: Optional[str] = None    # Ecrins, Dolomites, ...
    region: Optional[str] = None            # admin region, from geocoding
    country: Optional[str] = None
    country_code: Optional[str] = None
    enriched_lat: Optional[float] = None    # recovered location, not EXIF GPS
    enriched_lon: Optional[float] = None
    name_source: Optional[str] = None       # peak | crag | region | activity
    tag_summary: list[tuple[str, float]] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)

    @property
    def start(self) -> Optional[datetime]:
        stamps = [p.timestamp for p in self.photos if p.timestamp]
        return min(stamps) if stamps else None

    @property
    def end(self) -> Optional[datetime]:
        stamps = [p.timestamp for p in self.photos if p.timestamp]
        return max(stamps) if stamps else None

    @property
    def year(self) -> str:
        start = self.start
        return f"{start.year:04d}" if start else "0000"

    @property
    def gps_photos(self) -> list[Photo]:
        return [p for p in self.photos if p.has_gps]

    @property
    def missing_gps_count(self) -> int:
        return len(self.photos) - len(self.gps_photos)

    @property
    def missing_time_count(self) -> int:
        return sum(1 for p in self.photos if not p.has_exif_time)

    @property
    def heading_count(self) -> int:
        return sum(1 for p in self.photos if p.heading is not None)

    @property
    def cameras(self) -> list[str]:
        seen: dict[str, None] = {}
        for p in self.photos:
            cam = p.camera
            if cam:
                seen.setdefault(cam, None)
        return list(seen)

    @property
    def effective_name(self) -> str:
        """The folder name that would actually be used."""
        return self.user_name or self.proposed_name or f"Event{self.index:03d}"

    @property
    def rel_dir(self) -> Path:
        """Destination directory, relative to the output root."""
        return Path(self.year) / self.effective_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "photo_count": len(self.photos),
            "place_label": self.place_label,
            "proposed_name": self.proposed_name,
            "user_name": self.user_name,
            "effective_name": self.effective_name,
            "rel_dir": self.rel_dir.as_posix(),
            "missing_gps_count": self.missing_gps_count,
            "missing_time_count": self.missing_time_count,
            "cameras": self.cameras,
            "notes": list(self.notes),
            "activity": self.activity,
            "place_name": self.place_name,
            "route_name": self.route_name,
            "mountain_range": self.mountain_range,
            "region": self.region,
            "country": self.country,
            "country_code": self.country_code,
            "enriched_lat": self.enriched_lat,
            "enriched_lon": self.enriched_lon,
            "name_source": self.name_source,
            "tag_summary": [list(t) for t in self.tag_summary],
            "evidence": list(self.evidence),
            "photos": [p.to_dict() for p in self.photos],
        }


@dataclass
class SkippedFile:
    """A file that was seen but not ingested, and why."""

    path: Path
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "reason": self.reason}


@dataclass
class Plan:
    """The complete result of a planning run. Serializes to the manifest.

    A Plan describes what WOULD happen. Producing one writes nothing except,
    optionally, the manifest file itself inside the output root.
    """

    source_root: Path
    output_root: Path
    events: list[Event] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    config_snapshot: dict[str, Any] = field(default_factory=dict)
    schema_version: int = MANIFEST_SCHEMA_VERSION
    # Set when user edits are applied. Holds the clustering as it came out
    # of the pipeline, so an edits file can be re-exported against the same
    # event indices the user originally saw.
    pre_edit_events: Optional[list["Event"]] = None

    @property
    def photos(self) -> Iterable[Photo]:
        for event in self.events:
            yield from event.photos

    @property
    def photo_count(self) -> int:
        return sum(len(e.photos) for e in self.events)

    @property
    def total_bytes(self) -> int:
        return sum(p.size_bytes for p in self.photos)

    @property
    def missing_gps_count(self) -> int:
        return sum(e.missing_gps_count for e in self.events)

    @property
    def missing_time_count(self) -> int:
        return sum(e.missing_time_count for e in self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "created_at": self.created_at.isoformat(),
            "source_root": str(self.source_root),
            "output_root": str(self.output_root),
            "config": self.config_snapshot,
            "summary": {
                "event_count": len(self.events),
                "photo_count": self.photo_count,
                "total_bytes": self.total_bytes,
                "skipped_count": len(self.skipped),
                "missing_gps_count": self.missing_gps_count,
                "missing_time_count": self.missing_time_count,
            },
            "events": [e.to_dict() for e in self.events],
            "skipped": [s.to_dict() for s in self.skipped],
        }
