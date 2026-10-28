"""Render plain text as a multipage PDF in the user's handwriting.

Usage:
    python render.py --text "Hello world." --user amaya
    python render.py --input samples/notes.txt --user amaya --output output/notes.pdf --seed 7

Reads glyphs/<user>/glyph_metadata.json (which glyph image is which character,
and where its baseline is) and glyphs/<user>/style.json (measured letter and
word spacing). Never builds glyph file names by hand.

How a page is made
  1. Each character gets a random handwritten sample (never the same one twice
     in a row) with a small random rotation, scale and baseline shift.
  2. Letters are placed left to right with the measured letter gap, words with
     the measured word gap, wrapping at the right margin.
  3. Each glyph is positioned so its recorded baseline row lands on the ruled
     line, so descenders hang below the line instead of being bottom-aligned.
  4. The ruled paper is drawn as vector lines; the handwriting is a separate
     transparent image layer on top. reportlab writes the PDF.

Units: layout is done in pixels at EXTRACT_DPI (4 px per point). Glyph pixels
come from glyph_metadata.json (pixels_per_point), page sizes from config in
points; px_to_pdf_y() is the one place pixel rows turn into PDF heights.

Unsupported characters: letters and digits need handwritten samples. Other
ASCII symbols and punctuation are drawn with a fallback font (see config).
Typographic quotes and dashes become ASCII; any other character becomes "?"
with a warning. Each newline in the input starts a new line; blank lines leave
a blank ruled line; runs of spaces count as one word gap.
"""

import argparse
import json
import math
import os
import random
import re
import string
import sys

import pymupdf
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

import config
from extract import ExtractError


# ---------------------------------------------------------------------------
# Loading the handwriting set
# ---------------------------------------------------------------------------

def load_glyph_library(glyph_dir):
    """Load every glyph image listed in glyph_metadata.json.

    Returns {"pixels_per_point": float, "samples": {char: [glyph, ...]}} where each
    glyph is {"char", "sample", "image" (RGBA), "baseline_row"} and baseline_row is
    the row of the printed baseline measured down from the image top.
    """
    path = glyph_dir / config.GLYPH_METADATA_NAME
    if not glyph_dir.is_dir():
        raise ExtractError(f"No glyph folder {glyph_dir}. Run extract.py for this user first.")
    try:
        with open(path, encoding="utf-8") as f:
            metadata = json.load(f)
    except FileNotFoundError:
        raise ExtractError(f"{path} not found. Run extract.py for this user first.")
    except json.JSONDecodeError as error:
        raise ExtractError(f"{path} is not valid JSON: {error}")

    try:
        pixels_per_point = float(metadata["meta"]["pixels_per_point"])
        records = metadata["glyphs"]
    except (KeyError, TypeError, ValueError):
        raise ExtractError(f"{path} is missing meta.pixels_per_point or the glyphs list")
    if pixels_per_point <= 0 or not records:
        raise ExtractError(f"{path} has no usable glyphs")

    samples = {}
    for record in records:
        try:
            char, name = record["char"], record["file"]
            baseline_row = float(record["baseline_from_top_px"])
            size = (record["image_size_px"]["width"], record["image_size_px"]["height"])
        except (KeyError, TypeError, ValueError):
            raise ExtractError(f"{path}: glyph record {record!r} is missing required fields")
        image_path = glyph_dir / name  # the manifest, not the character, decides the file name
        try:
            image = Image.open(image_path).convert("RGBA")
        except (FileNotFoundError, OSError):
            raise ExtractError(f"Glyph image {image_path} listed in the metadata cannot be read")
        if image.size != size:
            raise ExtractError(f"{image_path} is {image.size} but the metadata says {size}")
        samples.setdefault(char, []).append(
            {"char": char, "sample": record.get("sample"), "image": image, "baseline_row": baseline_row})
    return {"pixels_per_point": pixels_per_point, "samples": samples}


def load_style(glyph_dir):
    """Read letter and word gaps (points) from style.json, clamped to config limits.

    Returns {"letter_gap_pt", "word_gap_pt", "warnings"}. Missing or unusable
    measurements are errors: arbitrary spacing is never substituted.
    """
    path = glyph_dir / config.STYLE_NAME
    try:
        with open(path, encoding="utf-8") as f:
            style = json.load(f)
    except FileNotFoundError:
        raise ExtractError(f"{path} not found. Run spacing.py for this user first.")
    except json.JSONDecodeError as error:
        raise ExtractError(f"{path} is not valid JSON: {error}")

    warnings = []
    values = {}
    for key, field, limits in (("letter_gap_pt", ("letter_spacing", "estimate_pt"), config.LETTER_GAP_LIMITS_PT),
                               ("word_gap_pt", ("word_spacing", "mean_pt"), config.WORD_GAP_LIMITS_PT)):
        try:
            value = style[field[0]][field[1]]
            quality = style[field[0]].get("quality")
        except (KeyError, TypeError, AttributeError):
            raise ExtractError(f"{path} is missing {field[0]}.{field[1]}")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or quality == "insufficient":
            raise ExtractError(f"{path}: {field[0]}.{field[1]} is not a usable measurement "
                               f"(value {value!r}, quality {quality!r}). Re-run spacing.py with a better sample.")
        clamped = min(max(float(value), limits[0]), limits[1])
        if clamped != value:
            warnings.append(f"{field[0]} {value} pt is outside the allowed {limits[0]}-{limits[1]} pt; using {clamped} pt.")
        values[key] = clamped
    values["warnings"] = warnings
    return values


# ---------------------------------------------------------------------------
# Text preparation
# ---------------------------------------------------------------------------

def normalize_text(text, keep=""):
    """Replace typographic characters; anything else non-ASCII becomes "?".

    `keep` lists non-ASCII characters that must survive because the glyph set can draw them
    (the handwritten bullet). Returns (text, warnings).
    """
    for old, new in config.TYPOGRAPHIC_REPLACEMENTS.items():
        text = text.replace(old, new)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    odd = sorted({c for c in text if c != "\n" and c not in keep and not (c == " " or 32 < ord(c) < 127)})
    warnings = []
    if odd:
        warnings.append("Unsupported characters replaced with '?': " + " ".join(repr(c) for c in odd))
        text = "".join("?" if c in odd else c for c in text)
    return text, warnings


def split_paragraphs(text):
    """One entry per input line: a list of words, or [] for a blank line."""
    return [line.split() for line in text.split("\n")]


def split_indents(text):
    """Number of leading spaces of each input line (same length as split_paragraphs)."""
    return [len(line) - len(line.lstrip(" ")) for line in text.split("\n")]


# A list marker at the start of a line: "*", "-", "1.", "2)", "a." or "A)".
LIST_MARKER = re.compile(r"^(?:[*\u2022-]|\d{1,3}[.)]|[a-z][.)]|[A-Z]\))$")
BULLET = "\u2022"


def check_letters_available(text, library):
    """Letters and digits cannot be faked with a font: every one needs a sample."""
    missing = sorted({c for c in text if c.isalnum() and c not in library["samples"]})
    if missing:
        raise ExtractError("The glyph set has no handwriting sample for: " + " ".join(missing)
                           + ". Re-run extract.py with a complete template, or remove these characters.")


# ---------------------------------------------------------------------------
# Glyph images: fallback font, random transform
# ---------------------------------------------------------------------------

def load_fallback_font(size_px):
    for path in config.FALLBACK_FONT_PATHS:
        try:
            return ImageFont.truetype(path, size_px)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size_px)  # Pillow's built-in scalable font
    except TypeError:
        raise ExtractError("This Pillow version has no scalable built-in font. Set FALLBACK_FONT_PATHS in config.py.")


def make_fallback_glyph(char, font):
    """Draw one punctuation character as a glyph dict, baseline at the font's baseline."""
    stroke = config.FALLBACK_STROKE_PX
    left, top, right, bottom = font.getbbox(char, anchor="ls", stroke_width=stroke)
    pad = 2
    image = Image.new("L", (right - left + 2 * pad, bottom - top + 2 * pad), 0)
    ImageDraw.Draw(image).text((-left + pad, -top + pad), char, fill=255, font=font, anchor="ls",
                               stroke_width=stroke, stroke_fill=255)
    rgba = Image.merge("RGBA", (Image.new("L", image.size, 0),) * 3 + (image,))
    return {"char": char, "sample": None, "image": rgba, "baseline_row": float(-top + pad)}


def ink_box(image):
    """Bounding box (left, top, right, bottom) of the visible ink in an RGBA image."""
    mask = image.getchannel("A").point(lambda a: 255 if a >= config.GLYPH_INK_ALPHA else 0)
    return mask.getbbox()


def normalize_baselines(library):
    """Choose, for every glyph, the image row that should sit on the ruled line.

    The recorded baseline is the printed guide, and the writer did not rest letters
    on it consistently, so using it directly makes the text bob up and down. Instead:
      * ordinary letters and digits have a target of 0: their lowest ink sits on the line;
      * descender letters (config.DESCENDER_CHARS) have a target depth below the line,
        learned from this writer's own samples that visibly hang below the guide
        (their median), capped at DESCENDER_MAX_FRACTION of the glyph's height. The
        recorded guide cannot tell us a descender's depth for samples drawn above it.
    The ink then lands at  target + BASELINE_KEEP_FRACTION * (sample's own deviation
    from target)  so a little of each sample's natural lean survives.

    Each glyph gets baseline_row (used for placement), recorded_baseline_row (the
    original, untouched), ink_top/ink_bottom and landing_px. Returns a list of warnings.
    """
    warnings = []
    px_per_pt = library["pixels_per_point"]
    groups = library["samples"]
    for group in groups.values():
        for glyph in group:
            top, bottom = ink_box(glyph["image"])[1::2]
            glyph["recorded_baseline_row"] = glyph["baseline_row"]
            glyph["ink_top"], glyph["ink_bottom"] = top, bottom
            glyph["hang_px"] = bottom - glyph["recorded_baseline_row"]  # + = ink ends below the guide
    if not config.BASELINE_NORMALIZE:
        for group in groups.values():
            for glyph in group:
                glyph["landing_px"] = glyph["hang_px"]
        return warnings
    # Punctuation is not "ink on the line": commas hang, quotes float. Its recorded guide-relative
    # placement is kept as written (see below), so it takes no part in the letter statistics.

    hanging = [g["hang_px"] for char in config.DESCENDER_CHARS for g in groups.get(char, [])
               if g["hang_px"] >= config.DESCENDER_MIN_HANG_PT * px_per_pt]
    depth = sorted(hanging)[len(hanging) // 2] if hanging else None
    needed = [c for c in config.DESCENDER_CHARS if c in groups]
    if depth is None and needed:
        warnings.append("No descender sample visibly hangs below the guide, so descender depth is unknown; "
                        "descender letters keep their recorded baseline.")

    for char, group in groups.items():
        for glyph in group:
            if char in config.PUNCTUATION_CHARS:
                glyph["landing_px"] = glyph["hang_px"]            # baseline_row stays the recorded guide row
                continue
            if char in config.DESCENDER_CHARS:
                if depth is None:
                    glyph["landing_px"] = glyph["hang_px"]  # nothing better is known
                    glyph["baseline_row"] = glyph["recorded_baseline_row"]
                    continue
                target = min(depth, config.DESCENDER_MAX_FRACTION * (glyph["ink_bottom"] - glyph["ink_top"]))
            else:
                target = 0.0
            glyph["landing_px"] = target + config.BASELINE_KEEP_FRACTION * (glyph["hang_px"] - target)
            glyph["baseline_row"] = glyph["ink_bottom"] - glyph["landing_px"]
    return warnings


def transform_glyph(glyph, rng, base_scale, jitter=True):
    """Scale, rotate and trim one glyph occurrence.

    Rotation is about the point on the baseline under the glyph's centre, so the
    glyph tilts in place on the line. The image is trimmed to its visible ink and
    baseline_row is updated for the trim, so layout can rely on both.
    """
    if jitter:
        scale = base_scale * (1 + rng.uniform(-config.SCALE_JITTER, config.SCALE_JITTER))
        angle = rng.uniform(-config.ROTATION_MAX_DEG, config.ROTATION_MAX_DEG)
        dy = rng.uniform(-config.BASELINE_JITTER_PX, config.BASELINE_JITTER_PX)
    else:
        scale, angle, dy = base_scale, 0.0, 0.0

    source = glyph["image"]
    width, height = max(1, round(source.width * scale)), max(1, round(source.height * scale))
    scaled = source.resize((width, height), Image.LANCZOS)  # one scale for both axes: shape is kept
    baseline = glyph["baseline_row"] * height / source.height

    # Transparent border big enough that the rotation can never push ink off the canvas.
    pivot = (width / 2, baseline)
    reach = max(math.hypot(x - pivot[0], y - pivot[1]) for x in (0, width) for y in (0, height))
    margin = math.ceil(reach * math.radians(config.ROTATION_MAX_DEG)) + 2
    canvas_image = Image.new("RGBA", (width + 2 * margin, height + 2 * margin), (0, 0, 0, 0))
    canvas_image.paste(scaled, (margin, margin))
    if angle:
        canvas_image = canvas_image.rotate(angle, resample=Image.BICUBIC, center=(pivot[0] + margin, pivot[1] + margin))

    box = ink_box(canvas_image) or (0, 0, canvas_image.width, canvas_image.height)
    return {"char": glyph["char"], "sample": glyph["sample"], "image": canvas_image.crop(box),
            "baseline_row": margin + baseline - box[1], "dy": dy, "angle": angle, "scale": scale}


# ---------------------------------------------------------------------------
# Layout context
# ---------------------------------------------------------------------------

def build_context(library, style, seed=None):
    """Everything layout needs: page geometry in pixels, gaps, random source, extents."""
    px = config.EXTRACT_DPI / 72  # render pixels per point
    glyph_px = px * config.RENDER_GLYPH_SCALE / library["pixels_per_point"]
    rng = random.Random(seed)

    # Fallback glyphs are sized so their cap height matches the writer's capitals.
    capitals = [g["baseline_row"] * glyph_px for c, group in library["samples"].items() if c.isupper() for g in group]
    cap_height_px = sorted(capitals)[len(capitals) // 2] if capitals else 14 * px
    font = load_fallback_font(max(8, round(cap_height_px / config.FALLBACK_CAP_HEIGHT_RATIO)))
    fallback = {c: make_fallback_glyph(c, font) for c in string.punctuation if c not in library["samples"]}

    # Worst-case extent above/below the baseline, including scale, rotation and jitter.
    handwritten = [g for group in library["samples"].values() for g in group]
    scaled_up = glyph_px * (1 + config.SCALE_JITTER)
    above = below = reach = 0
    for glyphs, factor in ((handwritten, scaled_up), (list(fallback.values()), 1.0)):
        if not glyphs:
            continue
        above = max(above, max(g["baseline_row"] for g in glyphs) * factor)
        below = max(below, max(g["image"].height - g["baseline_row"] for g in glyphs) * factor)
        reach = max(reach, max(math.hypot(g["image"].width, g["image"].height) for g in glyphs) * factor)
    allowance = math.ceil(reach * math.radians(config.ROTATION_MAX_DEG)) + config.BASELINE_JITTER_PX + 2
    above, below = math.ceil(above + allowance), max(0, math.ceil(below + allowance))

    page_w, page_h = round(config.PAGE_WIDTH * px), round(config.PAGE_HEIGHT * px)
    left, right = config.MARGIN_LEFT * px, (config.PAGE_WIDTH - config.MARGIN_RIGHT) * px
    pitch = config.RENDER_LINE_PITCH_PT * px
    first = config.MARGIN_TOP * px + above
    bottom_limit = (config.PAGE_HEIGHT - config.MARGIN_BOTTOM) * px
    baselines = []
    row = first
    while row + below <= bottom_limit:
        baselines.append(row)
        row += pitch
    if not baselines:
        raise ExtractError("No writing line fits between the page margins; reduce margins or glyph size in config.py")

    warnings = list(style["warnings"])
    if above + below > pitch:
        warnings.append(f"Tallest and deepest glyphs ({(above + below) / px:.0f} pt together) can exceed the "
                        f"{config.RENDER_LINE_PITCH_PT} pt line pitch; neighbouring lines might touch.")
    return {
        "px": px, "glyph_px": glyph_px, "rng": rng, "library": library, "fallback": fallback,
        "page_size": (page_w, page_h), "left": left, "right": right, "baselines": baselines,
        "letter_gap": style["letter_gap_pt"] * px, "word_gap": style["word_gap_pt"] * px,
        "indent_px": config.RENDER_INDENT_PT_PER_SPACE * px, "bullet_gap": config.RENDER_BULLET_GAP_PT * px,
        "last_sample": {}, "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Building words and laying them out
# ---------------------------------------------------------------------------

def choose_sample(char, ctx):
    """Random sample of `char`, avoiding the one used for its previous occurrence."""
    group = ctx["library"]["samples"][char]
    last = ctx["last_sample"].get(char)
    options = [i for i in range(len(group)) if i != last] or [0]
    index = ctx["rng"].choice(options)
    ctx["last_sample"][char] = index
    return group[index]


def build_item(char, ctx):
    if char in ctx["library"]["samples"]:
        return transform_glyph(choose_sample(char, ctx), ctx["rng"], ctx["glyph_px"])
    # No jitter for punctuation: tiny marks look worse tilted, and it keeps them on the line.
    return transform_glyph(ctx["fallback"][char], ctx["rng"], 1.0, jitter=False)


def word_width(items, letter_gap):
    return sum(i["image"].width for i in items) + letter_gap * (len(items) - 1)


def layout_text(paragraphs, ctx, indents=None):
    """Place every word. Returns a list of pages: {"lines": [{"baseline", "items": [(x_left, item)]}]}.

    Nothing is dropped: a full line wraps, a full page starts a new page, and a word wider
    than a line is broken between characters.

    `indents` (optional, one number of leading spaces per paragraph) indents a line. A line
    that starts with a list marker ("*", "-", "1.") gets a small fixed gap after the marker, and
    its wrapped lines hang under the text instead of under the marker.
    """
    pages = []
    state = {"page": None, "line_index": -1, "line": None}
    indents = indents or [0] * len(paragraphs)
    max_indent = config.RENDER_MAX_INDENT_FRACTION * (ctx["right"] - ctx["left"])

    def new_page():
        state["page"] = {"lines": [], "baselines": ctx["baselines"]}
        pages.append(state["page"])
        state["line_index"] = -1

    def advance_line(blank=False):
        """Move to the next ruled line; returns False if a blank line was swallowed at a page bottom."""
        if state["page"] is None:
            new_page()
        nxt = state["line_index"] + 1
        if nxt >= len(ctx["baselines"]):
            if blank:
                state["line_index"] = len(ctx["baselines"])  # whitespace only; the next text starts a new page
                return False
            new_page()
            nxt = 0
        state["line_index"] = nxt
        state["line"] = {"baseline": ctx["baselines"][nxt], "items": []}
        state["page"]["lines"].append(state["line"])
        return True

    for words, indent in zip(paragraphs, indents):
        if not words:
            advance_line(blank=True)
            continue
        advance_line()
        line_start = ctx["left"] + min(indent * ctx["indent_px"], max_indent)
        wrap_start = line_start                      # where wrapped lines begin (moved right for bullets)
        has_marker = len(words) > 1 and LIST_MARKER.match(words[0]) is not None
        x, at_start = line_start, True
        for index, word in enumerate(words):
            if has_marker and index == 0 and word == "*" and BULLET in ctx["library"]["samples"]:
                word = BULLET       # a list-marker asterisk is drawn as the handwritten bullet
            items = [build_item(c, ctx) for c in word]
            width = word_width(items, ctx["letter_gap"])
            gap = ctx["bullet_gap"] if has_marker and index == 1 else ctx["word_gap"]
            if not at_start and x + gap + width > ctx["right"]:
                advance_line()
                x, at_start = wrap_start, True
            x += 0 if at_start else gap

            if width <= ctx["right"] - x:
                for item in items:
                    state["line"]["items"].append((x, item))
                    x += item["image"].width + ctx["letter_gap"]
            else:
                # One word wider than a whole line: break it between characters.
                for item in items:
                    if x + item["image"].width > ctx["right"] and x > wrap_start:
                        advance_line()
                        x = wrap_start
                    state["line"]["items"].append((x, item))
                    x += item["image"].width + ctx["letter_gap"]
            x -= ctx["letter_gap"]  # the gap after a word's last glyph is replaced by the word gap
            at_start = False
            if has_marker and index == 0:
                wrap_start = x + ctx["bullet_gap"]   # text after the marker starts here; wrapped lines align to it
    return pages


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def paste_position(x_left, baseline, item):
    """Top-left pixel where an item's image is pasted so its baseline row sits on the line."""
    return round(x_left), round(baseline + item["dy"] - item["baseline_row"])


def compose_page(page, ctx):
    """Handwriting layer for one page: transparent RGBA, writing only (no paper, no rules)."""
    layer = Image.new("RGBA", ctx["page_size"], (0, 0, 0, 0))
    for line in page["lines"]:
        for x_left, item in line["items"]:
            layer.paste(item["image"], paste_position(x_left, line["baseline"], item), item["image"])
    return layer


def px_to_pdf_y(row_px, ctx):
    """Pixel row (down from the page top) -> PDF y (up from the page bottom), in points."""
    return config.PAGE_HEIGHT - row_px / ctx["px"]


def write_pdf(pages, ctx, path):
    """Write the multipage PDF: paper and ruled lines as vector graphics, handwriting on top."""
    # invariant=1 removes timestamps, so the same seed gives byte-identical files.
    pdf = canvas.Canvas(str(path), pagesize=(config.PAGE_WIDTH, config.PAGE_HEIGHT), invariant=1)
    pdf.setTitle("Handwritten notes")
    for page in pages:
        pdf.setFillGray(config.PAPER_GRAY)
        pdf.rect(0, 0, config.PAGE_WIDTH, config.PAGE_HEIGHT, stroke=0, fill=1)
        pdf.setStrokeGray(config.RULE_GRAY)
        pdf.setLineWidth(config.RULE_WIDTH)
        for baseline in page["baselines"]:
            y = px_to_pdf_y(baseline, ctx)
            pdf.line(0, y, config.PAGE_WIDTH, y)
        pdf.drawImage(ImageReader(compose_page(page, ctx)), 0, 0, config.PAGE_WIDTH, config.PAGE_HEIGHT, mask="auto")
        pdf.showPage()
    pdf.save()


def write_previews(pdf_path, stem):
    """Rasterise the PDF to PNGs in output/preview/ for checking by eye."""
    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    scale = config.PREVIEW_DPI / 72
    paths = []
    with pymupdf.open(pdf_path) as document:
        for number, page in enumerate(document, start=1):
            path = config.PREVIEW_DIR / f"render_{stem}_page{number}.png"
            page.get_pixmap(matrix=pymupdf.Matrix(scale, scale)).save(path)
            paths.append(path)
    return paths


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------

def render_text(text, glyph_dir, seed=None):
    """Whole pipeline up to (not including) the PDF. Returns (pages, ctx, warnings, normalized text)."""
    if not text.strip():
        raise ExtractError("The text to render is empty.")
    library = load_glyph_library(glyph_dir)
    baseline_warnings = normalize_baselines(library)
    style = load_style(glyph_dir)
    text, warnings = normalize_text(text, keep=BULLET if BULLET in library["samples"] else "")
    check_letters_available(text, library)
    ctx = build_context(library, style, seed)
    pages = layout_text(split_paragraphs(text), ctx, split_indents(text))
    return pages, ctx, ctx["warnings"] + baseline_warnings + warnings, text


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Render plain text in your handwriting.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--text", help="text to render")
    source.add_argument("--input", help="UTF-8 text file to render")
    parser.add_argument("--user", required=True, help="glyph folder name, e.g. amaya")
    parser.add_argument("--output", help="output PDF (default: output/<name>_<user>.pdf)")
    parser.add_argument("--seed", type=int, help="random seed, for a reproducible result")
    parser.add_argument("--preview", action="store_true", help="also write PNG previews to output/preview/")
    return parser.parse_args(argv)


def read_input(args):
    if args.text is not None:
        return args.text, "text"
    if not os.path.isfile(args.input):
        raise ExtractError(f"Input file not found: {args.input}")
    try:
        with open(args.input, encoding="utf-8") as f:
            return f.read(), os.path.splitext(os.path.basename(args.input))[0]
    except UnicodeDecodeError:
        raise ExtractError(f"{args.input} is not UTF-8 text")


def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.user):
        raise ExtractError("--user may only contain letters, digits, '_' and '-'")
    text, name = read_input(args)
    pages, ctx, warnings, _ = render_text(text, config.GLYPHS_DIR / args.user, args.seed)

    output = args.output and os.path.abspath(args.output) or str(config.OUTPUT_DIR / f"{name}_{args.user}.pdf")
    os.makedirs(os.path.dirname(output), exist_ok=True)
    write_pdf(pages, ctx, output)

    for warning in warnings:
        print(f"WARNING: {warning}")
    print(f"Rendered {sum(len(l['items']) for p in pages for l in p['lines'])} characters "
          f"on {len(pages)} page(s), {config.PAGE_WIDTH:g} x {config.PAGE_HEIGHT:g} pt")
    print(f"PDF: {output}")
    if args.preview:
        for path in write_previews(output, os.path.splitext(os.path.basename(output))[0]):
            print(f"Preview: {path}")
    return 0


def main(argv=None):
    try:
        return run(parse_args(argv))
    except ExtractError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
