# CLAUDE.md — Photo Organizer Project

This file is auto-loaded by Claude Code. It is the persistent brief for this
project. Read it before doing anything. A fuller reference lives in
`SPECIFICATION.md`.

## Goal

Take an unsorted photo dump from a mobile phone (and later, scattered external
drives) and produce a clean, organized library:

- Photos grouped into events, foldered as `YEAR/EventName_DD_MM`
  (example: `2025/Jungfrau_normalroute_12_07`).
- Content tags written into each file (mountain, summit, lake, snow, etc.).
- Duplicates detected and set aside for review.
- A per-photo quality rating (sharpness + rough aesthetic) written as a star
  rating.

digiKam is used AFTER this script for the mature library work (final dedup
confirmation, face recognition, tag editing, browsing). This script only does
the parts digiKam can't: auto event-clustering, name proposals, content tags,
and quality scoring.

## NON-NEGOTIABLE SAFETY RULES

These rules exist because the source photos are irreplaceable. Never break them.

1. **COPY ONLY. NEVER move, modify, or delete source files.** The script reads
   the source and writes copies to a new output tree. The source is never
   written to.
2. **Treat the source as read-only.** Assume the source folder is mounted
   read-only; never attempt to write, rename, or delete inside it.
3. **Dry-run first, always.** Before any file is copied, print a full preview of
   what WOULD happen (folders to create, file counts, suspected duplicates).
   Only proceed to actual copying after explicit user confirmation.
4. **Never auto-delete duplicates.** Suspected duplicates are COPIED into a
   `_duplicates_review/` folder for the user to judge. The script never deletes.
5. **Non-destructive by construction.** If anything goes wrong, the fix is
   always "delete the output folder and re-run." The source must remain a valid
   fallback at all times.
6. **The user deletes the original dump manually**, only after verifying the
   output and backing it up. The script must never do this.

## Pipeline (high level)

1. Scan source (read-only): read EXIF — timestamp, GPS, heading, camera.
2. Cluster into events by time gap and location jump.
3. Propose event names via reverse-geocoded GPS + date.
4. Dry-run preview (no files written).
5. On confirmation: copy into `YEAR/EventName_DD_MM/`.
6. Duplicate detection → copies to `_duplicates_review/`.
7. Content tagging (local CLIP model) → write XMP keyword tags.
8. Quality scoring (sharpness + aesthetic) → write star rating / color label.
9. User verifies in digiKam, backs up, then manually clears the source dump.

## Summit naming

Do NOT rely on visual pixel recognition to name specific peaks — unreliable for
non-iconic summits and off-angle shots. Instead deduce from GPS position + a
peaks database (e.g. OpenStreetMap named peaks), and use compass heading
(GPSImgDirection) when present to narrow to the visible summit. The script
PROPOSES a name; the user confirms. "Iconic/meaningful" is always a human call.

## Environment

- Windows laptop. Photos on external USB drives. Limited internal SSD.
- No server, no Docker, no always-on services. Everything runs as a local batch
  script, on demand.
- Large libraries: run heavy steps (CLIP tagging, aesthetic scoring) as an
  overnight batch, not live.
- Tags/ratings must be written INTO the files (XMP/IPTC) so they survive and are
  readable by digiKam.

## Build order (suggested first milestone)

Implement steps 1–4 only first (scan → cluster → propose names → dry-run
preview) with ZERO copying, so the proposed organization can be reviewed on
screen before a single file is written. Add copying and the AI steps after that
is trusted.
