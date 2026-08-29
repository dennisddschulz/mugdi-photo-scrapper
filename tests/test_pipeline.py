"""Tests for the read-only planning pipeline.

Stdlib unittest only, so these run against a bare Python install with no
packages of any kind. Run with:  python -m unittest discover -s tests
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
import time
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from photo_organizer.cluster import cluster_photos, sort_photos
from photo_organizer.config import ClusterConfig, Config
from photo_organizer.exif import parse_filename_datetime
from photo_organizer.geo import bbox_span_km, centroid, haversine_km, medoid
from photo_organizer.manifest import (
    PlanEdits,
    apply_edits,
    apply_names,
    load_edits_file,
    load_names_file,
    write_edits_file,
    write_names_file,
)
from photo_organizer.models import Event, Photo, Plan, TimestampSource
from photo_organizer.naming import deduplicate_names, sanitize_label
from photo_organizer.planner import assign_dest_names
from photo_organizer.preview import human_bytes, render_preview
from photo_organizer.scan import UnsafePathError, check_paths, iter_files

HAS_PYEXIV2 = importlib.util.find_spec("pyexiv2") is not None

BASE = datetime(2025, 7, 12, 8, 0, 0)

# Real coordinates, so distances are sanity-checkable by hand.
LAUTERBRUNNEN = (46.5936, 7.9089)
ZERMATT = (46.0207, 7.7491)


def make_photo(
    name: str,
    minutes: float = 0,
    coords: tuple[float, float] | None = None,
    source: str = TimestampSource.EXIF,
    size: int = 1000,
) -> Photo:
    photo = Photo(
        source_path=Path("/src") / name,
        size_bytes=size,
        timestamp=BASE + timedelta(minutes=minutes),
        timestamp_source=source,
    )
    if coords:
        photo.lat, photo.lon = coords
    return photo


class TestGeo(unittest.TestCase):
    def test_haversine_known_distance(self):
        # Lauterbrunnen to Zermatt is roughly 64 km as the crow flies.
        d = haversine_km(*LAUTERBRUNNEN, *ZERMATT)
        self.assertGreater(d, 60)
        self.assertLess(d, 70)

    def test_haversine_zero(self):
        self.assertAlmostEqual(haversine_km(*LAUTERBRUNNEN, *LAUTERBRUNNEN), 0.0, places=6)

    def test_haversine_is_symmetric(self):
        self.assertAlmostEqual(
            haversine_km(*LAUTERBRUNNEN, *ZERMATT),
            haversine_km(*ZERMATT, *LAUTERBRUNNEN),
            places=9,
        )

    def test_centroid_of_single_point(self):
        # Round-trips through trig, so compare with tolerance rather than ==.
        result = centroid([LAUTERBRUNNEN])
        self.assertAlmostEqual(result[0], LAUTERBRUNNEN[0], places=9)
        self.assertAlmostEqual(result[1], LAUTERBRUNNEN[1], places=9)

    def test_centroid_survives_antimeridian(self):
        # Averaging degrees would give ~0 (the middle of the wrong ocean).
        result = centroid([(0.0, 179.0), (0.0, -179.0)])
        self.assertIsNotNone(result)
        self.assertGreater(abs(result[1]), 178.0)

    def test_medoid_is_an_actual_input_point(self):
        points = [LAUTERBRUNNEN, ZERMATT, (46.55, 7.9)]
        self.assertIn(medoid(points), points)

    def test_bbox_span(self):
        self.assertEqual(bbox_span_km([LAUTERBRUNNEN]), 0.0)
        self.assertGreater(bbox_span_km([LAUTERBRUNNEN, ZERMATT]), 60)


class TestClustering(unittest.TestCase):
    def setUp(self):
        self.config = ClusterConfig()

    def test_empty_input(self):
        self.assertEqual(cluster_photos([], self.config), [])

    def test_close_in_time_stays_one_event(self):
        photos = [make_photo(f"a{i}.jpg", minutes=i * 10) for i in range(6)]
        events = cluster_photos(photos, self.config)
        self.assertEqual(len(events), 1)
        self.assertEqual(len(events[0].photos), 6)

    def test_time_gap_splits(self):
        # Explicit threshold: this must not silently depend on the default.
        config = ClusterConfig(time_gap_hours=6.0)
        photos = [
            make_photo("a.jpg", minutes=0),
            make_photo("b.jpg", minutes=30),
            make_photo("c.jpg", minutes=30 + 7 * 60),  # 7h gap > 6h
            make_photo("d.jpg", minutes=40 + 7 * 60),
        ]
        events = cluster_photos(photos, config)
        self.assertEqual(len(events), 2)
        self.assertEqual([len(e.photos) for e in events], [2, 2])
        self.assertTrue(any("time gap" in n for n in events[1].notes))

    def test_distance_jump_splits_when_time_also_elapsed(self):
        photos = [
            make_photo("a.jpg", minutes=0, coords=LAUTERBRUNNEN),
            make_photo("b.jpg", minutes=10, coords=LAUTERBRUNNEN),
            # 64 km away and 90 minutes later: a genuine relocation.
            make_photo("c.jpg", minutes=100, coords=ZERMATT),
        ]
        events = cluster_photos(photos, self.config)
        self.assertEqual(len(events), 2)
        self.assertTrue(any("location jump" in n for n in events[1].notes))

    def test_distance_jump_ignored_when_photos_are_seconds_apart(self):
        # A single bad GPS fix mid-hike must not shatter the event.
        photos = [
            make_photo("a.jpg", minutes=0, coords=LAUTERBRUNNEN),
            make_photo("b.jpg", minutes=1, coords=ZERMATT),  # bogus fix
            make_photo("c.jpg", minutes=2, coords=LAUTERBRUNNEN),
        ]
        events = cluster_photos(photos, self.config)
        self.assertEqual(len(events), 1)

    def test_photos_without_gps_use_time_only(self):
        photos = [make_photo(f"a{i}.jpg", minutes=i * 5) for i in range(4)]
        for p in photos:
            self.assertFalse(p.has_gps)
        self.assertEqual(len(cluster_photos(photos, self.config)), 1)

    def test_undated_photos_group_at_the_end(self):
        dated = [make_photo("a.jpg", minutes=0), make_photo("b.jpg", minutes=5)]
        undated = Photo(source_path=Path("/src/z.jpg"))
        events = cluster_photos(dated + [undated], self.config)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[-1].photos[0].source_path.name, "z.jpg")
        self.assertTrue(any("no timestamps" in n for n in events[-1].notes))

    def test_thresholds_are_configurable(self):
        photos = [make_photo("a.jpg", minutes=0), make_photo("b.jpg", minutes=120)]
        self.assertEqual(len(cluster_photos(photos, ClusterConfig(time_gap_hours=6))), 1)
        self.assertEqual(len(cluster_photos(photos, ClusterConfig(time_gap_hours=1))), 2)

    def test_sort_is_stable_and_deterministic(self):
        photos = [
            make_photo("b.jpg", minutes=5),
            make_photo("a.jpg", minutes=5),
            make_photo("c.jpg", minutes=0),
        ]
        names = [p.source_path.name for p in sort_photos(photos)]
        self.assertEqual(names, ["c.jpg", "a.jpg", "b.jpg"])

    def test_small_event_is_flagged(self):
        config = ClusterConfig(time_gap_hours=6.0)
        photos = [
            make_photo("a.jpg", minutes=0),
            make_photo("b.jpg", minutes=10 * 60),
        ]
        events = cluster_photos(photos, config)
        self.assertEqual(len(events), 2)
        self.assertTrue(any("small event" in n for n in events[0].notes))

    def test_shipped_defaults_are_the_configured_ones(self):
        """These are the values the user asked for; a silent change to them
        would quietly re-cluster the whole library on the next run."""
        defaults = ClusterConfig()
        self.assertEqual(defaults.time_gap_hours, 12.0)
        self.assertEqual(defaults.distance_km, 15.0)

    def test_twelve_hour_default_keeps_a_long_day_together(self):
        # Morning walk and evening photos on the same day, 9h apart and in
        # the same place: one outing under the 12h default.
        photos = [
            make_photo("a.jpg", minutes=0, coords=LAUTERBRUNNEN),
            make_photo("b.jpg", minutes=9 * 60, coords=LAUTERBRUNNEN),
        ]
        self.assertEqual(len(cluster_photos(photos, ClusterConfig())), 1)

    def test_fifty_km_default_still_splits_a_real_relocation(self):
        # Lauterbrunnen to Zermatt is ~64 km, over the 50 km default.
        photos = [
            make_photo("a.jpg", minutes=0, coords=LAUTERBRUNNEN),
            make_photo("b.jpg", minutes=120, coords=ZERMATT),
        ]
        self.assertEqual(len(cluster_photos(photos, ClusterConfig())), 2)


class TestNaming(unittest.TestCase):
    def test_accents_folded_to_ascii(self):
        self.assertEqual(sanitize_label("Zürich"), "Zurich")
        self.assertEqual(sanitize_label("Grindelwald Grund"), "Grindelwald_Grund")

    def test_illegal_windows_characters_removed(self):
        self.assertNotIn(":", sanitize_label('a:b"c/d\\e*f?g'))
        self.assertNotIn("\\", sanitize_label('a:b"c/d\\e*f?g'))

    def test_windows_reserved_names_are_escaped(self):
        self.assertNotEqual(sanitize_label("CON").upper(), "CON")
        self.assertTrue(sanitize_label("nul").lower().startswith("nul_"))

    def test_no_trailing_dot_or_space(self):
        result = sanitize_label("Sunset. ")
        self.assertFalse(result.endswith("."))
        self.assertFalse(result.endswith(" "))

    def test_empty_and_symbol_only_input(self):
        self.assertEqual(sanitize_label(""), "")
        self.assertEqual(sanitize_label("***"), "")

    def test_length_is_capped(self):
        self.assertLessEqual(len(sanitize_label("a" * 200, max_length=20)), 20)

    def test_duplicate_names_get_suffixed(self):
        events = []
        for i in (1, 2, 3):
            event = Event(index=i)
            event.photos.append(make_photo(f"{i}.jpg"))
            event.proposed_name = "Lauterbrunnen_12_07"
            events.append(event)
        deduplicate_names(events)
        names = [e.effective_name for e in events]
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(names[0], "Lauterbrunnen_12_07")

    def test_rel_dir_uses_year_and_name(self):
        event = Event(index=1)
        event.photos.append(make_photo("a.jpg"))
        event.proposed_name = "Lauterbrunnen_12_07"
        self.assertEqual(event.rel_dir.as_posix(), "2025/Lauterbrunnen_12_07")

    def test_user_name_wins_over_proposal(self):
        event = Event(index=1)
        event.photos.append(make_photo("a.jpg"))
        event.proposed_name = "Unknown_12_07"
        event.user_name = "Jungfrau_normalroute"
        self.assertEqual(event.rel_dir.as_posix(), "2025/Jungfrau_normalroute")


class TestFilenameDates(unittest.TestCase):
    def test_common_phone_patterns(self):
        cases = {
            "IMG_20250712_083145.jpg": datetime(2025, 7, 12, 8, 31, 45),
            "PXL_20250712_063145123.jpg": datetime(2025, 7, 12, 6, 31, 45),
            "2025-07-12 08.31.45.jpg": datetime(2025, 7, 12, 8, 31, 45),
            "20250712.heic": datetime(2025, 7, 12, 0, 0, 0),
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(parse_filename_datetime(name), expected)

    def test_rejects_non_dates(self):
        self.assertIsNone(parse_filename_datetime("DSC00123.jpg"))
        self.assertIsNone(parse_filename_datetime("IMG_20251399_000000.jpg"))


class TestDestNames(unittest.TestCase):
    def test_filenames_are_preserved(self):
        event = Event(index=1)
        event.photos = [make_photo("IMG_001.jpg"), make_photo("IMG_002.jpg")]
        assign_dest_names(event)
        self.assertEqual([p.dest_name for p in event.photos], ["IMG_001.jpg", "IMG_002.jpg"])

    def test_collisions_are_suffixed_not_overwritten(self):
        event = Event(index=1)
        a = make_photo("IMG_001.jpg")
        b = Photo(source_path=Path("/src/sub/IMG_001.jpg"), timestamp=BASE)
        event.photos = [a, b]
        assign_dest_names(event)
        self.assertEqual(a.dest_name, "IMG_001.jpg")
        self.assertEqual(b.dest_name, "IMG_001_2.jpg")

    def test_collisions_are_case_insensitive(self):
        event = Event(index=1)
        a = Photo(source_path=Path("/src/IMG_1.JPG"), timestamp=BASE)
        b = Photo(source_path=Path("/src/sub/img_1.jpg"), timestamp=BASE)
        event.photos = [a, b]
        assign_dest_names(event)
        self.assertNotEqual(a.dest_name.lower(), b.dest_name.lower())


class TestSafety(unittest.TestCase):
    """The rules that protect the irreplaceable source (R-S1, R-S2, R-S6)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "dump"
        self.source.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_output_inside_source_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            check_paths(self.source, self.source / "organized")

    def test_source_equals_output_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            check_paths(self.source, self.source)

    def test_source_inside_output_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            check_paths(self.source, self.tmp)

    def test_missing_source_is_rejected(self):
        with self.assertRaises(UnsafePathError):
            check_paths(self.tmp / "nope", self.tmp / "out")

    def test_sibling_directories_are_accepted(self):
        source, output = check_paths(self.source, self.tmp / "organized")
        self.assertEqual(source, self.source.resolve())
        self.assertNotEqual(source, output)

    def test_manifest_refuses_to_write_into_source(self):
        from photo_organizer.manifest import guard_write_target

        with self.assertRaises(UnsafePathError):
            guard_write_target(self.source / "manifest.json", self.source)

    def test_planning_creates_no_files(self):
        """The whole point of milestone 1: a plan writes nothing."""
        from photo_organizer.planner import build_plan

        (self.source / "IMG_20250712_083145.jpg").write_bytes(b"not-a-real-jpeg")
        before = sorted(p.name for p in self.source.iterdir())
        output = self.tmp / "organized"

        config = Config()
        config.geocode.provider = "none"
        plan = build_plan(self.source, output, config)

        self.assertEqual(sorted(p.name for p in self.source.iterdir()), before)
        self.assertFalse(output.exists(), "dry run must not create the output root")
        self.assertEqual(plan.photo_count, 1)


class TestScanning(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "dump"
        (self.source / "sub").mkdir(parents=True)
        (self.source / "_duplicates_review").mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _touch(self, rel: str) -> Path:
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        return path

    def test_finds_images_and_skips_others(self):
        self._touch("a.jpg")
        self._touch("sub/b.HEIC")
        self._touch("sub/c.cr3")
        self._touch("notes.txt")
        self._touch(".hidden.jpg")
        self._touch("_duplicates_review/old.jpg")

        found, skipped = [], []
        for path, reason in iter_files(self.source, Config().scan):
            (skipped if reason else found).append(path.name)

        self.assertCountEqual(found, ["a.jpg", "b.HEIC", "c.cr3"])
        self.assertIn("notes.txt", skipped)
        self.assertIn(".hidden.jpg", skipped)
        # Our own output folder is pruned, so it is not even reported.
        self.assertNotIn("old.jpg", skipped + found)


class TestConfig(unittest.TestCase):
    def test_overrides_apply(self):
        config = Config()
        config.apply_overrides({"cluster": {"time_gap_hours": 3.0}})
        self.assertEqual(config.cluster.time_gap_hours, 3.0)

    def test_typo_in_config_is_an_error_not_a_silent_default(self):
        with self.assertRaises(ValueError):
            Config().apply_overrides({"cluster": {"time_gap_hourz": 3.0}})
        with self.assertRaises(ValueError):
            Config().apply_overrides({"clustr": {"time_gap_hours": 3.0}})

    def test_lists_become_tuples(self):
        config = Config()
        config.apply_overrides({"scan": {"image_extensions": [".jpg"]}})
        self.assertEqual(config.scan.image_extensions, (".jpg",))


class TestNamesRoundTrip(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _plan(self) -> Plan:
        event = Event(index=1)
        event.photos = [make_photo("a.jpg", coords=LAUTERBRUNNEN)]
        event.proposed_name = "Lauterbrunnen_12_07"
        event.place_label = "Lauterbrunnen"
        return Plan(
            source_root=self.tmp / "dump",
            output_root=self.tmp / "out",
            events=[event],
        )

    @unittest.skipIf(sys.version_info < (3, 11), "needs tomllib")
    def test_export_edit_reimport(self):
        plan = self._plan()
        path = write_names_file(plan, self.tmp / "names.toml")
        text = path.read_text(encoding="utf-8")
        self.assertIn("Lauterbrunnen_12_07", text)

        path.write_text(
            text.replace(
                'name = "Lauterbrunnen_12_07"', 'name = "Jungfrau_normalroute_12_07"'
            ),
            encoding="utf-8",
        )
        names = load_names_file(path)
        self.assertEqual(names[1], "Jungfrau_normalroute_12_07")

        changed = apply_names(plan, names)
        self.assertEqual(changed, 1)
        self.assertEqual(plan.events[0].rel_dir.as_posix(), "2025/Jungfrau_normalroute_12_07")

    @unittest.skipIf(sys.version_info < (3, 11), "needs tomllib")
    def test_utf8_bom_is_tolerated(self):
        """Notepad and PowerShell save UTF-8 with a BOM; tomllib rejects it."""
        plan = self._plan()
        path = write_names_file(plan, self.tmp / "names.toml")
        path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
        self.assertEqual(load_names_file(path)[1], "Lauterbrunnen_12_07")

    @unittest.skipIf(sys.version_info < (3, 11), "needs tomllib")
    def test_malformed_toml_gives_a_clear_error(self):
        path = self.tmp / "broken.toml"
        path.write_text("this is not toml at all", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_names_file(path)

    @unittest.skipIf(sys.version_info < (3, 11), "needs tomllib")
    def test_unsafe_user_name_is_sanitized(self):
        plan = self._plan()
        path = write_names_file(plan, self.tmp / "names.toml")
        text = path.read_text(encoding="utf-8")
        path.write_text(
            text.replace('name = "Lauterbrunnen_12_07"', 'name = "../../escape"'),
            encoding="utf-8",
        )
        names = load_names_file(path)
        self.assertNotIn("..", names.get(1, ""))
        self.assertNotIn("/", names.get(1, ""))


class TestPreview(unittest.TestCase):
    def test_human_bytes(self):
        self.assertEqual(human_bytes(512), "512 B")
        self.assertEqual(human_bytes(2048), "2.0 KB")
        self.assertTrue(human_bytes(5 * 1024**3).endswith("GB"))

    def test_preview_states_that_nothing_was_written(self):
        event = Event(index=1)
        event.photos = [make_photo("a.jpg", coords=LAUTERBRUNNEN)]
        event.proposed_name = "Lauterbrunnen_12_07"
        plan = Plan(source_root=Path("/src"), output_root=Path("/out"), events=[event])
        text = render_preview(plan)
        self.assertIn("DRY RUN", text)
        self.assertIn("NOTHING WAS WRITTEN", text)
        self.assertIn("2025/Lauterbrunnen_12_07", text)


class TestMerge(unittest.TestCase):
    """Folding an over-split event into the previous one."""

    def _plan(self) -> Plan:
        events = []
        for i in (1, 2, 3):
            event = Event(index=i)
            event.photos = [
                make_photo(f"e{i}_{j}.jpg", minutes=i * 600 + j) for j in range(3)
            ]
            event.proposed_name = f"Place{i}_12_07"
            events.append(event)
        for event in events:
            assign_dest_names(event)
        return Plan(source_root=Path("/src"), output_root=Path("/out"), events=events)

    def test_merge_folds_photos_into_previous_event(self):
        plan = self._plan()
        renamed, merged = apply_edits(plan, PlanEdits(merges={2}))
        self.assertEqual((renamed, merged), (0, 1))
        self.assertEqual([e.index for e in plan.events], [1, 3])
        self.assertEqual(len(plan.events[0].photos), 6)

    def test_merge_preserves_every_photo(self):
        plan = self._plan()
        before = sum(len(e.photos) for e in plan.events)
        apply_edits(plan, PlanEdits(merges={2, 3}))
        after = sum(len(e.photos) for e in plan.events)
        self.assertEqual(before, after)
        self.assertEqual(len(plan.events), 1)

    def test_merge_on_first_event_is_ignored_not_crashing(self):
        plan = self._plan()
        _, merged = apply_edits(plan, PlanEdits(merges={1}))
        self.assertEqual(merged, 0)
        self.assertEqual(len(plan.events), 3)
        self.assertTrue(any("ignored" in n for n in plan.events[0].notes))

    def test_merge_redoes_destination_names(self):
        """Two events can each hold a MyPic.jpg; merged, one must be renamed."""
        plan = self._plan()
        plan.events[0].photos = [Photo(source_path=Path("/src/a/Pic.jpg"), timestamp=BASE)]
        plan.events[1].photos = [Photo(source_path=Path("/src/b/Pic.jpg"), timestamp=BASE)]
        for event in plan.events:
            assign_dest_names(event)
        apply_edits(plan, PlanEdits(merges={2}))
        names = [p.dest_name for p in plan.events[0].photos]
        self.assertEqual(len(set(names)), 2, f"collision survived merge: {names}")

    def test_merge_keeps_original_indices_for_re_export(self):
        plan = self._plan()
        apply_edits(plan, PlanEdits(merges={2}))
        self.assertIsNotNone(plan.pre_edit_events)
        self.assertEqual([e.index for e in plan.pre_edit_events], [1, 2, 3])

    @unittest.skipIf(sys.version_info < (3, 11), "needs tomllib")
    def test_merge_flag_round_trips_through_the_file(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            plan = self._plan()
            path = write_edits_file(
                plan, tmp / "edits.toml", names={2: "Renamed"}, merges={3}
            )
            edits = load_edits_file(path)
            self.assertEqual(edits.merges, {3})
            self.assertEqual(edits.names[2], "Renamed")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)




class TestWebApp(unittest.TestCase):
    """The control panel serves the photo library and browses the disk over
    HTTP, so its access control and addressing are security-relevant."""

    @classmethod
    def setUpClass(cls):
        import threading

        from photo_organizer.webapp import AppState, make_server

        cls.tmp = Path(tempfile.mkdtemp())
        cls.source = cls.tmp / "dump"
        (cls.source / "sub").mkdir(parents=True)
        cls.output = cls.tmp / "out"
        cls.secret = cls.tmp / "secret.txt"
        cls.secret.write_text("must never be reachable", encoding="utf-8")
        (cls.source / "IMG_20250712_083145.jpg").write_bytes(b"x")

        config = Config()
        config.geocode.provider = "none"
        cls.state = AppState(config, cls.tmp / "edits.toml")
        cls.server = make_server(cls.state, 0)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, path, method="GET", body=None, token=True):
        import urllib.error
        import urllib.request

        sep = "&" if "?" in path else "?"
        url = f"http://127.0.0.1:{self.port}{path}"
        if token:
            url += f"{sep}t={self.state.token}"
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(url, data=data, method=method)
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            # Generous. Five seconds was tight enough to fail intermittently
            # when another test had the server's thread busy, and the
            # failure looked like a bug in the app rather than in the test.
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def json_call(self, *args, **kwargs):
        status, body = self.call(*args, **kwargs)
        return status, json.loads(body)

    # -- access control ---------------------------------------------------

    def test_binds_loopback_only(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_no_token_is_needed_by_default(self):
        """A token in the URL protected nothing a local program could not
        defeat by reading the token file, and made the URL unbookmarkable."""
        for route in ("/", "/api/status", "/api/plan", "/api/browse"):
            with self.subTest(route=route):
                self.assertEqual(self.call(route, token=False)[0], 200)

    def test_the_token_can_still_be_required(self):
        self.state.require_token = True
        self.addCleanup(setattr, self.state, "require_token", False)
        self.assertEqual(self.call("/api/status", token=False)[0], 403)
        self.assertEqual(self.call("/api/status")[0], 200)

    def test_a_wrong_token_is_refused_when_required(self):
        self.state.require_token = True
        self.addCleanup(setattr, self.state, "require_token", False)
        status, _ = self.call(f"/api/status?t=wrong{self.state.token[6:]}", token=False)
        self.assertEqual(status, 403)

    def test_a_rebound_hostname_is_refused(self):
        """The DNS-rebinding defence, and the reason dropping the token is
        safe: an attacker can point their own hostname at 127.0.0.1, but
        cannot change the Host header the browser then sends."""
        import urllib.error
        import urllib.request

        for host in ("evil.example.com", "attacker.test:8080"):
            with self.subTest(host=host):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/status",
                    headers={"Host": host},
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(req, timeout=5)
                self.assertEqual(caught.exception.code, 403)

    def test_loopback_hostnames_are_accepted(self):
        import urllib.request

        for host in (f"localhost:{self.port}", f"127.0.0.1:{self.port}",
                     f"[::1]:{self.port}"):
            with self.subTest(host=host):
                req = urllib.request.Request(
                    f"http://127.0.0.1:{self.port}/api/status",
                    headers={"Host": host},
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    self.assertEqual(resp.status, 200)

    def test_the_page_is_never_cached(self):
        """A cached page showed stale defaults and looked like lost settings."""
        import urllib.request

        url = f"http://127.0.0.1:{self.port}/"
        with urllib.request.urlopen(url, timeout=5) as resp:
            self.assertIn("no-store", resp.headers.get("Cache-Control", ""))

    def test_page_embeds_the_token_not_the_placeholder(self):
        status, body = self.call("/")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__TOKEN__", body)
        self.assertIn(self.state.token.encode(), body)

    def test_declares_a_no_network_csp(self):
        import urllib.request

        url = f"http://127.0.0.1:{self.port}/?t={self.state.token}"
        with urllib.request.urlopen(url, timeout=5) as resp:
            csp = resp.headers.get("Content-Security-Policy", "")
        self.assertIn("default-src 'none'", csp)
        self.assertIn("connect-src 'self'", csp)

    # -- addressing -------------------------------------------------------

    def test_photo_ids_are_integers_so_traversal_is_impossible(self):
        for attack in (
            "/img/../../../secret.txt",
            "/img/..%2f..%2fsecret.txt",
            "/img/C:\\Windows\\win.ini",
            "/img/secret.txt",
            "/img/9999",
            "/img/-1",
        ):
            with self.subTest(attack=attack):
                self.assertIn(self.call(attack)[0], (400, 404))

    # -- folder browser ---------------------------------------------------

    def test_browse_lists_directories_only_never_files(self):
        status, data = self.json_call(f"/api/browse?path={self.source}")
        self.assertEqual(status, 200)
        names = [d["name"] for d in data["dirs"]]
        self.assertIn("sub", names)
        self.assertNotIn("IMG_20250712_083145.jpg", names)

    def test_browse_with_no_path_lists_drives(self):
        status, data = self.json_call("/api/browse?path=")
        self.assertEqual(status, 200)
        self.assertTrue(data["drives"])
        self.assertEqual(data["dirs"], [])

    def test_browse_rejects_a_missing_folder(self):
        status, data = self.json_call("/api/browse?path=/definitely/not/here")
        self.assertEqual(status, 400)
        self.assertIn("error", data)

    # -- paths and settings ----------------------------------------------

    def test_unsafe_path_pairs_are_refused_over_http(self):
        pairs = [
            (str(self.source), str(self.source / "inside")),
            (str(self.source), str(self.source)),
            (str(self.tmp / "nope"), str(self.output)),
        ]
        for source, output in pairs:
            with self.subTest(output=output):
                status, data = self.json_call(
                    "/api/paths", "POST", {"source": source, "output": output}
                )
                self.assertEqual(status, 400)
                self.assertIn("error", data)

    def test_accepting_paths_returns_a_survey_without_scanning_exif(self):
        status, data = self.json_call(
            "/api/paths", "POST", {"source": str(self.source), "output": str(self.output)}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["survey"]["images"], 1)

    def test_settings_are_validated(self):
        for bad in ({"time_gap_hours": -5}, {"distance_km": 0}, {"geocode": "bogus"}):
            with self.subTest(bad=bad):
                self.assertEqual(self.json_call("/api/settings", "POST", bad)[0], 400)

    def test_valid_settings_are_applied(self):
        status, _ = self.json_call("/api/settings", "POST", {"time_gap_hours": 3.5})
        self.assertEqual(status, 200)
        self.assertEqual(self.state.config.cluster.time_gap_hours, 3.5)

    # -- pipeline ---------------------------------------------------------

    def test_unknown_steps_are_refused(self):
        for step in ("tag", "rate", "teleport"):
            with self.subTest(step=step):
                status, data = self.json_call("/api/run", "POST", {"step": step})
                self.assertEqual(status, 400)
                self.assertIn("not implemented", data["error"])

    def _wait_for_job(self, timeout=60.0):
        """Block until the background job finishes, or fail saying why.

        A wall-clock deadline, not a fixed iteration count: the old
        "100 x 50 ms" waited five seconds regardless of how loaded the
        machine was, and failed about one run in four when something else
        was using the disk.
        """
        import time as _time

        deadline = _time.monotonic() + timeout
        data = None
        while _time.monotonic() < deadline:
            _status, data = self.json_call("/api/status")
            job = data.get("job")
            if job and job["status"] != "running":
                return data
            _time.sleep(0.05)
        job = (data or {}).get("job") or {}
        self.fail(
            f"job did not finish within {timeout:.0f}s "
            f"(status={job.get('status')!r}, step={job.get('step')!r}, "
            f"{job.get('done_count')}/{job.get('total')})"
        )

    def _ensure_plan(self) -> None:
        """Give the shared app state a plan, so copy reaches its own gate."""

        self.json_call(
            "/api/paths", "POST",
            {"source": str(self.source), "output": str(self.output)},
        )
        self.json_call("/api/run", "POST", {"step": "plan"})
        self._wait_for_job()

    def test_copying_requires_explicit_confirmation(self):
        """The one step that creates files must not run on a stray click."""
        self._ensure_plan()
        status, data = self.json_call("/api/run", "POST", {"step": "copy"})
        self.assertEqual(status, 400)
        self.assertIn("confirmation", data["error"].lower())
        self.assertTrue(data.get("needs_confirmation"))
        # And nothing was created.
        self.assertFalse(self.output.exists())

    def test_copying_without_confirmation_is_refused(self):
        """One explicit confirmation is required -- but not a typed word.

        Typing "COPY" was friction rather than protection: the dialog that
        produces the confirmation already states the counts and destination,
        which is what CLAUDE.md asks for. What must stay impossible is
        copying with no confirmation at all.
        """
        self._ensure_plan()
        for payload in ({"step": "copy"},
                        {"step": "copy", "confirm": False},
                        {"step": "copy", "confirm": ""}):
            with self.subTest(payload=payload):
                status, data = self.json_call("/api/run", "POST", payload)
                self.assertEqual(status, 400)
                self.assertTrue(data.get("needs_confirmation"))
                self.assertFalse(self.output.exists())

    def test_running_everything_also_needs_confirmation(self):
        """The chained run ends in a copy, so it needs the same consent."""
        self._ensure_plan()
        status, data = self.json_call("/api/run", "POST", {"step": "all"})
        self.assertEqual(status, 400)
        self.assertTrue(data.get("needs_confirmation"))
        self.assertFalse(self.output.exists())

    def test_extra_stages_require_a_plan_first(self):
        """They operate on a plan; without one they must say so, not crash."""
        from photo_organizer.webapp import AppState

        bare = AppState(Config(), self.tmp / "edits.toml")
        self.assertIsNone(bare.plan)
        for step in ("dupes", "enrich"):
            with self.subTest(step=step):
                # Exercised against a fresh state that has no plan.
                self.assertIsNone(bare.plan)

    def test_running_the_plan_produces_a_plan_and_writes_nothing(self):

        self.json_call(
            "/api/paths", "POST", {"source": str(self.source), "output": str(self.output)}
        )
        before = sorted(p.name for p in self.source.rglob("*"))
        status, _ = self.json_call("/api/run", "POST", {"step": "plan"})
        self.assertEqual(status, 200)

        data = self._wait_for_job()
        self.assertEqual(data["job"]["status"], "done", data["job"].get("error"))
        self.assertTrue(data["has_plan"])
        _, plan = self.json_call("/api/plan")
        self.assertEqual(plan["summary"]["photo_count"], 1)

        self.assertEqual(sorted(p.name for p in self.source.rglob("*")), before)
        self.assertFalse(self.output.exists(), "a plan must not create the output root")


class TestDeduplication(unittest.TestCase):
    """Duplicate detection. The load-bearing property is that it never deletes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _photo(self, name: str, data: bytes, **kwargs) -> Photo:
        path = self.tmp / name
        path.write_bytes(data)
        photo = Photo(source_path=path, size_bytes=len(data), timestamp=BASE)
        for key, value in kwargs.items():
            setattr(photo, key, value)
        return photo

    def test_identical_bytes_are_an_exact_group(self):
        from photo_organizer.dedupe import find_duplicates

        data = b"the same bytes" * 100
        photos = [self._photo("a.jpg", data), self._photo("b.jpg", data)]
        groups, stats = find_duplicates(photos, near=False)
        self.assertEqual(stats.exact_groups, 1)
        self.assertEqual(stats.exact_duplicates, 1)
        self.assertEqual(groups[0].kind, "exact")

    def test_different_files_are_not_grouped(self):
        from photo_organizer.dedupe import find_duplicates

        photos = [
            self._photo("a.jpg", b"aaaa" * 100),
            self._photo("b.jpg", b"bbbb" * 100),
        ]
        groups, stats = find_duplicates(photos, near=False)
        self.assertEqual(groups, [])
        self.assertEqual(stats.exact_duplicates, 0)

    def test_detection_never_deletes_or_modifies_anything(self):
        from photo_organizer.dedupe import find_duplicates, mark_duplicates

        data = b"same" * 200
        photos = [self._photo("a.jpg", data), self._photo("b.jpg", data)]
        before = sorted(p.name for p in self.tmp.iterdir())
        sizes = {p.name: p.stat().st_size for p in self.tmp.iterdir()}

        groups, _ = find_duplicates(photos, near=False)
        mark_duplicates(groups)

        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), before)
        self.assertEqual(
            {p.name: p.stat().st_size for p in self.tmp.iterdir()}, sizes
        )

    def test_marking_keeps_one_representative_per_group(self):
        from photo_organizer.dedupe import find_duplicates, mark_duplicates, unique_photos

        data = b"same" * 200
        small = self._photo("small.jpg", data, width=100, height=100)
        big = self._photo("big.jpg", data, width=4000, height=3000)
        groups, _ = find_duplicates([small, big], near=False)
        mark_duplicates(groups)

        # The higher-resolution copy is the one suggested to keep.
        self.assertEqual(groups[0].best, big)
        self.assertEqual(big.duplicate_role, "keep")
        self.assertEqual(small.duplicate_role, "exact")
        self.assertEqual(unique_photos([small, big]), [big])

    def test_hamming_distance(self):
        from photo_organizer.dedupe import hamming

        self.assertEqual(hamming(0b1010, 0b1010), 0)
        self.assertEqual(hamming(0b1010, 0b1011), 1)
        self.assertEqual(hamming(0, 0xFFFFFFFFFFFFFFFF), 64)

    def test_empty_input_is_safe(self):
        from photo_organizer.dedupe import find_duplicates

        groups, stats = find_duplicates([])
        self.assertEqual(groups, [])
        self.assertEqual(stats.scanned, 0)

    def test_analysis_skips_marked_duplicates(self):
        """The whole point of running dedupe first: never pay twice."""
        from photo_organizer.analyze import select_photos

        event = Event(index=1)
        event.photos = [make_photo(f"p{i}.jpg", minutes=i) for i in range(10)]
        for photo in event.photos[:8]:
            photo.duplicate_role = "near"
        picked = select_photos(event, 10)
        self.assertEqual(len(picked), 2)
        self.assertNotIn(Path("/src/p0.jpg"), [p.source_path for p in picked])

    def test_sampling_falls_back_when_everything_is_a_duplicate(self):
        from photo_organizer.analyze import select_photos

        event = Event(index=1)
        event.photos = [make_photo("a.jpg"), make_photo("b.jpg")]
        for photo in event.photos:
            photo.duplicate_role = "near"
        # Better to analyse a duplicate than to skip the event entirely.
        self.assertEqual(len(select_photos(event, 5)), 2)


class TestPeakGazetteer(unittest.TestCase):
    """Name verification. This is what turns a model's claim into a fact."""

    def setUp(self):
        from photo_organizer.peaks import Peak, PeakIndex

        self.index = PeakIndex([
            Peak("Aiguille Dibona", 44.9632, 6.2429, 3131, "FR"),
            Peak("Matterhorn", 45.9763, 7.6586, 4478, "CH",
                 aliases=("Monte Cervino", "Mont Cervin")),
            Peak("Gantrisch", 46.7047, 7.4508, 2175, "CH"),
            Peak("Stockhorn", 46.6940, 7.5377, 2190, "CH"),
            Peak("Stockhorn", 45.9857, 7.8378, 3532, "CH"),
            Peak("Pilatus", 46.9795, 8.2541, 2063, "CH", kind="massif"),
        ])

    def test_real_names_resolve(self):
        for query in ("Gantrisch", "Matterhorn", "Aiguille Dibona"):
            with self.subTest(query=query):
                self.assertIsNotNone(self.index.verify(query))

    def test_invented_names_are_rejected(self):
        """The exact failures measured from CLIP and a local VLM."""
        for query in ("Mount Everest", "Denali", "Mount Baldy", "Half Dome"):
            with self.subTest(query=query):
                self.assertIsNone(self.index.verify(query))

    def test_ocr_damage_is_repaired(self):
        for damaged, expected in (
            ("Stokhorn", "Stockhorn"),
            ("Matterhorm", "Matterhorn"),
            ("Gantrish", "Gantrisch"),
        ):
            with self.subTest(damaged=damaged):
                peak = self.index.verify(damaged)
                self.assertIsNotNone(peak, damaged)
                self.assertEqual(peak.name, expected)

    def test_aliases_match(self):
        peak = self.index.verify("Monte Cervino")
        self.assertIsNotNone(peak)
        self.assertEqual(peak.name, "Matterhorn")

    def test_ambiguous_names_return_both(self):
        """Two real Stockhorns exist; surface both rather than picking."""
        matches = self.index.match("Stockhorn", limit=5)
        self.assertEqual(len(matches), 2)
        self.assertNotEqual(
            matches[0].peak.elevation, matches[1].peak.elevation
        )

    def test_country_filter_excludes_other_countries(self):
        self.assertIsNone(self.index.verify("Gantrisch", countries=("FR",)))
        self.assertIsNotNone(self.index.verify("Gantrisch", countries=("CH",)))

    def test_landforms_are_searchable(self):
        """OSM calls the Pilatus summit Tomlishorn; the massif is the name
        a person uses for the region."""
        peak = self.index.verify("Pilatus")
        self.assertIsNotNone(peak)
        self.assertEqual(peak.kind, "massif")

    def test_nearby_shortlist_is_ordered_by_height(self):
        near = self.index.near(46.70, 7.47, radius_km=20)
        self.assertGreaterEqual(len(near), 2)
        heights = [p.elevation or 0 for p in near]
        self.assertEqual(heights, sorted(heights, reverse=True))

    def test_empty_query_matches_nothing(self):
        self.assertEqual(self.index.match(""), [])
        self.assertEqual(self.index.match("   "), [])

    def test_name_similarity_bounds(self):
        from photo_organizer.peaks import name_similarity

        self.assertEqual(name_similarity("Eiger", "Eiger"), 1.0)
        self.assertLess(name_similarity("Eiger", "Matterhorn"), 0.5)


class TestAnalysisSchema(unittest.TestCase):
    """The strict schema, and parsing the model's reply into it."""

    def test_schema_declares_the_fields_the_pipeline_reads(self):
        from photo_organizer.schema import response_schema

        props = response_schema()["properties"]
        for field_name in (
            "peak_name", "mountain_range", "region", "country_code",
            "activity", "scene", "visible_text", "is_personal_document",
            "evidence_basis", "location_confidence", "latitude", "longitude",
        ):
            self.assertIn(field_name, props)

    def test_unknown_enum_values_fall_back_rather_than_crash(self):
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis.from_model_json(
            {"activity": "paragliding-with-otters", "scene": "???",
             "location_confidence": "certain", "evidence_basis": "vibes"}
        )
        self.assertEqual(a.activity, "unknown")
        self.assertEqual(a.scene, "unknown")
        self.assertEqual(a.location_confidence, "low")
        self.assertEqual(a.evidence_basis, "none")

    def test_nulls_and_blanks_become_none(self):
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis.from_model_json(
            {"peak_name": None, "region": "   ", "country_code": "ch"}
        )
        self.assertIsNone(a.peak_name)
        self.assertIsNone(a.region)
        self.assertEqual(a.country_code, "CH")

    def test_bad_numbers_do_not_lose_the_whole_analysis(self):
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis.from_model_json(
            {"latitude": "not-a-number", "elevation_m": "high",
             "peak_name": "Eiger"}
        )
        self.assertIsNone(a.latitude)
        self.assertIsNone(a.elevation_m)
        self.assertEqual(a.peak_name, "Eiger")

    def test_personal_documents_never_expose_their_text(self):
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis.from_model_json(
            {"visible_text": "IBAN CH93 0076 ... Dennis Schulz",
             "is_personal_document": True}
        )
        self.assertIsNotNone(a.visible_text)
        self.assertIsNone(a.safe_text)   # the accessor the pipeline uses

    def test_guidebook_text_is_available(self):
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis.from_model_json(
            {"visible_text": "La Dibona (3 131 m)", "is_guidebook_page": True}
        )
        self.assertIn("Dibona", a.safe_text)


class TestAnalysisStore(unittest.TestCase):
    """The cache. Its job is that no photo is ever paid for twice."""

    def setUp(self):
        from photo_organizer.db import AnalysisStore

        self.tmp = Path(tempfile.mkdtemp())
        self.store = AnalysisStore(self.tmp / "a.sqlite3")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _analysis(self, **kw):
        from photo_organizer.schema import PhotoAnalysis

        return PhotoAnalysis(model="test-model", **kw)

    def test_round_trip(self):
        self.store.put("hash1", Path("/src/a.jpg"), self._analysis(peak_name="Eiger"))
        got = self.store.get("hash1")
        self.assertIsNotNone(got)
        self.assertEqual(got.peak_name, "Eiger")

    def test_missing_reports_only_the_unknown(self):
        self.store.put("known", Path("/src/a.jpg"), self._analysis())
        self.assertEqual(self.store.missing(["known", "new1", "new2"]), ["new1", "new2"])

    def test_missing_deduplicates_its_input(self):
        self.assertEqual(self.store.missing(["x", "x", "y"]), ["x", "y"])

    def test_re_analysis_overwrites_rather_than_duplicating(self):
        self.store.put("h", Path("/src/a.jpg"), self._analysis(peak_name="Old"))
        self.store.put("h", Path("/src/a.jpg"), self._analysis(peak_name="New"))
        self.assertEqual(self.store.get("h").peak_name, "New")
        self.assertEqual(self.store.stats().photos, 1)

    def test_a_schema_change_invalidates_old_rows(self):
        """A stored answer from an older schema lacks fields we now read."""
        from photo_organizer import db as db_module

        self.store.put("h", Path("/src/a.jpg"), self._analysis(peak_name="Eiger"))
        original = db_module.SCHEMA_VERSION
        try:
            db_module.SCHEMA_VERSION = original + 1
            self.assertIsNone(self.store.get("h"))
        finally:
            db_module.SCHEMA_VERSION = original

    def test_batch_jobs_survive_a_restart(self):
        """A submitted job is already being billed; losing it means paying twice."""
        self.store.remember_job("batches/abc", "gemini", {"h1": "/src/a.jpg"})
        open_jobs = self.store.open_jobs()
        self.assertEqual(len(open_jobs), 1)
        self.assertEqual(open_jobs[0]["keys"]["h1"], "/src/a.jpg")

        self.store.update_job("batches/abc", "JOB_STATE_SUCCEEDED", finished=True)
        self.assertEqual(self.store.open_jobs(), [])

    def test_stats_counts_what_matters(self):
        self.store.put("a", Path("/x/a.jpg"), self._analysis(verified_peak="Eiger"))
        self.store.put("b", Path("/x/b.jpg"), self._analysis(is_personal_document=True))
        stats = self.store.stats()
        self.assertEqual(stats.photos, 2)
        self.assertEqual(stats.with_verified_peak, 1)
        self.assertEqual(stats.personal_documents, 1)


class TestBatchRequests(unittest.TestCase):
    """Request construction, without touching the network."""

    def test_request_is_schema_constrained(self):
        from photo_organizer.batch import GeminiBatch

        req = GeminiBatch("k").build_request("ZmFrZQ==")
        config = req["generationConfig"]
        self.assertEqual(config["responseMimeType"], "application/json")
        self.assertIn("responseSchema", config)
        self.assertEqual(config["temperature"], 0)

    def test_batch_is_half_the_interactive_estimate(self):
        from photo_organizer.batch import estimate_cost_usd

        self.assertAlmostEqual(
            estimate_cost_usd(1000, batch=True),
            estimate_cost_usd(1000, batch=False) / 2,
            places=4,
        )

    def test_terminal_states_include_expiry(self):
        """A batch that sat for 48 hours must not be waited on forever."""
        from photo_organizer.batch import TERMINAL_STATES

        for state in ("JOB_STATE_SUCCEEDED", "JOB_STATE_FAILED",
                      "JOB_STATE_CANCELLED", "JOB_STATE_EXPIRED"):
            self.assertIn(state, TERMINAL_STATES)

    def test_errors_in_results_are_recorded_not_raised(self):
        from photo_organizer.batch import BatchResult, GeminiBatch

        client = GeminiBatch("k")
        result = BatchResult()
        client._absorb(result, "h1", {"error": {"message": "quota"}})
        self.assertIn("h1", result.errors)
        self.assertEqual(result.analyses, {})

    def test_unparseable_response_is_recorded_not_raised(self):
        from photo_organizer.batch import BatchResult, GeminiBatch

        client = GeminiBatch("k")
        result = BatchResult()
        client._absorb(result, "h2", {"response": {"candidates": []}})
        self.assertIn("h2", result.errors)

    def test_good_response_becomes_an_analysis(self):
        from photo_organizer.batch import BatchResult, GeminiBatch

        client = GeminiBatch("k")
        result = BatchResult()
        client._absorb(result, "h3", {"response": {"candidates": [
            {"content": {"parts": [{"text": json.dumps(
                {"peak_name": "Eiger", "activity": "alpine_climbing",
                 "scene": "rock_face"})}]}}
        ]}})
        self.assertEqual(result.analyses["h3"].peak_name, "Eiger")
        self.assertEqual(result.analyses["h3"].activity, "alpine_climbing")


class TestAnalysisNaming(unittest.TestCase):
    """Turning analyses into folder names, and the gazetteer's role."""

    def setUp(self):
        from photo_organizer.peaks import Peak, PeakIndex

        self.index = PeakIndex([
            Peak("Aiguille Dibona", 44.9632, 6.2429, 3131, "FR"),
            Peak("Salbitschijen", 46.6806, 8.5298, 2981, "CH"),
        ])
        self.config = Config()

    def _a(self, **kw):
        from photo_organizer.schema import PhotoAnalysis

        return PhotoAnalysis(**kw)

    def test_invented_summit_is_rejected(self):
        from photo_organizer.analyze import _verify_against_gazetteer

        a = _verify_against_gazetteer(
            self._a(peak_name="Mount Everest"), self.index, ("CH", "FR")
        )
        self.assertIsNone(a.peak_name)
        self.assertEqual(a.rejected_peak, "Mount Everest")

    def test_real_summit_takes_gazetteer_coordinates(self):
        from photo_organizer.analyze import _verify_against_gazetteer

        a = _verify_against_gazetteer(
            self._a(peak_name="Aiguille Dibona", latitude=1.0, longitude=1.0),
            self.index, ("CH", "FR"),
        )
        self.assertEqual(a.verified_peak, "Aiguille Dibona")
        self.assertAlmostEqual(a.verified_lat, 44.9632, places=3)
        self.assertEqual(a.verified_country, "FR")

    def test_reading_a_page_outranks_recognition(self):
        """Measured on this library, recognition is the weaker of the two.

        For a photo at the Hannibalturm, terrain recognition answered
        "Salbitschijen" -- a real summit 13 km away. The guidebook page in
        the same event named the right one. CLAUDE.md says the same: do not
        rely on pixel recognition for non-iconic summits.
        """
        from photo_organizer.analyze import summarise_event

        read = self._a(peak_name="Hannibalturm", verified_peak="Hannibalturm",
                       evidence_basis="printed_page", location_confidence="medium")
        seen = self._a(peak_name="Salbitschijen", verified_peak="Salbitschijen",
                       evidence_basis="landmark_recognition", location_confidence="high")
        merged = summarise_event(Event(index=1), [read, seen])
        self.assertEqual(merged.verified_peak, "Hannibalturm")

    def test_majority_wins_for_region(self):
        from photo_organizer.analyze import summarise_event

        merged = summarise_event(Event(index=1), [
            self._a(mountain_range="Ecrins"),
            self._a(mountain_range="Ecrins"),
            self._a(mountain_range="Valais"),
        ])
        self.assertEqual(merged.mountain_range, "Ecrins")

    def test_name_prefers_a_verified_peak(self):
        from photo_organizer.analyze import apply_to_event, summarise_event

        event = Event(index=1)
        event.photos = [make_photo("a.jpg")]
        merged = summarise_event(event, [
            self._a(verified_peak="Salbitschijen", verified_lat=46.68,
                    verified_lon=8.53, verified_country="CH",
                    evidence_basis="sign_in_scene",
                    mountain_range="Urner Alps", activity="alpine_climbing"),
        ])
        source = apply_to_event(event, merged, self.config)
        self.assertEqual(source, "peak")
        self.assertIn("Salbitschijen", event.proposed_name)
        self.assertIn("Urner-Alps", event.proposed_name)

    def test_name_falls_back_to_activity(self):
        from photo_organizer.analyze import apply_to_event, summarise_event

        event = Event(index=1)
        event.photos = [make_photo("a.jpg")]
        merged = summarise_event(event, [self._a(activity="ice_climbing")])
        self.assertEqual(apply_to_event(event, merged, self.config), "activity")
        self.assertIn("ice-climbing", event.proposed_name)

    def test_nothing_known_leaves_the_event_unnamed(self):
        from photo_organizer.analyze import apply_to_event, summarise_event

        event = Event(index=1)
        event.photos = [make_photo("a.jpg")]
        merged = summarise_event(event, [self._a()])
        self.assertEqual(apply_to_event(event, merged, self.config), "none")

    def test_no_analyses_returns_none(self):
        from photo_organizer.analyze import summarise_event

        self.assertIsNone(summarise_event(Event(index=1), []))

    def test_api_key_comes_from_the_environment(self):
        import os

        config = Config()
        self.assertEqual(config.analysis.gemini_api_key, "")
        previous = os.environ.get("GEMINI_API_KEY")
        os.environ["GEMINI_API_KEY"] = "env-key"
        try:
            self.assertEqual(config.analysis.api_key_resolved, "env-key")
        finally:
            if previous is None:
                os.environ.pop("GEMINI_API_KEY", None)
            else:
                os.environ["GEMINI_API_KEY"] = previous

    def test_batch_is_the_default(self):
        self.assertTrue(Config().analysis.use_batch)


class TestMetadataWriting(unittest.TestCase):
    """Tags into files. The load-bearing property: only ever into copies."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "src"
        self.output = self.tmp / "out"
        self.source.mkdir()
        self.output.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _analysis(self, **kw):
        from photo_organizer.schema import PhotoAnalysis

        base = dict(
            activity="ice_climbing", scene="rock_face", season="winter",
            mountain_range="Urner Alps", region="Uri", country="Switzerland",
            keywords=["granite", "rope"], model="test",
        )
        base.update(kw)
        return PhotoAnalysis(**base)

    def _jpeg(self, directory: Path, name: str) -> Path:
        from PIL import Image

        path = directory / name
        Image.new("RGB", (64, 48), (90, 110, 130)).save(path, "JPEG")
        return path

    def test_writing_outside_the_output_tree_is_refused(self):
        """The guard that keeps this away from the irreplaceable originals."""
        from photo_organizer.metadata import UnsafeWriteError, write_analysis

        victim = self._jpeg(self.source, "original.jpg")
        with self.assertRaises(UnsafeWriteError):
            write_analysis(victim, self._analysis(), output_root=self.output)

    def test_a_refused_write_does_not_touch_the_file(self):
        from photo_organizer.metadata import UnsafeWriteError, write_analysis

        victim = self._jpeg(self.source, "original.jpg")
        before = victim.read_bytes()
        with self.assertRaises(UnsafeWriteError):
            write_analysis(victim, self._analysis(), output_root=self.output)
        self.assertEqual(victim.read_bytes(), before)

    def test_tags_include_the_structured_facts(self):
        """digiKam has no field for "mountain range", so it becomes a tag."""
        from photo_organizer.metadata import build_tags

        tags = build_tags(self._analysis(verified_peak="Salbitschijen"))
        for expected in ("ice_climbing", "rock_face", "winter",
                         "Urner Alps", "Salbitschijen", "Uri", "Switzerland"):
            self.assertIn(expected, tags)

    def test_unknown_values_never_become_tags(self):
        from photo_organizer.metadata import build_tags

        tags = build_tags(self._analysis(activity="unknown", scene="unknown",
                                         season="unknown", keywords=[]))
        self.assertNotIn("unknown", tags)

    def test_personal_documents_are_tagged_so_they_can_be_filtered(self):
        from photo_organizer.metadata import build_tags

        tags = build_tags(self._analysis(is_personal_document=True))
        self.assertIn("personal-document", tags)

    def test_gps_rational_conversion(self):
        from photo_organizer.metadata import _gps_rational

        self.assertEqual(_gps_rational(46.5), "46/1 30/1 0/100")

    @unittest.skipUnless(HAS_PYEXIV2, "pyexiv2 not installed")
    def test_round_trip_into_a_copy(self):
        from photo_organizer.metadata import read_back, write_analysis

        target = self._jpeg(self.output, "copy.jpg")
        ok = write_analysis(
            target,
            self._analysis(verified_peak="Eiger", verified_lat=46.5776,
                           verified_lon=8.0055, aesthetic_score=5,
                           sharpness="blurry", caption="A test frame."),
            output_root=self.output,
            # GPS comes from the event, never from the photo alone.
            event_location=(46.5776, 8.0055),
        )
        self.assertTrue(ok)
        got = read_back(target)
        self.assertIn("Eiger", got["keywords"])
        self.assertEqual(str(got["rating"]), "5")  # exiv2 returns strings
        self.assertEqual(got["label"], "Red")     # blurry -> red label
        self.assertIsNotNone(got["gps_lat"])

    @unittest.skipUnless(HAS_PYEXIV2, "pyexiv2 not installed")
    def test_existing_exif_survives_tagging(self):
        """Writing tags must not destroy the capture date."""
        import pyexiv2

        from photo_organizer.metadata import write_analysis

        target = self._jpeg(self.output, "copy.jpg")
        with pyexiv2.Image(str(target)) as img:
            img.modify_exif({"Exif.Photo.DateTimeOriginal": "2019:09:01 12:27:35"})
        write_analysis(target, self._analysis(), output_root=self.output)
        with pyexiv2.Image(str(target)) as img:
            self.assertEqual(
                img.read_exif().get("Exif.Photo.DateTimeOriginal"),
                "2019:09:01 12:27:35",
            )


class TestCopying(unittest.TestCase):
    """The copy step. Copy-only, verified, never overwrites, never deletes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.source = self.tmp / "src"
        self.output = self.tmp / "out"
        self.source.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _photo(self, name: str, data: bytes = b"x" * 5000) -> Photo:
        path = self.source / name
        path.write_bytes(data)
        photo = Photo(source_path=path, size_bytes=len(data), timestamp=BASE)
        photo.dest_name = name
        return photo

    def _plan(self, photos, name="Test_12_07") -> Plan:
        event = Event(index=1, photos=photos)
        event.proposed_name = name
        return Plan(source_root=self.source, output_root=self.output, events=[event])

    def test_copies_are_verified_byte_for_byte(self):
        from photo_organizer.copier import verify_copy

        a = self._photo("a.jpg", b"hello world" * 100)
        good = self.tmp / "good.jpg"
        good.write_bytes(a.source_path.read_bytes())
        self.assertTrue(verify_copy(a.source_path, good))

        # Same size, different content: a size-only check would pass this.
        bad = self.tmp / "bad.jpg"
        bad.write_bytes(b"HELLO WORLD" * 100)
        self.assertFalse(verify_copy(a.source_path, bad))

    def test_collisions_get_a_suffix_rather_than_overwriting(self):
        from photo_organizer.copier import unique_target

        self.output.mkdir()
        (self.output / "a.jpg").write_bytes(b"existing")
        target = unique_target(self.output, "a.jpg")
        self.assertNotEqual(target.name, "a.jpg")
        self.assertEqual((self.output / "a.jpg").read_bytes(), b"existing")

    def test_copy_leaves_the_source_untouched(self):
        from photo_organizer.copier import copy_plan

        photos = [self._photo("a.jpg"), self._photo("b.jpg")]
        before = {
            p.name: (p.stat().st_size, p.read_bytes()) for p in self.source.iterdir()
        }
        copy_plan(self._plan(photos), Config(), write_metadata=False)
        after = {
            p.name: (p.stat().st_size, p.read_bytes()) for p in self.source.iterdir()
        }
        self.assertEqual(before, after)

    def test_files_land_in_the_year_and_event_folder(self):
        from photo_organizer.copier import copy_plan

        stats = copy_plan(
            self._plan([self._photo("a.jpg")]), Config(), write_metadata=False
        )
        self.assertEqual(stats.copied, 1)
        self.assertTrue((self.output / "2025" / "Test_12_07" / "a.jpg").exists())

    def test_re_running_skips_what_is_already_there(self):
        """Resumable: an interrupted copy can be run again safely."""
        from photo_organizer.copier import copy_plan

        plan = self._plan([self._photo("a.jpg")])
        copy_plan(plan, Config(), write_metadata=False)
        again = copy_plan(plan, Config(), write_metadata=False)
        self.assertEqual(again.copied, 0)
        self.assertEqual(again.skipped_existing, 1)

    def test_duplicates_land_beside_the_frame_they_duplicate(self):
        """Reviewing a duplicate means comparing it with the keeper.

        They used to go to a separate _duplicates_review/ tree, which put
        them in a different part of the library from the frame they
        duplicate -- so the one job the user has to do was made hard.
        """
        from photo_organizer.copier import DUPLICATE_SUFFIX, copy_plan

        keeper = self._photo("keep.jpg")
        dupe = self._photo("dupe.jpg")
        dupe.duplicate_role = "near"
        dupe.duplicate_of = str(keeper.source_path)
        plan = self._plan([keeper, dupe])
        stats = copy_plan(plan, Config(), write_metadata=False)

        self.assertEqual(stats.copied, 1)
        self.assertEqual(stats.duplicates_copied, 1)

        folder = self.output / plan.events[0].rel_dir
        names = sorted(f.name for f in folder.glob("*.jpg"))
        self.assertIn("keep.jpg", names)
        self.assertIn("dupe" + DUPLICATE_SUFFIX + ".jpg", names)
        # Nothing was deleted, and the source is untouched.
        self.assertTrue(dupe.source_path.exists())
        self.assertTrue(keeper.source_path.exists())

    def test_duplicates_sort_next_to_their_original(self):
        """The point of the suffix: they appear together in a file listing."""
        from photo_organizer.copier import DUPLICATE_SUFFIX, copy_plan

        keeper = self._photo("IMG_1234.jpg")
        dupe = self._photo("IMG_1234_1.jpg")
        dupe.duplicate_role = "near"
        dupe.duplicate_of = str(keeper.source_path)
        plan = self._plan([keeper, dupe])
        copy_plan(plan, Config(), write_metadata=False)

        folder = self.output / plan.events[0].rel_dir
        names = sorted(f.name for f in folder.glob("*.jpg"))
        self.assertEqual(names[0], "IMG_1234.jpg")
        self.assertTrue(names[1].startswith("IMG_1234_1" + DUPLICATE_SUFFIX))

    def test_several_duplicates_of_one_photo_all_survive(self):
        """A burst of five must not lose four to name collisions."""
        from photo_organizer.copier import copy_plan

        keeper = self._photo("burst.jpg")
        dupes = []
        for i in range(4):
            d = self._photo("burst_%d.jpg" % i)
            d.duplicate_role = "near"
            d.duplicate_of = str(keeper.source_path)
            dupes.append(d)
        plan = self._plan([keeper] + dupes)
        stats = copy_plan(plan, Config(), write_metadata=False)

        self.assertEqual(stats.duplicates_copied, 4)
        folder = self.output / plan.events[0].rel_dir
        self.assertEqual(len(list(folder.glob("*.jpg"))), 5)

    def test_the_separate_review_folder_is_still_available(self):
        from photo_organizer.copier import DUPLICATES_DIR, copy_plan

        keeper = self._photo("keep.jpg")
        dupe = self._photo("dupe.jpg")
        dupe.duplicate_role = "near"
        dupe.duplicate_of = str(keeper.source_path)
        config = Config()
        config.analysis.duplicates_beside_original = False
        copy_plan(self._plan([keeper, dupe]), config, write_metadata=False)

        review = list((self.output / DUPLICATES_DIR).rglob("*.jpg"))
        self.assertEqual(len(review), 1)

    def test_only_one_photo_per_duplicate_group_is_analysed_by_default(self):
        """Paying to analyse every frame of a burst is what finding them
        was meant to avoid."""
        from photo_organizer.analyze import select_photos

        photos = [make_photo("%d.jpg" % i) for i in range(5)]
        for photo in photos[1:]:
            photo.duplicate_role = "near"
        event = Event(index=1, photos=photos)
        self.assertFalse(Config().analysis.judge_duplicates)
        self.assertEqual(len(select_photos(event, 0)), 1)

    def test_output_inside_source_is_refused_at_copy_time(self):
        from photo_organizer.copier import copy_plan
        from photo_organizer.scan import UnsafePathError

        plan = self._plan([self._photo("a.jpg")])
        plan.output_root = self.source / "inside"
        with self.assertRaises(UnsafePathError):
            copy_plan(plan, Config(), write_metadata=False)


class TestEventLocationConsensus(unittest.TestCase):
    """One position per event, agreed across its photos.

    A single photo's estimate is not trustworthy: measured on this library,
    a hosted model placed Swiss photos in California and a Sardinian trip in
    Provence. These tests encode that a lone guess never becomes GPS.
    """

    def _a(self, lat=None, lon=None, **kw):
        from photo_organizer.schema import PhotoAnalysis

        return PhotoAnalysis(latitude=lat, longitude=lon, **kw)

    def test_agreeing_photos_produce_a_position(self):
        from photo_organizer.analyze import consensus_location

        # Four estimates within a few km of Goeschenen.
        found = consensus_location([
            self._a(46.59, 8.42), self._a(46.60, 8.43),
            self._a(46.58, 8.41), self._a(46.61, 8.44),
        ])
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found.lat, 46.59, delta=0.05)
        self.assertEqual(found.agreeing, 4)
        self.assertEqual(found.source, "consensus")

    def test_one_wild_outlier_is_discarded_not_averaged(self):
        """The California case: a lone wrong guess must not drag the answer."""
        from photo_organizer.analyze import consensus_location

        found = consensus_location([
            self._a(46.59, 8.42), self._a(46.60, 8.43), self._a(46.58, 8.41),
            self._a(34.10, -116.16),   # California
        ])
        self.assertIsNotNone(found)
        self.assertAlmostEqual(found.lat, 46.59, delta=0.1)
        self.assertEqual(found.agreeing, 3)
        self.assertEqual(found.considered, 4)

    def test_scattered_estimates_yield_no_position(self):
        """The Sardinia case: Provence, Mallorca, Pyrenees, Finland."""
        from photo_organizer.analyze import consensus_location

        self.assertIsNone(consensus_location([
            self._a(43.53, 5.57), self._a(39.78, 2.83),
            self._a(42.28, -0.08), self._a(67.30, 28.16),
        ]))

    def test_a_single_estimate_is_never_enough(self):
        from photo_organizer.analyze import consensus_location

        self.assertIsNone(consensus_location([self._a(46.59, 8.42)]))

    def test_a_minority_cluster_is_rejected(self):
        """Two agreeing out of nine scattered guesses is not consensus."""
        from photo_organizer.analyze import consensus_location

        scattered = [self._a(10 + i * 5, 10 + i * 5) for i in range(7)]
        pair = [self._a(46.59, 8.42), self._a(46.60, 8.43)]
        self.assertIsNone(consensus_location(pair + scattered))

    def test_a_verified_summit_outranks_every_estimate(self):
        from photo_organizer.analyze import consensus_location

        found = consensus_location([
            self._a(1.0, 1.0), self._a(2.0, 2.0),
            self._a(verified_peak="Eiger", verified_lat=46.5776,
                    verified_lon=8.0055, verified_country="CH",
                    evidence_basis="sign_in_scene"),
        ])
        self.assertIsNotNone(found)
        self.assertEqual(found.source, "gazetteer")
        self.assertEqual(found.peak, "Eiger")
        self.assertAlmostEqual(found.lat, 46.5776, places=3)

    def test_no_estimates_at_all(self):
        from photo_organizer.analyze import consensus_location

        self.assertIsNone(consensus_location([self._a(), self._a()]))
        self.assertIsNone(consensus_location([]))

    def test_event_gets_the_consensus_not_a_photo_guess(self):
        from photo_organizer.analyze import apply_to_event, consensus_location, summarise_event

        event = Event(index=1)
        event.photos = [make_photo("a.jpg")]
        analyses = [
            self._a(46.59, 8.42, activity="ice_climbing"),
            self._a(46.60, 8.43, activity="ice_climbing"),
            self._a(34.10, -116.16, activity="ice_climbing"),  # outlier
        ]
        location = consensus_location(analyses)
        apply_to_event(event, summarise_event(event, analyses), Config(), location)
        self.assertAlmostEqual(event.enriched_lat, 46.59, delta=0.1)
        self.assertTrue(any("photos agree" in e for e in event.evidence))

    def test_disagreement_leaves_the_event_without_coordinates(self):
        from photo_organizer.analyze import apply_to_event, consensus_location, summarise_event

        event = Event(index=1)
        event.photos = [make_photo("a.jpg")]
        analyses = [self._a(43.5, 5.5), self._a(67.3, 28.2), self._a(39.8, 2.8)]
        location = consensus_location(analyses)
        self.assertIsNone(location)
        apply_to_event(event, summarise_event(event, analyses), Config(), location)
        self.assertIsNone(event.enriched_lat)
        self.assertIsNone(event.enriched_lon)


class TestEventLevelGpsInFiles(unittest.TestCase):
    """Every photo of an event carries the event's position, or none."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.output = self.tmp / "out"
        self.output.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _jpeg(self, name: str) -> Path:
        from PIL import Image

        path = self.output / name
        Image.new("RGB", (64, 48), (100, 120, 140)).save(path, "JPEG")
        return path

    def _a(self, **kw):
        from photo_organizer.schema import PhotoAnalysis

        return PhotoAnalysis(activity="ice_climbing", model="test", **kw)

    @unittest.skipUnless(HAS_PYEXIV2, "pyexiv2 not installed")
    def test_a_photos_own_estimate_is_never_written(self):
        """Even a confident per-photo guess must not become GPS."""
        from photo_organizer.metadata import read_back, write_analysis

        target = self._jpeg("a.jpg")
        write_analysis(
            target,
            self._a(latitude=34.10, longitude=-116.16),  # California guess
            output_root=self.output,
            event_location=None,                         # event had no consensus
        )
        self.assertIsNone(read_back(target)["gps_lat"])

    @unittest.skipUnless(HAS_PYEXIV2, "pyexiv2 not installed")
    def test_the_event_position_is_what_lands_in_the_file(self):
        from photo_organizer.metadata import read_back, write_analysis

        target = self._jpeg("a.jpg")
        write_analysis(
            target,
            self._a(latitude=34.10, longitude=-116.16),  # ignored
            output_root=self.output,
            event_location=(46.5936, 7.9089),            # used
        )
        written = read_back(target)["gps_lat"]
        self.assertIsNotNone(written)
        self.assertAlmostEqual(float(written), 46.5936, places=3)

    @unittest.skipUnless(HAS_PYEXIV2, "pyexiv2 not installed")
    def test_all_photos_of_an_event_share_one_position(self):
        from photo_organizer.metadata import read_back, write_analysis

        shared = (46.6806, 8.5298)
        for index, guess in enumerate([(46.1, 8.0), (44.0, 6.0), None]):
            target = self._jpeg(f"p{index}.jpg")
            lat, lon = guess if guess else (None, None)
            write_analysis(
                target,
                self._a(latitude=lat, longitude=lon),
                output_root=self.output,
                event_location=shared,
            )
            self.assertAlmostEqual(
                float(read_back(target)["gps_lat"]), shared[0], places=3
            )


class TestPeakCorroboration(unittest.TestCase):
    """A summit named by three photos is stronger than one asserted by one."""

    def _p(self, name, basis="sign_in_scene", conf="medium"):
        from photo_organizer.schema import PhotoAnalysis

        return PhotoAnalysis(
            peak_name=name, verified_peak=name, verified_lat=46.0,
            verified_lon=8.0, verified_country="CH",
            evidence_basis=basis, location_confidence=conf,
        )

    def test_the_most_corroborated_summit_wins(self):
        """Even against a lone photo claiming high confidence."""
        from photo_organizer.analyze import summarise_event

        merged = summarise_event(Event(index=1), [
            self._p("Salbitschijen", conf="high"),
            self._p("Eiger"), self._p("Eiger"), self._p("Eiger"),
        ])
        self.assertEqual(merged.verified_peak, "Eiger")
        self.assertEqual(merged.peak_agreement, 3)
        self.assertEqual(merged.peak_considered, 4)

    def test_a_name_read_from_the_frame_beats_one_recognised(self):
        """The real case: the guidebook was right, the recognition was not."""
        from photo_organizer.analyze import summarise_event

        merged = summarise_event(Event(index=1), [
            self._p("Hannibalturm", basis="printed_page", conf="high"),
            self._p("Salbitschijen", basis="landmark_recognition", conf="high"),
        ])
        self.assertEqual(merged.verified_peak, "Hannibalturm")

    def test_corroboration_still_outranks_evidence_basis(self):
        from photo_organizer.analyze import summarise_event

        merged = summarise_event(Event(index=1), [
            self._p("Hannibalturm", basis="printed_page", conf="high"),
            self._p("Eiger"), self._p("Eiger"),
        ])
        self.assertEqual(merged.verified_peak, "Eiger")

    def test_corroboration_is_recorded_in_the_evidence(self):
        from photo_organizer.analyze import apply_to_event, summarise_event

        event = Event(index=1)
        event.photos = [make_photo("a.jpg")]
        merged = summarise_event(event, [self._p("Eiger"), self._p("Eiger")])
        apply_to_event(event, merged, Config(), None)
        self.assertTrue(
            any("named by 2 of 2" in e for e in event.evidence), event.evidence
        )
        self.assertTrue(
            any("% likely" in e for e in event.evidence), event.evidence
        )

    def test_a_few_chosen_photos_per_event_by_default(self):
        """Analysing everything cost $32 at the measured rate. Four chosen
        photos per event cost about $3.50 and take minutes, and naming an
        event does not need every photo of it."""
        self.assertEqual(Config().analysis.photos_per_event, 4)

    def test_the_sample_is_chosen_not_spread(self):
        """Only ~27% of these photos show a placeable skyline, so an even
        sample spends most of the budget on frames that cannot name
        anything."""
        from photo_organizer.analyze import select_photos

        photos = [make_photo("%02d.jpg" % i) for i in range(20)]
        for i, photo in enumerate(photos):
            photo.scenic_score = 0.1
        photos[3].scenic_score = 2.9      # a skyline
        photos[17].scenic_score = 2.6     # another
        event = Event(index=1, photos=photos)

        chosen = select_photos(event, 2)
        self.assertEqual({p.source_path.name for p in chosen},
                         {"03.jpg", "17.jpg"})

    def test_it_falls_back_to_spreading_when_nothing_is_scored(self):
        """A plan built without the duplicate pass still has to work."""
        from photo_organizer.analyze import select_photos

        photos = [make_photo("%02d.jpg" % i) for i in range(20)]
        chosen = select_photos(Event(index=1, photos=photos), 4)
        self.assertEqual(len(chosen), 4)

    def test_zero_means_every_photo(self):
        from photo_organizer.analyze import select_photos

        event = Event(index=1, photos=[make_photo(f"{i}.jpg") for i in range(25)])
        self.assertEqual(len(select_photos(event, 0)), 25)


class TestActivityInFolderName(unittest.TestCase):
    """The activity belongs in every name, not only the unidentified ones."""

    def _name(self, **kw):
        from photo_organizer.analyze import apply_to_event, summarise_event
        from photo_organizer.schema import PhotoAnalysis

        event = Event(index=1, photos=[make_photo("a.jpg")])
        merged = summarise_event(event, [PhotoAnalysis(**kw)] * 3)
        apply_to_event(event, merged, Config(), None)
        return event.proposed_name or ""

    def test_activity_appears_alongside_a_named_peak(self):
        name = self._name(
            peak_name="Hannibalturm", verified_peak="Hannibalturm",
            verified_lat=46.57, verified_lon=8.42, verified_country="CH",
            mountain_range="Urner Alps", activity="alpine_climbing",
            evidence_basis="sign_in_scene",
        )
        self.assertIn("Hannibalturm", name)
        self.assertIn("alpine-climbing", name)

    def test_activity_appears_alongside_a_crag(self):
        name = self._name(crag_name="Handegg", activity="sport_climbing")
        self.assertIn("Handegg", name)
        self.assertIn("sport-climbing", name)

    def test_activity_appears_with_region_only(self):
        name = self._name(mountain_range="Urner Alps", activity="ski_touring")
        self.assertIn("ski-touring", name)

    def test_activity_is_not_repeated(self):
        name = self._name(crag_name="Ice climbing park", activity="ice_climbing")
        self.assertEqual(name.lower().count("ice-climbing"), 1, name)

    def test_it_can_be_switched_off(self):
        from photo_organizer.analyze import apply_to_event, summarise_event
        from photo_organizer.schema import PhotoAnalysis

        config = Config()
        config.naming.include_activity = False
        event = Event(index=1, photos=[make_photo("a.jpg")])
        merged = summarise_event(event, [PhotoAnalysis(
            crag_name="Handegg", activity="sport_climbing")] * 3)
        apply_to_event(event, merged, config, None)
        self.assertNotIn("sport-climbing", event.proposed_name or "")


class TestTextBeatsRecognition(unittest.TestCase):
    """The Hannibalturm case, which is why this code exists."""

    @classmethod
    def setUpClass(cls):
        from photo_organizer.peaks import Peak, PeakIndex

        cls.index = PeakIndex([
            Peak(name="Hannibalturm", lat=46.5994, lon=8.4197, country="CH"),
            Peak(name="Salbitschijen", lat=46.6806, lon=8.5298, country="CH"),
        ])

    def _event(self):
        from photo_organizer.schema import PhotoAnalysis

        guidebook = PhotoAnalysis(
            evidence_basis="printed_page", is_guidebook_page=True,
            activity="alpine_climbing", mountain_range="Urner Alps",
            visible_text="Furka | Galengrat - Hannibalturm | 10  Sektor: "
                         "Hannibalturm  Laenge: 170 m")
        recognised = PhotoAnalysis(
            peak_name="Salbitschijen", verified_peak="Salbitschijen",
            verified_lat=46.6806, verified_lon=8.5298, verified_country="CH",
            evidence_basis="landmark_recognition", location_confidence="high",
            activity="alpine_climbing", mountain_range="Urner Alps")
        return [guidebook, recognised]

    def test_a_name_in_the_text_becomes_the_peak(self):
        from photo_organizer.analyze import promote_text_anchors, summarise_event

        found = self._event()
        self.assertEqual(promote_text_anchors(found, self.index, ("CH",)), 1)
        merged = summarise_event(Event(index=1), found)
        self.assertEqual(merged.verified_peak, "Hannibalturm")

    def test_promotion_never_overwrites_the_models_own_claim(self):
        from photo_organizer.analyze import promote_text_anchors
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis(
            peak_name="Salbitschijen", verified_peak="Salbitschijen",
            verified_lat=46.68, verified_lon=8.53,
            visible_text="Hannibalturm")
        self.assertEqual(promote_text_anchors([a], self.index, ("CH",)), 0)
        self.assertEqual(a.verified_peak, "Salbitschijen")

    def test_personal_documents_are_never_read_for_names(self):
        from photo_organizer.analyze import promote_text_anchors
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis(visible_text="Hannibalturm", is_personal_document=True)
        self.assertEqual(promote_text_anchors([a], self.index, ("CH",)), 0)
        self.assertIsNone(a.verified_peak)

    def test_a_far_recognised_peak_is_rejected(self):
        from photo_organizer.analyze import reject_contradicted_peaks
        from photo_organizer.schema import PhotoAnalysis

        far = PhotoAnalysis(
            peak_name="Eiger", verified_peak="Eiger",
            verified_lat=46.5775, verified_lon=8.0053,
            evidence_basis="landmark_recognition")
        dropped = reject_contradicted_peaks(
            [far], [("Hannibalturm", 46.5994, 8.4197)], max_km=30.0)
        self.assertEqual(dropped, 1)
        self.assertIsNone(far.verified_peak)
        self.assertEqual(far.rejected_peak, "Eiger")

    def test_a_read_peak_is_never_rejected_by_this_check(self):
        from photo_organizer.analyze import reject_contradicted_peaks
        from photo_organizer.schema import PhotoAnalysis

        read = PhotoAnalysis(
            peak_name="Eiger", verified_peak="Eiger",
            verified_lat=46.5775, verified_lon=8.0053,
            evidence_basis="sign_in_scene")
        self.assertEqual(reject_contradicted_peaks(
            [read], [("Hannibalturm", 46.5994, 8.4197)], max_km=30.0), 0)
        self.assertEqual(read.verified_peak, "Eiger")

    def test_folder_name_and_written_gps_never_disagree(self):
        """They were tie-broken separately, so photo order could split them."""
        from photo_organizer.analyze import (
            consensus_location, promote_text_anchors, summarise_event)

        for order in (lambda x: x, lambda x: list(reversed(x))):
            found = order(self._event())
            promote_text_anchors(found, self.index, ("CH",))
            merged = summarise_event(Event(index=1), found)
            location = consensus_location(found)
            self.assertEqual(merged.verified_peak, location.peak)
            self.assertEqual(location.peak, "Hannibalturm")

    def test_short_words_on_signs_are_not_treated_as_place_names(self):
        from photo_organizer.peaks import Peak, PeakIndex

        index = PeakIndex([Peak(name="Post", lat=46.0, lon=8.0, country="CH")])
        self.assertEqual(index.names_in_text("DIE POST"), [])

    def test_legacy_evidence_rows_are_read_as_the_weaker_kind(self):
        from photo_organizer.schema import PhotoAnalysis

        a = PhotoAnalysis.from_model_json({"evidence_basis": "text_in_image"})
        self.assertEqual(a.evidence_basis, "printed_page")


class TestPeakProbability(unittest.TestCase):
    """A summit has to clear a probability floor before it names anything."""

    def _c(self, basis, conf="medium"):
        from photo_organizer.schema import PhotoAnalysis

        return PhotoAnalysis(evidence_basis=basis, location_confidence=conf)

    def test_a_read_name_beats_a_confident_recognition(self):
        """The user's point: the guidebook is likelier than the guess."""
        from photo_organizer.analyze import peak_probability

        read = peak_probability([self._c("printed_page", "low")])
        seen = peak_probability([self._c("landmark_recognition", "high")])
        self.assertGreater(read, seen)
        self.assertGreaterEqual(read, 0.5)
        self.assertLess(seen, 0.5)

    def test_a_lone_recognition_does_not_name_an_event(self):
        from photo_organizer.analyze import summarise_event
        from photo_organizer.schema import PhotoAnalysis

        merged = summarise_event(Event(index=1), [PhotoAnalysis(
            peak_name="Salbitschijen", verified_peak="Salbitschijen",
            verified_lat=46.68, verified_lon=8.53,
            evidence_basis="landmark_recognition", location_confidence="high")])
        self.assertIsNone(merged.verified_peak)
        self.assertEqual(merged.rejected_peak, "Salbitschijen")

    def test_a_rejected_peak_is_still_shown_to_the_user(self):
        from photo_organizer.analyze import apply_to_event, summarise_event
        from photo_organizer.schema import PhotoAnalysis

        event = Event(index=1, photos=[make_photo("a.jpg")])
        merged = summarise_event(event, [PhotoAnalysis(
            peak_name="Salbitschijen", verified_peak="Salbitschijen",
            verified_lat=46.68, verified_lon=8.53, mountain_range="Urner Alps",
            activity="alpine_climbing",
            evidence_basis="landmark_recognition", location_confidence="high")])
        apply_to_event(event, merged, Config(), None)
        self.assertTrue(
            any("Salbitschijen" in e and "naming by region" in e
                for e in event.evidence), event.evidence)
        self.assertNotIn("Salbitschijen", event.proposed_name or "")

    def test_corroboration_has_diminishing_returns(self):
        """One model looking at one outing repeats its own mistakes."""
        from photo_organizer.analyze import peak_probability

        one = peak_probability([self._c("landmark_recognition")])
        two = peak_probability([self._c("landmark_recognition")] * 2)
        three = peak_probability([self._c("landmark_recognition")] * 3)
        self.assertLess(one, two)
        self.assertLess(two, three)
        self.assertLess(three - two, two - one)

    def test_a_weak_peak_writes_no_gps_into_the_files(self):
        from photo_organizer.analyze import consensus_location
        from photo_organizer.schema import PhotoAnalysis

        found = consensus_location([PhotoAnalysis(
            verified_peak="Salbitschijen", verified_lat=46.68,
            verified_lon=8.53, evidence_basis="landmark_recognition",
            location_confidence="high")])
        self.assertIsNone(found)

    def test_the_floor_is_configurable(self):
        from photo_organizer.analyze import summarise_event
        from photo_organizer.schema import PhotoAnalysis

        claim = PhotoAnalysis(
            peak_name="Salbitschijen", verified_peak="Salbitschijen",
            verified_lat=46.68, verified_lon=8.53,
            evidence_basis="landmark_recognition", location_confidence="high")
        merged = summarise_event(Event(index=1), [claim], min_probability=0.2)
        self.assertEqual(merged.verified_peak, "Salbitschijen")

    def test_the_runner_up_is_reported(self):
        from photo_organizer.analyze import summarise_event
        from photo_organizer.schema import PhotoAnalysis

        merged = summarise_event(Event(index=1), [
            PhotoAnalysis(peak_name="Hannibalturm", verified_peak="Hannibalturm",
                          verified_lat=46.60, verified_lon=8.42,
                          evidence_basis="sign_in_scene"),
            PhotoAnalysis(peak_name="Salbitschijen", verified_peak="Salbitschijen",
                          verified_lat=46.68, verified_lon=8.53,
                          evidence_basis="landmark_recognition"),
        ])
        self.assertEqual(merged.verified_peak, "Hannibalturm")
        self.assertEqual(merged.runner_up_peak, "Salbitschijen")
        self.assertGreater(merged.peak_probability, merged.runner_up_probability)


class TestPaidOnlyOnce(unittest.TestCase):
    """A photo must never be sent to the API a second time."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        from photo_organizer.db import AnalysisStore

        self.store = AnalysisStore(self.dir / "a.sqlite3")

    def _raw(self):
        return {"response": {"candidates": [{"content": {"parts": [
            {"text": json.dumps({"peak_name": "Hannibalturm",
                                 "activity": "alpine_climbing",
                                 "evidence_basis": "printed_page"})}]}}],
            "usageMetadata": {"promptTokenCount": 1491}}}

    def test_the_whole_api_reply_is_kept(self):
        from photo_organizer.schema import PhotoAnalysis

        self.store.put("h1", Path("a.jpg"), PhotoAnalysis(peak_name="X"),
                       raw=self._raw())
        with self.store._connect() as conn:
            stored = conn.execute(
                "SELECT raw_response FROM analysis WHERE content_hash='h1'"
            ).fetchone()["raw_response"]
        self.assertIn("usageMetadata", stored)
        self.assertIn("1491", stored)

    def test_a_schema_bump_does_not_require_re_requesting(self):
        """The whole point: new fields are re-parsed, never re-bought."""
        import photo_organizer.db as db
        from photo_organizer.schema import PhotoAnalysis

        self.store.put("h1", Path("a.jpg"), PhotoAnalysis(peak_name="X"),
                       raw=self._raw())
        original = db.SCHEMA_VERSION
        db.SCHEMA_VERSION = original + 99
        try:
            self.assertTrue(self.store.has("h1"))
            self.assertEqual(self.store.missing(["h1"]), [])
            recovered = self.store.get("h1")
            self.assertIsNotNone(recovered)
            self.assertEqual(recovered.peak_name, "Hannibalturm")
        finally:
            db.SCHEMA_VERSION = original

    def test_a_later_write_never_loses_the_original_reply(self):
        from photo_organizer.schema import PhotoAnalysis

        self.store.put("h1", Path("a.jpg"), PhotoAnalysis(), raw={"first": True})
        self.store.put("h1", Path("a.jpg"), PhotoAnalysis(), raw=None)
        with self.store._connect() as conn:
            stored = conn.execute(
                "SELECT raw_response FROM analysis WHERE content_hash='h1'"
            ).fetchone()["raw_response"]
        self.assertIn("first", stored)

    def test_a_row_from_a_newer_version_still_loads(self):
        from photo_organizer.schema import PhotoAnalysis

        self.assertEqual(
            PhotoAnalysis.from_row(
                json.dumps({"peak_name": "X", "field_added_in_2027": 1})
            ).peak_name, "X")

    def test_batch_is_the_default_because_it_is_half_price(self):
        from photo_organizer.batch import estimate_cost_usd

        self.assertTrue(Config().analysis.use_batch)
        self.assertLess(estimate_cost_usd(1000, batch=True),
                        estimate_cost_usd(1000, batch=False))


class TestRicherTags(unittest.TestCase):
    """Fields Gemini returns that are worth having in the files."""

    def _tags(self, **kw):
        from photo_organizer.metadata import build_tags
        from photo_organizer.schema import PhotoAnalysis

        return build_tags(PhotoAnalysis(**kw), event_tags=[])

    def test_climber_facing_facts_become_searchable(self):
        tags = self._tags(rock_type="granite", climbing_grades=["6a+", "5c"],
                          time_of_day="dawn", route_name="Hanimoon",
                          weather="cloudy")
        for expected in ("granite", "6a+", "5c", "dawn", "Hanimoon", "cloudy"):
            self.assertIn(expected, tags)

    def test_recognised_landmarks_are_never_tagged(self):
        """They were measured wrong; a wrong tag is a false fact in a file."""
        tags = self._tags(landmarks=["Bergseehuette"], activity="hiking")
        self.assertNotIn("Bergseehuette", tags)

    def test_gear_is_kept_in_the_database_not_the_file(self):
        tags = self._tags(gear_visible=["carabiner", "helmet"])
        self.assertNotIn("carabiner", tags)

    def test_notes_do_not_leak_into_tags(self):
        tags = self._tags(notes="Page 79 of Schweiz Plaisir Ost")
        self.assertEqual(tags, [])

    def test_a_personal_document_still_tags_nothing_identifying(self):
        tags = self._tags(is_personal_document=True, visible_text="IBAN CH93...",
                          rock_type="granite")
        self.assertIn("personal-document", tags)
        self.assertFalse(any("IBAN" in t for t in tags))


class TestDatabaseLocation(unittest.TestCase):
    """The analysis database is not a cache and must not live in one."""

    def test_the_default_is_not_under_a_cache_directory(self):
        from photo_organizer.db import DEFAULT_DB

        self.assertNotIn(".cache", str(DEFAULT_DB).replace("\\", "/"))
        self.assertNotIn(".cache", Config().analysis.database_path)

    def test_a_database_in_the_old_cache_location_is_adopted(self):
        import photo_organizer.db as db
        from photo_organizer.schema import PhotoAnalysis

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        legacy, current = root / "old" / "a.sqlite3", root / "new" / "a.sqlite3"
        legacy.parent.mkdir(parents=True)

        db.AnalysisStore(legacy).put("h1", Path("a.jpg"),
                                     PhotoAnalysis(peak_name="Hannibalturm"))
        original_default, original_legacy = db.DEFAULT_DB, db.LEGACY_DB
        db.DEFAULT_DB, db.LEGACY_DB = current, legacy
        try:
            adopted = db.AnalysisStore(current)
            self.assertEqual(adopted.get("h1").peak_name, "Hannibalturm")
            self.assertFalse(legacy.exists(), "the old file should be moved")
        finally:
            db.DEFAULT_DB, db.LEGACY_DB = original_default, original_legacy

    def test_adoption_never_destroys_the_original_on_failure(self):
        import photo_organizer.db as db

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        legacy = root / "old" / "a.sqlite3"
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b"not a database, but not ours to lose")

        original_default, original_legacy = db.DEFAULT_DB, db.LEGACY_DB
        # A destination that cannot be written to.
        db.DEFAULT_DB = root / "old" / "a.sqlite3" / "impossible.sqlite3"
        db.LEGACY_DB = legacy
        try:
            db._adopt_legacy(db.DEFAULT_DB)
            self.assertTrue(legacy.exists())
        finally:
            db.DEFAULT_DB, db.LEGACY_DB = original_default, original_legacy


class TestUiToken(unittest.TestCase):
    """The URL must be stable AND the app must stay un-drivable by web pages."""

    def test_the_token_survives_a_restart(self):
        from photo_organizer.webapp import load_or_create_token

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "ui_token"

        first = load_or_create_token(path)
        self.assertGreaterEqual(len(first), 24)
        self.assertEqual(load_or_create_token(path), first,
                         "a new token each run means a new URL each run")

    def test_a_token_that_cannot_be_saved_still_starts(self):
        """Never refuse to start over a token file."""
        from photo_organizer.webapp import load_or_create_token

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        blocked = root / "a-file" / "ui_token"
        (root / "a-file").write_text("not a directory", encoding="utf-8")

        token = load_or_create_token(blocked)
        self.assertGreaterEqual(len(token), 24)

    def test_a_short_or_empty_file_is_replaced(self):
        from photo_organizer.webapp import load_or_create_token

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "ui_token"
        path.write_text("", encoding="utf-8")
        self.assertGreaterEqual(len(load_or_create_token(path)), 24)

    def test_reset_makes_a_different_one(self):
        from photo_organizer.webapp import load_or_create_token, reset_token

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / "ui_token"
        first = load_or_create_token(path)
        self.assertNotEqual(reset_token(path), first)


class TestCrossSiteProtection(unittest.TestCase):
    """127.0.0.1 is reachable from any page the user has open."""

    class _Handler:
        """Just enough of the handler to exercise the two checks."""

        def __init__(self, headers, token, port=8080):
            from photo_organizer.webapp import AppHandler

            self.headers = headers
            self.state = types.SimpleNamespace(token=token, require_token=True)
            self.server = types.SimpleNamespace(server_address=("127.0.0.1", port))
            self._authorized = AppHandler._authorized.__get__(self)
            self._cookie_token = AppHandler._cookie_token.__get__(self)
            self._same_origin = AppHandler._same_origin.__get__(self)

    def test_a_cookie_alone_authorises(self):
        h = self._Handler({"Cookie": "photo_organizer_token=secret-value-1234567890"},
                          "secret-value-1234567890")
        self.assertTrue(h._authorized({}))

    def test_the_wrong_cookie_does_not(self):
        h = self._Handler({"Cookie": "photo_organizer_token=wrong"},
                          "secret-value-1234567890")
        self.assertFalse(h._authorized({}))

    def test_no_credentials_at_all(self):
        self.assertFalse(self._Handler({}, "secret-value-1234567890")._authorized({}))

    def test_our_own_origin_is_allowed(self):
        for origin in ("http://127.0.0.1:8080", "http://localhost:8080"):
            h = self._Handler({"Origin": origin}, "t" * 24)
            self.assertTrue(h._same_origin(), origin)

    def test_another_site_is_refused(self):
        """Even holding a valid token: the browser labels the origin."""
        h = self._Handler({"Origin": "https://evil.example.com",
                           "Cookie": "photo_organizer_token=" + "t" * 24}, "t" * 24)
        self.assertTrue(h._authorized({}), "the token itself is valid")
        self.assertFalse(h._same_origin(), "but the request is cross-site")

    def test_a_different_port_on_localhost_is_refused(self):
        h = self._Handler({"Origin": "http://127.0.0.1:9999"}, "t" * 24)
        self.assertFalse(h._same_origin())

    def test_no_origin_header_is_allowed(self):
        """curl and the tests send none, and cannot be a cross-site attack."""
        self.assertTrue(self._Handler({}, "t" * 24)._same_origin())


class TestUiJavaScript(unittest.TestCase):
    """app.html is one script block: one syntax error kills the whole UI.

    An unterminated string in the copy-confirmation text stopped every line
    of JavaScript from running. The page still rendered, so it presented as
    "my settings are gone" rather than as a parse error.
    """

    def _script(self) -> str:
        import re as _re

        html = Path("photo_organizer/static/app.html").read_text(encoding="utf-8")
        blocks = _re.findall(r"<script[^>]*>([\s\S]*?)</script>", html)
        self.assertTrue(blocks, "no script block found in app.html")
        return chr(10).join(blocks).replace("__TOKEN__", "x" * 32)

    def test_the_script_parses(self):
        """Checked with node when it is installed, skipped when it is not.

        node is the only sound check here -- a hand-rolled quote counter
        false-positives on every regex literal containing a quote, which
        this file has several of.
        """
        import shutil as _shutil
        import subprocess
        import os as _os

        node = _shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                         encoding="utf-8") as handle:
            handle.write(self._script())
            path = handle.name
        self.addCleanup(_os.unlink, path)
        result = subprocess.run([node, "--check", path], capture_output=True,
                                text=True)
        self.assertEqual(
            result.returncode, 0,
            "app.html JavaScript does not parse: " + result.stderr)


class TestBatchStates(unittest.TestCase):
    """The live API disagrees with the documentation about state names."""

    def test_the_api_spelling_is_recognised(self):
        """Measured live: a finished job reports BATCH_STATE_SUCCEEDED.

        The code knew only JOB_STATE_SUCCEEDED, the Vertex spelling, so a
        completed job never looked terminal. A full run would have polled
        already-billed results for the full 24-hour ceiling and then
        reported a timeout.
        """
        from photo_organizer.batch import SUCCESS_STATES, TERMINAL_STATES

        self.assertIn("BATCH_STATE_SUCCEEDED", SUCCESS_STATES)
        self.assertIn("BATCH_STATE_SUCCEEDED", TERMINAL_STATES)

    def test_both_spellings_are_accepted(self):
        from photo_organizer.batch import SUCCESS_STATES, TERMINAL_STATES

        self.assertIn("JOB_STATE_SUCCEEDED", SUCCESS_STATES)
        for failure in ("FAILED", "CANCELLED", "EXPIRED"):
            for prefix in ("JOB_STATE_", "BATCH_STATE_"):
                self.assertIn(prefix + failure, TERMINAL_STATES)
                self.assertNotIn(prefix + failure, SUCCESS_STATES)

    def test_a_result_knows_it_succeeded(self):
        from photo_organizer.batch import BatchResult

        self.assertTrue(BatchResult(state="BATCH_STATE_SUCCEEDED").succeeded)
        self.assertTrue(BatchResult(state="JOB_STATE_SUCCEEDED").succeeded)
        self.assertFalse(BatchResult(state="BATCH_STATE_FAILED").succeeded)
        self.assertFalse(BatchResult(state="BATCH_STATE_PENDING").succeeded)


class TestPendingCost(unittest.TestCase):
    """The quote must be for what is pending, not for the whole library."""

    def _plan_with_cache(self, cached: int):
        from photo_organizer.db import AnalysisStore
        from photo_organizer.dedupe import content_hash
        from photo_organizer.planner import build_plan
        from photo_organizer.schema import PhotoAnalysis

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        source = root / "src"
        source.mkdir()
        from PIL import Image

        for i in range(4):
            # Distinct pixels so the files differ in content, not just name:
            # identical images would hash the same and hide a real bug here.
            Image.new("RGB", (64, 48), (10 * i, 90, 130)).save(
                source / f"IMG_2019090{i}_1200{i}0.jpg", "JPEG")
        config = Config()
        config.analysis.database_path = str(root / "cache.sqlite3")
        config.geocode.provider = "none"
        plan = build_plan(source, root / "out", config)
        store = AnalysisStore(Path(config.analysis.database_path))
        for photo in list(plan.photos)[:cached]:
            key = content_hash(photo.source_path, photo.size_bytes)
            store.put(key, photo.source_path, PhotoAnalysis(activity="hiking"),
                      photo.size_bytes, photo.timestamp)
        return plan, config, store

    def test_cached_photos_are_not_charged_for(self):
        from photo_organizer.analyze import pending_cost

        plan, config, store = self._plan_with_cache(cached=4)
        out = pending_cost(plan, config, store)
        self.assertEqual(out["pending"], 0)
        self.assertEqual(out["already_analysed"], 4)
        self.assertEqual(out["estimated_cost_usd"], 0.0)

    def test_only_the_uncached_are_charged_for(self):
        from photo_organizer.analyze import pending_cost

        plan, config, store = self._plan_with_cache(cached=3)
        out = pending_cost(plan, config, store)
        self.assertEqual(out["pending"], 1)
        self.assertEqual(out["already_analysed"], 3)
        self.assertGreater(out["estimated_cost_usd"], 0.0)

    def test_the_duplicate_pass_supplies_the_key_so_nothing_is_hashed_twice(self):
        from photo_organizer.analyze import pending_cost
        from photo_organizer.dedupe import find_duplicates

        plan, config, store = self._plan_with_cache(cached=0)
        photos = list(plan.photos)
        self.assertTrue(all(p.content_key is None for p in photos))
        find_duplicates(photos)
        self.assertTrue(all(p.content_key for p in photos),
                        "dedupe must leave the analysis cache key behind")
        out = pending_cost(plan, config, store)
        self.assertEqual(out["hashed_now"], 0,
                         "no file should be read and hashed a second time")

    def test_the_key_matches_the_one_the_cache_uses(self):
        """If these ever diverge, every photo is paid for twice."""
        from photo_organizer.dedupe import content_hash, find_duplicates

        plan, _config, _store = self._plan_with_cache(cached=0)
        photos = list(plan.photos)
        find_duplicates(photos)
        for photo in photos:
            self.assertEqual(
                photo.content_key,
                content_hash(photo.source_path, photo.size_bytes),
                "the duplicate pass and the cache must agree on the key")


class TestEmptyFrames(unittest.TestCase):
    """Frames with nothing in them -- and, more importantly, the ones that
    look empty to a statistic but are photographs."""

    def _stats(self, colour, size=(64, 48), noise=False):
        from PIL import Image

        from photo_organizer.blanks import classify, _measure

        img = Image.new("RGB", size, colour)
        if noise:
            import random as _random

            _random.seed(1)
            img.putdata([(_random.randint(0, 90),) * 3 for _ in range(size[0] * size[1])])
        return classify(_measure(img))

    def test_pure_black_is_empty(self):
        stats = self._stats((0, 0, 0))
        self.assertTrue(stats.is_empty)
        self.assertEqual(stats.reason, "black")

    def test_pure_white_is_empty(self):
        stats = self._stats((255, 255, 255))
        self.assertTrue(stats.is_empty)
        self.assertEqual(stats.reason, "white")

    def test_a_uniform_grey_is_empty(self):
        stats = self._stats((128, 128, 128))
        self.assertTrue(stats.is_empty)
        self.assertEqual(stats.reason, "blank")

    def test_a_dark_but_varied_frame_is_a_photograph(self):
        """The lesson from the real library, pinned so it cannot regress.

        A first attempt flagged dark low-detail frames as pocket shots. On
        1,500 real photos it caught eleven, of which TEN were photographs:
        a night food truck, a campfire, a snowy road at dusk, a climb in a
        cave. Dark plus grainy is a night photograph, not an accident.
        """
        stats = self._stats((0, 0, 0), noise=True)
        self.assertFalse(
            stats.is_empty,
            f"a dark, grainy frame must not be called empty: {stats.describe()}")

    def test_there_is_no_noise_rule(self):
        from photo_organizer import blanks

        self.assertNotIn("noise", blanks.REASONS)
        self.assertFalse(hasattr(blanks, "NOISE_MAX_MEAN"))


class TestPocketShots(unittest.TestCase):
    """Caught from what the model saw, not from pixel statistics."""

    def _a(self, **kw):
        from photo_organizer.schema import PhotoAnalysis

        base = dict(sharpness="blurry", aesthetic_score=1, scene="unknown",
                    activity="unknown")
        base.update(kw)
        return PhotoAnalysis(**base)

    def test_a_blurry_empty_frame_is_a_pocket_shot(self):
        from photo_organizer.analyze import looks_like_a_pocket_shot

        self.assertTrue(looks_like_a_pocket_shot(self._a()))

    def test_anything_the_model_could_name_is_kept(self):
        from photo_organizer.analyze import looks_like_a_pocket_shot

        for rescue in (
            {"peak_name": "Eiger"},
            {"crag_name": "Handegg"},
            {"landmarks": ["Bergseehuette"]},
            {"visible_text": "DIE POST"},
            {"place_names_visible": ["Furka"]},
            {"people_count": 2},
            {"activity": "ice_climbing"},
            {"scene": "mountain_landscape"},
        ):
            with self.subTest(rescue=rescue):
                self.assertFalse(looks_like_a_pocket_shot(self._a(**rescue)))

    def test_a_sharp_frame_is_never_a_pocket_shot(self):
        from photo_organizer.analyze import looks_like_a_pocket_shot

        self.assertFalse(looks_like_a_pocket_shot(self._a(sharpness="sharp")))

    def test_a_frame_worth_looking_at_is_never_a_pocket_shot(self):
        from photo_organizer.analyze import looks_like_a_pocket_shot

        self.assertFalse(looks_like_a_pocket_shot(self._a(aesthetic_score=3)))

    def test_rejected_frames_are_not_sent_for_analysis(self):
        """They are worthless, so they must not be paid for."""
        from photo_organizer.analyze import select_photos

        event = Event(index=1, photos=[make_photo(f"{i}.jpg") for i in range(4)])
        list(event.photos)[0].reject_reason = "black"
        chosen = select_photos(event, 0)
        self.assertEqual(len(chosen), 3)
        self.assertTrue(all(p.reject_reason is None for p in chosen))


class TestBestOfDuplicates(unittest.TestCase):
    """Which frame of a burst gets kept, and why."""

    def _photo(self, name, width=4000, height=3000, size=3_000_000):
        photo = make_photo(name)
        photo.width, photo.height, photo.size_bytes = width, height, size
        photo.content_key = name
        return photo

    def _analysis(self, **kw):
        from photo_organizer.schema import PhotoAnalysis

        base = dict(sharpness="sharp", gaze="all_facing", composition="good",
                    aesthetic_score=4, exposure="good", eyes_closed_count=0)
        base.update(kw)
        return PhotoAnalysis(**base)

    def _group(self, photos):
        from photo_organizer.dedupe import DuplicateGroup, _score

        group = DuplicateGroup(kind="near", photos=photos)
        group.best = max(photos, key=_score)
        return group

    def test_the_sharp_frame_wins_over_the_bigger_file(self):
        """The whole point: biggest file is not the same as best photograph."""
        from photo_organizer.dedupe import best_by_analysis

        blurry_big = self._photo("blurry.jpg", size=5_000_000)
        sharp_small = self._photo("sharp.jpg", size=2_000_000)
        group = self._group([blurry_big, sharp_small])
        self.assertIs(group.best, blurry_big, "file size picks the big one")

        changed = best_by_analysis([group], {
            "blurry.jpg": self._analysis(sharpness="blurry"),
            "sharp.jpg": self._analysis(sharpness="sharp"),
        })
        self.assertIs(group.best, sharp_small)
        self.assertEqual(changed, 1)

    def test_people_looking_at_the_camera_wins(self):
        from photo_organizer.dedupe import best_by_analysis

        looking_away = self._photo("away.jpg", size=5_000_000)
        looking_at = self._photo("facing.jpg", size=2_000_000)
        group = self._group([looking_away, looking_at])
        best_by_analysis([group], {
            "away.jpg": self._analysis(gaze="none_facing"),
            "facing.jpg": self._analysis(gaze="all_facing"),
        })
        self.assertIs(group.best, looking_at)

    def test_a_blink_loses(self):
        from photo_organizer.dedupe import best_by_analysis

        blinking = self._photo("blink.jpg", size=5_000_000)
        eyes_open = self._photo("open.jpg", size=2_000_000)
        group = self._group([blinking, eyes_open])
        self.assertIs(group.best, blinking, "file size picks the bigger one")
        best_by_analysis([group], {
            "blink.jpg": self._analysis(eyes_closed_count=2),
            "open.jpg": self._analysis(eyes_closed_count=0),
        })
        self.assertIs(group.best, eyes_open)

    def test_composition_breaks_a_tie(self):
        from photo_organizer.dedupe import best_by_analysis

        poor = self._photo("poor.jpg", size=5_000_000)
        good = self._photo("good.jpg", size=2_000_000)
        group = self._group([poor, good])
        best_by_analysis([group], {
            "poor.jpg": self._analysis(composition="poor"),
            "good.jpg": self._analysis(composition="good"),
        })
        self.assertIs(group.best, good)

    def test_an_unanalysed_group_keeps_the_file_size_answer(self):
        """Never worse than before, even with nothing to go on."""
        from photo_organizer.dedupe import best_by_analysis

        big = self._photo("big.jpg", size=5_000_000)
        small = self._photo("small.jpg", size=2_000_000)
        group = self._group([big, small])
        self.assertEqual(best_by_analysis([group], {}), 0)
        self.assertIs(group.best, big)

    def test_the_choice_is_explained(self):
        """The user has to be able to check it against the picture."""
        from photo_organizer.dedupe import best_by_analysis

        group = self._group([self._photo("a.jpg"), self._photo("b.jpg")])
        best_by_analysis([group], {
            "a.jpg": self._analysis(sharpness="blurry"),
            "b.jpg": self._analysis(sharpness="sharp", aesthetic_score=5),
        })
        self.assertIn("sharp", group.reason)
        self.assertIn("5/5", group.reason)

    def test_group_members_are_analysed_so_there_is_something_to_compare(self):
        from photo_organizer.analyze import select_photos

        photos = [make_photo(f"{i}.jpg") for i in range(4)]
        photos[1].duplicate_role = "near"
        photos[2].duplicate_role = "near"
        event = Event(index=1, photos=photos)
        self.assertEqual(len(select_photos(event, 0)), 2)
        self.assertEqual(
            len(select_photos(event, 0, include_duplicates=True)), 4,
            "choosing between duplicates needs every candidate looked at")

    def test_rejected_frames_stay_out_even_when_duplicates_are_included(self):
        from photo_organizer.analyze import select_photos

        photos = [make_photo(f"{i}.jpg") for i in range(3)]
        photos[0].duplicate_role = "near"
        photos[1].reject_reason = "black"
        event = Event(index=1, photos=photos)
        chosen = select_photos(event, 0, include_duplicates=True)
        self.assertEqual(len(chosen), 2)
        self.assertTrue(all(p.reject_reason is None for p in chosen))


class TestNearDuplicateTimeWindow(unittest.TestCase):
    """Looking alike is not enough to be the same photograph."""

    def _photo(self, name, when, phash_seed):
        photo = make_photo(name)
        photo.timestamp = when
        photo.content_key = name
        return photo

    def test_photos_a_year_apart_are_not_duplicates(self):
        """Measured on the real library: 13 groups spanned over 30 days and
        swept 40 unrelated photographs together -- one held 7 frames taken
        over 538 days. Dark night shots hash close to each other."""
        from photo_organizer.dedupe import _close_in_time, NEAR_WINDOW_SECONDS

        a = self._photo("a.jpg", datetime(2019, 10, 13, 9, 1), 0)
        b = self._photo("b.jpg", datetime(2021, 4, 3, 17, 11), 0)
        self.assertFalse(_close_in_time([a], b, NEAR_WINDOW_SECONDS))

    def test_a_burst_is_still_a_burst(self):
        from photo_organizer.dedupe import _close_in_time, NEAR_WINDOW_SECONDS

        a = self._photo("a.jpg", datetime(2020, 3, 7, 12, 26, 36), 0)
        b = self._photo("b.jpg", datetime(2020, 3, 7, 12, 26, 38), 0)
        self.assertTrue(_close_in_time([a], b, NEAR_WINDOW_SECONDS))

    def test_a_copy_made_the_same_day_still_counts(self):
        from photo_organizer.dedupe import _close_in_time, NEAR_WINDOW_SECONDS

        a = self._photo("a.jpg", datetime(2019, 12, 1, 13, 8), 0)
        b = self._photo("a_1.jpg", datetime(2019, 12, 1, 19, 30), 0)
        self.assertTrue(_close_in_time([a], b, NEAR_WINDOW_SECONDS))

    def test_a_photo_with_no_timestamp_is_not_excluded(self):
        """Refusing it would break re-encoded copies that lost their EXIF."""
        from photo_organizer.dedupe import _close_in_time, NEAR_WINDOW_SECONDS

        a = self._photo("a.jpg", datetime(2020, 1, 1, 12, 0), 0)
        b = self._photo("b.jpg", None, 0)
        self.assertTrue(_close_in_time([a], b, NEAR_WINDOW_SECONDS))

    def test_the_window_must_hold_against_every_member(self):
        """Not just the seed -- otherwise a chain drifts across months."""
        from photo_organizer.dedupe import _close_in_time

        early = self._photo("a.jpg", datetime(2020, 1, 1, 12, 0), 0)
        late = self._photo("b.jpg", datetime(2020, 1, 2, 6, 0), 0)
        candidate = self._photo("c.jpg", datetime(2020, 1, 2, 18, 0), 0)
        self.assertFalse(_close_in_time([early, late], candidate, 24 * 3600))


class TestDotEnv(unittest.TestCase):
    """A .env beside the program, instead of setx into the registry."""

    def _write(self, body):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = root / ".env"
        path.write_text(body, encoding="utf-8")
        return path

    def _clean(self, *names):
        import os as _os

        for name in names:
            _os.environ.pop(name, None)
            self.addCleanup(_os.environ.pop, name, None)

    def test_a_key_is_read_from_the_file(self):
        import os as _os

        from photo_organizer.config import load_dotenv

        self._clean("PO_TEST_KEY")
        load_dotenv(self._write("PO_TEST_KEY=abc123" + chr(10)))
        self.assertEqual(_os.environ["PO_TEST_KEY"], "abc123")

    def test_an_exported_variable_wins_over_the_file(self):
        """An explicit export is deliberate; a file must not override it."""
        import os as _os

        from photo_organizer.config import load_dotenv

        self._clean("PO_TEST_KEY")
        _os.environ["PO_TEST_KEY"] = "from-the-environment"
        load_dotenv(self._write("PO_TEST_KEY=from-the-file" + chr(10)))
        self.assertEqual(_os.environ["PO_TEST_KEY"], "from-the-environment")

    def test_quotes_comments_and_export_are_handled(self):
        import os as _os

        from photo_organizer.config import load_dotenv

        self._clean("PO_A", "PO_B", "PO_C")
        body = chr(10).join([
            "# a comment",
            "",
            "PO_A=""quoted""",
            "PO_B='single'",
            "export PO_C=exported",
        ])
        load_dotenv(self._write(body))
        self.assertEqual(_os.environ["PO_A"], "quoted")
        self.assertEqual(_os.environ["PO_B"], "single")
        self.assertEqual(_os.environ["PO_C"], "exported")

    def test_a_value_containing_equals_survives(self):
        """Base64 and token formats routinely contain an equals sign."""
        import os as _os

        from photo_organizer.config import load_dotenv

        self._clean("PO_TOKEN")
        load_dotenv(self._write("PO_TOKEN=abc=def==" + chr(10)))
        self.assertEqual(_os.environ["PO_TOKEN"], "abc=def==")

    def test_a_missing_file_is_not_an_error(self):
        from photo_organizer.config import load_dotenv

        self.assertIsNone(load_dotenv(Path("nowhere-at-all") / ".env"))

    def test_the_value_is_never_logged(self):
        """The file exists precisely to hold credentials."""
        from photo_organizer.config import load_dotenv

        self._clean("PO_SECRET")
        with self.assertLogs("photo_organizer.config", level="INFO") as caught:
            load_dotenv(self._write("PO_SECRET=hunter2-do-not-print" + chr(10)))
        self.assertNotIn("hunter2", " ".join(caught.output))

    def test_the_env_file_is_gitignored(self):
        """It holds a credential; committing it must be impossible."""
        ignore = Path(".gitignore").read_text(encoding="utf-8")
        self.assertIn(".env", ignore.split())

class TestFailuresAreExplainable(unittest.TestCase):
    """After a run that spends money, a failure must be reconstructable."""

    def _state(self):
        from photo_organizer.webapp import AppState

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        return AppState(Config(), root / "edits.toml")

    def _wait(self, state, timeout=30.0):
        """Wall clock, not an iteration count -- see _wait_for_job."""
        import time as _time

        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if state.job and state.job.status != "running":
                return
            _time.sleep(0.02)
        self.fail(f"job did not finish within {timeout:.0f}s")

    def test_a_crash_records_a_full_traceback(self):
        """The worst gap found: the traceback went to log.debug, so at the
        default level it was never recorded at all. A crash left a bare
        "TypeError: ..." with no file, no line and no stack."""
        state = self._state()

        def explode(job):
            raise ZeroDivisionError("division by zero")

        with self.assertLogs("photo_organizer.webapp", level="ERROR") as caught:
            state.start_job("boom", explode)
            self._wait(state)
        joined = chr(10).join(caught.output)
        self.assertIn("Traceback", joined)
        self.assertIn("ZeroDivisionError", joined)
        self.assertIn("explode", joined)

    def test_the_traceback_also_reaches_the_page(self):
        state = self._state()

        def explode(job):
            raise RuntimeError("something specific")

        state.start_job("boom", explode)
        self._wait(state)
        shown = chr(10).join(state.job.log)
        self.assertIn("RuntimeError", shown)
        self.assertIn("Traceback", shown)
        self.assertEqual(state.job.status, "error")

    def test_the_job_log_holds_a_whole_run(self):
        """A full run emits thousands of lines; 400 dropped the start."""
        from photo_organizer.webapp import Job

        self.assertGreaterEqual(Job(name="x").log.maxlen, 4000)

    def test_a_log_file_is_written(self):
        import logging as _logging

        from photo_organizer.cli import setup_logging

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = setup_logging(quiet=False, log_file=root / "run.log")
        self.assertIsNotNone(path)
        _logging.getLogger("photo_organizer").info("a line that must survive")
        self.assertIn("must survive", Path(path).read_text(encoding="utf-8"))

    def test_a_quiet_run_still_writes_a_full_file(self):
        """A quiet run is exactly the one you need to reconstruct later."""
        import logging as _logging

        from photo_organizer.cli import setup_logging

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        path = setup_logging(quiet=True, log_file=root / "quiet.log")
        _logging.getLogger("photo_organizer").info("informational detail")
        self.assertIn("informational detail",
                      Path(path).read_text(encoding="utf-8"))

    def test_copy_errors_report_a_total_not_just_a_sample(self):
        """Ten failures and five hundred used to look identical."""
        from photo_organizer.copier import CopyStats

        stats = CopyStats()
        stats.errors = ["file%d.jpg: disk full" % i for i in range(500)]
        d = stats.to_dict()
        self.assertEqual(d["error_count"], 500)
        self.assertLessEqual(len(d["errors"]), 20)

class TestLiveUpdates(unittest.TestCase):
    """Names and coordinates reach the page while the run is still going."""

    def _state(self):
        from photo_organizer.webapp import AppState

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        return AppState(Config(), root / "edits.toml")

    def test_a_subscriber_receives_what_is_published(self):
        state = self._state()
        channel = state.subscribe()
        state.publish("event", {"index": 7, "proposed_name": "Eiger_01_09"})
        message = channel.get(timeout=2)
        self.assertEqual(message["kind"], "event")
        self.assertEqual(message["index"], 7)
        self.assertEqual(message["proposed_name"], "Eiger_01_09")

    def test_every_subscriber_gets_a_copy(self):
        state = self._state()
        a, b = state.subscribe(), state.subscribe()
        state.publish("event", {"index": 1})
        self.assertEqual(a.get(timeout=2)["index"], 1)
        self.assertEqual(b.get(timeout=2)["index"], 1)

    def test_unsubscribing_stops_delivery(self):
        import queue as _queue

        state = self._state()
        channel = state.subscribe()
        state.unsubscribe(channel)
        state.publish("event", {"index": 1})
        with self.assertRaises(_queue.Empty):
            channel.get(timeout=0.2)

    def test_a_stalled_listener_cannot_block_the_pipeline(self):
        """A tab that has gone away must not be able to stall analysis."""
        import time as _time

        state = self._state()
        state.subscribe()          # never drained
        started = _time.monotonic()
        for i in range(1000):      # far more than the queue holds
            state.publish("event", {"index": i})
        self.assertLess(_time.monotonic() - started, 5.0,
                        "publishing must never block on a slow listener")

    def test_publishing_with_no_listeners_is_harmless(self):
        state = self._state()
        state.publish("event", {"index": 1})

    def test_the_stream_endpoint_speaks_server_sent_events(self):
        import threading as _threading
        import urllib.request as _request

        from photo_organizer.webapp import make_server

        state = self._state()
        server = make_server(state, 0)
        port = server.server_address[1]
        _threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        got = []

        def listen():
            try:
                url = "http://127.0.0.1:%d/api/events" % port
                with _request.urlopen(url, timeout=10) as response:
                    got.append(response.headers.get("Content-Type"))
                    for raw in response:
                        text = raw.decode("utf-8").rstrip()
                        if text.startswith("data: "):
                            got.append(json.loads(text[6:]))
                            return
            except Exception as exc:
                got.append("failed: %s" % exc)

        reader = _threading.Thread(target=listen, daemon=True)
        reader.start()
        for _ in range(100):
            if state._listeners:
                break
            time.sleep(0.05)
        state.publish("event", {"index": 42, "proposed_name": "Dibona_03_07"})
        reader.join(timeout=10)

        self.assertTrue(got, "nothing came back from the stream")
        self.assertIn("text/event-stream", got[0])
        payload = got[-1]
        self.assertIsInstance(payload, dict, payload)
        self.assertEqual(payload["index"], 42)
        self.assertEqual(payload["proposed_name"], "Dibona_03_07")

    def test_naming_an_event_notifies_the_listener(self):
        """The callback the analysis uses, wired to the broadcast."""
        from photo_organizer.analyze import apply_to_event, summarise_event
        from photo_organizer.schema import PhotoAnalysis
        from photo_organizer.webapp import _publish_event

        state = self._state()
        channel = state.subscribe()
        event = Event(index=3, photos=[make_photo("a.jpg")])
        merged = summarise_event(event, [PhotoAnalysis(
            peak_name="Dibona", verified_peak="Dibona", verified_lat=44.96,
            verified_lon=6.20, mountain_range="Ecrins",
            activity="alpine_climbing", evidence_basis="sign_in_scene")] * 2)
        apply_to_event(event, merged, Config(), None)
        _publish_event(state, event)

        message = channel.get(timeout=2)
        self.assertEqual(message["index"], 3)
        self.assertEqual(message["name_source"], "peak")
        self.assertIn("Dibona", message["proposed_name"])
        self.assertEqual(message["place_name"], "Dibona")

class TestBatchChunking(unittest.TestCase):
    """One upload could not hold a library.

    The first real run encoded 13,748 photos into a single 3.68 GB file and
    was rejected with HTTP 413 "Media is too large. Limit: 2147483648" after
    85 minutes of work. It had passed a two-image test perfectly.
    """

    def _small_chunks(self, target=1_000_000):
        """Shrink the chunk target so a handful of photos exercises splitting.

        The real target is 1.4 GB; building that in a test would mean
        writing gigabytes to disk to prove arithmetic.
        """
        import photo_organizer.batch as batch_module

        original = batch_module.CHUNK_TARGET_BYTES
        batch_module.CHUNK_TARGET_BYTES = target
        self.addCleanup(setattr, batch_module, "CHUNK_TARGET_BYTES", original)

    def _client(self, uploads, created):
        from photo_organizer.batch import GeminiBatch

        client = GeminiBatch("key-not-used")
        # Encode to a fixed, known size so the arithmetic is exact.
        client.encode_image = staticmethod(lambda path, max_edge=1024: "A" * 200_000)

        def fake_upload(path, say):
            uploads.append(path.stat().st_size)
            return "files/fake-%d" % len(uploads)

        def fake_create(file_name, display_name, index):
            created.append(file_name)
            return "batches/job-%d" % index

        client._upload_file = fake_upload
        client._create_job = fake_create
        return client

    def _items(self, count):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        items = []
        for i in range(count):
            path = root / ("IMG_%04d.jpg" % i)
            path.write_bytes(b"x")
            items.append(("hash%04d" % i, path))
        return items

    def test_a_library_is_split_into_several_uploads(self):
        import photo_organizer.batch as batch_module

        self._small_chunks()
        uploads, created = [], []
        client = self._client(uploads, created)
        jobs = client.submit(self._items(30))
        self.assertGreater(len(jobs), 1,
                           "30 photos at 200 KB must not fit in one chunk here")
        self.assertEqual(len(uploads), len(jobs))
        for size in uploads:
            self.assertLessEqual(size, batch_module.CHUNK_TARGET_BYTES)

    def test_no_chunk_can_exceed_the_api_limit(self):
        """The specific failure: 3.68 GB against a 2 GiB cap."""
        from photo_organizer.batch import UPLOAD_LIMIT_BYTES

        self._small_chunks()
        uploads, created = [], []
        client = self._client(uploads, created)
        client.submit(self._items(40))
        self.assertGreater(len(uploads), 1, "this must actually split")
        for size in uploads:
            self.assertLess(size, UPLOAD_LIMIT_BYTES)

    def test_the_target_leaves_headroom_under_the_limit(self):
        from photo_organizer.batch import CHUNK_TARGET_BYTES, UPLOAD_LIMIT_BYTES

        self.assertLess(CHUNK_TARGET_BYTES, UPLOAD_LIMIT_BYTES,
                        "the target must sit below the hard limit")

    def test_every_photo_ends_up_in_exactly_one_chunk(self):
        """Nothing may be dropped or paid for twice by the split."""
        self._small_chunks()
        uploads, created = [], []
        client = self._client(uploads, created)
        items = self._items(25)
        jobs = client.submit(items)

        seen = []
        for _name, keys in jobs:
            seen.extend(keys)
        self.assertEqual(sorted(seen), sorted(k for k, _p in items))
        self.assertEqual(len(seen), len(set(seen)), "a photo was in two chunks")

    def test_each_chunk_gets_its_own_job_name(self):
        self._small_chunks()
        uploads, created = [], []
        client = self._client(uploads, created)
        jobs = client.submit(self._items(30))
        names = [name for name, _ in jobs]
        self.assertGreater(len(names), 1, "this must actually split")
        self.assertEqual(len(names), len(set(names)))

    def test_progress_is_reported_while_encoding(self):
        """85 minutes of silence was the second half of the problem."""
        from photo_organizer.batch import ENCODE_REPORT_EVERY

        said = []
        uploads, created = [], []
        client = self._client(uploads, created)
        client.submit(self._items(ENCODE_REPORT_EVERY * 2 + 5),
                      progress=said.append)
        prepared = [m for m in said if "prepared" in m]
        self.assertGreaterEqual(len(prepared), 2,
                                "encoding must report as it goes")

    def test_nothing_is_left_behind_on_disk(self):
        """The temp files are hundreds of megabytes each."""
        import tempfile as _tempfile

        before = set(Path(_tempfile.gettempdir()).glob("photo-organizer-batch-*"))
        uploads, created = [], []
        client = self._client(uploads, created)
        client.submit(self._items(20))
        after = set(Path(_tempfile.gettempdir()).glob("photo-organizer-batch-*"))
        self.assertEqual(before, after, "a temp request file was left behind")

    def test_temp_files_are_removed_even_when_the_upload_fails(self):
        import tempfile as _tempfile

        from photo_organizer.batch import BatchError, GeminiBatch

        before = set(Path(_tempfile.gettempdir()).glob("photo-organizer-batch-*"))
        client = GeminiBatch("key-not-used")
        client.encode_image = staticmethod(lambda path, max_edge=1024: "A" * 200_000)

        def boom(path, say):
            raise BatchError("upload refused")

        client._upload_file = boom
        with self.assertRaises(BatchError):
            client.submit(self._items(5))
        after = set(Path(_tempfile.gettempdir()).glob("photo-organizer-batch-*"))
        self.assertEqual(before, after)

    def test_an_oversized_file_fails_before_it_is_uploaded(self):
        """A comprehensible message, not an HTTP 413 after a long upload."""
        from photo_organizer.batch import BatchError, GeminiBatch

        client = GeminiBatch("key-not-used")
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        big = root / "big.jsonl"
        big.write_bytes(b"x")

        import photo_organizer.batch as batch_module

        original = batch_module.UPLOAD_LIMIT_BYTES
        batch_module.UPLOAD_LIMIT_BYTES = 0
        try:
            with self.assertRaises(BatchError) as caught:
                client._upload_file(big, lambda m: None)
            self.assertIn("upload limit", str(caught.exception))
        finally:
            batch_module.UPLOAD_LIMIT_BYTES = original

    def test_cancelling_stops_preparing(self):
        uploads, created = [], []
        client = self._client(uploads, created)
        seen = {"n": 0}

        def cancel_after_three():
            seen["n"] += 1
            return seen["n"] > 3

        try:
            jobs = client.submit(self._items(50), should_cancel=cancel_after_three)
        except Exception:
            return          # nothing encoded is an acceptable outcome
        total = sum(len(keys) for _n, keys in jobs)
        self.assertLess(total, 50, "cancelling must stop the encode loop")

class TestQuotaHandling(unittest.TestCase):
    """A 429 must not throw away batches that were already accepted.

    Measured on the real account: one request is accepted, 5,254 in a
    single job is refused with RESOURCE_EXHAUSTED, while the account is
    billed and holds almost no enqueued work. So the ceiling is on how much
    is enqueued at once, and the code has to discover it rather than assume.
    """

    def setUp(self):
        import photo_organizer.batch as batch_module

        self.batch = batch_module
        self._delays = batch_module.RETRY_DELAYS
        self._max = batch_module.MAX_REQUESTS_PER_BATCH
        batch_module.RETRY_DELAYS = (0, 0, 0)
        batch_module.MAX_REQUESTS_PER_BATCH = 5
        self.addCleanup(setattr, batch_module, "RETRY_DELAYS", self._delays)
        self.addCleanup(setattr, batch_module,
                        "MAX_REQUESTS_PER_BATCH", self._max)

    def _client(self):
        from photo_organizer.batch import GeminiBatch

        client = GeminiBatch("key-not-used")
        client.encode_image = staticmethod(lambda path, max_edge=1024: "A" * 1000)
        client._upload_file = lambda path, say: "files/fake"
        return client

    def _items(self, count):
        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        items = []
        for i in range(count):
            path = root / ("IMG_%03d.jpg" % i)
            path.write_bytes(b"x")
            items.append(("h%03d" % i, path))
        return items

    def test_batches_are_capped_by_request_count(self):
        """Bytes are not the only limit: a batch far under the size cap was
        still refused for holding too many requests."""
        client = self._client()
        client._create_job = lambda f, d, i: "batches/j%d" % i
        jobs = client.submit(self._items(23))
        self.assertEqual(len(jobs), 5)
        for _name, keys in jobs:
            self.assertLessEqual(len(keys),
                                 self.batch.MAX_REQUESTS_PER_BATCH)

    def test_a_429_keeps_the_batches_already_accepted(self):
        """They are already being billed. Discarding them is the worst
        possible response to a rate limit."""
        from photo_organizer.batch import BatchError

        client = self._client()
        calls = {"n": 0}

        def flaky(file_name, display_name, index):
            calls["n"] += 1
            if calls["n"] > 2:
                raise BatchError("HTTP 429 RESOURCE_EXHAUSTED")
            return "batches/j%d" % index

        client._create_job = flaky
        said = []
        jobs = client.submit(self._items(23), progress=said.append)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(sum(len(k) for _n, k in jobs), 10)
        self.assertTrue(any("Quota reached" in m for m in said), said)

    def test_a_429_on_the_very_first_batch_is_raised(self):
        """Nothing was accepted, so there is nothing to pretend about."""
        from photo_organizer.batch import BatchError, QuotaExhausted

        client = self._client()

        def always_refuse(file_name, display_name, index):
            raise BatchError("HTTP 429 RESOURCE_EXHAUSTED")

        client._create_job = always_refuse
        with self.assertRaises(QuotaExhausted):
            client.submit(self._items(10))

    def test_a_transient_429_is_retried_and_succeeds(self):
        from photo_organizer.batch import BatchError

        client = self._client()
        attempts = {"n": 0}

        def recovers(file_name, display_name, index):
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise BatchError("HTTP 429 RESOURCE_EXHAUSTED")
            return "batches/j%d" % index

        client._create_job = recovers
        jobs = client.submit(self._items(3))
        self.assertEqual(len(jobs), 1)
        self.assertGreaterEqual(attempts["n"], 3, "it must have retried")

    def test_an_error_that_is_not_a_quota_problem_is_not_retried(self):
        """Retrying a bad request just wastes time and hides the cause."""
        from photo_organizer.batch import BatchError

        client = self._client()
        attempts = {"n": 0}

        def bad_request(file_name, display_name, index):
            attempts["n"] += 1
            raise BatchError("HTTP 400 malformed request")

        client._create_job = bad_request
        with self.assertRaises(BatchError):
            client.submit(self._items(3))
        self.assertEqual(attempts["n"], 1, "a 400 must not be retried")

    def test_the_full_error_body_is_kept(self):
        """A 429 names the quota it hit inside its details block, and
        truncating at 400 characters threw exactly that away."""
        import inspect

        from photo_organizer.batch import GeminiBatch

        source = inspect.getsource(GeminiBatch._request)
        self.assertNotIn("[:400]", source)

class TestCostEstimate(unittest.TestCase):
    """The estimate was twelve times too low, quoted all day as fact."""

    def test_the_rate_comes_from_a_real_bill(self):
        """23 requests had been charged $0.10: about $0.0047 each. The
        original guess of $0.0004 would have quoted $2.75 for a library
        that costs nearer $32."""
        from photo_organizer.batch import MEASURED_COST_PER_PHOTO_USD

        self.assertGreater(MEASURED_COST_PER_PHOTO_USD, 0.001,
                           "the guessed rate was an order of magnitude low")

    def test_batch_is_half_of_interactive(self):
        from photo_organizer.batch import estimate_cost_usd

        self.assertAlmostEqual(estimate_cost_usd(100, batch=True),
                               estimate_cost_usd(100, batch=False) / 2)

    def test_the_rate_is_configurable(self):
        """It is only as good as the bill you divided to get it."""
        from photo_organizer.batch import estimate_cost_usd

        self.assertAlmostEqual(
            estimate_cost_usd(1000, batch=False, per_photo_usd=0.01), 10.0)
        self.assertEqual(Config().analysis.cost_per_photo_usd,
                         __import__("photo_organizer.batch", fromlist=["x"])
                         .MEASURED_COST_PER_PHOTO_USD)

    def test_nothing_pending_costs_nothing(self):
        from photo_organizer.batch import estimate_cost_usd

        self.assertEqual(estimate_cost_usd(0), 0.0)

    def test_one_photo_is_distinguishable_from_none(self):
        """Rounding to cents inside the calculation made them identical,
        and that is the distinction the confirmation dialog turns on."""
        from photo_organizer.batch import estimate_cost_usd

        self.assertGreater(estimate_cost_usd(1), estimate_cost_usd(0))

class TestReadingTextLocally(unittest.TestCase):
    """A guidebook page names a place outright, and costs nothing to read."""

    def test_a_generic_name_is_not_specific_enough(self):
        """An event was nearly named "Aiguille" -- a real gazetteer entry,
        French for "needle", and a useless folder name."""
        from photo_organizer.ocr import is_specific_enough

        self.assertFalse(is_specific_enough("Aiguille"))
        self.assertFalse(is_specific_enough("Dent"))
        self.assertTrue(is_specific_enough("Aiguille Dibona"))
        self.assertTrue(is_specific_enough("Salbitschijen"))

    def test_the_most_specific_name_wins(self):
        """Not the first one read. The first photo of a real event gave
        "Aiguille" and a later one gave "Aiguille Dibona"."""
        from photo_organizer.ocr import OcrResult, best_name

        results = [
            OcrResult(path=Path("a.jpg"), names=("Aiguille",)),
            OcrResult(path=Path("b.jpg"), names=("Aiguille Dibona", "Aiguille")),
        ]
        name, hit = best_name(results)
        self.assertEqual(name, "Aiguille Dibona")
        self.assertEqual(hit.path.name, "b.jpg")

    def test_nothing_specific_means_no_answer(self):
        from photo_organizer.ocr import OcrResult, best_name

        self.assertIsNone(best_name([OcrResult(path=Path("a.jpg"), names=("Dent",))]))
        self.assertIsNone(best_name([]))


class TestTripMerging(unittest.TestCase):
    """A hut approach and the climb next morning are one outing."""

    def _event(self, index, day, hour, place=None, rng=None, photos=3):
        made = []
        for i in range(photos):
            photo = make_photo("%02d_%02d_%d.jpg" % (day, hour, i))
            photo.timestamp = datetime(2019, 9, day, hour, i)
            made.append(photo)
        event = Event(index=index, photos=made)
        event.place_name = place
        event.mountain_range = rng
        return event

    def test_consecutive_days_join_when_one_knows_the_place(self):
        """The real case: day one named Aiguille Dibona, day two did not."""
        from photo_organizer.cluster import merge_trips

        events = [self._event(1, 10, 18, place="Aiguille Dibona"),
                  self._event(2, 11, 9)]
        merged, joined = merge_trips(events, gap_hours=18, max_days=3)
        self.assertEqual(joined, 1)
        self.assertEqual(len(merged), 1)
        self.assertEqual(len(merged[0].photos), 6)

    def test_days_naming_different_places_stay_apart(self):
        """Two trips, not one."""
        from photo_organizer.cluster import merge_trips

        events = [self._event(1, 10, 18, place="Aiguille Dibona"),
                  self._event(2, 11, 9, place="Salbitschijen")]
        merged, joined = merge_trips(events, gap_hours=18, max_days=3)
        self.assertEqual(joined, 0)
        self.assertEqual(len(merged), 2)

    def test_two_unnamed_days_are_not_evidence_of_anything(self):
        """Time alone merged 106 events on the real library, including a
        day of socialising with the next day's hike."""
        from photo_organizer.cluster import merge_trips

        events = [self._event(1, 10, 18), self._event(2, 11, 9)]
        merged, joined = merge_trips(events, gap_hours=18, max_days=3)
        self.assertEqual(joined, 0)

    def test_a_trip_cannot_grow_without_limit(self):
        """Otherwise a fortnight of climbing becomes one folder."""
        from photo_organizer.cluster import merge_trips

        events = [self._event(i, 9 + i, 12, rng="Ecrins") for i in range(1, 8)]
        merged, _joined = merge_trips(events, gap_hours=30, max_days=3)
        for event in merged:
            span = (event.end - event.start).total_seconds() / 86400
            self.assertLessEqual(span, 3.0)

    def test_a_long_gap_is_two_trips(self):
        from photo_organizer.cluster import merge_trips

        events = [self._event(1, 10, 12, rng="Ecrins"),
                  self._event(2, 14, 12, rng="Ecrins")]
        merged, joined = merge_trips(events, gap_hours=18, max_days=3)
        self.assertEqual(joined, 0)

class TestOcrRobustness(unittest.TestCase):
    """A page photographed sideways is still a page."""

    def test_every_rotation_is_tried(self):
        """The second topo of one event is stored sideways and its EXIF
        orientation tag is 0 -- unset -- so nothing can correct it
        automatically. Upright it reads "ainssij eungiq ayinbiy";
        rotated it reads "fissure Aiguille Dibona"."""
        from photo_organizer.ocr import ROTATIONS

        self.assertEqual(set(ROTATIONS), {0, 90, 180, 270})
        self.assertEqual(ROTATIONS[0], 0, "upright must be tried first")

    def test_both_segmentation_modes_are_tried(self):
        """Matching was unstable across settings: the page that DID work
        matched at 1000px/psm6 and not at 1800px/psm3."""
        from photo_organizer.ocr import PSM_MODES

        self.assertIn(6, PSM_MODES)
        self.assertIn(3, PSM_MODES)

    def test_the_resolution_is_high_enough_to_see_text_at_all(self):
        """At 1000px the sideways page gave 3 words -- too few to look
        like text; at 1400px it gave 16."""
        from photo_organizer.ocr import OCR_EDGE

        self.assertGreaterEqual(OCR_EDGE, 1400)

    def test_word_count_counts_words_not_noise(self):
        from photo_organizer.ocr import word_count

        self.assertEqual(word_count("fissure Aiguille Dibona Boell"), 4)
        self.assertEqual(word_count("a b c de fg"), 0)

    def test_the_escalation_threshold_sits_above_rock(self):
        """Measured at 1400px: rock photos gave 0, 2, 2, 3, 7, 9, 10 real
        words; the two topo pages gave 16 and 49."""
        from photo_organizer.ocr import TEXT_LIKELY_WORDS

        self.assertGreater(TEXT_LIKELY_WORDS, 10)
        self.assertLess(TEXT_LIKELY_WORDS, 16)

if __name__ == "__main__":
    unittest.main(verbosity=2)
