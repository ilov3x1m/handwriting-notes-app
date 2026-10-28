"""Tests for render.py. Run from the project root:

    python -m unittest discover -s tests -v

Most tests build a small synthetic glyph set: solid black rectangles whose size
identifies the character and sample, with known baseline rows. That makes
positions, spacing and baselines exactly predictable. Integration tests at the
end use the real glyphs/amaya/ set and are skipped if it is not on this machine.
"""

import contextlib
import hashlib
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
import extract  # noqa: E402
import render  # noqa: E402

PX = config.EXTRACT_DPI / 72           # 4 px per point
REAL_DIR = Path(config.GLYPHS_DIR) / "amaya"
DESCENDERS = "gjpqy"
LETTER_GAP_PT, WORD_GAP_PT = 2.0, 10.0

_tmp = None


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()


def tearDownModule():
    _tmp.cleanup()


# ---------------------------------------------------------------------------
# Synthetic glyph sets
# ---------------------------------------------------------------------------

def glyph_size(char, sample):
    """Width identifies char case and sample; descender letters hang 16 px below the baseline."""
    width = 24 + 6 * (sample - 1) + (20 if char.isupper() else 0) + (10 if char.isdigit() else 0)
    height = 46 if char in DESCENDERS else 30
    return width, height, 30  # baseline row is 30 for every glyph


def make_glyph_dir(chars="abcdefghijklmnopqrstuvwxyzABC0123456789", case_safe_names=False,
                   letter_gap=LETTER_GAP_PT, word_gap=WORD_GAP_PT, style=True):
    root = Path(tempfile.mkdtemp(dir=_tmp.name))
    records = []
    for index, char in enumerate(chars):
        for sample in (1, 2, 3):
            width, height, baseline = glyph_size(char, sample)
            if case_safe_names:
                name = f"{char}_upper_{sample}.png" if char.isupper() else f"{char}_{sample}.png"
            else:
                name = f"glyph_{index:03d}_{sample}.png"  # nothing here can be guessed from the character
            Image.new("RGBA", (width, height), (0, 0, 0, 255)).save(root / name)
            records.append({"char": char, "sample": sample, "file": name, "baseline_from_top_px": float(baseline),
                            "image_size_px": {"width": width, "height": height}})
    with open(root / config.GLYPH_METADATA_NAME, "w") as f:
        json.dump({"meta": {"pixels_per_point": PX, "trim_padding_px": 4}, "glyphs": records}, f)
    if style:
        write_style(root, letter_gap, word_gap)
    return root


def write_style(root, letter_gap=LETTER_GAP_PT, word_gap=WORD_GAP_PT, **overrides):
    data = {"letter_spacing": {"estimate_pt": letter_gap, "quality": "approximate"},
            "word_spacing": {"mean_pt": word_gap, "quality": "good"}}
    data.update(overrides)
    with open(root / config.STYLE_NAME, "w") as f:
        json.dump(data, f)


@contextlib.contextmanager
def no_jitter():
    with mock.patch.object(config, "ROTATION_MAX_DEG", 0), mock.patch.object(config, "SCALE_JITTER", 0), \
         mock.patch.object(config, "BASELINE_JITTER_PX", 0):
        yield


def lay_out(text, root, seed=1):
    pages, ctx, warnings, normalized = render.render_text(text, root, seed)
    return pages, ctx, warnings


def all_items(pages):
    """Every (page_no, line_no, x_left, item, baseline) in reading order."""
    return [(p, l, x, item, line["baseline"]) for p, page in enumerate(pages)
            for l, line in enumerate(page["lines"]) for x, item in line["items"]]


def rendered_chars(pages):
    return "".join(item["char"] for _, _, _, item, _ in all_items(pages))


def ink_box(x_left, baseline, item):
    """Ink bounding box on the page in pixels: (left, top, right, bottom)."""
    left, top = render.paste_position(x_left, baseline, item)
    return left, top, left + item["image"].width, top + item["image"].height


# ---------------------------------------------------------------------------
# Loading and lookup
# ---------------------------------------------------------------------------

class TestLoading(unittest.TestCase):
    def test_metadata_is_loaded(self):
        root = make_glyph_dir("abg")
        library = render.load_glyph_library(root)
        self.assertEqual(library["pixels_per_point"], PX)
        self.assertEqual(sorted(library["samples"]), ["a", "b", "g"])
        self.assertEqual(len(library["samples"]["g"]), 3)
        glyph = library["samples"]["g"][0]
        self.assertEqual(glyph["baseline_row"], 30.0)
        self.assertEqual(glyph["image"].size, (24, 46))

    def test_files_are_found_through_the_manifest_not_by_guessing_names(self):
        root = make_glyph_dir("ab")  # files are glyph_000_1.png ...
        self.assertFalse((root / "a_1.png").exists())
        library = render.load_glyph_library(root)
        self.assertEqual(library["samples"]["a"][0]["image"].width, 24)

    def test_upper_and_lower_case_stay_distinct_on_a_case_insensitive_filesystem(self):
        root = make_glyph_dir("aA", case_safe_names=True)
        names = [p.name for p in root.glob("*.png")]
        self.assertEqual(len(names), 6)
        self.assertEqual(len({n.lower() for n in names}), 6)  # nothing overwritten
        library = render.load_glyph_library(root)
        self.assertEqual({g["image"].width for g in library["samples"]["a"]}, {24, 30, 36})
        self.assertEqual({g["image"].width for g in library["samples"]["A"]}, {44, 50, 56})
        with no_jitter():
            pages, _, _ = lay_out("Aa", root)
        widths = [item["image"].width for _, _, _, item, _ in all_items(pages)]
        self.assertIn(widths[0], {44, 50, 56})   # the capital
        self.assertIn(widths[1], {24, 30, 36})   # the small letter

    def test_missing_folder_metadata_and_images(self):
        with self.assertRaisesRegex(extract.ExtractError, "Run extract.py"):
            render.load_glyph_library(Path(_tmp.name) / "nobody")
        empty = Path(tempfile.mkdtemp(dir=_tmp.name))
        with self.assertRaisesRegex(extract.ExtractError, "not found"):
            render.load_glyph_library(empty)

        root = make_glyph_dir("a")
        (root / config.GLYPH_METADATA_NAME).write_text("{not json")
        with self.assertRaisesRegex(extract.ExtractError, "not valid JSON"):
            render.load_glyph_library(root)

        root = make_glyph_dir("a")
        (root / config.GLYPH_METADATA_NAME).write_text(json.dumps({"meta": {}, "glyphs": []}))
        with self.assertRaisesRegex(extract.ExtractError, "pixels_per_point"):
            render.load_glyph_library(root)

        root = make_glyph_dir("a")
        next(root.glob("glyph_000_1.png")).unlink()
        with self.assertRaisesRegex(extract.ExtractError, "cannot be read"):
            render.load_glyph_library(root)

        root = make_glyph_dir("a")
        metadata = json.loads((root / config.GLYPH_METADATA_NAME).read_text())
        del metadata["glyphs"][0]["baseline_from_top_px"]
        (root / config.GLYPH_METADATA_NAME).write_text(json.dumps(metadata))
        with self.assertRaisesRegex(extract.ExtractError, "missing required fields"):
            render.load_glyph_library(root)

    def test_letter_without_a_sample_is_an_error_not_a_substitution(self):
        root = make_glyph_dir("ab")
        with self.assertRaisesRegex(extract.ExtractError, "no handwriting sample for: z"):
            render.render_text("abz", root, 1)


# ---------------------------------------------------------------------------
# style.json
# ---------------------------------------------------------------------------

class TestStyle(unittest.TestCase):
    def test_values_are_read(self):
        style = render.load_style(make_glyph_dir("a", letter_gap=2.5, word_gap=12))
        self.assertEqual((style["letter_gap_pt"], style["word_gap_pt"], style["warnings"]), (2.5, 12, []))

    def test_missing_style_file(self):
        with self.assertRaisesRegex(extract.ExtractError, "Run spacing.py"):
            render.load_style(make_glyph_dir("a", style=False))

    def test_invalid_json(self):
        root = make_glyph_dir("a")
        (root / config.STYLE_NAME).write_text("oops")
        with self.assertRaisesRegex(extract.ExtractError, "not valid JSON"):
            render.load_style(root)

    def test_missing_field(self):
        root = make_glyph_dir("a")
        (root / config.STYLE_NAME).write_text(json.dumps({"letter_spacing": {"estimate_pt": 2}}))
        with self.assertRaisesRegex(extract.ExtractError, "word_spacing"):
            render.load_style(root)

    def test_null_or_insufficient_measurement_is_rejected(self):
        root = make_glyph_dir("a")
        write_style(root, word_gap=None)
        with self.assertRaisesRegex(extract.ExtractError, "not a usable measurement"):
            render.load_style(root)
        write_style(root, letter_spacing={"estimate_pt": 2, "quality": "insufficient"})
        with self.assertRaisesRegex(extract.ExtractError, "not a usable measurement"):
            render.load_style(root)

    def test_wild_measurements_are_clamped_with_a_warning(self):
        style = render.load_style(make_glyph_dir("a", letter_gap=40, word_gap=0.1))
        self.assertEqual(style["letter_gap_pt"], config.LETTER_GAP_LIMITS_PT[1])
        self.assertEqual(style["word_gap_pt"], config.WORD_GAP_LIMITS_PT[0])
        self.assertEqual(len(style["warnings"]), 2)


# ---------------------------------------------------------------------------
# Selection and randomness
# ---------------------------------------------------------------------------

class TestRandomness(unittest.TestCase):
    def setUp(self):
        self.root = make_glyph_dir("abg")

    def sample_widths(self, text, seed):
        with no_jitter():
            pages, _, _ = lay_out(text, self.root, seed)
        return [item["image"].width for _, _, _, item, _ in all_items(pages)]

    def test_repeated_letters_use_different_samples(self):
        widths = self.sample_widths("a " * 60, seed=3)
        self.assertEqual(set(widths), {24, 30, 36})                      # all three versions appear
        self.assertTrue(all(a != b for a, b in zip(widths, widths[1:])))  # never the same twice in a row

    def test_same_seed_gives_the_same_layout_and_pdf(self):
        text = "ab ba abg gab " * 5
        first = lay_out(text, self.root, seed=11)[0]
        second = lay_out(text, self.root, seed=11)[0]
        key = lambda pages: [(p, l, round(x, 3), item["sample"], round(item["dy"], 6), round(item["angle"], 6))  # noqa: E731
                             for p, l, x, item, _ in all_items(pages)]
        self.assertEqual(key(first), key(second))
        paths = []
        for n in range(2):
            pages, ctx, _ = lay_out(text, self.root, seed=11)
            path = os.path.join(_tmp.name, f"seed_{n}.pdf")
            render.write_pdf(pages, ctx, path)
            paths.append(path)
        self.assertEqual(Path(paths[0]).read_bytes(), Path(paths[1]).read_bytes())

    def test_different_seeds_give_different_results(self):
        a = self.sample_widths("abg " * 20, seed=1)
        b = self.sample_widths("abg " * 20, seed=2)
        self.assertNotEqual(a, b)

    def test_jitter_stays_inside_the_configured_limits(self):
        pages, _, _ = lay_out("abg " * 40, self.root, seed=5)
        for _, _, _, item, _ in all_items(pages):
            self.assertLessEqual(abs(item["angle"]), config.ROTATION_MAX_DEG)
            self.assertLessEqual(abs(item["dy"]), config.BASELINE_JITTER_PX)
            self.assertLessEqual(abs(item["scale"] - 1), config.SCALE_JITTER + 1e-9)


# ---------------------------------------------------------------------------
# Baselines, descenders, spacing
# ---------------------------------------------------------------------------

class TestGeometry(unittest.TestCase):
    def setUp(self):
        self.root = make_glyph_dir("abgA")

    def test_baseline_row_of_every_glyph_lands_on_the_ruled_line(self):
        with no_jitter():
            pages, _, _ = lay_out("abg Ab gab", self.root)
        for _, _, x, item, baseline in all_items(pages):
            top = render.paste_position(x, baseline, item)[1]
            self.assertAlmostEqual(top + item["baseline_row"], baseline, delta=0.5, msg=item["char"])

    def test_descenders_hang_below_the_baseline_and_other_letters_do_not(self):
        with no_jitter():
            pages, _, _ = lay_out("ag", self.root)
        by_char = {item["char"]: ink_box(x, base, item) + (base,) for _, _, x, item, base in all_items(pages)}
        a_bottom, g_bottom = by_char["a"][3], by_char["g"][3]
        baseline = by_char["a"][4]
        self.assertAlmostEqual(a_bottom, baseline, delta=0.5)        # sits on the line
        self.assertAlmostEqual(g_bottom - baseline, 16, delta=0.5)   # 46 - 30 px below it
        self.assertGreater(g_bottom, a_bottom)                       # not bottom-aligned

    def test_descender_pixels_survive_in_the_page_layer(self):
        with no_jitter():
            pages, ctx, _ = lay_out("g", self.root)
        layer = render.compose_page(pages[0], ctx)
        baseline = round(pages[0]["lines"][0]["baseline"])
        below = layer.getchannel("A").crop((0, baseline + 4, layer.width, baseline + 14))
        self.assertIsNotNone(below.getbbox())  # ink exists 4-14 px under the line

    def test_glyphs_are_scaled_uniformly_never_stretched(self):
        # No rotation, so a solid rectangle must come out as the same rectangle scaled by one factor.
        with mock.patch.object(config, "ROTATION_MAX_DEG", 0), mock.patch.object(config, "BASELINE_JITTER_PX", 0):
            pages, _, _ = lay_out("abg " * 20, self.root, seed=9)
        library = render.load_glyph_library(self.root)
        seen = set()
        for _, _, _, item, _ in all_items(pages):
            original = next(g for g in library["samples"][item["char"]] if g["sample"] == item["sample"])["image"]
            self.assertAlmostEqual(item["image"].width / original.width, item["scale"], delta=1 / original.width)
            self.assertAlmostEqual(item["image"].height / original.height, item["scale"], delta=1 / original.height)
            seen.add(round(item["scale"], 3))
        self.assertGreater(len(seen), 5)  # the scale really does vary from glyph to glyph

    def test_measured_letter_and_word_gaps_are_applied(self):
        with no_jitter():
            pages, ctx, _ = lay_out("aab ba", self.root)
        items = [(x, item) for _, _, x, item, _ in all_items(pages)]
        letter_gap_px, word_gap_px = LETTER_GAP_PT * PX, WORD_GAP_PT * PX
        self.assertEqual(ctx["letter_gap"], letter_gap_px)
        self.assertEqual(ctx["word_gap"], word_gap_px)
        for (x1, i1), (x2, _) in zip(items[:2], items[1:3]):          # a-a and a-b inside "aab"
            self.assertAlmostEqual(x2 - (x1 + i1["image"].width), letter_gap_px, delta=0.01)
        (x_b, i_b), (x_next, _) = items[2], items[3]                  # b | b: word gap
        self.assertAlmostEqual(x_next - (x_b + i_b["image"].width), word_gap_px, delta=0.01)

    def test_a_different_style_changes_the_spacing(self):
        wide = make_glyph_dir("ab", letter_gap=5.0, word_gap=25.0)
        with no_jitter():
            tight = [(x, i) for _, _, x, i, _ in all_items(lay_out("ab ab", self.root)[0])]
            loose = [(x, i) for _, _, x, i, _ in all_items(lay_out("ab ab", wide)[0])]

        def gap(pair):  # ink gap between two neighbouring glyphs
            return pair[1][0] - (pair[0][0] + pair[0][1]["image"].width)

        self.assertAlmostEqual(gap(tight[0:2]), 2.0 * PX, delta=0.01)   # letter gap in "ab"
        self.assertAlmostEqual(gap(loose[0:2]), 5.0 * PX, delta=0.01)
        self.assertAlmostEqual(gap(tight[1:3]), 10.0 * PX, delta=0.01)  # word gap "b" | "a"
        self.assertAlmostEqual(gap(loose[1:3]), 25.0 * PX, delta=0.01)


# ---------------------------------------------------------------------------
# Punctuation and unsupported characters
# ---------------------------------------------------------------------------

class TestPunctuation(unittest.TestCase):
    def setUp(self):
        self.root = make_glyph_dir("abg")

    def test_punctuation_is_rendered_with_the_fallback_font(self):
        pages, _, warnings = lay_out("a.b, g!", self.root)
        items = [item for _, _, _, item, _ in all_items(pages)]
        self.assertEqual(rendered_chars(pages), "a.b,g!")
        self.assertEqual([i["sample"] is None for i in items], [False, True, False, True, False, True])
        self.assertEqual(warnings, [])

    def test_fallback_marks_rest_on_or_hang_from_the_baseline(self):
        with no_jitter():
            pages, _, _ = lay_out("a.,", self.root)
        boxes = {item["char"]: ink_box(x, base, item) + (base,) for _, _, x, item, base in all_items(pages)}
        period_bottom, comma_bottom, baseline = boxes["."][3], boxes[","][3], boxes["."][4]
        self.assertAlmostEqual(period_bottom, baseline, delta=config.FALLBACK_STROKE_PX + 1.5)
        self.assertGreater(comma_bottom, period_bottom)

    def test_all_ascii_punctuation_can_be_rendered(self):
        import string
        pages, _, _ = lay_out("a " + " ".join(string.punctuation), self.root)
        self.assertEqual(rendered_chars(pages), "a" + string.punctuation)

    def test_typographic_characters_become_ascii(self):
        text, warnings = render.normalize_text("“Hi” — it’s…")
        self.assertEqual(text, '"Hi" - it\'s...')
        self.assertEqual(warnings, [])

    def test_other_non_ascii_becomes_a_question_mark_with_a_warning(self):
        pages, _, warnings = lay_out("aéb", self.root)
        self.assertEqual(rendered_chars(pages), "a?b")
        self.assertTrue(any("replaced with '?'" in w for w in warnings))


# ---------------------------------------------------------------------------
# Wrapping, pages, paragraphs
# ---------------------------------------------------------------------------

class TestWrapping(unittest.TestCase):
    def setUp(self):
        self.root = make_glyph_dir("abg")

    def test_text_wraps_at_the_right_margin(self):
        with no_jitter():
            pages, ctx, _ = lay_out("aaaa " * 40, self.root)
        lines = pages[0]["lines"]
        self.assertGreater(len(lines), 2)
        for line in lines:
            for x, item in line["items"]:
                self.assertLessEqual(x + item["image"].width, ctx["right"] + 1e-6)
                self.assertGreaterEqual(x, ctx["left"] - 1e-6)
        # Greedy wrapping: the first word of each later line (4 glyphs) would not have fitted on the line before.
        for previous, line in zip(lines, lines[1:]):
            last_x, last_item = previous["items"][-1]
            first_word = line["items"][:4]
            word_width = first_word[-1][0] + first_word[-1][1]["image"].width - first_word[0][0]
            self.assertGreater(last_x + last_item["image"].width + ctx["word_gap"] + word_width, ctx["right"])

    def test_new_page_when_the_page_is_full(self):
        text = "\n".join(["ab"] * 60)
        pages, ctx, _ = lay_out(text, self.root)
        per_page = len(ctx["baselines"])
        self.assertGreater(len(pages), 1)
        self.assertEqual(len(pages), -(-60 // per_page))
        for page in pages[:-1]:
            self.assertEqual(len(page["lines"]), per_page)
        self.assertEqual(rendered_chars(pages), "ab" * 60)

    def test_paragraph_and_blank_lines_are_preserved(self):
        with no_jitter():
            pages, ctx, _ = lay_out("aa\n\nbb\nab\n\n\nba", self.root)
        line_numbers = {}
        for p, l, x, item, baseline in all_items(pages):
            line_numbers.setdefault(ctx["baselines"].index(baseline), []).append(item["char"])
        # Source lines: aa, blank, bb, ab, blank, blank, ba -> ruled lines 0, 2, 3, 6.
        self.assertEqual({k: "".join(v) for k, v in line_numbers.items()}, {0: "aa", 2: "bb", 3: "ab", 6: "ba"})

    def test_whitespace_only_lines_and_runs_of_spaces(self):
        pages, _, _ = lay_out("a    b\n   \nb", self.root)
        self.assertEqual(rendered_chars(pages), "abb")

    def test_blank_lines_at_the_bottom_of_a_page_do_not_start_a_new_page_by_themselves(self):
        ctx_lines = len(lay_out("a", self.root)[1]["baselines"])
        text = "\n".join(["a"] * ctx_lines) + "\n\n\n"
        pages, _, _ = lay_out(text, self.root)
        self.assertEqual(len(pages), 1)

    def test_a_word_wider_than_the_line_is_broken_not_looped(self):
        with no_jitter():
            pages, ctx, _ = lay_out("b" * 200 + " ab", self.root)
        self.assertEqual(rendered_chars(pages), "b" * 200 + "ab")
        self.assertGreater(len(pages[0]["lines"]), 3)
        for _, _, x, item, _ in all_items(pages):
            self.assertLessEqual(x + item["image"].width, ctx["right"] + 1e-6)

    def test_full_text_is_preserved_for_a_long_input(self):
        text = ("ab ga bg\n\n" * 30) + "gab."
        pages, _, _ = lay_out(text, self.root)
        self.assertEqual(rendered_chars(pages), "".join(text.split()))


class TestNoClippingOrOverlap(unittest.TestCase):
    def check(self, pages, ctx):
        width, height = ctx["page_size"]
        top_limit = config.MARGIN_TOP * PX - config.BASELINE_JITTER_PX
        bottom_limit = (config.PAGE_HEIGHT - config.MARGIN_BOTTOM) * PX
        for page in pages:
            for line in page["lines"]:
                previous_right = None
                for x, item in line["items"]:
                    left, top, right, bottom = ink_box(x, line["baseline"], item)
                    self.assertGreaterEqual(left, config.MARGIN_LEFT * PX - 1)
                    self.assertLessEqual(right, ctx["right"] + 1)
                    self.assertGreaterEqual(top, top_limit)
                    self.assertLessEqual(bottom, bottom_limit)
                    self.assertTrue(0 <= left and right <= width and 0 <= top and bottom <= height)
                    if previous_right is not None:
                        self.assertGreater(left, previous_right - 1e-6)  # no horizontal overlap on a line
                    previous_right = right
            # Lines never collide vertically.
            boxes = [[ink_box(x, line["baseline"], item) for x, item in line["items"]] for line in page["lines"]]
            for upper, lower in zip(boxes, boxes[1:]):
                if upper and lower:
                    self.assertLess(max(b[3] for b in upper), min(b[1] for b in lower))

    def test_synthetic_glyphs_with_maximum_jitter(self):
        root = make_glyph_dir()
        text = "the quick brown fox jumps over the lazy dog 0123456789 ABC\n" * 30
        pages, ctx, _ = lay_out(text, root, seed=4)
        self.check(pages, ctx)

    def test_glyph_just_inside_the_page_edge_stays_clipped_free_after_rotation(self):
        root = make_glyph_dir("a")
        with mock.patch.object(config, "ROTATION_MAX_DEG", 3):
            pages, ctx, _ = lay_out("a" * 60, root, seed=8)
        self.check(pages, ctx)


# ---------------------------------------------------------------------------
# PDF output and command line
# ---------------------------------------------------------------------------

class TestPdfAndCli(unittest.TestCase):
    def setUp(self):
        self.root = make_glyph_dir("abg")

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(config, "GLYPHS_DIR", self.root.parent), \
             mock.patch.object(config, "OUTPUT_DIR", Path(_tmp.name) / "out"), \
             mock.patch.object(config, "PREVIEW_DIR", Path(_tmp.name) / "preview"), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = render.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_pdf_has_the_right_page_count_and_size(self):
        pages, ctx, _ = lay_out("\n".join(["ab"] * 40), self.root)
        path = os.path.join(_tmp.name, "out.pdf")
        render.write_pdf(pages, ctx, path)
        with pymupdf.open(path) as document:
            self.assertEqual(document.page_count, len(pages))
            self.assertGreater(document.page_count, 1)
            for page in document:
                self.assertAlmostEqual(page.rect.width, 447.0, places=2)
                self.assertAlmostEqual(page.rect.height, 596.0, places=2)

    def test_pdf_contains_ruled_lines_and_handwriting_separately(self):
        pages, ctx, _ = lay_out("ab", self.root)
        path = os.path.join(_tmp.name, "layers.pdf")
        render.write_pdf(pages, ctx, path)
        with pymupdf.open(path) as document:
            page = document[0]
            lines = [d for d in page.get_drawings() if d["type"] == "s"]
            self.assertEqual(len(lines), len(ctx["baselines"]))
            self.assertEqual(len(page.get_images()), 1)
            # Rule positions come from the same baselines the glyphs sit on.
            expected_y = sorted(config.PAGE_HEIGHT - b / PX for b in ctx["baselines"])
            drawn_y = sorted(config.PAGE_HEIGHT - d["rect"].y0 for d in lines)
            for e, d in zip(expected_y, drawn_y):
                self.assertAlmostEqual(e, d, delta=0.05)

    def test_command_line_text_output_and_previews(self):
        out = Path(_tmp.name) / "cli" / "result.pdf"
        code, stdout, stderr = self.run_cli("--text", "ab ga.", "--user", self.root.name, "--output", str(out),
                                            "--seed", "3", "--preview")
        self.assertEqual(code, 0, stderr)
        self.assertTrue(out.exists())
        self.assertIn("1 page(s)", stdout)
        self.assertTrue((Path(_tmp.name) / "preview" / "render_result_page1.png").exists())

    def test_default_output_name_is_derived_from_the_input(self):
        source = Path(_tmp.name) / "notes.txt"
        source.write_text("ab ba")
        code, _, stderr = self.run_cli("--input", str(source), "--user", self.root.name)
        self.assertEqual(code, 0, stderr)
        self.assertTrue((Path(_tmp.name) / "out" / f"notes_{self.root.name}.pdf").exists())

    def test_missing_input_file(self):
        code, _, stderr = self.run_cli("--input", os.path.join(_tmp.name, "nope.txt"), "--user", self.root.name)
        self.assertEqual(code, 1)
        self.assertIn("Input file not found", stderr)

    def test_empty_and_whitespace_only_text(self):
        for text in ("", "   \n \n"):
            code, _, stderr = self.run_cli("--text", text, "--user", self.root.name)
            self.assertEqual(code, 1)
            self.assertIn("empty", stderr)

    def test_empty_input_file_and_non_utf8_file(self):
        empty = Path(_tmp.name) / "empty.txt"
        empty.write_text("")
        self.assertEqual(self.run_cli("--input", str(empty), "--user", self.root.name)[0], 1)
        binary = Path(_tmp.name) / "binary.txt"
        binary.write_bytes(b"\xff\xfe\x00bad")
        code, _, stderr = self.run_cli("--input", str(binary), "--user", self.root.name)
        self.assertEqual(code, 1)
        self.assertIn("UTF-8", stderr)

    def test_unknown_user_and_bad_user_name(self):
        code, _, stderr = self.run_cli("--text", "ab", "--user", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("extract.py", stderr)
        self.assertEqual(self.run_cli("--text", "ab", "--user", "../x")[0], 1)

    def test_missing_style_is_reported_through_the_command_line(self):
        (self.root / config.STYLE_NAME).unlink()
        code, _, stderr = self.run_cli("--text", "ab", "--user", self.root.name)
        self.assertEqual(code, 1)
        self.assertIn("spacing.py", stderr)


# ---------------------------------------------------------------------------
# Indentation and list markers (added for slides.py notes text)
# ---------------------------------------------------------------------------

class TestIndentationAndBullets(unittest.TestCase):
    def setUp(self):
        self.root = make_glyph_dir()   # a-z, A-C and 0-9 (the markers "*" and "-" come from the fallback font)

    def layout(self, text):
        with no_jitter(), mock.patch.object(config, "BASELINE_KEEP_FRACTION", 0.0):
            pages, ctx, _ = lay_out(text, self.root)
        return pages[0]["lines"], ctx

    def first_x(self, line):
        return line["items"][0][0]

    def gap(self, line, left, right):
        """Ink gap between item index `left` and item index `right` of a line."""
        (x1, i1), (x2, _) = line["items"][left], line["items"][right]
        return x2 - (x1 + i1["image"].width)

    def test_leading_spaces_indent_a_line(self):
        lines, ctx = self.layout("ab\n   ab\n      ab")
        step = config.RENDER_INDENT_PT_PER_SPACE * PX
        self.assertAlmostEqual(self.first_x(lines[0]), ctx["left"], delta=0.01)
        self.assertAlmostEqual(self.first_x(lines[1]) - ctx["left"], 3 * step, delta=0.01)
        self.assertAlmostEqual(self.first_x(lines[2]) - ctx["left"], 6 * step, delta=0.01)
        self.assertEqual(render.split_indents("ab\n   ab\n\n  "), [0, 3, 0, 2])

    def test_explicit_zero_indents_match_no_indents_exactly(self):
        text = "ab ba cab abc " * 10
        def positions(indents):
            library = render.load_glyph_library(self.root)
            render.normalize_baselines(library)
            ctx = render.build_context(library, render.load_style(self.root), seed=5)
            pages = render.layout_text(render.split_paragraphs(text), ctx, indents)
            return [[round(x, 6) for x, _ in line["items"]] for page in pages for line in page["lines"]]
        self.assertEqual(positions(None), positions([0]))

    def test_layout_text_still_works_without_the_indents_argument(self):
        with no_jitter():
            _, ctx, _ = lay_out("ab", self.root)
            pages = render.layout_text([["a", "b"], [], ["a"]], ctx)
        lines = pages[0]["lines"]
        self.assertEqual([len(line["items"]) for line in lines], [2, 0, 1])   # the blank line is a line

    def test_gap_after_a_bullet_marker_is_the_small_bullet_gap(self):
        lines, ctx = self.layout("* ab\na ab")
        self.assertAlmostEqual(self.gap(lines[0], 0, 1), ctx["bullet_gap"], delta=0.01)   # "*" then "a"
        self.assertLess(ctx["bullet_gap"], ctx["word_gap"])
        self.assertAlmostEqual(self.gap(lines[1], 0, 1), ctx["word_gap"], delta=0.01)    # "a" then "ab": ordinary gap

    def test_wrapped_bullet_lines_hang_under_the_text_not_the_marker(self):
        lines, ctx = self.layout("* " + "aaaa " * 30)
        self.assertGreater(len(lines), 2)
        marker_x, text_x = lines[0]["items"][0][0], lines[0]["items"][1][0]
        for line in lines[1:]:
            self.assertAlmostEqual(self.first_x(line), text_x, delta=0.01)
            self.assertGreater(self.first_x(line), marker_x)

    def test_nested_bullet_with_wrap_keeps_its_own_hanging_indent(self):
        lines, ctx = self.layout("   * " + "aaaa " * 30)
        indent = 3 * config.RENDER_INDENT_PT_PER_SPACE * PX
        self.assertAlmostEqual(self.first_x(lines[0]) - ctx["left"], indent, delta=0.01)
        text_x = lines[0]["items"][1][0]
        for line in lines[1:]:
            self.assertAlmostEqual(self.first_x(line), text_x, delta=0.01)
        for line in lines:
            for x, item in line["items"]:
                self.assertLessEqual(x + item["image"].width, ctx["right"] + 1e-6)

    def test_which_first_words_count_as_list_markers(self):
        for marker in ("*", "-", "1.", "12)", "a.", "b)", "C)"):
            self.assertTrue(render.LIST_MARKER.match(marker), marker)
        for word in ("A.", "ab", "1", "--", "1.5", "e.g.", "(1)"):
            self.assertIsNone(render.LIST_MARKER.match(word), word)

    def test_a_lone_dash_and_a_sentence_starting_with_a_capital_initial_are_not_bullets(self):
        lines, ctx = self.layout("-\nA. ab")
        self.assertEqual(len(lines[0]["items"]), 1)
        # "A." is two glyphs (A and the period); the next word follows after a normal word gap.
        self.assertAlmostEqual(self.gap(lines[1], 1, 2), ctx["word_gap"], delta=0.01)

    def test_numbered_markers_use_the_bullet_gap(self):
        lines, ctx = self.layout("1. ab\n2) ba")
        for line in lines:   # "1" "." then the text: the gap after the whole marker is the bullet gap
            self.assertAlmostEqual(self.gap(line, 1, 2), ctx["bullet_gap"], delta=0.01)

    def test_huge_indentation_is_limited_and_text_is_never_lost(self):
        with no_jitter():
            pages, ctx, _ = lay_out(" " * 500 + "ab ba gab", self.root)
        self.assertEqual(rendered_chars(pages), "abbagab")
        limit = config.RENDER_MAX_INDENT_FRACTION * (ctx["right"] - ctx["left"])
        self.assertAlmostEqual(pages[0]["lines"][0]["items"][0][0] - ctx["left"], limit, delta=0.01)
        for _, _, x, item, _ in all_items(pages):
            self.assertLessEqual(x + item["image"].width, ctx["right"] + 1e-6)


# ---------------------------------------------------------------------------
# Baseline normalization
# ---------------------------------------------------------------------------

def make_custom_glyph_dir(specs):
    """Glyph set with chosen geometry: specs[char] = [(width, height, recorded_baseline_row, pad), ...].

    The glyph is an opaque black rectangle inset by `pad` transparent pixels, like the
    real trimmed PNGs. The recorded baseline is whatever the spec says, even if wrong.
    """
    root = Path(tempfile.mkdtemp(dir=_tmp.name))
    records = []
    for char, samples in specs.items():
        for sample, (width, height, baseline, pad) in enumerate(samples, start=1):
            image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            image.paste(Image.new("RGBA", (width - 2 * pad, height - 2 * pad), (0, 0, 0, 255)), (pad, pad))
            name = f"g{len(records):03d}.png"
            image.save(root / name)
            records.append({"char": char, "sample": sample, "file": name, "baseline_from_top_px": float(baseline),
                            "image_size_px": {"width": width, "height": height}})
    with open(root / config.GLYPH_METADATA_NAME, "w") as f:
        json.dump({"meta": {"pixels_per_point": PX, "trim_padding_px": 4}, "glyphs": records}, f)
    write_style(root)
    return root


def landing_px(char, root, keep=0.0, text=None, **patches):
    """How far BELOW the ruled line (px) a glyph's ink bottom lands, with jitter off."""
    with no_jitter(), mock.patch.object(config, "BASELINE_KEEP_FRACTION", keep):
        pages, _, _ = lay_out(text or char, root)
    bottoms = [ink_box(x, base, item)[3] - base for _, _, x, item, base in all_items(pages) if item["char"] == char]
    return bottoms


# Recorded guide is deliberately wrong: ink bottoms are 40 px (10 pt) below, 24 px above, etc.
WRONG_GUIDE = {
    "a": [(30, 30, 30, 0)] * 3,                 # control: ink bottom exactly on the guide
    "x": [(30, 30, 20, 0)] * 3,                 # ink ends 10 px BELOW the guide (the "x sits too low" case)
    "Z": [(30, 30, 36, 0)] * 3,                 # ink ends 6 px ABOVE the guide (floating)
    "0": [(30, 30, 24, 0)] * 3,                 # digit, 6 px low
    "p": [(30, 50, 30, 0)] * 3,                 # stem hangs 20 px (5 pt): a visible descender
    "g": [(30, 40, 40, 0)] * 3,                 # drawn entirely above the guide: no hang recorded
    "y": [(30, 36, 38, 0)] * 3,                 # 2 px above the guide
    "j": [(30, 16, 16, 0)] * 3,                 # tiny glyph: learned depth must be capped by its height
    "d": [(30, 30, 30, 4)] * 3,                 # transparent padding all round, like a real trimmed PNG
}


class TestBaselineNormalization(unittest.TestCase):
    def setUp(self):
        self.root = make_custom_glyph_dir(WRONG_GUIDE)

    def test_ordinary_letters_digits_and_capitals_rest_on_the_line(self):
        for char in "axZ0":
            landings = landing_px(char, self.root, text=f"{char}{char}{char}")
            self.assertEqual(len(landings), 3)
            for value in landings:
                self.assertAlmostEqual(value, 0, delta=0.6, msg=f"{char} should sit on the line")

    def test_without_normalization_the_recorded_guide_is_used_as_before(self):
        with mock.patch.object(config, "BASELINE_NORMALIZE", False):
            x = landing_px("x", self.root)[0]
            z = landing_px("Z", self.root)[0]
        self.assertAlmostEqual(x, 10, delta=0.6)   # 10 px below the line, as recorded
        self.assertAlmostEqual(z, -6, delta=0.6)   # 6 px above it

    def test_a_little_of_each_samples_own_offset_is_kept(self):
        landings = landing_px("x", self.root, keep=0.2)
        self.assertAlmostEqual(landings[0], 0.2 * 10, delta=0.6)
        self.assertAlmostEqual(landing_px("Z", self.root, keep=0.2)[0], 0.2 * -6, delta=0.6)

    def test_padding_around_the_ink_does_not_move_the_baseline(self):
        # d has 4 px of transparent padding; alignment must use the visible ink, not the PNG edge.
        self.assertAlmostEqual(landing_px("d", self.root)[0], 0, delta=0.6)

    def test_descenders_extend_below_the_line_by_the_depth_the_data_shows(self):
        # p hangs 20 px in the recorded data; g and y do not, but they get the same learned depth,
        # limited to half of the glyph's own height (g is 40 px tall -> 20, y is 36 px tall -> 18).
        for char, expected in (("p", 20), ("g", 20), ("y", 18)):
            self.assertAlmostEqual(landing_px(char, self.root)[0], expected, delta=0.6, msg=char)
        self.assertGreater(landing_px("g", self.root)[0], landing_px("a", self.root)[0] + 10)  # not bottom-aligned

    def test_descender_depth_comes_from_the_samples_not_a_constant(self):
        shallow = make_custom_glyph_dir({**WRONG_GUIDE, "p": [(30, 42, 30, 0)] * 3})  # hangs only 12 px
        self.assertAlmostEqual(landing_px("g", shallow)[0], 12, delta=0.6)

    def test_learned_depth_is_capped_by_the_glyph_height(self):
        self.assertAlmostEqual(landing_px("j", self.root)[0], 0.5 * 16, delta=0.6)  # not 20 px

    def test_no_hanging_descender_sample_is_reported_not_guessed(self):
        root = make_custom_glyph_dir({"a": [(30, 30, 30, 0)] * 3, "g": [(30, 40, 40, 0)] * 3})
        library = render.load_glyph_library(root)
        warnings = render.normalize_baselines(library)
        self.assertTrue(any("descender depth is unknown" in w for w in warnings))
        glyph = library["samples"]["g"][0]
        self.assertEqual(glyph["baseline_row"], glyph["recorded_baseline_row"])  # recorded guide kept

    def test_normalization_never_changes_pixels_or_the_recorded_baseline(self):
        library = render.load_glyph_library(self.root)
        before = {(c, g["sample"]): (g["image"].tobytes(), g["baseline_row"]) for c, grp in library["samples"].items() for g in grp}
        render.normalize_baselines(library)
        for char, group in library["samples"].items():
            for glyph in group:
                pixels, recorded = before[(char, glyph["sample"])]
                self.assertEqual(glyph["image"].tobytes(), pixels)
                self.assertEqual(glyph["recorded_baseline_row"], recorded)

    def test_proportions_are_preserved_after_correction(self):
        with mock.patch.object(config, "ROTATION_MAX_DEG", 0), mock.patch.object(config, "BASELINE_JITTER_PX", 0):
            pages, _, _ = lay_out("xZ0apgy " * 8, self.root, seed=2)
        for _, _, _, item, _ in all_items(pages):
            self.assertAlmostEqual(item["image"].width / 30, item["scale"], delta=1 / 30)
            expected_height = {"x": 30, "Z": 30, "0": 30, "a": 30, "p": 50, "g": 40, "y": 36}[item["char"]]
            self.assertAlmostEqual(item["image"].height / expected_height, item["scale"], delta=1 / 16)

    def test_rotation_and_scaling_do_not_clip_or_collide_after_correction(self):
        pages, ctx, _ = lay_out("xZ0apgyj dxZ " * 40, self.root, seed=6)
        TestNoClippingOrOverlap().check(pages, ctx)

    def test_rotation_is_about_the_corrected_baseline(self):
        # A glyph tilted by the maximum angle must still have its baseline row on the line:
        # the anchor row is where the ink bottom was pinned, and it is the rotation pivot.
        with mock.patch.object(config, "SCALE_JITTER", 0), mock.patch.object(config, "BASELINE_JITTER_PX", 0), \
             mock.patch.object(config, "BASELINE_KEEP_FRACTION", 0.0):
            pages, _, _ = lay_out("x" * 30, self.root, seed=3)
        tilted = [i for _, _, _, i, _ in all_items(pages) if abs(i["angle"]) > 1.5]
        self.assertTrue(tilted)
        for _, _, x, item, base in all_items(pages):
            self.assertAlmostEqual(render.paste_position(x, base, item)[1] + item["baseline_row"], base, delta=0.6)

    def test_seeded_rendering_is_reproducible_with_normalization(self):
        paths = []
        for n in range(2):
            pages, ctx, _ = lay_out("xZ0apgy " * 20, self.root, seed=13)
            path = os.path.join(_tmp.name, f"norm_{n}.pdf")
            render.write_pdf(pages, ctx, path)
            paths.append(path)
        self.assertEqual(Path(paths[0]).read_bytes(), Path(paths[1]).read_bytes())

    def test_ink_lands_on_the_ruled_line_in_the_finished_pdf(self):
        """End to end: pixels/points conversion, layer and vector rules agree in the PDF itself."""
        with no_jitter(), mock.patch.object(config, "BASELINE_KEEP_FRACTION", 0.0):
            pages, ctx, _ = lay_out("x", self.root)
        path = os.path.join(_tmp.name, "landing.pdf")
        render.write_pdf(pages, ctx, path)
        with pymupdf.open(path) as document:
            page = document[0]
            rule_y = sorted(d["rect"].y0 for d in page.get_drawings() if d["type"] == "s")[0]  # first ruled line, top-left y
            pixmap = page.get_pixmap(matrix=pymupdf.Matrix(4, 4), alpha=False)
            gray = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert("L")
        ink_rows = [y for y in range(gray.height) if any(gray.getpixel((x, y)) < 60 for x in range(0, gray.width, 2))]
        self.assertAlmostEqual(max(ink_rows) / 4, rule_y, delta=0.5)  # ink bottom within half a point of the line


# ---------------------------------------------------------------------------
# Real dataset
# ---------------------------------------------------------------------------

def directory_fingerprint(path):
    digest = hashlib.sha256()
    for file in sorted(Path(path).iterdir()):
        digest.update(file.name.encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()


@unittest.skipUnless((REAL_DIR / config.GLYPH_METADATA_NAME).exists() and (REAL_DIR / config.STYLE_NAME).exists(),
                     "glyphs/amaya/ is not on this machine")
class TestRealDataset(unittest.TestCase):
    TEXT = ("Hello world. This is my handwriting.\n\n"
            "Today I jumped over 3 lazy dogs, quickly picked 12 big peppers (7:45 pm)! "
            "\"Practice makes perfect,\" my teacher said. Don't forget: PACK MY BOX WITH FIVE DOZEN JUGS - it's 100% urgent; "
            "call 555-0199 & bring Xavier to Room 8. " * 3)

    def test_all_62_characters_have_samples(self):
        library = render.load_glyph_library(REAL_DIR)
        expected = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")
        self.assertEqual(set(library["samples"]), expected)
        self.assertTrue(all(len(v) == 3 for v in library["samples"].values()))

    def test_real_render_end_to_end_and_glyph_files_untouched(self):
        before = directory_fingerprint(REAL_DIR)
        pages, ctx, warnings, text = render.render_text(self.TEXT, REAL_DIR, seed=21)
        path = os.path.join(_tmp.name, "real.pdf")
        render.write_pdf(pages, ctx, path)

        self.assertEqual(rendered_chars(pages), "".join(text.split()))                      # nothing lost
        self.assertGreater(len(pages), 1)                                                   # long enough to paginate
        with pymupdf.open(path) as document:
            self.assertEqual(document.page_count, len(pages))
            self.assertTrue(all(abs(p.rect.width - 447) < 0.01 and abs(p.rect.height - 596) < 0.01 for p in document))
        TestNoClippingOrOverlap().check(pages, ctx)
        self.assertEqual(directory_fingerprint(REAL_DIR), before)                            # inputs unchanged
        self.assertFalse(any("exceed" in w for w in warnings), warnings)

        used = {}
        for _, _, _, item, _ in all_items(pages):
            if item["sample"] is not None:
                used.setdefault(item["char"], set()).add(item["sample"])
        for char in "eoa":                                                                   # frequent letters use all versions
            self.assertEqual(len(used[char]), 3, char)

    def test_real_spacing_comes_from_style_json(self):
        style = json.loads((REAL_DIR / config.STYLE_NAME).read_text())
        loaded = render.load_style(REAL_DIR)
        self.assertEqual(loaded["letter_gap_pt"], style["letter_spacing"]["estimate_pt"])
        lo, hi = config.WORD_GAP_LIMITS_PT
        self.assertEqual(loaded["word_gap_pt"], min(max(style["word_spacing"]["mean_pt"], lo), hi))

    def test_real_descender_p_hangs_below_the_baseline(self):
        with no_jitter():
            pages, _, _ = render.render_text("op", REAL_DIR, seed=1)[:3]
        boxes = {item["char"]: ink_box(x, base, item) + (base,) for _, _, x, item, base in all_items(pages)}
        self.assertGreater(boxes["p"][3] - boxes["p"][4], 2 * PX)  # p hangs at least 2 pt below the line

    def test_real_normalization_reduces_the_scatter_and_makes_descenders_hang(self):
        def landings(normalize):
            with mock.patch.object(config, "BASELINE_NORMALIZE", normalize), no_jitter():
                library = render.load_glyph_library(REAL_DIR)
                render.normalize_baselines(library)
                return {c: [(g["hang_px"] if not normalize else g["landing_px"]) / PX for g in grp]
                        for c, grp in library["samples"].items()}
        before, after = landings(False), landings(True)
        ordinary = [c for c in before if c not in config.DESCENDER_CHARS]
        spread = lambda d: (sum(v * v for c in ordinary for v in d[c]) / sum(len(d[c]) for c in ordinary)) ** 0.5  # noqa: E731
        self.assertGreater(spread(before), 2.0)                       # the scatter the review reported
        self.assertLess(spread(after), 0.75)                          # now well under a point
        self.assertLess(max(abs(v) for c in ordinary for v in after[c]), 1.5)
        for char in config.DESCENDER_CHARS:                           # g, j, y used to sit on the line
            self.assertTrue(all(v >= config.DESCENDER_MIN_HANG_PT for v in after[char]), char)
        for char in "xZ0":                                            # the reported offenders
            self.assertTrue(all(abs(v) < 1.5 for v in after[char]), char)

    def test_real_glyph_files_and_metadata_are_untouched_by_normalization_and_rendering(self):
        before = directory_fingerprint(REAL_DIR)
        library = render.load_glyph_library(REAL_DIR)
        render.normalize_baselines(library)
        render.render_text("xZ0 gjpqy Hello", REAL_DIR, seed=1)
        self.assertEqual(directory_fingerprint(REAL_DIR), before)
        metadata = json.loads((REAL_DIR / config.GLYPH_METADATA_NAME).read_text())
        recorded = {(g["char"], g["sample"]): g["baseline_from_top_px"] for g in metadata["glyphs"]}
        for char, group in library["samples"].items():
            for glyph in group:
                self.assertEqual(glyph["recorded_baseline_row"], recorded[(char, glyph["sample"])])

    def test_real_seed_reproducibility(self):
        paths = []
        for n in range(2):
            pages, ctx, _, _ = render.render_text(self.TEXT, REAL_DIR, seed=5)
            path = os.path.join(_tmp.name, f"real_seed_{n}.pdf")
            render.write_pdf(pages, ctx, path)
            paths.append(path)
        self.assertEqual(Path(paths[0]).read_bytes(), Path(paths[1]).read_bytes())


if __name__ == "__main__":
    unittest.main()
