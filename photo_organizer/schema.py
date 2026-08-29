"""The strict schema every photo is analysed against, and its parsed form.

One request per photo, one row per photo, one shape for the answer. The
schema is enforced by the API itself via `responseSchema`, so the model
cannot return prose, a differently-shaped object, or an unexpected enum
value -- the failure modes that made earlier free-text answers unusable.

Design rules learned the hard way earlier in this project:

* Every identifying field is nullable and the prompt licenses "unknown".
  Models that cannot abstain invent: local ones claimed "K2" for a forest
  slope and "Mount Everest" for an Alpine ice fall.
* Names and geography are kept apart. A model named "Monte Oddeu" correctly
  but placed it in the Balearic Islands (it is in Sardinia). We take names
  from the model and coordinates from the gazetteer.
* `evidence_basis` asks HOW it knows. A peak read off a guidebook page in
  the frame is a different kind of claim from one recognised by its shape,
  and only the caller can weigh them if it is told which happened.
* Personal documents are flagged so their text never reaches a folder name.
"""

from __future__ import annotations

import json
import dataclasses
from dataclasses import dataclass, field, asdict
from typing import Any, Optional

SCHEMA_VERSION = 4

# Enumerations are closed sets so results are groupable rather than free
# text with forty spellings of "ice climbing".
ACTIVITIES = [
    "ice_climbing", "rock_climbing", "sport_climbing", "alpine_climbing",
    "bouldering", "via_ferrata", "mountaineering", "ski_touring",
    "ski_resort", "hiking", "trail_running", "cycling", "swimming",
    "sightseeing", "socialising", "travel", "none", "unknown",
]

SCENES = [
    "mountain_landscape", "rock_face", "glacier", "snow_slope", "forest",
    "lake", "river", "sea_coast", "beach", "city", "village", "interior",
    "hut_or_refuge", "food", "people_portrait", "animal", "plant",
    "vehicle", "document_or_screen", "sign_or_board", "other", "unknown",
]

# How well the picture is put together, for choosing between near-identical
# frames. Only ever compared WITHIN a group of the same subject -- across
# different photographs it would be taste dressed up as measurement.
COMPOSITIONS = ["good", "ordinary", "poor", "unknown"]

# Whether the people in the frame are looking at the camera with their eyes
# open. The single most common reason one frame of a burst is the keeper.
GAZES = ["all_facing", "some_facing", "none_facing", "no_people", "unknown"]

SEASONS = ["winter", "spring", "summer", "autumn", "unknown"]
TIMES_OF_DAY = ["dawn", "morning", "midday", "afternoon", "dusk", "night", "unknown"]
CONFIDENCE = ["high", "medium", "low"]

# How the model arrived at a place name. This is the field that stops a
# reading being mistaken for a recognition.
# Ordered strongest to weakest, and the split between the first two is the
# expensive lesson of this project. A name on a signboard, hut or bus stop
# is a fact about where the camera physically stood. A name on a printed
# page is a fact about what was in someone's rucksack -- often the same
# place, sometimes a route in the same region that was never climbed.
EVIDENCE_BASIS = [
    "sign_in_scene",       # a signboard, plaque, hut name, bus stop, cross
    "printed_page",        # a guidebook, map or screen photographed
    "landmark_recognition",  # recognised the terrain or a famous landmark
    "generic_inference",   # "looks like Mediterranean limestone"
    "none",
]

# Rows written before the split said "text_in_image" for both. Treat those
# as the weaker of the two rather than silently promoting them.
EVIDENCE_ALIASES = {"text_in_image": "printed_page"}


def response_schema() -> dict[str, Any]:
    """The JSON Schema handed to Gemini as `responseSchema`.

    Deliberately uses only the subset Gemini supports: object, array,
    string, number, integer, boolean, enum, nullable.
    """
    def s(t: str, **kw: Any) -> dict[str, Any]:
        return {"type": t, **kw}

    return {
        "type": "object",
        "properties": {
            # --- where -------------------------------------------------
            "country": s("string", nullable=True,
                         description="English country name, or null"),
            "country_code": s("string", nullable=True,
                              description="ISO 3166-1 alpha-2, or null"),
            "region": s("string", nullable=True,
                        description="Administrative region, province or island"),
            "mountain_range": s("string", nullable=True,
                                description="Massif or range, e.g. Ecrins, Dolomites"),
            "locality": s("string", nullable=True,
                          description="Nearest town, village or valley"),
            "peak_name": s("string", nullable=True,
                           description="A specific named summit. Null unless "
                                       "you are genuinely confident."),
            "crag_name": s("string", nullable=True,
                           description="Named crag, wall or sector"),
            "route_name": s("string", nullable=True),
            "latitude": s("number", nullable=True),
            "longitude": s("number", nullable=True),
            "elevation_m": s("integer", nullable=True),
            "location_confidence": s("string", enum=CONFIDENCE),
            "evidence_basis": s("string", enum=EVIDENCE_BASIS,
                                description="How the place was determined. "
                                            "sign_in_scene beats printed_page "
                                            "beats landmark_recognition."),

            # --- what --------------------------------------------------
            "activity": s("string", enum=ACTIVITIES),
            "scene": s("string", enum=SCENES),
            "season": s("string", enum=SEASONS),
            "time_of_day": s("string", enum=TIMES_OF_DAY),
            "people_count": s("integer", nullable=True),
            "is_indoor": s("boolean"),

            # --- text in the frame -------------------------------------
            "visible_text": s("string", nullable=True,
                              description="Verbatim transcription of text in "
                                          "the image, or null if none"),
            "is_guidebook_page": s("boolean"),
            "is_personal_document": s("boolean",
                                      description="Invoice, receipt, letter, "
                                                  "ID, screenshot of private "
                                                  "content, anything with "
                                                  "personal data"),
            "climbing_grades": {"type": "array", "items": s("string")},

            # --- quality (milestone 3) ---------------------------------
            "sharpness": s("string", enum=["sharp", "acceptable", "blurry", "unknown"]),
            "exposure": s("string", enum=["good", "underexposed", "overexposed", "unknown"]),
            "aesthetic_score": s("integer", nullable=True,
                                 description="1-5, coarse. 3 is ordinary."),
            "composition": s("string", enum=COMPOSITIONS,
                             description="How well framed and balanced the "
                                         "picture is: horizon level, subject "
                                         "not cut off, not obstructed."),
            "gaze": s("string", enum=GAZES,
                      description="Are the people looking at the camera with "
                                  "their eyes open? 'no_people' if nobody is "
                                  "in the frame."),
            "eyes_closed_count": s("integer", nullable=True,
                                   description="People with closed eyes or "
                                               "mid-blink, or null."),

            # --- free form ---------------------------------------------
            "caption": s("string", nullable=True,
                         description="One short factual sentence"),
            "keywords": {"type": "array", "items": s("string")},
            "reasoning": s("string", nullable=True,
                           description="One sentence on how the place was determined"),

            # Asked for because a photo is only ever sent once. Anything
            # plausibly useful later has to be requested now, or wanting it
            # later means paying for the whole library again.
            "place_names_visible": {
                "type": "array", "items": s("string"),
                "description": "Place names legible anywhere in the image: "
                               "signposts, hut boards, bus stops, summit "
                               "crosses, guidebook headings, map labels.",
            },
            "landmarks": {
                "type": "array", "items": s("string"),
                "description": "Named things visible: peaks, huts, glaciers, "
                               "lakes, buildings, monuments.",
            },
            "weather": s("string", nullable=True),
            "rock_type": s("string", nullable=True,
                           description="granite, limestone, gneiss, sandstone..."),
            "gear_visible": {"type": "array", "items": s("string")},
            "notes": s("string", nullable=True,
                       description="Anything else noteworthy that no other "
                                   "field captures. Free text."),
        },
        "required": [
            "activity", "scene", "season", "time_of_day", "is_indoor",
            "is_guidebook_page", "is_personal_document",
            "location_confidence", "evidence_basis",
            "sharpness", "exposure",
        ],
    }


PROMPT = (
    "Analyse this photograph and return JSON matching the provided "
    "schema.\n"
    "\n"
    "Rules:\n"
    "1. Use null for anything you are not genuinely confident about. A "
    "null is always better than a plausible guess. Do NOT name a summit "
    "unless you are confident.\n"
    "2. READ BEFORE YOU RECOGNISE. If a place name is legible anywhere in "
    "the frame -- a signpost, trail sign, bus stop, hut or refuge board, "
    "summit cross, via ferrata plaque, information panel, guidebook page "
    "-- that name wins. Never return a summit you recognised by its shape "
    "when it contradicts a name written in the image. Terrain recognition "
    "is the least reliable evidence available here: granite spires and "
    "limestone walls in the same massif look alike, and naming the wrong "
    "one is worse than naming none.\n"
    "3. Set evidence_basis honestly:\n"
    "- 'sign_in_scene' if the name is written on something physically at "
    "the location: signpost, trail sign, bus stop, hut or refuge name, "
    "summit cross, plaque, information panel;\n"
    "- 'printed_page' if you read it from a guidebook, topo, map or "
    "screen that was photographed;\n"
    "- 'landmark_recognition' if you recognise the terrain or landmark "
    "itself;\n"
    "- 'generic_inference' if you are inferring from vegetation, rock "
    "type, architecture or style;\n"
    "- 'none' if you cannot place it at all.\n"
    "4. visible_text: transcribe ALL text in the image verbatim, "
    "including decorative lettering and small, distant or partly obscured "
    "lettering on signs, boards, plaques and bus stops. Do not skip a "
    "sign because it looks incidental -- it is often the only reliable "
    "thing in the frame. Null only if there is genuinely no text at all.\n"
    "5. is_personal_document: true for invoices, receipts, letters, IDs, "
    "bank details, or screenshots containing personal information.\n"
    "6. place_names_visible: every place name legible anywhere in the frame, "
    "as a list. This is separate from visible_text and is the field the "
    "folder naming reads first.\n"
    "7. composition, gaze and eyes_closed_count decide which frame of a "
    "burst of near-identical shots is kept, so judge them carefully and "
    "consistently. composition: is the horizon level, the subject whole and "
    "unobstructed, the framing deliberate. gaze: are the people looking at "
    "the camera with their eyes open ('no_people' if nobody is in frame). "
    "eyes_closed_count: how many are blinking.\n"
    "8. sharpness must describe the SUBJECT. A portrait with a deliberately "
    "blurred background is sharp.\n"
    "9. latitude/longitude: your best estimate of where the camera stood, "
    "or null.\n"
    "Photos are mostly from the European Alps (Switzerland, France, "
    "Italy, Austria), the Tatras, Scandinavia, and Mediterranean climbing "
    "areas."
)


def unwrap_response(payload: Any) -> Optional[dict[str, Any]]:
    """The analysis object inside whatever shape the reply was stored in.

    The cache keeps the complete API envelope, so re-parsing a stored reply
    has to cope with all of: a batch result line ({"key", "response"}), a
    bare generateContent response ({"candidates"}), and the already-unwrapped
    analysis dict. Returns None if there is no analysis in there.
    """
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return None
    if not isinstance(payload, dict):
        return None
    inner = payload.get("response")
    if isinstance(inner, dict):
        payload = inner
    if "candidates" in payload:
        # It is an envelope, so it must unwrap or it is an error. An empty
        # candidates list is a failed generation, not an empty analysis.
        candidates = payload.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return None
        try:
            text = candidates[0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else None
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            return None
    # Already the analysis itself.
    return payload if payload else None


@dataclass
class PhotoAnalysis:
    """One photo's analysis, as stored. Mirrors the schema above."""

    country: Optional[str] = None
    country_code: Optional[str] = None
    region: Optional[str] = None
    mountain_range: Optional[str] = None
    locality: Optional[str] = None
    peak_name: Optional[str] = None
    crag_name: Optional[str] = None
    route_name: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    elevation_m: Optional[int] = None
    location_confidence: str = "low"
    evidence_basis: str = "none"

    activity: str = "unknown"
    scene: str = "unknown"
    season: str = "unknown"
    time_of_day: str = "unknown"
    people_count: Optional[int] = None
    is_indoor: bool = False

    visible_text: Optional[str] = None
    is_guidebook_page: bool = False
    is_personal_document: bool = False
    climbing_grades: list[str] = field(default_factory=list)

    sharpness: str = "unknown"
    exposure: str = "unknown"
    aesthetic_score: Optional[int] = None
    composition: str = "unknown"
    gaze: str = "unknown"
    eyes_closed_count: Optional[int] = None

    caption: Optional[str] = None
    keywords: list[str] = field(default_factory=list)
    reasoning: Optional[str] = None

    place_names_visible: list[str] = field(default_factory=list)
    landmarks: list[str] = field(default_factory=list)
    weather: Optional[str] = None
    rock_type: Optional[str] = None
    gear_visible: list[str] = field(default_factory=list)
    notes: Optional[str] = None

    # --- provenance, not from the model ---------------------------------
    model: str = ""
    schema_version: int = SCHEMA_VERSION
    # Set once the peak name has been checked against the gazetteer.
    verified_peak: Optional[str] = None
    verified_lat: Optional[float] = None
    verified_lon: Optional[float] = None
    verified_country: Optional[str] = None
    rejected_peak: Optional[str] = None
    # How many of an event's photos independently named this summit, and how
    # many were looked at. A peak asserted by one photo is much weaker
    # evidence than the same peak named by three, and the user should see
    # which they are looking at.
    peak_agreement: int = 0
    peak_considered: int = 0
    # P(the chosen summit is the right one), and the best alternative, so a
    # marginal call is visible in the preview instead of looking certain.
    peak_probability: float = 0.0
    runner_up_peak: Optional[str] = None
    runner_up_probability: float = 0.0

    @property
    def names_a_place(self) -> bool:
        return bool(self.verified_peak or self.crag_name or self.mountain_range)

    @property
    def safe_text(self) -> Optional[str]:
        """Text to read place names out of, personal paperwork excluded.

        `place_names_visible` comes first because it is the model's own
        shortlist of place names; the full transcription follows so a name
        it did not classify as one can still be found.
        """
        if self.is_personal_document:
            return None
        parts = [*self.place_names_visible]
        if self.visible_text:
            parts.append(self.visible_text)
        return "\n".join(parts) or None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_model_json(cls, data: dict[str, Any], model: str = "") -> "PhotoAnalysis":
        """Build from the model's reply, coercing types and dropping extras.

        Tolerant on purpose: a schema-constrained reply should already fit,
        but a single malformed field must not lose the whole analysis.
        """
        def text(key: str) -> Optional[str]:
            value = data.get(key)
            if value is None:
                return None
            value = str(value).strip()
            return value or None

        def flag(key: str) -> bool:
            return bool(data.get(key))

        def number(key: str) -> Optional[float]:
            try:
                value = data.get(key)
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        def whole(key: str) -> Optional[int]:
            value = number(key)
            return int(value) if value is not None else None

        def choice(key: str, allowed: list[str], default: str) -> str:
            value = (data.get(key) or "").strip().lower().replace(" ", "_")
            return value if value in allowed else default

        def basis() -> str:
            value = choice("evidence_basis",
                           EVIDENCE_BASIS + list(EVIDENCE_ALIASES), "none")
            return EVIDENCE_ALIASES.get(value, value)

        def strings(key: str) -> list[str]:
            value = data.get(key)
            if not isinstance(value, list):
                return []
            return [str(v).strip() for v in value if str(v).strip()][:20]

        return cls(
            country=text("country"),
            country_code=(text("country_code") or "").upper()[:2] or None,
            region=text("region"),
            mountain_range=text("mountain_range"),
            locality=text("locality"),
            peak_name=text("peak_name"),
            crag_name=text("crag_name"),
            route_name=text("route_name"),
            latitude=number("latitude"),
            longitude=number("longitude"),
            elevation_m=whole("elevation_m"),
            location_confidence=choice("location_confidence", CONFIDENCE, "low"),
            evidence_basis=basis(),
            activity=choice("activity", ACTIVITIES, "unknown"),
            scene=choice("scene", SCENES, "unknown"),
            season=choice("season", SEASONS, "unknown"),
            time_of_day=choice("time_of_day", TIMES_OF_DAY, "unknown"),
            people_count=whole("people_count"),
            is_indoor=flag("is_indoor"),
            visible_text=text("visible_text"),
            is_guidebook_page=flag("is_guidebook_page"),
            is_personal_document=flag("is_personal_document"),
            climbing_grades=strings("climbing_grades"),
            sharpness=choice("sharpness", ["sharp", "acceptable", "blurry", "unknown"], "unknown"),
            exposure=choice("exposure", ["good", "underexposed", "overexposed", "unknown"], "unknown"),
            aesthetic_score=whole("aesthetic_score"),
            composition=choice("composition", COMPOSITIONS, "unknown"),
            gaze=choice("gaze", GAZES, "unknown"),
            eyes_closed_count=whole("eyes_closed_count"),
            caption=text("caption"),
            keywords=strings("keywords"),
            reasoning=text("reasoning"),
            place_names_visible=strings("place_names_visible"),
            landmarks=strings("landmarks"),
            weather=text("weather"),
            rock_type=text("rock_type"),
            gear_visible=strings("gear_visible"),
            notes=text("notes"),
            model=model,
        )

    @classmethod
    def from_row(cls, payload: str) -> "PhotoAnalysis":
        """Rebuild from a stored payload, tolerating fields we no longer know.

        A row written by a newer version must not crash an older one, and a
        row written by an older version must not be discarded -- discarding
        it means paying to analyse that photo a second time.
        """
        data = json.loads(payload)
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})
