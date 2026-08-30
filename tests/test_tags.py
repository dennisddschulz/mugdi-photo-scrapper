"""Local tagging, and the duplicate keeper that no longer needs the API.

These cover the two gaps a full run exposed: 2,522 of 13,193 copies got
keywords, and the photographic ranking changed 0 of 524 duplicate groups.
"""

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path


def sims_for(**wanted):
    """Similarities that make the named prompts win their facet."""
    from photo_organizer.tags import BINARY, FACETS

    out = []
    for facet, (_threshold, entries) in FACETS.items():
        for label, _text in entries:
            out.append(1.0 if wanted.get(facet) == label else 0.0)
    for name in BINARY:
        out.append(1.0 if wanted.get(name) else 0.0)
        out.append(0.0 if wanted.get(name) else 1.0)
    return out


class TestLocalTagging(unittest.TestCase):
    """Tags for every photo, not the 19% the paid analysis reaches."""

    def test_no_people_means_no_activity(self):
        """An empty snowy landscape was tagged ski touring 4 times in 24."""
        from photo_organizer.tags import tags_from_similarity

        tags = tags_from_similarity(
            sims_for(people=None, activity="ski touring", terrain="valley"))
        self.assertNotIn("ski touring", tags)
        self.assertIn("valley", tags)

    def test_a_document_suppresses_the_outdoor_facets(self):
        """A photograph of a printed page scored rain at 0.99."""
        from photo_organizer.tags import tags_from_similarity

        tags = tags_from_similarity(sims_for(
            subject="document", terrain="rock face", people="people",
            activity="climbing", snow=True))
        self.assertEqual(tags, ["document"])

    def test_weather_is_not_asked_indoors(self):
        """A night photo inside a climbing gym came back clear sky."""
        from photo_organizer.tags import tags_from_similarity

        tags = tags_from_similarity(
            sims_for(terrain="indoors", **{"clear sky": True}))
        self.assertIn("indoors", tags)
        self.assertNotIn("clear sky", tags)

    def test_conditions_are_not_exclusive(self):
        """Snow AND a clear sky are both true on a good winter summit.

        One softmax over the conditions made the model pick a winner
        between two facts that were both true.
        """
        from photo_organizer.tags import tags_from_similarity

        tags = tags_from_similarity(
            sims_for(terrain="summit", snow=True, **{"clear sky": True}))
        self.assertIn("snow", tags)
        self.assertIn("clear sky", tags)

    def test_several_phrasings_of_one_label_are_summed(self):
        """Asking "are there people" once missed 9 of 10 photos where the
        person was a small figure in a big landscape."""
        from photo_organizer.tags import FACETS

        labels = [label for label, _text in FACETS["people"][1]]
        self.assertGreater(labels.count("people"), 1)

    def test_the_season_comes_from_the_clock_not_the_model(self):
        from photo_organizer.tags import season_of, time_of_day

        self.assertEqual(season_of(datetime(2020, 1, 5)), "winter")
        self.assertEqual(season_of(datetime(2020, 7, 5)), "summer")
        self.assertIsNone(season_of(None))
        self.assertEqual(time_of_day(datetime(2020, 7, 5, 7)), "early morning")
        self.assertIsNone(time_of_day(datetime(2020, 7, 5, 13)),
                          "ordinary daylight is not worth a tag")

    def test_a_saturated_softmax_makes_thresholds_meaningless(self):
        """Why SOFTMAX_SCALE exists. At CLIP's own scale of 100 every
        probability is 0.000 or 1.000, and sweeping the people threshold
        from 0.45 to 0.80 changed the score by exactly zero."""
        from photo_organizer.tags import SOFTMAX_SCALE

        self.assertLess(SOFTMAX_SCALE, 100.0)

    def test_no_prompt_asks_which_mountain_this_is(self):
        """CLIP answered K2 at 82% for a forest slope. Naming a specific
        peak from pixels is not attempted anywhere in this module."""
        from photo_organizer.tags import _prompts

        prompts = " ".join(_prompts()).lower()
        for banned in ("k2", "everest", "matterhorn", "mont blanc", "dibona"):
            self.assertNotIn(banned, prompts)


class TestTagsReachEveryPhoto(unittest.TestCase):
    def test_keywords_are_written_without_a_paid_analysis(self):
        """The gap this closes: 2,522 of 13,193 copies got keywords."""
        from photo_organizer.metadata import build_tags

        tags = build_tags(None, event_tags=["Ecrins"],
                          local_tags=["climbing", "snow"])
        self.assertEqual(tags, ["climbing", "snow", "Ecrins"])

    def test_local_tags_join_the_paid_ones(self):
        from photo_organizer.metadata import build_tags
        from photo_organizer.schema import PhotoAnalysis

        analysis = PhotoAnalysis(activity="alpine climbing")
        tags = build_tags(analysis, local_tags=["snow", "alpine climbing"])
        self.assertIn("alpine climbing", tags)
        self.assertIn("snow", tags)
        self.assertEqual(len(tags), len(set(tags)), "no duplicated keywords")


class TestEmbeddingsAreStored(unittest.TestCase):
    """The encode is the whole cost. Storing only the page score meant any
    change of wording cost another full pass over 13,825 photos."""

    def setUp(self):
        from photo_organizer.db import AnalysisStore

        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.store = AnalysisStore(self.root / "a.sqlite3")

    def test_a_vector_survives_a_round_trip(self):
        import numpy as np

        vector = np.arange(512, dtype="float32") / 512.0
        self.store.put_embedding("key", "ViT-B-32", vector)
        back = self.store.get_embedding("key", "ViT-B-32")
        self.assertIsNotNone(back)
        np.testing.assert_allclose(back, vector, rtol=1e-6)

    def test_another_model_is_not_reused(self):
        """Vectors from different models are not comparable, and mixing
        them silently would produce confident nonsense."""
        import numpy as np

        self.store.put_embedding("key", "ViT-B-32", np.zeros(512, "float32"))
        self.assertIsNone(self.store.get_embedding("key", "ViT-L-14"))

    def test_a_missing_key_is_none(self):
        self.assertIsNone(self.store.get_embedding("never", "ViT-B-32"))


class TestSharpnessChoosesTheKeeper(unittest.TestCase):
    """Without this the keeper is chosen by file size: the model-based
    ranking needs two analysed photos in a group, and on a real run that
    was true of so few groups it changed 0 of 524."""

    def test_a_blurred_frame_scores_below_a_sharp_one(self):
        from PIL import Image, ImageDraw, ImageFilter

        from photo_organizer.dedupe import measure_sharpness

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, True)
        image = Image.new("RGB", (400, 400), "white")
        draw = ImageDraw.Draw(image)
        for x in range(0, 400, 20):
            draw.line([(x, 0), (x, 400)], fill="black", width=3)
        sharp = root / "sharp.jpg"
        image.save(sharp, quality=95)
        blurred = root / "blurred.jpg"
        image.filter(ImageFilter.GaussianBlur(6)).save(blurred, quality=95)

        self.assertGreater(measure_sharpness(sharp), measure_sharpness(blurred))

    def test_an_unreadable_file_measures_as_none(self):
        """One bad file must not abort a 14,000-photo run."""
        from photo_organizer.dedupe import measure_sharpness

        self.assertIsNone(measure_sharpness(Path("/no/such/photo.jpg")))


if __name__ == "__main__":
    unittest.main()


class TestPagesAreNotPaidFor(unittest.TestCase):
    """A picture of paper cannot say where an event was.

    An IKEA mattress label scored 0.96 on the page detector, was sent to the
    API on two consecutive runs and failed both times -- having consumed one
    of that event's four paid slots each time.
    """

    def _event(self, count, pages=()):
        from photo_organizer.models import Event, Photo

        photos = []
        for n in range(count):
            photo = Photo(source_path=Path(f"/src/{n}.jpg"))
            photo.timestamp = datetime(2020, 6, 1, 9 + n)
            photo.is_page = n in pages
            photos.append(photo)
        return Event(index=1, photos=photos)

    def test_pages_are_left_out_of_the_sample(self):
        from photo_organizer.analyze import select_photos

        event = self._event(6, pages=(0, 1))
        chosen = select_photos(event, per_event=4)
        self.assertTrue(all(not p.is_page for p in chosen),
                        "a photograph of a page was sent to the paid analysis")

    def test_an_event_of_only_pages_still_gets_sampled(self):
        """Better named from a topo than from silence."""
        from photo_organizer.analyze import select_photos

        event = self._event(3, pages=(0, 1, 2))
        self.assertEqual(len(select_photos(event, per_event=2)), 2)

    def test_the_skip_threshold_is_stricter_than_the_reading_one(self):
        """Reading a borderline photo costs a second; wrongly excluding one
        from the paid sample could cost an event its name."""
        from photo_organizer.analyze import PAGE_SCORE_SKIP
        from photo_organizer.pages import PAGE_THRESHOLD

        self.assertGreater(PAGE_SCORE_SKIP, PAGE_THRESHOLD)


class TestPaperworkIsSetAsideNotDeleted(unittest.TestCase):
    """A train ticket is a picture of paper and is rubbish. A guidebook topo
    is a picture of paper and is what named the Aiguille Dibona."""

    def test_the_threshold_favours_keeping(self):
        """A guidebook page sent to the review folder is a worse mistake
        than a receipt left in the library."""
        from photo_organizer.tags import PAPERWORK_THRESHOLD

        self.assertGreaterEqual(PAPERWORK_THRESHOLD, 0.85)

    def test_guidebook_prompts_are_on_the_keep_side(self):
        from photo_organizer.tags import PAPERWORK_KEEP, PAPERWORK_TRASH

        keep = " ".join(PAPERWORK_KEEP).lower()
        self.assertIn("guidebook", keep)
        self.assertIn("topo", keep)
        trash = " ".join(PAPERWORK_TRASH).lower()
        for expected in ("receipt", "label", "ticket"):
            self.assertIn(expected, trash)

    def test_paperwork_is_a_reject_reason_not_a_deletion(self):
        """CLAUDE.md rule 4: suspected junk is copied aside, never deleted.
        Nothing in the codebase may delete a source file."""
        import inspect

        from photo_organizer import copier

        body = inspect.getsource(copier._copy_rejected)
        self.assertIn("shutil.copy2(photo.source_path", body)
        # It DOES unlink -- a copy that failed verification, inside the
        # output. What must never happen is anything destructive aimed at
        # the SOURCE.
        for verb in ("unlink", "os.remove", "shutil.move", "os.rename",
                     "rmtree", "write_bytes", "write_text"):
            self.assertNotIn(f"photo.source_path.{verb}", body)
            self.assertNotIn(f"{verb}(photo.source_path", body)


class TestTheBlacklist(unittest.TestCase):
    """Text beats a classifier: IKEA on a page is certain."""

    def test_it_matches_a_blacklisted_word(self):
        from photo_organizer.tags import blacklisted_word

        text = "Matelas en mousse MORGEDAL 90x200 Design IKEA of Sweden 179.-"
        self.assertEqual(blacklisted_word(text, ("ikea", "coop")), "ikea")

    def test_it_matches_whole_words_only(self):
        """'vat' must not fire on 'private', and a peak called Coopstock
        must not be thrown away because of 'coop'."""
        from photo_organizer.tags import blacklisted_word

        self.assertIsNone(blacklisted_word("a private crag", ("vat",)))
        self.assertIsNone(blacklisted_word("Coopstock south face", ("coop",)))

    def test_a_guidebook_page_is_not_blacklisted(self):
        from photo_organizer.config import AnalysisConfig
        from photo_organizer.tags import blacklisted_word

        page = ("Pointes Lachenal South Face Mixed Pelissier Gully "
                "Aiguille du Midi arete des Cosmiques Vallee Blanche")
        self.assertIsNone(
            blacklisted_word(page, AnalysisConfig().document_blacklist))

    def test_empty_input_is_safe(self):
        from photo_organizer.tags import blacklisted_word

        self.assertIsNone(blacklisted_word("", ("ikea",)))
        self.assertIsNone(blacklisted_word("anything", ()))


class TestPeaksMustBePlausiblyPlaced(unittest.TestCase):
    """A country code is not a location: Kerguelen is coded FR, the same as
    Chamonix, so 'La Cheminee' named an Alpine event after a hill at
    -49.2150, 70.0033 in the sub-Antarctic Indian Ocean."""

    def _peak(self, name, lat, lon):
        from photo_organizer.peaks import Peak

        return Peak(name=name, lat=lat, lon=lon, country="FR")

    def test_an_overseas_territory_is_rejected(self):
        from photo_organizer.peaks import PeakIndex

        index = PeakIndex([self._peak("La Cheminee", -49.2150, 70.0033)])
        self.assertEqual(len(index), 0)

    def test_the_alps_are_kept(self):
        from photo_organizer.peaks import PeakIndex

        index = PeakIndex([self._peak("Aiguille Dibona", 44.9632, 6.2429)])
        self.assertEqual(len(index), 1)

    def test_svalbard_is_kept(self):
        """Genuinely Norway, and a plausible place to have been -- which is
        why the northern limit is 81 and not 72."""
        from photo_organizer.peaks import PeakIndex

        index = PeakIndex([self._peak("Newtontoppen", 79.0167, 17.3500)])
        self.assertEqual(len(index), 1)

    def test_the_whole_range_of_this_library_is_kept(self):
        """Sardinia in the south, Norway in the north."""
        from photo_organizer.peaks import PeakIndex

        peaks = [self._peak("Monte Oddeu", 40.2000, 9.5000),
                 self._peak("Store Skagastolstind", 61.4700, 7.8300)]
        self.assertEqual(len(PeakIndex(peaks)), 2)

    def test_bounds_can_be_switched_off(self):
        from photo_organizer.peaks import PeakIndex

        index = PeakIndex([self._peak("La Cheminee", -49.2, 70.0)], bounds=None)
        self.assertEqual(len(index), 1)


class TestRouteNamesAreNotPlaces(unittest.TestCase):
    """The actual Kerguelen bug. OCR read a Swiss crag topo correctly:

        sektor C - petit pilier   Laenge 40 m   Einstieg 1020 m
        la cheminee 5b
        Pilier Kocher 6b+ (6b obl.)      Le pelerin 6a+

    "la cheminee 5b" is a route -- the chimney, graded 5b. Taken for a place
    name, it matched the one gazetteer entry spelled that way and named an
    Alpine crag after a hill at -49.2150, 70.0033.
    """

    CRAG = ("sektor C - petit pilier Lange 40 m Einstieg 1020 m\n"
            "la cheminee 5b\nPilier Kocher 6b+ (6b obl.)\nLe pelerin 6a+\n"
            "Hitch-hiking 5c+\n")
    SUMMIT = ("18 | Salbit | Goschenertal\nsalbitschijen 2981 m\n"
              "Zwillingsturm\nGemsplanggenstock 2752 m\n")

    def test_a_graded_name_is_a_route(self):
        from photo_organizer.ocr import followed_by_grade

        self.assertTrue(followed_by_grade("la cheminee", self.CRAG))
        self.assertTrue(followed_by_grade("Pilier Kocher", self.CRAG))
        self.assertTrue(followed_by_grade("Le pelerin", self.CRAG))

    def test_a_summit_with_its_altitude_is_kept(self):
        """The pattern must not match a four-figure height."""
        from photo_organizer.ocr import followed_by_grade

        self.assertFalse(followed_by_grade("salbitschijen", self.SUMMIT))
        self.assertFalse(followed_by_grade("Gemsplanggenstock", self.SUMMIT))
        self.assertFalse(followed_by_grade("Zwillingsturm", self.SUMMIT))

    def test_other_grade_systems(self):
        from photo_organizer.ocr import followed_by_grade

        self.assertTrue(followed_by_grade("Nose", "The Nose 5.10a classic"))
        self.assertTrue(followed_by_grade("Directe", "Directe IV+ 200m"))
        self.assertTrue(followed_by_grade("Gully", "Gully WI4 steep"))

    def test_empty_input_is_safe(self):
        from photo_organizer.ocr import followed_by_grade

        self.assertFalse(followed_by_grade("", "5b"))
        self.assertFalse(followed_by_grade("name", ""))


class TestAltitudesAreNotGrades(unittest.TestCase):
    """A regression I introduced and then measured.

    German guidebooks print altitudes as "(3.275 m)". The first version of
    the grade pattern accepted a bare digit, read that 3 as a French grade,
    and threw away "Dammazwillinge (3.275 m)" -- a real 3275 m summit --
    leaving the event named after a different peak on the same page.
    """

    PAGE = ("400 Dammazwillinge (3.275 m)\n"
            "SW-Pfeiler VI+ (VI- obl.)\n1962 P. Arigoni, H.-P. Geier\n"
            "Lange 220m Zustieg 2Std. Albert-Heim-Hutte\n")

    def test_a_german_altitude_is_not_a_grade(self):
        from photo_organizer.ocr import followed_by_grade

        self.assertFalse(followed_by_grade("Dammazwillinge", self.PAGE))

    def test_a_plain_altitude_is_not_a_grade(self):
        from photo_organizer.ocr import followed_by_grade

        self.assertFalse(followed_by_grade("Piz Buin", "Piz Buin 3312 m"))
        self.assertFalse(followed_by_grade("Mont Blanc", "Mont Blanc 4808 m"))

    def test_a_grade_still_needs_a_letter_or_a_sign(self):
        """The cost of the fix, accepted deliberately: a bare digit grade is
        no longer caught. Ambiguous digits are what ate the summit."""
        from photo_organizer.ocr import followed_by_grade

        self.assertTrue(followed_by_grade("Route", "Route 6a+"))
        self.assertTrue(followed_by_grade("Route", "Route 7-"))
        self.assertTrue(followed_by_grade("Pfeiler", "SW-Pfeiler VI+"))
        self.assertFalse(followed_by_grade("Route", "Route 6"))


class TestTheCantonReachesTheFolderName(unittest.TestCase):
    """Geocoding runs before naming, when only 1 of 379 events has GPS. The
    position an event is named from only exists after naming, so the canton
    has to be filled -- and the name rebuilt -- afterwards."""

    def _event(self, lat=None, lon=None, place=None):
        from photo_organizer.models import Event, Photo

        photo = Photo(source_path=Path("/src/a.jpg"))
        photo.timestamp = datetime(2019, 9, 20, 9)
        event = Event(index=1, photos=[photo])
        event.enriched_lat, event.enriched_lon = lat, lon
        event.place_name = place
        return event

    def test_a_position_yields_a_canton_and_country(self):
        from photo_organizer.geo import fill_admin_regions

        event = self._event(46.6226, 8.4315, "Dammazwillinge")   # Furka
        self.assertEqual(fill_admin_regions([event]), 1)
        self.assertEqual(event.region, "Uri")
        self.assertEqual(event.country_code, "CH")

    def test_an_event_with_no_position_is_left_alone(self):
        from photo_organizer.geo import fill_admin_regions

        event = self._event()
        self.assertEqual(fill_admin_regions([event]), 0)
        self.assertIsNone(event.region)

    def test_a_massif_is_not_overwritten_by_a_canton(self):
        """Ecrins says more than Rhone-Alpes."""
        from photo_organizer.geo import fill_admin_regions

        event = self._event(44.9632, 6.2429, "Aiguille Dibona")
        event.region = "Ecrins"
        fill_admin_regions([event])
        self.assertEqual(event.region, "Ecrins")

    def test_the_name_carries_peak_canton_and_country(self):
        from photo_organizer.analyze import build_folder_name
        from photo_organizer.config import Config

        event = self._event(46.6226, 8.4315, "Dammazwillinge")
        event.region, event.country_code = "Uri", "CH"
        event.activity = "alpine climbing"
        build_folder_name(event, Config().naming)
        for expected in ("Dammazwillinge", "Uri", "CH"):
            self.assertIn(expected, event.proposed_name)


class TestTheCantonBeatsTheClaimedMassif(unittest.TestCase):
    """Measured on the real library: the Aiguille Dibona event was filed as
    FR_Mont-Blanc-Massif_Aiguille-Dibona -- 200 km from the Ecrins -- and
    Dammazwillinge as plain "Alps". The canton comes from the event's own
    position and cannot be wrong that way."""

    def _event(self):
        from photo_organizer.models import Event, Photo

        photo = Photo(source_path=Path("/src/a.jpg"))
        photo.timestamp = datetime(2019, 9, 11, 9)
        event = Event(index=1, photos=[photo])
        event.place_name = "Aiguille Dibona"
        event.country_code = "FR"
        return event

    def test_the_canton_wins_when_both_are_known(self):
        from photo_organizer.analyze import build_folder_name
        from photo_organizer.config import Config

        event = self._event()
        event.mountain_range = "Mont Blanc Massif"
        event.region = "Rhone-Alpes"
        build_folder_name(event, Config().naming)
        self.assertIn("Rhone-Alpes", event.proposed_name)
        self.assertNotIn("Mont-Blanc", event.proposed_name)

    def test_a_claimed_massif_is_used_when_there_is_no_canton(self):
        """Better than nothing, when the event has no position at all."""
        from photo_organizer.analyze import build_folder_name
        from photo_organizer.config import Config

        event = self._event()
        event.mountain_range = "Ecrins Massif"
        build_folder_name(event, Config().naming)
        self.assertIn("Ecrins", event.proposed_name)

    def test_useless_regions_are_dropped(self):
        """"Alps" appeared on 25 folders and says nothing the country does
        not already say."""
        from photo_organizer.analyze import build_folder_name
        from photo_organizer.config import Config

        event = self._event()
        event.mountain_range = "Alps"
        build_folder_name(event, Config().naming)
        self.assertNotIn("Alps", event.proposed_name)
        self.assertIn("Aiguille-Dibona", event.proposed_name)
