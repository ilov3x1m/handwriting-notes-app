"""Central settings for the handwriting app.

Everything that controls the template layout lives here so no other script
needs hardcoded numbers.

Units: every length below is in PDF points (1 pt = 1/72 inch) unless the
name says otherwise.

Coordinate convention (used by the PDF and by template_layout.json):
the origin is the BOTTOM-LEFT corner of each page, x grows right, y grows up.
This is reportlab's native system. PyMuPDF and image pixels use a TOP-LEFT
origin, so convert in one place only (see preview.py).
"""

from pathlib import Path

# ---------------------------------------------------------------------------
# Device profiles
# ---------------------------------------------------------------------------
# display_px is the tablet's screen resolution in pixels. It is NOT the PDF
# size: pixels are only used to check the aspect ratio. page_pt is the PDF
# page size in points. 447 x 596 is exactly 3:4, matching 1404 x 1872, and is
# about 226 pixels per inch (the reMarkable 2 screen density).
#
# To support another device, add an entry here and change DEVICE below.
DEVICES = {
    "remarkable_2": {
        "name": "reMarkable 2",
        "display_px": (1404, 1872),
        "page_pt": (447.0, 596.0),
    },
}

DEVICE = "remarkable_2"

# How far page_pt's aspect ratio may differ from display_px's (relative).
ASPECT_TOLERANCE = 0.001


def validate_device(key):
    """Return the profile for `key`, or raise ValueError if it is unusable."""
    if key not in DEVICES:
        raise ValueError(f"Unknown device '{key}'. Known devices: {sorted(DEVICES)}")
    profile = DEVICES[key]
    px_w, px_h = profile["display_px"]
    pt_w, pt_h = profile["page_pt"]
    if min(px_w, px_h, pt_w, pt_h) <= 0:
        raise ValueError(f"Device '{key}' has a zero or negative dimension")
    # Compare ratios, because a page with the wrong shape would be scaled or
    # letterboxed by the tablet and every crop would drift.
    px_ratio = px_w / px_h
    pt_ratio = pt_w / pt_h
    if abs(px_ratio - pt_ratio) / px_ratio > ASPECT_TOLERANCE:
        raise ValueError(
            f"Device '{key}': page_pt aspect {pt_ratio:.4f} does not match "
            f"display_px aspect {px_ratio:.4f}"
        )
    return profile


DEVICE_PROFILE = validate_device(DEVICE)
PAGE_WIDTH, PAGE_HEIGHT = DEVICE_PROFILE["page_pt"]

# ---------------------------------------------------------------------------
# Page margins and header
# ---------------------------------------------------------------------------
MARGIN_LEFT = 24
MARGIN_RIGHT = 24
MARGIN_TOP = 24
MARGIN_BOTTOM = 24

# Space under the top margin for the page title and instruction line.
HEADER_HEIGHT = 34
TITLE_FONT_SIZE = 12
INSTRUCTION_FONT_SIZE = 7.5
FOOTER_FONT_SIZE = 7
FOOTER_Y = 12  # baseline of the footer text, inside the bottom margin

# ---------------------------------------------------------------------------
# Character grid (pages 1-3)
# ---------------------------------------------------------------------------
SAMPLES_PER_CHAR = 3
COLUMNS = 2
COLUMN_GAP = 14      # space between the two columns
LABEL_WIDTH = 16     # column reserved for the printed character label
LABEL_GAP = 4        # space between the label and the first box
BOX_GAP = 4          # space between the three boxes of one character
ROW_GAP = 6          # space between rows
BOX_HEIGHT = 33      # box width is worked out from the column width
LABEL_FONT_SIZE = 12

# Where the baseline guide sits, as a fraction of box height measured up from
# the bottom edge. 0.30 leaves room below the line for g, j, p, q, y.
BASELINE_FRACTION = 0.30

# ---------------------------------------------------------------------------
# Sample sentence area (page 3)
# ---------------------------------------------------------------------------
SAMPLE_SENTENCE = "The quick brown fox jumps over the lazy dog."
SECTION_GAP = 20            # space between the digit grid and the sentence section
SENTENCE_HEADING_HEIGHT = 30  # room for the heading and the printed prompt
SENTENCE_HEADING_FONT_SIZE = 10
SENTENCE_PROMPT_FONT_SIZE = 8
SENTENCE_LINES = 4          # ruled lines inside the area
SENTENCE_LINE_PITCH = 60    # height of one ruled line

# ---------------------------------------------------------------------------
# Line thickness and colours (grayscale, 0 = black, 1 = white)
# ---------------------------------------------------------------------------
# Light borders so the printed grid is easy to tell apart from pen ink.
BOX_BORDER_WIDTH = 0.5
BOX_BORDER_GRAY = 0.70
BASELINE_WIDTH = 0.4
BASELINE_GRAY = 0.82
TEXT_GRAY = 0.15
PROMPT_GRAY = 0.55
FONT_NAME = "Helvetica"
FONT_NAME_BOLD = "Helvetica-Bold"

# Coordinates are rounded to this many decimals before drawing AND saving,
# so the PDF and the JSON can never disagree by rounding.
COORD_DECIMALS = 3

# ---------------------------------------------------------------------------
# Page contents
# ---------------------------------------------------------------------------
# Template versions. V1 (3 pages, 186 samples) is kept so profiles made from it still work;
# V2 adds a 4th page of punctuation (13 symbols x 3 = 39 samples, 225 in all).
PUNCTUATION_CHARS = ",.?!'\"():;-*\u2022"   # page 4 of V2, in template order
PUNCTUATION_FILE_NAMES = {                  # safe, case-insensitive-unique stems for the PNG files
    ",": "comma", ".": "period", "?": "question", "!": "exclaim", "'": "quote_single",
    '"': "quote_double", "(": "paren_left", ")": "paren_right", ":": "colon", ";": "semicolon",
    "-": "hyphen", "*": "asterisk", "\u2022": "bullet",
}
_BASE_PAGES = [
    {"title": "Uppercase letters A-Z", "chars": "ABCDEFGHIJKLMNOPQRSTUVWXYZ"},
    {"title": "Lowercase letters a-z", "chars": "abcdefghijklmnopqrstuvwxyz"},
    {"title": "Digits 0-9 and sample sentence", "chars": "0123456789"},
]
TEMPLATE_VERSIONS = {
    1: {"pages": _BASE_PAGES, "sentence_page": 3},
    2: {"pages": _BASE_PAGES + [
        {"title": "Punctuation and symbols", "chars": PUNCTUATION_CHARS, "box_height": 52,
         "note": "Write each symbol where it normally sits: commas hang below the line, quotes sit high."}],
        "sentence_page": 3},
}
DEFAULT_TEMPLATE_VERSION = 2
PAGES = TEMPLATE_VERSIONS[DEFAULT_TEMPLATE_VERSION]["pages"]
SENTENCE_PAGE = TEMPLATE_VERSIONS[DEFAULT_TEMPLATE_VERSION]["sentence_page"]

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_PDF = BASE_DIR / "template.pdf"
LAYOUT_JSON = BASE_DIR / "template_layout.json"     # the default (newest) version
TEMPLATES_DIR = BASE_DIR / "templates"              # templates/v<N>/template.pdf + template_layout.json
PREVIEW_DIR = BASE_DIR / "output" / "preview"
PREVIEW_DPI = 150
GLYPHS_DIR = BASE_DIR / "glyphs"          # extract.py writes glyphs/<user>/
GLYPH_METADATA_NAME = "glyph_metadata.json"

# ---------------------------------------------------------------------------
# Extraction (extract.py)
# ---------------------------------------------------------------------------
# A multiple of 72 keeps points -> pixels exact (4 px per point), so the crop
# maths has no rounding drift. 288 dpi is well above the tablet's ~226 ppi.
EXTRACT_DPI = 288

# Crop this far inside each box. The printed border is centred on the box edge
# (0.5 pt wide), so 1 pt clears the line plus its anti-aliasing.
CROP_INSET_PT = 1.0

# Pixels lighter than this (0 = black, 255 = white) are treated as paper.
# Black pen ink is 0; the printed baseline guide (gray 0.82 = 209) is lighter
# than the cutoff, so it disappears. The border (0.70 = 178) is darker, but it
# is already outside the crop because of CROP_INSET_PT. Between 0 and the
# cutoff, darkness becomes transparency, so anti-aliased stroke edges stay soft.
# 190 was chosen by measurement: at 150, thin 2 pt strokes lost up to 8% of
# their ink at the edges; at 190 they keep 95%+ and the guide is still gone.
INK_CUTOFF = 190

# A sample needs at least this many clearly dark pixels (alpha >= INK_ALPHA)
# to count as handwriting rather than a stray speck.
INK_ALPHA = 128
MIN_INK_PIXELS = 30

# Transparent margin kept around the ink after trimming, in pixels.
TRIM_PADDING_PX = 4

# How far a filled-in PDF's page size may differ from the template's. The
# tablet should return the size unchanged, so anything bigger means the
# crop boxes would no longer line up.
PAGE_SIZE_TOLERANCE_PT = 0.5

# ---------------------------------------------------------------------------
# Spacing analysis (spacing.py)
# ---------------------------------------------------------------------------
STYLE_NAME = "style.json"
SPACING_PREVIEW_PREFIX = "spacing_analysis"   # output/preview/spacing_analysis_<user>.png

# Ink rows closer together than this belong to the same text line (the dot of
# an "i" sits a little above the letter body). Lines are about 60 pt apart.
SPACING_LINE_MERGE_GAP_PT = 6

# A horizontal gap must be at least this many pixels of empty columns to count.
SPACING_MIN_GAP_PX = 1

# Fewer ink pixels than this in the whole sentence area means "no handwriting".
SPACING_MIN_INK_PIXELS = 200

# Smallest sample sizes for each measurement; below these the value is null.
SPACING_MIN_LETTER_GAPS = 5
SPACING_MIN_WORD_GAPS = 2
SPACING_MIN_BASELINE_OBSERVATIONS = 8

# Word gaps count as "clearly separated" from letter gaps when the smallest
# word gap is at least this many times the largest letter gap.
SPACING_WORD_SEPARATION_RATIO = 1.5

# ---------------------------------------------------------------------------
# Rendering (render.py)
# ---------------------------------------------------------------------------
OUTPUT_DIR = BASE_DIR / "output"

# Notebook lines. The writing baseline sits ON each ruled line. This writer's
# glyphs reach about 22 pt above and 8 pt below the baseline, so 36 pt keeps
# neighbouring lines from colliding.
RENDER_LINE_PITCH_PT = 38
RENDER_GLYPH_SCALE = 1.0   # 1.0 = the size the glyphs were written at

# Natural variation added to every glyph occurrence (documented project defaults).
ROTATION_MAX_DEG = 3        # +/- degrees, about the glyph's baseline point
SCALE_JITTER = 0.05         # +/- 5 % uniform scale (never stretched along one axis)
BASELINE_JITTER_PX = 2      # +/- pixels (at EXTRACT_DPI) up or down

# style.json spacing is approximate, so it is clamped to these limits to keep
# an unreliable measurement from producing unreadable text. In points.
LETTER_GAP_LIMITS_PT = (0.5, 6.0)
WORD_GAP_LIMITS_PT = (8.0, 30.0)

# Alpha below this counts as an anti-aliasing fringe, not ink, when measuring
# a glyph's extent for spacing.
GLYPH_INK_ALPHA = 16

# Page appearance (gray levels, 0 = black, 1 = white).
PAPER_GRAY = 0.99
RULE_GRAY = 0.80
RULE_WIDTH = 0.5

# Punctuation has no handwriting samples. It is drawn with a font instead:
# the first existing file in FALLBACK_FONT_PATHS, otherwise Pillow's built-in
# font (no extra file or dependency). Example: "/System/Library/Fonts/Noteworthy.ttc".
FALLBACK_FONT_PATHS = []
FALLBACK_CAP_HEIGHT_RATIO = 0.72   # cap height / font size, to match the writer's capital height
FALLBACK_STROKE_PX = 1             # thickens thin font strokes toward pen weight

# Typographic characters that are rewritten to plain ASCII before rendering.
TYPOGRAPHIC_REPLACEMENTS = {
    "‘": "'", "’": "'", "“": '"', "”": '"',
    "–": "-", "—": "-", "−": "-", "…": "...",
    " ": " ", "\t": " ",
}

# ---------------------------------------------------------------------------
# Baseline normalization (render.py)
# ---------------------------------------------------------------------------
# glyph_metadata.json records where the PRINTED GUIDE was, not where the writer
# actually rested each letter (the samples sit anywhere from 5 pt above to 7 pt
# below it). With normalization on, the renderer instead measures each glyph's
# own ink and anchors it as described in render.normalize_baselines().
BASELINE_NORMALIZE = True        # False = trust the recorded guide exactly (old behaviour)

# Fraction of each sample's own deviation from its target (see above) that is
# kept. 0 = every ordinary glyph sits exactly on the line; 1 = no correction.
BASELINE_KEEP_FRACTION = 0.2

# Letters whose shape has a tail below the writing line.
DESCENDER_CHARS = "gjpqy"

# A descender sample "visibly hangs" if its ink bottom is at least this far
# below the recorded guide. The writer's depth is learned from those samples.
DESCENDER_MIN_HANG_PT = 2.0

# A learned depth is never allowed to exceed this fraction of a glyph's height.
DESCENDER_MAX_FRACTION = 0.5

# ---------------------------------------------------------------------------
# Slide and PDF text extraction (slides.py, slides_pdf.py)
# ---------------------------------------------------------------------------
SLIDES_BULLET_MARKER = "*"          # ASCII marker written for bullets in the notes text
SLIDES_INDENT_SPACES = 3            # spaces of indentation per nesting level in the notes text
SLIDES_MAX_LEVEL = 4                # deeper nesting is flattened to this level
SLIDES_SLIDE_GAP_LINES = 2          # blank lines between slides (one blank line separates elements inside a slide)
SLIDES_FALLBACK_HEADING = "Slide {number}"   # heading used only when no title can be found

# Characters that cleaning rewrites before the text reaches the renderer. Accented
# letters are reduced to their plain ASCII letter automatically (e -> e).
SLIDE_TEXT_REPLACEMENTS = {
    "→": "->", "←": "<-", "↔": "<->", "⇒": "=>",
    "≥": ">=", "≤": "<=", "≠": "!=", "×": "x", "÷": "/",
    "·": "-", "•": "*", "©": "(c)", "®": "(R)", "™": "(TM)",
    "°": " degrees", "½": "1/2", "¼": "1/4", "¾": "3/4",
}

# Reading order of PowerPoint shapes: shapes whose tops are within this distance
# (inches) are treated as one row and read left to right.
PPTX_ROW_TOLERANCE_IN = 0.5
# A shape with no title placeholder can still supply a title if it is a short,
# single-paragraph text in the top part of the slide.
PPTX_TITLE_MAX_WORDS = 15
PPTX_TITLE_ZONE = 0.30              # fraction of slide height
# Two shapes with identical text count as one duplicate when they overlap by this
# fraction of the smaller shape's area.
PPTX_DUPLICATE_OVERLAP = 0.5

# --- PDF heuristics (every value here is a guess; see slides_pdf.py) ---
PDF_TITLE_ZONE = 0.90               # a title may start anywhere above the bottom 10% of the page
PDF_TITLE_MIN_RATIO = 1.25          # ...and be this many times larger than the body text
PDF_HEADING_MIN_RATIO = 1.15        # subheadings: larger than body text by this much
PDF_HEADING_MAX_WORDS = 15
PDF_BULLET_CHARS = "•◦▪■●○‣∙·–—-*►▸◆◇✓✔▶⁃"
PDF_LEVEL_TOLERANCE = 0.015         # x positions closer than this fraction of page width share a level
PDF_COLUMN_GAP = 0.25               # a gap this wide (fraction of page width) can separate columns
PDF_EDGE_ZONE = 0.08                # top/bottom strip searched for page numbers and running footers
PDF_RUNNING_MIN_PAGES = 3           # a footer must repeat on at least this many pages to be dropped
PDF_MIN_PAGE_CHARS = 3              # fewer non-space characters than this = "no text"
PDF_IMAGE_COVERAGE = 0.5            # image area fraction that makes a low-text page "uncertain"
PDF_IMAGE_LOW_TEXT_CHARS = 40
PDF_GARBLED_RATIO = 0.2             # share of replacement/private-use characters that makes a page "uncertain"

# --- Indentation and bullets in the renderer (render.py) ---
RENDER_INDENT_PT_PER_SPACE = 6      # each leading space of a line indents it this far
RENDER_MAX_INDENT_FRACTION = 0.5    # indentation never exceeds this fraction of the line width
RENDER_BULLET_GAP_PT = 8            # gap between a bullet marker and its text

# ---------------------------------------------------------------------------
# Streamlit app (app.py, app_core.py)
# ---------------------------------------------------------------------------
APP_ALLOWED_UPLOADS = (".pptx", ".pdf")
APP_MAX_UPLOAD_MB = 50
APP_DEFAULT_SEED = 7                # same text + same seed = identical handwriting
APP_PREVIEW_DPI = 100               # page previews are drawn at this resolution
APP_PROFILE_NAME_MAX = 32
APP_WORKSPACE_ROOT = None           # None = <system temp folder>/handwriting_notes_app
APP_STALE_WORKSPACE_HOURS = 24      # leftover job folders older than this are removed at start
