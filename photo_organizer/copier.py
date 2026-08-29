"""Copy the library into its new shape, then tag the copies (R-F10, R-F11).

This is the only module that creates files, and the rules it follows come
straight from CLAUDE.md:

* Sources are opened read-only and never modified, moved or deleted.
* Every copy is verified before it counts as done.
* Existing files are never overwritten; a collision gets a numeric suffix.
* Suspected duplicates are COPIED into `_duplicates_review/`, never deleted.
* Metadata is written only after a copy is verified, and only to the copy.

Recovery is unchanged: if anything goes wrong, delete the output folder and
run again. The source remains a complete, untouched fallback throughout.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .models import Photo, Plan

log = logging.getLogger(__name__)

DUPLICATES_DIR = "_duplicates_review"
VERIFY_CHUNK = 1024 * 1024


class CopyCancelled(Exception):
    """Raised when the caller asks a running copy to stop."""


@dataclass
class CopyStats:
    planned: int = 0
    copied: int = 0
    skipped_existing: int = 0
    duplicates_copied: int = 0
    verify_failures: int = 0
    errors: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    tagged: int = 0
    tag_failures: int = 0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["errors"] = self.errors[:10]
        d["gb_copied"] = round(self.bytes_copied / 1e9, 2)
        return d


def _digest(path: Path) -> Optional[str]:
    """Full-content hash, used to verify a copy is byte-identical."""
    try:
        digest = hashlib.blake2b(digest_size=16)
        with open(path, "rb") as handle:
            while True:
                chunk = handle.read(VERIFY_CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        log.debug("Could not hash %s: %s", path, exc)
        return None


def verify_copy(source: Path, target: Path, deep: bool = True) -> bool:
    """Is the copy genuinely identical to the source?

    Size always; full content when `deep`. A size-only check would pass a
    truncated-then-padded file, and these originals are irreplaceable.
    """
    try:
        if source.stat().st_size != target.stat().st_size:
            return False
    except OSError:
        return False
    if not deep:
        return True
    return _digest(source) == _digest(target)


def unique_target(directory: Path, name: str) -> Path:
    """A path in `directory` that does not exist yet.

    Never overwrites. Comparison is case-insensitive because Windows treats
    IMG_1.JPG and img_1.jpg as the same file.
    """
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stem, suffix = Path(name).stem, Path(name).suffix
    existing = {p.name.lower() for p in directory.iterdir()} if directory.exists() else set()
    counter = 1
    while True:
        counter += 1
        attempt = f"{stem}_{counter}{suffix}"
        if attempt.lower() not in existing and not (directory / attempt).exists():
            return directory / attempt


def copy_plan(
    plan: Plan,
    config,
    store=None,
    write_metadata: bool = True,
    deep_verify: bool = True,
    on_step: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> CopyStats:
    """Copy every planned photo into the output tree and tag the copies."""
    from . import metadata as meta
    from .db import AnalysisStore
    from .dedupe import content_hash

    stats = CopyStats()

    def say(message: str) -> None:
        log.info("%s", message)
        if on_step:
            on_step(message)

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise CopyCancelled()

    output_root = Path(plan.output_root)
    source_root = Path(plan.source_root)

    # The guard that makes everything below safe. Re-checked here rather
    # than trusted from the planning stage.
    from .scan import check_paths

    source_root, output_root = check_paths(source_root, output_root)

    tagging = write_metadata and meta.available()
    if write_metadata and not tagging:
        say(meta.unavailable_reason())

    store = store or AnalysisStore(Path(config.analysis.database_path).expanduser())
    tag_stats = meta.WriteStats()

    total = plan.photo_count
    stats.planned = total
    say(f"Copying {total} photo(s) into {output_root}")
    say("Sources are opened read-only; nothing in the source tree is modified.")

    done = 0
    for event in plan.events:
        check_cancel()
        destination = output_root / event.rel_dir
        destination.mkdir(parents=True, exist_ok=True)

        # The event's agreed position, applied to every photo in it.
        event_location = (
            (event.enriched_lat, event.enriched_lon)
            if event.enriched_lat is not None and event.enriched_lon is not None
            else None
        )

        # One analysis per event supplies the tags shared by all its photos.
        event_tags = [t for t, _score in (event.tag_summary or [])]
        if event.mountain_range:
            event_tags.append(event.mountain_range)
        if event.place_name:
            event_tags.append(event.place_name)

        for photo in event.photos:
            check_cancel()
            done += 1
            if on_progress:
                on_progress(done, total, photo.source_path.name)

            role = getattr(photo, "duplicate_role", None)
            if role in ("exact", "near"):
                _copy_duplicate(photo, output_root, stats, deep_verify)
                continue

            target = destination / (photo.dest_name or photo.source_path.name)
            if target.exists():
                # Resumable: an identical file already there is done.
                if verify_copy(photo.source_path, target, deep=deep_verify):
                    stats.skipped_existing += 1
                    continue
                target = unique_target(destination, target.name)

            if not _copy_one(photo, target, stats, deep_verify):
                continue

            if tagging:
                digest = content_hash(photo.source_path, photo.size_bytes)
                analysis = store.get(digest) if digest else None
                if analysis is not None:
                    ok = meta.write_analysis(
                        target,
                        analysis,
                        output_root=output_root,
                        event_tags=event_tags,
                        event_title=event.place_name or event.effective_name,
                        event_location=event_location,
                        stats=tag_stats,
                    )
                    if ok:
                        stats.tagged += 1
                    else:
                        stats.tag_failures += 1

    if tagging:
        say(
            f"Tagged {stats.tagged} copy(s): {tag_stats.tags_written} with "
            f"keywords, {tag_stats.ratings_written} with a rating, "
            f"{tag_stats.gps_written} with an estimated position."
        )
    say(
        f"Copied {stats.copied} file(s) ({stats.bytes_copied/1e9:.1f} GB); "
        f"{stats.skipped_existing} already present; "
        f"{stats.duplicates_copied} to {DUPLICATES_DIR}/; "
        f"{stats.verify_failures} failed verification."
    )
    say("The source is unchanged. To start over, delete the output folder.")
    return stats


def _copy_one(photo: Photo, target: Path, stats: CopyStats, deep: bool) -> bool:
    try:
        shutil.copy2(photo.source_path, target)
    except OSError as exc:
        stats.errors.append(f"{photo.source_path.name}: {exc}")
        log.warning("Copy failed for %s: %s", photo.source_path, exc)
        return False

    if not verify_copy(photo.source_path, target, deep=deep):
        stats.verify_failures += 1
        stats.errors.append(f"{photo.source_path.name}: copy did not verify")
        log.warning("Verification failed for %s", target)
        # Remove the bad copy -- it is ours, in the output tree, and leaving
        # a corrupt file behind is worse than not having one.
        try:
            target.unlink()
        except OSError:
            pass
        return False

    stats.copied += 1
    stats.bytes_copied += photo.size_bytes or 0
    return True


def _copy_duplicate(
    photo: Photo, output_root: Path, stats: CopyStats, deep: bool
) -> None:
    """Put a suspected duplicate where the user can judge it.

    Grouped by the photo it duplicates, so what-is-a-copy-of-what is
    visible. Nothing is deleted, here or anywhere else.
    """
    group = "ungrouped"
    if getattr(photo, "duplicate_of", None):
        group = Path(photo.duplicate_of).stem[:60] or "ungrouped"
    folder = output_root / DUPLICATES_DIR / f"{photo.duplicate_role}_{group}"
    folder.mkdir(parents=True, exist_ok=True)
    target = unique_target(folder, photo.source_path.name)
    try:
        shutil.copy2(photo.source_path, target)
    except OSError as exc:
        stats.errors.append(f"{photo.source_path.name}: {exc}")
        return
    if verify_copy(photo.source_path, target, deep=deep):
        stats.duplicates_copied += 1
        stats.bytes_copied += photo.size_bytes or 0
    else:
        stats.verify_failures += 1
        try:
            target.unlink()
        except OSError:
            pass
