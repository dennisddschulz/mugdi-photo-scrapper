"""Local store of per-photo analysis. SQLite, one row per photo.

Why a database and not a JSON blob beside the manifest: analysis costs
money and hours, so it must survive re-runs, re-clustering, threshold
changes, and the source being re-scanned. Keying on the photo's content
hash rather than its path means a file that is renamed, moved, or
duplicated elsewhere is recognised as already analysed.

The store lives outside the source tree and outside the output tree, so it
is unaffected by the delete-the-output-and-retry recovery story.

Nothing here writes to a photo. It only remembers what was learned about one.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .schema import SCHEMA_VERSION, PhotoAnalysis, unwrap_response

log = logging.getLogger(__name__)

DEFAULT_DB = Path("~/.photo_organizer/analysis.sqlite3").expanduser()

# Where the database used to live. A cache directory is the wrong home for
# the only copy of something that was paid for, so an existing file there is
# moved rather than abandoned.
LEGACY_DB = Path("~/.cache/photo_organizer/analysis.sqlite3").expanduser()


def _adopt_legacy(path: Path) -> None:
    """Move a database out of the old cache location, once."""
    if path != DEFAULT_DB or path.exists() or not LEGACY_DB.exists():
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            part = LEGACY_DB.with_name(LEGACY_DB.name + suffix)
            if part.exists():
                part.replace(path.with_name(path.name + suffix))
        log.info("Moved the analysis database out of the cache to %s", path)
    except OSError as exc:
        # Not fatal: worst case the old rows are re-analysed. Never lose the
        # original by half-moving it.
        log.warning("Could not move %s to %s: %s", LEGACY_DB, path, exc)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis (
    content_hash   TEXT PRIMARY KEY,
    source_path    TEXT NOT NULL,
    file_name      TEXT NOT NULL,
    size_bytes     INTEGER,
    taken_at       TEXT,
    model          TEXT,
    schema_version INTEGER NOT NULL,
    analysed_at    TEXT NOT NULL,
    payload        TEXT NOT NULL,
    -- The model's reply exactly as it arrived, before any parsing. This is
    -- what makes "one request per photo, ever" true: when the schema gains
    -- a field, every stored row is re-parsed from here instead of being
    -- re-requested. Never overwritten once set.
    raw_response   TEXT,
    -- Columns duplicated out of the payload purely so they can be queried
    -- and indexed; the payload stays the single source of truth.
    country_code   TEXT,
    region         TEXT,
    mountain_range TEXT,
    peak_name      TEXT,
    verified_peak  TEXT,
    activity       TEXT,
    scene          TEXT,
    is_personal    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_analysis_peak    ON analysis(verified_peak);
CREATE INDEX IF NOT EXISTS idx_analysis_range   ON analysis(mountain_range);
CREATE INDEX IF NOT EXISTS idx_analysis_country ON analysis(country_code);
CREATE INDEX IF NOT EXISTS idx_analysis_path    ON analysis(source_path);

-- Batch jobs, so a submitted job survives the app being closed. A batch can
-- take up to 24 hours; losing the job name would mean paying twice.
CREATE TABLE IF NOT EXISTS batch_job (
    job_name     TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    model        TEXT,
    state        TEXT,
    request_count INTEGER,
    keys_json    TEXT NOT NULL,
    finished_at  TEXT
);
"""


@dataclass
class DbStats:
    photos: int = 0
    with_verified_peak: int = 0
    with_range: int = 0
    personal_documents: int = 0
    models: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


class AnalysisStore:
    """SQLite-backed cache of photo analyses, safe for concurrent readers."""

    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self.path = Path(path).expanduser()
        _adopt_legacy(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            # Databases created before raw_response existed keep their rows;
            # they simply cannot be re-parsed and will be re-requested once.
            columns = {r["name"] for r in conn.execute("PRAGMA table_info(analysis)")}
            if "raw_response" not in columns:
                log.info("Adding raw_response column to the analysis cache")
                conn.execute("ALTER TABLE analysis ADD COLUMN raw_response TEXT")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            # WAL lets the UI read while a batch writes.
            conn.execute("PRAGMA journal_mode=WAL")
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- reads ------------------------------------------------------------

    @staticmethod
    def _parse(row: sqlite3.Row) -> Optional[PhotoAnalysis]:
        """A stored row as a PhotoAnalysis, re-parsing an old one if needed.

        A schema change used to make a row unusable, which meant paying for
        that photo again. Now the raw reply is kept, so an old row is simply
        re-parsed under the current schema -- offline, and free.
        """
        if row["schema_version"] == SCHEMA_VERSION:
            try:
                return PhotoAnalysis.from_row(row["payload"])
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                log.warning("Corrupt analysis payload: %s", exc)
        raw = row["raw_response"] if "raw_response" in row.keys() else None
        if not raw:
            return None
        inner = unwrap_response(raw)
        if inner is None:
            log.warning("Stored reply has no analysis in it")
            return None
        try:
            return PhotoAnalysis.from_model_json(inner, model=row["model"] or "")
        except (TypeError, ValueError) as exc:
            log.warning("Could not re-parse stored reply: %s", exc)
            return None

    def get(self, content_hash: str) -> Optional[PhotoAnalysis]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload, raw_response, model, schema_version FROM"
                " analysis WHERE content_hash=?",
                (content_hash,),
            ).fetchone()
        return self._parse(row) if row is not None else None

    def get_many(self, hashes: Iterable[str]) -> dict[str, PhotoAnalysis]:
        wanted = list(hashes)
        found: dict[str, PhotoAnalysis] = {}
        if not wanted:
            return found
        with self._connect() as conn:
            # Chunked: SQLite caps variables per statement at 999 by default.
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    f"SELECT content_hash, payload, raw_response, model,"
                    f" schema_version FROM analysis "
                    f"WHERE content_hash IN ({marks})",
                    chunk,
                ).fetchall()
                for row in rows:
                    parsed = self._parse(row)
                    if parsed is not None:
                        found[row["content_hash"]] = parsed
        return found

    def has(self, content_hash: str) -> bool:
        """Is this photo already paid for? A re-parsable old row counts."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM analysis WHERE content_hash=? AND"
                " (schema_version=? OR raw_response IS NOT NULL)",
                (content_hash, SCHEMA_VERSION),
            ).fetchone()
        return row is not None

    def missing(self, hashes: Iterable[str]) -> list[str]:
        """Which of these have not been analysed under the current schema."""
        wanted = list(dict.fromkeys(hashes))
        have = set(self.get_many(wanted))
        return [h for h in wanted if h not in have]

    def stats(self) -> DbStats:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n,"
                " SUM(verified_peak IS NOT NULL) AS peaks,"
                " SUM(mountain_range IS NOT NULL) AS ranges,"
                " SUM(is_personal) AS personal"
                " FROM analysis WHERE schema_version=?",
                (SCHEMA_VERSION,),
            ).fetchone()
            models = conn.execute(
                "SELECT DISTINCT model FROM analysis WHERE model IS NOT NULL"
            ).fetchall()
        return DbStats(
            photos=row["n"] or 0,
            with_verified_peak=row["peaks"] or 0,
            with_range=row["ranges"] or 0,
            personal_documents=row["personal"] or 0,
            models=", ".join(sorted({m["model"] for m in models if m["model"]})),
        )

    # -- writes -----------------------------------------------------------

    def put(
        self,
        content_hash: str,
        source_path: Path,
        analysis: PhotoAnalysis,
        size_bytes: int = 0,
        taken_at: Optional[datetime] = None,
        raw: Optional[dict] = None,
    ) -> None:
        payload = json.dumps(analysis.to_dict(), ensure_ascii=False)
        raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT INTO analysis (content_hash, source_path, file_name,"
                " size_bytes, taken_at, model, schema_version, analysed_at,"
                " payload, raw_response, country_code, region, mountain_range,"
                " peak_name, verified_peak, activity, scene, is_personal)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(content_hash) DO UPDATE SET"
                "  source_path=excluded.source_path,"
                "  file_name=excluded.file_name,"
                "  model=excluded.model,"
                "  schema_version=excluded.schema_version,"
                "  analysed_at=excluded.analysed_at,"
                "  payload=excluded.payload,"
                # Never lose an original reply to a later write.
                "  raw_response=COALESCE(excluded.raw_response, analysis.raw_response),"
                "  country_code=excluded.country_code,"
                "  region=excluded.region,"
                "  mountain_range=excluded.mountain_range,"
                "  peak_name=excluded.peak_name,"
                "  verified_peak=excluded.verified_peak,"
                "  activity=excluded.activity,"
                "  scene=excluded.scene,"
                "  is_personal=excluded.is_personal",
                (
                    content_hash,
                    str(source_path),
                    source_path.name,
                    size_bytes,
                    taken_at.isoformat() if taken_at else None,
                    analysis.model,
                    analysis.schema_version,
                    datetime.now().isoformat(timespec="seconds"),
                    payload,
                    raw_json,
                    analysis.country_code,
                    analysis.region,
                    analysis.mountain_range,
                    analysis.peak_name,
                    analysis.verified_peak,
                    analysis.activity,
                    analysis.scene,
                    1 if analysis.is_personal_document else 0,
                ),
            )

    def put_many(self, items: Iterable[tuple[str, Path, PhotoAnalysis, int, Optional[datetime]]]) -> int:
        written = 0
        for content_hash, path, analysis, size, taken in items:
            self.put(content_hash, path, analysis, size, taken)
            written += 1
        return written

    # -- batch jobs -------------------------------------------------------

    def remember_job(
        self, job_name: str, model: str, keys: dict[str, str], state: str = "PENDING"
    ) -> None:
        """Record a submitted batch so it can be reclaimed after a restart."""
        with self._lock, self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO batch_job (job_name, created_at, model,"
                " state, request_count, keys_json, finished_at)"
                " VALUES (?,?,?,?,?,?,NULL)",
                (
                    job_name,
                    datetime.now().isoformat(timespec="seconds"),
                    model,
                    state,
                    len(keys),
                    json.dumps(keys),
                ),
            )

    def update_job(self, job_name: str, state: str, finished: bool = False) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                "UPDATE batch_job SET state=?, finished_at=? WHERE job_name=?",
                (
                    state,
                    datetime.now().isoformat(timespec="seconds") if finished else None,
                    job_name,
                ),
            )

    def open_jobs(self) -> list[dict]:
        """Batches that were submitted and never seen through to the end."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM batch_job WHERE finished_at IS NULL"
                " ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "job_name": r["job_name"],
                "created_at": r["created_at"],
                "model": r["model"],
                "state": r["state"],
                "request_count": r["request_count"],
                "keys": json.loads(r["keys_json"]),
            }
            for r in rows
        ]
