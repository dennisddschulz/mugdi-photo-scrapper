"""Gemini Batch API client: half the price, at the cost of waiting.

Batch runs at 50% of interactive cost with a target turnaround of 24 hours
(usually much less). For a 14,000-photo library analysed once, that trade is
obviously right: nothing here is interactive, and halving the bill matters
more than latency.

Two properties this file exists to guarantee:

* **Submitted work is never paid for twice.** A job name is written to the
  local database the moment it is created, before anything can go wrong.
  Closing the app, a crash, or a reboot does not orphan a running batch --
  it is reclaimed on the next start.
* **Nothing is submitted that is already known.** The caller filters against
  the analysis store first, so a re-run costs nothing.

Endpoints follow the documented REST surface:
  create : POST {base}/models/{model}:batchGenerateContent
  poll   : GET  {base}/{batch_name}
  fetch  : GET  {download}/{file}:download?alt=media
"""

from __future__ import annotations

import base64
import io
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .schema import PROMPT, PhotoAnalysis, response_schema, unwrap_response

log = logging.getLogger(__name__)

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
DOWNLOAD_BASE = "https://generativelanguage.googleapis.com/download/v1beta"
UPLOAD_BASE = "https://generativelanguage.googleapis.com/upload/v1beta/files"

DEFAULT_MODEL = "gemini-3.6-flash"
# Long edge before upload. Place recognition does not improve above this and
# every extra byte is upload time and request size.
MAX_EDGE = 1024
# Inline requests are capped at 20MB total, so anything more than a handful
# of images goes via an uploaded JSONL file.
INLINE_LIMIT_BYTES = 18 * 1024 * 1024
INLINE_MAX_REQUESTS = 12

# The API refuses an upload over 2 GiB:
#   HTTP 413 "Media is too large. Limit: 2147483648"
# Measured the hard way, after 85 minutes of encoding produced a 3.68 GB
# file that was rejected outright. Chunks are kept well under it: the
# accounting is approximate, a rejected chunk costs the whole run, and
# nothing is gained by cutting it fine.
UPLOAD_LIMIT_BYTES = 2 * 1024 * 1024 * 1024
CHUNK_TARGET_BYTES = 1400 * 1024 * 1024

# A batch is capped by REQUEST COUNT as well as by bytes. Measured: one
# request is accepted, 5,254 in a single job is refused with
# RESOURCE_EXHAUSTED even though the account is billed and holds almost no
# enqueued work. The exact ceiling is not documented in the error we saw, so
# the code starts well under it and halves on refusal rather than assuming.
MAX_REQUESTS_PER_BATCH = 1000

# How long to wait after a 429 before trying again, and how many times.
# A quota that resets per minute recovers on its own; one that resets per
# day does not, and the run then stops cleanly with everything already
# submitted still in flight.
RETRY_DELAYS = (20, 60, 180)

# Progress cadence during encoding. It is the slowest stage in the pipeline
# and it used to report nothing at all until it was finished.
ENCODE_REPORT_EVERY = 250

# The API returns BATCH_STATE_* on the generativelanguage endpoint, while
# the documentation and the Vertex flavour use JOB_STATE_*. Measured live:
# a finished job reports BATCH_STATE_SUCCEEDED. Accept both spellings --
# failing to recognise "finished" means polling a completed job for 24 hours
# and then reporting a timeout on work already paid for.
SUCCESS_STATES = {"JOB_STATE_SUCCEEDED", "BATCH_STATE_SUCCEEDED"}

TERMINAL_STATES = SUCCESS_STATES | {
    "JOB_STATE_FAILED",
    "JOB_STATE_CANCELLED",
    "JOB_STATE_EXPIRED",
    "BATCH_STATE_FAILED",
    "BATCH_STATE_CANCELLED",
    "BATCH_STATE_EXPIRED",
}


@dataclass
class BatchResult:
    job_name: str = ""
    state: str = ""
    analyses: dict[str, PhotoAnalysis] = field(default_factory=dict)
    # The complete API reply per photo, kept verbatim so the cache can hold
    # everything that was paid for -- including fields no current code
    # reads. A photo is only ever sent once.
    raw: dict[str, dict] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    submitted: int = 0

    @property
    def succeeded(self) -> bool:
        return self.state in SUCCESS_STATES

    def to_dict(self) -> dict:
        return {
            "job_name": self.job_name,
            "state": self.state,
            "submitted": self.submitted,
            "returned": len(self.analyses),
            "errors": len(self.errors),
        }


class BatchError(RuntimeError):
    """A batch could not be created, polled or read."""


class QuotaExhausted(BatchError):
    """The account will not accept more enqueued work right now."""


class GeminiBatch:
    """Submits photo-analysis requests as one batch job and collects results."""

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_MODEL,
        timeout: int = 180,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    # -- plumbing ---------------------------------------------------------

    def _headers(self, extra: Optional[dict] = None) -> dict:
        return {"x-goog-api-key": self.api_key, **(extra or {})}

    def _request(
        self,
        url: str,
        data: Optional[bytes] = None,
        method: str = "GET",
        headers: Optional[dict] = None,
        raw: bool = False,
    ):
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass
        request = urllib.request.Request(
            url, data=data, headers=self._headers(headers), method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read()
                return body if raw else json.loads(body.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # NOT truncated. A 429 names the exact quota that was hit inside
            # its "details" block, and cutting the message at 400 characters
            # threw away the one piece of information needed to act on it.
            detail = exc.read().decode("utf-8", "replace")
            log.error("HTTP %s from %s:\n%s", exc.code, url.split("?")[0], detail)
            raise BatchError(
                f"HTTP {exc.code} from {url.split('?')[0]}: {detail[:1500]}"
            ) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            raise BatchError(f"Network failure calling {url.split('?')[0]}: {exc}") from None

    # -- request construction --------------------------------------------

    @staticmethod
    def encode_image(path: Path, max_edge: int = MAX_EDGE) -> Optional[str]:
        try:
            from PIL import Image, ImageOps

            with Image.open(path) as img:
                img = ImageOps.exif_transpose(img) or img
                scale = max_edge / max(img.width, img.height)
                if scale < 1:
                    img = img.resize(
                        (max(1, int(img.width * scale)), max(1, int(img.height * scale))),
                        Image.LANCZOS,
                    )
                buffer = io.BytesIO()
                img.convert("RGB").save(buffer, "JPEG", quality=88)
            return base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception as exc:
            log.debug("Could not encode %s: %s", path, exc)
            return None

    def build_request(self, image_b64: str) -> dict:
        """One analysis request, schema-constrained."""
        return {
            "contents": [
                {
                    "parts": [
                        {"text": PROMPT},
                        {"inline_data": {"mime_type": "image/jpeg", "data": image_b64}},
                    ]
                }
            ],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": response_schema(),
            },
        }

    # -- submission -------------------------------------------------------

    def submit(
        self,
        items: Sequence[tuple[str, Path]],
        display_name: str = "photo-organizer",
        progress: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> list[tuple[str, dict[str, str]]]:
        """Create one or more batch jobs. Returns [(job_name, {key: path})].

        A LIST, because a library does not fit in one upload. Photos are
        encoded one at a time and written straight to a temp file; when the
        file approaches the size limit it is uploaded, a job is created, and
        a new chunk begins.

        The key is the photo's content hash, so results map back to database
        rows without depending on ordering or on file paths.
        """
        import tempfile

        def say(message: str) -> None:
            log.info("%s", message)
            if progress:
                progress(message)

        jobs: list[tuple[str, dict[str, str]]] = []
        total = len(items)
        encoded_count = 0
        skipped = 0
        started = time.monotonic()

        handle = None
        path = None
        chunk_bytes = 0
        chunk_keys: dict[str, str] = {}

        def open_chunk():
            nonlocal handle, path, chunk_bytes, chunk_keys
            fd, name = tempfile.mkstemp(prefix="photo-organizer-batch-", suffix=".jsonl")
            handle = open(fd, "w", encoding="utf-8", newline="\n")
            path = Path(name)
            chunk_bytes = 0
            chunk_keys = {}

        def close_and_submit_chunk():
            """Upload the current chunk and create its job."""
            nonlocal handle
            if handle is None:
                return
            handle.close()
            handle = None
            if not chunk_keys:
                path.unlink(missing_ok=True)
                return
            try:
                say(
                    f"Uploading batch {len(jobs) + 1} "
                    f"({len(chunk_keys)} photo(s), {chunk_bytes / 1e6:.0f} MB)"
                )
                file_name = self._upload_file(path, say)
                job_name = self._create_job_with_retry(
                    file_name, display_name, len(jobs) + 1, say
                )
                jobs.append((job_name, dict(chunk_keys)))
                say(f"  batch {len(jobs)} submitted: {job_name}")
            finally:
                # The temp file is several hundred megabytes. Remove it
                # whether or not the upload worked.
                path.unlink(missing_ok=True)

        open_chunk()
        try:
            for key, source in items:
                if should_cancel is not None and should_cancel():
                    say("Cancelled while preparing; nothing further submitted.")
                    break
                encoded = self.encode_image(source)
                if encoded is None:
                    skipped += 1
                    continue
                line = json.dumps(
                    {"key": key, "request": self.build_request(encoded)},
                    ensure_ascii=False,
                ) + "\n"
                blob = line.encode("utf-8")

                # Start a new chunk BEFORE crossing either limit, never
                # after. Request count matters as much as bytes: a batch far
                # under the size cap was still refused for holding too many
                # requests.
                too_big = chunk_bytes + len(blob) > CHUNK_TARGET_BYTES
                too_many = len(chunk_keys) >= MAX_REQUESTS_PER_BATCH
                if chunk_bytes and (too_big or too_many):
                    close_and_submit_chunk()
                    open_chunk()

                handle.write(line)
                chunk_bytes += len(blob)
                chunk_keys[key] = str(source)
                encoded_count += 1

                if encoded_count % ENCODE_REPORT_EVERY == 0:
                    elapsed = time.monotonic() - started
                    rate = encoded_count / elapsed if elapsed else 0
                    left = (total - encoded_count) / rate if rate else 0
                    say(
                        f"  prepared {encoded_count}/{total} photo(s) "
                        f"({rate:.0f}/s, about {left / 60:.0f} min left; "
                        f"chunk {len(jobs) + 1} at {chunk_bytes / 1e6:.0f} MB)"
                    )
            close_and_submit_chunk()
        except QuotaExhausted as exc:
            # Stop cleanly and KEEP what was accepted. Previously one refusal
            # discarded every batch already submitted, along with the work
            # behind them -- and they were already being billed.
            if handle is not None:
                handle.close()
            if path is not None:
                path.unlink(missing_ok=True)
            if not jobs:
                raise
            say(
                f"Quota reached after {len(jobs)} batch(es): {exc}. "
                f"Keeping them; the remaining photos can be submitted later, "
                f"and nothing already analysed is paid for twice."
            )
            return jobs
        except BaseException:
            if handle is not None:
                handle.close()
            if path is not None:
                path.unlink(missing_ok=True)
            raise

        if skipped:
            say(f"  {skipped} photo(s) could not be encoded and were skipped")
        if not jobs:
            raise BatchError("Nothing could be encoded for submission.")
        say(f"Submitted {len(jobs)} batch job(s) covering {encoded_count} photo(s).")
        return jobs

    def _create_job_with_retry(
        self, file_name: str, display_name: str, index: int, say
    ) -> str:
        """Create a job, waiting out a rate limit rather than failing on it.

        A 429 here means the account will not enqueue more work at this
        moment. That may clear in a minute or may not clear until tomorrow,
        so it is retried a few times and then reported honestly rather than
        retried forever.
        """
        last = ""
        for attempt, delay in enumerate((0,) + RETRY_DELAYS):
            if delay:
                say(f"  quota reached; waiting {delay}s before retrying "
                    f"(attempt {attempt} of {len(RETRY_DELAYS)})")
                time.sleep(delay)
            try:
                return self._create_job(file_name, display_name, index)
            except BatchError as exc:
                last = str(exc)
                if "429" not in last and "RESOURCE_EXHAUSTED" not in last:
                    raise
        raise QuotaExhausted(last)

    def _create_job(self, file_name: str, display_name: str, index: int) -> str:
        """Create one batch job from an already-uploaded file."""
        body = {
            "batch": {
                "display_name": f"{display_name}-{index}",
                "input_config": {"file_name": file_name},
            }
        }
        data = self._request(
            f"{API_BASE}/models/{self.model}:batchGenerateContent",
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        job_name = data.get("name") or ""
        if not job_name:
            raise BatchError(f"Batch created but no job name returned: {str(data)[:200]}")
        return job_name

    def _upload_file(self, path: Path, say) -> str:
        """Upload a JSONL file and return its resource name.

        Streams from disk. The previous version built the whole body in
        memory, which for this library meant 3.7 GB of process memory for
        something that was only ever going to be sent once.
        """
        size = path.stat().st_size
        if size > UPLOAD_LIMIT_BYTES:
            # Should be impossible now, but a chunk that slips past the
            # target must fail here with a comprehensible message rather
            # than as an HTTP 413 after a long upload.
            raise BatchError(
                f"Request file is {size/1e9:.2f} GB, over the "
                f"{UPLOAD_LIMIT_BYTES/1e9:.2f} GB upload limit."
            )
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass

        start = urllib.request.Request(
            f"{UPLOAD_BASE}?key={urllib.parse.quote(self.api_key)}",
            data=json.dumps({"file": {"display_name": "photo-organizer-batch"}}).encode(),
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(size),
                "X-Goog-Upload-Header-Content-Type": "application/jsonl",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(start, timeout=self.timeout) as response:
                upload_url = response.headers.get("X-Goog-Upload-URL")
        except urllib.error.HTTPError as exc:
            raise BatchError(
                f"Upload start failed: HTTP {exc.code} "
                f"{exc.read().decode('utf-8','replace')[:200]}"
            ) from None
        if not upload_url:
            raise BatchError("Upload start returned no upload URL.")

        with open(path, "rb") as body:
            finish = urllib.request.Request(
                upload_url,
                data=body,                       # streamed, not read into memory
                headers={
                    "Content-Length": str(size),
                    "X-Goog-Upload-Offset": "0",
                    "X-Goog-Upload-Command": "upload, finalize",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    finish, timeout=max(self.timeout, 3600)
                ) as response:
                    info = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                raise BatchError(
                    f"Upload failed: HTTP {exc.code} "
                    f"{exc.read().decode('utf-8','replace')[:200]}"
                ) from None
        name = (info.get("file") or {}).get("name") or info.get("name")
        if not name:
            raise BatchError(f"Upload returned no file name: {str(info)[:200]}")
        return name

    # -- polling and collection ------------------------------------------

    def poll(self, job_name: str) -> dict:
        return self._request(f"{API_BASE}/{job_name}")

    def state_of(self, job_name: str) -> str:
        data = self.poll(job_name)
        return (data.get("metadata") or {}).get("state") or data.get("state") or "UNKNOWN"

    def wait(
        self,
        job_name: str,
        poll_seconds: int = 30,
        max_wait_seconds: int = 24 * 3600,
        progress: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> str:
        """Poll until the job reaches a terminal state. Returns that state."""
        started = time.monotonic()
        last = ""
        while True:
            if should_cancel is not None and should_cancel():
                return "CANCELLED_LOCALLY"
            state = self.state_of(job_name)
            if state != last:
                last = state
                if progress:
                    progress(f"batch {job_name.split('/')[-1]}: {state}")
            if state in TERMINAL_STATES:
                return state
            if time.monotonic() - started > max_wait_seconds:
                return "TIMED_OUT_LOCALLY"
            # Batch jobs are minutes-to-hours; polling faster achieves
            # nothing but load on a shared service.
            time.sleep(poll_seconds)

    def collect(self, job_name: str) -> BatchResult:
        """Fetch and parse the results of a finished job."""
        data = self.poll(job_name)
        metadata = data.get("metadata") or {}
        state = metadata.get("state") or data.get("state") or "UNKNOWN"
        result = BatchResult(job_name=job_name, state=state)
        if state not in SUCCESS_STATES:
            return result

        dest = metadata.get("output") or metadata.get("response") or {}
        file_name = (
            dest.get("responsesFile")
            or dest.get("responses_file")
            or (dest.get("inlinedResponses") and "INLINE")
        )

        # Small jobs come back inline rather than as a file.
        inlined = dest.get("inlinedResponses") or dest.get("inlined_responses")
        if inlined:
            for entry in inlined.get("inlinedResponses", inlined) if isinstance(inlined, dict) else inlined:
                key = ((entry.get("metadata") or {}).get("key")) or ""
                self._absorb(result, key, entry)
            return result

        if not file_name or file_name == "INLINE":
            result.errors["_job"] = f"No results file in job metadata: {str(metadata)[:200]}"
            return result

        blob = self._request(
            f"{DOWNLOAD_BASE}/{file_name}:download?alt=media", raw=True
        )
        for line in blob.decode("utf-8", "replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = entry.get("key") or ((entry.get("metadata") or {}).get("key")) or ""
            self._absorb(result, key, entry)
        return result

    def _absorb(self, result: BatchResult, key: str, entry: dict) -> None:
        """Turn one result line into an analysis or an error."""
        if not key:
            return
        if "error" in entry and entry["error"]:
            message = entry["error"]
            result.errors[key] = (
                message.get("message") if isinstance(message, dict) else str(message)
            )[:200]
            return
        payload = unwrap_response(entry)
        if payload is None:
            result.errors[key] = "unparseable response"
            return
        result.analyses[key] = PhotoAnalysis.from_model_json(payload, model=self.model)
        result.raw[key] = entry


# What one photo actually costs, measured against a real bill rather than
# assumed from a pricing page.
#
# The first version guessed $0.0004 per image. Reconciled against the
# account: 23 requests (20 interactive, 3 batch) had been billed $0.10, so
# the real rate is about $0.0047 -- TWELVE TIMES the guess. On this library
# that is the difference between quoting $2.75 and quoting $32.
#
# It is a small sample and may include fixed charges, so treat it as an
# order of magnitude, not a price list. What it is definitely not is
# $0.0004. Override with `cost_per_photo_usd` once you have a bill of your
# own to divide.
MEASURED_COST_PER_PHOTO_USD = 0.0047

# For reference, from real replies: 1,491 prompt tokens and ~420 output
# tokens per photo. Kept so the number above can be sanity-checked against
# published per-token pricing when it changes.
TOKENS_PER_PHOTO_IN = 1491
TOKENS_PER_PHOTO_OUT = 420


def estimate_cost_usd(
    images: int,
    batch: bool = True,
    per_photo_usd: float = MEASURED_COST_PER_PHOTO_USD,
) -> float:
    """Approximate cost, from a measured rate rather than a guessed one.

    Deliberately approximate and labelled as such wherever it is shown: the
    purpose is to stop someone submitting 14,000 photos with no sense of the
    bill. The previous guess was twelve times too low, which is exactly the
    failure this is meant to prevent.

    Not rounded. Rounding to cents inside the calculation made "one photo
    pending" and "nothing pending" both read as $0.00, and that is the
    distinction the confirmation dialog turns on.
    """
    return images * per_photo_usd * (0.5 if batch else 1.0)
