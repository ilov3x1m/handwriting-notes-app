"""Tests for extract.py. Run from the project root:

    python -m unittest discover -s tests -v

Most tests build a synthetic "filled-in" PDF: the real blank template with a
black rectangle of known size drawn into chosen boxes. Because the ink position
is known exactly, the tests can check the crop, trim, baseline and mapping
numbers precisely. One integration test runs on the real reMarkable export and
is skipped if that file is not on this machine.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import contact_sheet  # noqa: E402
import extract  # noqa: E402
import template  # noqa: E402

SCALE = config.EXTRACT_DPI / 72  # 4 pixels per point
PX_TOL = 2                        # pixels; anti-aliasing blurs edges by about a pixel
REAL_PDF = Path(config.BASE_DIR) / "samples" / "remarkable_test.pdf"

# Test ink: a vertical stem, `stem_width` wide, from DESCENT below the box's
# baseline up to ASCENT above it, so it crosses the baseline like a "g" or "y".
STEM_LEFT = 10   # pt from the box's left edge
DESCENT = 6      # pt below baseline
ASCENT = 14      # pt above baseline

_tmp = None
layout = None
blank_pdf = None


def setUpModule():
    global _tmp, layout, blank_pdf
    _tmp = tempfile.TemporaryDirectory()
    blank_pdf = os.path.join(_tmp.name, "blank.pdf")
    layout_path = os.path.join(_tmp.name, "layout.json")
    built = template.build_layout(1)
    template.write_pdf(built, blank_pdf)
    template.write_json(built, layout_path)
    layout = extract.load_layout(layout_path)


def tearDownModule():
    _tmp.cleanup()


def stem_width(index):
    """Different width per box index, so a glyph's width identifies its box."""
    return 2 + (index % 8) * 1.5


def make_filled_pdf(name, skip=lambda box: False, only=None):
    """Copy the blank template and draw one stem into each (selected) box."""
    path = os.path.join(_tmp.name, name)
    document = pymupdf.open(blank_pdf)
    for index, box in enumerate(layout["boxes"]):
        if skip(box) or (only is not None and (box["char"], box["sample"]) != only):
            continue
        height = layout["pages"][box["page"] - 1]["height"]
        x0 = box["x0"] + STEM_LEFT
        x1 = x0 + stem_width(index)
        # PyMuPDF wants top-left coordinates, so flip y here, independently of extract.py.
        top = height - (box["baseline_y"] + ASCENT)
        bottom = height - (box["baseline_y"] - DESCENT)
        document[box["page"] - 1].draw_rect(pymupdf.Rect(x0, top, x1, bottom), color=None, fill=(0, 0, 0))
    document.save(path)
    document.close()
    return path


def run_extraction(pdf_path):
    document = extract.open_pdf(pdf_path)
    extract.validate_pdf(document, layout)
    return extract.extract_all(document, layout)


def by_key(glyphs):
    return {(g["record"]["char"], g["record"]["sample"]): g for g in glyphs}


class TestValidation(unittest.TestCase):
    def test_real_template_passes(self):
        document = extract.open_pdf(blank_pdf)
        extract.validate_pdf(document, layout)  # must not raise

    def test_wrong_page_count(self):
        path = os.path.join(_tmp.name, "one_page.pdf")
        document = pymupdf.open()
        document.new_page(width=447, height=596)
        document.save(path)
        with self.assertRaisesRegex(extract.ExtractError, "1 pages"):
            extract.validate_pdf(extract.open_pdf(path), layout)

    def test_wrong_page_size(self):
        path = os.path.join(_tmp.name, "wrong_size.pdf")
        document = pymupdf.open()
        for _ in range(3):
            document.new_page(width=400, height=500)
        document.save(path)
        with self.assertRaisesRegex(extract.ExtractError, "would not line up"):
            extract.validate_pdf(extract.open_pdf(path), layout)

    def test_missing_pdf(self):
        with self.assertRaisesRegex(extract.ExtractError, "not found"):
            extract.open_pdf(os.path.join(_tmp.name, "nope.pdf"))

    def test_file_that_is_not_a_pdf(self):
        path = os.path.join(_tmp.name, "fake.pdf")
        with open(path, "w") as f:
            f.write("this is not a pdf")
        with self.assertRaises(extract.ExtractError):
            extract.open_pdf(path)

    def test_missing_layout_file(self):
        with self.assertRaisesRegex(extract.ExtractError, "not found"):
            extract.load_layout(os.path.join(_tmp.name, "nope.json"))

    def test_layout_with_missing_fields(self):
        path = os.path.join(_tmp.name, "bad_layout.json")
        with open(path, "w") as f:
            json.dump({"meta": {}, "pages": []}, f)
        with self.assertRaisesRegex(extract.ExtractError, "missing 'boxes'"):
            extract.load_layout(path)

    def test_layout_with_inconsistent_counts(self):
        path = os.path.join(_tmp.name, "bad_counts.json")
        with open(path, "w") as f:
            json.dump({"meta": {"page_count": 3, "box_count": 5}, "pages": [], "boxes": []}, f)
        with self.assertRaises(extract.ExtractError):
            extract.load_layout(path)

    def test_main_reports_errors_without_crashing(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = extract.main([os.path.join(_tmp.name, "nope.pdf"), "--user", "t"])
        self.assertEqual(code, 1)
        self.assertIn("Error:", stderr.getvalue())

    def test_user_name_is_restricted(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = extract.main([blank_pdf, "--user", "../evil"])
        self.assertEqual(code, 1)


class TestCoordinates(unittest.TestCase):
    def test_y_axis_flip(self):
        self.assertEqual(extract.pdf_y_to_pixel_y(0, 596, 4), 2384)    # bottom of page -> last row
        self.assertEqual(extract.pdf_y_to_pixel_y(596, 596, 4), 0)     # top of page -> row 0
        self.assertEqual(extract.pdf_y_to_pixel_y(505, 596, 4), 364)

    def test_known_box_to_pixel_crop(self):
        # Box A/1 from the layout: x 44..98.833, y 505..538 on a 596 pt page.
        rect = {"x0": 44.0, "y0": 505.0, "x1": 98.833, "y1": 538.0}
        self.assertEqual(extract.pdf_rect_to_pixel_box(rect, 596, 4, 1.0), (180, 236, 391, 360))

    def test_crop_is_inside_the_box_and_top_left_ordered(self):
        for box in layout["boxes"][:20]:
            left, top, right, bottom = extract.pdf_rect_to_pixel_box(box, 596, SCALE, 0)
            self.assertLess(left, right)
            self.assertLess(top, bottom)
            self.assertEqual(top, round((596 - box["y1"]) * SCALE))  # higher y = smaller row

    def test_inset_shrinks_every_side(self):
        rect = {"x0": 40, "y0": 100, "x1": 90, "y1": 140}
        plain = extract.pdf_rect_to_pixel_box(rect, 596, 4, 0)
        inset = extract.pdf_rect_to_pixel_box(rect, 596, 4, 1)
        self.assertEqual(tuple(b - a for a, b in zip(plain, inset)), (4, 4, -4, -4))

    def test_crop_lands_on_the_ink(self):
        """Ink drawn at a known PDF position must appear at the matching trimmed offset."""
        path = make_filled_pdf("one_g.pdf", only=("g", 2))
        glyphs, _, _ = run_extraction(path)
        record = glyphs[0]["record"]
        index = next(i for i, b in enumerate(layout["boxes"]) if (b["char"], b["sample"]) == ("g", 2))
        ink_left_px = (layout["boxes"][index]["x0"] + STEM_LEFT) * SCALE
        got = record["crop_px"]["left"] + record["trim_offset_px"]["left"] + config.TRIM_PADDING_PX
        self.assertAlmostEqual(got, ink_left_px, delta=PX_TOL)


class TestMappingAndOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = make_filled_pdf("full.pdf")
        cls.glyphs, cls.missing, cls.warnings = run_extraction(cls.pdf)

    def test_all_186_samples_extracted(self):
        self.assertEqual(len(self.glyphs), 186)
        self.assertEqual(self.missing, [])
        self.assertEqual(self.warnings, [])

    def test_every_character_and_sample_present(self):
        keys = {(g["record"]["char"], g["record"]["sample"]) for g in self.glyphs}
        expected = {(b["char"], b["sample"]) for b in layout["boxes"]}
        self.assertEqual(keys, expected)

    def test_each_glyph_comes_from_its_own_box(self):
        """Width of the extracted PNG identifies which box it was cut from."""
        found = by_key(self.glyphs)
        for index, box in enumerate(layout["boxes"]):
            record = found[(box["char"], box["sample"])]["record"]
            self.assertEqual(record["page"], box["page"])
            expected_width = stem_width(index) * SCALE + 2 * config.TRIM_PADDING_PX
            self.assertAlmostEqual(record["image_size_px"]["width"], expected_width, delta=PX_TOL,
                                   msg=f"{box['char']}_{box['sample']}")

    def test_trim_removes_whitespace_and_template_lines(self):
        crop_width = self.glyphs[0]["record"]["crop_px"]["right"] - self.glyphs[0]["record"]["crop_px"]["left"]
        for glyph in self.glyphs:
            size = glyph["record"]["image_size_px"]
            # A retained baseline guide or border would make the PNG as wide as the crop.
            self.assertLess(size["width"], crop_width / 3)
            expected_height = (ASCENT + DESCENT) * SCALE + 2 * config.TRIM_PADDING_PX
            self.assertAlmostEqual(size["height"], expected_height, delta=PX_TOL)

    def test_strokes_are_preserved(self):
        found = by_key(self.glyphs)
        for index, box in enumerate(layout["boxes"][:40]):
            record = found[(box["char"], box["sample"])]["record"]
            expected_ink = stem_width(index) * SCALE * (ASCENT + DESCENT) * SCALE
            self.assertAlmostEqual(record["ink_pixels"] / expected_ink, 1.0, delta=0.08)

    def test_png_is_transparent_black_ink(self):
        image = self.glyphs[0]["image"]
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getpixel((0, 0))[3], 0)  # padding corner is transparent
        centre = image.getpixel((image.width // 2, image.height // 2))
        self.assertEqual(centre, (0, 0, 0, 255))  # stroke is solid black

    def test_descender_and_baseline_metadata(self):
        """The stem runs 6 pt below and 14 pt above the baseline."""
        pad = config.TRIM_PADDING_PX
        for glyph in self.glyphs[:30]:
            record = glyph["record"]
            height = record["image_size_px"]["height"]
            self.assertAlmostEqual(record["baseline_from_top_px"], ASCENT * SCALE + pad, delta=PX_TOL)
            self.assertAlmostEqual(record["baseline_from_bottom_px"], DESCENT * SCALE + pad, delta=PX_TOL)
            self.assertAlmostEqual(record["baseline_from_top_px"] + record["baseline_from_bottom_px"], height, places=1)

    def test_descender_pixels_exist_below_the_baseline(self):
        glyph = by_key(self.glyphs)[("g", 1)]
        alpha = glyph["image"].getchannel("A")
        baseline_row = round(glyph["record"]["baseline_from_top_px"])
        below = alpha.crop((0, baseline_row + 1, alpha.width, alpha.height))
        self.assertGreater(sum(below.histogram()[config.INK_ALPHA:]), (DESCENT - 1) * SCALE * 4)

    def test_metadata_records_original_geometry(self):
        found = by_key(self.glyphs)
        box = next(b for b in layout["boxes"] if (b["char"], b["sample"]) == ("A", 1))
        record = found[("A", 1)]["record"]
        self.assertEqual(record["pdf_bbox"], {k: box[k] for k in ("x0", "y0", "x1", "y1")})
        self.assertEqual(record["baseline_y_pdf"], box["baseline_y"])
        self.assertEqual(record["crop_px"], {"left": 180, "top": 236, "right": 391, "bottom": 360})

    def test_saved_files_and_directory_structure(self):
        out_dir = Path(_tmp.name) / "glyphs_out" / "tester"
        metadata = extract.save_results(self.glyphs, self.missing, layout, self.pdf, out_dir)

        names = sorted(p.name for p in out_dir.glob("*.png"))
        self.assertEqual(len(names), 186)
        # Case-insensitive file systems (macOS) must not merge A_x and a_x.
        self.assertEqual(len({n.lower() for n in names}), 186)
        self.assertIn("A_upper_1.png", names)
        self.assertIn("a_1.png", names)
        self.assertIn("5_2.png", names)
        self.assertTrue((out_dir / config.GLYPH_METADATA_NAME).exists())

        with open(out_dir / config.GLYPH_METADATA_NAME) as f:
            saved = json.load(f)
        self.assertEqual(saved["meta"]["extracted_samples"], 186)
        self.assertEqual(saved["meta"]["missing_samples"], 0)
        self.assertEqual(len(saved["glyphs"]), 186)
        for record in saved["glyphs"]:
            self.assertTrue((out_dir / record["file"]).exists())
            with Image.open(out_dir / record["file"]) as image:
                self.assertEqual(list(image.size), [record["image_size_px"]["width"], record["image_size_px"]["height"]])
        self.assertEqual(metadata["meta"]["dpi"], config.EXTRACT_DPI)

    def test_contact_sheets_are_written(self):
        out_dir = Path(_tmp.name) / "glyphs_sheet" / "tester"
        metadata = extract.save_results(self.glyphs, self.missing, layout, self.pdf, out_dir)
        with mock.patch.object(config, "PREVIEW_DIR", Path(_tmp.name) / "preview"):
            paths = contact_sheet.make_sheets(out_dir, "tester", metadata)
        self.assertEqual(len(paths), 3)
        for path in paths:
            with Image.open(path) as sheet:
                self.assertGreater(sheet.width, 300)


class TestBlankAndPrintedTemplate(unittest.TestCase):
    def test_blank_template_yields_no_glyphs(self):
        """Borders, baseline guides and printed labels alone are never mistaken for ink."""
        glyphs, missing, warnings = run_extraction(blank_pdf)
        self.assertEqual(glyphs, [])
        self.assertEqual(len(missing), 186)
        self.assertEqual(len(warnings), 186)

    def test_blank_samples_are_reported_by_name(self):
        skip = lambda box: (box["char"], box["sample"]) in {("Q", 3), ("z", 1), ("z", 2), ("z", 3)}  # noqa: E731
        path = make_filled_pdf("with_gaps.pdf", skip=skip)
        glyphs, missing, warnings = run_extraction(path)
        self.assertEqual(len(glyphs), 182)
        self.assertEqual({(m["char"], m["sample"]) for m in missing},
                         {("Q", 3), ("z", 1), ("z", 2), ("z", 3)})
        self.assertTrue(any("'Q' sample 3" in w for w in warnings))
        self.assertNotIn(("z", 1), by_key(glyphs))  # nothing invented

    def test_stale_file_removed_when_sample_becomes_blank(self):
        out_dir = Path(_tmp.name) / "stale" / "tester"
        out_dir.mkdir(parents=True)
        stale = out_dir / "Q_upper_3.png"
        Image.new("RGBA", (5, 5)).save(stale)
        extract.save_results([], [{"char": "Q", "sample": 3, "page": 1, "reason": "x"}],
                             layout, "x.pdf", out_dir)
        self.assertFalse(stale.exists())

    def test_single_speck_is_not_a_glyph(self):
        gray = Image.new("L", (200, 120), 255)
        gray.putpixel((50, 50), 0)
        self.assertLess(extract.count_ink(extract.ink_to_alpha(gray)), config.MIN_INK_PIXELS)

    def test_baseline_guide_gray_becomes_transparent(self):
        gray = Image.new("L", (100, 60), 255)
        for x in range(100):
            gray.putpixel((x, 40), round(config.BASELINE_GRAY * 255))  # printed baseline guide
        self.assertIsNone(extract.ink_to_alpha(gray).getbbox())

    def test_crops_of_blank_template_contain_no_border_or_guide_pixels(self):
        """Every pixel of every crop of the blank template must come out fully transparent.

        The border is excluded by cropping inside it; the baseline guide by the threshold.
        """
        document = pymupdf.open(blank_pdf)
        for info in layout["pages"]:
            page_image = extract.render_page(document[info["page"] - 1], SCALE)
            for box in layout["boxes"]:
                if box["page"] != info["page"]:
                    continue
                crop = extract.pdf_rect_to_pixel_box(box, info["height"], SCALE, config.CROP_INSET_PT)
                alpha = extract.ink_to_alpha(page_image.crop(crop))
                self.assertEqual(alpha.getextrema()[1], 0, f"{box['char']}_{box['sample']}")

    def test_crop_inset_clears_the_printed_border(self):
        half_border = config.BOX_BORDER_WIDTH / 2
        self.assertGreater(config.CROP_INSET_PT, half_border + 0.5)  # leaves room for anti-aliasing

    def test_ink_touching_the_crop_edge_is_flagged(self):
        path = make_filled_pdf("edge.pdf", only=("A", 1))
        document = pymupdf.open(path)
        box = next(b for b in layout["boxes"] if (b["char"], b["sample"]) == ("A", 1))
        height = layout["pages"][0]["height"]
        # Ink that crosses the box's left border.
        document[0].draw_rect(pymupdf.Rect(box["x0"] - 3, height - box["y1"] + 8, box["x0"] + 6, height - box["y1"] + 12),
                              color=None, fill=(0, 0, 0))
        edge_path = os.path.join(_tmp.name, "edge2.pdf")
        document.save(edge_path)
        _, _, warnings = run_extraction(edge_path)
        self.assertTrue(any("'A' sample 1" in w and "crop edge" in w for w in warnings))


class TestRealRemarkableExport(unittest.TestCase):
    @unittest.skipUnless(REAL_PDF.exists(), "samples/remarkable_test.pdf is not on this machine")
    def test_real_export_extracts_all_186(self):
        with tempfile.TemporaryDirectory() as out:
            with mock.patch.object(config, "GLYPHS_DIR", Path(out) / "glyphs"), \
                 mock.patch.object(config, "PREVIEW_DIR", Path(out) / "preview"), \
                 contextlib.redirect_stdout(io.StringIO()):
                code = extract.main([str(REAL_PDF), "--user", "real"])
            glyph_dir = Path(out) / "glyphs" / "real"
            with open(glyph_dir / config.GLYPH_METADATA_NAME) as f:
                metadata = json.load(f)

            self.assertEqual(code, 0)
            self.assertEqual(metadata["meta"]["extracted_samples"], 186)
            self.assertEqual(metadata["missing"], [])
            self.assertEqual(len(list(glyph_dir.glob("*.png"))), 186)
            self.assertEqual(len(list((Path(out) / "preview").glob("*.png"))), 3)

            crop = metadata["glyphs"][0]["crop_px"]
            crop_width = crop["right"] - crop["left"]
            for record in metadata["glyphs"]:
                with Image.open(glyph_dir / record["file"]) as image:
                    alpha = image.getchannel("A")
                    self.assertEqual(image.size[0], record["image_size_px"]["width"])
                # Real handwriting is far narrower than a box; a kept border or
                # guide line would span the whole crop width.
                self.assertLess(record["image_size_px"]["width"], crop_width * 0.6, record["file"])
                self.assertGreater(record["ink_pixels"], config.MIN_INK_PIXELS)
                rows = [sum(1 for x in range(alpha.width) if alpha.getpixel((x, y)) >= config.INK_ALPHA)
                        for y in range(alpha.height)]
                self.assertLess(max(rows), 0.9 * alpha.width, f"{record['file']} has a full-width line")
                self.assertAlmostEqual(record["baseline_from_top_px"] + record["baseline_from_bottom_px"],
                                       record["image_size_px"]["height"], places=1)


if __name__ == "__main__":
    unittest.main()
