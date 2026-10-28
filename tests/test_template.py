"""Tests for template.py. Run from the project root:

    python -m unittest discover -s tests -v

The PDF and JSON are regenerated into a temporary folder, so these tests never
depend on stale files and never overwrite the real template.
"""

import json
import os
import string
import sys
import tempfile
import unittest
from collections import Counter

import pymupdf

# Make `import config` / `import template` work when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import template  # noqa: E402

TOL = 0.01  # points; PDF floating-point numbers are not exact

UPPER = string.ascii_uppercase
LOWER = string.ascii_lowercase
DIGITS = string.digits
EXPECTED_PAGE_CHARS = {1: UPPER, 2: LOWER, 3: DIGITS}

_temp_dir = None
layout = None
pdf_path = None
document = None


def setUpModule():
    global _temp_dir, layout, pdf_path, document
    _temp_dir = tempfile.TemporaryDirectory()
    pdf_path = os.path.join(_temp_dir.name, "template.pdf")
    json_path = os.path.join(_temp_dir.name, "template_layout.json")
    template.write_pdf(template.build_layout(1), pdf_path)
    template.write_json(template.build_layout(1), json_path)
    # Read the JSON back from disk: that is what extract.py will consume.
    with open(json_path, encoding="utf-8") as f:
        layout = json.load(f)
    document = pymupdf.open(pdf_path)


def tearDownModule():
    document.close()
    _temp_dir.cleanup()


def overlap(a, b):
    """True if two rects share area (touching edges do not count)."""
    return (min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]) > TOL
            and min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]) > TOL)


def to_pymupdf(rect, page_height):
    """JSON (bottom-left origin) -> PyMuPDF (top-left origin) rect."""
    return pymupdf.Rect(rect["x0"], page_height - rect["y1"], rect["x1"], page_height - rect["y0"])


class TestCharacterCoverage(unittest.TestCase):
    def test_exactly_186_boxes(self):
        self.assertEqual(len(layout["boxes"]), 186)
        self.assertEqual(layout["meta"]["box_count"], 186)

    def test_each_character_has_exactly_three_samples(self):
        counts = Counter(b["char"] for b in layout["boxes"])
        for char in UPPER + LOWER + DIGITS:
            self.assertEqual(counts[char], 3, f"'{char}' should have 3 boxes")

    def test_uppercase_lowercase_digits_each_checked(self):
        for name, chars in (("uppercase", UPPER), ("lowercase", LOWER), ("digits", DIGITS)):
            for char in chars:
                samples = [b["sample"] for b in layout["boxes"] if b["char"] == char]
                self.assertEqual(sorted(samples), [1, 2, 3], f"{name} '{char}'")

    def test_no_missing_or_extra_characters(self):
        found = {b["char"] for b in layout["boxes"]}
        self.assertEqual(found, set(UPPER + LOWER + DIGITS))

    def test_no_duplicate_sample_index(self):
        keys = [(b["char"], b["sample"]) for b in layout["boxes"]]
        self.assertEqual(len(keys), len(set(keys)))

    def test_characters_are_on_the_intended_pages(self):
        for box in layout["boxes"]:
            self.assertIn(box["char"], EXPECTED_PAGE_CHARS[box["page"]])


class TestGeometry(unittest.TestCase):
    def test_rects_are_well_formed(self):
        for box in layout["boxes"]:
            self.assertLess(box["x0"], box["x1"])
            self.assertLess(box["y0"], box["y1"])

    def test_boxes_inside_page_and_margins(self):
        width, height = config.PAGE_WIDTH, config.PAGE_HEIGHT
        rects = layout["boxes"] + [layout["sample_sentence"]]
        for rect in rects:
            self.assertGreaterEqual(rect["x0"], config.MARGIN_LEFT - TOL)
            self.assertLessEqual(rect["x1"], width - config.MARGIN_RIGHT + TOL)
            self.assertGreaterEqual(rect["y0"], config.MARGIN_BOTTOM - TOL)
            self.assertLessEqual(rect["y1"], height - config.MARGIN_TOP - config.HEADER_HEIGHT + TOL)

    def test_no_overlaps_on_any_page(self):
        for number in EXPECTED_PAGE_CHARS:
            rects = [b for b in layout["boxes"] if b["page"] == number]
            if layout["sample_sentence"]["page"] == number:
                rects.append(layout["sample_sentence"])
            for i, a in enumerate(rects):
                for b in rects[i + 1:]:
                    self.assertFalse(overlap(a, b), f"page {number}: {a} overlaps {b}")

    def test_baseline_is_inside_its_box(self):
        for box in layout["boxes"]:
            self.assertGreater(box["baseline_y"], box["y0"])
            self.assertLess(box["baseline_y"], box["y1"])


class TestSampleSentence(unittest.TestCase):
    def test_area_exists_on_page_3_inside_page(self):
        area = layout["sample_sentence"]
        self.assertIsNotNone(area)
        self.assertEqual(area["page"], 3)
        self.assertGreaterEqual(area["x0"], 0)
        self.assertGreaterEqual(area["y0"], 0)
        self.assertLessEqual(area["x1"], config.PAGE_WIDTH)
        self.assertLessEqual(area["y1"], config.PAGE_HEIGHT)
        self.assertEqual(len(area["baseline_ys"]), config.SENTENCE_LINES)


class TestPdfMatchesJson(unittest.TestCase):
    def test_exactly_three_pages(self):
        self.assertEqual(document.page_count, 3)
        self.assertEqual(layout["meta"]["page_count"], 3)
        self.assertEqual([p["page"] for p in layout["pages"]], [1, 2, 3])

    def test_page_sizes_match_json_and_device_shape(self):
        for info in layout["pages"]:
            rect = document[info["page"] - 1].rect
            self.assertAlmostEqual(rect.width, info["width"], delta=TOL)
            self.assertAlmostEqual(rect.height, info["height"], delta=TOL)
        px_w, px_h = config.DEVICE_PROFILE["display_px"]
        self.assertAlmostEqual(document[0].rect.width / document[0].rect.height, px_w / px_h, places=3)

    def test_json_boxes_match_rectangles_drawn_in_pdf(self):
        for info in layout["pages"]:
            page = document[info["page"] - 1]
            drawn = [d["rect"] for d in page.get_drawings() if d["items"] and d["items"][0][0] == "re"]
            expected = [b for b in layout["boxes"] if b["page"] == info["page"]]
            area = layout["sample_sentence"]
            if area["page"] == info["page"]:
                expected.append(area)
            for item in expected:
                target = to_pymupdf(item, page.rect.height)
                match = any(abs(d.x0 - target.x0) < TOL and abs(d.y0 - target.y0) < TOL
                            and abs(d.x1 - target.x1) < TOL and abs(d.y1 - target.y1) < TOL
                            for d in drawn)
                self.assertTrue(match, f"page {info['page']}: no drawn rectangle at {target}")
            # Nothing extra: every drawn rectangle must be described by the JSON.
            self.assertEqual(len(drawn), len(expected))

    def test_printed_labels_sit_beside_the_right_boxes(self):
        for info in layout["pages"]:
            page = document[info["page"] - 1]
            words = page.get_text("words")  # (x0, y0, x1, y1, text, ...)
            for box in (b for b in layout["boxes"] if b["page"] == info["page"] and b["sample"] == 1):
                target = to_pymupdf(box, page.rect.height)
                found = any(w[4] == box["char"] and w[2] <= target.x0 + TOL
                            and target.y0 - TOL <= (w[1] + w[3]) / 2 <= target.y1 + TOL
                            for w in words)
                self.assertTrue(found, f"label '{box['char']}' not beside its first box")

    def test_sentence_heading_is_printed_on_page_3(self):
        self.assertIn("Sample sentence", document[2].get_text())


class TestConfigValidation(unittest.TestCase):
    def test_current_device_is_valid(self):
        profile = config.validate_device(config.DEVICE)
        self.assertEqual(profile["display_px"], (1404, 1872))

    def test_unknown_device_is_rejected(self):
        with self.assertRaises(ValueError):
            config.validate_device("not_a_device")

    def test_wrong_aspect_ratio_is_rejected(self):
        config.DEVICES["_bad"] = {"name": "bad", "display_px": (1404, 1872), "page_pt": (600.0, 596.0)}
        try:
            with self.assertRaises(ValueError):
                config.validate_device("_bad")
        finally:
            del config.DEVICES["_bad"]


if __name__ == "__main__":
    unittest.main()
