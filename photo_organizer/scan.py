"""Recursive, read-only discovery of source images (R-F1).

This module only ever reads. The one guard it enforces up front --
`check_paths` -- exists to make the "output inside source" mistake
impossible, since that would make the tool appear to write into the source
tree and would break the delete-output-and-retry recovery story (R-S6).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterator, Optional

from .config import ScanConfig
from .exif import BACKENDS, read_photo
from .models import Photo, SkippedFile

log = logging.getLogger(__name__)


class UnsafePathError(Exception):
    """Raised when the requested source/output layout is not safe to use."""


def _resolve(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(path)))))


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def check_paths(source: Path, output: Path) -> tuple[Path, Path]:
    """Validate source/output and return them resolved.

    Refuses the two layouts that could put written files inside the source
    tree, or feed our own output back in as input on a later run.
    """
    source = _resolve(source)
    output = _resolve(output)

    if not source.exists():
        raise UnsafePathError(f"Source does not exist: {source}")
    if not source.is_dir():
        raise UnsafePathError(f"Source is not a directory: {source}")

    if source == output:
        raise UnsafePathError(
            "Source and output are the same directory. The output must be a "
            "separate tree so the source is never written to."
        )
    if _is_within(output, source):
        raise UnsafePathError(
            f"Output ({output}) is inside the source ({source}). Choose an "
            "output directory outside the source tree; writing inside the "
            "source is never allowed."
        )
    if _is_within(source, output):
        raise UnsafePathError(
            f"Source ({source}) is inside the output ({output}). That would "
            "re-ingest copied files on the next run."
        )
    return source, output


def iter_entries(
    source: Path, config: ScanConfig
) -> Iterator[tuple[os.DirEntry, Optional[str]]]:
    """Walk the source yielding (DirEntry, skip_reason).

    Uses scandir rather than os.walk because a DirEntry carries the file
    size from the directory read itself. Calling Path.stat() per file
    instead costs a fresh syscall each time, which measured 44s on a
    14k-photo library versus 0.3s here.
    """
    excluded = {d.lower() for d in config.exclude_dirs}
    extensions = {e.lower() for e in config.image_extensions}

    stack = [source]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as it:
                entries = sorted(it, key=lambda e: e.name)
        except (PermissionError, OSError) as exc:
            log.warning("Skipping unreadable folder %s: %s", current, exc)
            continue

        for entry in entries:
            try:
                is_dir = entry.is_dir(follow_symlinks=config.follow_symlinks)
            except OSError:
                continue
            if is_dir:
                if entry.name.lower() not in excluded:
                    stack.append(Path(entry.path))
                continue

            name = entry.name
            if name.startswith("."):
                yield entry, "hidden file"
                continue
            suffix = os.path.splitext(name)[1].lower()
            if not suffix:
                yield entry, "no extension"
                continue
            if suffix not in extensions:
                yield entry, f"not an image ({suffix})"
                continue
            yield entry, None


def iter_files(
    source: Path, config: ScanConfig
) -> Iterator[tuple[Path, Optional[str]]]:
    """Walk the source, yielding (path, skip_reason). skip_reason is None
    for files we intend to ingest."""
    for entry, reason in iter_entries(source, config):
        yield Path(entry.path), reason


class Cancelled(Exception):
    """Raised when a caller asks a long-running scan to stop."""


def survey_source(source: Path, config: ScanConfig) -> dict:
    """Cheap first look at a folder: counts and sizes, no EXIF parsing.

    Used by the UI to show what is in a folder before committing to a full
    scan, which on a large library can take minutes.
    """
    by_ext: dict[str, int] = {}
    images = 0
    other = 0
    total_bytes = 0
    folders = 0

    for entry, reason in iter_entries(source, config):
        if reason is not None:
            other += 1
            continue
        images += 1
        ext = os.path.splitext(entry.name)[1].lower()
        by_ext[ext] = by_ext.get(ext, 0) + 1
        try:
            # Free on Windows: the size came back with the directory read.
            total_bytes += entry.stat().st_size
        except OSError:
            pass

    for _root, dirnames, _files in os.walk(source):
        folders += len(dirnames)
        break

    return {
        "images": images,
        "other_files": other,
        "total_bytes": total_bytes,
        "top_level_folders": folders,
        "by_extension": dict(sorted(by_ext.items(), key=lambda kv: -kv[1])),
    }


def count_images(source: Path, config: ScanConfig) -> int:
    """How many files a full scan would read. Lets the UI show N of M."""
    return sum(1 for _path, reason in iter_files(source, config) if reason is None)


def scan_source(
    source: Path,
    config: ScanConfig,
    progress: Optional[Callable[[int, Path], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[list[Photo], list[SkippedFile]]:
    """Read every image under `source`. Returns (photos, skipped).

    Raises Cancelled if `should_cancel` starts returning True, so a long
    scan started from the UI can be stopped.
    """
    # Resolve optional backends before any worker thread runs, so none of
    # them races on a half-initialised import.
    BACKENDS.warm_up()

    photos: list[Photo] = []
    skipped: list[SkippedFile] = []
    targets: list[tuple[Path, Any]] = []

    for entry, reason in iter_entries(source, config):
        path = Path(entry.path)
        if reason is not None:
            skipped.append(SkippedFile(path=path, reason=reason))
            continue
        try:
            # Size and mtime already came back with the directory read, so
            # hand them over rather than paying for another stat per file.
            stat_result = entry.stat()
        except OSError:
            stat_result = None
        targets.append((path, stat_result))

    def read_one(item: tuple[Path, Any]) -> tuple[Path, Optional[Photo], str]:
        path, stat_result = item
        try:
            return path, read_photo(
                path,
                use_filename_fallback=config.use_filename_fallback,
                use_mtime_fallback=config.use_mtime_fallback,
                stat_result=stat_result,
            ), ""
        except OSError as exc:
            # An unreadable file is skipped, not fatal: one bad file on a
            # flaky USB drive must not abort a 40k-photo scan.
            return path, None, f"unreadable: {exc}"
        except Exception as exc:
            return path, None, f"metadata read failed: {exc}"

    workers = max(1, int(getattr(config, "scan_workers", 1) or 1))

    def absorb(result: tuple[Path, Optional[Photo], str]) -> None:
        path, photo, problem = result
        if photo is None:
            skipped.append(SkippedFile(path=path, reason=problem))
            return
        photos.append(photo)
        if progress is not None:
            progress(len(photos), path)

    if workers == 1:
        for index, item in enumerate(targets):
            if should_cancel is not None and index % 25 == 0 and should_cancel():
                raise Cancelled()
            absorb(read_one(item))
    else:
        # Chunked so cancellation is responsive without cancelling futures
        # mid-flight; map preserves input order, keeping runs reproducible.
        chunk = workers * 8
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for start in range(0, len(targets), chunk):
                if should_cancel is not None and should_cancel():
                    raise Cancelled()
                for result in pool.map(read_one, targets[start : start + chunk]):
                    absorb(result)

    return photos, skipped
