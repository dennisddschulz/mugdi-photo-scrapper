"""Reading the text in photographs, locally and for nothing.

A guidebook page, a signpost or a hut board names a place outright. That is
the most reliable evidence this project has -- measured, it beats terrain
recognition, which once answered "Salbitschijen" for a photo taken 13 km
away and "Aiguille de la Republique" for an event in the Ecrins.

Two things had to be learned the hard way before this file existed.

**Pixel statistics cannot find a page.** Four attempts failed against the
real library: brightness and edges scored a portrait of a person 3.17 and a
real guidebook page 0.0; adding saturation still scored the page 0.0; and at
higher resolution the "pages" it found turned out to be climbers on grey
granite. Grey rock and printed paper are statistically the same thing. A
climbing library is full of grey rock.

**OCR does not need a detector, because it is one.** Run over a photograph
of rock, Tesseract returns "SESS JURE SaaS Paes iets Seen" -- gibberish that
matches nothing in a gazetteer of real place names. Run over a guidebook
page it returns "Furka Galengrat Hannibalturm". The gazetteer does the
filtering, exactly as it does for the model's own answers.

Measured on this library: 1.1 photos/second at 1000px, and the one photo in
a 41-photo event that named Aiguille Dibona was found in 38 seconds. It
costs nothing but time, and the time is spent locally rather than billed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

# Where Tesseract usually is on Windows, plus whatever is on the PATH.
CANDIDATE_BINARIES = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)

# Measured: 1000px reads a guidebook page cleanly at about 1.1 photos/second.
# 1600px was slower AND read less -- accuracy is not monotonic in resolution,
# which this project has now learned twice.
OCR_EDGE = 1000

# Alpine guidebooks are written in these.
LANGUAGES = "eng+deu+fra+ita"


@dataclass
class OcrResult:
    path: Path
    text: str = ""
    names: tuple = ()          # gazetteer names found in the text

    @property
    def useful(self) -> bool:
        return bool(self.names)


def find_tesseract() -> Optional[str]:
    """The Tesseract binary, or None if it is not installed."""
    found = shutil.which("tesseract")
    if found:
        return found
    for candidate in CANDIDATE_BINARIES:
        if Path(candidate).is_file():
            return candidate
    return None


def available() -> bool:
    return find_tesseract() is not None


def unavailable_reason() -> str:
    return (
        "Tesseract is not installed, so text in photos cannot be read locally. "
        "Install it from https://github.com/UB-Mannheim/tesseract/wiki -- it is "
        "free, runs offline, and is the most reliable way to name an event."
    )


def read_text(path: Path, edge: int = OCR_EDGE, timeout: int = 60) -> str:
    """The text in one photo, or an empty string."""
    binary = find_tesseract()
    if binary is None:
        return ""
    from PIL import Image

    tmp = None
    try:
        with Image.open(path) as img:
            # draft() decodes JPEGs at reduced size, which is most of the
            # speed. Greyscale because Tesseract wants it anyway.
            img.draft("L", (edge, edge))
            grey = img.convert("L")
            grey.thumbnail((edge, edge))
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                tmp = Path(handle.name)
            grey.save(tmp)
        result = subprocess.run(
            [binary, str(tmp), "stdout", "-l", LANGUAGES, "--psm", "6"],
            capture_output=True, timeout=timeout,
        )
        return result.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("OCR failed on %s: %s", path, exc)
        return ""
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def specificity(name: str) -> tuple:
    """How specific a place name is. Higher is better.

    "Aiguille" is a real gazetteer entry and a useless answer -- it is
    French for "needle" and there are hundreds. "Aiguille Dibona" is an
    answer. Word count first, then length, because a two-word name is
    almost always the fuller form of the one-word one it contains.
    """
    words = name.split()
    return (len(words), len(name))


def is_specific_enough(name: str) -> bool:
    """Reject names too generic to be worth putting in a folder.

    A single short word passes the gazetteer's own test and still says
    nothing: measured, an event was nearly named "Aiguille" because that
    photo happened to be read first.
    """
    words = name.split()
    return len(words) >= 2 or len(name) >= 11


def read_event(
    photos: Sequence,
    peak_index,
    countries: Sequence[str] = (),
    stop_after_hit: bool = False,
    max_photos: int = 0,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, Path], None]] = None,
) -> list[OcrResult]:
    """Read an event's photos and collect the place names in them.

    It does NOT stop at the first match. Measured: the first photo of one
    event yielded the bare word "Aiguille" -- a real gazetteer entry and a
    useless name -- while a later photo yielded "Aiguille Dibona". Stopping
    early meant taking the worse of the two. The caller ranks what comes
    back by how specific it is.

    Photos already flagged as duplicates or empty are skipped -- a duplicate
    of a page says the same thing the page did.
    """
    results: list[OcrResult] = []
    candidates = [
        p for p in photos
        if getattr(p, "duplicate_role", None) in (None, "keep")
        and getattr(p, "reject_reason", None) is None
    ] or list(photos)
    if max_photos:
        candidates = candidates[:max_photos]

    for position, photo in enumerate(candidates, start=1):
        if should_cancel is not None and should_cancel():
            break
        if progress:
            progress(position, len(candidates), photo.source_path)
        text = read_text(photo.source_path)
        if not text.strip():
            continue
        names = ()
        if peak_index is not None and len(peak_index):
            names = tuple(
                peak.name
                for peak in peak_index.names_in_text(
                    text, countries=countries or None
                )
            )
        result = OcrResult(path=photo.source_path, text=text, names=names)
        results.append(result)
        if names:
            log.info("OCR found %s in %s", ", ".join(names), photo.source_path.name)
            # Stop only once something specific has been found. A one-word
            # match is not worth ending the search for.
            if stop_after_hit and any(is_specific_enough(n) for n in names):
                break
    return results


def best_name(results: Sequence[OcrResult]) -> Optional[tuple]:
    """The most specific place name found, with the photo it came from."""
    candidates = [
        (name, result)
        for result in results
        for name in result.names
        if is_specific_enough(name)
    ]
    if not candidates:
        return None
    name, result = max(candidates, key=lambda pair: specificity(pair[0]))
    return name, result
