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
from typing import Callable, Optional, Sequence

from .models import Photo, Plan

log = logging.getLogger(__name__)

DUPLICATES_DIR = "_duplicates_review"
# Appended to a duplicate's filename so it sorts beside the frame it
# duplicates. Reviewing them means comparing them, and comparing them means
# having them in the same folder.
DUPLICATE_SUFFIX = "_duplicate"
# Frames judged empty -- all black, all white, or an accidental
# pocket shot. Set aside for a look, exactly like duplicates, and
# deleted never.
REJECTED_DIR = "_rejected_review"

# Dropped at the root of every output tree we create, so a later run can
# recognise its own work and clear it without guessing.
OUTPUT_MARKER = ".photo-organizer-output"

# Top-level entries a run of ours legitimately leaves behind. Anything else
# means the folder holds something we did not put there.
_ALLOWED_TOP_LEVEL = {
    OUTPUT_MARKER, DUPLICATES_DIR, REJECTED_DIR,
    "manifest.json", "names.toml", "edits.toml",
    "desktop.ini", "thumbs.db", ".ds_store",
}


class OutputNotOurs(Exception):
    """The output folder holds files this tool did not write."""


def _is_year_dir(entry: Path) -> bool:
    return entry.is_dir() and len(entry.name) == 4 and entry.name.isdigit()


def unrecognised_entries(output: Path) -> list[str]:
    """Top-level things in the output that a run of ours would not create."""
    if not output.exists():
        return []
    strange = []
    for entry in sorted(output.iterdir()):
        if _is_year_dir(entry) or entry.name.lower() in _ALLOWED_TOP_LEVEL:
            continue
        strange.append(entry.name)
    return strange


def mark_output(output: Path) -> None:
    """Record that this tree is ours, so a later run may clear it."""
    try:
        output.mkdir(parents=True, exist_ok=True)
        (output / OUTPUT_MARKER).write_text(
            "Written by photo_organizer. This folder is disposable: deleting "
            "it and re-running rebuilds it from the read-only source.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.debug("Could not write the output marker: %s", exc)


def clear_output(
    output: Path,
    source: Path,
    dry_run: bool = False,
) -> tuple[int, int]:
    """Empty the output tree before a fresh run. Returns (files, bytes).

    Refuses unless the folder is demonstrably ours: either it carries our
    marker, or every top-level entry is one a run of ours creates (a year
    folder, or one of the review folders). A folder holding anything else is
    left completely alone -- better a confusing extra folder than deleting
    someone's photos.

    The source is never touched, and check_paths is re-run here rather than
    trusted from the caller.
    """
    import shutil

    from .scan import check_paths

    source, output = check_paths(source, output)
    if not output.exists():
        return 0, 0

    strange = unrecognised_entries(output)
    if strange and not (output / OUTPUT_MARKER).exists():
        raise OutputNotOurs(
            f"{output} holds {len(strange)} item(s) this tool did not write "
            f"({', '.join(strange[:5])}"
            + (", ..." if len(strange) > 5 else "")
            + "). Nothing was deleted. Point the output somewhere else, or "
            "empty that folder yourself if you are sure."
        )

    files = 0
    total = 0
    for entry in output.rglob("*"):
        if entry.is_file():
            files += 1
            try:
                total += entry.stat().st_size
            except OSError:
                pass
    if dry_run:
        return files, total

    for entry in sorted(output.iterdir()):
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            log.warning("Could not remove %s: %s", entry, exc)
    log.info("Cleared %s: %d file(s) removed", output, files)
    return files, total

VERIFY_CHUNK = 1024 * 1024


class CopyCancelled(Exception):
    """Raised when the caller asks a running copy to stop."""


@dataclass
class CopyStats:
    planned: int = 0
    copied: int = 0
    skipped_existing: int = 0
    duplicates_copied: int = 0
    rejected_copied: int = 0
    verify_failures: int = 0
    errors: list[str] = field(default_factory=list)
    bytes_copied: int = 0
    tagged: int = 0
    tag_failures: int = 0

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        # The count matters as much as the samples: ten failures and five
        # hundred looked identical when only the first ten were reported.
        d["error_count"] = len(self.errors)
        d["errors"] = self.errors[:20]
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
    # Off by default so an interrupted copy stays resumable: re-running
    # skips what is already there rather than starting the 50 GB again.
    # Clearing belongs to STARTING a run, and the pipeline does it once.
    clear_first: bool = False,
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

    # Start from an empty output, so a re-run cannot leave the previous
    # run's folders standing next to the new ones. That is how a stale
    # Mont-Blanc-Massif survived a run that had already learned better.
    # Refuses anything it does not recognise as our own output.
    if clear_first:
        try:
            removed, freed = clear_output(output_root, source_root)
            if removed:
                say(f"Cleared {removed} file(s) from a previous run "
                    f"({freed / 1e9:.1f} GB). The source is untouched.")
        except OutputNotOurs as exc:
            say(str(exc))
            raise
    mark_output(output_root)

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

        # Which cache key describes each kept frame, so a duplicate can
        # borrow the analysis of the photo it duplicates. Built once per
        # event; the alternative is hashing the keeper again for every
        # duplicate of it.
        keeper_keys: dict[str, str] = {}
        for candidate in event.photos:
            if getattr(candidate, "duplicate_role", None) in (None, "keep"):
                key = candidate.content_key or content_hash(
                    candidate.source_path, candidate.size_bytes
                )
                if key:
                    candidate.content_key = key
                    keeper_keys[str(candidate.source_path)] = key

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

            reason = getattr(photo, "reject_reason", None)
            if reason:
                _copy_rejected(photo, output_root, stats, deep_verify, reason)
                continue
            role = getattr(photo, "duplicate_role", None)
            if role in ("exact", "near"):
                _copy_duplicate(
                    photo, output_root, stats, deep_verify,
                    destination=destination,
                    beside_original=config.analysis.duplicates_beside_original,
                    # Everything the tagged path gets. A duplicate is the
                    # same picture as the frame that was analysed, so the
                    # same description applies to it.
                    store=store if tagging else None,
                    keeper_key=keeper_keys.get(getattr(photo, "duplicate_of", None)),
                    event_tags=event_tags,
                    event_title=event.place_name or event.effective_name,
                    event_location=event_location,
                    tag_stats=tag_stats,
                )
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


def _copy_rejected(
    photo: Photo, output_root: Path, stats: CopyStats, deep: bool, reason: str
) -> None:
    """Set an empty frame aside where it can be looked at.

    Grouped by why it was rejected, so a wrong call is easy to spot and
    easy to undo -- the file is right there. Nothing is deleted.
    """
    folder = output_root / REJECTED_DIR / reason
    folder.mkdir(parents=True, exist_ok=True)
    target = unique_target(folder, photo.source_path.name)
    try:
        shutil.copy2(photo.source_path, target)
    except OSError as exc:
        stats.errors.append(f"{photo.source_path.name}: {exc}")
        return
    if verify_copy(photo.source_path, target, deep=deep):
        stats.rejected_copied += 1
        stats.bytes_copied += photo.size_bytes or 0
    else:
        stats.verify_failures += 1
        try:
            target.unlink()
        except OSError:
            pass


def _copy_duplicate(
    photo: Photo,
    output_root: Path,
    stats: CopyStats,
    deep: bool,
    destination: Optional[Path] = None,
    beside_original: bool = True,
    store=None,
    keeper_key: Optional[str] = None,
    event_tags: Sequence[str] = (),
    event_title: Optional[str] = None,
    event_location: Optional[tuple] = None,
    tag_stats=None,
) -> None:
    """Put a suspected duplicate where it can actually be judged.

    By default that is the SAME folder as the frame it duplicates, with
    `_duplicate` appended to the name, so the two sort together and can be
    compared without hunting through a second tree.

    Set `duplicates_beside_original = false` to keep the older behaviour of
    a separate `_duplicates_review/` folder grouped by original.

    Nothing is deleted, here or anywhere else.
    """
    if beside_original and destination is not None:
        folder = destination
        source_name = photo.dest_name or photo.source_path.name
        stem = Path(source_name).stem
        name = f"{stem}{DUPLICATE_SUFFIX}{Path(source_name).suffix}"
    else:
        group = "ungrouped"
        if getattr(photo, "duplicate_of", None):
            group = Path(photo.duplicate_of).stem[:60] or "ungrouped"
        folder = output_root / DUPLICATES_DIR / f"{photo.duplicate_role}_{group}"
        name = photo.source_path.name

    folder.mkdir(parents=True, exist_ok=True)
    target = unique_target(folder, name)
    try:
        shutil.copy2(photo.source_path, target)
    except OSError as exc:
        stats.errors.append(f"{photo.source_path.name}: {exc}")
        return
    if not verify_copy(photo.source_path, target, deep=deep):
        stats.verify_failures += 1
        try:
            target.unlink()
        except OSError:
            pass
        return

    stats.duplicates_copied += 1
    stats.bytes_copied += photo.size_bytes or 0

    # Inherit the analysis of the frame this duplicates. Without it a burst
    # of five leaves one searchable photo and four blanks beside it.
    if store is None or not keeper_key:
        return
    analysis = store.get(keeper_key)
    if analysis is None:
        return
    from . import metadata as meta

    if meta.write_analysis(
        target,
        analysis,
        output_root=output_root,
        event_tags=list(event_tags),
        event_title=event_title,
        event_location=event_location,
        stats=tag_stats,
    ):
        stats.tagged += 1
    else:
        stats.tag_failures += 1


