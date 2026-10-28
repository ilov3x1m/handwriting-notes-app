"""Tests for spacing.py. Run from the project root:

    python -m unittest discover -s tests -v

Unit tests use synthetic images with black blocks at known positions, so the
expected gaps and baselines are exact. End-to-end tests draw known "handwriting"
into the real blank template's sentence area. One integration test runs on the
real reMarkable export and is skipped if that file is not on this machine.
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

import numpy as np
import pymupdf
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import extract  # noqa: E402
import spacing  # noqa: E402
import template  # noqa: E402

SCALE = config.EXTRACT_DPI / 72  # 4 px per point
REAL_PDF = Path(config.BASE_DIR) / "samples" / "remarkable_test.pdf"
PAGE_HEIGHT = 596

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
    spacing_layout_path = layout_path
    globals()["layout_path"] = spacing_layout_path


def tearDownModule():
    _tmp.cleanup()


# ---------------------------------------------------------------------------
# Synthetic input builders
# ---------------------------------------------------------------------------

def blank_image(width=700, height=240):
    return Image.new("L", (width, height), 255)


def draw_blocks(image, top, bottom, x, widths, gaps, bottoms=None):
    """Draw black blocks left to right. gaps[i] is the empty space after block i.

    `bottoms` optionally overrides each block's bottom row (to vary the baseline).
    Returns the list of (left, right) column spans drawn.
    """
    draw = ImageDraw.Draw(image)
    spans = []
    for index, width in enumerate(widths):
        block_bottom = bottoms[index] if bottoms else bottom
        draw.rectangle([x, top, x + width - 1, block_bottom - 1], fill=0)
        spans.append((x, x + width))
        x += width + (gaps[index] if index < len(gaps) else 0)
    return spans


def sentence_pdf(name, letter_gap, word_gap, letter_width=5, letter_height=12):
    """Blank template plus rectangles "written" in the sentence area.

    Every letter of the prompt is a letter_width x letter_height block resting on
    the printed guide of its line (6 words on line 1, 3 on line 2, like the real sample).
    """
    area = layout["sample_sentence"]
    words = spacing.sentence_words(area["text"])
    document = pymupdf.open(blank_pdf)
    page = document[area["page"] - 1]
    for line_index, line_words in enumerate((words[:6], words[6:])):
        baseline_y = area["baseline_ys"][line_index]
        x = area["x0"] + 10
        for word in line_words:
            for letter_index in range(len(word)):
                # PyMuPDF uses a top-left origin, so flip y independently of spacing.py.
                page.draw_rect(pymupdf.Rect(x, PAGE_HEIGHT - baseline_y - letter_height,
                                            x + letter_width, PAGE_HEIGHT - baseline_y),
                               color=None, fill=(0, 0, 0))
                x += letter_width + (letter_gap if letter_index < len(word) - 1 else word_gap)
    path = os.path.join(_tmp.name, name)
    document.save(path)
    document.close()
    return path


def run_spacing(pdf_path, user="tester", glyph_metadata=True, extra_args=()):
    """Run spacing.main with output redirected to a temporary folder. Returns (code, glyph_dir, preview_dir, out, err)."""
    root = Path(tempfile.mkdtemp(dir=_tmp.name))
    glyph_dir = root / "glyphs" / user
    if glyph_metadata:
        glyph_dir.mkdir(parents=True)
        with open(glyph_dir / config.GLYPH_METADATA_NAME, "w") as f:
            json.dump({"sentinel": 123, "meta": {"pixels_per_point": 4.0, "trim_padding_px": 4},
                       "glyphs": [{"baseline_from_bottom_px": 4 + i} for i in range(5)]}, f)
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(config, "GLYPHS_DIR", root / "glyphs"), \
         mock.patch.object(config, "PREVIEW_DIR", root / "preview"), \
         contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = spacing.main([pdf_path, "--user", user, "--layout", layout_path, *extra_args])
    return code, glyph_dir, root / "preview", out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# Loading and coordinates
# ---------------------------------------------------------------------------

class TestAreaAndCoordinates(unittest.TestCase):
    def test_sentence_area_is_loaded_from_the_layout(self):
        area = layout["sample_sentence"]
        self.assertEqual(area["page"], 3)
        self.assertEqual(spacing.sentence_words(area["text"]),
                         ["The", "quick", "brown", "fox", "jumps", "over", "the", "lazy", "dog"])

    def test_crop_matches_the_area_and_guides_use_the_pdf_to_image_flip(self):
        document = extract.open_pdf(blank_pdf)
        gray, guide_rows, box = spacing.crop_sentence_area(document, layout, SCALE)
        area = layout["sample_sentence"]
        self.assertEqual(box, extract.pdf_rect_to_pixel_box(area, PAGE_HEIGHT, SCALE, config.CROP_INSET_PT))
        self.assertEqual(gray.size, (box[2] - box[0], box[3] - box[1]))
        # Area top is y=299 pt; the crop starts 1 pt lower: row (596 - 298) * 4 = 1192.
        # Guide y=257 pt -> page row (596 - 257) * 4 = 1356 -> crop row 164.
        self.assertEqual(guide_rows, [164, 404, 644, 884])

    def test_ink_drawn_at_a_known_pdf_position_lands_on_the_expected_rows(self):
        path = sentence_pdf("conv.pdf", letter_gap=2, word_gap=12)
        document = extract.open_pdf(path)
        gray, guide_rows, _ = spacing.crop_sentence_area(document, layout, SCALE)
        _, segmentation, _ = spacing.analyse_image(gray, layout["sample_sentence"]["text"], guide_rows, SCALE)
        first_line = segmentation["lines"][0]
        # Letters rest on the guide (y=257) and are 12 pt = 48 px tall.
        self.assertEqual(first_line["rows"], (guide_rows[0] - 48, guide_rows[0]))

    def test_missing_sentence_area_in_layout(self):
        broken = {k: v for k, v in layout.items() if k != "sample_sentence"}
        with self.assertRaisesRegex(extract.ExtractError, "no sample_sentence"):
            spacing.crop_sentence_area(extract.open_pdf(blank_pdf), broken, SCALE)

    def test_sentence_area_with_missing_field(self):
        broken = dict(layout, sample_sentence={"page": 3})
        with self.assertRaisesRegex(extract.ExtractError, "missing"):
            spacing.crop_sentence_area(extract.open_pdf(blank_pdf), broken, SCALE)


# ---------------------------------------------------------------------------
# Segmentation and gaps (synthetic images)
# ---------------------------------------------------------------------------

class TestSegmentation(unittest.TestCase):
    def test_printed_guide_lines_are_not_ink(self):
        image = blank_image()
        draw = ImageDraw.Draw(image)
        for row in (60, 160):
            draw.line([(0, row), (700, row)], fill=round(config.BASELINE_GRAY * 255))
        draw_blocks(image, 30, 60, 50, [20, 20], [8])
        mask = spacing.ink_mask(np.asarray(image))
        self.assertEqual(int(mask.sum()), 2 * 20 * 30)            # only the blocks
        self.assertEqual(len(spacing.find_text_lines(mask, 24)), 1)  # not a line at row 160
        regions = spacing.find_regions(mask, (30, 60), 1)
        self.assertEqual([(r["left"], r["right"]) for r in regions], [(50, 70), (78, 98)])

    def test_gaps_between_separate_regions_are_measured(self):
        image = blank_image()
        draw_blocks(image, 30, 70, 20, [10, 12, 8, 15], [3, 7, 11])
        mask = spacing.ink_mask(np.asarray(image))
        line = {"rows": (30, 70), "regions": spacing.find_regions(mask, (30, 70), 1)}
        spacing.classify_gaps([line], expected_word_gaps=0)
        self.assertEqual([g["width_px"] for g in line["gaps"]], [3, 7, 11])
        self.assertTrue(all(g["kind"] == "letter" for g in line["gaps"]))

    def test_i_dot_does_not_become_its_own_text_line(self):
        image = blank_image()
        draw_blocks(image, 40, 80, 20, [10], [])
        ImageDraw.Draw(image).rectangle([22, 30, 25, 33], fill=0)  # dot, 6 px above the body
        mask = spacing.ink_mask(np.asarray(image))
        lines = spacing.find_text_lines(mask, config.SPACING_LINE_MERGE_GAP_PT * SCALE)
        self.assertEqual(lines, [(30, 80)])

    def test_word_gaps_are_told_apart_from_letter_gaps(self):
        image = blank_image()
        # "ab cd ef": letter gaps 4 px, word gaps 40 px.
        draw_blocks(image, 40, 80, 20, [10] * 6, [4, 40, 4, 40, 4])
        measurements, segmentation, warnings = spacing.analyse_image(image, "ab cd ef", [80], SCALE)
        kinds = [g["kind"] for g in segmentation["lines"][0]["gaps"]]
        self.assertEqual(kinds, ["letter", "word", "letter", "word", "letter"])
        self.assertEqual(measurements["word_spacing"]["observations"], 2)
        self.assertAlmostEqual(measurements["word_spacing"]["mean_pt"], 40 / SCALE, places=3)
        self.assertEqual(measurements["word_spacing"]["quality"], "good")
        self.assertEqual([w["text"] for w in segmentation["words"]], ["ab", "cd", "ef"])
        self.assertEqual(measurements["letter_spacing"]["observations"], 3)

    def test_sentence_wrapping_onto_two_lines_is_handled_per_line(self):
        image = blank_image(700, 260)
        draw_blocks(image, 40, 80, 20, [10] * 4, [4, 40, 4])     # line 1: "ab cd"
        draw_blocks(image, 140, 180, 20, [10] * 4, [4, 40, 4])   # line 2: "ef gh"
        measurements, segmentation, _ = spacing.analyse_image(image, "ab cd ef gh", [80, 180], SCALE)
        self.assertEqual(len(segmentation["lines"]), 2)
        # 4 words -> 3 word gaps, one of which is the line break, which is never measured.
        self.assertEqual(segmentation["expected_word_gaps"], 2)
        self.assertEqual(measurements["word_spacing"]["observations"], 2)
        self.assertEqual([w["text"] for w in segmentation["words"]], ["ab", "cd", "ef", "gh"])
        self.assertEqual([w["line"] for w in segmentation["words"]], [0, 0, 1, 1])

    def test_ambiguous_word_gaps_are_flagged_not_called_good(self):
        image = blank_image()
        draw_blocks(image, 40, 80, 20, [10] * 6, [20, 24, 20, 24, 20])  # word gaps barely bigger
        measurements, _, warnings = spacing.analyse_image(image, "ab cd ef", [80], SCALE)
        self.assertEqual(measurements["word_spacing"]["quality"], "approximate")
        self.assertTrue(any("not clearly larger" in w for w in warnings))

    def test_touching_letters_are_reported_as_unresolved(self):
        image = blank_image()
        # "abcd efgh": a and b touch (one 20 px region), so 5 of the 6 letter pairs show a gap.
        draw_blocks(image, 40, 80, 20, [20, 10, 10, 10, 10, 10, 10], [4, 4, 40, 4, 4, 4])
        measurements, _, _ = spacing.analyse_image(image, "abcd efgh", [80], SCALE)
        letter = measurements["letter_spacing"]
        self.assertEqual(letter["expected_letter_pairs"], 6)
        self.assertEqual(letter["unresolved_pairs"], 1)
        # Five 4-px gaps (1 pt each) spread over 6 pairs, the touching pair counting as 0.
        self.assertAlmostEqual(letter["estimate_pt"], 5 * 1.0 / 6, places=3)
        self.assertEqual(letter["quality"], "approximate")

    def test_blank_sample_is_rejected(self):
        with self.assertRaisesRegex(extract.ExtractError, "No handwriting"):
            spacing.analyse_image(blank_image(), "ab cd", [80], SCALE)

    def test_too_little_handwriting_gives_null_values_not_guesses(self):
        image = blank_image()
        draw_blocks(image, 40, 80, 20, [20, 20, 20], [5, 5])  # 3 regions, 2 gaps
        measurements, _, warnings = spacing.analyse_image(image, "abc", [80], SCALE)
        for key in ("letter_spacing", "word_spacing", "baseline_variation"):
            self.assertEqual(measurements[key]["quality"], "insufficient", key)
        self.assertIsNone(measurements["letter_spacing"]["estimate_pt"])
        self.assertIsNone(measurements["baseline_variation"]["robust_std_pt"])
        self.assertGreaterEqual(len(warnings), 3)


# ---------------------------------------------------------------------------
# Baseline variation
# ---------------------------------------------------------------------------

class TestBaseline(unittest.TestCase):
    def setUp(self):
        self.bottoms = [80, 82, 78, 84, 80, 76, 80, 81, 79, 80]  # px, around row 80
        self.image = blank_image()
        draw_blocks(self.image, 30, 80, 20, [10] * 10, [6] * 9, bottoms=self.bottoms)

    def analyse(self, image, guide=80):
        return spacing.analyse_image(image, "abcdefghij", [guide], SCALE)[0]["baseline_variation"]

    def test_robust_std_matches_the_known_spread(self):
        result = self.analyse(self.image)
        centred = (np.array(self.bottoms) - np.median(self.bottoms)) / SCALE
        self.assertEqual(result["observations"], 10)
        self.assertAlmostEqual(result["robust_std_pt"], round(spacing.robust_std(centred), 3), places=3)
        self.assertAlmostEqual(result["std_pt"], round(float(np.std(centred)), 3), places=3)

    def test_offset_from_the_printed_guide_uses_the_stated_sign(self):
        # Median bottom is row 80; a guide at row 88 is 2 pt BELOW the letters -> +2 pt above guide.
        result = self.analyse(self.image, guide=88)
        self.assertAlmostEqual(result["per_line"][0]["median_bottom_above_guide_pt"], 8 / SCALE)
        result = self.analyse(self.image, guide=76)
        self.assertAlmostEqual(result["per_line"][0]["median_bottom_above_guide_pt"], -4 / SCALE)

    def test_spread_is_unchanged_by_moving_the_whole_line(self):
        shifted = blank_image()
        draw_blocks(shifted, 50, 100, 20, [10] * 10, [6] * 9, bottoms=[b + 20 for b in self.bottoms])
        self.assertEqual(self.analyse(self.image)["robust_std_pt"], self.analyse(shifted, guide=100)["robust_std_pt"])

    def test_perfectly_level_writing_has_zero_spread(self):
        level = blank_image()
        draw_blocks(level, 30, 80, 20, [10] * 10, [6] * 9)
        self.assertEqual(self.analyse(level)["robust_std_pt"], 0.0)

    def test_a_few_descenders_do_not_dominate_the_robust_spread(self):
        bottoms = [80] * 10
        bottoms[2], bottoms[6] = 100, 100  # two letters with long descenders
        image = blank_image()
        draw_blocks(image, 30, 80, 20, [10] * 10, [6] * 9, bottoms=bottoms)
        result = self.analyse(image)
        self.assertEqual(result["robust_std_pt"], 0.0)
        self.assertGreater(result["std_pt"], 1.0)  # the plain std does react


# ---------------------------------------------------------------------------
# End to end: JSON, errors, repeat runs, real data
# ---------------------------------------------------------------------------

class TestEndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pdf = sentence_pdf("tight.pdf", letter_gap=2, word_gap=12)
        cls.code, cls.glyph_dir, cls.preview_dir, cls.out, cls.err = run_spacing(cls.pdf)
        with open(cls.glyph_dir / config.STYLE_NAME) as f:
            cls.style = json.load(f)

    def test_runs_and_writes_valid_style_json(self):
        self.assertEqual(self.code, 0, self.err)
        for key in ("schema_version", "user", "source_pdf", "device", "analysis_date", "units",
                    "letter_spacing", "word_spacing", "baseline_variation", "quality_labels", "warnings"):
            self.assertIn(key, self.style)
        self.assertEqual(self.style["user"], "tester")
        self.assertEqual(self.style["source_pdf"], self.pdf)
        self.assertEqual(self.style["device"]["key"], "remarkable_2")
        self.assertEqual(self.style["units"]["pixels_per_point"], SCALE)
        for key in ("letter_spacing", "word_spacing", "baseline_variation"):
            self.assertEqual(self.style[key]["unit"], "pt")
            self.assertIn(self.style[key]["quality"], self.style["quality_labels"])
            self.assertTrue(self.style[key]["method"])

    def test_measurements_match_the_known_synthetic_writing(self):
        self.assertEqual(self.style["sentence"]["text_lines_detected"], 2)
        self.assertEqual(self.style["sentence"]["words_detected"], 9)
        word, letter, base = self.style["word_spacing"], self.style["letter_spacing"], self.style["baseline_variation"]
        self.assertEqual(word["observations"], 7)
        self.assertAlmostEqual(word["mean_pt"], 12.0, places=2)
        self.assertEqual(word["quality"], "good")
        self.assertEqual(letter["observations"], 26)            # 35 letters - 9 words
        self.assertAlmostEqual(letter["estimate_pt"], 2.0, places=2)
        self.assertEqual(letter["unresolved_pairs"], 0)
        self.assertEqual(letter["quality"], "good")
        self.assertEqual(base["robust_std_pt"], 0.0)            # every letter rests on the guide
        self.assertAlmostEqual(base["median_bottom_above_guide_pt"], 0.0, places=2)

    def test_output_comes_from_the_input_not_from_constants(self):
        wide = sentence_pdf("wide.pdf", letter_gap=4, word_gap=24)
        code, glyph_dir, _, _, err = run_spacing(wide)
        self.assertEqual(code, 0, err)
        with open(glyph_dir / config.STYLE_NAME) as f:
            style = json.load(f)
        self.assertAlmostEqual(style["word_spacing"]["mean_pt"], 24.0, places=2)
        self.assertAlmostEqual(style["letter_spacing"]["estimate_pt"], 4.0, places=2)
        self.assertNotEqual(style["word_spacing"]["mean_pt"], self.style["word_spacing"]["mean_pt"])

    def test_preview_image_is_written(self):
        path = self.preview_dir / f"{config.SPACING_PREVIEW_PREFIX}_tester.png"
        self.assertTrue(path.exists())
        with Image.open(path) as image:
            self.assertGreater(image.width, 1000)

    def test_repeated_runs_are_valid_and_leave_glyph_metadata_alone(self):
        code1, glyph_dir, _, _, _ = run_spacing(self.pdf)
        metadata_path = glyph_dir / config.GLYPH_METADATA_NAME
        before = metadata_path.read_bytes()
        root = glyph_dir.parent.parent
        outputs = []
        for _ in range(2):
            with mock.patch.object(config, "GLYPHS_DIR", root / "glyphs"), \
                 mock.patch.object(config, "PREVIEW_DIR", root / "preview"), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(spacing.main([self.pdf, "--user", "tester", "--layout", layout_path]), 0)
            with open(glyph_dir / config.STYLE_NAME) as f:
                outputs.append(json.load(f))
        self.assertEqual(code1, 0)
        self.assertEqual(metadata_path.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in glyph_dir.iterdir()),
                         sorted([config.GLYPH_METADATA_NAME, config.STYLE_NAME]))  # no stray .tmp file
        for style in outputs:
            style.pop("analysis_date")
        self.assertEqual(outputs[0], outputs[1])

    def test_glyph_reference_is_read_from_the_metadata_file(self):
        reference = self.style["glyph_reference"]
        self.assertEqual(reference["observations"], 5)
        # Fixture glyphs have baseline_from_bottom_px 4..8 -> minus padding 4 -> 0..4 px -> median 2 px.
        self.assertAlmostEqual(reference["median_glyph_bottom_above_guide_pt"], 2 / SCALE, places=3)

    def test_missing_glyph_metadata_is_a_warning_not_a_failure(self):
        code, glyph_dir, _, out, _ = run_spacing(self.pdf, user="nometa", glyph_metadata=False)
        self.assertEqual(code, 0)
        with open(glyph_dir / config.STYLE_NAME) as f:
            style = json.load(f)
        self.assertIsNone(style["glyph_reference"])
        self.assertTrue(any("glyph_metadata.json" in w for w in style["warnings"]))


class TestErrors(unittest.TestCase):
    def test_missing_pdf(self):
        code, glyph_dir, _, _, err = run_spacing(os.path.join(_tmp.name, "nope.pdf"))
        self.assertEqual(code, 1)
        self.assertIn("not found", err)
        self.assertFalse((glyph_dir / config.STYLE_NAME).exists())

    def test_missing_layout_file(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = spacing.main([blank_pdf, "--user", "t", "--layout", os.path.join(_tmp.name, "nope.json")])
        self.assertEqual(code, 1)
        self.assertIn("Layout file not found", err.getvalue())

    def test_layout_without_sample_sentence(self):
        bad = os.path.join(_tmp.name, "no_sentence.json")
        data = {k: v for k, v in layout.items() if k != "sample_sentence"}
        with open(bad, "w") as f:
            json.dump(data, f)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = spacing.main([blank_pdf, "--user", "t", "--layout", bad])
        self.assertEqual(code, 1)
        self.assertIn("sample_sentence", err.getvalue())

    def test_wrong_page_size_pdf(self):
        path = os.path.join(_tmp.name, "small.pdf")
        document = pymupdf.open()
        for _ in range(3):
            document.new_page(width=300, height=400)
        document.save(path)
        code, _, _, _, err = run_spacing(path)
        self.assertEqual(code, 1)
        self.assertIn("would not line up", err)

    def test_blank_template_is_rejected_and_never_overwrites_an_existing_style(self):
        code, glyph_dir, _, _, err = run_spacing(blank_pdf)  # fixture dir exists, no style yet
        self.assertEqual(code, 1)
        self.assertIn("No handwriting", err)
        self.assertFalse((glyph_dir / config.STYLE_NAME).exists())
        # An earlier good style.json must survive a failed run.
        (glyph_dir / config.STYLE_NAME).write_text('{"keep": "me"}')
        root = glyph_dir.parent.parent
        with mock.patch.object(config, "GLYPHS_DIR", root / "glyphs"), \
             mock.patch.object(config, "PREVIEW_DIR", root / "preview"), \
             contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(spacing.main([blank_pdf, "--user", "tester", "--layout", layout_path]), 1)
        self.assertEqual((glyph_dir / config.STYLE_NAME).read_text(), '{"keep": "me"}')

    def test_user_name_is_restricted(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(spacing.main([blank_pdf, "--user", "../x", "--layout", layout_path]), 1)


class TestRealRemarkableExport(unittest.TestCase):
    @unittest.skipUnless(REAL_PDF.exists(), "samples/remarkable_test.pdf is not on this machine")
    def test_real_sentence_is_analysed(self):
        real_metadata = Path(config.GLYPHS_DIR) / "amaya" / config.GLYPH_METADATA_NAME
        root = Path(tempfile.mkdtemp(dir=_tmp.name))
        glyph_dir = root / "glyphs" / "real"
        glyph_dir.mkdir(parents=True)
        if real_metadata.exists():
            (glyph_dir / config.GLYPH_METADATA_NAME).write_bytes(real_metadata.read_bytes())
        with mock.patch.object(config, "GLYPHS_DIR", root / "glyphs"), \
             mock.patch.object(config, "PREVIEW_DIR", root / "preview"), \
             contextlib.redirect_stdout(io.StringIO()):
            code = spacing.main([str(REAL_PDF), "--user", "real"])
        self.assertEqual(code, 0)
        with open(glyph_dir / config.STYLE_NAME) as f:
            style = json.load(f)

        # The real sample wraps over two lines and has all nine words.
        self.assertEqual(style["sentence"]["text_lines_detected"], 2)
        self.assertEqual(style["sentence"]["words_detected"], 9)
        word, letter, base = style["word_spacing"], style["letter_spacing"], style["baseline_variation"]
        self.assertEqual(word["observations"], 7)
        self.assertEqual(word["quality"], "good")
        self.assertGreater(word["separation_ratio"], config.SPACING_WORD_SEPARATION_RATIO)
        # Sanity ranges: a word gap is wider than a letter gap, and both are a plausible handwriting size.
        self.assertGreater(word["min_pt"], letter["max_pt"])
        self.assertTrue(5 < word["mean_pt"] < 60)
        self.assertTrue(0 < letter["estimate_pt"] < 10)
        self.assertGreaterEqual(letter["observations"], config.SPACING_MIN_LETTER_GAPS)
        self.assertGreaterEqual(base["observations"], config.SPACING_MIN_BASELINE_OBSERVATIONS)
        self.assertTrue(0 < base["robust_std_pt"] < 10)
        self.assertEqual(len(base["per_line"]), 2)
        self.assertTrue((root / "preview" / f"{config.SPACING_PREVIEW_PREFIX}_real.png").exists())


if __name__ == "__main__":
    unittest.main()
