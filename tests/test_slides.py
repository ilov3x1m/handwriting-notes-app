"""Tests for slides.py / slides_pdf.py. Run from the project root:

    python -m unittest discover -s tests -v

Fixtures are built on the fly: PowerPoint files with python-pptx and PDFs with PyMuPDF,
so the expected structure is known exactly. Integration tests at the end use the real
deck in slides_in/ and the real glyphs/amaya/ set and are skipped if either is missing.
"""

import contextlib
import hashlib
import io
import json
import os
import string
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

import pymupdf
from PIL import Image
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.enum.chart import XL_CHART_TYPE
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import extract  # noqa: E402
import render  # noqa: E402
import slides  # noqa: E402
import slides_pdf  # noqa: E402

REAL_DECK = next(Path(config.BASE_DIR, "slides_in").glob("*.pptx"), None) if Path(config.BASE_DIR, "slides_in").exists() else None
REAL_GLYPHS = Path(config.GLYPHS_DIR) / "amaya"

_tmp = None


def setUpModule():
    global _tmp
    _tmp = tempfile.TemporaryDirectory()


def tearDownModule():
    _tmp.cleanup()


def tmp_path(name):
    return os.path.join(_tmp.name, name)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# PowerPoint fixtures
# ---------------------------------------------------------------------------

def set_bullet(paragraph, tag, **attributes):
    """Give a paragraph an explicit bullet setting: a:buNone, a:buChar or a:buAutoNum."""
    element = OxmlElement(f"a:{tag}")
    for key, value in attributes.items():
        element.set(key, value)
    paragraph._p.get_or_add_pPr().append(element)


def add_textbox(slide, left, top, texts, width=3.5, height=1.0):
    box = slide.shapes.add_textbox(Inches(left), Inches(top), Inches(width), Inches(height))
    frame = box.text_frame
    frame.text = texts[0]
    for text in texts[1:]:
        frame.add_paragraph().text = text
    return box


def build_pptx(name="deck.pptx"):
    """A deck exercising titles, subtitles, bullets, nesting, boxes, numbering, groups, tables, duplicates."""
    prs = Presentation()
    layouts = prs.slide_layouts

    s = prs.slides.add_slide(layouts[0])                          # 1: title slide with subtitle
    s.shapes.title.text = "Course Overview"
    s.placeholders[1].text = "Spring 2025 - Room 4.2"

    s = prs.slides.add_slide(layouts[1])                          # 2: bullets, nesting, explicit no-bullet paragraph
    s.shapes.title.text = "Main Topic"
    body = s.placeholders[1].text_frame
    body.text = "Key Idea"
    set_bullet(body.paragraphs[0], "buNone")                      # a sub-heading, not a bullet
    for text, level in (("First point", 0), ("Second point, with punctuation!", 0),
                        ("Supporting detail: 3.5% on 2025-03-14", 1), ("Deeper detail (see p. 12)", 2),
                        ("Back to the top level", 0)):
        paragraph = body.add_paragraph()
        paragraph.text = text
        paragraph.level = level

    s = prs.slides.add_slide(layouts[6])                          # 3: blank layout, no title placeholder, boxes added right-first
    add_textbox(s, 5.2, 2.0, ["Right column heading", "right detail"])
    add_textbox(s, 0.5, 2.0, ["Left column heading", "left detail"])
    add_textbox(s, 0.5, 0.4, ["A Slide Without A Title Placeholder"], width=8)

    s = prs.slides.add_slide(layouts[5])                          # 4: title only + overlapping duplicate boxes + a repeated word elsewhere
    s.shapes.title.text = "Duplicates"
    add_textbox(s, 1, 2, ["Shadowed text"])
    add_textbox(s, 1.02, 2.02, ["Shadowed text"])                 # same text, overlapping: a duplicate
    add_textbox(s, 6, 5, ["Shadowed text"])                       # same text, far away: kept

    s = prs.slides.add_slide(layouts[6])                          # 5: no text at all
    s.shapes.add_picture(tmp_image(), Inches(1), Inches(1))

    s = prs.slides.add_slide(layouts[6])                          # 6: numbered list in a box
    box = add_textbox(s, 1, 1, ["Install the tool", "Run the tests", "Read the results", "Sub step"], width=6, height=3)
    for index, paragraph in enumerate(box.text_frame.paragraphs):
        set_bullet(paragraph, "buAutoNum", type="arabicPeriod")
        if index == 3:
            paragraph.level = 1
    s.shapes.title if False else None

    s = prs.slides.add_slide(layouts[5])                          # 7: group + table
    s.shapes.title.text = "Group And Table"
    group = s.shapes.add_group_shape()
    box = group.shapes.add_textbox(Inches(1), Inches(2), Inches(3), Inches(1))
    box.text_frame.text = "Text inside a group"
    table = s.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(6), Inches(1)).table
    for row, values in enumerate((("Name", "Score"), ("Ada", "99")),):
        for col, value in enumerate(values):
            table.cell(row, col).text = value

    s = prs.slides.add_slide(layouts[1])                          # 8: one long paragraph (wraps when displayed), hidden
    s.shapes.title.text = "Long Paragraph"
    s.placeholders[1].text = ("This is a long paragraph that PowerPoint would wrap over several lines but which is one "
                              "paragraph in the file, so it must stay a single element, with “smart quotes”, "
                              "an em dash — and an é accent.")
    s._element.set("show", "0")

    chart_data = CategoryChartData()
    chart_data.categories = ["A", "B"]
    chart_data.add_series("Sales", (1, 2))
    prs.slides[6].shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(5), Inches(2), Inches(3), Inches(2), chart_data)

    path = tmp_path(name)
    prs.save(path)
    return path


_IMAGE = []


def tmp_image():
    if not _IMAGE:
        path = tmp_path("pic.png")
        Image.new("RGB", (40, 40), (200, 100, 50)).save(path)
        _IMAGE.append(path)
    return _IMAGE[0]


# ---------------------------------------------------------------------------
# PDF fixtures
# ---------------------------------------------------------------------------

W, H = 960, 540


def draw_slide(page, title=None, items=(), number=None, footer=None, x=60, y=70, wrap=64):
    """Draw a PowerPoint-style slide. items: (kind, text, level), kind in bullet|para|heading|number."""
    if title:
        for line in textwrap.wrap(title, 40):
            page.insert_text((x, y), line, fontsize=36, fontname="hebo")
            y += 44
        y += 20
    for kind, text, level in items:
        indent = x + level * 40
        if kind in ("bullet", "number"):
            marker = "•" if kind == "bullet" else None
            lines = textwrap.wrap(text, wrap - level * 4)
            for i, line in enumerate(lines):
                if i == 0 and marker:
                    page.insert_text((indent, y), marker, fontsize=20, fontname="helv")
                page.insert_text((indent + 22, y), line, fontsize=20, fontname="helv")
                y += 24
            y += 10
        elif kind == "heading":
            page.insert_text((x, y), text, fontsize=26, fontname="hebo")
            y += 38
        else:
            for line in textwrap.wrap(text, wrap):
                page.insert_text((x, y), line, fontsize=20, fontname="helv")
                y += 24
            y += 10
    if footer:
        page.insert_text((60, H - 20), footer, fontsize=12, fontname="helv")
    if number is not None:
        page.insert_text((W - 70, H - 20), str(number), fontsize=12, fontname="helv")


def build_pdf(name, pages):
    """pages: list of callables taking a PyMuPDF page and drawing on it."""
    document = pymupdf.open()
    for draw in pages:
        page = document.new_page(width=W, height=H)
        draw(page)
    path = tmp_path(name)
    document.save(path)
    document.close()
    return path


def slide_fixture():
    """Four text pages with titles, bullets, nesting, wrapped text, numbers; footer and page numbers."""
    foot = "Quarterly Review 2025"
    return [
        lambda p: draw_slide(p, "Quarterly Review", [], number=1, footer=foot),                     # title-only page
        lambda p: draw_slide(p, "Revenue Highlights", [
            ("bullet", "Revenue grew 12% in 2024 and reached $3.4 million", 0),
            ("bullet", "Europe: 4.5% (up from 2023)", 1),
            ("bullet", "Asia - 3.2%", 1),
            ("bullet", "A long bullet that has to wrap onto a second and even a third line because it keeps going "
                       "and going well past the margin of the slide", 0),
            ("heading", "Risks", 0),
            ("para", "Costs may rise. See the appendix for details.", 0),
        ], number=2, footer=foot),
        lambda p: draw_slide(p, "Plan", [
            ("number", "1. Hire two engineers", 0),
            ("number", "2) Launch on 2025-06-01", 0),
        ], number=3, footer=foot),
        lambda p: draw_slide(p, "Closing", [("bullet", "Thank you!", 0)], number=4, footer=foot),
    ]


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

class TestCleanText(unittest.TestCase):
    def test_typography_accents_and_symbols_become_ascii(self):
        text, unknown = slides.clean_text("“Café” — it’s 5° → 6 ≥ 3…")
        self.assertEqual(text, '"Cafe" - it\'s 5 degrees -> 6 >= 3...')
        self.assertEqual(unknown, set())

    def test_unmappable_characters_are_reported(self):
        text, unknown = slides.clean_text("snowman ☃ here")
        self.assertEqual(text, "snowman ? here")
        self.assertEqual(unknown, {"☃"})

    def test_whitespace_and_soft_line_breaks_collapse(self):
        self.assertEqual(slides.clean_text("a\vb  \t c\n d")[0], "a b c d")

    def test_digits_dates_and_punctuation_are_untouched(self):
        sample = "Due 2025-03-14, 3.5% (approx.) at $12: yes/no; 100%!"
        self.assertEqual(slides.clean_text(sample)[0], sample)


# ---------------------------------------------------------------------------
# PowerPoint extraction
# ---------------------------------------------------------------------------

class TestPptx(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = build_pptx()
        cls.hash_before = sha256(cls.path)
        cls.doc = slides.extract_document(cls.path)
        cls.by_number = {s["number"]: s for s in cls.doc["slides"]}

    def texts(self, number, kind=None):
        return [e["text"] for e in self.by_number[number]["elements"] if kind is None or e["type"] == kind]

    def test_document_overview_and_slide_order(self):
        self.assertEqual(self.doc["source"]["format"], "pptx")
        self.assertEqual([s["number"] for s in self.doc["slides"]], list(range(1, 9)))
        self.assertEqual(self.doc["summary"]["slides"], 8)
        self.assertEqual(self.by_number[2]["title"], "Main Topic")
        self.assertEqual(self.by_number[1]["title"], "Course Overview")

    def test_titles_come_from_placeholders_and_are_explicit(self):
        slide = self.by_number[2]
        self.assertEqual(slide["title_source"], "placeholder")
        self.assertEqual(slide["elements"][0], {"type": "title", "text": "Main Topic", "level": 0, "structure": "explicit"})

    def test_subtitle_is_kept_as_a_subtitle(self):
        self.assertEqual([(e["type"], e["text"]) for e in self.by_number[1]["elements"]],
                         [("title", "Course Overview"), ("subtitle", "Spring 2025 - Room 4.2")])

    def test_bullets_inherited_from_the_master_and_explicit_non_bullets(self):
        elements = [(e["type"], e["text"], e["level"]) for e in self.by_number[2]["elements"][1:]]
        self.assertEqual(elements, [
            ("paragraph", "Key Idea", 0),                                   # buNone: a plain paragraph
            ("bullet", "First point", 0),
            ("bullet", "Second point, with punctuation!", 0),
            ("bullet", "Supporting detail: 3.5% on 2025-03-14", 1),         # numbers, dates, punctuation kept
            ("bullet", "Deeper detail (see p. 12)", 2),
            ("bullet", "Back to the top level", 0),
        ])

    def test_slide_without_a_title_placeholder_uses_a_real_short_text_as_title(self):
        slide = self.by_number[3]
        self.assertEqual(slide["title"], "A Slide Without A Title Placeholder")
        self.assertEqual(slide["title_source"], "inferred_position")
        self.assertEqual(slide["elements"][0]["structure"], "inferred")
        self.assertEqual(self.texts(3).count("A Slide Without A Title Placeholder"), 1)   # not repeated in the body

    def test_multiple_text_boxes_are_read_in_position_order_not_creation_order(self):
        # The right-hand box was created first; reading order must still be left, then right.
        self.assertEqual(self.texts(3)[1:], ["Left column heading", "left detail", "Right column heading", "right detail"])

    def test_overlapping_duplicate_boxes_are_dropped_but_distant_repeats_are_kept(self):
        self.assertEqual(self.texts(4).count("Shadowed text"), 2)           # 3 boxes: 1 duplicate removed
        self.assertTrue(any("duplicate" in w for w in self.by_number[4]["warnings"]))

    def test_slide_with_only_a_picture_is_reported_not_silently_empty(self):
        slide = self.by_number[5]
        self.assertEqual((slide["status"], slide["elements"]), ("no_text", []))
        self.assertTrue(any("picture" in w for w in slide["warnings"]))
        self.assertIn("5", self.doc["warnings"][-1])

    def test_numbered_lists_are_numbered_with_nesting(self):
        bullets = [(e.get("marker"), e["text"], e["level"]) for e in self.by_number[6]["elements"]]
        self.assertEqual(bullets[:3], [("1.", "Install the tool", 0), ("2.", "Run the tests", 0), ("3.", "Read the results", 0)])
        self.assertEqual(bullets[3][2], 1)

    def test_text_in_groups_and_tables_is_found(self):
        self.assertIn("Text inside a group", self.texts(7))
        self.assertEqual(self.texts(7, "table_row"), ["Name | Score", "Ada | 99"])

    def test_charts_are_reported_because_their_text_is_not_read(self):
        self.assertTrue(any("chart" in w and "not extracted" in w for w in self.by_number[7]["warnings"]))
        self.assertIn("Text inside a group", self.texts(7))                  # the rest of the slide is unaffected

    def test_long_paragraph_stays_one_element_and_is_cleaned(self):
        elements = [e for e in self.by_number[8]["elements"] if e["type"] != "title"]
        self.assertEqual(len(elements), 1)
        self.assertIn('"smart quotes", an em dash - and an e accent.', elements[0]["text"])
        self.assertTrue(self.by_number[8].get("hidden"))
        self.assertTrue(any("hidden" in w for w in self.by_number[8]["warnings"]))

    def test_every_word_of_the_source_survives(self):
        """Independent check: words from every text node in the slide XML are all in the extraction."""
        prs = Presentation(self.path)
        expected = []
        for slide in prs.slides:
            for node in slide._element.iter("{http://schemas.openxmlformats.org/drawingml/2006/main}t"):
                expected.extend(slides.clean_text(node.text or "")[0].split())
        found = " ".join(e["text"] for s in self.doc["slides"] for e in s["elements"]).split()
        from collections import Counter
        missing = Counter(expected) - Counter(found) - Counter(["Shadowed", "text"])   # one duplicate pair was dropped
        self.assertEqual(missing, Counter())

    def test_original_file_is_never_modified(self):
        slides.extract_document(self.path)
        self.assertEqual(sha256(self.path), self.hash_before)
        self.assertEqual(self.doc["source"]["sha256"], self.hash_before)


# ---------------------------------------------------------------------------
# PDF extraction
# ---------------------------------------------------------------------------

class TestPdf(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = build_pdf("slides.pdf", slide_fixture())
        cls.doc = slides.extract_document(cls.path)
        cls.by_number = {s["number"]: s for s in cls.doc["slides"]}

    def triples(self, number):
        return [(e["type"], e["text"], e["level"]) for e in self.by_number[number]["elements"]]

    def test_pages_in_order_with_inferred_titles(self):
        self.assertEqual([s["number"] for s in self.doc["slides"]], [1, 2, 3, 4])
        self.assertEqual([s["title"] for s in self.doc["slides"]],
                         ["Quarterly Review", "Revenue Highlights", "Plan", "Closing"])
        for slide in self.doc["slides"]:
            self.assertEqual(slide["title_source"], "inferred_font_size")
            self.assertEqual(slide["elements"][0]["structure"], "inferred")

    def test_title_only_page_still_gets_its_title(self):
        self.assertEqual(self.triples(1), [("title", "Quarterly Review", 0)])

    def test_bullets_nesting_wrapped_text_headings_and_paragraphs(self):
        self.assertEqual(self.triples(2), [
            ("title", "Revenue Highlights", 0),
            ("bullet", "Revenue grew 12% in 2024 and reached $3.4 million", 0),
            ("bullet", "Europe: 4.5% (up from 2023)", 1),
            ("bullet", "Asia - 3.2%", 1),
            ("bullet", "A long bullet that has to wrap onto a second and even a third line because it keeps going "
                       "and going well past the margin of the slide", 0),
            ("heading", "Risks", 0),
            ("paragraph", "Costs may rise. See the appendix for details.", 0),
        ])

    def test_numbered_markers_are_kept(self):
        elements = self.by_number[3]["elements"]
        self.assertEqual([(e.get("marker"), e["text"]) for e in elements[1:]],
                         [("1.", "Hire two engineers"), ("2)", "Launch on 2025-06-01")])

    def test_page_numbers_and_repeated_footers_are_dropped_and_reported(self):
        everything = " ".join(e["text"] for s in self.doc["slides"] for e in s["elements"])
        self.assertNotIn("Quarterly Review 2025", everything)
        self.assertTrue(any("Dropped" in w for w in self.doc["warnings"]))

    def test_wrapped_line_that_starts_like_a_number_stays_inside_its_bullet(self):
        path = build_pdf("wrap_number.pdf", [lambda p: draw_slide(p, "Methods", [
            ("bullet", "The method includes (1) showing the appearance, (2) displaying the actions, (3) revealing "
                       "the thoughts", 0),
            ("bullet", "Second bullet", 0)], wrap=40)])
        doc = slides.extract_document(path)
        elements = doc["slides"][0]["elements"]
        self.assertEqual(len(elements), 3)
        self.assertIn("(1) showing", elements[1]["text"])
        self.assertEqual(elements[2]["text"], "Second bullet")

    def test_hyphenated_line_break_keeps_the_hyphen(self):
        self.assertEqual(slides_pdf.join_wrapped("well-", "known"), "well-known")
        self.assertEqual(slides_pdf.join_wrapped("well", "known"), "well known")

    def test_two_columns_are_read_left_then_right(self):
        def draw(page):
            page.insert_text((60, 80), "Compare", fontsize=36, fontname="hebo")
            for i, text in enumerate(("Left one", "Left two", "Left three")):
                page.insert_text((60, 160 + 40 * i), text, fontsize=20, fontname="helv")
            for i, text in enumerate(("Right one", "Right two", "Right three")):
                page.insert_text((560, 160 + 40 * i), text, fontsize=20, fontname="helv")
        doc = slides.extract_document(build_pdf("columns.pdf", [draw]))
        self.assertEqual([e["text"] for e in doc["slides"][0]["elements"][1:]],
                         ["Left one", "Left two", "Left three", "Right one", "Right two", "Right three"])

    def test_scrambled_content_stream_order_is_sorted_by_position(self):
        def draw(page):
            page.insert_text((60, 240), "third", fontsize=20, fontname="helv")
            page.insert_text((60, 160), "first", fontsize=20, fontname="helv")
            page.insert_text((60, 200), "second", fontsize=20, fontname="helv")
            page.insert_text((60, 80), "Heading Here", fontsize=36, fontname="hebo")
        doc = slides.extract_document(build_pdf("scrambled.pdf", [draw]))
        self.assertEqual([e["text"] for e in doc["slides"][0]["elements"]], ["Heading Here", "first", "second", "third"])

    def test_repeated_overlapping_text_is_extracted_once(self):
        def draw(page):
            draw_slide(page, "Shadow", [("para", "Only once please", 0)])
            page.insert_text((61, 71 + 44 + 20), "Only once please", fontsize=20, fontname="helv")  # a shadow copy
        doc = slides.extract_document(build_pdf("shadow.pdf", [draw]))
        self.assertEqual([e["text"] for e in doc["slides"][0]["elements"]].count("Only once please"), 1)

    def test_page_without_text_is_reported_and_the_rest_is_kept(self):
        def image_only(page):
            page.draw_rect(pymupdf.Rect(0, 0, W, H), fill=(0.8, 0.7, 0.5))
        pages = slide_fixture()
        pages.insert(2, image_only)
        doc = slides.extract_document(build_pdf("partial.pdf", pages))
        statuses = [s["status"] for s in doc["slides"]]
        self.assertEqual(statuses, ["ok", "ok", "no_text", "ok", "ok"])
        self.assertEqual(doc["slides"][2]["elements"], [])
        self.assertTrue(any("No selectable text on page 3" in w for w in doc["slides"][2]["warnings"]))
        self.assertTrue(any("3" in w and "without reliably extracted text" in w for w in doc["warnings"]))
        self.assertEqual(doc["summary"]["slides_with_text"], 4)               # real text from the other pages is preserved
        self.assertEqual(doc["slides"][1]["title"], "Revenue Highlights")

    def test_mostly_image_page_with_little_text_is_marked_uncertain(self):
        buffer = io.BytesIO()
        Image.new("RGB", (80, 60), (30, 90, 160)).save(buffer, "PNG")

        def draw(page):
            page.insert_image(pymupdf.Rect(0, 0, W, H), stream=buffer.getvalue())
            page.insert_text((60, 500), "Fig. 1", fontsize=20, fontname="helv")
        doc = slides.extract_document(build_pdf("image_page.pdf", [draw, lambda p: draw_slide(p, "Real Text", [("para", "Plenty of words here on this page.", 0)])]))
        self.assertEqual(doc["slides"][0]["status"], "uncertain")
        self.assertEqual([e["text"] for e in doc["slides"][0]["elements"]], ["Fig. 1"])      # the text there is still kept
        self.assertEqual(doc["slides"][1]["status"], "ok")

    def test_document_with_no_text_at_all_is_an_error(self):
        path = build_pdf("blank.pdf", [lambda p: p.draw_rect(pymupdf.Rect(0, 0, 100, 100), fill=(0, 0, 0))] * 2)
        with self.assertRaisesRegex(extract.ExtractError, "No extractable text.*OCR"):
            slides.extract_document(path)


# ---------------------------------------------------------------------------
# Regressions found by validating against a real PowerPoint-exported PDF
# ---------------------------------------------------------------------------

def put(page, x, baseline, text, size=20, bold=False):
    page.insert_text((x, baseline), text, fontsize=size, fontname="hebo" if bold else "helv")


def extract_one(name, draw):
    doc = slides.extract_document(build_pdf(name, [draw]))
    return doc["slides"][0]


class TestRealExportRegressions(unittest.TestCase):
    """Each test reproduces a failure seen on the real export, with a small synthetic page."""

    def triples(self, slide):
        return [(e["type"], e["text"], e["level"]) for e in slide["elements"]]

    def test_bullet_and_text_on_one_line_with_a_hanging_indent_wrap(self):
        # Real PDFs keep "- text" as ONE line starting at the bullet glyph; wrapped lines start further right.
        def draw(page):
            put(page, 60, 90, "Reading Assignment", 42)
            put(page, 60, 170, "\u2022 Wednesday, September 18 - The Pardoner", 28)
            put(page, 96, 210, "\u2013 The General Prologue (Pardoner's Description) -Pgs. 31-", 24)
            put(page, 118, 238, "33: Lines 671-716", 24)                       # the wrapped rest, indented under the text
            put(page, 96, 270, "\u2013 Pardoner's Intro & Prologue - Pgs. 117-122", 24)
            put(page, 60, 310, "\u2022 Monday, September 23 - The Wife of Bath", 28)
        slide = extract_one("hanging.pdf", draw)
        self.assertEqual(self.triples(slide), [
            ("title", "Reading Assignment", 0),
            ("bullet", "Wednesday, September 18 - The Pardoner", 0),
            ("bullet", "The General Prologue (Pardoner's Description) -Pgs. 31-33: Lines 671-716", 1),
            ("bullet", "Pardoner's Intro & Prologue - Pgs. 117-122", 1),
            ("bullet", "Monday, September 23 - The Wife of Bath", 0),
        ])

    def test_text_start_after_the_marker_is_recorded(self):
        def draw(page):
            put(page, 100, 100, "\u2022 Some text here", 20)
        page = pymupdf.open(build_pdf("textx.pdf", [draw]))[0]
        line = slides_pdf.page_lines(page)[0]
        self.assertGreater(line["text_x0"], line["x0"] + 5)      # the text starts after the bullet glyph
        self.assertIn(line["text"][0], config.PDF_BULLET_CHARS)      # PyMuPDF reads Helvetica's bullet as U+00B7

    def test_title_that_starts_just_below_the_old_40_percent_cutoff(self):
        def draw(page):                                           # a title slide: 47 pt title at 41% of the page height
            put(page, 60, 260, "Geoffrey", 47, bold=True)
            put(page, 60, 312, "Chaucer", 47, bold=True)
            put(page, 60, 500, "body text elsewhere", 19)
            put(page, 60, 524, "and some more", 19)
        slide = extract_one("title_low.pdf", draw)
        self.assertEqual(slide["title"], "Geoffrey Chaucer")

    def test_title_lower_on_the_page_beside_a_column_of_bullets(self):
        def draw(page):                                           # like the "About the Pilgrims" slide
            put(page, 62, 385, "About the", 31)
            put(page, 76, 423, "Pilgrims", 31)
            put(page, 284, 335, "\u2022 These pilgrims and their stories", 16)
            put(page, 284, 400, "\u2022 3 groups of pilgrims", 16)
            put(page, 284, 425, "\u2022 1)Oratores (those who pray)", 16)
            put(page, 284, 450, "\u2022 2) Bellatores (those who fight)", 16)
        slide = extract_one("title_beside.pdf", draw)
        self.assertEqual(slide["title"], "About the Pilgrims")
        self.assertEqual([e["type"] for e in slide["elements"][1:]], ["bullet"] * 4)

    def test_multi_line_title_continues_below_the_old_cutoff(self):
        def draw(page):                                           # a 3-line title starting at 36% and ending at 60%
            put(page, 518, 230, "The", 38, bold=True)
            put(page, 518, 271, "Canterbury", 38, bold=True)
            put(page, 518, 312, "Tales", 38, bold=True)
            put(page, 60, 480, "caption text in small type", 16)
            put(page, 60, 500, "second caption line", 16)
        self.assertEqual(extract_one("title_multi.pdf", draw)["title"], "The Canterbury Tales")

    def test_the_title_is_the_top_most_large_text_not_the_biggest(self):
        def draw(page):                                           # a sub-heading (48 pt) set larger than the title (43 pt)
            put(page, 134, 70, "Literary Characteristics of", 43, bold=True)
            put(page, 174, 122, "The Canterbury Tales", 43, bold=True)
            put(page, 57, 200, "Allegory", 48, bold=True)
            put(page, 84, 250, "\u2022 An allegory is a story that functions on two levels: a literal level and a", 19)
            put(page, 84, 274, "\u2022 A character, place, or event is used to deliver a broader message about", 19)
            put(page, 84, 298, "\u2022 Allegories often teach a moral lesson through their symbolic meaning here", 19)
            put(page, 84, 322, "\u2022 For example, the surface story might be about two neighbors throwing rocks", 19)
        slide = extract_one("heading_bigger.pdf", draw)
        self.assertEqual(slide["title"], "Literary Characteristics of The Canterbury Tales")
        self.assertEqual(self.triples(slide)[1], ("heading", "Allegory", 0))

    def test_title_column_beside_two_body_columns(self):
        """The 'Seven Deadly Sins' layout: a tall narrow title on the left and two columns of bullets.

        Positions are from the real 720 pt wide export, scaled to this test's 960 pt wide page.
        """
        sx = W / 720

        def draw(page):
            for i, line in enumerate(["Chaucer's", "Focus On", "The Seven", "Deadly Sins", "and Seven", "Virtues"]):
                put(page, 56 * sx, 140 + 38 * i, line, 35, bold=True)
            left = [(275, 130, "\u2022 The seven deadly sins"), (293, 149, "are transgressions or"),
                    (293, 168, "sins that lead to"), (293, 187, "spiritual ruin and"), (293, 206, "damnation."),
                    (275, 232, "\u2022 Gluttony"), (275, 255, "\u2022 Greed")]
            right = [(506, 130, "\u2022 To combat the Seven"), (524, 149, "Deadly Sins, believers"),
                     (524, 168, "were urged to cultivate"), (524, 187, "the Seven Virtues:"),
                     (515, 212, "\u2022 Moderation"), (515, 235, "\u2022 Generosity")]
            for x, y, text in left + right:
                put(page, x * sx, y, text, 17)
        slide = extract_one("three_columns.pdf", draw)
        self.assertEqual(self.triples(slide), [
            ("title", "Chaucer's Focus On The Seven Deadly Sins and Seven Virtues", 0),
            ("bullet", "The seven deadly sins are transgressions or sins that lead to spiritual ruin and damnation.", 0),
            ("bullet", "Gluttony", 0),
            ("bullet", "Greed", 0),
            ("bullet", "To combat the Seven Deadly Sins, believers were urged to cultivate the Seven Virtues:", 0),
            ("bullet", "Moderation", 0),            # level is judged within its own column, not against the left column
            ("bullet", "Generosity", 0),
        ])

    def test_split_columns_handles_more_than_two_columns(self):
        lines = [{"x0": x, "x1": x + 60, "y0": y, "y1": y + 20, "text": f"{x}-{y}"}
                 for x in (50, 300, 600) for y in (100, 140)]
        groups = slides_pdf.split_columns(lines, 960)
        self.assertEqual([sorted({l["x0"] for l in g}) for g in groups], [[50], [300], [600]])

    def test_one_column_of_text_is_not_split(self):
        lines = [{"x0": 60 + 36 * (i % 2), "x1": 700, "y0": 100 + 30 * i, "y1": 120 + 30 * i, "text": "x"} for i in range(8)]
        self.assertEqual(len(slides_pdf.split_columns(lines, 960)), 1)


REAL_EXPORT = Path(config.BASE_DIR) / "output" / "validation_pdf" / "chaucer_powerpoint_export.pdf"


@unittest.skipUnless(REAL_EXPORT.exists() and REAL_DECK, "needs the real PowerPoint export in output/validation_pdf/ and the deck")
class TestRealPowerPointExport(unittest.TestCase):
    """The genuine PDF exported by PowerPoint must agree with the explicit PowerPoint extraction."""

    @classmethod
    def setUpClass(cls):
        cls.pdf = slides.extract_document(str(REAL_EXPORT))["slides"]
        cls.pptx = slides.extract_document(str(REAL_DECK))["slides"]

    def test_same_titles_text_order_and_levels(self):
        self.assertEqual(len(self.pdf), len(self.pptx))
        for from_pdf, from_pptx in zip(self.pdf, self.pptx):
            self.assertEqual(from_pdf["title"], from_pptx["title"], f"slide {from_pptx['number']} title")
            self.assertEqual([e["text"] for e in from_pdf["elements"]], [e["text"] for e in from_pptx["elements"]],
                             f"slide {from_pptx['number']}")
            self.assertEqual([e["level"] for e in from_pdf["elements"]], [e["level"] for e in from_pptx["elements"]])

    def test_every_element_is_marked_as_inferred(self):
        self.assertTrue(all(e["structure"] == "inferred" for s in self.pdf for e in s["elements"]))

    def test_picture_heavy_title_slides_are_flagged_not_hidden(self):
        flagged = [s["number"] for s in self.pdf if s["status"] == "uncertain"]
        self.assertEqual(flagged, [1, 4])
        self.assertTrue(all("image" in w for n in flagged for w in self.pdf[n - 1]["warnings"]))

    def test_no_duplicate_lines_and_no_dropped_footers(self):
        for slide in self.pdf:
            texts = [e["text"] for e in slide["elements"]]
            self.assertEqual(len(texts), len(set(texts)), f"duplicate text on page {slide['number']}")


# ---------------------------------------------------------------------------
# Errors and the command line
# ---------------------------------------------------------------------------

class TestErrors(unittest.TestCase):
    def test_missing_file(self):
        with self.assertRaisesRegex(extract.ExtractError, "not found"):
            slides.extract_document(tmp_path("nope.pptx"))

    def test_unsupported_extensions(self):
        for name in ("notes.txt", "old.ppt", "noextension"):
            path = tmp_path(name)
            Path(path).write_text("hello")
            with self.assertRaisesRegex(extract.ExtractError, "Unsupported file type"):
                slides.extract_document(path)

    def test_corrupt_pptx_and_pdf(self):
        bad_pptx, bad_pdf = tmp_path("bad.pptx"), tmp_path("bad.pdf")
        Path(bad_pptx).write_bytes(b"this is not a zip file")
        Path(bad_pdf).write_bytes(b"this is not a pdf")
        with self.assertRaisesRegex(extract.ExtractError, "could not be opened as a PowerPoint"):
            slides.extract_document(bad_pptx)
        with self.assertRaises(extract.ExtractError):
            slides.extract_document(bad_pdf)

    def test_presentation_without_any_text(self):
        prs = Presentation()
        prs.slides.add_slide(prs.slide_layouts[6])
        path = tmp_path("empty_deck.pptx")
        prs.save(path)
        with self.assertRaisesRegex(extract.ExtractError, "No extractable text"):
            slides.extract_document(path)

    def test_presentation_with_no_slides(self):
        path = tmp_path("no_slides.pptx")
        Presentation().save(path)
        with self.assertRaisesRegex(extract.ExtractError, "no slides"):
            slides.extract_document(path)


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = slides.main(list(args))
    return code, out.getvalue(), err.getvalue()


class TestCommandLineAndOutputs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pptx = build_pptx("cli_deck.pptx")
        cls.out_dir = Path(tempfile.mkdtemp(dir=_tmp.name))
        cls.notes = cls.out_dir / "notes.txt"
        cls.code, cls.stdout, cls.stderr = run_cli(cls.pptx, "--output", str(cls.notes), "--preview", str(cls.out_dir / "p.txt"))

    def test_writes_notes_json_and_preview_next_to_each_other(self):
        self.assertEqual(self.code, 0, self.stderr)
        self.assertTrue(self.notes.exists())
        self.assertTrue((self.out_dir / "notes.json").exists())
        self.assertIn("=== 2", (self.out_dir / "p.txt").read_text())
        self.assertIn("Extracted", self.stdout)
        self.assertIn("WARNING", self.stdout)                                   # the picture-only slide

    def test_json_is_valid_and_follows_the_documented_schema(self):
        doc = json.loads((self.out_dir / "notes.json").read_text())
        self.assertEqual(doc["schema_version"], 1)
        for key in ("source", "extracted_at", "method", "structure_note", "slides", "warnings", "summary"):
            self.assertIn(key, doc)
        for key in ("file", "path", "format", "size_bytes", "sha256", "pages"):
            self.assertIn(key, doc["source"])
        for slide in doc["slides"]:
            for key in ("number", "title", "title_source", "status", "elements", "warnings"):
                self.assertIn(key, slide)
            self.assertIn(slide["status"], ("ok", "no_text", "uncertain"))
            self.assertIn(slide["title_source"], ("placeholder", "inferred_position", "inferred_font_size", "none"))
            for element in slide["elements"]:
                self.assertIn(element["type"], ("title", "subtitle", "heading", "paragraph", "bullet", "table_row"))
                self.assertIsInstance(element["level"], int)
                self.assertIn(element["structure"], ("explicit", "inferred"))
                self.assertTrue(element["text"].isascii())
        self.assertEqual(doc["summary"]["elements"], sum(len(s["elements"]) for s in doc["slides"]))

    def test_default_output_goes_under_output_dir(self):
        with mock.patch.object(config, "OUTPUT_DIR", self.out_dir / "default"):
            code, stdout, stderr = run_cli(self.pptx)
        self.assertEqual(code, 0, stderr)
        self.assertTrue((self.out_dir / "default" / "cli_deck_notes.txt").exists())
        self.assertTrue((self.out_dir / "default" / "cli_deck_notes.json").exists())

    def test_input_is_never_overwritten_or_modified(self):
        before = sha256(self.pptx)
        for target in (self.pptx, self.pptx.replace(".pptx", ".pdf")):
            code, _, stderr = run_cli(self.pptx, "--output", target)
            self.assertEqual(code, 1)
            self.assertIn("Refusing to write", stderr)
        self.assertEqual(sha256(self.pptx), before)

    def test_error_messages_through_the_command_line(self):
        code, _, stderr = run_cli(tmp_path("missing.pdf"))
        self.assertEqual((code, "not found" in stderr), (1, True))
        code, _, stderr = run_cli(self.pptx.replace(".pptx", ".docx"))
        self.assertEqual(code, 1)

    def test_notes_text_exact_format(self):
        document = {"slides": [
            {"number": 1, "title": "Presentation title", "elements": [
                {"type": "title", "text": "Presentation title", "level": 0},
                {"type": "subtitle", "text": "Main topic", "level": 0},
                {"type": "bullet", "text": "First point", "level": 0},
                {"type": "bullet", "text": "Second point", "level": 0},
                {"type": "bullet", "text": "Supporting detail", "level": 1},
                {"type": "bullet", "text": "Step one", "level": 0, "marker": "1."}]},
            {"number": 2, "title": None, "elements": [
                {"type": "paragraph", "text": "No title here", "level": 0},
                {"type": "bullet", "text": "Another point", "level": 0}]},
            {"number": 3, "title": None, "elements": []},
        ]}
        self.assertEqual(slides.to_notes_text(document), "\n".join([
            "Presentation title", "Main topic", "",
            "* First point", "* Second point", "   * Supporting detail", "1. Step one", "", "",
            "Slide 2", "", "No title here", "", "* Another point", ""]))


# ---------------------------------------------------------------------------
# Compatibility with render.py
# ---------------------------------------------------------------------------

def make_solid_glyph_dir():
    """All 62 characters as solid 24 x 30 px blocks (3 samples each) plus a style file."""
    root = Path(tempfile.mkdtemp(dir=_tmp.name))
    records = []
    for char in string.ascii_letters + string.digits:
        for sample in (1, 2, 3):
            name = f"g{len(records):03d}.png"
            Image.new("RGBA", (24 + 2 * sample, 30), (0, 0, 0, 255)).save(root / name)
            records.append({"char": char, "sample": sample, "file": name, "baseline_from_top_px": 30.0,
                            "image_size_px": {"width": 24 + 2 * sample, "height": 30}})
    (root / config.GLYPH_METADATA_NAME).write_text(json.dumps(
        {"meta": {"pixels_per_point": 4.0, "trim_padding_px": 4}, "glyphs": records}))
    (root / config.STYLE_NAME).write_text(json.dumps(
        {"letter_spacing": {"estimate_pt": 1.5, "quality": "approximate"},
         "word_spacing": {"mean_pt": 10, "quality": "good"}}))
    return root


class TestRendererCompatibility(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.glyphs = make_solid_glyph_dir()
        cls.doc = slides.extract_document(build_pptx("render_deck.pptx"))
        cls.notes = slides.to_notes_text(cls.doc)

    def test_notes_text_renders_and_every_character_survives(self):
        pages, ctx, warnings, normalized = render.render_text(self.notes, self.glyphs, seed=1)
        placed = "".join(item["char"] for page in pages for line in page["lines"] for _, item in line["items"])
        self.assertEqual(placed, "".join(self.notes.split()))
        self.assertEqual([w for w in warnings if "replaced" in w], [])        # nothing non-ASCII left for the renderer

    def test_notes_text_survives_the_renderer_unchanged_by_its_own_normalization(self):
        self.assertEqual(render.normalize_text(self.notes)[0], self.notes.replace("\r", ""))

    def test_nested_bullets_are_indented_and_wrapped_lines_hang_under_their_text(self):
        text = "* top level bullet\n   * nested bullet\n"
        with mock.patch.object(config, "ROTATION_MAX_DEG", 0), mock.patch.object(config, "SCALE_JITTER", 0), \
             mock.patch.object(config, "BASELINE_JITTER_PX", 0), mock.patch.object(config, "BASELINE_KEEP_FRACTION", 0.0):
            pages, ctx, _, _ = render.render_text(text, self.glyphs, seed=1)
        lines = pages[0]["lines"]
        first_x = lines[0]["items"][0][0]
        second_x = lines[1]["items"][0][0]
        self.assertAlmostEqual(first_x, ctx["left"], delta=0.01)
        self.assertAlmostEqual(second_x - first_x, 3 * config.RENDER_INDENT_PT_PER_SPACE * ctx["px"], delta=0.01)

    def test_full_pipeline_to_a_handwritten_pdf(self):
        notes = tmp_path("pipeline_notes.txt")
        out = tmp_path("pipeline.pdf")
        self.assertEqual(run_cli(build_pptx("pipeline.pptx"), "--output", notes)[0], 0)
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(config, "GLYPHS_DIR", self.glyphs.parent), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = render.main(["--input", notes, "--user", self.glyphs.name, "--output", out, "--seed", "2"])
        self.assertEqual(code, 0, stderr.getvalue())
        with pymupdf.open(out) as document:
            self.assertGreaterEqual(document.page_count, 1)
            for page in document:
                self.assertAlmostEqual(page.rect.width, 447.0, places=2)
                self.assertAlmostEqual(page.rect.height, 596.0, places=2)


@unittest.skipUnless(REAL_DECK and (REAL_GLYPHS / config.GLYPH_METADATA_NAME).exists(),
                     "needs the deck in slides_in/ and glyphs/amaya/")
class TestRealDeckIntegration(unittest.TestCase):
    def test_real_deck_extracts_and_renders_in_real_handwriting(self):
        before = sha256(REAL_DECK)
        document = slides.extract_document(str(REAL_DECK))
        self.assertEqual(sha256(REAL_DECK), before)                            # original untouched
        self.assertEqual(document["source"]["pages"], len(document["slides"]))
        self.assertTrue(all(s["status"] == "ok" for s in document["slides"]))
        self.assertTrue(all(s["title"] for s in document["slides"]))

        first_slides = dict(document, slides=document["slides"][:2])          # keep the test quick
        notes = slides.to_notes_text(first_slides)
        out = tmp_path("real_deck.pdf")
        notes_path = tmp_path("real_deck_notes.txt")
        Path(notes_path).write_text(notes)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            code = render.main(["--input", notes_path, "--user", "amaya", "--output", out, "--seed", "3"])
        self.assertEqual(code, 0)
        with pymupdf.open(out) as pdf:
            self.assertGreaterEqual(pdf.page_count, 1)
            self.assertTrue(all(abs(p.rect.width - 447) < 0.01 and abs(p.rect.height - 596) < 0.01 for p in pdf))

        pages, _, _, normalized = render.render_text(notes, REAL_GLYPHS, seed=3)
        placed = "".join(item["char"] for page in pages for line in page["lines"] for _, item in line["items"])
        self.assertEqual(placed, "".join(normalized.split()))                 # every extracted character was written


if __name__ == "__main__":
    unittest.main()
