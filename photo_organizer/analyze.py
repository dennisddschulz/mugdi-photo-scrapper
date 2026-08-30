"""Analysis stage: submit unseen photos to Gemini, store results, name events.

Replaces the local-model experiments. Those were measured against this
library and none of them worked well enough to keep:

    CLIP ViT-B/32     "K2" at 82% for a forest slope with no mountain in it
    qwen2.5vl:3b      "Mount Everest", high confidence, for an Alpine ice fall
    GeoCLIP           median error 139 km; 0 of 41 photos within 10 km
    SIFT place match  correct in principle, but no repeat visits found

The pipeline is now: analyse each photo once with a large hosted model
against a strict schema, cache the answer in SQLite keyed by content hash,
and build folder names from the cache.

Three things survive from the earlier work because they are not models:

* the peaks gazetteer, which verifies that a claimed summit is real;
* duplicate detection, so the same picture is never paid for twice;
* the rule that a name is a proposal until the user confirms it.
"""

from __future__ import annotations

import logging
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

from .config import Config
from .models import Event, Photo, Plan
from .schema import PhotoAnalysis

# P(this peak name is the right one), given only how it was obtained.
#
# These are the numbers the whole naming decision turns on, so they are
# stated openly rather than buried in a comparison chain. They are informed
# estimates, not measurements from a labelled set -- this library has one
# fully-adjudicated event -- but the ordering is measured, and the gap
# between reading and recognising is deliberately large:
#
#   sign_in_scene   a name bolted to the ground where the camera stood. It
#                   can still be misread, and it can name a neighbouring
#                   feature rather than the subject.
#   printed_page    a guidebook or topo. Usually the right massif; may be a
#                   route photographed for a trip and never climbed.
#   landmark_recog  measured wrong in the one adjudicated case, at high
#                   stated confidence. Local models scored far worse
#                   (CLIP: "K2" for a forest slope). Non-iconic granite and
#                   limestone summits look alike from off-angles.
PEAK_PRIOR = {
    "sign_in_scene": 0.92,
    "printed_page": 0.72,
    "landmark_recognition": 0.28,
    "generic_inference": 0.05,
    "none": 0.02,
}

# The model's own stated confidence barely moves the answer, on purpose. In
# the one case we adjudicated it said "high" and was wrong, while the true
# answer carried no confidence at all. It is a weak signal, not a prior.
CONFIDENCE_NUDGE = {"high": 1.10, "medium": 1.00, "low": 0.90}

# Repeat claims of the same peak within one event are NOT independent: it is
# one model looking at one outing, so a wrong idea recurs. Each additional
# photo naming the same summit therefore counts for less than the last.
CORROBORATION_DECAY = 0.6

# No single photo is ever more than this likely on its own. Signs get
# misread, and photographs of them get cropped.
MAX_SINGLE_CLAIM = 0.95


def peak_probability(claims: Sequence[PhotoAnalysis]) -> float:
    """P(this peak is right), combining an event's claims for one summit.

    Noisy-OR over the individual claims, each discounted for correlation
    with the ones before it. Several weak guesses stay weak; one name read
    off a signboard does not need corroborating.
    """
    def strength(c: PhotoAnalysis) -> float:
        basis = c.evidence_basis
        if basis in ("none", "generic_inference", ""):
            # Naming a summit while reporting no basis for it is incoherent
            # output, not weak output. Score it as what it most likely was:
            # the terrain looked like somewhere.
            basis = "landmark_recognition"
        # Capped below certainty so that corroboration always has somewhere
        # to go. Without a cap under 1, one confident photo saturates and no
        # number of agreeing photos can ever overtake it.
        return min(
            MAX_SINGLE_CLAIM,
            PEAK_PRIOR[basis] * CONFIDENCE_NUDGE.get(c.location_confidence, 1.0),
        )

    strengths = sorted((strength(c) for c in claims), reverse=True)
    miss = 1.0
    for rank, strength in enumerate(strengths):
        miss *= 1.0 - strength * (CORROBORATION_DECAY ** rank)
    return 1.0 - miss


def rank_peaks(
    analyses: Sequence[PhotoAnalysis],
) -> list[tuple[str, float, list[PhotoAnalysis]]]:
    """Every claimed summit in an event, most probable first."""
    # Coordinates are NOT required here. Naming an event and positioning it
    # are different questions, and a peak verified by name with no position
    # can still name a folder.
    groups: dict[str, list[PhotoAnalysis]] = {}
    for a in analyses:
        if a and a.verified_peak:
            groups.setdefault(a.verified_peak, []).append(a)
    ranked = [
        (name, peak_probability(claims), claims) for name, claims in groups.items()
    ]
    # Name, so the order is stable when two peaks score identically.
    ranked.sort(key=lambda r: (-r[1], r[0]))
    return ranked

log = logging.getLogger(__name__)


class AnalysisCancelled(Exception):
    """Raised when the caller asks a running analysis to stop."""


def slug(text: str, max_length: int = 40) -> str:
    """Filesystem-safe ASCII token, matching naming.sanitize_label's rules."""
    import re
    import unicodedata

    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-")
    return text[:max_length].rstrip("-")


@dataclass
class AnalyzeStats:
    events: int = 0
    photos_selected: int = 0
    already_known: int = 0
    submitted: int = 0
    returned: int = 0
    failed: int = 0
    peaks_verified: int = 0
    peaks_rejected: int = 0
    # Recognised summits thrown out because a name written in the event's
    # own photos put them somewhere else.
    peaks_contradicted: int = 0
    # Summits taken straight from text in the frame rather than recognition.
    peaks_from_text: int = 0
    personal_documents: int = 0
    # Frames set aside as empty: flat ones caught before analysis,
    # pocket shots caught by it. Copied to _rejected_review/, never
    # deleted.
    empty_frames: int = 0
    pocket_shots: int = 0
    # Duplicate groups whose keeper changed once the photos had
    # actually been looked at, rather than ranked by file size.
    better_duplicate_chosen: int = 0
    named_from_peak: int = 0
    named_from_crag: int = 0
    named_from_region: int = 0
    named_from_activity: int = 0
    still_unknown: int = 0
    located_events: int = 0
    location_disagreed: int = 0
    estimated_cost_usd: float = 0.0
    job_name: str = ""
    state: str = ""

    def to_dict(self) -> dict:
        return self.__dict__.copy()


def select_photos(
    event: Event,
    per_event: int,
    prefer_scenic: bool = True,
    include_duplicates: bool = False,
) -> list[Photo]:
    """Choose which photos of an event are worth analysing.

    Duplicates are skipped -- analysing the same picture twice costs money
    and tells us nothing new. The sample is spread through the event rather
    than taken from the front, because the first frames of an outing are
    usually the car park.
    """
    usable = [
        p for p in event.photos
        # Duplicates are normally skipped -- paying thirty times to learn what
        # one frame says is the whole point of finding them. They ARE included
        # when the caller intends to choose between them on photographic
        # merit, which needs every candidate looked at.
        if (include_duplicates
            or getattr(p, "duplicate_role", None) in (None, "keep"))
        and getattr(p, "reject_reason", None) is None
    ] or event.photos
    if not usable:
        return []
    # 0 means every photo. Each one is analysed once in its life and cached
    # forever, so full coverage costs a few dollars once rather than a
    # sampling decision that has to be revisited whenever a name is missed.
    if per_event <= 0 or len(usable) <= per_event:
        return list(usable)

    if prefer_scenic and any(p.scenic_score for p in usable):
        # Choose, do not spread. Only about 27% of this library shows a
        # placeable skyline, so an even sample through an event spends most
        # of the budget on close-ups of gear and hands that could never name
        # anything. The score comes free from the duplicate pass.
        #
        # Ties break on time so the choice is stable between runs.
        ranked = sorted(
            usable,
            key=lambda p: (-p.scenic_score, p.timestamp or datetime.min),
        )
        return ranked[:per_event]

    step = len(usable) / per_event
    return [usable[int(i * step)] for i in range(per_event)]


def _verify_against_gazetteer(
    analysis: PhotoAnalysis, peak_index, countries: Sequence[str]
) -> PhotoAnalysis:
    """Check a claimed summit against real ones, and take its geography.

    The model names peaks well and places them badly: measured, it named
    "Monte Oddeu" correctly but put it in the Balearic Islands (Sardinia),
    and "Cima Groste" correctly but put it in Valais (Brenta Dolomites).
    So the name comes from the model and the coordinates from the gazetteer.

    Note the limit honestly: this proves a name is REAL, not that it is the
    RIGHT one. A plausible neighbouring summit in the right country passes.
    """
    if not analysis.peak_name or peak_index is None or not len(peak_index):
        return analysis
    matches = peak_index.match(analysis.peak_name, countries=countries or None, limit=1)
    if matches:
        summit = matches[0].peak
        analysis.verified_peak = summit.name
        analysis.verified_lat = summit.lat
        analysis.verified_lon = summit.lon
        analysis.verified_country = summit.country
    else:
        analysis.rejected_peak = analysis.peak_name
        analysis.peak_name = None
    return analysis


def text_anchors(
    analyses: Sequence[PhotoAnalysis],
    peak_index,
    countries: Sequence[str] = (),
) -> list[tuple[str, float, float]]:
    """Real places named verbatim in an event's transcribed text.

    A name written down somewhere in the event -- on a signboard, a hut, a
    guidebook heading -- is the hardest evidence available, because it does
    not depend on a model recognising anything. It only has to be read.
    """
    if peak_index is None or not len(peak_index):
        return []
    found: list[tuple[str, float, float]] = []
    seen: set[str] = set()
    for a in analyses:
        if not a or not a.safe_text:
            continue
        for peak in peak_index.names_in_text(a.safe_text, countries=countries or None):
            if peak.name not in seen:
                seen.add(peak.name)
                found.append((peak.name, peak.lat, peak.lon))
    return found


def promote_text_anchors(
    analyses: Sequence[PhotoAnalysis],
    peak_index,
    countries: Sequence[str] = (),
) -> int:
    """Turn a real place name found in transcribed text into a peak claim.

    Without this the strongest evidence in an event can be sitting unused
    in a string field. In the one measured failure the model transcribed a
    guidebook heading reading "Furka | Galengrat - Hannibalturm" but left
    `peak_name` null, and separately recognised "Salbitschijen" 13 km away
    from a different photo. The right answer was already in the event; it
    just was not in a field anything looked at.

    A promoted name never overwrites one the model asserted itself.
    """
    if peak_index is None or not len(peak_index):
        return 0
    promoted = 0
    for a in analyses:
        if not a or a.verified_peak or not a.safe_text:
            continue
        hits = peak_index.names_in_text(a.safe_text, countries=countries or None)
        if not hits:
            continue
        peak = hits[0]
        a.peak_name = peak.name
        a.verified_peak = peak.name
        a.verified_lat = peak.lat
        a.verified_lon = peak.lon
        a.verified_country = peak.country or a.verified_country
        if a.evidence_basis not in ("sign_in_scene", "printed_page"):
            # It was read, whatever the model thought it was doing.
            a.evidence_basis = "printed_page" if a.is_guidebook_page else "sign_in_scene"
        log.info("promoted %s, read from text in the frame", peak.name)
        promoted += 1
    return promoted


def reject_contradicted_peaks(
    analyses: Sequence[PhotoAnalysis],
    anchors: Sequence[tuple[str, float, float]],
    max_km: float = 30.0,
) -> int:
    """Drop recognised summits that contradict a name written in the frame.

    This is the check that would have caught the one measured failure. For
    an event at the Hannibalturm (Furkapass), the model recognised
    "Salbitschijen" -- real, in the right country, in the right massif, and
    13 km away. Nothing in the gazetteer could refuse it, because the
    gazetteer only knows the name exists. What refuses it is the guidebook
    heading in another photo of the same event, which names a summit 13 km
    from the claim.

    Only recognised peaks are dropped. A name that was read is never
    overruled by a name that was inferred.
    """
    if not anchors:
        return 0
    from .geo import haversine_km

    dropped = 0
    for a in analyses:
        if not a or not a.verified_peak or a.verified_lat is None:
            continue
        if a.evidence_basis in ("sign_in_scene", "printed_page"):
            continue
        if any(a.verified_peak == name for name, _, _ in anchors):
            continue
        nearest = min(
            haversine_km(a.verified_lat, a.verified_lon, lat, lon)
            for _, lat, lon in anchors
        )
        if nearest > max_km:
            log.info(
                "rejecting recognised peak %s: %.0f km from %s named in the "
                "event's own text",
                a.verified_peak, nearest, anchors[0][0],
            )
            a.rejected_peak = a.verified_peak
            a.peak_name = None
            a.verified_peak = None
            a.verified_lat = None
            a.verified_lon = None
            dropped += 1
    return dropped


@dataclass
class EventLocation:
    """One location for a whole event, agreed across its photos.

    A single photo's estimate is not evidence -- measured on this library, a
    hosted model placed Swiss photos in California and a Sardinian trip in
    Provence. Several photos of the same outing landing in the same place
    is evidence, and photos that disagree are the signal that there is no
    answer rather than a reason to pick one.
    """

    lat: float
    lon: float
    source: str                 # "gazetteer" | "consensus"
    agreeing: int = 0           # photos inside the agreement radius
    considered: int = 0         # photos that offered any estimate
    spread_km: float = 0.0      # radius of the agreeing cluster
    peak: Optional[str] = None
    probability: float = 0.0    # P(the peak is right), when one was used

    @property
    def agreement(self) -> float:
        return self.agreeing / self.considered if self.considered else 0.0

    def describe(self) -> str:
        return (
            f"{self.lat:.4f},{self.lon:.4f} from {self.source} "
            f"({self.agreeing}/{self.considered} photos agree "
            f"within {self.spread_km:.0f} km)"
        )


def consensus_location(
    analyses: Sequence[PhotoAnalysis],
    agreement_km: float = 25.0,
    min_agreeing: int = 2,
    min_fraction: float = 0.4,
    min_probability: float = 0.5,
) -> Optional[EventLocation]:
    """The most defensible single position for an event, or None.

    Verified summits win outright: their coordinates come from the
    gazetteer, so they are facts rather than estimates. Otherwise the
    largest cluster of mutually-agreeing estimates is taken and everything
    outside it discarded -- which is what stops one wild guess dragging an
    otherwise sound average across a border.

    Returns None when the photos do not agree. That is a real answer, and a
    better one than a confident average of contradictory guesses.
    """
    from .geo import haversine_km, medoid

    usable = [a for a in analyses if a is not None]
    if not usable:
        return None

    # --- gazetteer-verified summits are not estimates -------------------
    verified = [
        a for a in usable
        if a.verified_peak and a.verified_lat is not None
    ]
    if verified:
        # The same ranking summarise_event uses. It has to be the same, or
        # an event could be foldered under one summit while the GPS written
        # into its files pointed at another.
        ranked = rank_peaks(verified)
        best_name, probability, claims = ranked[0]
        if probability >= min_probability:
            chosen = max(claims, key=lambda a: PEAK_PRIOR.get(a.evidence_basis, 0.0))
            return EventLocation(
                lat=chosen.verified_lat,
                lon=chosen.verified_lon,
                source="gazetteer",
                agreeing=len(claims),
                considered=len(usable),
                spread_km=0.0,
                peak=best_name,
                probability=probability,
            )
        # Too weak to write into the files as a fact. Fall through to the
        # estimate consensus below, which may still find something.

    # --- otherwise, find the largest agreeing cluster --------------------
    points = [
        (a.latitude, a.longitude)
        for a in usable
        if a.latitude is not None and a.longitude is not None
        and -90 <= a.latitude <= 90 and -180 <= a.longitude <= 180
    ]
    if not points:
        return None

    best_cluster: list[tuple[float, float]] = []
    for seed in points:
        cluster = [
            p for p in points
            if haversine_km(seed[0], seed[1], p[0], p[1]) <= agreement_km
        ]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster

    # One photo agreeing with itself is not a consensus.
    if len(best_cluster) < min_agreeing:
        return None
    if len(best_cluster) / len(points) < min_fraction:
        return None

    centre = medoid(best_cluster)
    if centre is None:
        return None
    spread = max(
        (haversine_km(centre[0], centre[1], p[0], p[1]) for p in best_cluster),
        default=0.0,
    )
    return EventLocation(
        lat=centre[0],
        lon=centre[1],
        source="consensus",
        agreeing=len(best_cluster),
        considered=len(points),
        spread_km=spread,
    )


def summarise_event(
    event: Event,
    analyses: Sequence[PhotoAnalysis],
    min_probability: float = 0.5,
) -> Optional[PhotoAnalysis]:
    """Combine an event's photo analyses into one answer.

    The summit with the highest probability of being right wins, and if
    even that one falls below `min_probability` the event is named by its
    region instead. Otherwise the most frequently agreed range, region and
    country are used: several photos agreeing is a much stronger claim than
    one photo asserting.
    """
    usable = [a for a in analyses if a is not None]
    if not usable:
        return None

    merged = PhotoAnalysis()
    ranked = rank_peaks(usable)
    if ranked:
        best_name, probability, claims = ranked[0]
        best = max(claims, key=lambda a: PEAK_PRIOR.get(a.evidence_basis, 0.0))
        merged.peak_name = best.peak_name
        merged.verified_peak = best.verified_peak
        merged.verified_lat = best.verified_lat
        merged.verified_lon = best.verified_lon
        merged.verified_country = best.verified_country
        merged.evidence_basis = best.evidence_basis
        merged.location_confidence = best.location_confidence
        merged.peak_agreement = len(claims)
        merged.peak_considered = len(usable)
        merged.peak_probability = probability
        if len(ranked) > 1:
            merged.runner_up_peak = ranked[1][0]
            merged.runner_up_probability = ranked[1][1]

        # Below the floor the event is named by region instead. A summit
        # nobody can corroborate and nobody read anywhere is a guess, and a
        # guess in a folder name is indistinguishable from a fact once the
        # library has been browsed for a year.
        if probability < min_probability:
            log.info(
                "peak %s scored %.0f%%, below the %.0f%% floor; naming by "
                "region instead", best_name, probability * 100,
                min_probability * 100,
            )
            merged.rejected_peak = best_name
            merged.peak_name = None
            merged.verified_peak = None
            merged.verified_lat = None
            merged.verified_lon = None

    def most_common(values) -> Optional[str]:
        counts: dict[str, int] = {}
        for value in values:
            if value:
                counts[value] = counts.get(value, 0) + 1
        return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None

    merged.crag_name = most_common([a.crag_name for a in usable])
    merged.mountain_range = most_common([a.mountain_range for a in usable])
    merged.region = most_common([a.region for a in usable])
    merged.locality = most_common([a.locality for a in usable])
    merged.country = most_common([a.country for a in usable])
    merged.country_code = most_common([a.country_code for a in usable])
    merged.season = most_common([a.season for a in usable]) or "unknown"
    # An activity has to be the plurality answer, not merely present once.
    activities = [a.activity for a in usable if a.activity not in ("unknown", "none")]
    merged.activity = most_common(activities) or "unknown"
    merged.keywords = sorted({k for a in usable for k in a.keywords})[:12]
    return merged


def apply_to_event(
    event: Event,
    merged: PhotoAnalysis,
    config: Config,
    location: Optional[EventLocation] = None,
) -> str:
    """Write the summary onto the event and build its folder name.

    `location` is the event's agreed position. It is authoritative: one
    photo's own estimate is never used, because a single estimate has been
    measured to be unreliable (Swiss photos placed in California) while
    several photos agreeing is real evidence.
    """

    naming = config.naming
    event.activity = merged.activity if merged.activity != "unknown" else None
    event.mountain_range = merged.mountain_range
    event.region = merged.region
    event.country = merged.country
    event.country_code = merged.country_code or merged.verified_country
    event.tag_summary = [(k, 1.0) for k in merged.keywords[:5]]

    # The event's position, or nothing. Never a single photo's guess.
    if location is not None:
        event.enriched_lat = location.lat
        event.enriched_lon = location.lon
        event.evidence.append(f"location: {location.describe()}")
    else:
        # The photos disagreed, so there is no position for this event. No
        # GPS is better than a confident average of contradictory guesses.
        event.enriched_lat = None
        event.enriched_lon = None

    if merged.verified_peak:
        event.place_name = merged.verified_peak
        event.country_code = merged.verified_country or event.country_code
        runner_up = (
            f"; next best {merged.runner_up_peak} "
            f"{merged.runner_up_probability * 100:.0f}%"
            if merged.runner_up_peak
            else ""
        )
        event.evidence.append(
            f"peak: {merged.verified_peak} "
            f"({merged.peak_probability * 100:.0f}% likely; "
            f"named by {merged.peak_agreement} of {merged.peak_considered} "
            f"photos analysed via {merged.evidence_basis}"
            f"{runner_up})"
        )
    elif merged.rejected_peak:
        event.evidence.append(
            f"peak {merged.rejected_peak} suggested but only "
            f"{merged.peak_probability * 100:.0f}% likely -- naming by region "
            "instead"
        )
    elif merged.crag_name:
        event.place_name = merged.crag_name
        event.evidence.append(f"crag: {merged.crag_name}")

    parts: list[str] = []
    if naming.include_country and event.country_code:
        parts.append(slug(event.country_code))
    region = event.mountain_range or event.region
    if naming.include_region and region:
        parts.append(slug(region))

    if event.place_name:
        parts.append(slug(event.place_name))
        source = "peak" if merged.verified_peak else "crag"
    elif parts:
        source = "region"
    elif event.activity:
        source = "activity"
    else:
        return "none"

    # The activity is always the last token, so a folder says both where and
    # what: Urner-Alps_Hannibalturm_alpine-climbing_01_09. It is dropped only
    # when it would repeat a word already in the name.
    if naming.include_activity and event.activity:
        activity = slug(event.activity)
        if activity.lower() not in "_".join(parts).lower():
            parts.append(activity)

    name = "_".join(p for p in parts if p)[: naming.max_name_length]
    if name:
        start = event.start
        event.proposed_name = f"{name}{f'_{start:%d_%m}' if start else ''}"[
            : naming.max_name_length
        ]
        event.name_source = source
        return source
    return "none"


def looks_like_a_pocket_shot(a: PhotoAnalysis) -> bool:
    """Is this a frame the camera took by accident?

    Judged from the model's reading, not from pixels. Pixel statistics were
    tried and abandoned: on 1,500 real photos a "dark and low-detail" rule
    flagged eleven frames of which TEN were photographs -- a night food
    truck, a campfire, a snowy road at dusk, a climb inside a cave. A pocket
    shot and a night photograph have the same statistics.

    Every condition must hold, and any sign of content rescues the frame.
    The cost of being wrong here is asymmetric: a missed pocket shot is a
    file in a folder, a wrong rejection is a photograph the user has to go
    looking for.
    """
    if a is None:
        return False
    # Anything the model could actually name is a photograph.
    if a.peak_name or a.crag_name or a.verified_peak or a.landmarks:
        return False
    if a.place_names_visible or a.visible_text or a.caption and len(a.caption) > 60:
        return False
    if a.people_count:
        return False
    if a.activity not in ("none", "unknown"):
        return False
    if a.scene not in ("other", "unknown"):
        return False
    return (
        a.sharpness == "blurry"
        and (a.aesthetic_score is not None and a.aesthetic_score <= 1)
    )


def pending_cost(
    plan: Plan, config: Config, store=None, sample: int = 0
) -> dict:
    """How many photos still need analysing, and what that will cost.

    Only photos absent from the cache count. Quoting the whole library on a
    re-run is wrong by the entire cached amount, and wrong in the direction
    that discourages re-running something free.

    `sample` bounds the work. Photos whose key the duplicate pass already
    computed are free to check, but before that pass has run the key has to
    be read off the disk, and hashing 14,000 files takes over two minutes --
    far too slow for a check whose whole purpose is to be instant. With a
    sample, the cached fraction is measured on that many photos and applied
    to the rest, and the result says it was estimated.
    """
    from .batch import estimate_cost_usd
    from .db import AnalysisStore
    from .dedupe import content_hash

    settings = config.analysis
    store = store or AnalysisStore(Path(settings.database_path).expanduser())

    chosen: list = []
    for event in plan.events:
        chosen.extend(select_photos(event, settings.photos_per_event))

    # Anything already keyed is free to check. Only the rest costs disk.
    unkeyed = [p for p in chosen if not p.content_key]
    budget = sample if sample > 0 else len(unkeyed)
    hashed_now = 0
    for photo in unkeyed[:budget]:
        photo.content_key = content_hash(photo.source_path, photo.size_bytes)
        hashed_now += 1
    sampled = len(unkeyed) > budget

    keys = [p.content_key for p in chosen if p.content_key]
    unique = list(dict.fromkeys(keys))
    known = store.get_many(unique)
    pending_known = len(unique) - len(known)

    if sampled and unique:
        # Scale what was measured up to the whole selection. The cache is
        # keyed by content, so the fraction is representative.
        fraction = pending_known / len(unique)
        total = len(chosen)
        pending_count = int(round(total * fraction))
        already = total - pending_count
    else:
        total = len(unique)
        pending_count = pending_known
        already = len(known)

    return {
        "selected": total,
        "already_analysed": already,
        "pending": pending_count,
        "estimated_cost_usd": estimate_cost_usd(
            pending_count, batch=settings.use_batch,
            per_photo_usd=settings.cost_per_photo_usd,
        ),
        "hashed_now": hashed_now,
        "sampled": sampled,
    }


@dataclass
class Check:
    """One preflight result. Ordered so failures can be reported first."""

    name: str
    ok: bool
    detail: str = ""
    fatal: bool = True

    def to_dict(self) -> dict:
        return {"name": self.name, "ok": self.ok, "detail": self.detail,
                "fatal": self.fatal}


def preflight(
    plan: Plan,
    config: Config,
    store=None,
    check_api: bool = True,
) -> list[Check]:
    """Everything that can be known before the expensive work starts.

    Runs in about a second. The first run of this pipeline spent 85 minutes
    encoding photos and then failed on an upload limit that was entirely
    predictable from the photo count -- this exists so that never happens
    again.
    """
    from .batch import CHUNK_TARGET_BYTES
    from .db import AnalysisStore

    settings = config.analysis
    checks: list[Check] = []

    # State the setting that decides the size of the bill. A run once cost
    # ten times what had been promised because this value lived in a config
    # file that disagreed with the code default, and nothing said so.
    checks.append(Check(
        "Photos analysed per event", True,
        (f"{settings.photos_per_event} chosen per event"
         if settings.photos_per_event
         else "EVERY photo -- this is the expensive setting")
        + (f", from {config.loaded_from}" if config.loaded_from else ", built-in default"),
        fatal=False,
    ))

    # --- somewhere to write ---------------------------------------------
    output = Path(plan.output_root)
    try:
        output.mkdir(parents=True, exist_ok=True)
        probe = output / ".photo-organizer-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        checks.append(Check("Output folder is writable", True, str(output)))
    except OSError as exc:
        checks.append(Check("Output folder is writable", False, f"{output}: {exc}"))

    # --- is there already a library in there? ----------------------------
    # A previous run's output does not stop this one, but it changes what you
    # get: this run writes correctly-named folders BESIDE the old ones rather
    # than replacing them, so the result is two libraries in one folder.
    # Measured after one such run: 13,825 files and 46.7 GB of leftovers.
    #
    # This warns rather than deletes. The tool does not remove photographs,
    # not even copies of them -- deleting the output is the user's decision
    # and their recovery story.
    try:
        existing = [p for p in output.rglob("*") if p.is_file()]
        if existing:
            size = sum(p.stat().st_size for p in existing)
            stale = sum(
                1 for d in output.iterdir()
                if d.is_dir() and d.name.startswith(("_duplicates", "_rejected"))
            )
            checks.append(Check(
                "Output folder is empty", False,
                f"{len(existing):,} file(s), {size/1e9:.1f} GB already in "
                f"{output}. This run will add correctly-named folders BESIDE "
                f"them, not replace them. Delete the folder first for a clean "
                f"library; nothing in your source is affected."
                + (f" ({stale} review folder(s) present.)" if stale else ""),
                fatal=False,
            ))
    except OSError as exc:
        log.debug("Could not inspect the output folder: %s", exc)

    # --- room for the copy ----------------------------------------------
    try:
        import shutil as _shutil

        need = sum(p.size_bytes or 0 for p in plan.photos)
        free = _shutil.disk_usage(output).free
        ok = free > need * 1.05
        checks.append(Check(
            "Enough disk space", ok,
            f"{need/1e9:.1f} GB needed, {free/1e9:.1f} GB free",
        ))
    except OSError as exc:
        checks.append(Check("Enough disk space", False, str(exc), fatal=False))

    # --- what is left to analyse, and what it will cost ------------------
    store = store or AnalysisStore(Path(settings.database_path).expanduser())
    costing = pending_cost(plan, config, store, sample=400)
    pending = costing["pending"]
    cost = costing["estimated_cost_usd"]
    within_budget = cost <= settings.max_cost_usd
    checks.append(Check(
        "Cost is within the budget", within_budget,
        f"{pending} photo(s) to analyse, {costing['already_analysed']} already "
        f"cached: about ${cost:.2f}, budget ${settings.max_cost_usd:.2f}"
        + (" (estimated from a sample)" if costing.get("sampled") else "")
        + ("" if within_budget else
           f". Reduce photos_per_event (currently "
           f"{settings.photos_per_event or 'every photo'}) or raise "
           f"max_cost_usd if you mean to spend this."),
    ))

    # --- will the upload fit? THE check that was missing ------------------
    # Measured on the real library: 261 KB per photo once encoded.
    approx_bytes = pending * 261_000
    chunks = max(1, -(-approx_bytes // CHUNK_TARGET_BYTES)) if pending else 0
    checks.append(Check(
        "Upload fits inside the API limit", True,
        f"about {approx_bytes/1e9:.2f} GB in {chunks} batch job(s), "
        f"each under {CHUNK_TARGET_BYTES/1e9:.2f} GB",
        fatal=False,
    ))

    # --- can we actually talk to Gemini? ---------------------------------
    if pending == 0:
        checks.append(Check("Gemini API key", True, "nothing to analyse", fatal=False))
    elif not settings.api_key_resolved:
        checks.append(Check(
            "Gemini API key", False,
            "not set. Put GEMINI_API_KEY in a .env file beside the program.",
        ))
    elif check_api:
        key_check = _check_api_key(settings.api_key_resolved)
        checks.append(key_check)
        # Listing models is free and works on the free tier, so it proves
        # only that the key exists. What matters is whether a BATCH JOB can
        # be created, and that is a different quota entirely.
        if key_check.ok:
            checks.append(_check_batch_quota(plan, config, store))
    else:
        checks.append(Check("Gemini API key", True, "present (not verified)",
                            fatal=False))

    # --- the gazetteer, which only affects name quality -------------------
    if settings.use_gazetteer:
        try:
            from .peaks import PeakIndex

            index = PeakIndex.load()
            checks.append(Check(
                "Peaks gazetteer", bool(len(index)),
                f"{len(index):,} named peaks and landforms"
                if len(index) else
                "empty -- run --build-gazetteer; summit names cannot be verified",
                fatal=False,
            ))
        except Exception as exc:
            checks.append(Check("Peaks gazetteer", False, str(exc), fatal=False))

    return checks


def _check_batch_quota(plan: Plan, config: Config, store) -> Check:
    """Can a batch job actually be created? Submit one photo and find out.

    The only honest way to know. The account quota for creating batch jobs
    is separate from the one that lets you list models, and a run that
    discovers the difference does so after encoding the whole library.

    The photo is real and its result is cached like any other, so nothing is
    wasted. The job is recorded before this returns, so it is collected on
    the next run rather than orphaned.
    """
    from .batch import GeminiBatch

    settings = config.analysis
    candidate = None
    for event in plan.events:
        for photo in select_photos(event, 1):
            candidate = photo
            break
        if candidate is not None:
            break
    if candidate is None:
        return Check("Batch quota", True, "no photo to test with", fatal=False)

    client = GeminiBatch(settings.api_key_resolved, model=settings.model)
    key = candidate.content_key or ""
    if not key:
        from .dedupe import content_hash

        key = content_hash(candidate.source_path, candidate.size_bytes) or ""
        candidate.content_key = key

    try:
        jobs = client.submit(
            [(key, candidate.source_path)], display_name="photo-organizer-preflight"
        )
    except Exception as exc:
        message = str(exc)
        if "429" in message or "RESOURCE_EXHAUSTED" in message:
            return Check(
                "Batch quota", False,
                "the account cannot create batch jobs: quota exhausted. "
                "Check billing at https://ai.dev/rate-limit -- listing models "
                "works on the free tier, creating a batch job does not.",
            )
        return Check("Batch quota", False, f"a one-photo batch was refused: {message[:200]}")

    # It worked. Keep the job: one photo already paid for, collected later.
    for job_name, keys in jobs:
        try:
            store.remember_job(job_name, settings.model, keys)
        except Exception as exc:
            log.debug("Could not record the preflight job: %s", exc)
    return Check(
        "Batch quota", True,
        f"a one-photo batch was accepted ({jobs[0][0].split('/')[-1]})",
    )


def _check_api_key(api_key: str) -> Check:
    """Is the key real? A free call, so the answer costs nothing.

    Worth a second: the alternative is discovering it after the scan, the
    clustering, the duplicate pass and forty minutes of encoding.
    """
    import json as _json
    import urllib.error
    import urllib.request

    from .batch import API_BASE

    try:
        import truststore

        truststore.inject_into_ssl()
    except ImportError:
        pass
    request = urllib.request.Request(
        f"{API_BASE}/models", headers={"x-goog-api-key": api_key}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            models = _json.load(response).get("models") or []
        return Check("Gemini API key", True, f"valid, {len(models)} models available")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:160]
        return Check("Gemini API key", False, f"rejected: HTTP {exc.code} {detail}")
    except Exception as exc:
        # A network problem is not a bad key, and refusing to start over it
        # would be wrong. Say so and let the run decide.
        return Check("Gemini API key", True, f"could not verify ({exc})", fatal=False)


def analyze_plan(
    plan: Plan,
    config: Config,
    store=None,
    on_step: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
    wait_for_batch: bool = True,
    duplicate_groups: Optional[Sequence] = None,
    on_event_named: Optional[Callable[[Event], None]] = None,
) -> AnalyzeStats:
    """Analyse the plan's photos and rewrite event names from the results.

    `duplicate_groups`, when given, has its keeper re-chosen from what the
    model saw rather than from file size -- which is a different question
    and usually a different answer.
    """
    from .batch import SUCCESS_STATES, GeminiBatch, estimate_cost_usd
    from .db import AnalysisStore
    from .dedupe import content_hash
    from .peaks import PeakIndex

    settings = config.analysis
    stats = AnalyzeStats()

    def say(message: str) -> None:
        log.info("%s", message)
        if on_step:
            on_step(message)

    def check_cancel() -> None:
        if should_cancel is not None and should_cancel():
            raise AnalysisCancelled()

    store = store or AnalysisStore(Path(settings.database_path).expanduser())
    peak_index = PeakIndex.load() if settings.use_gazetteer else None
    if peak_index is not None and not len(peak_index):
        say("Gazetteer empty; run --build-gazetteer to enable peak verification.")
        peak_index = None

    targets = [e for e in plan.events if not e.place_label]
    stats.events = len(targets)
    if not targets:
        say("Every event already has a name from GPS; nothing to analyse.")
        return stats

    # --- collect anything already paid for -------------------------------
    # Batches submitted in an earlier session are still running on Google's
    # side and are recorded in the database. Collect them FIRST: they are
    # bought and paid for, and anything they cover must not be submitted
    # again. This is why the job table exists, and for a while nothing
    # actually called it -- so 1,328 paid-for results sat stranded.
    try:
        reclaimed = collect_open_jobs(plan, config, store=store, on_step=say)
        if reclaimed.returned:
            say(f"Collected {reclaimed.returned} result(s) from earlier batches.")
    except Exception as exc:
        # Never let this stop a run: the worst case is paying again, and
        # stopping means not running at all.
        log.warning("Could not collect earlier batches: %s", exc)
        say(f"Could not collect earlier batches ({exc}); continuing.")

    # --- what needs analysing -------------------------------------------
    chosen: list[tuple[str, Photo]] = []
    for event in targets:
        for photo in select_photos(event, settings.photos_per_event,
                                   include_duplicates=settings.judge_duplicates):
            # Reuse the key the duplicate pass already computed. Re-reading
            # 14,000 files off an external drive to learn what we were just
            # told is minutes of pure waste.
            digest = photo.content_key or content_hash(
                photo.source_path, photo.size_bytes
            )
            if digest:
                photo.content_key = digest
                chosen.append((digest, photo))
    stats.photos_selected = len(chosen)

    by_hash = {digest: photo for digest, photo in chosen}
    known = store.get_many(by_hash)
    stats.already_known = len(known)
    pending = [(d, p) for d, p in chosen if d not in known]
    # De-duplicate: two events can select the same file only if it is a
    # duplicate, but the same hash must never be submitted twice.
    seen: set[str] = set()
    pending = [(d, p) for d, p in pending if not (d in seen or seen.add(d))]

    say(
        f"{len(chosen)} photo(s) selected from {len(targets)} event(s); "
        f"{stats.already_known} already analysed, {len(pending)} to submit."
    )

    if pending and settings.max_photos_per_run:
        if len(pending) > settings.max_photos_per_run:
            say(
                f"Limiting this run to {settings.max_photos_per_run} photo(s) "
                f"of {len(pending)}. Re-run to continue where it left off."
            )
            pending = pending[: settings.max_photos_per_run]

    stats.estimated_cost_usd = estimate_cost_usd(
        len(pending), batch=settings.use_batch,
        per_photo_usd=settings.cost_per_photo_usd,
    )

    # --- submit ----------------------------------------------------------
    if pending:
        if not settings.api_key_resolved:
            say(
                "No Gemini API key. Set GEMINI_API_KEY, or analysis.gemini_api_key "
                "in the config. Nothing was submitted."
            )
            return stats

        check_cancel()
        client = GeminiBatch(settings.api_key_resolved, model=settings.model)
        say(
            f"Submitting {len(pending)} photo(s). Estimated cost "
            f"~${stats.estimated_cost_usd:.2f} at batch rates (half interactive). "
            "These images are uploaded to Google."
        )
        try:
            jobs = client.submit(
                [(d, p.source_path) for d, p in pending],
                progress=say,
                should_cancel=should_cancel,
            )
        except Exception as exc:
            say(f"Batch submission failed: {exc}")
            return stats

        # Recorded before anything else can go wrong, so a crash or a closed
        # app cannot orphan work that is already being billed. Every job,
        # not just the first: a library needs several.
        for job_name, keys in jobs:
            store.remember_job(job_name, settings.model, keys)
        stats.job_name = ", ".join(name for name, _ in jobs)
        stats.submitted = sum(len(keys) for _, keys in jobs)

        if not wait_for_batch:
            stats.state = "SUBMITTED"
            say(
                f"{len(jobs)} batch job(s) submitted and recorded. Results can "
                "be collected later; nothing is lost by closing the app."
            )
            return stats

        # --- wait for each job, then collect it --------------------------
        states: list[str] = []
        for position, (job_name, _keys) in enumerate(jobs, start=1):
            check_cancel()
            say(f"Waiting for batch {position} of {len(jobs)}...")
            state = client.wait(
                job_name,
                poll_seconds=settings.poll_seconds,
                max_wait_seconds=settings.max_wait_seconds,
                progress=say,
                should_cancel=should_cancel,
            )
            states.append(state)
            store.update_job(job_name, state, finished=state in SUCCESS_STATES)
            if state not in SUCCESS_STATES:
                # One failed chunk must not discard the others. It stays in
                # the database and can be collected later.
                say(
                    f"Batch {position} ended in state {state}; keeping it for "
                    "later collection and continuing with the rest."
                )
                continue

            result = client.collect(job_name)
            stats.returned += len(result.analyses)
            stats.failed += len(result.errors)
            for key, analysis in result.analyses.items():
                photo = by_hash.get(key)
                if photo is None:
                    continue
                analysis = _verify_against_gazetteer(
                    analysis, peak_index, settings.peak_countries
                )
                if analysis.verified_peak:
                    stats.peaks_verified += 1
                if analysis.rejected_peak:
                    stats.peaks_rejected += 1
                if analysis.is_personal_document:
                    stats.personal_documents += 1
                store.put(key, photo.source_path, analysis, photo.size_bytes,
                          photo.timestamp, raw=result.raw.get(key))
                known[key] = analysis
            say(
                f"  batch {position}: {len(result.analyses)} analysed, "
                f"{len(result.errors)} failed"
            )
        stats.state = ", ".join(sorted(set(states))) if states else ""
        say(f"Stored {stats.returned} analysis result(s); {stats.failed} failed.")
        say(f"Stored {stats.returned} analysis result(s); {stats.failed} failed.")

    # --- choose the best of each duplicate group -------------------------
    # Now, not before: this is the first point at which anything has
    # actually looked at the pictures. The file-size ranking that ran during
    # detection answered "which is the biggest", which is not the question.
    if duplicate_groups:
        from .dedupe import best_by_analysis, mark_duplicates

        changed = best_by_analysis(duplicate_groups, known)
        stats.better_duplicate_chosen = changed
        mark_duplicates(duplicate_groups)
        say(
            f"Reviewed {len(duplicate_groups)} duplicate group(s) on sharpness, "
            f"composition and gaze; {changed} kept a different frame than "
            "file size alone would have."
        )

    # --- name the events -------------------------------------------------
    for position, event in enumerate(targets, start=1):
        if on_progress:
            on_progress(position, len(targets), f"event {event.index}")
        found = []
        for photo in select_photos(event, settings.photos_per_event):
            digest = content_hash(photo.source_path, photo.size_bytes)
            analysis = known.get(digest) if digest else None
            if analysis is not None:
                found.append(analysis)
        # A name written in the frame outranks one recognised from the
        # terrain, so resolve those first and drop anything they contradict.
        # Frames the model saw nothing in. Marking only; the copier sets
        # them aside and the user decides.
        for photo in select_photos(event, settings.photos_per_event):
            key = photo.content_key
            analysis = known.get(key) if key else None
            if analysis is not None and looks_like_a_pocket_shot(analysis):
                photo.reject_reason = "pocket"
                stats.pocket_shots += 1

        promoted = promote_text_anchors(found, peak_index, settings.peak_countries)
        if promoted:
            stats.peaks_from_text += promoted
            event.evidence.append(
                f"{promoted} peak name(s) read directly from text in the photos"
            )
        anchors = text_anchors(found, peak_index, settings.peak_countries)
        if anchors:
            contradicted = reject_contradicted_peaks(
                found, anchors, max_km=settings.peak_contradiction_km)
            stats.peaks_contradicted += contradicted
            if contradicted:
                event.evidence.append(
                    f"rejected {contradicted} recognised peak(s): more than "
                    f"{settings.peak_contradiction_km:.0f} km from "
                    f"{anchors[0][0]}, named in this event's own photos"
                )
        merged = summarise_event(
            event, found, min_probability=settings.min_peak_probability)
        location = consensus_location(
            found,
            agreement_km=settings.location_agreement_km,
            min_agreeing=settings.location_min_agreeing,
            min_fraction=settings.location_min_fraction,
            min_probability=settings.min_peak_probability,
        )
        if location is not None:
            stats.located_events += 1
            if location.source == "consensus":
                say(f"  event {event.index} location: {location.describe()}")
        elif found:
            stats.location_disagreed += 1
        if merged is None:
            stats.still_unknown += 1
            continue
        source = apply_to_event(event, merged, config, location)
        # Tell whoever is watching, immediately. A run over 13,000 photos
        # takes long enough that seeing folders acquire real names as it
        # goes is the difference between "it is working" and "it is working
        # well" -- and the second is what you actually want to know.
        if on_event_named is not None:
            try:
                on_event_named(event)
            except Exception as exc:
                # A listener must never be able to break the analysis.
                log.debug("on_event_named failed: %s", exc)
        if source == "peak":
            stats.named_from_peak += 1
        elif source == "crag":
            stats.named_from_crag += 1
        elif source == "region":
            stats.named_from_region += 1
        elif source == "activity":
            stats.named_from_activity += 1
        else:
            stats.still_unknown += 1

    from .naming import deduplicate_names

    deduplicate_names(plan.events)
    # --- read the text in what is still unnamed --------------------------
    # Local, free, and the most reliable evidence there is: a guidebook page
    # names a place outright. Measured, it is what turned an event the model
    # had placed in the Mont Blanc massif into Aiguille Dibona, 120 km away
    # in the Ecrins.
    if settings.read_text_locally:
        try:
            read_events_locally(
                plan, config, on_step=say, on_progress=on_progress,
                should_cancel=should_cancel,
            )
        except Exception as exc:
            log.warning("Local text reading failed: %s", exc)
            say(f"Could not read text locally ({exc}); continuing.")

    # --- join consecutive days that are one trip -------------------------
    # After naming, so a merge can require evidence rather than just a gap.
    if config.cluster.trip_gap_hours > 0:
        from .cluster import merge_trips

        before = len(plan.events)
        plan.events, joined = merge_trips(
            plan.events,
            gap_hours=config.cluster.trip_gap_hours,
            max_days=config.cluster.trip_max_days,
        )
        if joined:
            say(
                f"Joined {joined} event(s) into multi-day trips "
                f"({before} events became {len(plan.events)})."
            )
            # Re-name whatever was joined, from everything now inside it.
            for event in plan.events:
                if event.proposed_name:
                    continue
                found = [
                    known.get(p.content_key)
                    for p in select_photos(event, settings.photos_per_event)
                    if p.content_key and known.get(p.content_key)
                ]
                merged = summarise_event(
                    event, found, min_probability=settings.min_peak_probability
                )
                if merged is not None:
                    apply_to_event(event, merged, config,
                                   consensus_location(found))

    say(
        f"Named: peak={stats.named_from_peak} crag={stats.named_from_crag} "
        f"region={stats.named_from_region} activity={stats.named_from_activity} "
        f"from-text={stats.peaks_from_text} "
        f"pocket-shots={stats.pocket_shots} "
        f"contradicted={stats.peaks_contradicted} "
        f"unknown={stats.still_unknown}"
    )
    say(
        f"Located: {stats.located_events} event(s) have an agreed position; "
        f"{stats.location_disagreed} had estimates that disagreed and were "
        "left without one."
    )
    return stats


def read_events_locally(
    plan: Plan,
    config: Config,
    events: Optional[Sequence[Event]] = None,
    on_step: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
    should_cancel: Optional[Callable[[], bool]] = None,
) -> dict:
    """Read the text in photos of events that have no confident place name.

    This is the cheapest evidence in the project and the most reliable: a
    guidebook page names a place outright, and costs nothing to read.

    It runs AFTER the paid analysis, on what that could not name, because
    OCR is slow (about one photo a second) and most events do not need it.
    """
    from .db import AnalysisStore
    from .ocr import available, best_name, read_event, unavailable_reason
    from .pages import available as page_detection_available
    from .pages import find_pages
    from .peaks import PeakIndex

    settings = config.analysis

    def say(message: str) -> None:
        log.info("%s", message)
        if on_step:
            on_step(message)

    if not available():
        say(unavailable_reason())
        return {"read": 0, "named": 0, "skipped": True}

    store = AnalysisStore(Path(settings.database_path).expanduser())

    peak_index = PeakIndex.load() if settings.use_gazetteer else None
    if peak_index is None or not len(peak_index):
        say("No gazetteer, so text in photos cannot be checked against real places.")
        return {"read": 0, "named": 0, "skipped": True}

    targets = [
        event for event in (events if events is not None else plan.events)
        # Only what is still unplaced. An event already named from a summit
        # or a crag does not need a second opinion that costs a minute.
        if not event.place_name
    ]
    if not targets:
        say("Every event already has a place name; no text to read.")
        return {"read": 0, "named": 0, "skipped": False}

    if settings.detect_pages and page_detection_available():
        say(
            f"Looking for photos of guidebook pages in {len(targets)} unnamed "
            "event(s), then reading those. Local and free."
        )
    else:
        say(
            f"Reading text in photos of {len(targets)} unnamed event(s). "
            "No page detection available, so every photo is read -- this is "
            "slow. " + ("" if settings.detect_pages else "Enabled by detect_pages.")
        )

    read_count = 0
    named = 0
    for position, event in enumerate(targets, start=1):
        if should_cancel is not None and should_cancel():
            say("Stopped reading.")
            break
        if on_progress:
            on_progress(position, len(targets), f"event {event.index}")

        # Find the pictures of writing first, and read only those. Reading
        # everything took five hours and named six events "Se Pe"; about 3%
        # of this library is a printed page, so this is the difference
        # between hours and minutes.
        photos = list(event.photos)
        if settings.detect_pages and page_detection_available():
            found = find_pages(photos, store=store, should_cancel=should_cancel)
            by_path = {str(p.source_path): p for p in photos}
            photos = [by_path[str(f.path)] for f in found if str(f.path) in by_path]
            if not photos:
                continue

        results = read_event(
            photos,
            peak_index,
            countries=settings.peak_countries,
            max_photos=settings.ocr_max_photos_per_event,
            should_cancel=should_cancel,
            store=store,
        )
        read_count += len(results)
        # The most specific name found anywhere in the event, not the first.
        chosen = best_name(results)
        if chosen is None:
            continue
        name, hit = chosen
        peak = peak_index.verify(name, countries=settings.peak_countries)
        event.place_name = name
        event.name_source = "peak"
        event.name_from_text = True
        if peak is not None:
            event.enriched_lat, event.enriched_lon = peak.lat, peak.lon
            event.country_code = peak.country or event.country_code
        event.evidence.append(
            f"peak: {name} (read from text in {hit.path.name} by local OCR; "
            "verified in gazetteer)"
        )
        named += 1
        say(f"  event {event.index}: {name}, read from {hit.path.name}")

    say(f"Read {read_count} photo(s); named {named} event(s) from text.")
    return {"read": read_count, "named": named, "skipped": False}


def collect_open_jobs(
    plan: Plan,
    config: Config,
    store=None,
    on_step: Optional[Callable[[str], None]] = None,
) -> AnalyzeStats:
    """Reclaim results from batches submitted in an earlier session."""
    from .batch import GeminiBatch
    from .db import AnalysisStore
    from .peaks import PeakIndex

    settings = config.analysis
    stats = AnalyzeStats()

    def say(message: str) -> None:
        log.info("%s", message)
        if on_step:
            on_step(message)

    store = store or AnalysisStore(Path(settings.database_path).expanduser())
    jobs = store.open_jobs()
    if not jobs:
        say("No batch jobs are waiting to be collected.")
        return stats
    if not settings.api_key_resolved:
        say("No API key, so pending batches cannot be collected.")
        return stats

    peak_index = PeakIndex.load() if settings.use_gazetteer else None
    client = GeminiBatch(settings.api_key_resolved, model=settings.model)
    by_path = {str(p.source_path): p for p in plan.photos}

    for job in jobs:
        say(f"Checking batch {job['job_name']} from {job['created_at']}...")
        try:
            result = client.collect(job["job_name"])
        except Exception as exc:
            say(f"  could not collect: {exc}")
            continue
        stats.state = result.state
        if not result.succeeded:
            say(f"  state {result.state}; leaving it open.")
            continue
        for key, analysis in result.analyses.items():
            path = job["keys"].get(key)
            photo = by_path.get(path) if path else None
            analysis = _verify_against_gazetteer(
                analysis, peak_index, settings.peak_countries
            )
            store.put(
                key,
                Path(path) if path else Path(key),
                analysis,
                photo.size_bytes if photo else 0,
                photo.timestamp if photo else None,
            )
            stats.returned += 1
        store.update_job(job["job_name"], result.state, finished=True)
        say(f"  collected {len(result.analyses)} result(s).")
    return stats
