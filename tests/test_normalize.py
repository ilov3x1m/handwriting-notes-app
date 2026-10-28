"""Completed templates whose page bounds were expanded by stray ink (a real reMarkable export problem).

All inputs are synthetic: the blank template with a stem drawn in every box, then page bounds changed.
"""

import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app_core as core  # noqa: E402
import app_fixtures as fx  # noqa: E402
import config  # noqa: E402
import extract  # noqa: E402

V2_LAYOUT = Path(config.TEMPLATES_DIR) / "v2" / "template_layout.json"


def expand_pages(data, boxes, mark=True):
    """Copy of a PDF whose listed pages (1-based -> (x0, y0, x1, y1) PDF bounds) have bigger bounds.

    The original page stays at the user-space origin, as in the real export; a small ink mark is
    drawn in the added area so the expansion has a cause.
    """
    document = pymupdf.open(stream=data, filetype="pdf")
    for number, (x0, y0, x1, y1) in boxes.items():
        page = document[number - 1]
        if mark:
            top = page.rect.height
            page.draw_rect(pymupdf.Rect(x0 + 5, top - y1 + 5, x0 + 15, top - y1 + 15), color=None, fill=(0, 0, 0))
        for key in ("MediaBox", "CropBox"):
            document.xref_set_key(page.xref, key, f"[{x0} {y0} {x1} {y1}]")
    return document.tobytes()


def glyph_files(folder):
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(folder).glob("*.png")}


class TestNormalization(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.good = fx.make_completed_template_bytes()
        cls.layout = extract.load_layout(V2_LAYOUT)
        good_path = Path(cls.tmp.name) / "good.pdf"
        good_path.write_bytes(cls.good)
        cls.reference = Path(cls.tmp.name) / "reference"
        extract.extract_to_directory(good_path, None, cls.reference)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_extract(self, data, name="in.pdf"):
        path = Path(self.tmp.name) / name
        path.write_bytes(data)
        out = Path(self.tmp.name) / ("out_" + name)
        shutil.rmtree(out, ignore_errors=True)
        metadata, warnings = extract.extract_to_directory(path, None, out)
        return path, out, metadata, warnings

    def test_a_normal_four_page_pdf_needs_no_trimming(self):
        document = pymupdf.open(stream=self.good, filetype="pdf")
        self.assertEqual(extract.validate_pdf(document, self.layout), [])

    def test_expanded_page_is_trimmed_and_glyphs_are_identical_to_the_normal_export(self):
        data = expand_pages(self.good, {4: (-59.03, 0, 463.09, 729.21)})   # the real John.pdf geometry
        path, out, metadata, warnings = self.run_extract(data, "expanded.pdf")
        self.assertEqual((metadata["meta"]["extracted_samples"], metadata["meta"]["expected_samples"]), (225, 225))
        self.assertEqual(glyph_files(out), glyph_files(self.reference))        # same crops, same alignment
        self.assertTrue(any("Page 4" in w and "trimmed" in w for w in warnings), warnings)
        self.assertEqual(path.read_bytes(), data)                              # the uploaded file is untouched

    def test_all_four_pages_can_be_expanded(self):
        data = expand_pages(self.good, {n: (-10, 0, 480, 640) for n in (1, 2, 3, 4)})
        _, out, metadata, warnings = self.run_extract(data, "all.pdf")
        self.assertEqual(metadata["meta"]["extracted_samples"], 225)
        self.assertEqual(glyph_files(out), glyph_files(self.reference))
        self.assertEqual(len([w for w in warnings if "trimmed" in w]), 4)

    def test_a_small_stray_mark_that_grows_one_edge_slightly(self):
        data = expand_pages(self.good, {2: (0, 0, 452, 596)})
        _, out, _, warnings = self.run_extract(data, "slight.pdf")
        self.assertEqual(glyph_files(out), glyph_files(self.reference))
        self.assertTrue(any("Page 2" in w for w in warnings))

    def test_spacing_measurement_also_accepts_the_expanded_pdf(self):
        import spacing
        data = expand_pages(self.good, {3: (-30, -20, 500, 640)})
        path = Path(self.tmp.name) / "spacing.pdf"
        path.write_bytes(data)
        style, _, _, _ = spacing.build_style_from_pdf(path, None, "x", Path(self.tmp.name))
        self.assertIn("word_spacing", style)

    def test_an_oversized_page_without_the_printed_template_is_refused_clearly(self):
        document = pymupdf.open()
        for _ in range(3):
            document.new_page(width=447, height=596)
        document.new_page(width=522, height=729)                               # right size class, wrong content
        path = Path(self.tmp.name) / "blank_big.pdf"
        path.write_bytes(document.tobytes())
        with self.assertRaises(extract.ExtractError) as caught:
            extract.extract_to_directory(path, None, Path(self.tmp.name) / "o1")
        message = str(caught.exception)
        self.assertIn("Page 4 is 522.0 x 729.0 pt", message)
        self.assertIn("could not be located", message)
        self.assertIn("Export the PDF again", message)

    def test_a_shifted_template_area_is_not_cropped_blindly(self):
        # bounds that do not include the user-space origin: the template position cannot be assumed
        data = expand_pages(self.good, {4: (20, 10, 560, 700)}, mark=False)
        with self.assertRaisesRegex(extract.ExtractError, "could not be located"):
            self.run_extract(data, "shifted.pdf")

    def test_a_page_smaller_than_the_template_is_refused(self):
        document = pymupdf.open()
        for _ in range(4):
            document.new_page(width=400, height=500)
        path = Path(self.tmp.name) / "small.pdf"
        path.write_bytes(document.tobytes())
        with self.assertRaisesRegex(extract.ExtractError, "smaller than the template"):
            extract.extract_to_directory(path, None, Path(self.tmp.name) / "o2")

    def test_wrong_page_counts_are_refused(self):
        for pages in (2, 5):
            document = pymupdf.open()
            for _ in range(pages):
                document.new_page(width=447, height=596)
            path = Path(self.tmp.name) / f"count{pages}.pdf"
            path.write_bytes(document.tobytes())
            with self.assertRaisesRegex(extract.ExtractError, "no template version matches"):
                extract.extract_to_directory(path, None, Path(self.tmp.name) / "o3")


class TestProfileCreationWithExpandedPdf(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "samples").mkdir()
        self.glyphs = self.root / "glyphs"
        self.glyphs.mkdir()
        for patch in (mock.patch.object(config, "BASE_DIR", self.root), mock.patch.object(config, "GLYPHS_DIR", self.glyphs),
                      mock.patch.object(config, "APP_WORKSPACE_ROOT", self.root / "jobs")):
            patch.start()
            self.addCleanup(patch.stop)
        self.workspace = core.new_workspace()

    def test_creates_a_complete_profile_without_touching_existing_ones(self):
        fx.make_profile(self.glyphs, "existing")
        before = glyph_files(self.glyphs / "existing")
        data = expand_pages(fx.make_completed_template_bytes(), {4: (-59, 0, 463, 729)})
        result = core.create_profile("friend", data, self.workspace)
        self.assertEqual((result["samples"], result["expected"]), (225, 225))
        self.assertEqual(core.check_profile(self.glyphs / "friend"), [])
        self.assertTrue(any("trimmed" in w for w in result["warnings"]), result["warnings"])
        self.assertEqual(glyph_files(self.glyphs / "existing"), before)
        self.assertEqual((self.root / "samples" / "friend.pdf").read_bytes(), data)   # kept exactly as uploaded

    def test_an_unfixable_pdf_explains_the_problem_and_saves_nothing(self):
        document = pymupdf.open()
        for _ in range(3):
            document.new_page(width=447, height=596)
        document.new_page(width=522, height=729)
        with self.assertRaises(core.AppError) as caught:
            core.create_profile("nope", document.tobytes(), self.workspace)
        self.assertIn("Export the PDF again", caught.exception.message)
        self.assertEqual(list(self.glyphs.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
