"""The copy path with tagging switched ON.

The rest of the suite copies with write_metadata=False, so the branch that
writes keywords was never executed by a test -- which is exactly where the
19%-coverage fix lives. These run a real copy against real JPEG files and
read the keywords back out.
"""

import shutil
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from photo_organizer.config import Config
from photo_organizer.models import Event, Photo, Plan


def make_jpeg(path: Path, colour="white") -> None:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (320, 240), colour)
    draw = ImageDraw.Draw(image)
    for x in range(0, 320, 16):
        draw.line([(x, 0), (x, 240)], fill="black", width=2)
    image.save(path, quality=90)


class TestCopyWritesLocalTags(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.source = self.root / "src"
        self.output = self.root / "out"
        self.source.mkdir()
        self.photo_path = self.source / "IMG_0001.jpg"
        make_jpeg(self.photo_path)

        photo = Photo(source_path=self.photo_path)
        photo.size_bytes = self.photo_path.stat().st_size
        photo.timestamp = datetime(2020, 1, 15, 11, 0)
        event = Event(index=1, photos=[photo])
        event.place_name = "Somewhere"
        self.plan = Plan(
            source_root=self.source,
            output_root=self.output,
            events=[event],
        )
        self.config = Config()
        self.config.analysis.database_path = str(self.root / "a.sqlite3")

    def _copy(self, local_tags=True):
        from photo_organizer.copier import copy_plan

        self.config.analysis.local_tags = local_tags
        return copy_plan(self.plan, self.config, write_metadata=True,
                         deep_verify=True)

    def _keywords(self, target: Path):
        import pyexiv2

        with pyexiv2.Image(str(target)) as img:
            return img.read_xmp().get("Xmp.dc.subject", [])

    def test_a_photo_with_no_analysis_still_gets_keywords(self):
        """The whole point: 2,522 of 13,193 copies had keywords before."""
        stats = self._copy()
        self.assertEqual(stats.copied, 1)
        copies = list(self.output.rglob("IMG_0001.jpg"))
        self.assertEqual(len(copies), 1, "the copy should exist")
        keywords = self._keywords(copies[0])
        self.assertTrue(keywords, "a photo with no paid analysis got no tags")
        # The event name alone would have been written before this change,
        # so the test is only meaningful if something the MODEL saw is in
        # there too. (The fixture is line art, which reads as a screenshot;
        # that is a fine answer for it.)
        self.assertTrue(
            [k for k in keywords if k != "Somewhere"],
            f"only the event tag was written: {keywords}",
        )

    def test_the_season_is_added_to_an_ordinary_photograph(self):
        """The clock knows it exactly, so it is never asked of the model.

        Documents are the exception -- a photographed page has no season --
        which is why this is checked on the tagging directly rather than on
        the line-art fixture above.
        """
        from photo_organizer.tags import describe

        from tests.test_tags import sims_for

        tags = describe(sims_for(terrain="summit", snow=True),
                        datetime(2020, 1, 15, 11, 0))
        self.assertIn("winter", tags)
        self.assertIn("summit", tags)

    def test_the_source_is_not_touched_by_tagging(self):
        before = self.photo_path.read_bytes()
        self._copy()
        self.assertEqual(self.photo_path.read_bytes(), before,
                         "tagging must only ever write to the copy")

    def test_it_still_works_with_local_tags_switched_off(self):
        stats = self._copy(local_tags=False)
        self.assertEqual(stats.copied, 1)
        self.assertEqual(stats.verify_failures, 0)


if __name__ == "__main__":
    unittest.main()
