"""The local control panel: browse folders, run pipeline steps, review results.

Runs on demand, on the loopback interface, and stops when you close it. It is
a local batch tool with a browser front end, not a service: nothing is
installed, nothing autostarts, and closing the page's Quit button ends the
process.

Safety, unchanged from the rest of the tool:

* Source files are opened read-only. The only writes are the edits file and
  the manifest, both through `guard_write_target`.
* Photos are addressed by integer index into the current plan, never by
  path, so no URL can name a file that is not already in the plan.
* The folder browser returns directory names only -- never file contents.
* Loopback bind plus a per-session token on every route.
"""

from __future__ import annotations

import io
import json
import logging
import os
import secrets
import socket
import string
import threading
import traceback
import webbrowser
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .config import Config
from .copier import CopyCancelled
from .exif import BACKENDS, backend_availability
from .geo import bbox_span_km, medoid
from .manifest import save_manifest, write_edits_file
from .models import Photo, Plan
from .planner import build_plan
from .scan import Cancelled, UnsafePathError, _resolve, check_paths, survey_source

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_PORT = 8080

THUMB_SIZE = 260
FULL_SIZE = 1600
THUMB_CACHE_ENTRIES = 600

# Pipeline stages shown in the UI. Milestones 2-4 are declared but disabled,
# so the roadmap is visible rather than mysteriously absent.
STEPS = [
    {"id": "scan", "label": "Scan", "detail": "Read EXIF, GPS and timestamps", "ready": True},
    {"id": "cluster", "label": "Cluster", "detail": "Group photos into events", "ready": True},
    {"id": "name", "label": "Name", "detail": "Propose names from GPS", "ready": True},
    {"id": "plan", "label": "Plan", "detail": "Work out destination paths", "ready": True},
    {
        "id": "dupes",
        "label": "Duplicates",
        "detail": "Find exact and near-duplicates — marked, never deleted",
        "ready": True,
        # Part of the main run: it writes nothing, and skipping it means
        # paying to analyse every frame of a burst.
        "separate": False,
    },
    {
        "id": "analyze",
        "label": "Identify",
        "detail": "Analyse photos with Gemini (batch, half price) and name events",
        "ready": True,
        "separate": True,
    },
    {
        "id": "copy",
        "label": "Copy",
        "detail": "Copy into the library and write tags into the copies",
        "ready": True,
        "separate": True,
        "destructive": False,
    },
]


# --------------------------------------------------------------------------
# Thumbnails
# --------------------------------------------------------------------------


class ThumbnailRenderer:
    """Decodes source images to bounded JPEGs, with a small LRU cache."""

    def __init__(self) -> None:
        self._cache: OrderedDict[tuple[str, int], bytes] = OrderedDict()
        self._lock = threading.Lock()
        self.available = BACKENDS.pillow is not None
        self.heif = BACKENDS.heif

    def render(self, photo: Photo, max_size: int) -> Optional[bytes]:
        key = (str(photo.source_path), max_size)
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                return hit

        data = self._render_uncached(photo, max_size)
        if data is None:
            return None
        with self._lock:
            self._cache[key] = data
            self._cache.move_to_end(key)
            while len(self._cache) > THUMB_CACHE_ENTRIES:
                self._cache.popitem(last=False)
        return data

    def _render_uncached(self, photo: Photo, max_size: int) -> Optional[bytes]:
        Image = BACKENDS.pillow
        if Image is None:
            return None
        try:
            from PIL import ImageOps

            with Image.open(photo.source_path) as img:
                # draft() decodes JPEGs at reduced resolution -- far cheaper
                # than a full decode when we only want a thumbnail.
                try:
                    img.draft("RGB", (max_size * 2, max_size * 2))
                except (AttributeError, ValueError):
                    pass
                img = ImageOps.exif_transpose(img) or img
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                img.thumbnail((max_size, max_size), Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=82, optimize=True)
                return buf.getvalue()
        except Exception as exc:
            log.debug("Could not render %s: %s", photo.source_path, exc)
            return None


# --------------------------------------------------------------------------
# Background job
# --------------------------------------------------------------------------


@dataclass
class Job:
    """One pipeline run, executing on a worker thread."""

    name: str
    status: str = "running"  # running | done | error | cancelled
    step: str = ""
    detail: str = ""
    done_count: int = 0
    total: int = 0
    current: str = ""
    warnings: int = 0
    skipped: int = 0
    started: datetime = field(default_factory=datetime.now)
    finished: Optional[datetime] = None
    error: Optional[str] = None
    # Generous: a full run over 14,000 photos emits several thousand lines
    # across scan, duplicates, analysis and copy, and the beginning is
    # exactly what someone goes looking for after a failure. The complete
    # record is on disk regardless -- this is only what the page can show.
    log: deque = field(default_factory=lambda: deque(maxlen=4000))
    completed_steps: list[str] = field(default_factory=list)
    _cancel: threading.Event = field(default_factory=threading.Event)

    def say(self, message: str) -> None:
        self.log.append(f"{datetime.now():%H:%M:%S}  {message}")
        log.info("%s", message)

    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def to_dict(self) -> dict[str, Any]:
        elapsed = ((self.finished or datetime.now()) - self.started).total_seconds()
        rate = self.done_count / elapsed if elapsed > 0.5 and self.done_count else 0.0
        remaining = max(0, self.total - self.done_count)
        # Only offer an ETA once the rate has settled; an estimate from the
        # first few cached files is worse than no estimate at all.
        eta = remaining / rate if rate > 0 and self.done_count >= 200 else None
        return {
            "name": self.name,
            "status": self.status,
            "step": self.step,
            "detail": self.detail,
            "done_count": self.done_count,
            "total": self.total,
            "current": self.current,
            "warnings": self.warnings,
            "skipped": self.skipped,
            "elapsed": elapsed,
            "rate": rate,
            "eta": eta,
            "error": self.error,
            "log": list(self.log),
            "completed_steps": list(self.completed_steps),
        }


class AppState:
    """Everything the running app holds. Guarded by a lock for the job."""

    def __init__(self, config: Config, edits_path: Path) -> None:
        self.config = config
        self.default_edits_path = edits_path
        # Prefilled into the UI's text boxes only. Nothing is read from these
        # until the user presses "Use these folders", so a missing or
        # unplugged drive costs nothing at startup.
        self.suggested_source = config.paths.default_source
        self.suggested_output = config.paths.default_output
        self.token = load_or_create_token()
        # Off by default: see the module note. The Host and Origin
        # checks are what actually protect these routes.
        self.require_token = False
        self.renderer = ThumbnailRenderer()
        self.source: Optional[Path] = None
        self.output: Optional[Path] = None
        self.plan: Optional[Plan] = None
        self.plan_built_at: Optional[datetime] = None
        self.job: Optional[Job] = None
        self.survey: Optional[dict] = None
        self.photos_by_id: list[Photo] = []
        self.last_saved: Optional[str] = None
        self.duplicate_groups: list = []
        # Live subscribers, one queue each. A slow or vanished reader must
        # never block the pipeline, so a full queue simply drops the message
        # -- the page refetches the plan when the run ends anyway.
        self._listeners: list = []
        self._listener_lock = threading.Lock()
        self.duplicate_stats = None
        self.copy_stats = None
        self._lock = threading.Lock()

    # -- plan access ------------------------------------------------------

    # -- live updates -----------------------------------------------------

    def subscribe(self):
        """A queue that will receive every published update."""
        import queue

        channel = queue.Queue(maxsize=200)
        with self._listener_lock:
            self._listeners.append(channel)
        return channel

    def unsubscribe(self, channel) -> None:
        with self._listener_lock:
            if channel in self._listeners:
                self._listeners.remove(channel)

    def publish(self, kind: str, payload: dict) -> None:
        """Send one update to every listener. Never blocks, never raises.

        A browser tab that has gone away, or one that cannot keep up, must
        not be able to stall the analysis. Dropping a message is fine: the
        page reloads the plan when the run finishes.
        """
        import queue

        message = {"kind": kind, **payload}
        with self._listener_lock:
            listeners = list(self._listeners)
        for channel in listeners:
            try:
                channel.put_nowait(message)
            except queue.Full:
                log.debug("A live listener is not keeping up; dropping an update")

    def set_plan(self, plan: Plan) -> None:
        self.plan = plan
        self.plan_built_at = datetime.now()
        self.photos_by_id = [p for event in plan.events for p in event.photos]

    def photo(self, photo_id: int) -> Optional[Photo]:
        if 0 <= photo_id < len(self.photos_by_id):
            return self.photos_by_id[photo_id]
        return None

    # -- jobs -------------------------------------------------------------

    def start_job(self, name: str, target) -> tuple[bool, str]:
        with self._lock:
            if self.job is not None and self.job.status == "running":
                return False, f"{self.job.name} is already running"
            job = Job(name=name)
            self.job = job

        def run() -> None:
            try:
                target(job)
                if job.cancelled:
                    job.say("Cancelled. Nothing was written.")
                    job.status = "cancelled"
                else:
                    job.status = "done"
            except (Cancelled, CopyCancelled):
                # Both mean "you pressed Stop". The copier raises its own
                # class, which was not caught here, so a deliberate stop was
                # reported as a crash complete with traceback -- alarming,
                # and it hid whether anything had actually gone wrong.
                job.say("Cancelled. Nothing further was written.")
                job.status = "cancelled"
            except UnsafePathError as exc:
                job.error = str(exc)
                job.say(f"Refused: {exc}")
                log.error("Job %r refused an unsafe path: %s", job.name, exc)
                job.status = "error"
            except Exception as exc:
                job.error = f"{type(exc).__name__}: {exc}"
                job.say(f"Failed: {job.error}")
                # ERROR, not DEBUG. At the default level a debug traceback is
                # discarded, which left a crash with no file, no line and no
                # stack -- unexplainable after the fact, which is the one
                # thing a log has to prevent.
                trace = traceback.format_exc()
                log.error("Job %r failed\n%s", job.name, trace)
                # And into the page's own log, so it is visible without
                # going to find the file.
                for line in trace.strip().splitlines()[-12:]:
                    job.log.append(f"        {line}")
                # LAST. Anything watching the job polls this field to decide
                # the run is over; setting it before the record is complete
                # means a reader can see "failed" and an empty explanation.
                job.status = "error"
            finally:
                job.finished = datetime.now()

        threading.Thread(target=run, daemon=True, name=f"job-{name}").start()
        return True, "started"

    # -- serialization ----------------------------------------------------

    def status_dict(self) -> dict[str, Any]:
        return {
            "source": str(self.source) if self.source else "",
            "output": str(self.output) if self.output else "",
            # Whether analysis can run at all. Without this the only way to
            # find out is to press Identify and have it stop -- after the
            # scan, the clustering and the duplicate pass have all run.
            "api_key_present": bool(self.config.analysis.api_key_resolved),
            "suggested_source": self.suggested_source,
            "suggested_output": self.suggested_output,
            "has_plan": self.plan is not None,
            "plan_built_at": (
                self.plan_built_at.isoformat() if self.plan_built_at else None
            ),
            "survey": self.survey,
            "job": self.job.to_dict() if self.job else None,
            "steps": STEPS,
            "config": {
                "time_gap_hours": self.config.cluster.time_gap_hours,
                "distance_km": self.config.cluster.distance_km,
                "geocode": self.config.geocode.provider,
            },
            "backends": backend_availability(),
            "thumbnails": self.renderer.available,
            "edits_path": str(self.default_edits_path),
            "last_saved": self.last_saved,
            "duplicates": (
                self.duplicate_stats.to_dict() if self.duplicate_stats else None
            ),
        }

    def plan_dict(self) -> dict[str, Any]:
        plan = self.plan
        if plan is None:
            return {"events": [], "summary": None}
        events = []
        photo_id = 0
        for event in plan.events:
            points = [p.coords for p in event.gps_photos]
            reference = medoid(points) if points else None
            photos = []
            for photo in event.photos:
                photos.append(
                    {
                        "id": photo_id,
                        "name": photo.source_path.name,
                        "dest": photo.dest_name,
                        "time": photo.timestamp.isoformat() if photo.timestamp else None,
                        "time_source": photo.timestamp_source,
                        "lat": photo.lat,
                        "lon": photo.lon,
                        "altitude": photo.altitude,
                        "heading": photo.heading,
                        "camera": photo.camera,
                        "size": photo.size_bytes,
                        "width": photo.width,
                        "height": photo.height,
                        "warnings": photo.warnings,
                    }
                )
                photo_id += 1
            events.append(
                {
                    "index": event.index,
                    "proposed_name": event.proposed_name,
                    "year": event.year,
                    "rel_dir": event.rel_dir.as_posix(),
                    "place_label": event.place_label,
                    "start": event.start.isoformat() if event.start else None,
                    "end": event.end.isoformat() if event.end else None,
                    "photo_count": len(event.photos),
                    "size_bytes": sum(p.size_bytes for p in event.photos),
                    "missing_gps": event.missing_gps_count,
                    "missing_time": event.missing_time_count,
                    "heading_count": event.heading_count,
                    "cameras": event.cameras,
                    "notes": event.notes,
                    "lat": reference[0] if reference else None,
                    "lon": reference[1] if reference else None,
                    "span_km": round(bbox_span_km(points), 2) if points else 0.0,
                    # Identification results, shown as the reasoning behind
                    # a proposed name rather than hidden behind it.
                    "activity": event.activity,
                    "place_name": event.place_name,
                    "route_name": event.route_name,
                    "mountain_range": event.mountain_range,
                    "region": event.region,
                    "country": event.country,
                    "country_code": event.country_code,
                    "enriched_lat": event.enriched_lat,
                    "enriched_lon": event.enriched_lon,
                    "name_source": event.name_source,
                    "tag_summary": [list(t) for t in event.tag_summary],
                    "evidence": event.evidence,
                    "photos": photos,
                }
            )
        return {
            "source_root": str(plan.source_root),
            "output_root": str(plan.output_root),
            "summary": {
                "event_count": len(plan.events),
                "photo_count": plan.photo_count,
                "total_bytes": plan.total_bytes,
                "skipped_count": len(plan.skipped),
                "missing_gps_count": plan.missing_gps_count,
                "missing_time_count": plan.missing_time_count,
            },
            "events": events,
        }


# --------------------------------------------------------------------------
# Folder browsing
# --------------------------------------------------------------------------


def list_drives() -> list[dict[str, str]]:
    """Windows drive letters that currently exist, with free space."""
    drives = []
    if os.name == "nt":
        import shutil as _shutil

        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            entry = {"path": root, "label": f"{letter}:"}
            try:
                usage = _shutil.disk_usage(root)
                entry["free"] = usage.free
                entry["total"] = usage.total
            except OSError:
                pass
            drives.append(entry)
    else:
        drives.append({"path": "/", "label": "/"})
    return drives


def browse(path_str: str) -> dict[str, Any]:
    """List subdirectories of a path. Directory names only, never files."""
    if not path_str:
        return {"path": "", "parent": None, "dirs": [], "drives": list_drives()}

    path = _resolve(Path(path_str))
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist")
    if not path.is_dir():
        raise NotADirectoryError(f"{path} is not a folder")

    dirs = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                if entry.name.startswith("."):
                    continue
                dirs.append({"name": entry.name, "path": str(Path(entry.path))})
    except PermissionError as exc:
        raise PermissionError(f"Cannot read {path}: {exc}") from None

    dirs.sort(key=lambda d: d["name"].lower())
    parent = str(path.parent) if path.parent != path else None
    return {
        "path": str(path),
        "parent": parent,
        "dirs": dirs,
        "drives": list_drives(),
    }


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------


def _publish_event(state, event) -> None:
    """Push one freshly-named event to the page.

    Only the fields that change during identification, so the message stays
    small: the name, where it came from, the agreed position and the tags.
    """
    state.publish("event", {
        "index": event.index,
        "year": event.year,
        "proposed_name": event.proposed_name,
        "rel_dir": event.rel_dir.as_posix(),
        "name_source": event.name_source,
        "place_name": event.place_name,
        "mountain_range": event.mountain_range,
        "region": event.region,
        "country": event.country,
        "activity": event.activity,
        "lat": event.enriched_lat,
        "lon": event.enriched_lon,
        "evidence": list(event.evidence),
    })


def _dedupe_into(state, plan, job) -> None:
    """Fingerprint the plan's photos and mark duplicate groups.

    Detection only. Nothing is deleted here or anywhere else -- suspected
    duplicates are copied to _duplicates_review/ at copy time for the user
    to judge. Shared by the main run and the standalone button, so the two
    cannot drift apart.
    """
    from .dedupe import find_duplicates, mark_duplicates

    photos = list(plan.photos)
    job.total = len(photos)
    job.done_count = 0
    job.step = "dupes"
    job.say(f"Fingerprinting {len(photos)} photo(s) for duplicates...")

    def progress(done, total) -> None:
        job.done_count = done

    groups, stats = find_duplicates(
        photos,
        workers=state.config.scan.scan_workers,
        progress=progress,
        should_cancel=lambda: job.cancelled,
    )
    marked = mark_duplicates(groups)
    state.duplicate_groups = groups
    state.duplicate_stats = stats
    state.set_plan(plan)
    empty = sum(1 for p in photos if getattr(p, "reject_reason", None))
    job.detail = (
        f"{stats.exact_groups} exact group(s) ({stats.exact_duplicates} extra "
        f"copies), {stats.near_groups} near group(s) "
        f"({stats.near_duplicates} extra), {marked} photo(s) marked"
        + (f", {empty} empty frame(s)" if empty else "")
    )
    job.say(f"  {job.detail}")
    if empty:
        job.say(
            f"  {empty} frame(s) look empty (all black, all white or blank). "
            "They will go to _rejected_review/ and are skipped by the paid "
            "analysis."
        )
    job.say(
        "  Marked only. Nothing was deleted, and nothing will be: duplicates "
        "and empty frames are copied aside for you to judge."
    )


class AppHandler(BaseHTTPRequestHandler):
    server_version = "PhotoOrganizer/0.1"
    state: AppState  # injected by make_server
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        log.debug("%s - %s", self.address_string(), fmt % args)

    # -- helpers ----------------------------------------------------------

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: Optional[dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; img-src 'self' data:; style-src 'unsafe-inline'; "
            "script-src 'unsafe-inline'; connect-src 'self'; form-action 'none'",
        )
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        # Never cache. This is a local control panel whose page and status
        # reflect live state; a browser reusing an old copy showed stale
        # defaults and looked like the settings had been lost.
        self.send_header("Cache-Control", "no-store, must-revalidate")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: dict) -> None:
        self._send(
            status,
            json.dumps(payload, default=str).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _authorized(self, query: dict[str, list[str]]) -> bool:
        """Is this request from the person who started the server?

        Three ways to prove it, so the URL only has to carry the token once:
        the query string, the cookie set on that first visit, or an explicit
        header from the page's own fetch calls.
        """
        if not getattr(self.state, "require_token", False):
            # The Host and Origin checks carry the security here. A token in
            # the URL cannot stop a local program that can read the token
            # file, and it is not what stops a web page: Origin is.
            return True
        candidates = [
            (query.get("t") or [""])[0],
            self.headers.get("X-Photo-Organizer-Token", ""),
            self._cookie_token(),
        ]
        return any(
            supplied and secrets.compare_digest(supplied, self.state.token)
            for supplied in candidates
        )

    def _cookie_token(self) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return ""

    def _host_allowed(self) -> bool:
        """Refuse any Host header that is not a loopback name.

        This is the DNS-rebinding defence, and it is the one that matters
        once there is no token. An attacker can point a hostname they own at
        127.0.0.1; the browser then treats their page as same-origin with
        this server, and Origin checks pass because the origin genuinely is
        theirs. What does not match is the Host they had to send to get
        here, so that is what gets checked.
        """
        host = self.headers.get("Host", "")
        # Strip the port, coping with the [::1]:8080 form.
        if host.startswith("["):
            name = host.partition("]")[0].lstrip("[")
        else:
            name = host.partition(":")[0]
        return name.lower() in ("localhost", "127.0.0.1", "::1", "")

    def _same_origin(self) -> bool:
        """Reject requests a browser says came from somewhere else.

        A token in a URL can be shoulder-surfed, logged, or pasted into a
        chat. This is the second lock: a page on another site can still make
        the browser SEND a request here, but the browser will label it with
        its own Origin, and that label is not forgeable by the page.

        No Origin header at all means a non-browser client (curl, the tests,
        a script), which cannot be a cross-site attack.
        """
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host, port = self.server.server_address[:2]
        allowed = {
            f"http://{host}:{port}",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }
        return origin in allowed

    def _body(self) -> Optional[dict]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 8 * 1024 * 1024:
            self._json(413, {"error": "payload too large"})
            return None
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._json(400, {"error": f"bad JSON: {exc}"})
            return None

    # -- GET --------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        route = parsed.path

        if not self._host_allowed():
            self._send(403, b"Forbidden: not a loopback host.",
                       "text/plain; charset=utf-8")
            return

        if route == "/":
            if not self._authorized(query):
                self._send(
                    403,
                    b"Forbidden: open the URL printed in the terminal, "
                    b"which includes this session's token.",
                    "text/plain; charset=utf-8",
                )
                return
            try:
                html = (STATIC_DIR / "app.html").read_bytes()
            except OSError as exc:
                self._send(500, f"UI missing: {exc}".encode(), "text/plain")
                return
            # Hand the token back as a cookie so every later visit can use
            # the bare URL. HttpOnly keeps it out of reach of page scripts;
            # SameSite=Strict means a browser will not attach it to a request
            # started by any other site.
            self._send(
                200,
                html.replace(b"__TOKEN__", self.state.token.encode()),
                "text/html; charset=utf-8",
                extra={
                    "Set-Cookie":
                        f"{COOKIE_NAME}={self.state.token}; Path=/; HttpOnly;"
                        " SameSite=Strict; Max-Age=31536000",
                },
            )
            return

        if not self._authorized(query):
            self._json(403, {"error": "bad token"})
            return

        if route == "/api/status":
            self._json(200, self.state.status_dict())
            return
        if route == "/api/events":
            self._stream_events()
            return

        if route == "/api/preflight":
            self._preflight()
            return

        if route == "/api/estimate":
            self._estimate()
            return

        if route == "/api/plan":
            self._json(200, self.state.plan_dict())
            return
        if route == "/api/browse":
            self._browse(query)
            return
        if route.startswith("/img/"):
            self._image(route, query)
            return
        self._json(404, {"error": "not found"})

    # -- POST -------------------------------------------------------------

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)
        # Origin first: these are the routes that scan drives and copy files,
        # so a request a browser itself labels as coming from another site is
        # refused before the token is even considered.
        if not self._host_allowed():
            self._json(403, {"error": "not a loopback host"})
            return
        if not self._same_origin():
            self._json(403, {"error": "cross-site request refused"})
            return
        if not self._authorized(query):
            self._json(403, {"error": "bad token"})
            return

        payload = self._body()
        if payload is None:
            return
        route = parsed.path

        if route == "/api/paths":
            self._set_paths(payload)
        elif route == "/api/settings":
            self._settings(payload)
        elif route == "/api/run":
            self._run(payload)
        elif route == "/api/cancel":
            job = self.state.job
            if job and job.status == "running":
                job.cancel()
                self._json(200, {"ok": True})
            else:
                self._json(409, {"error": "nothing running"})
        elif route == "/api/save":
            self._save(payload)
        elif route == "/api/manifest":
            self._manifest(payload)
        elif route == "/api/quit":
            self._json(200, {"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            self._json(404, {"error": "not found"})

    # -- handlers ---------------------------------------------------------

    def _browse(self, query: dict[str, list[str]]) -> None:
        raw = unquote((query.get("path") or [""])[0])
        try:
            self._json(200, browse(raw))
        except (OSError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def _set_paths(self, payload: dict) -> None:
        """Validate a source/output pair without scanning anything."""
        source = (payload.get("source") or "").strip()
        output = (payload.get("output") or "").strip()
        if not source or not output:
            self._json(400, {"error": "both a source and an output are required"})
            return
        try:
            resolved_source, resolved_output = check_paths(Path(source), Path(output))
        except UnsafePathError as exc:
            self._json(400, {"error": str(exc)})
            return

        state = self.state
        state.source, state.output = resolved_source, resolved_output
        state.plan = None
        state.photos_by_id = []
        state.plan_built_at = None

        try:
            state.survey = survey_source(resolved_source, state.config.scan)
        except OSError as exc:
            self._json(400, {"error": f"cannot read source: {exc}"})
            return

        self._json(
            200,
            {
                "ok": True,
                "source": str(resolved_source),
                "output": str(resolved_output),
                "survey": state.survey,
            },
        )

    def _settings(self, payload: dict) -> None:
        cfg = self.state.config
        try:
            # An empty number box arrives as JSON null (JS NaN does not
            # survive JSON.stringify). Treat that as "leave unchanged"
            # rather than crashing on float(None).
            if payload.get("time_gap_hours") is not None:
                try:
                    value = float(payload["time_gap_hours"])
                except (TypeError, ValueError):
                    raise ValueError("time gap must be a number") from None
                if not 0 < value <= 24 * 30:
                    raise ValueError("time gap must be between 0 and 720 hours")
                cfg.cluster.time_gap_hours = value
            if payload.get("distance_km") is not None:
                try:
                    value = float(payload["distance_km"])
                except (TypeError, ValueError):
                    raise ValueError("distance must be a number") from None
                if not 0 < value <= 20000:
                    raise ValueError("distance must be between 0 and 20000 km")
                cfg.cluster.distance_km = value
            if "geocode" in payload:
                provider = str(payload["geocode"])
                if provider not in ("offline", "nominatim", "none"):
                    raise ValueError(f"unknown geocode provider {provider!r}")
                cfg.geocode.provider = provider
        except (TypeError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ok": True, "config": self.state.status_dict()["config"]})

    def _run(self, payload: dict) -> None:
        step = payload.get("step", "plan")
        state = self.state

        if step == "all":
            self._run_everything(payload)
            return

        # "enrich" is what the page has always sent; "analyze" is the id in
        # STEPS. Both mean the same thing, and only accepting one of them
        # made the Identify button answer "not implemented yet".
        if step in ("analyze", "enrich", "identify"):
            self._run_analyze()
            return

        if step == "dupes":
            self._run_dupes()
            return

        if step == "copy":
            self._run_copy(payload)
            return

        if step != "plan":
            self._json(
                400,
                {
                    "error": (
                        f"'{step}' is not implemented yet. Milestone 1 covers "
                        "scan, cluster, name and plan only -- no files are copied."
                    )
                },
            )
            return
        if state.source is None or state.output is None:
            self._json(400, {"error": "choose a source and output folder first"})
            return

        source, output = state.source, state.output
        config = state.config

        def work(job: Job) -> None:
            job.say(f"Source: {source}")
            job.say(f"Output: {output} (nothing will be written)")
            job.total = state.survey["images"] if state.survey else 0

            def on_step(step_id: str, detail: str) -> None:
                if step_id.endswith("_done"):
                    job.completed_steps.append(step_id[: -len("_done")])
                    job.say(f"  {detail}")
                else:
                    job.step = step_id
                    job.detail = detail
                    job.say(detail)

            last_report = [0]

            def progress(count: int, path: Path) -> None:
                job.done_count = count
                job.current = path.name
                # A periodic line with the measured rate, so a slow disk is
                # visible as a slow disk rather than as a hung program.
                if count - last_report[0] >= 500:
                    last_report[0] = count
                    elapsed = (datetime.now() - job.started).total_seconds()
                    rate = count / elapsed if elapsed else 0
                    left = (job.total - count) / rate if rate else 0
                    job.say(
                        f"  {count}/{job.total} files  "
                        f"({rate:.0f}/s, about {left / 60:.0f} min left)"
                    )

            plan = build_plan(
                source,
                output,
                config,
                progress=progress,
                on_step=on_step,
                should_cancel=lambda: job.cancelled,
            )
            state.set_plan(plan)
            job.step = "plan"
            job.current = ""
            job.skipped = len(plan.skipped)
            job.warnings = sum(1 for p in plan.photos if p.warnings)
            job.detail = (
                f"{len(plan.events)} event(s), {plan.photo_count} photo(s) planned"
            )
            # Say plainly what the metadata actually looked like: whether the
            # photos carry GPS decides whether place names are possible at all.
            with_gps = plan.photo_count - plan.missing_gps_count
            job.say(
                f"  {with_gps}/{plan.photo_count} photo(s) have GPS; "
                f"{plan.photo_count - plan.missing_time_count} have an EXIF timestamp"
            )
            if job.warnings:
                job.say(f"  {job.warnings} file(s) had metadata warnings")
            if plan.missing_gps_count == plan.photo_count and plan.photo_count:
                job.say(
                    "  No photo has GPS, so every event is named Unknown_DD_MM. "
                    "Place lookup cannot help here; name them yourself below."
                )
            # Duplicates are part of the run, not a separate click. This
            # writes nothing, and skipping it means paying to analyse thirty
            # frames of one burst to learn what one frame would have said.
            if not job.cancelled:
                _dedupe_into(state, plan, job)
                job.completed_steps.append("dupes")
                job.step = "dupes"

            job.say("Done. Nothing was written -- this is a preview.")

        ok, message = state.start_job("Build plan", work)
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _preflight(self) -> None:
        """Everything that can fail, checked in about two seconds."""
        from .analyze import preflight

        state = self.state
        if state.plan is None:
            self._json(400, {"error": "run the pipeline first, then check"})
            return
        try:
            checks = [c.to_dict() for c in preflight(state.plan, state.config)]
        except Exception as exc:
            self._json(500, {"error": f"preflight failed: {exc}"})
            return
        self._json(200, {
            "checks": checks,
            "ready": not any(c["fatal"] and not c["ok"] for c in checks),
        })

    def _stream_events(self) -> None:
        """Server-sent events: one line per update, until the client leaves.

        Held open deliberately. ThreadingHTTPServer gives this its own
        thread, and the heartbeat every 15 seconds keeps proxies and idle
        timeouts from closing a connection that is simply quiet.
        """
        import queue

        channel = self.state.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.wfile.write(b": connected\n\n")
            self.wfile.flush()

            while True:
                try:
                    message = channel.get(timeout=15)
                except queue.Empty:
                    # A comment line. Keeps the connection alive without
                    # pretending anything happened.
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                body = json.dumps(message, default=str)
                self.wfile.write(f"data: {body}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass  # the tab was closed or reloaded; entirely normal
        except OSError as exc:
            log.debug("Live stream ended: %s", exc)
        finally:
            self.state.unsubscribe(channel)

    def _estimate(self) -> None:
        """What the next analysis run would actually cost.

        Only photos absent from the cache count. Quoting the whole library
        on a re-run overstates it by the entire cached amount.
        """
        from .analyze import pending_cost

        state = self.state
        if state.plan is None:
            survey = state.survey or {}
            images = survey.get("images", 0)
            from .batch import estimate_cost_usd

            self._json(200, {
                "known": False,
                "selected": images,
                "already_analysed": None,
                "pending": images,
                "estimated_cost_usd": estimate_cost_usd(
                    images, batch=state.config.analysis.use_batch,
                    per_photo_usd=state.config.analysis.cost_per_photo_usd),
                "note": "upper bound -- nothing has been scanned yet, so any "
                        "photos already in the cache are not yet discounted",
            })
            return
        try:
            data = pending_cost(state.plan, state.config)
        except Exception as exc:
            self._json(500, {"error": f"could not estimate: {exc}"})
            return
        data["known"] = True
        data["note"] = (
            "photos already analysed cost nothing; only the pending ones are billed"
        )
        self._json(200, data)

    def _run_everything(self, payload: dict) -> None:
        """Everything, in one job: plan, duplicates, identify, copy.

        The last two spend money and create files, so this needs the same
        explicit confirmation the copy button does -- once, for the whole
        run, from a dialog that has already stated the numbers.
        """
        from .analyze import analyze_plan
        from .copier import copy_plan

        state = self.state
        if not payload.get("confirm"):
            self._json(400, {
                "error": "This run analyses and copies. Confirm first.",
                "needs_confirmation": True,
                "images": (state.survey or {}).get("images", 0),
                "output": str(state.output) if state.output else "",
            })
            return
        if state.source is None or state.output is None:
            self._json(400, {"error": "choose a source and output folder first"})
            return

        source, output, config = state.source, state.output, state.config

        def work(job: Job) -> None:
            job.say(f"Source: {source}   (read-only -- never modified)")
            job.say(f"Output: {output}")
            job.total = (state.survey or {}).get("images", 0)

            def on_step(step_id: str, detail: str) -> None:
                if step_id.endswith("_done"):
                    job.completed_steps.append(step_id[: -len("_done")])
                    job.say(f"  {detail}")
                else:
                    job.step = step_id
                    job.detail = detail
                    job.say(detail)

            def progress(count: int, path) -> None:
                job.done_count = count
                job.current = getattr(path, "name", str(path))

            # --- scan, cluster, name, plan -----------------------------
            # Reuse a plan already built for these same folders. Re-reading
            # 14,000 files to rediscover what is already in memory wastes
            # minutes and tells us nothing new.
            reusable = (
                state.plan is not None
                and state.source == source
                and state.output == output
            )
            if reusable:
                plan = state.plan
                job.say(
                    f"Reusing the plan already built for these folders: "
                    f"{len(plan.events)} event(s), {plan.photo_count} photo(s)."
                )
                job.say("  (Use 'Preview only' to rebuild it from scratch.)")
                for done_step in ("scan", "cluster", "name", "plan"):
                    job.completed_steps.append(done_step)
            else:
                plan = build_plan(source, output, config, progress=progress,
                                  on_step=on_step,
                                  should_cancel=lambda: job.cancelled)
                state.set_plan(plan)
                job.skipped = len(plan.skipped)
                job.say(f"  {len(plan.events)} event(s), {plan.photo_count} photo(s)")
            if job.cancelled:
                return

            # --- fail fast --------------------------------------------
            # Everything that can be known is checked here, in about two
            # seconds. The first real run spent 85 minutes encoding and then
            # died on an upload limit that was predictable from the photo
            # count alone.
            from .analyze import preflight

            job.step = "preflight"
            job.say("Checking before starting:")
            problems = []
            for check in preflight(plan, config):
                mark = "ok" if check.ok else ("FAILED" if check.fatal else "warning")
                job.say(f"  [{mark}] {check.name}: {check.detail}")
                if not check.ok and check.fatal:
                    problems.append(f"{check.name}: {check.detail}")
            if problems:
                job.say("Stopping before any work is done. Nothing was written.")
                raise RuntimeError("Preflight failed -- " + "; ".join(problems))

            # --- duplicates, so the paid step skips repeats ------------
            # Also reused: the marks live on the photo objects, so re-running
            # the fingerprint pass over the same plan finds the same answer.
            if reusable and state.duplicate_groups:
                job.say(
                    f"Reusing {len(state.duplicate_groups)} duplicate group(s) "
                    "already found."
                )
                job.completed_steps.append("dupes")
            else:
                _dedupe_into(state, plan, job)
                job.completed_steps.append("dupes")
            if job.cancelled:
                return

            # --- identify ----------------------------------------------
            job.step = "analyze"
            job.done_count = 0
            job.say("Identifying photos with Gemini (batch, half price)")
            try:
                stats = analyze_plan(
                    plan, config,
                    on_step=lambda m: (setattr(job, "detail", m), job.say(f"  {m}")),
                    on_progress=lambda done, total, label: (
                        setattr(job, "done_count", done),
                        setattr(job, "total", total or job.total),
                        setattr(job, "current", label),
                    ),
                    should_cancel=lambda: job.cancelled,
                    duplicate_groups=state.duplicate_groups,
                    on_event_named=lambda ev: _publish_event(state, ev),
                )
                state.set_plan(plan)
                if stats.better_duplicate_chosen:
                    job.say(
                        f"  {stats.better_duplicate_chosen} duplicate group(s) "
                        "kept a better frame than file size would have picked"
                    )
                job.say(
                    f"  {stats.named_from_peak} from a peak, "
                    f"{stats.named_from_crag} from a crag, "
                    f"{stats.named_from_region} from region, "
                    f"{stats.named_from_activity} from activity, "
                    f"{stats.still_unknown} still unknown"
                )
            except Exception as exc:
                # Do not lose the run over this. Without analysis the folders
                # are simply named from what is already known, and the photos
                # still get copied -- which is the part that cannot be redone
                # cheaply.
                log.warning("Analysis failed during the full run: %s", exc)
                job.say(f"  Analysis failed ({exc}).")
                job.say("  Continuing to copy with the names already known.")
            job.completed_steps.append("analyze")
            if job.cancelled:
                return

            # --- copy ---------------------------------------------------
            job.step = "copy"
            job.done_count = 0
            job.total = plan.photo_count
            job.say("Copying into the library. Source files are read, never written.")
            result = copy_plan(
                plan, config,
                on_step=lambda m: (setattr(job, "detail", m), job.say(f"  {m}")),
                on_progress=lambda done, total, name: (
                    setattr(job, "done_count", done),
                    setattr(job, "current", name),
                ),
                should_cancel=lambda: job.cancelled,
            )
            state.copy_stats = result
            job.completed_steps.append("copy")
            job.detail = (
                f"{result.copied} copied, {result.tagged} tagged, "
                f"{result.duplicates_copied} duplicates set aside, "
                f"{result.verify_failures} verification failures"
            )
            job.say(job.detail)
            job.say("Done. Your source folder is unchanged.")

        ok, message = state.start_job("Run everything", work)
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _run_copy(self, payload: dict) -> None:
        """Copy into the output tree and tag the copies. Requires confirmation."""
        from .copier import copy_plan

        state = self.state
        if state.plan is None:
            self._json(400, {"error": "run the pipeline first, then copy"})
            return
        # The one irreversible-ish step in the tool: it creates files. It
        # does not proceed on a stray click.
        # One explicit confirmation, not a typed word. The dialog that
        # produces it states the counts and the destination, which is what
        # CLAUDE.md actually asks for; typing COPY added annoyance only.
        if not payload.get("confirm"):
            self._json(
                400,
                {
                    "error": "Copying needs explicit confirmation.",
                    "needs_confirmation": True,
                    "photos": state.plan.photo_count,
                    "output": str(state.plan.output_root),
                },
            )
            return

        plan = state.plan
        config = state.config

        def work(job: Job) -> None:
            job.total = plan.photo_count
            job.step = "copy"

            def on_progress(done: int, total: int, name: str) -> None:
                job.done_count = done
                job.current = name

            stats = copy_plan(
                plan,
                config,
                on_step=lambda m: (setattr(job, "detail", m), job.say(m)),
                on_progress=on_progress,
                should_cancel=lambda: job.cancelled,
            )
            state.copy_stats = stats
            job.detail = (
                f"{stats.copied} copied, {stats.tagged} tagged, "
                f"{stats.duplicates_copied} duplicates set aside, "
                f"{stats.verify_failures} verification failures"
            )
            job.say(job.detail)
            job.say("The source folder was not modified.")

        ok, message = state.start_job("Copy library", work)
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _run_dupes(self) -> None:
        """Find duplicates across the whole plan. Marks only; deletes nothing."""
        state = self.state
        if state.plan is None:
            self._json(400, {"error": "run the pipeline first, then find duplicates"})
            return
        plan = state.plan

        def work(job: Job) -> None:
            _dedupe_into(state, plan, job)

        ok, message = state.start_job("Find duplicates", work)
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _run_analyze(self) -> None:
        """Analyse photos with the hosted model and name events from the cache."""
        from .analyze import analyze_plan

        state = self.state
        if state.plan is None:
            self._json(400, {"error": "run the pipeline first, then identify"})
            return

        plan = state.plan
        config = state.config

        def work(job: Job) -> None:
            unknown = [e for e in plan.events if not e.place_label]
            job.total = len(unknown)
            job.step = "enrich"
            job.say(f"{len(unknown)} event(s) have no name from GPS.")

            def on_step(message: str) -> None:
                job.detail = message
                job.say(message)

            def on_progress(done: int, total: int, label: str) -> None:
                job.done_count = done
                job.current = label

            stats = analyze_plan(
                plan,
                config,
                on_step=on_step,
                on_progress=on_progress,
                should_cancel=lambda: job.cancelled,
                duplicate_groups=state.duplicate_groups,
                on_event_named=lambda ev: _publish_event(state, ev),
            )
            # The plan object was mutated in place; refresh the photo index
            # and the built-at stamp so the UI reloads it.
            state.set_plan(plan)
            job.detail = (
                f"{stats.named_from_peak} from a verified peak, "
                f"{stats.named_from_crag} from a crag, "
                f"{stats.named_from_region} from region, "
                f"{stats.named_from_activity} from activity, "
                f"{stats.still_unknown} still unknown"
            )
            job.say(job.detail)
            job.say("Nothing was written -- names are proposals until you commit.")

        ok, message = state.start_job("Analyse and identify", work)
        self._json(200 if ok else 409, {"ok": ok, "message": message})

    def _image(self, route: str, query: dict[str, list[str]]) -> None:
        try:
            photo_id = int(route.rsplit("/", 1)[-1])
        except ValueError:
            self._json(400, {"error": "bad id"})
            return
        photo = self.state.photo(photo_id)
        if photo is None:
            self._json(404, {"error": "no such photo"})
            return
        full = (query.get("full") or ["0"])[0] == "1"
        data = self.state.renderer.render(photo, FULL_SIZE if full else THUMB_SIZE)
        if data is None:
            self._json(415, {"error": "cannot render"})
            return
        self._send(200, data, "image/jpeg", {"Cache-Control": "private, max-age=3600"})

    def _save(self, payload: dict) -> None:
        state = self.state
        if state.plan is None:
            self._json(400, {"error": "no plan to save"})
            return
        events = payload.get("events")
        if not isinstance(events, list):
            self._json(400, {"error": "events must be a list"})
            return

        names: dict[int, str] = {}
        merges: set[int] = set()
        for entry in events:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("index"))
            except (TypeError, ValueError):
                continue
            name = str(entry.get("name") or "").strip()
            if name:
                names[index] = name
            if entry.get("merge_into_previous"):
                merges.add(index)

        target = payload.get("path") or state.default_edits_path
        try:
            written = write_edits_file(
                state.plan, Path(target), names=names, merges=merges
            )
        except (UnsafePathError, OSError) as exc:
            self._json(400, {"error": str(exc)})
            return

        state.last_saved = str(written)
        self._json(200, {"ok": True, "path": str(written)})

    def _manifest(self, payload: dict) -> None:
        state = self.state
        if state.plan is None:
            self._json(400, {"error": "no plan to export"})
            return
        target = payload.get("path") or "photo_plan.json"
        try:
            written = save_manifest(state.plan, Path(target))
        except (UnsafePathError, OSError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"ok": True, "path": str(written)})


class _AppServer(ThreadingHTTPServer):
    # HTTPServer sets this True, which on Windows means SO_REUSEADDR lets a
    # second process bind a port another process is already serving. The
    # newcomer then prints a URL nobody reaches while the stale server keeps
    # answering with old code. Refuse to start instead.
    allow_reuse_address = False
    daemon_threads = True


class _AppServerV6(_AppServer):
    """The same server on the IPv6 loopback."""

    address_family = socket.AF_INET6


def make_server(state: AppState, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    handler = type("BoundAppHandler", (AppHandler,), {"state": state})
    # Loopback only. Never 0.0.0.0 or "" -- that would expose the photo
    # library to the whole network.
    return _AppServer(("127.0.0.1", port), handler)


def make_v6_server(
    state: AppState, port: int = DEFAULT_PORT
) -> Optional[ThreadingHTTPServer]:
    """A second listener on ::1, or None if IPv6 is unavailable.

    `localhost` resolves to ::1 before 127.0.0.1 on this machine, so without
    this a browser pointed at http://localhost:8080/ is simply refused. Also
    loopback-only: ::1 is no more reachable from outside than 127.0.0.1.

    Returns None rather than failing. A missing IPv6 stack should cost the
    convenience of one hostname, never the ability to start.
    """
    if not socket.has_ipv6:
        return None
    handler = type("BoundAppHandlerV6", (AppHandler,), {"state": state})
    try:
        server = _AppServerV6(("::1", port), handler)
    except OSError as exc:
        log.debug("No IPv6 loopback listener: %s", exc)
        return None
    # Refuse to hand back anything that is not loopback, whatever the OS did.
    bound = server.server_address[0]
    if bound not in ("::1", "0:0:0:0:0:0:0:1"):
        log.warning("Refusing a non-loopback IPv6 bind on %s", bound)
        server.server_close()
        return None
    return server


# The UI token lives beside the analysis database, not in the source tree,
# and outlives a single run so the URL can be bookmarked.
TOKEN_PATH = Path("~/.photo_organizer/ui_token").expanduser()


COOKIE_NAME = "photo_organizer_token"


def load_or_create_token(path: Path = TOKEN_PATH) -> str:
    """The stable token for this machine's UI, creating one if needed.

    Persisted deliberately. A per-run token meant a new URL every time and
    nothing you could bookmark, which is what made people want to turn the
    protection off altogether -- the worst outcome of the three.
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 24:
            return existing
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        # Best effort on Windows, meaningful on anything POSIX.
        try:
            path.chmod(0o600)
        except (OSError, NotImplementedError):
            pass
    except OSError as exc:
        # A token we cannot store still protects this run; it just will not
        # give a stable URL. Never fail to start over this.
        log.warning("Could not save the UI token to %s: %s", path, exc)
    return token


def reset_token(path: Path = TOKEN_PATH) -> str:
    """Throw the saved token away and make a new one."""
    try:
        path.unlink()
    except OSError:
        pass
    return load_or_create_token(path)


def serve_app(
    config: Config,
    edits_path: Path,
    source: Optional[Path] = None,
    output: Optional[Path] = None,
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    require_token: bool = False,
) -> None:
    """Run the control panel until Quit is clicked or Ctrl+C is pressed."""
    state = AppState(config, edits_path)
    state.require_token = require_token
    if source is not None and output is not None:
        try:
            state.source, state.output = check_paths(source, output)
            state.survey = survey_source(state.source, config.scan)
        except (UnsafePathError, OSError) as exc:
            log.warning("Ignoring prefilled paths: %s", exc)
            state.source = state.output = None

    try:
        server = make_server(state, port)
    except OSError as exc:
        log.error(
            "Could not listen on port %d: %s. "
            "Something else may be using it; pass --port to pick another.",
            port,
            exc,
        )
        raise SystemExit(2) from None

    host, bound = server.server_address[:2]
    # localhost resolves to ::1 before 127.0.0.1, so serve both or the name
    # everybody actually types does not work.
    v6_server = make_v6_server(state, bound)
    if v6_server is not None:
        threading.Thread(
            target=v6_server.serve_forever, name="photo-organizer-v6", daemon=True
        ).start()

    plain = f"http://localhost:{bound}/"
    url = f"{plain}?t={state.token}" if state.require_token else plain

    print("\n  Photo Organizer is running. Nothing has been written.", flush=True)
    print(f"    {url}", flush=True)
    if not state.require_token:
        print(f"    http://127.0.0.1:{bound}/   works too -- bookmark either.",
              flush=True)
        print("  Reachable only from this machine. Requests from a web page,", flush=True)
        print("  or under any other hostname, are refused.", flush=True)
    # Where the full record is, said once, so it can be found after a
    # failure without knowing to look for it.
    for handler in logging.getLogger().handlers:
        path = getattr(handler, "baseFilename", None)
        if path:
            print(f"  Full log: {path}", flush=True)
            break
    print("  Click Quit in the page, or press Ctrl+C here, to stop.\n", flush=True)

    if not state.renderer.available:
        log.warning("Pillow is not installed, so thumbnails will not render.")

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if v6_server is not None:
            v6_server.shutdown()
            v6_server.server_close()
        print("\n  Stopped.", flush=True)
    finally:
        server.server_close()
