import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from run_event_image_enrichment import has_event_data  # noqa: E402


class MasterRowDetectionTests(unittest.TestCase):
    def test_unchecked_checkbox_does_not_make_blank_row_an_event(self):
        self.assertFalse(has_event_data({"Editorial Feature": "FALSE"}))

    def test_checked_checkbox_alone_does_not_make_blank_row_an_event(self):
        self.assertFalse(has_event_data({"Editorial Feature": "TRUE"}))

    def test_real_event_is_retained_regardless_of_checkbox_state(self):
        self.assertTrue(has_event_data({
            "Title": "Wallis Bird",
            "Date": "2026-09-10",
            "Venue": "The Grand Social",
            "URL": "https://example.com/wallis-bird",
            "Editorial Feature": "TRUE",
        }))

    def test_missing_link_event_is_still_retained(self):
        self.assertTrue(has_event_data({
            "Title": "Community event",
            "Date": "2026-09-11",
            "Venue": "Local venue",
            "URL": "",
            "Editorial Feature": "FALSE",
        }))


if __name__ == "__main__":
    unittest.main()
