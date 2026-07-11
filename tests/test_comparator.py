import os
import tempfile
import unittest
from unittest import mock

import fitz

from dashboard.utils import comparator
from dashboard.utils.comparator import CollusionDetector, compare_documents


def create_pdf(path, pages):
    document = fitz.open()
    for content in pages:
        page = document.new_page()
        page.insert_text((72, 72), content, fontsize=10)
    document.save(path)
    document.close()


class ComparatorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_dir = os.path.join(self.temp_dir.name, "cache")
        self.cache_patch = mock.patch.object(comparator, "CACHE_DIR", self.cache_dir)
        self.cache_patch.start()

    def tearDown(self):
        self.cache_patch.stop()
        self.temp_dir.cleanup()

    def path(self, name):
        return os.path.join(self.temp_dir.name, name)

    def test_entity_extraction_keeps_full_valid_identity(self):
        detector = CollusionDetector()
        entities = detector.extract_entities(
            "identity 11010519491231002X mobile 13800138000 email TEST@example.com"
        )
        self.assertIn("11010519491231002X", entities)
        self.assertIn("13800138000", entities)
        self.assertIn("test@example.com", entities)
        self.assertNotIn("110105194912310", entities)

    def test_adjacent_entities_separated_only_by_space(self):
        detector = CollusionDetector()
        entities = detector.extract_entities("11010519491231002X 13800138000")

        self.assertIn("11010519491231002X", entities)
        self.assertIn("13800138000", entities)

    def test_fuzzy_match_detects_small_rewrite(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        create_pdf(
            path_a,
            ["The project implementation schedule includes three quality review stages."],
        )
        create_pdf(
            path_b,
            ["The project implementation schedule contains three quality review stages."],
        )

        result = compare_documents(path_a, path_b, check_entity=False)

        fuzzy = [item for item in result["paragraphs"] if item["type"] == "fuzzy"]
        self.assertTrue(fuzzy)
        self.assertGreaterEqual(fuzzy[0]["similarity"], 78)

    def test_shared_tender_edit_has_separate_classification(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        tender_path = self.path("tender.pdf")
        create_pdf(path_a, ["The supplier shall provide a six year warranty for all devices."])
        create_pdf(path_b, ["The supplier shall provide a six year warranty for all devices."])
        create_pdf(tender_path, ["The supplier shall provide a five year warranty for all devices."])

        result = compare_documents(
            path_a, path_b, tender_path, check_entity=False, check_spelling=False
        )

        tender_related = [
            item for item in result["paragraphs"] if item["type"] == "tender_related"
        ]
        self.assertTrue(tender_related)
        self.assertTrue(tender_related[0]["tender_text"])

    def test_page_level_scan_statistics(self):
        path_a = self.path("a.pdf")
        path_b = self.path("b.pdf")
        create_pdf(path_a, ["", "", "readable ascii page"])
        create_pdf(path_b, ["", "readable ascii page"])

        result = compare_documents(path_a, path_b, check_entity=False, check_text=False)
        stats = result["metadata"]["text_stats"]

        self.assertEqual(stats["file_a"]["total_pages"], 3)
        self.assertEqual(stats["file_a"]["suspected_scan_pages"], 3)
        self.assertTrue(result["metadata"]["warnings"])

    def test_extraction_cache_is_reused(self):
        pdf_path = self.path("cached.pdf")
        create_pdf(pdf_path, ["cacheable document content"])
        detector = CollusionDetector()
        first = detector.extract_text_with_pages(pdf_path)

        with mock.patch("fitz.open", side_effect=AssertionError("PDF reopened")):
            second = detector.extract_text_with_pages(pdf_path)

        self.assertEqual(first, second)

    def test_fuzzy_matching_does_not_reuse_one_target_unit(self):
        detector = CollusionDetector()
        units_a = [
            {"text": "projectimplementationqualitycontrolschedulealpha", "page": 1},
            {"text": "projectimplementationqualitycontrolschedulebeta", "page": 2},
        ]
        units_b = [
            {"text": "projectimplementationsafetycontrolschedulealpha", "page": 3}
        ]

        matches = detector._find_fuzzy_collisions(units_a, units_b, set())

        self.assertLessEqual(len(matches), 1)


if __name__ == "__main__":
    unittest.main()
