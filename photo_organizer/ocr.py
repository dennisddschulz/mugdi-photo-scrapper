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
import re
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
# 1400, not 1000. The page that was stored sideways gave 3 words upright at
# 1000px -- too few to look like text at all -- and 16 at 1400px. Reading it
# at all depended on this.
OCR_EDGE = 1400

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


# How many real words a pass has to find before it is worth trying the
# photo at other angles. Photographs of rock produce a handful of gibberish
# tokens; a page produces many more.
# Measured at 1400px on real photos: the two topo pages of one event gave
# 16 and 49 real words upright, while photographs of rock gave 0, 2, 2, 3,
# 7, 9, 10 -- and one gave 27. The sets overlap, so this cannot be a
# classifier. It does not need to be: escalating on a rock photo costs a few
# seconds, and the gazetteer throws away the gibberish that comes back.
TEXT_LIKELY_WORDS = 12

# Page-segmentation modes worth trying. 6 assumes a uniform block of text,
# 3 lets Tesseract find the layout itself; measured, each reads pages the
# other misses.
PSM_MODES = (6, 3)

# Cameras do not always record which way up a photo is: the topo that could
# not be read has an EXIF orientation of 0, meaning unset.
ROTATIONS = (0, 270, 180, 90)


def _run_tesseract(binary: str, path: Path, edge: int, psm: int,
                   rotation: int, timeout: int) -> str:
    from PIL import Image, ImageOps

    tmp = None
    try:
        with Image.open(path) as img:
            # Honour the orientation tag when there is one. Often there is
            # not, which is why rotations are tried as well.
            img = ImageOps.exif_transpose(img) or img
            img.draft("L", (edge, edge))
            grey = img.convert("L")
            grey.thumbnail((edge, edge))
            if rotation:
                grey = grey.rotate(rotation, expand=True)
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                tmp = Path(handle.name)
            grey.save(tmp)
        result = subprocess.run(
            [binary, str(tmp), "stdout", "-l", LANGUAGES, "--psm", str(psm)],
            capture_output=True, timeout=timeout,
        )
        return result.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("OCR failed on %s (psm %s, rot %s): %s", path, psm, rotation, exc)
        return ""
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def word_count(text: str) -> int:
    """Words long enough to be words, as a proxy for "this holds text"."""
    import re

    return len(re.findall(r"[A-Za-z\u00C0-\u024F]{4,}", text))


def read_text(path: Path, edge: int = OCR_EDGE, timeout: int = 60) -> str:
    """One cheap pass. Enough for a page that is the right way up."""
    binary = find_tesseract()
    if binary is None:
        return ""
    return _run_tesseract(binary, path, edge, PSM_MODES[0], 0, timeout)


def read_text_hard(
    path: Path,
    peak_index=None,
    countries: Sequence[str] = (),
    edge: int = OCR_EDGE,
    timeout: int = 60,
) -> tuple:
    """Read a photo, trying harder when it looks like it holds text.

    Returns (text, names). Stops as soon as a specific place name appears --
    there is nothing to gain from reading the rest of a page whose subject
    is already known.

    The escalation only happens for photos that look like text. Rock
    produces gibberish at every angle, and trying eight passes on all of it
    would make this unaffordable.
    """
    binary = find_tesseract()
    if binary is None:
        return "", ()

    def names_in(text: str) -> tuple:
        if not text.strip() or peak_index is None or not len(peak_index):
            return ()
        return tuple(
            peak.name
            for peak in peak_index.names_in_text(text, countries=countries or None)
            # A name with a climbing grade after it is a route on a topo,
            # not a place: "la cheminee 5b" named an Alpine crag after a
            # hill in the sub-Antarctic. An altitude cannot trigger this.
            if not followed_by_grade(peak.name, text)
        )

    collected: list[str] = []
    best = 0
    for rotation in ROTATIONS:
        for psm in PSM_MODES:
            text = _run_tesseract(binary, path, edge, psm, rotation, timeout)
            collected.append(text)
            best = max(best, word_count(text))
            found = names_in(text)
            if any(is_specific_enough(name) for name in found):
                return "\n".join(collected), found
        # After the upright attempts, only keep going if something in this
        # photo actually looks like writing.
        if rotation == 0 and best < TEXT_LIKELY_WORDS:
            break

    joined = "\n".join(collected)
    return joined, names_in(joined)


def specificity(name: str) -> tuple:
    """How specific a place name is. Higher is better.

    "Aiguille" is a real gazetteer entry and a useless answer -- it is
    French for "needle" and there are hundreds. "Aiguille Dibona" is an
    answer. Word count first, then length, because a two-word name is
    almost always the fuller form of the one-word one it contains.
    """
    words = name.split()
    return (len(words), len(name))


# Articles and geographic generics. A gazetteer contains thousands of real
# places called "Le Toit" or "Am Berg", and reading one off a photo says
# nothing about where the photo was taken.
_ARTICLES = {
    "le", "la", "les", "der", "die", "das", "den", "dem", "il", "lo", "gli",
    "the", "am", "auf", "im", "in", "de", "du", "des", "del", "al", "el",
    "aux", "zum", "zur", "sur", "sous",
}
_GENERIC = {
    "toit", "nid", "puy", "berg", "dent", "pointe", "cima", "punta", "mont",
    "col", "tete", "tour", "roc", "pic", "haus", "hut", "alp", "see", "val",
    "hof", "bach", "wald", "feld", "kopf", "horn", "stein", "cresta", "croix",
}


def is_specific_enough(name: str) -> bool:
    """Is this name worth putting in a folder?

    Two rules, and both are needed. The length rule alone let through "Sé
    Pé" -- two accented fragments -- and "Le Toit", "Le Nid", "Le Puy",
    which are French for the roof, the nest and the puy. All are real
    gazetteer entries and none of them says where a photo was taken.

    So: enough of a name (two words, or one long one), AND something in it
    that is neither an article nor the generic word every third mountain
    shares. "Aiguille Dibona" keeps its Dibona; "Le Toit" has nothing left.
    """
    words = [w.strip("-–—") for w in name.split() if w.strip("-–—")]
    if not words:
        return False
    if len(words) < 2 and len(name) < 11:
        return False
    core = [
        w for w in (word.lower() for word in words)
        if w not in _ARTICLES and w not in _GENERIC
    ]
    return any(len(w) >= 5 for w in core)


def scan_for_text(
    photos: Sequence,
    max_photos: int = 0,
    store=None,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, Path], None]] = None,
) -> list[tuple]:
    """One cheap pass over an event, ranking photos by how much text they hold.

    The expensive reading -- every rotation, both segmentation modes -- costs
    about eight seconds a photo, which is unaffordable across an event. One
    upright pass costs one second and is enough to say which few photos are
    worth that. Returns [(words, photo)], most text first.
    """
    binary = find_tesseract()
    if binary is None:
        return []
    candidates = [
        p for p in photos
        if getattr(p, "duplicate_role", None) in (None, "keep")
        and getattr(p, "reject_reason", None) is None
    ] or list(photos)
    if max_photos:
        candidates = candidates[:max_photos]

    ranked: list[tuple] = []
    for position, photo in enumerate(candidates, start=1):
        if should_cancel is not None and should_cancel():
            break
        if progress:
            progress(position, len(candidates), photo.source_path)
        # Never read the same bytes twice. A pass over this library takes
        # hours; doing it again on the next run is the same mistake as
        # paying twice for the same analysis.
        key = getattr(photo, "content_key", None)
        text = store.get_text(key) if (store is not None and key) else None
        if text is None:
            text = _run_tesseract(
                binary, photo.source_path, OCR_EDGE, PSM_MODES[0], 0, 60
            )
            if store is not None and key:
                store.put_text(key, text)
        ranked.append((word_count(text), photo))
    ranked.sort(key=lambda pair: -pair[0])
    return ranked


def read_event(
    photos: Sequence,
    peak_index,
    countries: Sequence[str] = (),
    stop_after_hit: bool = True,
    max_photos: int = 0,
    deep_photos: int = 6,
    store=None,
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

    # Cheap pass first, to find out WHICH photos are worth reading properly.
    ranked = scan_for_text(photos, max_photos=max_photos, store=store,
                           should_cancel=should_cancel, progress=progress)
    candidates = [photo for words, photo in ranked if words >= 3][:deep_photos]
    if not candidates and ranked:
        candidates = [ranked[0][1]]

    for position, photo in enumerate(candidates, start=1):
        if should_cancel is not None and should_cancel():
            break
        if progress:
            progress(position, len(candidates), photo.source_path)
        text, names = read_text_hard(
            photo.source_path, peak_index, countries=countries
        )
        if not text.strip():
            continue
        names = tuple(n for n in names if not followed_by_grade(n, text))
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


# A name followed by a climbing grade is a ROUTE, not a place.
#
# This is what actually went wrong on the Kerguelen naming. OCR read a Swiss
# crag topo correctly:
#
#     sektor C - petit pilier   Laenge 40 m   Einstieg 1020 m
#     la cheminee 5b
#     Pilier Kocher 6b+ (6b obl.)      Le pelerin 6a+
#
# "la cheminee 5b" is a route -- the chimney, graded 5b. It was taken for a
# place name, the gazetteer had exactly one place spelled that way, and an
# Alpine crag was named after a hill at -49.2150, 70.0033 in the
# sub-Antarctic Indian Ocean.
#
# Altitudes must NOT trigger this: "Salbitschijen 2981 m" is a real summit
# with its height, and the pattern below cannot match a four-figure number.
# A grade must carry a LETTER or a SIGN. A bare digit is not enough, and
# that is not a detail: German guidebooks print altitudes as "(3.275 m)",
# and a bare-digit pattern read the 3 as a French grade and threw away
# "Dammazwillinge (3.275 m)" -- a real 3275 m summit -- leaving the event
# named after a different peak on the same page.
_GRADE = (
    r"(?:"
    r"5\.\d{1,2}[a-d]?"          # YDS: 5.10a  (before the sport grade)
    r"|[3-9][abc][+-]?"          # French/sport: 5b, 6a+, 7c
    r"|[3-9][+-]"                # 6+, 7-
    r"|[IVX]{2,5}[+-]?"          # UIAA: IV+, VI-, VII
    r"|WI\s?\d|M[3-9]|A[0-5]"    # ice, mixed, aid
    r")"
    # Nothing alphanumeric may follow. A word boundary cannot be used
    # here: a grade ending in + or - has no word character after it, so
    # "7-" was missed entirely. This also stops "VI" matching in "Villa".
    r"(?![A-Za-z0-9.,])"
)
_GRADE_AFTER = re.compile(
    r"[\s:.,\-]{0,4}\(?" + _GRADE, re.IGNORECASE
)


def followed_by_grade(name: str, text: str) -> bool:
    """Does this name appear in the text with a climbing grade after it?"""
    if not name or not text:
        return False
    for match in re.finditer(re.escape(name), text, re.IGNORECASE):
        if _GRADE_AFTER.match(text, match.end()):
            return True
    return False
