"""Configuration: defaults, TOML loading, CLI overrides.

Thresholds live here rather than in the clustering code so they can be
tuned (R-N5) without touching logic.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # Python 3.11+
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    tomllib = None  # type: ignore[assignment]


# Extensions we ingest (R-F1). Lowercase, with dot.
DEFAULT_IMAGE_EXTENSIONS: tuple[str, ...] = (
    # Standard
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".tif", ".tiff", ".webp",
    # RAW
    ".cr2", ".cr3", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".dng", ".orf", ".rw2", ".raf", ".pef", ".raw",
)

# Directory names never descended into. Prevents re-ingesting our own output
# and picking up thumbnail caches as if they were originals.
DEFAULT_EXCLUDE_DIRS: tuple[str, ...] = (
    "_duplicates_review",
    ".thumbnails",
    ".cache",
    "__pycache__",
    "$RECYCLE.BIN",
    "System Volume Information",
    ".git",
)


def read_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file, tolerating a UTF-8 BOM.

    Notepad and PowerShell both write UTF-8 with a BOM by default, and
    tomllib rejects it outright. Since these files exist to be hand-edited
    on Windows, stripping the BOM is required rather than optional.
    """
    if tomllib is None:
        raise RuntimeError("Reading TOML requires Python 3.11+ (tomllib).")
    raw = Path(path).read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"{path} is not valid UTF-8 text: {exc}. Re-save it as UTF-8."
        ) from None
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{path} is not valid TOML: {exc}") from None


@dataclass
class PathsConfig:
    """Folders the UI starts with. Neither is read until you press a button."""

    # Prefilled in the control panel; still fully editable there.
    default_source: str = r"D:\FotosTemp"
    default_output: str = r"C:\FotosTempOrganized"


@dataclass
class ClusterConfig:
    """Event-boundary thresholds (R-F4)."""

    # Start a new event when the gap to the previous photo exceeds this.
    time_gap_hours: float = 12.0
    # ...or when the jump from the last known GPS fix exceeds this. 15 km is
    # tight enough to separate two crags in the same valley, which 50 km
    # merged into one event. It only fires on photos that HAVE a fix, and
    # this library has 41 of 13,881 -- so on the phone dump the time gap
    # does nearly all the work, and this matters for cameras that record
    # position.
    distance_km: float = 15.0
    # Ignore GPS-based splits when photos are close in time anyway; a single
    # bad fix in the middle of a hike should not shatter the event.
    min_gap_minutes_for_distance_split: float = 20.0
    # Events smaller than this are still kept, but flagged in the preview.
    small_event_threshold: int = 3


@dataclass
class GeocodeConfig:
    """Reverse-geocoding source (R-F7)."""

    # "offline"  -> reverse_geocoder package, no network
    # "nominatim"-> OpenStreetMap Nominatim HTTP API (opt-in, rate limited)
    # "none"     -> skip; every event becomes Unknown_DD_MM
    provider: str = "nominatim"
    # Only used by the nominatim provider. Nominatim's usage policy requires
    # a real contact address in the User-Agent.
    user_agent: str = "photo-organizer/0.1"
    nominatim_email: str = ""
    # Seconds between network calls; Nominatim asks for >= 1.0.
    request_interval_seconds: float = 1.1
    # Cache geocode lookups on disk so re-runs are free and offline.
    cache_path: str = "~/.cache/photo_organizer/geocode.json"
    # Round coordinates to this many decimals for cache keys.
    # 2 decimals ~= 1.1 km, plenty for a place label.
    cache_precision: int = 2


@dataclass
class ScanConfig:
    image_extensions: tuple[str, ...] = DEFAULT_IMAGE_EXTENSIONS
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDE_DIRS
    follow_symlinks: bool = False
    # Fall back to mtime when EXIF has no timestamp (R-F3).
    use_mtime_fallback: bool = True
    # Try to parse a date out of the filename before falling back to mtime;
    # phone dumps often carry IMG_20250712_083145.jpg.
    use_filename_fallback: bool = True
    # Reading metadata is seek-bound, not CPU-bound: on an external hard disk
    # each file costs ~80ms of latency and the drive sits idle in between.
    # Overlapping reads lets it reorder them. Measured on a USB HDD with
    # 14k photos: 1 worker 11/s, 8 workers 34/s, 16 workers 48/s.
    # Set to 1 to scan strictly sequentially.
    scan_workers: int = 16


@dataclass
class AnalysisConfig:
    """Photo analysis via a hosted vision model, cached in a local database.

    Replaces the local-model experiments, all of which were measured on this
    library and none of which were good enough: CLIP named "K2" for a forest
    slope, a local 3B VLM named "Mount Everest" for an Alpine ice fall, and
    GeoCLIP had a 139 km median error.
    """

    # How many photos of each event get analysed. **0 means every photo.**
    #
    # Sampling was a false economy. Each photo is analysed once in its life
    # and the full reply is cached forever, so the whole library costs a few
    # dollars once, in batch, and nothing after that. Sampling saved cents
    # and lost peaks: only ~27% of these photos show a placeable skyline, so
    # any sample misses events whose one identifiable frame was not picked.
    photos_per_event: int = 0
    # Ceiling on new photos submitted in one run, so a first run on a large
    # library cannot produce a surprise bill. 0 removes the ceiling.
    # Ceiling on NEW photos per run. 0 removes it. The whole library in one
    # batch is the cheapest way to do this, and the real spending guard is
    # the limit set on the Google billing account.
    max_photos_per_run: int = 0

    model: str = "gemini-3.6-flash"
    # Batch is half the price of interactive, with a target turnaround of 24
    # hours. Nothing in this pipeline is interactive, so batch is the default.
    use_batch: bool = True
    poll_seconds: int = 30
    max_wait_seconds: int = 24 * 3600

    # Prefer the GEMINI_API_KEY environment variable; a key in a config file
    # is a key that gets committed.
    gemini_api_key: str = ""

    # Where analyses are cached. Outside the source and the output tree, so
    # deleting the output and re-running costs nothing.
    # NOT under ~/.cache. A cache is by definition safe to delete, and this
    # file is the opposite: it is the only record of analyses that cost real
    # money, and losing it means paying for the whole library again.
    database_path: str = "~/.photo_organizer/analysis.sqlite3"

    # Check every claimed summit against the peaks gazetteer. This proves a
    # name is real; it cannot prove it is the right one.
    use_gazetteer: bool = True
    peak_countries: tuple[str, ...] = ("CH", "FR", "IT", "AT", "SK", "DE", "NO")

    # --- event location consensus ---------------------------------------
    # A single photo's estimated position is not trustworthy: measured, a
    # hosted model placed Swiss photos in California and a Sardinian trip in
    # Provence. So a position is only used when several photos of the same
    # event agree on it, and the agreed position is then applied to every
    # photo in that event rather than each photo keeping its own guess.
    #
    # Photos within this distance of each other count as agreeing.
    location_agreement_km: float = 25.0
    # How many must agree. One photo agreeing with itself is not consensus.
    location_min_agreeing: int = 2
    # And they must be this share of the photos that offered any estimate,
    # so two agreeing out of nine scattered guesses is still rejected.
    location_min_fraction: float = 0.4
    # A summit RECOGNISED from the terrain is thrown out if it sits further
    # than this from a place name READ out of the same event's photos. The
    # gazetteer cannot catch this on its own: it only knows a name exists,
    # which is how "Salbitschijen" was accepted for an event 13 km away.
    # Analyse EVERY member of a duplicate group, not just the one the file-
    # size rule picked, so the keeper is chosen on sharpness, composition and
    # whether people are looking at the camera. Measured on this library:
    # 528 groups, 627 extra photos, $0.13 at batch rates. Turning this off
    # falls back to "biggest file wins", which is not the same question.
    judge_duplicates: bool = True
    peak_contradiction_km: float = 30.0
    # A summit must reach this probability to name a folder or to have its
    # coordinates written into the files. Below it the event is named by
    # region and the summit is kept in the evidence as a suggestion.
    # At the defaults: one signboard 0.92, one guidebook page 0.65, one
    # confident terrain recognition 0.31, five agreeing recognitions 0.55.
    min_peak_probability: float = 0.5

    @property
    def api_key_resolved(self) -> str:
        import os

        return self.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")


@dataclass
class NamingConfig:
    # Folder name pattern. Available fields: place, dd, mm, yyyy, index.
    pattern: str = "{place}_{dd}_{mm}"
    unknown_place_label: str = "Unknown"
    max_name_length: int = 60
    # Enriched names are assembled from these parts, in this order, taking
    # the first two that exist. Region before place because the user rated
    # region as more useful than the specific route.
    include_region: bool = True
    include_country: bool = False
    # The activity goes in every folder name, not only the ones with no
    # place. Browsing the library by what was done -- ski touring, ice
    # climbing -- is the point, and a name that drops it for the events
    # that DID get identified loses it exactly where it is most useful.
    include_activity: bool = True


# Where a config file is looked for when none is named on the command line,
# in order. The working directory first, so a project folder can carry its
# own settings; the home directory last, as the per-machine fallback.
log = logging.getLogger(__name__)

CONFIG_NAMES = ("config.toml", "photo_organizer.toml")


def find_config() -> Optional[Path]:
    """The config file to use when none was named, or None."""
    roots = [
        Path.cwd(),
        Path(__file__).resolve().parent.parent,
        Path("~/.photo_organizer").expanduser(),
    ]
    seen: set[Path] = set()
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        for name in CONFIG_NAMES:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


@dataclass
class Config:
    paths: PathsConfig = field(default_factory=PathsConfig)
    cluster: ClusterConfig = field(default_factory=ClusterConfig)
    analysis: AnalysisConfig = field(default_factory=AnalysisConfig)
    geocode: GeocodeConfig = field(default_factory=GeocodeConfig)
    scan: ScanConfig = field(default_factory=ScanConfig)
    naming: NamingConfig = field(default_factory=NamingConfig)

    # Set by load() so the UI can say which file is in effect. Empty
    # means "built-in defaults only".
    loaded_from: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        """Build a Config, overlaying a TOML file onto the defaults.

        With no path given, look for one in the obvious places rather than
        silently ignoring a config.toml sitting next to the program. Passing
        a path explicitly always wins.
        """
        cfg = cls()
        if path is None:
            path = find_config()
        if path is None:
            return cfg
        log.info("Using configuration from %s", path)
        cfg.apply_overrides(read_toml(path))
        cfg.loaded_from = str(path)
        return cfg

    def apply_overrides(self, data: dict[str, Any]) -> None:
        """Overlay a nested dict onto this config, ignoring unknown keys.

        Unknown keys are reported rather than silently dropped, so a typo in
        a config file does not quietly leave a threshold at its default.
        """
        unknown: list[str] = []
        for section_name, values in data.items():
            section = getattr(self, section_name, None)
            if section is None or not dataclasses.is_dataclass(section):
                unknown.append(section_name)
                continue
            valid = {f.name: f for f in dataclasses.fields(section)}
            for key, value in (values or {}).items():
                if key not in valid:
                    unknown.append(f"{section_name}.{key}")
                    continue
                # TOML gives lists where we hold tuples.
                if isinstance(getattr(section, key), tuple) and isinstance(value, list):
                    value = tuple(value)
                setattr(section, key, value)
        if unknown:
            raise ValueError(
                "Unknown config key(s): " + ", ".join(sorted(unknown))
            )
