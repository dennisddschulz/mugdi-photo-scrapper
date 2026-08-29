"""Write the analysis into the image files, so it outlives this tool.

The point of R-N4: tags and ratings live INSIDE the photo, in standard XMP
and IPTC fields, not in a private database. digiKam reads them, so does
Lightroom, so will whatever replaces both. The SQLite cache exists to avoid
paying for analysis twice -- it is not where your metadata lives.

SAFETY, non-negotiable
----------------------
This module writes to COPIES ONLY. Every entry point takes a path that must
sit inside the output tree and refuses anything else, because writing
metadata is a modification and the source is read-only (CLAUDE.md rule 1).
There is no code path here that can be pointed at the source.

Fields are chosen for what digiKam actually reads:

  Xmp.dc.subject / Iptc Keywords  tags -- activity, scene, season, place
  Xmp.dc.description              the caption
  Xmp.dc.title                    the place, when one is known
  Xmp.xmp.Rating                  1-5 stars from the aesthetic score
  Xmp.xmp.Label                   colour label, used to flag blurry frames
  Xmp.photoshop.City/State/Country  locality, region, country
  Exif GPS                        estimated position, clearly marked as such
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Sequence

from .schema import PhotoAnalysis

log = logging.getLogger(__name__)

# Formats whose metadata containers pyexiv2 can safely rewrite.
WRITABLE_SUFFIXES = {".jpg", ".jpeg", ".tif", ".tiff", ".png", ".webp", ".dng"}

# Colour labels digiKam understands.
LABEL_FOR_SHARPNESS = {"blurry": "Red", "acceptable": "Yellow", "sharp": "Green"}

# Marks a GPS position as inferred rather than recorded by the camera, so a
# future reader cannot mistake a model's estimate for a real fix.
ESTIMATED_GPS_METHOD = "ESTIMATED-photo-organizer"


class UnsafeWriteError(Exception):
    """Raised when a write was aimed anywhere other than the output tree."""


@dataclass
class WriteStats:
    written: int = 0
    skipped_unsupported: int = 0
    failed: int = 0
    tags_written: int = 0
    ratings_written: int = 0
    gps_written: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["errors"] = self.errors[:10]
        return d


def available() -> bool:
    """True when the metadata library is installed (no import cost)."""
    import importlib.util

    return importlib.util.find_spec("pyexiv2") is not None


def unavailable_reason() -> str:
    return (
        "pyexiv2 is not installed, so tags cannot be written into the files.\n"
        "  pip install pyexiv2"
    )


def _assert_inside_output(target: Path, output_root: Path) -> None:
    """The single guard that keeps this module away from the source tree."""
    try:
        target.resolve().relative_to(output_root.resolve())
    except ValueError:
        raise UnsafeWriteError(
            f"Refusing to write metadata to {target}: it is outside the output "
            f"tree ({output_root}). Metadata is only ever written to copies."
        ) from None


def build_tags(analysis: PhotoAnalysis, event_tags: Sequence[str] = ()) -> list[str]:
    """The keyword list for one photo.

    Deliberately includes the structured facts as tags too -- activity,
    season, range -- because that is how they become searchable in digiKam,
    which has no field for "mountain range".
    """
    tags: list[str] = []

    def add(value: Optional[str]) -> None:
        if value and value not in ("unknown", "none"):
            cleaned = value.strip()
            if cleaned and cleaned not in tags:
                tags.append(cleaned)

    add(analysis.activity)
    add(analysis.scene)
    add(analysis.season)
    add(analysis.time_of_day)
    add(analysis.mountain_range)
    add(analysis.verified_peak or analysis.peak_name)
    add(analysis.crag_name)
    add(analysis.route_name)
    add(analysis.region)
    add(analysis.country)
    # Climber-facing facts. Rock type is how a crag is actually searched for
    # ("granite", "limestone"), and grades make a route findable by number.
    add(analysis.rock_type)
    add(analysis.weather)
    for grade in analysis.climbing_grades:
        add(grade)
    for keyword in analysis.keywords:
        add(keyword)

    # NOT tagged, on purpose:
    #   landmarks           recognised, not read -- measured wrong (the model
    #                       returned "Bergseehuette" for a photo 13 km away).
    #                       A wrong tag is a false fact written into a file.
    #   place_names_visible already drives the folder name; on a guidebook
    #                       page these are places in the region rather than
    #                       the place the photo was taken.
    #   gear_visible        accurate but noisy; forty photos tagged
    #                       "carabiner" help nobody.
    #   notes               prose, kept in the database for searching there.
    # All of them remain in the cache and can be surfaced later without
    # re-analysing anything.
    for tag in event_tags:
        add(tag)
    # A guidebook page is worth finding again; a private document is worth
    # being able to filter out.
    if analysis.is_guidebook_page:
        add("guidebook")
    if analysis.is_personal_document:
        add("personal-document")
    return tags[:40]


def _gps_rational(value: float) -> str:
    """Decimal degrees to the rational DMS string exiv2 expects."""
    value = abs(value)
    degrees = int(value)
    minutes_full = (value - degrees) * 60
    minutes = int(minutes_full)
    seconds = round((minutes_full - minutes) * 60 * 100)
    return f"{degrees}/1 {minutes}/1 {seconds}/100"


def write_analysis(
    target: Path,
    analysis: PhotoAnalysis,
    output_root: Path,
    event_tags: Sequence[str] = (),
    event_title: Optional[str] = None,
    event_location: Optional[tuple[float, float]] = None,
    write_gps: bool = True,
    stats: Optional[WriteStats] = None,
) -> bool:
    """Write one photo's analysis into the copy at `target`.

    `event_location` is the position agreed across the whole event, and it
    is the ONLY source of GPS. A single photo's own estimate is never
    written: measured on this library, individual estimates put Swiss
    photos in California and a Sardinian trip in Provence, while several
    photos of one outing agreeing is real evidence. Every photo in an event
    therefore carries the same agreed position, or none at all.

    Returns True if anything was written. Never raises for a single bad
    file: one unreadable copy must not abort a 14,000-photo run.
    """
    stats = stats or WriteStats()
    _assert_inside_output(target, output_root)

    if target.suffix.lower() not in WRITABLE_SUFFIXES:
        stats.skipped_unsupported += 1
        return False

    try:
        import pyexiv2
    except ImportError:
        stats.failed += 1
        stats.errors.append(unavailable_reason())
        return False

    tags = build_tags(analysis, event_tags)
    xmp: dict = {}
    iptc: dict = {}
    exif: dict = {}

    if tags:
        xmp["Xmp.dc.subject"] = tags
        # IPTC keywords are what older tools read; digiKam reads both.
        iptc["Iptc.Application2.Keywords"] = tags[:32]
        stats.tags_written += 1

    if analysis.caption:
        xmp["Xmp.dc.description"] = {"lang=x-default": analysis.caption[:500]}
    title = event_title or analysis.verified_peak or analysis.crag_name
    if title:
        xmp["Xmp.dc.title"] = {"lang=x-default": title[:120]}

    if analysis.aesthetic_score:
        score = max(1, min(5, int(analysis.aesthetic_score)))
        xmp["Xmp.xmp.Rating"] = str(score)
        stats.ratings_written += 1

    label = LABEL_FOR_SHARPNESS.get(analysis.sharpness)
    if label:
        xmp["Xmp.xmp.Label"] = label

    if analysis.locality:
        xmp["Xmp.photoshop.City"] = analysis.locality
    if analysis.region:
        xmp["Xmp.photoshop.State"] = analysis.region
    if analysis.country:
        xmp["Xmp.photoshop.Country"] = analysis.country
    if analysis.country_code:
        xmp["Xmp.iptc.CountryCode"] = analysis.country_code

    # GPS comes from the event, never from this photo alone. If the event's
    # photos disagreed there is no position, and none is written -- an empty
    # GPS field is honest, a wrong one is not.
    lat, lon = (event_location or (None, None))
    if write_gps and lat is not None and lon is not None and -90 <= lat <= 90:
        exif["Exif.GPSInfo.GPSLatitude"] = _gps_rational(lat)
        exif["Exif.GPSInfo.GPSLatitudeRef"] = "N" if lat >= 0 else "S"
        exif["Exif.GPSInfo.GPSLongitude"] = _gps_rational(lon)
        exif["Exif.GPSInfo.GPSLongitudeRef"] = "E" if lon >= 0 else "W"
        exif["Exif.GPSInfo.GPSProcessingMethod"] = ESTIMATED_GPS_METHOD
        xmp["Xmp.exif.GPSLatitude"] = f"{lat:.6f}"
        xmp["Xmp.exif.GPSLongitude"] = f"{lon:.6f}"
        stats.gps_written += 1

    # Provenance, so a future reader can tell where these tags came from.
    if analysis.model:
        xmp["Xmp.xmp.CreatorTool"] = f"photo-organizer via {analysis.model}"
    if analysis.evidence_basis and analysis.evidence_basis != "none":
        xmp["Xmp.dc.source"] = (
            f"location by {analysis.evidence_basis} "
            f"({analysis.location_confidence} confidence)"
        )

    if not (xmp or iptc or exif):
        return False

    try:
        with pyexiv2.Image(str(target)) as img:
            if xmp:
                img.modify_xmp(xmp)
            if iptc:
                img.modify_iptc(iptc)
            if exif:
                img.modify_exif(exif)
    except Exception as exc:
        stats.failed += 1
        stats.errors.append(f"{target.name}: {exc}")
        log.debug("Could not write metadata to %s: %s", target, exc)
        return False

    stats.written += 1
    return True


def read_back(target: Path) -> dict:
    """Read the tags we wrote, for verification and for the tests."""
    try:
        import pyexiv2

        with pyexiv2.Image(str(target)) as img:
            xmp = img.read_xmp()
            iptc = img.read_iptc()
        return {
            "keywords": xmp.get("Xmp.dc.subject") or iptc.get("Iptc.Application2.Keywords"),
            "description": xmp.get("Xmp.dc.description"),
            "title": xmp.get("Xmp.dc.title"),
            "rating": xmp.get("Xmp.xmp.Rating"),
            "label": xmp.get("Xmp.xmp.Label"),
            "city": xmp.get("Xmp.photoshop.City"),
            "state": xmp.get("Xmp.photoshop.State"),
            "country": xmp.get("Xmp.photoshop.Country"),
            "gps_lat": xmp.get("Xmp.exif.GPSLatitude"),
            "source": xmp.get("Xmp.dc.source"),
        }
    except Exception as exc:
        log.debug("Could not read metadata from %s: %s", target, exc)
        return {}
