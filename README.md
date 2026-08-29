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
sample, and duplicates are removed first.

### What it actually costs — and how wrong the first estimate was

The estimate started as a guess of **$0.0004 per photo**. Reconciled against a
real bill it is about **$0.0047** — **twelve times higher**:

| | Per photo | 1,000 photos | This library (13,748) |
| --- | --- | --- | --- |
| Original guess | $0.0004 | $0.20 | **$2.75** |
| **Measured against a bill** | **$0.0047** | **$2.35** | **$32.31** |

The measurement: 23 requests (20 interactive, 3 batch) had been charged
**$0.10**. Small sample, and it may include fixed charges, so treat it as an
order of magnitude — but what it is definitely not is $0.0004.

For reference, real replies use **1,491 prompt tokens and ~420 output tokens**
per photo, so the number can be re-derived when pricing changes.

**Set `cost_per_photo_usd` from your own bill.** Divide what you were charged
by the number of photos analysed. The estimate is only as good as that number,
and every dialog that quotes a cost uses it.

**Run a small batch first.** Analyse a thousand photos, look at the bill,
then decide about the rest. The cache means nothing is paid for twice, so
starting small costs nothing extra.

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

## Reading the text in photos — locally and for nothing

A guidebook page names a place outright. That is the most reliable evidence
in this project, and Tesseract reads it for free.

Measured on a real failure: an event on 11 September 2019 was named
**Mont-Blanc-Massif** because the model recognised "Aiguille de la
République" — a real peak, in the wrong massif, **120 km** from where the
photos were taken. The event contained a guidebook page naming **Aiguille
Dibona**. That photo scored 0.71 on the scenic scale, one of the lowest in
the event, so it was never among the four sent for analysis.

OCR found it in **38 seconds**, locally:

```
event: Aiguille Dibona, read from IMG_20190911_201210.jpg
coords: 44.9632445, 6.2428893      (Écrins, not Mont Blanc)
```

It runs after the paid analysis, only on events still unnamed, at about one
photo per second.

**There is no page detector, and there does not need to be one.** Four
attempts to find pages from pixels failed: brightness and edges scored a
portrait of a person 3.17 and a real page 0.0; adding saturation still scored
the page 0.0; and at higher resolution the "pages" turned out to be climbers
on grey granite. Grey rock and printed paper are statistically identical, and
a climbing library is full of grey rock. OCR needs no detector because it
*is* one — over rock it returns `SESS JURE SaaS Paes iets Seen`, which matches
nothing in a gazetteer of real places.

One refinement that mattered: it does **not** stop at the first name found.
The first photo of that event yielded the bare word "Aiguille" — a real
gazetteer entry, French for "needle", and a useless folder name. Names are
ranked by how specific they are.

## Multi-day trips

The 12-hour rule splits an overnight, so a hut approach and the climb next
morning become two events. On the real library one three-day trip became
three, and **only the first day was named correctly**.

Raising `time_gap_hours` globally is the wrong fix — it merges genuinely
separate day trips. Instead, consecutive events are joined **after naming**,
when there is evidence to justify it: the gap must be no more than a night,
the trip must fit inside `trip_max_days` (3), and **one event must know where
it was while the other does not contradict it**.

That ordering is the whole design. Merging during clustering, on time alone,
joined 106 events on this library — including a day of socialising with the
next day's hike. Two unnamed days are not evidence of anything.

## Choosing which photos to analyse

Naming 379 events does not need 13,748 analyses. It needs a few photos of
each — and **the right few**.

Only about 27% of this library shows a placeable skyline; the rest is
close-ups of climbers, gear, hands and food that could never name anything.
So the sample is **chosen, not spread**: the duplicate pass already reads each
photo's thumbnail, and scoring it for sky over terrain and landscape framing
costs nothing extra.

| | Photos | Cost | Encode + upload |
| --- | --- | --- | --- |
| Every photo | 13,748 | $32.31 | 64 min |
| **4 chosen per event** | **1,516** | **$3.56** | **7 min** |

Verified by looking: the top-scoring frames from a 400-photo sample were
granite spires, the Mont Blanc massif at sunset, a sea cliff, and climbers on
snowy ridges. Scoring runs at 79 photos/second.

**There is deliberately no page detection in the scorer.** Two attempts failed
against real data: brightness plus edges scored a portrait of a person 3.17
and an actual guidebook page 0.0 — wrong in both directions — and adding
saturation still scored the page 0.0. At 96 pixels printed text averages into
grey. Detecting a page needs resolution, which is the one thing this scorer
exists to avoid spending, so pages are left to OCR or to the model — both of
which actually read.

**Image size does not affect cost.** Measured with `countTokens`: 1024px,
768px, 512px and 384px are all exactly **1,064 tokens**. Smaller images are
still worth it for speed and upload size, not for price.

## Quotas

The account limit is not on spending, it is on **how much you enqueue at
once**. Measured on a billed account that had been charged 10 cents and held
2 jobs and 3 requests:

| Submitted | Result |
| --- | --- |
| 1 request | accepted |
| 5,254 requests in one job | `HTTP 429 RESOURCE_EXHAUSTED` |

So batches are capped by **request count** (1,000) as well as by bytes, a 429
is retried with backoff, and — most importantly — **batches already accepted
are kept**. They are already being billed; discarding them is the worst
possible response to a rate limit. A run that hits the ceiling stops cleanly,
keeps what it has, and the rest can be submitted later at no extra cost.

The full error body is now logged. A 429 names the exact quota inside its
`details` block, and the code used to truncate the message at 400 characters
— cutting off precisely the part that says which limit was hit.

## Fail fast

Before any expensive work, a preflight runs in about **two seconds** and
refuses to start if something is already known to be wrong:

```
[ok] Output folder is writable          C:\FotosTempOrganized
[ok] Enough disk space                  50.2 GB needed, 149.1 GB free
[ok] Analysis cost is known             13825 to analyse, about $2.77
[ok] Upload fits inside the API limit   about 3.61 GB in 3 batch jobs, each under 1.47 GB
[ok] Gemini API key                     valid, 50 models available
[ok] Peaks gazetteer                    125,572 named peaks and landforms
```

**That fourth line is why this exists.** The first real run spent 85 minutes
encoding photos and then died on an upload limit that was predictable from the
photo count in the first second. Every fact needed to see it coming was
available before any work began.

The API key is verified with a real call (free), because "not set" and "set
but rejected" are different problems and both should surface before the scan,
not after it. A network failure is reported as a warning rather than a
blocker — that is not a bad key.

Counting what is already cached would mean hashing every file, which took
150 seconds. It samples 400 photos and scales, and says when it did so.

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
