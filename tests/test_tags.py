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
