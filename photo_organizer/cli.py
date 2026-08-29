"""Command-line entry point.

Milestone 1 is read-only by construction: there is no code path here that
copies a photo. `--commit` is accepted only so it can explain that copying
is not implemented yet, rather than silently doing nothing.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config
from .manifest import (
    apply_edits,
    guard_write_target,
    load_edits_file,
    save_manifest,
    write_names_file,
)
from .planner import build_plan
from .preview import render_preview
from .scan import UnsafePathError
from .webapp import DEFAULT_PORT, serve_app

log = logging.getLogger("photo_organizer")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="photo-organizer",
        description=(
            "Plan an organized photo library from an unsorted dump. "
            "This tool copies only, never moves or deletes, and does nothing "
            "at all until you review the preview."
        ),
        epilog="Copying happens only with --commit, after you review the preview.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    # Both are optional so `--serve` can start with nothing chosen and let
    # the user pick folders in the UI.
    parser.add_argument(
        "source",
        type=Path,
        nargs="?",
        help="Source photo folder (read-only). Optional with --serve.",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        help="Output root for the organized library. Must be outside the source.",
    )
    parser.add_argument("--config", type=Path, help="TOML config file")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    clustering = parser.add_argument_group("clustering")
    clustering.add_argument(
        "--time-gap-hours",
        type=float,
        help="Start a new event after this many hours without a photo",
    )
    clustering.add_argument(
        "--distance-km",
        type=float,
        help="Start a new event after a location jump this large",
    )

    naming = parser.add_argument_group("naming")
    naming.add_argument(
        "--geocode",
        choices=("offline", "nominatim", "none"),
        help="Reverse-geocoding source. 'nominatim' makes network calls.",
    )
    naming.add_argument(
        "--write-names",
        type=Path,
        metavar="FILE",
        help="Export proposed event names to an editable TOML file",
    )
    naming.add_argument(
        "--names",
        type=Path,
        metavar="FILE",
        help="Apply event names and merges from a previously exported TOML file",
    )
    naming.add_argument(
        "--build-gazetteer",
        action="store_true",
        help=(
            "Download the peaks and landforms gazetteer from OpenStreetMap "
            "for the configured countries. One-off; everything is cached "
            "and works offline afterwards."
        ),
    )
    naming.add_argument(
        "--identify",
        action="store_true",
        help=(
            "Name events that have no GPS by reading their content: "
            "photos are analysed once by Gemini and cached locally. "
            "Uses the Batch API at half price. Writes nothing to your photos."
        ),
    )

    ui = parser.add_argument_group("browser UI")
    ui.add_argument(
        "--serve",
        action="store_true",
        help=(
            "Open the control panel in your browser: pick folders, run the "
            "pipeline, and review the result. Starts a temporary server on "
            "127.0.0.1 that exits when you click Quit."
        ),
    )
    ui.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port for the control panel",
    )
    ui.add_argument(
        "--edits",
        type=Path,
        default=Path("photo_plan_edits.toml"),
        metavar="FILE",
        help="Where the UI saves your name edits",
    )
    ui.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not open a browser; just print the URL",
    )

    output = parser.add_argument_group("output")
    output.add_argument(
        "--manifest",
        type=Path,
        metavar="FILE",
        help="Write the full plan as JSON (not written unless given)",
    )
    output.add_argument("--log-file", type=Path, help="Write a run log to this file")
    output.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="List every photo under its event",
    )
    output.add_argument(
        "--all",
        action="store_true",
        help="Show every event instead of the first 50",
    )
    output.add_argument("--quiet", action="store_true", help="Only warnings and errors")

    parser.add_argument(
        "--commit",
        action="store_true",
        help=(
            "Copy into the output tree and write tags into the copies. "
            "The source is never modified."
        ),
    )
    return parser


def setup_logging(quiet: bool, log_file: Path | None) -> None:
    level = logging.WARNING if quiet else logging.INFO
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


def apply_cli_overrides(config: Config, args: argparse.Namespace) -> None:
    if args.time_gap_hours is not None:
        config.cluster.time_gap_hours = args.time_gap_hours
    if args.distance_km is not None:
        config.cluster.distance_km = args.distance_km
    if args.geocode is not None:
        config.geocode.provider = args.geocode


def warn_on_name_collisions(plan) -> None:
    """After user renames, two events may share a folder. Say so; do not
    silently rename, because merging may be exactly what the user intended."""
    seen: dict[str, list[int]] = {}
    for event in plan.events:
        seen.setdefault(f"{event.year}/{event.effective_name}".lower(), []).append(
            event.index
        )
    for key, indices in seen.items():
        if len(indices) > 1:
            log.warning(
                "Events %s all map to %s - they would be merged into one folder.",
                ", ".join(str(i) for i in indices),
                key,
            )


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.quiet, args.log_file)

    if args.commit:
        log.error(
            "--commit is not implemented yet. Milestone 1 is preview-only; "
            "no copying code exists. Re-run without --commit."
        )
        return 2

    try:
        config = Config.load(args.config)
    except (OSError, ValueError, RuntimeError) as exc:
        log.error("Config error: %s", exc)
        return 2
    apply_cli_overrides(config, args)

    if args.build_gazetteer:
        from .peaks import DEFAULT_COUNTRIES, download_landforms, download_peaks, save_peaks

        entries = list(download_peaks(DEFAULT_COUNTRIES, progress=lambda m: log.info("%s", m)))
        entries += download_landforms(DEFAULT_COUNTRIES, progress=lambda m: log.info("%s", m))
        target = save_peaks(entries)
        print(f"Saved {len(entries)} summits and landforms to {target}")
        return 0

    if args.serve:
        # The UI can start empty and let the user pick folders, but if one
        # path is given the other must be too, or the pair is meaningless.
        if bool(args.source) != bool(args.output):
            log.error(
                "Give both a source and --output, or neither and pick them in the UI."
            )
            return 2
        serve_app(
            config,
            args.edits,
            source=args.source,
            output=args.output,
            port=args.port,
            open_browser=not args.no_browser,
        )
        return 0

    if args.source is None or args.output is None:
        log.error(
            "A source folder and --output are required. "
            "Or run with --serve to pick them in the browser."
        )
        return 2

    def progress(count: int, path: Path) -> None:
        if count % 250 == 0:
            log.info("  ...%d images read (%s)", count, path.name)

    try:
        plan = build_plan(args.source, args.output, config, progress=progress)
    except UnsafePathError as exc:
        log.error("Unsafe path: %s", exc)
        return 2
    except ValueError as exc:
        log.error("%s", exc)
        return 2

    if args.names:
        try:
            edits = load_edits_file(args.names)
        except (OSError, ValueError, RuntimeError) as exc:
            log.error("Could not read names file: %s", exc)
            return 2
        renamed, merged = apply_edits(plan, edits)
        log.info(
            "Applied %d event name(s) and %d merge(s)", renamed, merged
        )

    if args.identify:
        from .analyze import AnalysisCancelled, analyze_plan

        last = [0]

        def on_progress(done: int, total: int, label: str) -> None:
            if done - last[0] >= 5 or done == total:
                last[0] = done
                log.info("  identified %d/%d events", done, total)

        try:
            stats = analyze_plan(plan, config, on_progress=on_progress)
        except AnalysisCancelled:
            log.warning("Identification stopped. Nothing was written.")
        else:
            log.info(
                "Identified: %d from a verified peak, %d from a crag, %d from "
                "region, %d from activity, %d still unknown",
                stats.named_from_peak,
                stats.named_from_crag,
                stats.named_from_region,
                stats.named_from_activity,
                stats.still_unknown,
            )

    warn_on_name_collisions(plan)

    if not plan.events:
        print("No images found under the source. Nothing to plan.")
        return 1


    print(render_preview(plan, verbose=args.verbose, max_events=0 if args.all else 50))

    for path, writer, label in (
        (args.write_names, write_names_file, "names file"),
        (args.manifest, save_manifest, "manifest"),
    ):
        if not path:
            continue
        try:
            guard_write_target(path, plan.source_root)
            written = writer(plan, path)
        except (UnsafePathError, OSError) as exc:
            log.error("Could not write %s: %s", label, exc)
            return 2
        print(f"\nWrote {label}: {written}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
