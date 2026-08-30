"""Telling a photograph of a printed page from a photograph of a mountain.

This is the detector that four attempts at pixel statistics could not build.
Brightness and edges scored a portrait of a person 3.17 and a real guidebook
page 0.0. Adding saturation still scored the page 0.0. At higher resolution
the "pages" turned out to be climbers on grey granite, because grey rock and
printed paper have the same statistics and a climbing library is full of grey
rock.

CLIP does it perfectly, first try, because it is the right kind of question.
The earlier CLIP failure in this project was asking it to name a summit --
an IDENTITY question, where it answered "K2" at 82% for a forest slope. "Is
this a printed page?" is a CATEGORY question, which is what it is for.

Measured on this library, 400 random photos plus known pages:

    three known guidebook pages      1.000, 1.000, 1.000
    seven random photographs         0.000 to 0.016
    3% of the library scores >= 0.9

and the ten highest-scoring were, on inspection, ten printed pages: eight
climbing topos and two invoices. No false positives at all.

That 3% is the point. It turns reading the library from five hours of OCR
over 13,825 photos into fourteen minutes over four hundred.

Nothing here is asked to identify a place. It only says where the writing is.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

log = logging.getLogger(__name__)

MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"

# What the image is compared against. The first two are pages; the rest are
# the alternatives, and they matter as much -- a softmax needs something to
# be confident against.
PROMPTS = (
    "a photograph of a printed page from a climbing guidebook with text and a route topo",
    "a scanned page of a book with printed text",
    "a photograph of a mountain landscape",
    "a photograph of a person climbing a rock face",
    "a photograph of snow and ice",
    "a photograph of people",
)
PAGE_PROMPTS = 2          # how many of the above count as "a page"

# Measured: pages score 1.000 and photographs 0.000-0.016, so anything in the
# middle of this range is arbitrary. 0.5 sits in an empty gap.
PAGE_THRESHOLD = 0.5

_lock = threading.Lock()
_model = None
_preprocess = None
_text_features = None


@dataclass
class PageScore:
    path: Path
    score: float = 0.0

    @property
    def is_page(self) -> bool:
        return self.score >= PAGE_THRESHOLD


def available() -> bool:
    import importlib.util

    return all(importlib.util.find_spec(name) for name in ("open_clip", "torch"))


def unavailable_reason() -> str:
    return (
        "open_clip and torch are not installed, so photos of guidebook pages "
        "cannot be found. Without them every photo has to be read with OCR, "
        "which takes hours instead of minutes."
    )


def _load():
    """Load the model once, on first use. Takes about six seconds."""
    global _model, _preprocess, _text_features
    with _lock:
        if _model is not None:
            return _model, _preprocess, _text_features
        import open_clip
        import torch

        try:
            import truststore

            truststore.inject_into_ssl()
        except ImportError:
            pass

        log.info("Loading %s for page detection...", MODEL_NAME)
        model, _, preprocess = open_clip.create_model_and_transforms(
            MODEL_NAME, pretrained=PRETRAINED
        )
        model.eval()
        tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        with torch.no_grad():
            features = model.encode_text(tokenizer(list(PROMPTS)))
            features /= features.norm(dim=-1, keepdim=True)
        _model, _preprocess, _text_features = model, preprocess, features
        return _model, _preprocess, _text_features


def embed_path(path: Path):
    """The normalised CLIP embedding of one photo, or None.

    This is the expensive step -- about a second a photo, hours over a
    library -- so callers store the result. Everything else in this module
    and in tags.py is a dot product against it.
    """
    if not available():
        return None
    import torch
    from PIL import Image

    try:
        model, preprocess, _ = _load()
        with Image.open(path) as img:
            # draft() decodes the JPEG at reduced size; CLIP wants 224px
            # anyway, so the full resolution is pure waste.
            img.draft("RGB", (448, 448))
            tensor = preprocess(img.convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            features = model.encode_image(tensor)
            features /= features.norm(dim=-1, keepdim=True)
        return features[0].cpu().numpy()
    except Exception as exc:
        log.debug("Could not embed %s: %s", path, exc)
        return None


def page_score_from_embedding(vector) -> float:
    """How much an already-embedded photo looks like a printed page."""
    import numpy as np
    import torch

    _model, _preprocess, text_features = _load()
    sims = 100.0 * np.asarray(vector, dtype="float32") @ (
        text_features.cpu().numpy().T
    )
    probs = torch.tensor(sims).softmax(dim=-1).numpy()
    return float(probs[:PAGE_PROMPTS].sum())


def score_path(path: Path) -> Optional[float]:
    """How much this photo looks like a printed page, from 0 to 1."""
    vector = embed_path(path)
    if vector is None:
        return None
    try:
        return page_score_from_embedding(vector)
    except Exception as exc:
        log.debug("Could not score %s: %s", path, exc)
        return None


def find_pages(
    photos: Sequence,
    store=None,
    threshold: float = PAGE_THRESHOLD,
    should_cancel: Optional[Callable[[], bool]] = None,
    progress: Optional[Callable[[int, int, Path], None]] = None,
) -> list[PageScore]:
    """The photos that are pictures of writing, highest score first.

    Scores are cached by content hash like everything else: this takes about
    four photos a second, and doing it twice for the same bytes is the same
    mistake as paying twice for the same analysis.
    """
    found: list[PageScore] = []
    candidates = [
        p for p in photos
        if getattr(p, "duplicate_role", None) in (None, "keep")
        and getattr(p, "reject_reason", None) is None
    ] or list(photos)

    for position, photo in enumerate(candidates, start=1):
        if should_cancel is not None and should_cancel():
            break
        if progress:
            progress(position, len(candidates), photo.source_path)

        key = getattr(photo, "content_key", None)
        value = store.get_page_score(key) if (store is not None and key) else None
        if value is None:
            value = score_path(photo.source_path)
            if value is None:
                continue
            if store is not None and key:
                store.put_page_score(key, value)
        if value >= threshold:
            found.append(PageScore(path=photo.source_path, score=value))

    found.sort(key=lambda s: -s.score)
    return found
