"""Geographic helpers: distance, centroids, reverse geocoding.

Reverse geocoding is pluggable (R-F7). The default provider is fully
offline; the network provider is opt-in and never contacted unless the
config selects it.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Optional, Sequence

from .config import GeocodeConfig

log = logging.getLogger(__name__)

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres between two WGS84 points."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def centroid(points: Sequence[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """Spherical mean of lat/lon points.

    Averaging degrees directly breaks across the antimeridian, so average
    the 3D unit vectors instead.
    """
    if not points:
        return None
    x = y = z = 0.0
    for lat, lon in points:
        rlat, rlon = math.radians(lat), math.radians(lon)
        clat = math.cos(rlat)
        x += clat * math.cos(rlon)
        y += clat * math.sin(rlon)
        z += math.sin(rlat)
    n = len(points)
    x, y, z = x / n, y / n, z / n
    if abs(x) < 1e-12 and abs(y) < 1e-12 and abs(z) < 1e-12:
        return points[0]
    lon_c = math.atan2(y, x)
    lat_c = math.atan2(z, math.hypot(x, y))
    return (math.degrees(lat_c), math.degrees(lon_c))


def medoid(points: Sequence[tuple[float, float]]) -> Optional[tuple[float, float]]:
    """The actual point with the smallest total distance to all others.

    Preferred over the centroid for naming: the centroid of a valley walk
    and a summit can land on a spot the user was never at, whereas the
    medoid is always a real photo location.
    """
    if not points:
        return None
    if len(points) <= 2:
        return points[0]
    best, best_total = points[0], float("inf")
    for candidate in points:
        total = sum(
            haversine_km(candidate[0], candidate[1], other[0], other[1])
            for other in points
        )
        if total < best_total:
            best, best_total = candidate, total
    return best


def bbox_span_km(points: Sequence[tuple[float, float]]) -> float:
    """Rough diagonal extent of a set of points, for preview context."""
    if len(points) < 2:
        return 0.0
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    return haversine_km(min(lats), min(lons), max(lats), max(lons))


# --------------------------------------------------------------------------
# Reverse geocoding
# --------------------------------------------------------------------------


class GeocodeResult:
    __slots__ = ("name", "admin", "country", "provider")

    def __init__(
        self,
        name: str,
        admin: str = "",
        country: str = "",
        provider: str = "",
    ) -> None:
        self.name = name
        self.admin = admin
        self.country = country
        self.provider = provider

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "admin": self.admin,
            "country": self.country,
            "provider": self.provider,
        }

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> "GeocodeResult":
        return cls(
            d.get("name", ""),
            d.get("admin", ""),
            d.get("country", ""),
            d.get("provider", ""),
        )


class Geocoder:
    """Reverse-geocodes coordinates to a place label, with a disk cache.

    The cache is keyed on rounded coordinates, so a whole event's photos
    (and repeat runs) collapse to at most one lookup.
    """

    def __init__(self, config: GeocodeConfig) -> None:
        self.config = config
        self._cache: dict[str, dict[str, str]] = {}
        self._cache_path: Optional[Path] = None
        self._cache_dirty = False
        self._last_request = 0.0
        self._offline_db = None
        self._offline_failed = False
        self._load_cache()

    # -- cache ------------------------------------------------------------

    def _load_cache(self) -> None:
        raw = self.config.cache_path
        if not raw:
            return
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        self._cache_path = path
        if path.exists():
            try:
                self._cache = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                log.warning("Could not read geocode cache %s: %s", path, exc)
                self._cache = {}

    def save_cache(self) -> None:
        if not self._cache_dirty or self._cache_path is None:
            return
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps(self._cache, indent=1, ensure_ascii=False),
                encoding="utf-8",
            )
            self._cache_dirty = False
        except OSError as exc:
            log.warning("Could not write geocode cache: %s", exc)

    def _key(self, lat: float, lon: float) -> str:
        # The provider is part of the key: offline and Nominatim give
        # different names for the same spot, so a cached offline answer must
        # not be served after the user switches to Nominatim.
        p = self.config.cache_precision
        return f"{self.config.provider}:{lat:.{p}f},{lon:.{p}f}"

    # -- public API -------------------------------------------------------

    def lookup(self, lat: float, lon: float) -> Optional[GeocodeResult]:
        if self.config.provider == "none":
            return None
        key = self._key(lat, lon)
        if key in self._cache:
            cached = self._cache[key]
            return GeocodeResult.from_dict(cached) if cached else None

        if self.config.provider == "offline":
            result = self._lookup_offline(lat, lon)
        elif self.config.provider == "nominatim":
            result = self._lookup_nominatim(lat, lon)
        else:
            raise ValueError(f"Unknown geocode provider: {self.config.provider!r}")

        self._cache[key] = result.to_dict() if result else {}
        self._cache_dirty = True
        return result

    # -- providers --------------------------------------------------------

    def _lookup_offline(self, lat: float, lon: float) -> Optional[GeocodeResult]:
        """Nearest populated place from the bundled GeoNames dataset."""
        if self._offline_failed:
            return None
        if self._offline_db is None:
            try:
                import reverse_geocoder  # type: ignore
            except ImportError:
                log.warning(
                    "Offline geocoding needs the reverse_geocoder package; "
                    "place names will be unavailable. Install it, or set "
                    "provider to none in the [geocode] config section."
                )
                self._offline_failed = True
                return None
            self._offline_db = reverse_geocoder
        try:
            hits = self._offline_db.search((lat, lon), mode=1, verbose=False)
        except Exception as exc:  # the library raises assorted error types
            log.warning("Offline geocode failed for %s,%s: %s", lat, lon, exc)
            return None
        if not hits:
            return None
        hit = hits[0]
        return GeocodeResult(
            name=hit.get("name", ""),
            admin=hit.get("admin1", ""),
            country=hit.get("cc", ""),
            provider="offline",
        )

    def _lookup_nominatim(self, lat: float, lon: float) -> Optional[GeocodeResult]:
        """OpenStreetMap Nominatim. Only reached when explicitly configured."""
        import urllib.error
        import urllib.parse
        import urllib.request

        # Respect the published rate limit.
        elapsed = time.monotonic() - self._last_request
        wait = self.config.request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

        params = {
            "lat": f"{lat:.5f}",
            "lon": f"{lon:.5f}",
            "format": "jsonv2",
            "zoom": "12",
            "addressdetails": "1",
        }
        if self.config.nominatim_email:
            params["email"] = self.config.nominatim_email
        url = (
            "https://nominatim.openstreetmap.org/reverse?"
            + urllib.parse.urlencode(params)
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": self.config.user_agent}
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning("Nominatim lookup failed for %s,%s: %s", lat, lon, exc)
            return None
        finally:
            self._last_request = time.monotonic()

        addr = data.get("address") or {}
        # Prefer the most specific settlement-ish field available.
        for field_name in (
            "village",
            "hamlet",
            "town",
            "city",
            "municipality",
            "suburb",
            "county",
            "state",
        ):
            if addr.get(field_name):
                return GeocodeResult(
                    name=addr[field_name],
                    admin=addr.get("state", ""),
                    country=(addr.get("country_code") or "").upper(),
                    provider="nominatim",
                )
        if data.get("name"):
            return GeocodeResult(name=data["name"], provider="nominatim")
        return None

    # -- forward geocoding ------------------------------------------------

    def locate_place(self, name: str, hint: str = "") -> Optional[dict]:
        """Turn a place name into coordinates plus its admin hierarchy.

        This is the inverse of the usual lookup, and it is how a library with
        no GPS still gets a region: a mountain name read off a guidebook page
        resolves to a country, a state and coordinates. Results are cached,
        and the Nominatim rate limit is respected.
        """
        import urllib.error
        import urllib.parse
        import urllib.request

        query = f"{name} {hint}".strip()
        if not query:
            return None
        key = f"fwd:{query.lower()}"
        if key in self._cache:
            return self._cache[key] or None

        elapsed = time.monotonic() - self._last_request
        wait = self.config.request_interval_seconds - elapsed
        if wait > 0:
            time.sleep(wait)

        params = {
            "q": query,
            "format": "jsonv2",
            "addressdetails": "1",
            "limit": "5",
            "accept-language": "en",
        }
        if self.config.nominatim_email:
            params["email"] = self.config.nominatim_email
        url = (
            "https://nominatim.openstreetmap.org/search?"
            + urllib.parse.urlencode(params)
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": self.config.user_agent}
        )
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                results = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            log.warning("Forward geocode failed for %r: %s", query, exc)
            return None
        finally:
            self._last_request = time.monotonic()

        if not results:
            self._cache[key] = {}
            self._cache_dirty = True
            return None

        # Only natural features count. Nominatim will happily fuzzy-match a
        # summit name onto a residential street on another continent -- asked
        # for "Grande Floria" it returned a street in Brazil. A wrong country
        # in a folder name is far worse than no name, so anything that is not
        # a mountain-like feature is rejected outright rather than ranked.
        acceptable_types = {
            "peak", "massif", "ridge", "arete", "volcano", "saddle", "col",
            "cliff", "glacier", "valley", "mountain_range", "rock", "range",
            # Huts are precise, well-mapped points in the right massif, and
            # their names survive OCR better than display-font summit names.
            "alpine_hut", "wilderness_hut", "shelter",
        }
        candidates = [
            entry
            for entry in results
            if entry.get("type") in acceptable_types
            or (
                entry.get("class") in ("natural", "place")
                and entry.get("type") in acceptable_types
            )
        ]
        if not candidates:
            log.info(
                "No natural feature matched %r (best was %r); refusing to guess.",
                query,
                results[0].get("display_name", "")[:60],
            )
            self._cache[key] = {}
            self._cache_dirty = True
            return None

        def rank(entry: dict) -> int:
            order = {"peak": 0, "massif": 1, "mountain_range": 1, "range": 1}
            return order.get(entry.get("type", ""), 2)

        best = sorted(candidates, key=rank)[0]
        addr = best.get("address") or {}
        found = {
            "name": best.get("name") or query,
            "lat": float(best["lat"]),
            "lon": float(best["lon"]),
            "type": best.get("type", ""),
            "category": best.get("class", ""),
            "country": addr.get("country", ""),
            "country_code": (addr.get("country_code") or "").upper(),
            "state": addr.get("state", "") or addr.get("region", ""),
            "county": addr.get("county", "") or addr.get("district", ""),
            "municipality": (
                addr.get("municipality")
                or addr.get("city")
                or addr.get("town")
                or addr.get("village")
                or ""
            ),
            "display_name": best.get("display_name", ""),
        }
        self._cache[key] = found
        self._cache_dirty = True
        return found


# Administrative region from coordinates, offline and instant.
#
# This exists because of a sequencing accident: geocoding runs BEFORE naming,
# when only 1 of 379 events has any GPS at all, so nothing gets a region. By
# the time peaks are matched, fifty-odd events have coordinates and nothing
# looks again. Measured on real peaks, the offline database answers exactly
# what is wanted, with no network and no rate limit:
#
#     Dammazwillinge  46.6226, 8.4315  ->  Uri, CH
#     Salbitschijen   46.6806, 8.5298  ->  Uri, CH
#     Aiguille Dibona 44.9632, 6.2429  ->  Rhone-Alpes, FR
#
# It gives the CANTON, not the area. Furka, Grimsel and Chamonix are not
# available from any source here and are not invented: measured, the
# gazetteer has no Furkapass or Grimselpass, and Nominatim answers Realp,
# Guttannen and -- for the Mont Blanc summit, which straddles the border --
# Courmayeur, on the Italian side of a French trip.
def fill_admin_regions(events) -> int:
    """Set `region` and `country_code` on every event that has coordinates.

    Uses the enriched position -- the one worked out from peaks and photo
    consensus -- not EXIF GPS, which this library barely has.

    Never overwrites a mountain_range: a massif is a better folder name than
    a canton, and the name builder already prefers it.
    """
    try:
        import reverse_geocoder
    except ImportError:
        log.debug("reverse_geocoder is not installed; regions left blank")
        return 0

    pending = []
    for event in events:
        lat = getattr(event, "enriched_lat", None)
        lon = getattr(event, "enriched_lon", None)
        if lat is None or lon is None:
            continue
        # OVERWRITE, not fill. The region used to be whatever the model
        # said while the peak came from a page, so the two could disagree
        # inside one folder name: measured on a real run,
        # IT_Aosta-Valley_Haute-Montagne named a peak in Lorraine,
        # IT_Rhone-Alpes_Col-de-la-Fourche put a French col in Italy,
        # FR_Valais_Cerisier used a Swiss canton for a French crag, and
        # FR_Haute-Savoie_Aiguille-Dibona put the Ecrins in Chamonix.
        # Deriving the region from the SAME position the peak came from
        # makes the two consistent by construction.
        pending.append((event, (float(lat), float(lon))))
    if not pending:
        return 0

    try:
        # mode=1 is the single-threaded lookup. The default spawns a process
        # pool, which is a poor trade for a few hundred points and misbehaves
        # when the caller is already inside a worker thread.
        hits = reverse_geocoder.search([point for _e, point in pending], mode=1)
    except Exception as exc:
        # Never fatal: a missing canton is a worse folder name, not a
        # broken run.
        log.warning("Offline reverse geocoding failed: %s", exc)
        return 0

    filled = 0
    for (event, _point), hit in zip(pending, hits):
        admin = (hit.get("admin1") or "").strip()
        country = (hit.get("cc") or "").strip()
        if admin:
            event.region = admin
        if country:
            event.country_code = country
        # A claimed massif that contradicts the position is worse than no
        # massif, and the name builder falls back to it when there is no
        # region at all.
        if admin:
            event.mountain_range = None
        if admin or country:
            filled += 1
    return filled
