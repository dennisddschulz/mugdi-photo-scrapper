# Photo Organizer — Specification

A local, non-destructive tool that turns an unsorted phone photo dump into an
organized, tagged, quality-rated library, ready for final curation in digiKam.

This document is the detailed reference. The short brief Claude Code loads
automatically is in `CLAUDE.md`. Where the two overlap, the safety rules in
`CLAUDE.md` are authoritative.

---

## 1. Purpose and scope

### In scope
- Ingest an unsorted folder of photos (initially a mobile phone dump).
- Group photos into events and copy them into a dated folder structure.
- Detect duplicates and set them aside for review.
- Tag photo content (scene/object categories) into file metadata.
- Score each photo for technical quality and rough aesthetic appeal.
- Propose event names from GPS + date.

### Out of scope (handled by digiKam or the user)
- Face recognition and people tagging (digiKam does this well natively).
- Final duplicate deletion (user decides in digiKam).
- Final curation and "is this photo meaningful/iconic" judgement (human only).
- Cloud upload (a separate sync step after the library is organized).

### Design philosophy
The script does only what digiKam cannot: automatic event clustering, GPS-based
name proposals, content tagging, and quality scoring. Everything mature and
trusted (library management, dedup confirmation, faces, tag editing) is left to
digiKam. The script's output is a folder tree with metadata written into files,
which digiKam then indexes.

---

## 2. Safety requirements (highest priority)

The source photos are irreplaceable. These requirements override all others.

- **R-S1 Copy-only.** The tool reads source files and writes copies to a
  separate output directory. It must never move, rename, edit, or delete a
  source file.
- **R-S2 Read-only source.** The tool must function with the source mounted
  read-only and must never attempt a write inside the source path. Recommended:
  the user marks the source read-only at the OS level before running.
- **R-S3 Mandatory dry-run.** The default mode produces a preview only: the
  planned folder structure, per-event file counts, proposed names, and suspected
  duplicate count. No files are written in dry-run mode.
- **R-S4 Explicit confirmation to write.** Actual copying happens only after the
  user reviews the dry-run and explicitly confirms (e.g. a `--commit` flag or a
  typed "yes").
- **R-S5 No auto-deletion, ever.** Suspected duplicates are copied to a review
  folder; the tool never deletes anything, including duplicates.
- **R-S6 Recoverable by design.** Any failure is recoverable by deleting the
  output directory and re-running. The source remains a complete, valid copy at
  all times.
- **R-S7 Idempotent / resumable.** Re-running must not corrupt prior output. Use
  a manifest (see R-F5) to skip already-processed files or write to a fresh
  output folder.
- **R-S8 Manual source cleanup.** Only the user deletes the original dump, by
  hand, after verifying output and making a backup. The tool must never offer or
  perform this.

---

## 3. Functional requirements

### 3.1 Scan (read-only)
- **R-F1** Recursively scan the source for image files (JPEG, HEIC, PNG, and
  common RAW: CR2/CR3, NEF, ARW, DNG). Skip non-images.
- **R-F2** For each file read EXIF: capture timestamp (DateTimeOriginal), GPS
  latitude/longitude/altitude, GPS image direction (heading) if present, camera
  make/model. Handle missing fields gracefully (many photos lack GPS/heading).
- **R-F3** Fall back to file modification time when EXIF timestamp is absent, and
  flag such photos as lower-confidence for clustering.

### 3.2 Event clustering
- **R-F4** Sort photos chronologically. Start a new event when EITHER:
  - the time gap to the previous photo exceeds a threshold (default 6 hours), OR
  - the location jump exceeds a distance threshold (default 30 km), computed by
    haversine distance between consecutive GPS points.
- Thresholds must be configurable. Photos without GPS use time gaps only.
- **R-F5** Produce an in-memory (and serializable) manifest describing every
  photo, its assigned event, proposed destination path, and detected metadata,
  BEFORE any copying.

### 3.3 Name proposal

The library turned out to have essentially no GPS (41 of 13,881 files, zero
headings), so R-F6/R-F7 as originally written could never fire. Names are
built from photo analysis instead, and the rules below replace them.

**Folder shape** — `YEAR/[Range_]Place_Activity_DD_MM`

    2019/Uri-Alps_Hannibalturm_alpine-climbing_01_09
    2019/Urner-Alps_ice-climbing_09_02
    2019/hiking_14_06

- **R-F6** Every name ends with `_DD_MM` of the event's first photo, and lives
  under its `YEAR`. The date is the one field always available.
- **R-F7** The **activity** appears in EVERY name, not only in names that
  failed to find a place, so the library is browsable by what was done as
  well as by where. It is omitted only when it would repeat a word already
  present. Configurable: `naming.include_activity`.
- **R-F8** The **place** part is chosen in this order:
  1. a **summit** that clears the probability floor (R-F8b);
  2. a **crag** or sector name;
  3. the **mountain range**, else the administrative region;
  4. nothing — the name is then activity plus date;
  5. `Unknown_DD_MM`, flagged for manual naming.
- **R-F8a** A summit name is never used unless it exists in the peaks
  gazetteer. The gazetteer supplies the coordinates; the model supplies only
  the name. (Measured: the model named "Monte Oddeu" correctly but placed it
  in the Balearic Islands rather than Sardinia.)
- **R-F8b** **A summit must clear a probability floor** (`min_peak_probability`,
  default 0.5) before it may name a folder or contribute coordinates. The
  probability is computed from how the name was obtained, not from the model's
  own stated confidence, which was measured to be anti-correlated with
  correctness. See section 5.
- **R-F8c** A summit that is proposed but falls below the floor is **kept and
  shown in the event's evidence**, so the user can promote it. It is never
  silently discarded and never silently used.
- **R-F8d** Every proposed name is editable before any file is copied. The
  tool never finalises a name unilaterally.

### 3.4 Dry-run preview
- **R-F9** Print a human-readable plan: number of events, each proposed folder
  name, photo count per event, count of photos missing GPS/timestamp, and number
  of suspected duplicates. No writes occur.

### 3.5 Copy into structure
- **R-F10** On confirmation, copy (never move) each photo into its event folder
  under the output root. Preserve original filenames; on collision, append a
  numeric suffix rather than overwrite.
- **R-F11** Verify each copy (size and/or checksum match) before considering it
  done. Record success in the manifest.

### 3.6 Duplicate detection
- **R-F12** Detect exact duplicates (content hash) and near-duplicates
  (perceptual hash, configurable similarity threshold — catches resizes,
  re-compressions, burst near-identicals).
- **R-F13** Copy suspected duplicates into `_duplicates_review/`, grouped so the
  user can see which photos are considered duplicates of each other. Never
  delete. The "best" copy heuristic (highest resolution / has EXIF) may be noted
  but not enforced.

### 3.7 Content tagging
- **R-F14** Run a local image-understanding model (CLIP-style) to assign
  category tags from a configurable vocabulary (e.g. mountain, summit, glacier,
  lake, forest, snow, ridge, hut, sunset, panorama, people).
- **R-F15** Write tags into each copied file as XMP/IPTC keywords, so digiKam
  reads them. Do not rely on a sidecar-only approach unless the format can't
  embed (then use XMP sidecars consistently).

### 3.8 Quality scoring
- **R-F16** Compute a technical sharpness score (e.g. variance of Laplacian) and
  flag likely-blurry photos. Note: cannot distinguish intentional blur/bokeh.
- **R-F17** Compute exposure sanity (under/over-exposure) as a secondary signal.
- **R-F18** Compute a rough aesthetic score via an aesthetic model (e.g. a
  NIMA-style or newer model). Treat as a coarse ranking signal, reliable only at
  the extremes.
- **R-F19** Combine into a star rating (1–5) or color label and write it into the
  file metadata for digiKam. "Iconic/meaningful" is explicitly NOT scored — that
  remains the user's judgement.

---

## 4. Non-functional requirements

- **R-N1 Platform.** Runs on Windows on a laptop. No server, no Docker, no
  always-on service.
- **R-N2 Removable drives.** Must tolerate photos and output living on external
  USB drives that are attached on demand. Use paths, not assumptions about a
  fixed library location.
- **R-N3 Batch-friendly.** Heavy steps (CLIP tagging, aesthetic scoring) may be
  slow; support running them as a resumable overnight batch, separate from the
  fast scan/cluster/preview steps.
- **R-N4 Metadata portability.** All tags and ratings are written into the files
  (XMP/IPTC) so they survive copying, cloud upload, and are readable by other
  tools, not locked in a private database.
- **R-N5 Configurable.** Thresholds (time gap, distance, dup similarity), tag
  vocabulary, and model choices are configurable without code edits where
  reasonable.
- **R-N6 Logging.** Every run writes a log and a manifest so actions are
  auditable and reproducible.

---

## 5. Summit naming (detailed)

The original premise here — GPS position plus heading against a peaks
database — was correct in principle and **inapplicable in practice**: this
library has GPS on 41 of 13,881 files and headings on none. The precondition
this section asked to verify was verified, and it failed.

What replaced it keeps the section's core rule intact: *never assert a summit
as fact from vision alone.*

### 5.1 Evidence is ranked by HOW the name was obtained

`evidence_basis`, returned per photo:

| Basis | Meaning | P(correct) |
| --- | --- | --- |
| `sign_in_scene` | written on something at the location: signpost, bus stop, hut board, summit cross, plaque | 0.92 |
| `printed_page` | read from a photographed guidebook, topo, map or screen | 0.72 |
| `landmark_recognition` | recognised from the terrain itself | 0.28 |
| `generic_inference` | inferred from vegetation, rock type, architecture | 0.05 |
| `none` | could not place it | 0.02 |

**Reading beats recognising.** This is the reverse of the intuitive ordering
and it is what this library measured. For photos taken at the Hannibalturm
(Furkapass), terrain recognition answered "Salbitschijen" — a real summit
13 km away, in the same canton and massif, which the gazetteer therefore
accepted. A guidebook page in the same event named the right one. A
wrongly-read name misplaces an event *within* a massif; a hallucinated
summit misplaces it entirely.

A named summit reported with basis `none` is treated as `landmark_recognition`:
naming something while reporting no basis is incoherent output, not weak
output.

### 5.2 Probability, not a comparison chain

**The probability is computed locally, not returned by the model.** The API
returns only `evidence_basis` and a coarse `location_confidence`. Asking a
model how likely it is to be right returns another opinion, not a
calibration — in the adjudicated case it reported "high" for the wrong
summit. The priors below belong to this project and can be tuned against it.


Claims for the same summit across an event's photos combine by noisy-OR:

    P = 1 - Π (1 - strength_i × decay^i)

- `strength` = the prior above, nudged ±10% by the model's stated confidence.
  The nudge is deliberately small: in the one adjudicated case the model said
  "high" and was wrong, while the correct answer carried no confidence at all.
- `decay` = 0.6. Repeat claims within one event are **not independent** — one
  model, one outing, so a wrong idea recurs. Each agreeing photo counts less
  than the last.
- No single claim may exceed 0.95, or one confident photo would saturate and
  no amount of corroboration could ever overtake it.

Worked values at the defaults:

| Evidence | P |
| --- | --- |
| one signboard | 0.92 |
| one guidebook page, high confidence | 0.79 |
| one guidebook page, low confidence | 0.65 |
| **one confident terrain recognition** | **0.31 — below the floor, does not name** |
| three agreeing terrain recognitions | 0.50 |
| five agreeing terrain recognitions | 0.55 |

### 5.3 Text in the frame is read for place names

Two fields carry names, and only one of them may be used for naming:

| Field | Origin | May name an event? |
| --- | --- | --- |
| `place_names_visible` | names **read** off signs, boards, guidebook headings | **yes** |
| `visible_text` | the full verbatim transcription | **yes** |
| `landmarks` | things the model **recognised** | **no** |

`landmarks` is excluded on purpose. Measured on the bench photo, the model
returned `landmarks: ["Bergseehütte"]` — a real hut, 13 km from where the
photo was taken. Recognition does not become reliable by being written into
a different field.


- Names appearing verbatim in `visible_text` are matched against the
  gazetteer and **promoted to peak claims**. Without this the strongest
  evidence in an event can sit unused in a string field — which is exactly
  what happened at the Hannibalturm.
- Matching is **exact, never fuzzy**, and a single word must be 8+ characters,
  so the "DIE POST" on a bus stop cannot match a hamlet called Post.
- Photos flagged `is_personal_document` are never read for names.
- **Limit, stated honestly:** a name a human recognises instantly may be
  unresolvable. The "Hanicity" board at the foot of the Hannibalturm is not in
  OpenStreetMap, so it resolves to nothing.

### 5.4 A read name overrules a recognised one

A summit recognised from terrain is **discarded** when it lies further than
`peak_contradiction_km` (default 30) from a name read out of the same event's
photos. The gazetteer cannot catch this alone: it only knows that a name
exists.

### 5.5 The name and the coordinates cannot disagree

Folder naming and GPS both use the same ranking. They were once tie-broken
independently, which allowed an event foldered under one summit to carry
another summit's coordinates in its files. A test asserts they agree
regardless of photo order.

### 5.6 Position when no summit is found

Per-photo position estimates are never written. Only the largest cluster of
mutually-agreeing estimates across an event is used, and if the photos do not
agree the event gets **no coordinates at all**. An empty GPS field is honest;
a wrong one is not. See `location_agreement_km`, `location_min_agreeing`,
`location_min_fraction`.

Output remains a PROPOSED name, always user-confirmable.

## 5b. Analysis caching — one request per photo, ever

**R-A1** Every photo is analysed **exactly once in its life**. The result is
cached in SQLite keyed by the photo's **content hash**, so a file that is
renamed, moved, re-scanned, re-clustered or duplicated elsewhere is
recognised as already paid for.

**R-A2** The cache stores the **complete API reply verbatim**, not only the
parsed fields. This is what makes R-A1 true across time: when the schema
gains a field, every stored row is **re-parsed from the stored reply**,
offline and free. A schema change must never trigger a re-request.

**R-A3** A stored reply is **never overwritten** by a later write that has
none. A row from a newer version of the software must load in an older one,
and a row from an older version must load in a newer one; unknown fields are
ignored rather than fatal.

**R-A4** The schema deliberately requests **more than the pipeline currently
uses** — `place_names_visible`, `landmarks`, `weather`, `rock_type`,
`gear_visible`, and a free-text `notes`. A photo is only ever sent once, so
anything plausibly wanted later has to be asked for now. Wanting a field
later would otherwise mean paying for the whole library a second time.

**R-A5** **Batch mode is the default** (`use_batch = true`): half the
interactive price, target turnaround 24 hours. Nothing in this pipeline is
interactive. A submitted job is recorded in the `batch_job` table so it
survives the application closing — losing a job name would mean paying twice.

**R-A6** **Every photo is analysed, not a sample** (`photos_per_event = 0`).
Sampling was a false economy: only ~27% of these photos show a placeable
skyline, so any sample misses events whose single identifiable frame was not
picked. Duplicates are detected **before** analysis, so a burst of 30
near-identical frames costs one analysis rather than 30.

**R-A7b** The database must **not** live in a cache directory. A cache is by
definition disposable and this file is the only record of what was paid for.
Default: `~/.photo_organizer/analysis.sqlite3`. A database found in the old
cache location is moved on first run, and a failed move leaves the original
intact rather than half-copied.

**R-A8** Analysed fields that are **not** written into files —
`landmarks` (recognised, not read, and measured wrong), `place_names_visible`
(already drives naming; on a guidebook page they name the region rather than
the location), `gear_visible` (noisy), `notes` (prose) — remain in the
database and can be surfaced later without re-analysing anything.

**R-A5b** Batch state names must be matched on **both** spellings. The live
`generativelanguage` endpoint returns `BATCH_STATE_*`; the documentation and
Vertex use `JOB_STATE_*`. Verified live 2026-08-29. Not recognising "finished"
means polling already-billed results until the 24-hour ceiling and reporting a
timeout.

**R-A7** Indicative cost at batch rates for this library (~13,881 photos,
~9,700 after duplicates): **about $2**, once.

**R-A9** The analyses must be **queryable**: free text across captions, notes,
transcribed text, names, keywords and grades, plus structured filters
(activity, scene, region, range, peak, crag, locality, rock type, season,
time of day, evidence basis, minimum rating, climbing grade) and facet counts
for building filter menus.

**R-A10** Personal documents are excluded from every query result unless
explicitly requested. Flagging them exists so they stay out of everything by
default.

**R-A11** Browsing must stay interactive at **400,000 photos**, verified by
measurement on the target machine rather than assumed. Achieved at: cache hit
3.3 ms, filters 20-57 ms, facets 334 ms, stats 114 ms, full text 284 ms, with
a 2.1 GB database.

This requirement is met with **indexes, not a different database engine**.
Measured before and after: facets 19,661 ms -> 334 ms, grade filter 4,869 ms
-> 22 ms, stats 5,218 ms -> 114 ms. The specific techniques, each of which
matters:

* partial indexes carrying the `is_personal = 0` predicate every query applies;
* composite `(filter, taken_at DESC)` indexes so one index serves both the
  filter and the ordering;
* a `photo_grade` table, since grades are many-per-photo;
* generated columns for fields that live inside the JSON payload;
* a complement index over the rows flagged personal.

**R-A12** No server, no container, no Postgres. Measured at 400k photos there
is no scaling argument for one, and the single-file database is the property
that makes the paid-for analyses easy to back up. See CLAUDE.md.

**R-U1** The control panel binds loopback only and requires a token. This is
not ceremony: `127.0.0.1` is reachable by every program on the machine and by
any web page the user has open, and the API browses directories and copies
files.

**R-U1b** The control panel listens on **both** loopback addresses, `::1`
and `127.0.0.1`. `localhost` resolves to `::1` first on Windows, so an
IPv4-only bind makes the hostname everybody types simply fail. Neither
socket is reachable off the machine; binding `0.0.0.0` or `""` is forbidden.

**R-U2** The control panel requires **no token by default**. A URL token
does not defend against local software -- any program running as the user can
read the token file -- and it is not what stops a web page, which is R-U4.
Its reliable effect was an unbookmarkable URL. `--require-token` restores it
for a genuinely shared machine.

**R-U3** Every request must carry a **loopback `Host`** (`localhost`,
`127.0.0.1`, `::1`). This is the DNS-rebinding defence and it is what makes
R-U2 safe: an attacker can resolve a hostname they own to `127.0.0.1`, and
`Origin` will then legitimately be theirs, but the `Host` they had to send
gives it away.

**R-U4** State-changing requests verify `Origin`, and refuse any value that
is not this server. A browser sets `Origin` itself and a page
cannot forge it, so this holds even if the token leaks. A missing `Origin`
means a non-browser client and is allowed.

---

## 6. Suggested implementation stack (reference, not binding)

- Language: Python.
- EXIF/metadata read + write: a maintained EXIF library plus a tool capable of
  writing XMP/IPTC keywords and ratings (so digiKam reads them).
- Geo: haversine for distance; a reverse-geocoding source (offline dataset or
  configurable API); OpenStreetMap peaks data for summits.
- Duplicates: a content hash for exact matches; a perceptual-hash library for
  near-duplicates.
- Sharpness: an image-processing library (Laplacian variance).
- Content tags: a local CLIP-style model.
- Aesthetic score: a NIMA-style or newer aesthetic model.

Model files should be downloaded once and run locally; no cloud dependency is
required for the core pipeline (a reverse-geocoding API is the only optional
network call).

---

## 7. Recommended build order

1. **Milestone 1 — read-only planner.** Steps 3.1–3.4: scan, cluster, propose
   names, dry-run preview. Writes nothing. Lets the user validate the proposed
   organization on real photos before trusting the tool with copies.
2. **Milestone 2 — safe copy.** Add 3.5 (copy) and 3.6 (duplicate review),
   keeping all safety rules. Now it produces the organized tree.
3. **Milestone 3 — AI enrichment.** Add 3.7 (content tags) and 3.8 (quality
   scoring) as a resumable batch over the already-organized copies.
4. **Milestone 4 — summit naming.** Add section 5, first as GPS-only shortlist,
   then optionally heading-based, gated on whether real photos carry the needed
   EXIF.

Throughout: digiKam is the final human-facing tool for dedup confirmation, face
recognition, tag/rating editing, and browsing. This tool feeds it; it does not
replace it.

**R-U5** A `config.toml` found in the working directory, the program
directory, or `~/.photo_organizer/` is loaded without being named on the
command line, and the terminal reports which file is in effect. Settings that
exist only as code defaults are invisible and look lost.

**R-U6** The control panel sends `Cache-Control: no-store`. It reflects live
state, and a cached copy showed stale values that looked like lost settings.

**R-P1** Duplicate detection runs as part of the main pipeline, not as a
separate action. It writes nothing, so there is nothing to confirm, and
leaving it optional means paying to analyse every frame of a burst. It stays
available as a standalone re-run.

**R-P2** Analysis and copying are available as separate actions, and are also
chainable into a single run.

**R-P3** Copying requires **one explicit confirmation**, from a dialog that
first states the photo count, both folders and the estimated analysis cost.
That dialog is the preview-then-consent CLAUDE.md requires. A typed magic word
is NOT required -- it was friction rather than protection. Copying with no
confirmation at all must remain impossible, and is tested.

**R-P4** A failure in the analysis step must not abort a chained run. The
folders are then named from what is already known and the photos are still
copied: copying is the part that cannot be redone cheaply, and analysis
results are cached anyway.

**R-A13** Cost quoted to the user must be for the photos that are actually
**pending**, never for the whole library. Quoting the full amount on a re-run
overstates it by everything already cached, and discourages re-running
something that is free.

**R-A14** The duplicate pass records each photo's analysis cache key on the
photo, and the analysis stage reuses it. The two must compute the SAME key --
if they ever diverge every photo is paid for twice, and a test asserts they
agree. This also removes a second full read of every file.

**R-F13** Frames with no content are detected and set aside, never deleted.
Two detectors: pixel statistics before analysis for genuinely flat frames
(all black, all white, uniform), and the model's own reading after analysis
for pocket shots.

**R-F14** There must be NO pixel-statistics rule for pocket shots. Measured
on 1,500 photos from the real library, a "dark and low-detail" rule flagged
eleven frames of which ten were photographs (a night food truck, a campfire,
a snowy road at dusk, a climb in a cave). A pocket shot and a night
photograph have the same statistics; the thresholds that catch one delete the
other. A test asserts a dark grainy frame is not called empty.

**R-F15** Rejected frames are copied to `_rejected_review/<reason>/` and
excluded from the paid analysis. Detection is asymmetric by design: a missed
pocket shot is one file in a folder, a wrong rejection is a photograph the
user has to go looking for.

**R-F16** The duplicate kept in the library is chosen on **photographic
merit** -- sharpness, gaze, blinks, composition, rating -- from the model's
reading, not on file size. Every member of a group is analysed so there is
something to compare (627 extra photos on this library, $0.13). Ties fall
back to the file-size ranking, and the reason for each choice is reported.

**R-F17** Near-duplicate group members must be within `NEAR_WINDOW_SECONDS`
(24 hours) of each other. Perceptual similarity alone grouped 13 sets
spanning over 30 days on the real library, one covering 538 days, sweeping 40
unrelated photographs together -- dark night shots hash close to each other.
Measured distribution: 511 groups under a minute, 4 under an hour, none
between an hour and 30 days, 13 over a month. A photo with no timestamp is
still allowed in, so a re-encoded copy that lost its EXIF is not missed.

**R-U7** Credentials may be supplied in a `.env` file next to the program,
loaded at startup from the working directory, the program directory or
`~/.photo_organizer/`. A variable already present in the environment wins
over the file. The file must be gitignored, and its values must never be
logged.

**R-L1** Every run writes a complete log to
`~/.photo_organizer/logs/run-<timestamp>.log`, whether or not one was asked
for, and the path is printed at startup. The last 20 are kept. It lives beside
the database, not in the output tree, so the delete-and-retry recovery never
destroys the record.

**R-L2** An unhandled failure must be logged at ERROR with a full traceback,
and the last frames must also reach the page. A traceback at DEBUG level is
not recorded at the default level, which makes a crash unexplainable.

**R-L3** A job's `status` is the LAST field set when it finishes. Readers poll
it to decide the run is over; setting it before the record is complete lets a
reader see "failed" with an empty explanation.

**R-L4** Counts of failures are reported alongside samples. Ten failures and
five hundred must not look identical.

**R-L5** The console honours `--quiet`; the file never does. A quiet run is
precisely the one that later needs reconstructing.

**R-U8** Results must reach the page as they are produced, not when the run
ends. Identification is streamed over server-sent events at `/api/events`;
each named event publishes its folder name, name source, place, range, region,
agreed coordinates and evidence.

**R-U9** Publishing must never block the pipeline. Each listener has a bounded
queue; a full queue drops the message rather than waiting. A browser tab that
has gone away or cannot keep up must not be able to stall analysis, and the
plan is refetched when the run ends regardless.

**R-U10** A live update must not overwrite a name the user is editing or has
already changed.

**R-U11** Folders that could not be identified carry no badge. A marker on
every folder conveys nothing; region-only and activity-only names are marked
distinctly so a guess is never presented as a find.

**R-A15** A submission must be split into chunks below the API's 2 GiB upload
limit. Measured: 13,748 photos produce 3.68 GB, which is rejected with HTTP
413 after the entire encode. The chunk target is 1.4 GB, giving ~5,500 photos
per job and 3 jobs for this library.

**R-A16** Requests are streamed to a temp file as they are encoded, never
accumulated in memory, and the temp file is removed whether or not the upload
succeeds. Measured: holding them cost 3.7 GB of process memory.

**R-A17** Encoding reports progress at least every 250 photos. It is the
slowest stage in the pipeline and reported nothing at all for 85 minutes.

**R-A18** Every chunk is recorded in `batch_job` before the next begins, and
one failing chunk must not discard the others.

**R-A19** Every cancellation class must be caught by the job runner. A
deliberate Stop reported as a crash hides whether anything actually failed.
