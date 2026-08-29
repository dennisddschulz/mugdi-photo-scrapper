# Photo Organizer

Turns an unsorted phone photo dump into an organized library with tags,
ratings and locations written into the files, ready for curation in digiKam.
See `SPECIFICATION.md` for the full spec and `CLAUDE.md` for the safety rules.

## Safety

**Your originals are never touched.** Not by policy — by construction, and the
15 tests covering it run on every commit:

- Source files are opened in binary read mode. Nothing writes, renames, or
  deletes inside the source tree.
- Metadata is written **only to copies**, inside the output tree. The writer
  refuses any path outside it, and a test asserts a refused write leaves the
  file byte-identical.
- Every copy is verified against the original by full content hash before it
  counts as done. A copy that fails verification is removed, not kept.
- Existing files are never overwritten; a name collision gets a suffix.
- Suspected duplicates are **copied into the same event folder** as the frame
  they duplicate, with `_duplicate` appended, so the two sort together and can
  be compared. Frames judged empty go to `_rejected_review/`. Nothing is ever
  deleted — not duplicates, not empty frames, not originals, not anything.
- Copying requires explicit confirmation and never happens by default.
- Recovery from any failure is always "delete the output folder and re-run".

Recommended: mark the source drive read-only at the OS level before running.

## How it works

```
Run pipeline    scan → cluster → name → plan → duplicates
                one click, writes nothing, ~5 min for 14k photos
  ↓
Identify        Gemini Batch API → SQLite cache
                separate, because it is the step that spends money
  ↓
Copy            verified copy + tags written into the copies
                separate, and needs typed confirmation
```

**There is one button: "Run everything."** It sits in the header, visible at
any scroll position, and does all of the above in one pass. It asks once — a
dialog stating the photo count, both folders and the estimated cost — and then
runs to the end.

The per-step buttons are collapsed under *"Run one step at a time"* in the
Pipeline card, for re-running a single stage or reviewing the proposed names
before anything is copied. **Copy library…** is that same copy step on its
own: useful once you have already reviewed the names, and unnecessary
otherwise.

A run reuses a plan already built for the same folders rather than re-reading
14,000 files to rediscover what is in memory.

There is no typed confirmation. A dialog that states the numbers and waits for
OK is the explicit consent `CLAUDE.md` asks for; typing a word was friction,
not protection. What stays impossible is copying with no confirmation at all,
and a test asserts it.

**Every photo is analysed exactly once in its life.** The result is cached in
SQLite keyed by the photo's content hash, so a re-scan, a rename, a
re-cluster, or deleting and rebuilding the output all cost nothing. The cache
lives in `~/.photo_organizer/analysis.sqlite3`, outside both trees.

**It is deliberately not under `~/.cache`.** A cache is by definition safe to
delete; this file is the only record of analyses that cost real money. An
existing database in the old cache location is moved there automatically on
first run. Back it up with your photos — losing it means paying again.

The cache keeps the **complete API reply**, not just the parsed fields, and
the schema asks for **more than the pipeline currently uses** — visible place
names, landmarks, weather, rock type, gear, free-text notes. Both exist for
the same reason: a photo is only ever sent once, so when the schema gains a
field, every stored row is re-parsed from the stored reply rather than
re-requested. Verified: bumping the schema version leaves nothing to
re-request.

**Duplicates are detected before analysis**, so a burst of 30 near-identical
frames costs one analysis rather than 30.

### Duplicates stay next to the photo they duplicate

```
2020/Unknown_07_03/IMG_20200307_122630_duplicate.jpg
2020/Unknown_07_03/IMG_20200307_122631_duplicate.jpg
2020/Unknown_07_03/IMG_20200307_122632_duplicate.jpg
2020/Unknown_07_03/IMG_20200307_122633.jpg          <- the one that was kept
2020/Unknown_07_03/IMG_20200307_130000.jpg
```

They sort together, so choosing between them is a matter of looking at the
folder and deleting the ones you do not want. A separate `_duplicates_review/`
tree put them in a different part of the library from the frame they
duplicate, which made the one job you actually have to do harder than it
needed to be. Set `duplicates_beside_original = false` to restore it.

**Only one photo per duplicate group is analysed** — that is the whole point
of finding them, and a burst of thirty costs one analysis rather than thirty.

### Which duplicate gets kept

By default the largest file, and then you decide by eye — they are right
there in the folder.

Set `judge_duplicates = true` and every frame in a group is analysed instead
(627 extra photos across this library, **$0.13**), with the keeper chosen on,
in order:

1. **sharpness** — a blurred keeper is not a keeper
2. **gaze** — are the people looking at the camera
3. **blinks** — fewer is better
4. **composition** — horizon level, subject whole and unobstructed
5. **rating**, then exposure

Ties fall back to the old file-size ranking, and the preview says *why* each
one was chosen, so you can check it against the picture.

Measured on a real 7-frame group: file size picked a **blurry, poor
composition, rated 1/5** frame because it was the largest at 2.7 MB. The new
ranking picks a **sharp, good composition, rated 4/5** frame instead.

Turn it off with `judge_duplicates = false` and it reverts to biggest-file,
which is a different question.

### Near-duplicates must also be close in time

Looking alike is not enough. Measured on this library, perceptual hashing
alone put **13 groups spanning more than 30 days** together — one held 7
frames taken over **538 days** — because dark night photographs hash close to
one another. That would have exiled **40 unrelated photographs** into
`_duplicates_review/`.

The distribution made the cut obvious: 511 groups span under a minute, 4
under an hour, **nothing at all between an hour and 30 days**, then 13 over a
month. Members must now be within **24 hours** of each other. Result: 0 groups
span more than a day, and the widest is now 10 minutes.

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

Double-click `start.bat`, or from a console:

```bash
python -m photo_organizer --serve
```

On Windows, `python` on the PATH is often the Microsoft Store stub, which
silently does nothing. `start.bat` finds the real interpreter for you; by hand
you may need the full path, e.g.
`%LOCALAPPDATA%\Programs\Python\Python312\python.exe`.

Then open **`http://localhost:8080/`** (or `http://127.0.0.1:8080/`).

The server listens on **both loopback addresses**. It has to: `localhost`
resolves to IPv6 `::1` before `127.0.0.1` on Windows, so an IPv4-only bind
left `http://localhost:8080/` refused by the browser while `127.0.0.1`
worked. Both sockets are loopback-only — `::1` is no more reachable from
the network than `127.0.0.1` is. Pick folders, run the pipeline, review
the result, and copy when you are satisfied. It is not a service: it starts
when you ask and stops when you click Quit, and it binds loopback only.

`start.bat --port 8090` if something already holds 8080.

### Configuration

A `config.toml` in the working directory, the project directory, or
`~/.photo_organizer/` is **loaded automatically** — no `--config` flag. The
terminal says which file is in effect. `config.example.toml` documents every
option; copy it to `config.toml` and edit.

### What protects the app, since there is no token in the URL

`127.0.0.1` is not private: every program on the machine can reach it, and so
can any web page you have open. These routes browse directories and copy
files, so that matters. Three checks, none of which need a token:

| Check | Stops |
| --- | --- |
| **Loopback bind only** (`::1` and `127.0.0.1`) | anything on the network. Verified refused on the LAN address. |
| **`Host` must be a loopback name** | DNS rebinding — an attacker pointing their own hostname at `127.0.0.1` so the browser treats their page as same-origin |
| **`Origin` must be this server** on state-changing requests | a web page you have open driving the app. A browser sets `Origin` itself; a page cannot forge it. |

A URL token was **dropped as the default** because it protected less than it
appeared to: any program running as you can read `~/.photo_organizer/ui_token`
just as the app does, so it never defended against local software, and the
web-page threat is handled by `Origin` whether or not a token exists. What it
did reliably was make the URL unbookmarkable.

If you want it anyway — a shared machine with other user accounts, where the
loopback interface is genuinely shared — `--require-token` restores it.

Verified against the running server: plain URLs 200 on `localhost`,
`127.0.0.1` and `[::1]`; cross-site POST 403; rebound `Host` 403; LAN address
refused.

### Stopping it

Click **Quit** in the page, or press Ctrl+C in its terminal. If one is left
running in the background and holds the port:

```bash
powershell -Command "Get-NetTCPConnection -LocalPort 8080 -State Listen | ForEach-Object { Stop-Process -Id $_.OwningProcess -Force }"
```

For analysis you need a Gemini API key. Put it in a **`.env`** file beside
the program:

```bash
GEMINI_API_KEY=your-key-here
```

It is loaded automatically at startup, from the working directory, the
program directory, or `~/.photo_organizer/`. `.env` is in `.gitignore` and a
test asserts it stays there.

An environment variable already set **wins** over the file — exporting a
different key for one run is deliberate and a file should not override it.
`setx GEMINI_API_KEY "..."` still works if you prefer, with the caveat that it
does not affect terminals that are already open, which is how a key that *is*
set can still look missing.

The control panel says which state it is in next to the Identify button
(*"Gemini API key found."* or a warning). That check exists because the only
other way to find out is to press Identify and have it stop — after the scan,
the clustering and the duplicate pass have all already run.

Analysis uses the **Batch API at half the interactive price** — nothing here
is interactive, so batch is the default. Every photo is analysed, not a
sample (`photos_per_event = 0`), because sampling saved cents and lost peaks.
Duplicates are removed first, so this library costs roughly **$2 once**:

| | Photos | Batch | Interactive |
| --- | --- | --- | --- |
| Whole library | 13,881 | $2.78 | $5.55 |
| After duplicates | ~9,700 | **$1.94** | $3.88 |
| **Every run after the first** | 0 pending | **$0.00** | — |

That last row is the one that matters. The cost is a **one-off**. Every later
Identify run finds each photo in the cache and sends nothing — proved by
running it with a deliberately invalid API key, which would fail loudly on any
call, and getting `submitted: 0, cost: $0.0000` with the events still named.

The confirmation dialogs quote what is **actually pending**, not the size of
the library, so a re-run correctly says free rather than $2.78.

Set a spending limit on the Google billing account — that is the real guard.
A submitted batch job is recorded in the database so it survives the app
closing; losing a job name would mean paying twice.

**Verified against the live API** with a two-image job: submit, poll, collect
and parse all work, and the results match the interactive path exactly.
Turnaround was **86 seconds**, not the 24 hours the batch API advertises as
its ceiling.

That test found a bug worth knowing about: the live `generativelanguage`
endpoint reports **`BATCH_STATE_SUCCEEDED`**, while the documentation and the
Vertex flavour use `JOB_STATE_SUCCEEDED`. The code knew only the latter, so a
finished job never looked finished — a full run would have had its results
completed and billed within minutes, then polled for the full 24-hour ceiling
and reported a timeout. Both spellings are now accepted.

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

## Browsing and searching the analyses

Every Gemini answer is queryable, not just the fields that reach a folder name.

```python
from photo_organizer.db import AnalysisStore
store = AnalysisStore()

store.search("Hannibalturm")                              # full text
store.search(filters={"rock_type": "granite", "min_score": 5})
store.search(grade="6a+", filters={"region": "Uri"})
store.facets()                                            # counts for filter menus
```

Free text covers caption, notes, transcribed text, route and peak names,
keywords, visible place names, grades and the file name. Personal documents
are excluded from every query unless explicitly asked for.

### Does SQLite scale? Measured at 400,000 photos

Asked directly, so it was measured rather than guessed: 400,000 synthetic
photos with realistic payloads, through the real `AnalysisStore` API, on this
laptop.

| Operation | Before indexing | After |
| --- | --- | --- |
| Cache hit (`get`) — the path that matters most | 4.4 ms | **3.3 ms** |
| `missing()` for 500 hashes | 49 ms | **34 ms** |
| Filter by activity | 753 ms | **20 ms** |
| Filter by grade `6a+` | 4,869 ms | **22 ms** |
| Filter 5-star granite | 989 ms | **29 ms** |
| All facet menus | 19,661 ms | **334 ms** |
| `stats()` | 5,218 ms | **114 ms** |
| Full-text search | 284 ms | **284 ms** |

Database size: **2.1 GB for 400k photos** (~5.3 KB each, including the
complete API reply kept verbatim). Bulk load ~117 s; full-text index ~30 s.

**Conclusion: SQLite is not the constraint, and Postgres would not have been
faster here.** What was slow was missing indexes, and every fix is one:

- **Partial indexes** (`WHERE is_personal = 0`) matching the predicate every
  browse query applies. Without the predicate in the index, it cannot serve
  the query — this alone was the 19.7 s → 0.3 s facet fix.
- **Composite `(filter, taken_at DESC)` indexes**, because an index on the
  filter alone still left SQLite sorting 80,000 matches to return 200.
- **Grades in their own indexed table** instead of `json_each` over every row.
- **Generated columns** for fields inside the JSON, so queries never parse it.
- A **complement index** on the 0.2% of rows flagged personal, which the
  `WHERE is_personal = 0` indexes by definition cannot cover.

One caveat on the numbers: the fixture gives every photo the same caption and
notes, so a common-word text search matches all 400,000 rows — a worst case,
not a typical one. `"Plaisir Ost"` at 644 ms is that case.

## Empty frames: pocket shots, black, white

Two detectors, because one is not enough and neither was guessed.

**Before analysis**, pixel statistics catch frames that are genuinely flat —
all black, all white, uniform grey. Measured on 1,500 photos from this
library: **2 flagged, both actually blank, no photograph touched.** They cost
nothing to find (the thumbnail is already in memory from the duplicate pass)
and they are skipped by the paid analysis.

**After analysis**, the model's own reading catches pocket shots — blurry,
lowest rating, no activity, no scene, nothing legible, nobody in frame. Any
one of those failing rescues the photo.

**There is no pixel rule for pocket shots, and that is a measurement, not an
omission.** The first attempt flagged dark low-detail frames. Checked against
the real library it caught eleven, of which **ten were photographs**:

| Flagged | What it actually was |
| --- | --- |
| `IMG_20200927_210401` | a food truck at night |
| `IMG_20200928_213712` | a campfire |
| `IMG_20191207_161143` | a snowy road at dusk |
| `IMG_20210127_121231` | a climb inside a cave |
| `IMG_20210418_200100` | someone eating cake |

A pocket shot and a night photograph have the same brightness, spread and
edge energy. No threshold separates them, so the rule was removed rather than
tuned, and a test pins the lesson.

**Nothing is deleted.** Rejected frames are copied to `_rejected_review/`,
sorted by reason, so a wrong call is visible and trivially undone. Verified
end to end: a black and a white frame set aside, the real photo filed, the
source untouched.

## How events are decided

Photos are sorted by timestamp. A new event starts after a gap of
`time_gap_hours` (default 12), or a position jump over `distance_km`
(default 15) *with* enough elapsed time to make the jump plausible — that
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

## Watching it work

Identifying 13,000 photos takes a while, and a progress bar only tells you it
is *running*. The page streams results as they are produced, over
server-sent events (`/api/events`), so folders acquire real names, coordinates
and tags **while the run is still going** — no refresh, no polling for it.

Each identified folder gets a badge saying what was found:

| Badge | Meaning |
| --- | --- |
| **peak identified** | a summit, verified against the gazetteer |
| **crag identified** | a named crag or sector |
| region identified | placed, but no specific feature — amber, not green |
| activity only | nothing but what was being done — amber |

Hovering shows the evidence behind it: the place, the range, the agreed
coordinates and the reasoning. Folders that could not be identified get **no
badge at all** — a badge on everything would say nothing. The card pulses once
when it updates, and nothing persists afterwards, because a permanently
highlighted card is noise after the first minute.

A name you are editing is never overwritten by an incoming update.

The stream cannot stall the pipeline: each listener has a bounded queue and a
full one drops messages rather than blocking, since the plan is reloaded when
the run finishes anyway. A test publishes 1,000 messages to a listener that
never reads and asserts it stays fast.

## What the first real run found

Four defects, none of which a small test could have caught. Recorded because
they are the shape of what goes wrong at scale.

**The submission was rejected outright.** Every photo went into one JSONL and
one upload: 13,748 photos came to **3.68 GB** against a **2 GiB** limit, and
after 85 minutes of encoding the API answered
`HTTP 413 Media is too large. Limit: 2147483648`. Nothing was analysed and —
the one mercy — nothing was billed. It had passed a two-image test perfectly.

Submission is now **chunked**: photos are encoded one at a time, written
straight to a temp file, and a new chunk starts before the limit is reached.
For this library that is **3 jobs of ~5,500 photos, ~1.47 GB each**. Each is
recorded in `batch_job` separately, and one failing chunk no longer discards
the others.

**Eighty-five minutes of silence.** The encode loop reported nothing until it
finished, so the slowest stage in the pipeline showed one static line while
every other stage reported every 500 files. It now reports every 250 photos
with a rate and an estimate.

**The whole payload was held in RAM** — measured at 3.7 GB of process memory
for something sent once. It streams to disk now, and the upload streams from
the file rather than a bytes blob.

**Pressing Stop was reported as a crash**, traceback and all, because the
copier raises its own cancellation class that the job runner did not catch.

## When something goes wrong

Every run writes a complete log to `~/.photo_organizer/logs/run-<timestamp>.log`,
and the path is printed at startup. The last 20 runs are kept. The log lives
next to the database, not in the output tree, so "delete the output and start
again" never destroys the record of what happened.

| | Where |
| --- | --- |
| Live progress | the page, and the terminal |
| Full history of a run | the log file |
| Crash, with file and line | the log file **and** the page |
| Per-photo copy failures | the copy summary, with a total and 20 samples |
| A submitted batch job | the `batch_job` table, so it survives a restart |

Four things were wrong when this was audited before the first real run, and
they are worth knowing because they are the failure modes a log exists to
prevent:

- **Tracebacks went to `log.debug`**, and the default level is INFO — so they
  were never recorded at all. A crash left `TypeError: ...` with no file, no
  line, no stack. Now `ERROR`, and the last 12 frames also appear in the page.
- **Nothing was written to a file.** The record lived in terminal scrollback;
  closing the window destroyed the evidence of what had been paid for.
- **The page's log held 400 lines.** A full run emits thousands, so the
  beginning was dropped exactly when you went looking for it. Now 4,000, with
  the complete record on disk regardless.
- **Copy errors were reported as "the first ten"** with no total. Ten failures
  and five hundred looked identical.

A fifth was found while testing the fix: the job set its status to `error`
*before* recording the traceback, so anything polling could see "finished" and
read an incomplete explanation. Status is now the last thing set.

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
