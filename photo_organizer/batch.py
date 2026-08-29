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
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise BatchError(f"HTTP {exc.code} from {url.split('?')[0]}: {detail}") from None
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
    ) -> tuple[str, dict[str, str]]:
        """Create a batch job. Returns (job_name, {key: source_path}).

        The key is the photo's content hash, so results map straight back to
        database rows without depending on ordering or on file paths.
        """
        def say(message: str) -> None:
            log.info("%s", message)
            if progress:
                progress(message)

        prepared: list[tuple[str, dict]] = []
        keys: dict[str, str] = {}
        for key, path in items:
            encoded = self.encode_image(path)
            if encoded is None:
                continue
            prepared.append((key, self.build_request(encoded)))
            keys[key] = str(path)
        if not prepared:
            raise BatchError("Nothing could be encoded for submission.")

        payload_size = sum(len(json.dumps(r)) for _k, r in prepared)
        use_file = (
            len(prepared) > INLINE_MAX_REQUESTS or payload_size > INLINE_LIMIT_BYTES
        )
        say(
            f"Submitting {len(prepared)} photo(s) to {self.model} "
            f"({payload_size/1e6:.1f} MB, {'file' if use_file else 'inline'} mode)"
        )

        if use_file:
            file_name = self._upload_jsonl(prepared, say)
            body = {
                "batch": {
                    "display_name": display_name,
                    "input_config": {"file_name": file_name},
                }
            }
        else:
            body = {
                "batch": {
                    "display_name": display_name,
                    "input_config": {
                        "requests": {
                            "requests": [
                                {"request": req, "metadata": {"key": key}}
                                for key, req in prepared
                            ]
                        }
                    },
                }
            }

        url = f"{API_BASE}/models/{self.model}:batchGenerateContent"
        data = self._request(
            url,
            data=json.dumps(body).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        job_name = data.get("name") or ""
        if not job_name:
            raise BatchError(f"Batch created but no job name returned: {str(data)[:200]}")
        say(f"Batch submitted: {job_name}")
        return job_name, keys

    def _upload_jsonl(self, prepared: Sequence[tuple[str, dict]], say) -> str:
        """Upload the requests as a JSONL file and return its resource name."""
        lines = [
            json.dumps({"key": key, "request": req}, ensure_ascii=False)
            for key, req in prepared
        ]
        blob = ("\n".join(lines)).encode("utf-8")
        say(f"Uploading request file ({len(blob)/1e6:.1f} MB)...")

        # Resumable upload: start, then send the bytes.
        start = urllib.request.Request(
            f"{UPLOAD_BASE}?key={urllib.parse.quote(self.api_key)}",
            data=json.dumps({"file": {"display_name": "photo-organizer-batch"}}).encode(),
            headers={
                "X-Goog-Upload-Protocol": "resumable",
                "X-Goog-Upload-Command": "start",
                "X-Goog-Upload-Header-Content-Length": str(len(blob)),
                "X-Goog-Upload-Header-Content-Type": "application/jsonl",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass
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

        finish = urllib.request.Request(
            upload_url,
            data=blob,
            headers={
                "Content-Length": str(len(blob)),
                "X-Goog-Upload-Offset": "0",
                "X-Goog-Upload-Command": "upload, finalize",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(finish, timeout=max(self.timeout, 600)) as response:
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


def estimate_cost_usd(images: int, batch: bool = True) -> float:
    """Rough order-of-magnitude cost, for showing before a run starts.

    Deliberately approximate and labelled as such: token pricing changes and
    image token counts vary. The purpose is to stop someone submitting
    14,000 photos without any sense of the bill, not to predict it exactly.
    """
    # ~300 tokens per 1024px image plus prompt, ~250 output tokens.
    per_image_usd = 0.0004
    # Not rounded here. Rounding to cents inside the calculation made "one
    # photo pending" and "nothing pending" both read as $0.00, and that is
    # exactly the distinction the confirmation dialog turns on. Callers
    # round for display.
    return images * per_image_usd * (0.5 if batch else 1.0)
