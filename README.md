# Photo Organizer

Turns an unsorted phone photo dump into an organized library with tags,
ratings and locations written into the files, ready for curation in digiKam.
See `SPECIFICATION.md` for the full spec and `CLAUDE.md` for the safety rules.

## Safety

The source photos are irreplaceable, so the tool is copy-only by construction:

- Source files are opened in binary read mode. Nothing writes, renames, or
  deletes inside the source tree.
- Metadata is written **only to copies**, inside the output tree. The writer
  refuses any path outside it, and a test asserts a refused write leaves the
  file byte-identical.
- Every copy is verified against the original by full content hash before it
  counts as done. A copy that fails verification is removed, not kept.
- Existing files are never overwritten; a name collision gets a suffix.
- Suspected duplicates are **copied** to `_duplicates_review/`. Nothing is
  ever deleted — not duplicates, not originals, not anything.
- Copying requires explicit confirmation and never happens by default.
- Recovery from any failure is always "delete the output folder and re-run".

Recommended: mark the source drive read-only at the OS level before running.

## How it works

```
scan → cluster → plan          fast, local, writes nothing
  ↓
duplicates                     marks exact and near-duplicates
  ↓
identify                       Gemini Batch API → SQLite cache
  ↓
copy                           verified copy + tags written into the copies
```

**Every photo is analysed exactly once in its life.** The result is cached in
SQLite keyed by the photo's content hash, so a re-scan, a rename, a
re-cluster, or deleting and rebuilding the output all cost nothing. The cache
lives in `~/.cache/photo_organizer/analysis.sqlite3`, outside both trees.

The cache keeps the **complete API reply**, not just the parsed fields, and
the schema asks for **more than the pipeline currently uses** — visible place
names, landmarks, weather, rock type, gear, free-text notes. Both exist for
the same reason: a photo is only ever sent once, so when the schema gains a
field, every stored row is re-parsed from the stored reply rather than
re-requested. Verified: bumping the schema version leaves nothing to
re-request.

**Duplicates are detected before analysis**, so a burst of 30 near-identical
frames costs one analysis rather than 30.

## Install

Requires Python 3.11+.

```bash
pip install -e ".[full]"
```

| Package | Why |
| --- | --- |
| `Pillow`, `pillow-heif`, `exifread` | EXIF, timestamps, thumbnails, HEIC |
| `pyexiv2` | writing XMP/IPTC tags into the copies |
| `truststore` | corporate TLS proxies (certifi alone fails) |

Then, once:

```bash
python -m photo_organizer --build-gazetteer
```

This downloads ~125,000 named summits and landforms for CH, FR, IT, AT, SK,
DE and NO from OpenStreetMap. It works offline afterwards, and it is what
stops an invented summit name reaching a folder.

## Use

```bash
python -m photo_organizer --serve
```

Opens `http://127.0.0.1:8080`. Pick folders, run the pipeline, review the
result, and copy when you are satisfied. It is not a service: it starts when
you ask and stops when you click Quit, binds loopback only, and is behind a
one-time token printed in the terminal.

For analysis you need a Gemini API key:

```bash
setx GEMINI_API_KEY "your-key"
```

Analysis uses the **Batch API at half the interactive price** — nothing here
is interactive, so batch is the default. Every photo is analysed, not a
sample (`photos_per_event = 0`), because sampling saved cents and lost peaks.
Duplicates are removed first, so this library costs roughly **$2 once**:

| | Photos | Batch | Interactive |
| --- | --- | --- | --- |
| Whole library | 13,881 | $2.78 | $5.55 |
| After duplicates | ~9,700 | **$1.94** | $3.88 |

Set a spending limit on the Google billing account — that is the real guard.
A submitted batch job is recorded in the database so it survives the app
closing; losing a job name would mean paying twice.

Terminal equivalents:

```bash
python -m photo_organizer "E:\PhoneDump" -o "D:\Organized"
```

```bash
python -m photo_organizer "E:\PhoneDump" -o "D:\Organized" --identify
```

## What gets written into your photos

Into the **copies** only, in standard fields digiKam reads:

| Field | Contents |
| --- | --- |
| `Xmp.dc.subject` + IPTC Keywords | activity, scene, season, time of day, range, peak, crag, route, region, country, rock type, weather, climbing grades, keywords |
| `Xmp.dc.description` | one-sentence caption |
| `Xmp.dc.title` | the place, when one is known |
| `Xmp.xmp.Rating` | 1–5 stars |
| `Xmp.xmp.Label` | colour label — red flags a blurry frame |
| `Xmp.photoshop.City/State/Country` | locality, region, country |
| EXIF GPS | the **event's** agreed position, marked `ESTIMATED-photo-organizer` |
| `Xmp.dc.source` | how the location was determined, and with what confidence |

Estimated positions are labelled as estimates so a future reader cannot
mistake one for a camera fix.

Some analysed fields are deliberately **not** written into files, though they
stay in the database and can be surfaced later without re-analysing anything:

| Field | Why not |
| --- | --- |
| `landmarks` | recognised, not read — measured wrong ("Bergseehütte" for a photo 13 km away). A wrong tag is a false fact inside a file. |
| `place_names_visible` | already drives the folder name; on a guidebook page these are places in the region, not where the photo was taken |
| `gear_visible` | accurate but noisy — 40 photos tagged "carabiner" help nobody |
| `notes` | prose; searchable in the database instead |

Real example, written from an actual Gemini reply:

```
keywords    : rock_climbing, document_or_screen, Urner Alps, Hannibalturm,
              Uri, Switzerland, granite, 6a+, 5c+, 6b+, 6c, A0, 7a+,
              climbing topo, guidebook, plaisir ost, Furka
description : A page from the Schweiz Plaisir Ost climbing guidebook
              detailing routes on the Hannibalturm at Furka Pass.
title       : Hannibalturm      city/state: Realp / Uri
gps_lat     : 46.599400         source: location by printed_page (high confidence)
```

### Location is decided per event, never per photo

A single photo's estimate is not trustworthy — measured on this library, the
model placed Swiss photos in California and a Sardinian trip in Provence. So
no photo's own guess is ever written. Instead:

1. Every analysed photo in an event contributes its estimate.
2. The largest cluster of mutually-agreeing estimates wins; everything
   outside it is discarded, so one wild guess cannot drag the average.
3. That single agreed position is written into **every** photo of the event.
4. If the estimates do not agree, the event gets **no coordinates at all**.
   An empty GPS field is honest; a wrong one is not.

A gazetteer-verified summit skips all of this — its coordinates are a fact,
not an estimate.

Tunable under `[analysis]`: `location_agreement_km` (default 25),
`location_min_agreeing` (2), `location_min_fraction` (0.4).

## How events are decided

Photos are sorted by timestamp. A new event starts after a gap of
`time_gap_hours` (default 12), or a position jump over `distance_km`
(default 50) *with* enough elapsed time to make the jump plausible — that
second condition stops one bad GPS fix mid-hike splitting an event.

A name is assembled as `[range_]place_activity_DD_MM`, for example
`2019/Urner-Alps_Hannibalturm_alpine-climbing_01_09`. **The activity is in
every name**, including events that got a peak, so the library can be browsed
by what was done as well as by where. Turn it off with
`include_activity = false`; it is skipped automatically when it would repeat a
word already in the name.

The place part is proposed in this order, and every name is editable before
any copy:

1. **A verified summit** — named by the model, confirmed against the
   gazetteer, with the gazetteer's coordinates. When an event's photos name
      different summits, the one named by the most of them wins, and a name
   **read** from the frame (a guidebook page, a signboard) outranks one
   recognised from the terrain — see the worked example below. The
   preview says how many photos agreed (`named by 3 of 8 photos analysed`),
   so a summit resting on a single frame is visible as such.
2. **A crag or region.**
3. **The activity** — `Ice-climbing_09_02`.
4. `Unknown_DD_MM`, flagged for manual naming.

## What was tried and rejected

Measured against this library, and removed rather than left in:

| Approach | Result |
| --- | --- |
| CLIP ViT-B/32 | named "K2" at 82% for a forest slope with no mountain in it |
| qwen2.5vl:3b (local) | named "Mount Everest", high confidence, for an Alpine ice fall |
| GeoCLIP (local) | median error 139 km; 0 of 41 photos within 10 km |
| SIFT place matching | sound in principle; found no repeat visits at usable thresholds |
| Gemini | **median 11 km, abstains when unsure** — kept |

Model size was the binding constraint. The small local models were not a
cheaper version of the large one; they were a different, unusable thing.

Two settings decide whether a peak is found at all. `photos_per_event`
(default 8) governs whether an identifiable frame is even sampled — roughly
27% of this library's photos show a placeable skyline, so 4 samples miss an
event's summit about a quarter of the time and 8 miss it under a tenth. And
corroboration across those samples is what separates a summit three photos
agree on from one photo's guess.

**The gazetteer proves a name is real, not that it is right.** Worked example
from this library: for photos taken at the **Hannibalturm (Furkapass)**, the
model answered **"Salbitschijen"** — a real Swiss summit **13 km away**, in
the same canton and the same massif. The gazetteer accepted it, because all
the gazetteer can check is that the name exists.

The right answer was written in the photos the whole time: a guidebook page
headed "Furka | Galengrat – Hannibalturm", and the "Hanicity" board at the
foot of the tower. It went unused because it landed in `visible_text` and
nothing read that field for place names.

Three things changed as a result:

| Change | Effect |
| --- | --- |
| `evidence_basis` splits `sign_in_scene` from `printed_page` | a name on a signpost is now distinguishable from one on a photographed page |
| Names in `visible_text` are matched against the gazetteer and **promoted to peak claims** | the Hannibalturm heading now names the event |
| A **recognised** peak further than `peak_contradiction_km` (30) from a **read** name is rejected | a hallucinated summit cannot outvote a written one |

**The probabilities are computed here, not by Gemini.** Gemini returns
`evidence_basis` (how it knows) and `location_confidence` (high/medium/low).
The confidence is treated as a weak signal worth ±10%, because in the one
adjudicated case it said "high" and was wrong. The probability itself comes
from the local priors below.

Ranking is by **probability**, not by a chain of comparisons. Each claim gets
P(correct) from how it was obtained, claims for the same summit combine by
noisy-OR with a decay for correlated errors, and a summit must clear
`min_peak_probability` (0.5) before it can name a folder or contribute
coordinates:

| Evidence | P |
| --- | --- |
| one signboard | 0.92 |
| one guidebook page, high confidence | 0.79 |
| **one confident terrain recognition** | **0.31 — does not name** |
| three agreeing terrain recognitions | 0.50 |

The model's own stated confidence moves this by ±10% at most: in the
adjudicated case it said "high" and was wrong, while the correct answer
carried no confidence at all. A summit below the floor is kept in the
event's evidence as a suggestion rather than used or discarded.

Verified against the live API on the two real photos:

| Photo | Before | After |
| --- | --- | --- |
| Bench under the bus stop | `Salbitschijen`, high confidence | `peak: null` — abstained |
| Guidebook page | — | `Hannibalturm`, `printed_page`, 79% |

Result: `2019/Uri-Alps_Hannibalturm_alpine-climbing_01_09`, GPS 46.5994, 8.4197.

Matching against `visible_text` is exact, never fuzzy, and single words must
be 8+ characters — otherwise the "DIE POST" on a bus stop would match a
hamlet called Post. `Hanicity` still resolves to nothing, because OpenStreetMap
has never heard of it; the guidebook page is what carries this particular
event.
 A plausible
neighbouring summit in the right country passes: the model said
"Salbitschijen" for a photo taken 13 km away and it was accepted, because
Salbitschijen exists. Treat proposed names as proposals.

## Tests

No test dependencies beyond the package's own:

```bash
python -m unittest discover -s tests -v
```

## Status

| Milestone | Scope | State |
| --- | --- | --- |
| 1 | Scan, cluster, propose names, dry-run preview | done |
| 2 | Verified copy, duplicate review folder | done |
| 3 | Tags, ratings and location written as XMP/IPTC | done |
| 4 | Summit naming | done, with the caveat above |

digiKam takes over afterwards for face recognition, duplicate confirmation,
tag editing and browsing. This tool feeds it; it does not replace it.
