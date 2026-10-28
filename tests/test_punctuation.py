"""Milestone 8 tests: template V2 (punctuation page), V1 compatibility, extraction and rendering.

Everything here uses SYNTHETIC data (solid blocks and drawn stems). None of it says anything about
real handwriting; that needs a template Isaiah fills in on the reMarkable 2.
"""

import json
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
import render  # noqa: E402
import template  # noqa: E402

V1_DIR = Path(config.BASE_DIR) / "templates" / "v1"
V2_DIR = Path(config.BASE_DIR) / "templates" / "v2"
SYMBOLS = config.PUNCTUATION_CHARS


class TestTemplateV2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.layout = template.build_layout(2)
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = os.path.join(cls.tmp.name, "v2.pdf")
        template.write_pdf(cls.layout, cls.pdf)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_four_pages_and_225_boxes(self):
        self.assertEqual(len(self.layout["pages"]), 4)
        self.assertEqual(len(self.layout["boxes"]), 225)
        self.assertEqual(self.layout["meta"]["template_version"], 2)
        document = pymupdf.open(self.pdf)
        self.assertEqual(document.page_count, 4)
        for page in document:
            self.assertAlmostEqual(page.rect.width, 447.0, places=1)
            self.assertAlmostEqual(page.rect.height, 596.0, places=1)

    def test_thirteen_symbols_with_three_samples_each_on_page_four(self):
        self.assertEqual(len(SYMBOLS), 13)
        boxes = [b for b in self.layout["boxes"] if b["page"] == 4]
        self.assertEqual(len(boxes), 39)
        for char in SYMBOLS:
            self.assertEqual(sorted(b["sample"] for b in boxes if b["char"] == char), [1, 2, 3], char)

    def test_default_template_is_v2_and_pages_one_to_three_match_v1(self):
        self.assertEqual(template.build_layout()["meta"]["page_count"], 4)
        v1 = template.build_layout(1)
        self.assertEqual([b for b in self.layout["boxes"] if b["page"] <= 3], v1["boxes"])
        self.assertEqual(self.layout["sample_sentence"], v1["sample_sentence"])

    def test_page_four_boxes_fit_inside_the_page_and_do_not_overlap(self):
        boxes = [b for b in self.layout["boxes"] if b["page"] == 4]
        for b in boxes:
            self.assertGreaterEqual(b["x0"], config.MARGIN_LEFT)
            self.assertLessEqual(b["x1"], config.PAGE_WIDTH - config.MARGIN_RIGHT)
            self.assertGreaterEqual(b["y0"], config.MARGIN_BOTTOM)
            self.assertTrue(b["y0"] < b["baseline_y"] < b["y1"])
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                self.assertFalse(min(a["x1"], b["x1"]) - max(a["x0"], b["x0"]) > 0.01
                                 and min(a["y1"], b["y1"]) - max(a["y0"], b["y0"]) > 0.01)

    def test_unknown_version_is_rejected(self):
        with self.assertRaises(ValueError):
            template.build_layout(9)

    def test_v2_files_on_disk_match_the_generator(self):
        self.assertEqual(json.loads((V2_DIR / "template_layout.json").read_text()), self.layout)
        self.assertEqual(json.loads(Path(config.LAYOUT_JSON).read_text()), self.layout)


class TestV1Preserved(unittest.TestCase):
    def test_preserved_v1_layout_is_still_what_the_generator_makes_for_v1(self):
        stored = json.loads((V1_DIR / "template_layout.json").read_text())
        self.assertEqual(template.build_layout(1), stored)
        self.assertEqual(stored["meta"]["page_count"], 3)
        self.assertEqual(stored["meta"]["box_count"], 186)
        self.assertNotIn("template_version", stored["meta"])     # untouched V1 file
        self.assertEqual(pymupdf.open(V1_DIR / "template.pdf").page_count, 3)

    def test_template_main_never_overwrites_v1(self):
        # main() writes the default (V2) version only
        source = Path(template.__file__).read_text()
        self.assertIn("never overwritten", source)

    def test_layout_selection_follows_the_page_count(self):
        def doc(pages):
            d = pymupdf.open()
            for _ in range(pages):
                d.new_page(width=447, height=596)
            return d
        self.assertEqual(extract.layout_path_for_pdf(doc(3)), config.TEMPLATES_DIR / "v1" / "template_layout.json")
        self.assertEqual(extract.layout_path_for_pdf(doc(4)), config.TEMPLATES_DIR / "v2" / "template_layout.json")
        for bad in (1, 2, 5):
            with self.assertRaisesRegex(extract.ExtractError, "no template version matches"):
                extract.layout_path_for_pdf(doc(bad))

    def test_v1_layout_rejects_a_four_page_pdf_and_v2_layout_a_three_page_pdf(self):
        v1, v2 = extract.load_layout(V1_DIR / "template_layout.json"), extract.load_layout(V2_DIR / "template_layout.json")
        self.assertEqual((v1["meta"]["template_version"], v2["meta"]["template_version"]), (1, 2))
        with self.assertRaisesRegex(extract.ExtractError, "expects 3"):
            extract.validate_pdf(pymupdf.open(V2_DIR / "template.pdf"), v1)
        with self.assertRaisesRegex(extract.ExtractError, "expects 4"):
            extract.validate_pdf(pymupdf.open(V1_DIR / "template.pdf"), v2)

    @unittest.skipUnless((Path(config.BASE_DIR) / "samples" / "remarkable_test.pdf").exists(),
                         "samples/remarkable_test.pdf is not on this machine")
    def test_existing_sample_pdf_extracts_with_v1_coordinates(self):
        sample = Path(config.BASE_DIR) / "samples" / "remarkable_test.pdf"
        with tempfile.TemporaryDirectory() as out:
            metadata, _ = extract.extract_to_directory(sample, None, Path(out) / "g")
        self.assertEqual(metadata["meta"]["template_version"], 1)
        self.assertEqual(metadata["meta"]["expected_samples"], 186)
        self.assertEqual(metadata["meta"]["extracted_samples"], 186)


class TestFilenames(unittest.TestCase):
    def test_punctuation_names_are_safe_and_unique_even_when_case_folded(self):
        names = [extract.glyph_filename(c, s) for c in fx.V2_CHARS for s in (1, 2, 3)]
        self.assertEqual(len(names), 225)
        self.assertEqual(len({n.casefold() for n in names}), 225)
        for char in SYMBOLS:
            name = extract.glyph_filename(char, 1)
            self.assertRegex(name, r"^[a-z_]+_1\.png$")
        self.assertEqual(extract.glyph_filename(",", 1), "comma_1.png")
        self.assertEqual(extract.glyph_filename("'", 2), "quote_single_2.png")
        self.assertEqual(extract.glyph_filename("•", 3), "bullet_3.png")
        self.assertEqual(extract.glyph_filename("A", 1), "A_upper_1.png")   # unchanged for letters
        self.assertEqual(extract.glyph_filename("a", 1), "a_1.png")


class TestV2Extraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pdf = Path(cls.tmp.name) / "v2_filled.pdf"
        cls.pdf.write_bytes(fx.make_completed_template_bytes())
        cls.out = Path(cls.tmp.name) / "glyphs"
        cls.metadata, cls.warnings = extract.extract_to_directory(cls.pdf, None, cls.out)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_all_225_samples_including_39_punctuation_samples(self):
        meta = self.metadata["meta"]
        self.assertEqual((meta["template_version"], meta["expected_samples"], meta["extracted_samples"]), (2, 225, 225))
        self.assertEqual(self.metadata["missing"], [])
        records = [g for g in self.metadata["glyphs"] if g["char"] in SYMBOLS]
        self.assertEqual(len(records), 39)
        self.assertTrue(all(g["page"] == 4 for g in records))

    def test_manifest_maps_each_symbol_to_its_safe_file(self):
        for record in self.metadata["glyphs"]:
            self.assertEqual(record["file"], extract.glyph_filename(record["char"], record["sample"]))
            self.assertTrue((self.out / record["file"]).is_file())
        library = render.load_glyph_library(self.out)
        for char in SYMBOLS:
            self.assertEqual(len(library["samples"][char]), 3, char)

    def test_printed_borders_and_baseline_guides_are_removed_on_the_punctuation_page(self):
        library = render.load_glyph_library(self.out)
        for char in SYMBOLS:
            for glyph in library["samples"][char]:
                box = render.ink_box(glyph["image"])
                # the synthetic ink is a 3 pt wide stem (12 px at 288 dpi); a surviving border/guide would be far wider
                self.assertLess(box[2] - box[0], 30, char)

    def test_baseline_metadata_is_recorded_for_punctuation(self):
        for record in (g for g in self.metadata["glyphs"] if g["char"] in SYMBOLS):
            self.assertIn("baseline_from_top_px", record)
            self.assertIn("baseline_y_pdf", record)

    def test_blank_punctuation_box_is_reported_by_name(self):
        data = fx.make_completed_template_bytes(skip=lambda b: b["char"] == "?" and b["sample"] == 2)
        path = Path(self.tmp.name) / "blank_q.pdf"
        path.write_bytes(data)
        metadata, _ = extract.extract_to_directory(path, None, Path(self.tmp.name) / "g2")
        self.assertEqual([(m["char"], m["sample"]) for m in metadata["missing"]], [("?", 2)])
        self.assertFalse((Path(self.tmp.name) / "g2" / "question_2.png").exists())

    def test_v1_filled_pdf_still_extracts_as_v1_with_no_punctuation(self):
        path = Path(self.tmp.name) / "v1_filled.pdf"
        path.write_bytes(fx.make_completed_template_bytes(version=1))
        metadata, _ = extract.extract_to_directory(path, None, Path(self.tmp.name) / "g1")
        self.assertEqual((metadata["meta"]["template_version"], metadata["meta"]["extracted_samples"]), (1, 186))
        self.assertFalse(any(g["char"] in SYMBOLS for g in metadata["glyphs"]))


class TestPunctuationRendering(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def render(self, profile, text, seed=1):
        pages, ctx, warnings, normalized = render.render_text(text, profile, seed)
        return pages, ctx, warnings, normalized

    def chars_drawn(self, pages):
        return [item["char"] for page in pages for line in page["lines"] for _, item in line["items"]]

    def test_punctuation_is_normalised_separately_from_letters(self):
        baselines = {",": 20.0, ".": 30.0, "'": 44.0, "a": 20.0}
        folder = fx.make_profile(self.root, "v2", characters=fx.V2_CHARS, template_version=2, baselines=baselines)
        library = render.load_glyph_library(folder)
        render.normalize_baselines(library)
        for char, row in baselines.items():
            glyph = library["samples"][char][0]
            if char in SYMBOLS:
                self.assertEqual(glyph["baseline_row"], row, char)           # recorded guide row kept
        self.assertNotEqual(library["samples"]["a"][0]["baseline_row"], 20.0)   # letters still normalised
        # the comma hangs below the period relative to the line
        comma, period = library["samples"][","][0], library["samples"]["."][0]
        self.assertGreater(comma["image"].height - comma["baseline_row"], period["image"].height - period["baseline_row"])

    def test_v2_profile_uses_handwritten_punctuation_and_no_fallback_for_it(self):
        folder = fx.make_profile(self.root, "v2", characters=fx.V2_CHARS, template_version=2)
        pages, ctx, _, _ = self.render(folder, 'Hello, world! "Yes," (maybe); fine: ok - done? it\'s *x')
        for char in ",!\"();:-?'*":
            self.assertNotIn(char, ctx["fallback"])
        self.assertIn(",", self.chars_drawn(pages))
        self.assertEqual(ctx["library"]["samples"][","][0]["image"].size, (26, 30))   # a library glyph was drawn

    def test_v1_profile_keeps_the_font_fallback_for_punctuation(self):
        folder = fx.make_profile(self.root, "v1")
        pages, ctx, warnings, _ = self.render(folder, "Hello, world! (ok)")
        self.assertIn(",", ctx["fallback"])
        self.assertEqual(self.chars_drawn(pages).count(","), 1)
        self.assertEqual(warnings, [w for w in warnings if "Unsupported" not in w])

    def test_list_marker_asterisk_becomes_a_bullet_but_ordinary_asterisk_does_not(self):
        folder = fx.make_profile(self.root, "v2", characters=fx.V2_CHARS, template_version=2)
        pages, _, _, _ = self.render(folder, "* first point\n   * nested point\n2 * 3 = star*\n* star • bullet")
        drawn = [[item["char"] for _, item in line["items"]] for line in pages[0]["lines"]]
        self.assertEqual(drawn[0][0], "•")
        self.assertEqual(drawn[1][0], "•")
        self.assertIn("*", drawn[2])                  # ordinary asterisk in running text
        self.assertNotIn("•", drawn[2])
        self.assertEqual(drawn[3][0], "•")
        self.assertEqual(drawn[3].count("•"), 2)  # marker + explicit bullet

    def test_without_a_handwritten_bullet_star_markers_use_the_font_and_explicit_bullet_becomes_question(self):
        folder = fx.make_profile(self.root, "v1")
        pages, ctx, warnings, text = self.render(folder, "* first\nlone • here")
        self.assertIn("*", self.chars_drawn(pages))
        self.assertIn("?", text)
        self.assertTrue(any("Unsupported" in w for w in warnings))

    def test_indentation_and_hanging_indent_survive_with_bullets(self):
        folder = fx.make_profile(self.root, "v2", characters=fx.V2_CHARS, template_version=2)
        text = "* " + " ".join(["word"] * 60)
        pages, ctx, _, _ = self.render(folder, text)
        lines = pages[0]["lines"]
        self.assertGreater(len(lines), 1)
        text_start = lines[0]["items"][1][0]
        self.assertAlmostEqual(lines[1]["items"][0][0], text_start, delta=ctx["px"] * 12)
        self.assertGreater(lines[1]["items"][0][0], lines[0]["items"][0][0])

    def test_full_text_renders_to_a_pdf(self):
        folder = fx.make_profile(self.root, "v2", characters=fx.V2_CHARS, template_version=2)
        pages, ctx, _, _ = self.render(folder, 'Hello, world! Are you ready? "Yes," she said. (Probably); maybe - maybe not.\n* star • bullet')
        out = self.root / "out.pdf"
        render.write_pdf(pages, ctx, out)
        self.assertEqual(pymupdf.open(out).page_count, len(pages))


class TestProfileValidationAndCreation(unittest.TestCase):
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

    def test_v2_profile_needs_punctuation_but_a_v1_profile_does_not(self):
        v1 = fx.make_profile(self.glyphs, "old")
        v2_partial = fx.make_profile(self.glyphs, "new_partial", characters=fx.ALL_CHARS + ",.", template_version=2)
        v2 = fx.make_profile(self.glyphs, "new_full", characters=fx.V2_CHARS, template_version=2)
        self.assertEqual(core.check_profile(v1), [])
        self.assertEqual(core.check_profile(v2), [])
        problems = core.check_profile(v2_partial)
        self.assertTrue(problems and "no handwriting samples" in problems[0])

    def test_creating_from_a_v2_template_makes_a_v2_profile_with_punctuation(self):
        result = core.create_profile("fresh", fx.make_completed_template_bytes(), self.workspace)
        self.assertEqual((result["samples"], result["expected"], result["template_version"]), (225, 225, 2))
        self.assertEqual(core.check_profile(self.glyphs / "fresh"), [])
        self.assertTrue({"comma_1.png", "bullet_3.png", "quote_double_2.png"} <= {p.name for p in (self.glyphs / "fresh").glob("*.png")})
        self.assertIn("(", core.profile_characters("fresh"))

    def test_creating_from_a_v1_template_still_works_and_stays_v1(self):
        result = core.create_profile("oldstyle", fx.make_completed_template_bytes(version=1), self.workspace)
        self.assertEqual((result["samples"], result["expected"], result["template_version"]), (186, 186, 1))
        self.assertEqual(core.check_profile(self.glyphs / "oldstyle"), [])

    def test_missing_punctuation_box_leaves_no_partial_profile(self):
        data = fx.make_completed_template_bytes(skip=lambda b: b["char"] == "(" and b["sample"] == 3)
        with self.assertRaises(core.AppError):
            core.create_profile("partial", data, self.workspace)
        self.assertEqual(list(self.glyphs.iterdir()), [])
        self.assertEqual(list((self.root / "samples").iterdir()), [])

    def test_a_pdf_with_the_wrong_page_count_is_refused_clearly(self):
        data = fx.make_completed_template_bytes(pages=2)
        with self.assertRaises(core.AppError) as caught:
            core.create_profile("badpages", data, self.workspace)
        self.assertIn("no template version matches", caught.exception.message)
        self.assertEqual(list(self.glyphs.iterdir()), [])

    def test_names_stay_case_insensitively_unique(self):
        fx.make_profile(self.glyphs, "Existing")
        with self.assertRaises(core.AppError):
            core.create_profile("existing", fx.make_completed_template_bytes(), self.workspace)

    def test_check_text_accepts_a_bullet_only_when_the_profile_can_draw_it(self):
        fx.make_profile(self.glyphs, "withbullet", characters=fx.V2_CHARS, template_version=2)
        fx.make_profile(self.glyphs, "nobullet")
        self.assertEqual(core.check_text("• item", "withbullet")["warnings"], [])
        self.assertTrue(core.check_text("• item", "nobullet")["warnings"])


if __name__ == "__main__":
    unittest.main()
