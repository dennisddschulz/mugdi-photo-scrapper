"""Content tags, worked out locally and free, for every photo.

WHY THIS EXISTS
---------------
Tags used to come only from the paid analysis, which sees four photos per
event. Measured on the real library after a full run: 2,522 of 13,193 copies
got keywords -- 19%. Covering the rest through the API would cost about $53
at the measured rate, which is not going to happen. This does the same job
locally for nothing.

WHAT IT ASKS
------------
Only CATEGORY questions, never IDENTITY ones. CLIP answered "K2" at 82% for a
forest slope, and a Gemini guess put a summit 13 km from the real one; naming
a specific peak from pixels does not work and is not attempted here. "Is
there snow?" is a different kind of question and it answers it well.

WHAT WAS MEASURED, AND DROPPED
------------------------------
A first vocabulary was scored against 24 hand-labelled photos. Two thirds of
it worked; the rest was removed rather than tuned:

    document / page      2 of 2      kept
    indoors, food        right       kept
    snow                 right       kept
    terrain              mostly right kept
    selfie               1 of 13     DROPPED -- a climber 30 m away scored 1.00
    ski touring          fired on 4 photos with no people at all
    ice vs rock climbing routinely swapped; a climbing gym read "via ferrata"
    animal               1 of 3      -- a blue glove, and pictures on a wall

So: no `selfie`; activities are coarse enough to be right rather than precise
and wrong; and no activity is emitted at all unless people are actually
detected, which is what stopped empty landscapes being tagged "ski touring".

Season and time of day are NOT asked of the model. The timestamp already
knows them exactly, and the light facet declined on all 40 photos it saw.
"""

from __future__ import annotations

import logging
from datetime import datetime

from typing import Optional, Sequence

log = logging.getLogger(__name__)

# A facet is one question with mutually exclusive answers, scored by softmax.
# `None` marks the answer that means "the question does not apply", which is
# what stops a facet inventing an answer for every photo.
#
# Thresholds are per facet because the questions are not equally hard.
FACETS: dict[str, tuple[float, tuple[tuple[Optional[str], str], ...]]] = {
    # Asked first, and gates the activity facet: no people, no activity.
    # Several phrasings share the "people" label and their probabilities are
    # summed. One prompt asking "are there people" missed 9 of 10 photos
    # where the person was a small figure in a big landscape -- which in a
    # mountain library is most of them.
    "people": (0.80, (
        ("people", "a photo with people in it"),
        ("people", "a photo of a small distant person in a huge mountain landscape"),
        ("people", "a photo of a climber on a rock wall"),
        ("people", "a photo of a skier or a walker seen from far away"),
        (None, "an empty landscape with nobody in it"),
        (None, "a close-up photo of an object, food or a printed page"),
    )),
    # Deliberately coarse. Ice versus rock versus via ferrata was measured
    # wrong often enough that the distinction is not worth making.
    # paragliding and cycling were removed: across 24 labelled photos they
    # produced two false positives (a plate of Toblerone read "paragliding")
    # and not one correct answer.
    "activity": (0.60, (
        ("climbing", "a photo of people climbing on rock, ice or a climbing wall"),
        ("climbing", "a photo of a mountaineer on a steep snowy ridge with an axe"),
        ("ski touring", "a photo of people with skis in the snow"),
        ("hiking", "a photo of people walking on a mountain trail with backpacks"),
        (None, "a photo of people standing still, indoors or posing"),
        (None, "a landscape with nobody doing anything"),
    )),
    "terrain": (0.30, (
        ("summit", "a photo taken on a mountain summit looking over other peaks"),
        ("ridge", "a photo of a narrow rocky mountain ridge"),
        ("rock face", "a photo of a steep rock wall"),
        ("glacier", "a photo of a glacier with crevasses"),
        ("lake", "a photo of a mountain lake"),
        ("waterfall", "a photo of a river or a waterfall"),
        ("forest", "a photo of a forest with trees"),
        ("meadow", "a photo of a green alpine meadow with flowers"),
        ("valley", "a photo looking down into a valley"),
        ("hut", "a photo of an alpine hut or mountain refuge building"),
        ("tent", "a photo of a tent at a campsite"),
        ("town", "a photo of a town or city street with buildings"),
        ("indoors", "a photo taken indoors inside a room"),
        # An indoor climbing gym read as "rock face" outdoors, which then
        # let a night-time gym photo be tagged "clear sky".
        ("indoors", "a photo inside an indoor climbing gym with colourful plastic holds"),
        ("sea", "a photo of the sea or a sandy beach"),
    )),
    "subject": (0.40, (
        ("portrait", "a close-up photo of the face of one person"),
        ("food", "a photo of a meal, food or drinks on a table"),
        ("document", "a photograph of a printed page of a book with text"),
        ("screenshot", "a screenshot of a phone or computer screen"),
        (None, "an ordinary photograph of a place or an activity"),
    )),
}

# Independent yes/no questions, each against its own opposite. Conditions are
# not exclusive -- a summit photo can be snowy AND under a clear sky -- and
# forcing one softmax over them made the model pick a winner between two
# facts that were both true.
BINARY: dict[str, tuple[float, str, str]] = {
    "snow": (0.55, "a photo of a landscape covered in snow",
             "a photo of a landscape with no snow"),
    "fog": (0.55, "a photo of mountains in thick fog and low cloud",
            "a photo taken in clear air with good visibility"),
    "clear sky": (0.55, "a photo under a clear blue sky",
                  "a photo under a grey overcast sky"),
}

# Tags that mean "this photo cannot tell anyone where you were".
#
# These are kept out of the PAID sample. A photograph of an IKEA mattress
# label was sent to the API on two consecutive runs and failed both times,
# having taken one of its event's four slots each time. The scenic heuristic
# that was supposed to prevent this does not: measured, it scores an indoor
# fireplace 1.82 and a summit ridge 0.89, because a bright wall reads as sky.
UNPLACEABLE = frozenset({"document", "screenshot", "food", "indoors",
                         "portrait"})


def cannot_place(tags) -> bool:
    """True when nothing in this photo could name a place."""
    return any(tag in UNPLACEABLE for tag in tags)


# Facets that describe the outdoors and mean nothing on a picture of paper.
# Measured: a photograph of a guidebook page scored "rain" at 0.99.
_SUPPRESSED_BY_DOCUMENT = ("activity", "terrain", "people")
_DOCUMENT_TAGS = ("document", "screenshot")

MAX_TAGS = 12

# How sharply the raw cosine similarities are turned into probabilities.
#
# CLIP's own logit scale is 100, and at that scale the softmax saturates:
# every probability is 0.000 or 1.000, so a threshold does nothing at all
# and the answer is whatever argmax says. Measured -- sweeping the people
# threshold from 0.45 to 0.80 changed the score by exactly zero. A lower
# scale keeps the probabilities graded, so "how sure" is a real question.
#
# 30 with people=0.80 and activity=0.60 scored 110/119 on the labelled set.
# It was chosen from the MIDDLE of a plateau (108-110 across scales 25-60
# and thresholds 0.60-0.90), not from the peak, because three knobs tuned on
# 24 photos will otherwise fit the noise in them.
SOFTMAX_SCALE = 30.0


def season_of(when: Optional[datetime]) -> Optional[str]:
    """Northern-hemisphere season from the timestamp.

    Asked of the clock, not the model: it is exact, free, and the light
    facet CLIP was given declined on all 40 photos it saw.
    """
    if when is None:
        return None
    return {12: "winter", 1: "winter", 2: "winter",
            3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer",
            9: "autumn", 10: "autumn", 11: "autumn"}[when.month]


def time_of_day(when: Optional[datetime]) -> Optional[str]:
    if when is None:
        return None
    hour = when.hour
    if hour < 6:
        return "night"
    if hour < 9:
        return "early morning"
    if hour < 18:
        return None          # ordinary daylight is not worth a tag
    if hour < 21:
        return "evening"
    return "night"


def _prompts() -> list[str]:
    """Every prompt, in a fixed order, so the text features can be cached."""
    out: list[str] = []
    for _threshold, entries in FACETS.values():
        out.extend(text for _label, text in entries)
    for _threshold, yes, no in BINARY.values():
        out.extend((yes, no))
    return out


def tags_from_similarity(sims) -> list[str]:
    """Turn one photo's prompt similarities into tags.

    `sims` is the raw similarity of the image against every prompt from
    `_prompts()`, in that order. Kept separate from any model call so it can
    be tested without torch, and so a stored embedding can be re-scored
    against a new vocabulary without touching the drive.
    """
    import numpy as np

    # Raw cosine similarities in, so the temperature is applied here rather
    # than by whatever happened to call this.
    sims = np.asarray(sims, dtype="float64") * SOFTMAX_SCALE
    chosen: dict[str, Optional[str]] = {}
    cursor = 0

    for facet, (threshold, entries) in FACETS.items():
        window = sims[cursor:cursor + len(entries)]
        cursor += len(entries)
        exp = np.exp(window - window.max())
        probs = exp / exp.sum()
        # Probabilities are summed PER LABEL, so a facet can ask the same
        # question several ways. Asking "are there people" once missed 9 of
        # 10 photos where the person was a small figure in a big landscape.
        totals: dict[Optional[str], float] = {}
        for (label, _text), probability in zip(entries, probs):
            totals[label] = totals.get(label, 0.0) + float(probability)
        best = max(totals, key=lambda key: totals[key])
        chosen[facet] = best if (best and totals[best] >= threshold) else None

    binaries: dict[str, bool] = {}
    for name, (threshold, _yes, _no) in BINARY.items():
        pair = sims[cursor:cursor + 2]
        cursor += 2
        exp = np.exp(pair - pair.max())
        binaries[name] = bool((exp / exp.sum())[0] >= threshold)

    # A picture of paper is not a picture of a place. Everything the outdoor
    # facets said about it is noise.
    is_document = chosen.get("subject") in _DOCUMENT_TAGS
    if is_document:
        for facet in _SUPPRESSED_BY_DOCUMENT:
            chosen[facet] = None
        binaries = {}

    # No people, no activity. This is what stopped empty snowy landscapes
    # being tagged "ski touring".
    if chosen.get("people") is None:
        chosen["activity"] = None

    # Weather is an outdoor question. A night photo inside a climbing gym
    # came back "clear sky".
    if chosen.get("terrain") == "indoors":
        binaries = {}

    tags: list[str] = []
    for value in (chosen.get("activity"), chosen.get("terrain"),
                  chosen.get("subject")):
        if value and value not in tags:
            tags.append(value)
    for name, present in binaries.items():
        if present and name not in tags:
            tags.append(name)
    if chosen.get("people") and "portrait" not in tags:
        tags.append("people")
    return tags[:MAX_TAGS]


def describe(sims, when: Optional[datetime] = None) -> list[str]:
    """Tags for one photo: what the model saw, plus what the clock knows."""
    tags = tags_from_similarity(sims)
    if any(t in _DOCUMENT_TAGS for t in tags):
        return tags               # a page has no season worth recording
    for extra in (season_of(when), time_of_day(when)):
        if extra and extra not in tags:
            tags.append(extra)
    return tags[:MAX_TAGS]


# ---------------------------------------------------------------------------
# Running it over a library.
# ---------------------------------------------------------------------------

_text_cache = None


def _text_features():
    """The vocabulary, encoded once per process."""
    global _text_cache
    if _text_cache is not None:
        return _text_cache
    import open_clip
    import torch

    from .pages import MODEL_NAME, _load

    model, _preprocess, _page_text = _load()
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    with torch.no_grad():
        feats = model.encode_text(tokenizer(_prompts()))
        feats /= feats.norm(dim=-1, keepdim=True)
    _text_cache = feats.cpu().numpy()
    return _text_cache


def tag_photo(vector, when=None) -> list[str]:
    """Tags from a stored embedding. No file is opened."""
    import numpy as np

    sims = np.asarray(vector, dtype="float32") @ _text_features().T
    return describe(sims, when)


def tag_library(
    photos: Sequence,
    store,
    on_progress=None,
    should_cancel=None,
) -> dict:
    """Tag every photo, reusing the embedding whenever one is stored.

    Returns {content_key: [tags]}. The encode is the whole cost, so a photo
    already embedded for page detection is tagged for free -- which is why
    the embedding is stored rather than just the page score.
    """
    from .pages import MODEL_NAME, available, embed_path, page_score_from_embedding

    if not available():
        return {}

    out: dict[str, list[str]] = {}
    encoded = reused = 0
    for position, photo in enumerate(photos, start=1):
        if should_cancel is not None and should_cancel():
            break
        if on_progress:
            on_progress(position, len(photos), photo.source_path)

        key = getattr(photo, "content_key", None)
        if not key:
            continue
        if key in out:
            continue

        vector = store.get_embedding(key, MODEL_NAME) if store else None
        if vector is None:
            vector = embed_path(photo.source_path)
            if vector is None:
                continue
            encoded += 1
            if store is not None:
                store.put_embedding(key, MODEL_NAME, vector)
                # Free, now that the embedding exists.
                if store.get_page_score(key) is None:
                    try:
                        store.put_page_score(
                            key, page_score_from_embedding(vector))
                    except Exception:
                        pass
        else:
            reused += 1
        stored = store.get_tags(key) if store is not None else None
        if stored is None:
            stored = tag_photo(vector, getattr(photo, "timestamp", None))
            if store is not None:
                store.put_tags(key, stored)
        out[key] = stored

    log.info("Tagged %d photo(s): %d embedded, %d reused from cache",
             len(out), encoded, reused)
    return out


# ---------------------------------------------------------------------------
# Paperwork: pictures of paper that are not worth keeping in a photo library.
# ---------------------------------------------------------------------------
#
# A guidebook topo is a picture of paper and is precious -- it is what named
# the Aiguille Dibona. A train ticket is a picture of paper and is rubbish.
# Both score the same on the page detector, so they are separated here.
#
# Measured on this library: of 436 photos scoring >=0.85 as printed pages,
# 17 score >=0.90 as paperwork. The twelve most paperwork-like were
# inspected and all twelve were junk -- train tickets, BKW invoices, a
# supermarket discount sticker, pharmacy packaging, tax letters and an IKEA
# mattress label. No guidebook page appeared among them.
PAPERWORK_KEEP = (
    "a page from a climbing guidebook with a route topo drawn on a photo of a cliff",
    "a printed page of a book with paragraphs of text",
    "a topographic map",
)
PAPERWORK_TRASH = (
    "a product label or price tag on packaging in a shop",
    "a paper receipt or invoice",
    "a screenshot of a phone screen",
    "a business card or ticket",
)

# Deliberately high. These photos are COPIED ASIDE for review, never deleted,
# but a guidebook page sent to the review folder is a worse mistake than a
# receipt left in the library, so the bar favours keeping.
PAPERWORK_THRESHOLD = 0.90

_paperwork_cache = None


def _paperwork_features():
    global _paperwork_cache
    if _paperwork_cache is not None:
        return _paperwork_cache
    import open_clip
    import torch

    from .pages import MODEL_NAME, _load

    model, _preprocess, _page_text = _load()
    tokenizer = open_clip.get_tokenizer(MODEL_NAME)
    with torch.no_grad():
        feats = model.encode_text(
            tokenizer(list(PAPERWORK_KEEP) + list(PAPERWORK_TRASH))
        )
        feats /= feats.norm(dim=-1, keepdim=True)
    _paperwork_cache = feats.cpu().numpy()
    return _paperwork_cache


def paperwork_score(vector) -> Optional[float]:
    """How much an already-embedded page is paperwork rather than a topo."""
    try:
        import numpy as np

        sims = SOFTMAX_SCALE * np.asarray(vector, dtype="float32") @ (
            _paperwork_features().T
        )
        exp = np.exp(sims - sims.max())
        probs = exp / exp.sum()
        return float(probs[len(PAPERWORK_KEEP):].sum())
    except Exception as exc:
        log.debug("Could not score paperwork: %s", exc)
        return None


def blacklisted_word(text: str, blacklist) -> Optional[str]:
    """The first blacklisted word in this OCR text, if any.

    Whole words only, case-insensitive. "vat" must not fire on "private",
    and a peak called Coopstock must not fire on "coop".
    """
    if not text or not blacklist:
        return None
    import re

    words = set(re.findall(r"[a-z]+", text.lower()))
    for term in blacklist:
        if term.lower() in words:
            return term
    return None
