"""A gazetteer of named summits, and name matching against it.

This is the piece that separates *detection* from *guessing*. Any name a
model or an OCR pass produces is a claim; checking it against a real list of
summits with coordinates turns it into a fact or rejects it. Measured on
this project, both CLIP and a local VLM asserted "K2" and "Mount Everest"
for Alpine photos with high confidence -- neither name survives a check
against a database of French, Swiss, Italian, Austrian and Slovak peaks.

It also repairs OCR. A vision model read "Refuge du Sorelier" for what is
actually "Soreiller"; fuzzy matching against real names fixes that instead
of silently failing to geocode.

Data comes from OpenStreetMap (`natural=peak` nodes) via Overpass, cached
to disk so the pipeline stays offline after one download. OSM is the source
the specification already names for summits.
"""

from __future__ import annotations

import json
import logging
import math
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

log = logging.getLogger(__name__)

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
USER_AGENT = "photo-organizer/0.1 (personal photo organiser)"
DEFAULT_CACHE = Path("~/.cache/photo_organizer/peaks.json").expanduser()

# The countries this library actually covers.
DEFAULT_COUNTRIES: tuple[str, ...] = ("CH", "FR", "IT", "AT", "SK", "DE", "NO")

COUNTRY_NAMES = {
    "CH": "Switzerland", "FR": "France", "IT": "Italy", "AT": "Austria",
    "SK": "Slovakia", "DE": "Germany", "NO": "Norway", "SI": "Slovenia",
    "ES": "Spain", "PL": "Poland",
}

# Landform types worth having alongside summits. These carry the names
# people use for a *region* -- "Pilatus", "Karwendel" -- which OSM does not
# put on a peak node.
LANDFORM_TYPES: tuple[str, ...] = ("ridge", "massif", "arete", "saddle", "glacier")


@dataclass
class Peak:
    """A named summit or landform.

    Landforms are included deliberately. OSM tags the Pilatus summit as
    "Tomlishorn"; "Pilatus" is the massif, and the massif is the name a
    person would actually use for the region. Summits alone would miss it.
    """

    name: str
    lat: float
    lon: float
    elevation: Optional[int] = None
    country: str = ""
    # Alternative spellings OSM records: name:de, name:fr, name:it, alt_name.
    aliases: tuple[str, ...] = ()
    # peak | ridge | massif | saddle | arete | glacier | cliff
    kind: str = "peak"

    @property
    def is_summit(self) -> bool:
        return self.kind == "peak"

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "lat": self.lat,
            "lon": self.lon,
            "ele": self.elevation,
            "cc": self.country,
            "alt": list(self.aliases),
            "k": self.kind,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Peak":
        return cls(
            name=d["name"],
            lat=d["lat"],
            lon=d["lon"],
            elevation=d.get("ele"),
            country=d.get("cc", ""),
            aliases=tuple(d.get("alt", [])),
            kind=d.get("k", "peak"),
        )


@dataclass
class PeakMatch:
    peak: Peak
    score: float           # 0..1, 1 is an exact normalised match
    matched_on: str = ""   # which spelling matched
    def to_dict(self) -> dict:
        return {"peak": self.peak.to_dict(), "score": round(self.score, 3),
                "matched_on": self.matched_on}


# --------------------------------------------------------------------------
# Name normalisation
# --------------------------------------------------------------------------

# Words that carry no identifying information and differ between languages
# and between a guidebook's phrasing and OSM's. Dropping them lets
# "Aiguille du Midi" match "Aiguille Midi" and "Punta Giradili" match
# "Giradili".
_STOPWORDS = {
    "le", "la", "les", "l", "du", "de", "des", "d", "der", "die", "das",
    "il", "lo", "gli", "dei", "del", "della", "di", "da",
    "mont", "monte", "mount", "piz", "pizzo", "punta", "pointe", "cima",
    "spitze", "spitz", "berg", "kogel", "horn", "kopf", "gipfel",
    "aiguille", "dent", "tete", "grand", "grande", "gross", "grosse",
    "petit", "petite", "klein", "kleine", "vrch", "stit", "veľky", "maly",
}


def normalise(text: str) -> str:
    """Fold a name to a comparable form: ASCII, lowercase, no punctuation."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    """Significant words of a name, stopwords removed."""
    words = [w for w in normalise(text).split() if len(w) > 1]
    meaningful = [w for w in words if w not in _STOPWORDS]
    # If a name is nothing but stopwords ("Le Grand"), keep them: better a
    # weak comparison than none.
    return meaningful or words


def _edit_ratio(a: str, b: str) -> float:
    """Similarity of two strings, 0..1, via Levenshtein distance.

    Written out rather than pulled from a library: the dependency list here
    is already long, and this runs over a few thousand short strings.
    """
    if a == b:
        return 1.0
    if not a or not b:
        return 0.0
    if abs(len(a) - len(b)) / max(len(a), len(b)) > 0.5:
        return 0.0
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb))
            )
        previous = current
    return 1.0 - previous[-1] / max(len(a), len(b))


def name_similarity(query: str, candidate: str) -> float:
    """How alike two summit names are, tolerant of OCR damage.

    Compares whole normalised strings and token sets, taking the better of
    the two: OCR loses whole words as often as it mangles letters.
    """
    qn, cn = normalise(query), normalise(candidate)
    if not qn or not cn:
        return 0.0
    if qn == cn:
        return 1.0

    whole = _edit_ratio(qn, cn)

    qt, ct = set(tokens(query)), set(tokens(candidate))
    if not qt or not ct:
        return whole
    # Every query token gets its best partner among the candidate tokens,
    # so word order and extra words matter less than the words themselves.
    scores = [max(_edit_ratio(q, c) for c in ct) for q in qt]
    token_score = sum(scores) / len(scores)
    # Penalise a match that only covers part of the candidate.
    coverage = min(1.0, len(qt) / len(ct)) ** 0.25
    return max(whole, token_score * coverage)


# --------------------------------------------------------------------------
# Download
# --------------------------------------------------------------------------


def _overpass(query: str, timeout: int = 300, retries: int = 3) -> dict:
    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
    data = urllib.parse.urlencode({"data": query}).encode()
    last: Optional[Exception] = None
    for attempt in range(retries):
        request = urllib.request.Request(
            OVERPASS_URL, data=data, headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read())
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            # Overpass is a free shared service; back off rather than hammer.
            wait = 10 * (attempt + 1)
            log.warning("Overpass attempt %d failed (%s); retrying in %ds",
                        attempt + 1, exc, wait)
            time.sleep(wait)
    raise RuntimeError(f"Overpass failed after {retries} attempts: {last}")


def download_peaks(
    countries: Sequence[str] = DEFAULT_COUNTRIES,
    min_elevation: int = 500,
    progress: Optional[Callable[[str], None]] = None,
) -> list[Peak]:
    """Fetch named summits for the given countries from OpenStreetMap."""
    def say(message: str) -> None:
        log.info("%s", message)
        if progress:
            progress(message)

    found: list[Peak] = []
    for code in countries:
        say(f"Downloading peaks for {COUNTRY_NAMES.get(code, code)}...")
        query = (
            f'[out:json][timeout:280];'
            f'area["ISO3166-1"="{code}"][admin_level=2]->.a;'
            f'(node["natural"="peak"]["name"](area.a););'
            f'out body;'
        )
        try:
            data = _overpass(query)
        except RuntimeError as exc:
            say(f"  {code}: failed ({exc}); skipping")
            continue

        count = 0
        for element in data.get("elements", []):
            tags = element.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            elevation = None
            raw = tags.get("ele")
            if raw:
                try:
                    elevation = int(float(str(raw).replace(",", ".").split()[0]))
                except (ValueError, IndexError):
                    elevation = None
            # Hills and named knolls add noise without adding summits.
            if min_elevation and elevation is not None and elevation < min_elevation:
                continue
            aliases = tuple(
                v.strip()
                for k, v in tags.items()
                if k in ("alt_name", "name:de", "name:fr", "name:it",
                         "name:sk", "name:en", "official_name")
                and v and v.strip() and v.strip() != name
            )
            found.append(
                Peak(
                    name=name,
                    lat=float(element["lat"]),
                    lon=float(element["lon"]),
                    elevation=elevation,
                    country=code,
                    aliases=aliases,
                    kind="peak",
                )
            )
            count += 1
        say(f"  {code}: {count} named peaks")
        time.sleep(5)  # be polite between country queries
    return found


def download_landforms(
    countries: Sequence[str] = DEFAULT_COUNTRIES,
    progress: Optional[Callable[[str], None]] = None,
) -> list[Peak]:
    """Fetch named ridges, massifs, aretes, saddles and glaciers.

    Separate from peaks because these are what answer "which region", and
    because OSM stores several of them as ways rather than nodes -- so the
    query needs `out center` to get a representative coordinate.
    """
    def say(message: str) -> None:
        log.info("%s", message)
        if progress:
            progress(message)

    selector = "|".join(LANDFORM_TYPES)
    found: list[Peak] = []
    for code in countries:
        say(f"Downloading landforms for {COUNTRY_NAMES.get(code, code)}...")
        query = (
            f'[out:json][timeout:280];'
            f'area["ISO3166-1"="{code}"][admin_level=2]->.a;'
            f'(node["natural"~"^({selector})$"]["name"](area.a);'
            f' way["natural"~"^({selector})$"]["name"](area.a););'
            f'out center;'
        )
        try:
            data = _overpass(query)
        except RuntimeError as exc:
            say(f"  {code}: failed ({exc}); skipping")
            continue

        count = 0
        for element in data.get("elements", []):
            tags = element.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name:
                continue
            centre = element.get("center") or {}
            lat = element.get("lat", centre.get("lat"))
            lon = element.get("lon", centre.get("lon"))
            if lat is None or lon is None:
                continue
            elevation = None
            raw = tags.get("ele")
            if raw:
                try:
                    elevation = int(float(str(raw).replace(",", ".").split()[0]))
                except (ValueError, IndexError):
                    elevation = None
            found.append(
                Peak(
                    name=name,
                    lat=float(lat),
                    lon=float(lon),
                    elevation=elevation,
                    country=code,
                    kind=tags.get("natural", "ridge"),
                )
            )
            count += 1
        say(f"  {code}: {count} named landforms")
        time.sleep(5)
    return found


def save_peaks(peaks: Sequence[Peak], path: Path = DEFAULT_CACHE) -> Path:
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "peaks": [p.to_dict() for p in peaks]}),
        encoding="utf-8",
    )
    return path


def load_peaks(path: Path = DEFAULT_CACHE) -> list[Peak]:
    path = Path(path).expanduser()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Could not read peak cache %s: %s", path, exc)
        return []
    return [Peak.from_dict(d) for d in data.get("peaks", [])]


# --------------------------------------------------------------------------
# Index
# --------------------------------------------------------------------------


# Where a matched peak is allowed to be.
#
# The country filter is not enough, because overseas territories carry the
# same country code as the mainland. Measured on this gazetteer: 1,616 of
# 125,572 entries (1.3%) sit outside Europe -- 224 on Reunion, 95 in French
# Polynesia, 72 in the Marquesas, 66 on the Kerguelen Islands, all coded FR.
#
# A generic French feature name then matches one of them. The event of
# 13 April 2020 was named "La Cheminee" after a hill at -49.2150, 70.0033 in
# the sub-Antarctic Indian Ocean, twelve thousand kilometres from the Alps.
#
# The northern limit is 81, not 72, deliberately: that keeps Svalbard, which
# is genuinely Norwegian and a plausible place for this library's owner to
# have been. Everything excluded is southern-hemisphere or Pacific.
EUROPE_BOUNDS = (35.0, 81.0, -25.0, 45.0)   # min_lat, max_lat, min_lon, max_lon


def within(peak, bounds) -> bool:
    """Is this peak inside the plausible box? Unknown positions pass."""
    if bounds is None or peak.lat is None or peak.lon is None:
        return True
    min_lat, max_lat, min_lon, max_lon = bounds
    return min_lat <= peak.lat <= max_lat and min_lon <= peak.lon <= max_lon


class PeakIndex:
    """Lookup over the gazetteer: by name, and by position."""

    def __init__(self, peaks: Iterable[Peak], bounds=EUROPE_BOUNDS) -> None:
        # Filtered at construction, so every lookup route is covered rather
        # than the two that were remembered.
        self.bounds = bounds
        peaks = [p for p in peaks if within(p, bounds)]
        self.peaks: list[Peak] = list(peaks)
        self._exact: dict[str, list[Peak]] = {}
        for peak in self.peaks:
            for spelling in (peak.name, *peak.aliases):
                key = normalise(spelling)
                if key:
                    self._exact.setdefault(key, []).append(peak)

    def __len__(self) -> int:
        return len(self.peaks)

    @classmethod
    def load(cls, path: Path = DEFAULT_CACHE, bounds=EUROPE_BOUNDS) -> "PeakIndex":
        return cls(load_peaks(path), bounds=bounds)

    def match(
        self,
        query: str,
        min_score: float = 0.82,
        countries: Optional[Sequence[str]] = None,
        near: Optional[tuple[float, float]] = None,
        within_km: float = 80.0,
        limit: int = 5,
    ) -> list[PeakMatch]:
        """Find real peaks whose name matches `query`.

        A name that scores below `min_score` returns nothing rather than the
        closest thing available -- the whole point is to reject inventions,
        so "no match" has to be a possible answer.
        """
        if not query or not query.strip():
            return []

        candidates = self.peaks
        if countries:
            wanted = {c.upper() for c in countries}
            candidates = [p for p in candidates if p.country in wanted]
        if near is not None:
            lat, lon = near
            candidates = [
                p for p in candidates
                if _rough_km(lat, lon, p.lat, p.lon) <= within_km
            ]

        # Exact normalised hits first: cheap and unambiguous.
        # Membership by identity: Peak is a dataclass with eq=True, so it is
        # unhashable and cannot go in a set.
        key = normalise(query)
        allowed = {id(p) for p in candidates}
        exact = (
            [p for p in self._exact.get(key, []) if id(p) in allowed] if key else []
        )
        if exact:
            return [PeakMatch(peak=p, score=1.0, matched_on=p.name) for p in exact[:limit]]

        scored: list[PeakMatch] = []
        for peak in candidates:
            best, matched = 0.0, ""
            for spelling in (peak.name, *peak.aliases):
                score = name_similarity(query, spelling)
                if score > best:
                    best, matched = score, spelling
            if best >= min_score:
                scored.append(PeakMatch(peak=peak, score=best, matched_on=matched))

        scored.sort(key=lambda m: (-m.score, -(m.peak.elevation or 0)))
        return scored[:limit]

    def near(
        self, lat: float, lon: float, radius_km: float = 15.0, limit: int = 12
    ) -> list[Peak]:
        """Prominent peaks around a point, highest first.

        This is the shortlist the specification asks for when there is no
        heading: candidates for a human to choose from, not an assertion.
        """
        close = [
            p for p in self.peaks
            if _rough_km(lat, lon, p.lat, p.lon) <= radius_km
        ]
        close.sort(key=lambda p: -(p.elevation or 0))
        return close[:limit]

    def names_in_text(
        self,
        text: str,
        countries: Optional[Sequence[str]] = None,
        min_single_word: int = 8,
        limit: int = 5,
    ) -> list[Peak]:
        """Real place names appearing verbatim in transcribed text.

        Signboards, hut plaques, bus stops and guidebook headings write the
        answer down. This finds it by walking every 1-4 word span of the
        text against the gazetteer's exact index -- no fuzzy matching, so
        the "Arco inside Parco" and "Grande Floria in Brazil" failures
        cannot recur here.

        Single words must be long to count. "Post", "Granit" and "Sektor"
        appear on signs everywhere and some of them are also somebody's
        summit; "Hannibalturm" is not an accident.
        """
        if not text or not self._exact:
            return []
        wanted = {c.upper() for c in countries} if countries else None
        words = [w for w in re.split(r"[^0-9A-Za-zÀ-ɏ]+", text) if w]
        found: list[Peak] = []
        seen: set[str] = set()
        for size in (4, 3, 2, 1):
            for i in range(len(words) - size + 1):
                span = " ".join(words[i:i + size])
                if size == 1 and len(span) < min_single_word:
                    continue
                for peak in self._exact.get(normalise(span), []):
                    if wanted and peak.country not in wanted:
                        continue
                    if peak.name in seen:
                        continue
                    seen.add(peak.name)
                    found.append(peak)
                    if len(found) >= limit:
                        return found
        return found

    def verify(self, name: str, countries: Optional[Sequence[str]] = None) -> Optional[Peak]:
        """Is this a real summit in these countries? The peak, or None."""
        matches = self.match(name, countries=countries, limit=1)
        return matches[0].peak if matches else None


def _rough_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Equirectangular distance -- fast, and accurate enough for filtering."""
    x = math.radians(lon2 - lon1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return 6371.0088 * math.hypot(x, y)
