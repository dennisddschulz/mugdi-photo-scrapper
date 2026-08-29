"""Read capture metadata from image files (R-F2, R-F3).

Every file here is opened in binary READ mode only. Nothing in this module
writes, renames, or deletes anything -- that is what makes it safe to point
at a read-only source (R-S2).

Backends are optional and probed lazily:

  exifread  -- JPEG and TIFF-based RAW (CR2, NEF, ARW, DNG, ORF, ...)
  Pillow    -- JPEG, PNG, TIFF, WebP
  pillow-heif -- HEIC/HEIF (iPhone default)

If none are installed the scan still runs, falling back to filename and
mtime timestamps, and the preview says so loudly.
"""

from __future__ import annotations

import io
import logging
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .models import Photo, TimestampSource

log = logging.getLogger(__name__)

# Extensions exifread handles well (JPEG + TIFF-container RAW).
_EXIFREAD_EXTS = {
    ".jpg", ".jpeg", ".tif", ".tiff",
    ".cr2", ".nef", ".nrw", ".arw", ".srf", ".sr2",
    ".dng", ".orf", ".rw2", ".raf", ".pef", ".raw",
}
_PILLOW_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".webp"}
_HEIF_EXTS = {".heic", ".heif"}

# Matches IMG_20250712_083145, 2025-07-12 08.31.45, PXL_20250712_063145123, ...
_FILENAME_DATE_RE = re.compile(
    r"(?P<y>19\d{2}|20\d{2})[-_.]?(?P<mo>0[1-9]|1[0-2])[-_.]?(?P<d>0[1-9]|[12]\d|3[01])"
    r"(?:[-_.T ]?(?P<h>[01]\d|2[0-3])[-_.:]?(?P<mi>[0-5]\d)(?:[-_.:]?(?P<s>[0-5]\d))?)?"
)

_EXIF_DT_FORMATS = ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M")


class _Backends:
    """Lazily-probed optional dependencies, resolved once per process.

    The probing is lock-guarded because the scanner reads files from a
    thread pool. Marking a backend "probed" before its import finished let
    other threads see None and silently skip EXIF entirely -- producing a
    scan that looks successful but has thrown the metadata away.
    """

    def __init__(self) -> None:
        self._exifread: Any = None
        self._pillow: Any = None
        self._heif_ready: Optional[bool] = None
        self._probed_exifread = False
        self._probed_pillow = False
        self._lock = threading.RLock()

    @property
    def exifread(self) -> Any:
        with self._lock:
            if not self._probed_exifread:
                try:
                    import exifread  # type: ignore

                    self._exifread = exifread
                except ImportError:
                    log.debug("exifread not installed")
                finally:
                    self._probed_exifread = True
            return self._exifread

    @property
    def pillow(self) -> Any:
        with self._lock:
            if not self._probed_pillow:
                try:
                    from PIL import Image  # type: ignore

                    self._pillow = Image
                except ImportError:
                    log.debug("Pillow not installed")
                finally:
                    self._probed_pillow = True
            return self._pillow

    @property
    def heif(self) -> bool:
        """True once the HEIF opener is registered with Pillow."""
        with self._lock:
            if self._heif_ready is None:
                self._heif_ready = False
                if self.pillow is not None:
                    try:
                        import pillow_heif  # type: ignore

                        pillow_heif.register_heif_opener()
                        self._heif_ready = True
                    except ImportError:
                        log.debug("pillow-heif not installed")
            return self._heif_ready

    def warm_up(self) -> None:
        """Resolve every backend up front, before any worker threads start."""
        _ = self.exifread, self.pillow, self.heif

    def availability(self) -> dict[str, bool]:
        return {
            "exifread": self.exifread is not None,
            "pillow": self.pillow is not None,
            "pillow-heif": self.heif,
        }


BACKENDS = _Backends()


def backend_availability() -> dict[str, bool]:
    return BACKENDS.availability()


# --------------------------------------------------------------------------
# Value coercion helpers
# --------------------------------------------------------------------------


def _to_float(value: Any) -> Optional[float]:
    """Coerce EXIF rationals / bytes / strings to float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    # exifread Ratio and PIL IFDRational both expose num/den.
    num = getattr(value, "num", None)
    den = getattr(value, "den", None)
    if num is not None and den:
        return float(num) / float(den)
    if isinstance(value, (list, tuple)) and len(value) == 2:
        try:
            return float(value[0]) / float(value[1])
        except (TypeError, ZeroDivisionError, ValueError):
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dms_to_degrees(parts: Any, ref: Optional[str]) -> Optional[float]:
    """Convert (degrees, minutes, seconds) + hemisphere ref to signed float."""
    if parts is None:
        return None
    if not isinstance(parts, (list, tuple)):
        return None
    values = [_to_float(p) for p in parts[:3]]
    while len(values) < 3:
        values.append(0.0)
    if values[0] is None:
        return None
    deg = values[0] + (values[1] or 0.0) / 60.0 + (values[2] or 0.0) / 3600.0
    if ref and str(ref).strip().upper()[:1] in ("S", "W"):
        deg = -deg
    # Reject impossible fixes rather than clustering on garbage.
    if not (-180.0 <= deg <= 180.0):
        return None
    return deg


def _parse_exif_datetime(raw: Any) -> Optional[datetime]:
    if raw is None:
        return None
    text = str(raw).strip().strip("\x00")
    if not text or text.startswith("0000"):
        return None
    for fmt in _EXIF_DT_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def parse_filename_datetime(name: str) -> Optional[datetime]:
    """Best-effort date from a filename like IMG_20250712_083145.jpg."""
    match = _FILENAME_DATE_RE.search(name)
    if not match:
        return None
    g = match.groupdict()
    try:
        return datetime(
            int(g["y"]),
            int(g["mo"]),
            int(g["d"]),
            int(g["h"] or 0),
            int(g["mi"] or 0),
            int(g["s"] or 0),
        )
    except ValueError:
        return None


def _clean_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().strip("\x00").strip()
    return text or None


# --------------------------------------------------------------------------
# Backend readers -- each returns a flat dict of normalized fields
# --------------------------------------------------------------------------


# A JPEG's EXIF lives in the APP1 segment right after the header, so reading
# a bounded prefix avoids pulling multi-megabyte images off a slow external
# drive. TIFF-based RAW files can place EXIF offsets much deeper, so those
# are read in full.
_HEADER_BYTES = 256 * 1024
_PREFIX_SAFE_EXTS = {".jpg", ".jpeg"}


def _read_with_exifread(path: Path) -> dict[str, Any]:
    exifread = BACKENDS.exifread
    if exifread is None:
        return {}

    if path.suffix.lower() in _PREFIX_SAFE_EXTS:
        # One sequential read beats letting the library seek around the file:
        # on a USB hard disk the seeks dominate everything else.
        with open(path, "rb") as fh:
            head = fh.read(_HEADER_BYTES)
        tags = exifread.process_file(
            io.BytesIO(head), details=False, strict=False
        )
        if not tags:
            # Unusual layout: fall back to the whole file before giving up.
            with open(path, "rb") as fh:
                tags = exifread.process_file(fh, details=False, strict=False)
    else:
        with open(path, "rb") as fh:
            tags = exifread.process_file(fh, details=False, strict=False)

    if not tags:
        return {}

    def val(key: str) -> Any:
        tag = tags.get(key)
        return tag.values if tag is not None else None

    def first(key: str) -> Any:
        v = val(key)
        if isinstance(v, (list, tuple)):
            return v[0] if v else None
        return v

    out: dict[str, Any] = {}
    dt = _parse_exif_datetime(val("EXIF DateTimeOriginal")) or _parse_exif_datetime(
        val("EXIF DateTimeDigitized")
    ) or _parse_exif_datetime(val("Image DateTime"))
    if dt:
        out["timestamp"] = dt

    lat = _dms_to_degrees(val("GPS GPSLatitude"), _clean_text(val("GPS GPSLatitudeRef")))
    lon = _dms_to_degrees(val("GPS GPSLongitude"), _clean_text(val("GPS GPSLongitudeRef")))
    if lat is not None and lon is not None and -90.0 <= lat <= 90.0:
        out["lat"], out["lon"] = lat, lon

    alt = _to_float(first("GPS GPSAltitude"))
    if alt is not None:
        # AltitudeRef 1 means below sea level.
        ref = _to_float(first("GPS GPSAltitudeRef"))
        out["altitude"] = -alt if ref == 1 else alt

    heading = _to_float(first("GPS GPSImgDirection"))
    if heading is not None and 0.0 <= heading <= 360.0:
        out["heading"] = heading

    make = _clean_text(val("Image Make"))
    model = _clean_text(val("Image Model"))
    if make:
        out["camera_make"] = make
    if model:
        out["camera_model"] = model

    width = _to_float(first("EXIF ExifImageWidth"))
    height = _to_float(first("EXIF ExifImageLength"))
    if width:
        out["width"] = int(width)
    if height:
        out["height"] = int(height)
    return out


# EXIF/GPS numeric tag ids, so we do not depend on Pillow's name tables.
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_DATETIME = 0x0132
_TAG_MAKE = 0x010F
_TAG_MODEL = 0x0110
_TAG_PIXEL_X = 0xA002
_TAG_PIXEL_Y = 0xA003
_GPS_LAT_REF, _GPS_LAT = 1, 2
_GPS_LON_REF, _GPS_LON = 3, 4
_GPS_ALT_REF, _GPS_ALT = 5, 6
_GPS_IMG_DIR = 17


def _read_with_pillow(path: Path) -> dict[str, Any]:
    Image = BACKENDS.pillow
    if Image is None:
        return {}
    out: dict[str, Any] = {}
    with Image.open(path) as img:
        out["width"], out["height"] = img.size
        try:
            exif = img.getexif()
        except Exception:
            exif = None
        if not exif:
            return out

        dt = (
            _parse_exif_datetime(exif.get(_TAG_DATETIME_ORIGINAL))
            or _parse_exif_datetime(exif.get(_TAG_DATETIME_DIGITIZED))
            or _parse_exif_datetime(exif.get(_TAG_DATETIME))
        )
        # DateTimeOriginal usually lives in the Exif sub-IFD, not the root.
        if dt is None:
            try:
                sub = exif.get_ifd(0x8769)
            except Exception:
                sub = None
            if sub:
                dt = _parse_exif_datetime(
                    sub.get(_TAG_DATETIME_ORIGINAL)
                ) or _parse_exif_datetime(sub.get(_TAG_DATETIME_DIGITIZED))
                if not out.get("width"):
                    out["width"] = sub.get(_TAG_PIXEL_X)
                    out["height"] = sub.get(_TAG_PIXEL_Y)
        if dt:
            out["timestamp"] = dt

        make = _clean_text(exif.get(_TAG_MAKE))
        model = _clean_text(exif.get(_TAG_MODEL))
        if make:
            out["camera_make"] = make
        if model:
            out["camera_model"] = model

        try:
            gps = exif.get_ifd(0x8825)
        except Exception:
            gps = None
        if gps:
            lat = _dms_to_degrees(gps.get(_GPS_LAT), _clean_text(gps.get(_GPS_LAT_REF)))
            lon = _dms_to_degrees(gps.get(_GPS_LON), _clean_text(gps.get(_GPS_LON_REF)))
            if lat is not None and lon is not None and -90.0 <= lat <= 90.0:
                out["lat"], out["lon"] = lat, lon
            alt = _to_float(gps.get(_GPS_ALT))
            if alt is not None:
                ref = _to_float(gps.get(_GPS_ALT_REF))
                out["altitude"] = -alt if ref == 1 else alt
            heading = _to_float(gps.get(_GPS_IMG_DIR))
            if heading is not None and 0.0 <= heading <= 360.0:
                out["heading"] = heading
    return out


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def read_photo(
    path: Path,
    use_filename_fallback: bool = True,
    use_mtime_fallback: bool = True,
    stat_result: Any = None,
) -> Photo:
    """Read one image into a Photo. Never raises for a single bad file.

    `stat_result` lets the scanner pass the stat it already has from the
    directory walk, avoiding a second syscall per file.
    """
    photo = Photo(source_path=path)
    ext = path.suffix.lower()

    if stat_result is None:
        try:
            stat_result = path.stat()
        except OSError as exc:
            photo.warnings.append(f"stat failed: {exc}")
    if stat_result is not None:
        photo.size_bytes = stat_result.st_size

    data: dict[str, Any] = {}

    # Order matters: exifread understands RAW containers Pillow cannot open.
    if ext in _EXIFREAD_EXTS and BACKENDS.exifread is not None:
        try:
            data = _read_with_exifread(path)
        except Exception as exc:
            photo.warnings.append(f"exifread failed: {exc}")

    # Only re-read the file if we still have no timestamp. A photo simply
    # having no GPS is normal, and treating that as "try harder" meant every
    # GPS-less file was opened and parsed twice -- which on an external disk
    # cost far more than the metadata was worth.
    needs_more = not data.get("timestamp")
    pillow_usable = ext in _PILLOW_EXTS or (ext in _HEIF_EXTS and BACKENDS.heif)
    if needs_more and pillow_usable and BACKENDS.pillow is not None:
        try:
            fallback = _read_with_pillow(path)
        except Exception as exc:
            photo.warnings.append(f"pillow failed: {exc}")
        else:
            # Fill gaps without overwriting what exifread already resolved.
            for key, value in fallback.items():
                if value is not None and data.get(key) is None:
                    data[key] = value

    if not data and ext in _HEIF_EXTS and not BACKENDS.heif:
        photo.warnings.append("no HEIC backend (install pillow-heif)")

    photo.lat = data.get("lat")
    photo.lon = data.get("lon")
    photo.altitude = data.get("altitude")
    photo.heading = data.get("heading")
    photo.camera_make = data.get("camera_make")
    photo.camera_model = data.get("camera_model")
    photo.width = data.get("width")
    photo.height = data.get("height")

    timestamp = data.get("timestamp")
    if timestamp:
        photo.timestamp = timestamp
        photo.timestamp_source = TimestampSource.EXIF
        return photo

    if use_filename_fallback:
        guess = parse_filename_datetime(path.name)
        if guess:
            photo.timestamp = guess
            photo.timestamp_source = TimestampSource.FILENAME
            photo.warnings.append("timestamp from filename, not EXIF")
            return photo

    if use_mtime_fallback:
        try:
            mtime = (
                stat_result.st_mtime
                if stat_result is not None
                else os.path.getmtime(path)
            )
        except OSError as exc:
            photo.warnings.append(f"mtime unavailable: {exc}")
        else:
            photo.timestamp = datetime.fromtimestamp(mtime)
            photo.timestamp_source = TimestampSource.MTIME
            photo.warnings.append("timestamp from file mtime, not EXIF")
            return photo

    photo.warnings.append("no timestamp available")
    return photo
