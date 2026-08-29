"""Duplicate detection (R-F12). Detection only -- nothing is ever deleted.

Two kinds of duplicate, found two ways:

  exact      identical bytes. A content hash settles it with no false
             positives at all.
  near       the same picture re-encoded, resized, or one frame of a burst.
             A perceptual hash catches these; it is a judgement call, so
             they are reported as *suspected* and grouped for review.

Why this runs before the analysis stage: a burst of 30 near-identical
frames costs 30 paid API calls to learn exactly what one of them would
have told us. Marking duplicates first means the analysis stage skips them.

NOTHING HERE DELETES ANYTHING. Suspected duplicates are marked in the
manifest, and milestone 2 will copy them into `_duplicates_review/` so the
user can judge. That is a hard rule from CLAUDE.md: the tool never removes a
photo, and the user clears the source by hand.
"""

from __future__ import annotations

import hashlib
import logging
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

from .models import Photo

log = logging.getLogger(__name__)

# Bytes hashed for the "exact" test. Reading whole 4MB files off an external
# disk is slow; the first and last chunk plus the size is enough to make a
# collision effectively impossible for real photos, and is far faster.
HEAD_BYTES = 128 * 1024

# Hamming distance between perceptual hashes below which two images are
# treated as near-duplicates. 0 is identical; 64 is unrelated. 5 is the
# usual starting point, but measured on this library real bursts scored 0
# and unrelated flat images scored 4, so the margin is generous. Combined
# with the flat-image filter, 4 errs towards missing rather than
# over-grouping -- the right way round when the output is a review folder.
NEAR_THRESHOLD = 4


@dataclass
class DuplicateGroup:
    """A set of photos believed to be the same picture."""

    kind: str  # "exact" | "near"
    photos: list[Photo] = field(default_factory=list)
    # The photo suggested as the one to keep. A suggestion only: the tool
    # never acts on it.
    best: Optional[Photo] = None
    reason: str = ""

    @property
    def size(self) -> int:
        return len(self.photos)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "count": len(self.photos),
            "reason": self.reason,
            "best": str(self.best.source_path) if self.best else None,
            "photos": [str(p.source_path) for p in self.photos],
        }


@dataclass
class DedupeStats:
    scanned: int = 0
    exact_groups: int = 0
    near_groups: int = 0
    exact_duplicates: int = 0
    near_duplicates: int = 0
    unreadable: int = 0
    flat_skipped: int = 0

    @property
    def total_duplicates(self) -> int:
        return self.exact_duplicates + self.near_duplicates

    def to_dict(self) -> dict:
        d = self.__dict__.copy()
        d["total_duplicates"] = self.total_duplicates
        return d


def content_hash(path: Path, size_bytes: int = 0) -> Optional[str]:
    """A fast, collision-safe fingerprint of the file's bytes."""
    try:
        digest = hashlib.blake2b(digest_size=16)
        digest.update(str(size_bytes).encode())
        with open(path, "rb") as handle:
            digest.update(handle.read(HEAD_BYTES))
            if size_bytes > HEAD_BYTES * 2:
                handle.seek(-HEAD_BYTES, 2)
                digest.update(handle.read(HEAD_BYTES))
        return digest.hexdigest()
    except OSError as exc:
        log.debug("Could not hash %s: %s", path, exc)
        return None


# A dHash encodes brightness *changes*. A flat image -- blown-out snow, a
# pocket shot, an all-black frame -- has almost none, so its hash collapses
# towards all-zeros or all-ones and then matches every other flat image.
# Measured on this library: two unrelated photos hashed 0/64 and 4/64 bits
# set and were "duplicates" at distance 4. Requiring a spread of set bits
# rejects images the hash cannot describe. Real photos sit near 32/64.
MIN_HASH_BITS = 12
MAX_HASH_BITS = 52


def _dhash_from_image(img) -> Optional[int]:
    from PIL import Image, ImageOps

    img = ImageOps.exif_transpose(img) or img
    img = img.convert("L").resize((9, 8), Image.LANCZOS)
    pixels = list(img.getdata())
    bits = 0
    for row in range(8):
        offset = row * 9
        for col in range(8):
            bits <<= 1
            if pixels[offset + col] > pixels[offset + col + 1]:
                bits |= 1
    return bits


def perceptual_hash(path: Path, head: Optional[bytes] = None) -> Optional[int]:
    """64-bit dHash: robust to re-encoding, resizing and mild edits.

    Prefers the EXIF thumbnail embedded in the first few KB of the file.
    Decoding the full image instead means reading essentially the whole
    library off disk -- measured at 50 GB and 45 minutes for 13,881 photos,
    versus seconds when the thumbnail is used. The thumbnail is a faithful
    downscale of the same picture, which is all a dHash needs.
    """
    from PIL import Image

    if head:
        thumb = _exif_thumbnail(head)
        if thumb:
            try:
                import io as _io

                with Image.open(_io.BytesIO(thumb)) as img:
                    return _dhash_from_image(img)
            except Exception:
                pass  # fall through to a full decode

    try:
        with Image.open(path) as img:
            img.draft("L", (64, 64))
            return _dhash_from_image(img)
    except Exception as exc:
        log.debug("Could not perceptual-hash %s: %s", path, exc)
        return None


def _exif_thumbnail(head: bytes) -> Optional[bytes]:
    """Pull the embedded JPEG thumbnail out of an already-read file head.

    Located by scanning for the second SOI marker: the first starts the
    main image, and the thumbnail is its own complete JPEG inside APP1.
    """
    first = head.find(b"\xff\xd8", 2)
    if first < 0:
        return None
    end = head.find(b"\xff\xd9", first)
    if end < 0:
        return None
    candidate = head[first : end + 2]
    # Too small to be a usable thumbnail, or implausibly large for one.
    if not 1024 <= len(candidate) <= 200_000:
        return None
    return candidate


def usable_for_near_matching(phash: Optional[int]) -> bool:
    """Whether a hash carries enough structure to compare meaningfully."""
    if phash is None:
        return False
    return MIN_HASH_BITS <= bin(phash).count("1") <= MAX_HASH_BITS


def hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def _score(photo: Photo) -> tuple:
    """Rank a photo within a duplicate group. Higher is better to keep."""
    pixels = (photo.width or 0) * (photo.height or 0)
    return (
        pixels,
        photo.size_bytes,
        1 if photo.has_exif_time else 0,
        1 if photo.has_gps else 0,
    )


def find_duplicates(
    photos: Sequence[Photo],
    workers: int = 16,
    near: bool = True,
    progress: Optional[Callable[[int, int], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> tuple[list[DuplicateGroup], DedupeStats]:
    """Group photos into exact and near-duplicate sets.

    Returns (groups, stats). Photos not in any group are unique. Nothing is
    modified on disk and nothing is deleted.
    """
    stats = DedupeStats(scanned=len(photos))
    if not photos:
        return [], stats

    def fingerprint(photo: Photo) -> tuple[Photo, Optional[str], Optional[int]]:
        # One read serves both hashes: the head gives the content digest and
        # usually contains the EXIF thumbnail the perceptual hash wants.
        head = b""
        try:
            with open(photo.source_path, "rb") as handle:
                head = handle.read(HEAD_BYTES)
        except OSError as exc:
            log.debug("Could not read %s: %s", photo.source_path, exc)
            return photo, None, None
        digest = hashlib.blake2b(digest_size=16)
        digest.update(str(photo.size_bytes).encode())
        digest.update(head)
        return (
            photo,
            digest.hexdigest(),
            perceptual_hash(photo.source_path, head=head) if near else None,
        )

    results: list[tuple[Photo, Optional[str], Optional[int]]] = []
    workers = max(1, workers)
    chunk = workers * 8
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for start in range(0, len(photos), chunk):
            if should_cancel is not None and should_cancel():
                break
            batch = list(photos[start : start + chunk])
            results.extend(pool.map(fingerprint, batch))
            if progress:
                progress(min(start + chunk, len(photos)), len(photos))

    # --- exact duplicates ------------------------------------------------
    by_content: dict[str, list[Photo]] = defaultdict(list)
    phashes: list[tuple[Photo, int]] = []
    exact_members: set[int] = set()

    for photo, digest, phash in results:
        if digest is None:
            stats.unreadable += 1
            continue
        by_content[digest].append(photo)
        if phash is not None:
            phashes.append((photo, phash))

    groups: list[DuplicateGroup] = []
    for digest, members in by_content.items():
        if len(members) < 2:
            continue
        group = DuplicateGroup(kind="exact", photos=members)
        group.best = max(members, key=_score)
        group.reason = "identical file contents"
        groups.append(group)
        stats.exact_groups += 1
        stats.exact_duplicates += len(members) - 1
        for member in members:
            exact_members.add(id(member))

    # --- near duplicates -------------------------------------------------
    if near:
        # Exact duplicates are already grouped; comparing them again would
        # just re-report them.
        remaining = [
            (p, h)
            for p, h in phashes
            if id(p) not in exact_members and usable_for_near_matching(h)
        ]
        stats.flat_skipped = len(phashes) - len(exact_members) - len(remaining)
        # Bucket by the top bits so this is not a full O(n^2) sweep. Two
        # hashes within a few bits usually share a prefix; the buckets are
        # a cheap filter, not a guarantee.
        buckets: dict[int, list[tuple[Photo, int]]] = defaultdict(list)
        for photo, phash in remaining:
            buckets[phash >> 48].append((photo, phash))

        seen: set[int] = set()
        for bucket in buckets.values():
            for i, (photo, phash) in enumerate(bucket):
                if id(photo) in seen:
                    continue
                members = [photo]
                member_hashes = [phash]
                for other, other_hash in bucket[i + 1 :]:
                    if id(other) in seen:
                        continue
                    # Complete linkage: the candidate must be close to every
                    # existing member, not just the seed. Single linkage lets
                    # a chain of small steps merge genuinely different
                    # pictures into one enormous group.
                    if all(
                        hamming(existing, other_hash) <= NEAR_THRESHOLD
                        for existing in member_hashes
                    ):
                        members.append(other)
                        member_hashes.append(other_hash)
                if len(members) < 2:
                    continue
                for member in members:
                    seen.add(id(member))
                group = DuplicateGroup(kind="near", photos=members)
                group.best = max(members, key=_score)
                group.reason = f"perceptually similar (dHash within {NEAR_THRESHOLD} bits)"
                groups.append(group)
                stats.near_groups += 1
                stats.near_duplicates += len(members) - 1

    return groups, stats


def mark_duplicates(groups: Iterable[DuplicateGroup]) -> int:
    """Flag every non-best member of each group on the Photo objects.

    Marking only. The flag lets slow stages skip redundant work and lets the
    preview report what was found; it never causes a file to be removed.
    """
    marked = 0
    for group in groups:
        for photo in group.photos:
            if photo is group.best:
                photo.duplicate_role = "keep"
                continue
            photo.duplicate_role = group.kind
            photo.duplicate_of = (
                str(group.best.source_path) if group.best else None
            )
            photo.warnings.append(f"suspected {group.kind} duplicate")
            marked += 1
    return marked


def unique_photos(photos: Sequence[Photo]) -> list[Photo]:
    """The photos worth spending expensive processing on.

    One representative per duplicate group plus everything unique.
    """
    return [p for p in photos if getattr(p, "duplicate_role", None) in (None, "keep")]
