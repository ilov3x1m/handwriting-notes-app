"""Extract the handwritten characters from a filled-in template PDF.

Usage:
    python extract.py samples/remarkable_test.pdf --user amaya

Reads template_layout.json, crops every character box from the PDF, removes the
printed template (borders, baseline guides), trims the whitespace and saves

    glyphs/<user>/<char>_<n>.png     transparent PNG, black ink
    glyphs/<user>/glyph_metadata.json

and a contact sheet per page in output/preview/ for checking the result by eye.

Coordinates: the layout JSON uses PDF points with the origin at the BOTTOM-LEFT.
Image pixels have the origin at the TOP-LEFT with y pointing down. The only
place that flips the axis is pdf_y_to_pixel_y() / pdf_rect_to_pixel_box().
"""

import argparse
import json
import os
import re
import sys

import pymupdf
from PIL import Image

import config


class ExtractError(Exception):
    """A problem with the input PDF or layout that the user can fix."""


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------

def glyph_filename(char, sample):
    """File name for one sample.

    macOS volumes are usually case-insensitive, so "A_1.png" and "a_1.png"
    would overwrite each other. Uppercase letters therefore get "_upper".
    """
    if char in config.PUNCTUATION_FILE_NAMES:
        return f"{config.PUNCTUATION_FILE_NAMES[char]}_{sample}.png"
    if char.isupper():
        return f"{char}_upper_{sample}.png"
    return f"{char}_{sample}.png"


def load_layout(path):
    """Load template_layout.json and check it has the fields extraction needs."""
    try:
        with open(path, encoding="utf-8") as f:
            layout = json.load(f)
    except FileNotFoundError:
        raise ExtractError(f"Layout file not found: {path}. Run 'python template.py' first.")
    except json.JSONDecodeError as error:
        raise ExtractError(f"Layout file {path} is not valid JSON: {error}")

    for key in ("meta", "pages", "boxes"):
        if key not in layout:
            raise ExtractError(f"Layout file {path} is missing '{key}'")
    for box in layout["boxes"]:
        for key in ("page", "char", "sample", "x0", "y0", "x1", "y1", "baseline_y"):
            if key not in box:
                raise ExtractError(f"Layout box {box} is missing '{key}'")
    if layout["meta"].get("page_count") != len(layout["pages"]):
        raise ExtractError("Layout meta.page_count does not match the number of pages listed")
    if layout["meta"].get("box_count") != len(layout["boxes"]):
        raise ExtractError("Layout meta.box_count does not match the number of boxes listed")
    layout["meta"].setdefault("template_version", 1)   # V1 layout files predate the field
    return layout


def layout_path_for_pdf(document):
    """Layout file for a filled-in template, chosen by its page count (never by guesswork).

    Each template version has its own coordinates, so a PDF is only ever read with the
    layout it was printed from: 3 pages = V1, 4 pages = V2.
    """
    by_pages = {len(v["pages"]): n for n, v in config.TEMPLATE_VERSIONS.items()}
    version = by_pages.get(document.page_count)
    if version is None:
        known = ", ".join(f"{count} pages (V{n})" for count, n in sorted(by_pages.items()))
        raise ExtractError(f"PDF has {document.page_count} pages but no template version matches. "
                           f"Expected {known}. Use the downloaded blank template without adding or removing pages.")
    return config.TEMPLATES_DIR / f"v{version}" / "template_layout.json"


def open_pdf(path):
    # PyMuPDF has its own FileNotFoundError type, so check first for a clear message.
    if not os.path.isfile(path):
        raise ExtractError(f"PDF not found: {path}")
    try:
        return pymupdf.open(path)
    except Exception as error:  # PyMuPDF raises several different types here
        raise ExtractError(f"Could not open {path} as a PDF: {error}")


def reference_render(layout, page_number, scale):
    """Grayscale render of one page of the BLANK template made from `layout` (for aligning exports)."""
    import tempfile
    import numpy
    import template
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "blank.pdf")
        template.write_pdf(layout, path)
        with pymupdf.open(path) as blank:
            return gray_array(blank[page_number - 1], scale, numpy)


def gray_array(page, scale, numpy):
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csGRAY, alpha=False)
    return numpy.frombuffer(pixmap.samples, dtype=numpy.uint8).reshape(pixmap.height, pixmap.width)


def trim_oversized_page(page, info, layout):
    """Cut an expanded page back to the template area, or return None if that cannot be done safely.

    The reMarkable grows a page's bounds when ink is drawn outside it (a stray mark). The original page
    stays at the PDF's user-space origin, so the template area is (0, 0)-(width, height) in that space.
    That is only accepted if the printed boxes and guides of the blank template really line up there
    (checked against a render of the blank template), so a different or shifted layout is never cropped
    blindly. Only the in-memory page is changed; the uploaded file is never written.
    """
    import numpy
    width, height = info["width"], info["height"]
    box = page.mediabox                                   # PDF coordinates, y up
    if box.x0 > 0.01 or box.y0 > 0.01 or box.x1 < width - 0.01 or box.y1 < height - 0.01:
        return None                                       # the template area is not inside the page bounds
    page.set_cropbox(pymupdf.Rect(0, box.y1 - height, width, box.y1))   # y measured down from the top of the bounds
    scale = 2
    reference = reference_render(layout, info["page"], scale) < 250      # where the template prints lines
    found = gray_array(page, scale, numpy)
    rows, cols = min(found.shape[0], reference.shape[0]), min(found.shape[1], reference.shape[1])
    if abs(found.shape[0] - reference.shape[0]) > 1 or abs(found.shape[1] - reference.shape[1]) > 1:
        return None
    wanted = reference[:rows, :cols]
    score = float((found[:rows, :cols][wanted] < 250).mean())
    return score if score >= config.PAGE_ALIGN_MIN_SCORE else None


def validate_pdf(document, layout):
    """Check the PDF has the pages and page sizes the layout was made for.

    A page that is larger than the template only because of expanded bounds is trimmed back in memory
    when its printed template provably lines up. Returns a list of plain-language notes about that.
    """
    expected_pages = len(layout["pages"])
    if document.page_count != expected_pages:
        raise ExtractError(
            f"PDF has {document.page_count} pages but this template version "
            f"(V{layout['meta'].get('template_version', 1)}) expects {expected_pages}"
        )
    notes = []
    for info in layout["pages"]:
        page = document[info["page"] - 1]
        if page.rotation != 0:
            raise ExtractError(f"Page {info['page']} is rotated {page.rotation} degrees")
        width, height = page.rect.width, page.rect.height
        tolerance = config.PAGE_SIZE_TOLERANCE_PT
        if abs(width - info["width"]) <= tolerance and abs(height - info["height"]) <= tolerance:
            continue
        size = f"Page {info['page']} is {width:.1f} x {height:.1f} pt but the template page is {info['width']:g} x {info['height']:g} pt."
        if width < info["width"] - tolerance or height < info["height"] - tolerance:
            raise ExtractError(size + " It is smaller than the template, so the crop boxes would not line up. "
                               "Upload the PDF exported from the downloaded template at its original size.")
        if trim_oversized_page(page, info, layout) is None:
            raise ExtractError(
                size + " It is larger than the template, probably because something was written outside the page, "
                "and the printed boxes could not be located inside it, so the crop boxes would not line up. "
                "Export the PDF again from the reMarkable after removing any marks outside the page, "
                "and make sure the file was made from the template you downloaded here.")
        notes.append(f"Page {info['page']} was {width:.1f} x {height:.1f} pt (probably a mark outside the page). "
                     f"It was trimmed back to the template area of {info['width']:g} x {info['height']:g} pt, "
                     "and the printed boxes lined up. Your original file was not changed.")
    return notes


# ---------------------------------------------------------------------------
# Coordinates
# ---------------------------------------------------------------------------

def pdf_y_to_pixel_y(y, page_height, scale):
    """PDF y (up from the bottom, points) -> pixel row (down from the top)."""
    return (page_height - y) * scale


def pdf_rect_to_pixel_box(rect, page_height, scale, inset_pt):
    """Turn a layout rect into an image crop box (left, top, right, bottom).

    `scale` is pixels per point. The box is shrunk by `inset_pt` on every side
    so the printed border, which is centred on the rect edge, stays outside.
    """
    left = round((rect["x0"] + inset_pt) * scale)
    right = round((rect["x1"] - inset_pt) * scale)
    # The top edge of the image box comes from the rect's HIGHER y value.
    top = round(pdf_y_to_pixel_y(rect["y1"] - inset_pt, page_height, scale))
    bottom = round(pdf_y_to_pixel_y(rect["y0"] + inset_pt, page_height, scale))
    return left, top, right, bottom


# ---------------------------------------------------------------------------
# Image processing
# ---------------------------------------------------------------------------

def render_page(page, scale):
    """Render a PDF page to a grayscale image at `scale` pixels per point."""
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale),
                             colorspace=pymupdf.csGRAY, alpha=False)
    expected = (round(page.rect.width * scale), round(page.rect.height * scale))
    if abs(pixmap.width - expected[0]) > 1 or abs(pixmap.height - expected[1]) > 1:
        raise ExtractError(f"Rendered size {pixmap.width}x{pixmap.height} is not the expected {expected}")
    return Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)


def ink_to_alpha(gray):
    """Turn a grayscale crop into an alpha mask: dark = opaque, light = clear.

    Anything lighter than INK_CUTOFF (paper, borders, guides) becomes fully
    transparent. Darker pixels get alpha in proportion to their darkness, so
    anti-aliased stroke edges keep their smooth look.
    """
    cutoff = config.INK_CUTOFF
    table = [0 if v >= cutoff else round(255 * (cutoff - v) / cutoff) for v in range(256)]
    return gray.point(table)


def count_ink(alpha):
    """Number of clearly dark pixels in an alpha mask."""
    return sum(alpha.histogram()[config.INK_ALPHA:])


def touches_edge(alpha):
    """True if ink reaches the crop edge, meaning a stroke may have been cut."""
    width, height = alpha.size
    strips = [(0, 0, width, 1), (0, height - 1, width, height),
              (0, 0, 1, height), (width - 1, 0, width, height)]
    return any(alpha.crop(strip).getextrema()[1] >= config.INK_ALPHA for strip in strips)


def trim_alpha(alpha):
    """Cut the mask down to the ink plus a small padding.

    Returns (trimmed RGBA image, (left, top) offset of the trim inside the
    crop), or None if there is no ink at all.
    """
    box = alpha.getbbox()
    if box is None:
        return None
    pad = config.TRIM_PADDING_PX
    left = max(0, box[0] - pad)
    top = max(0, box[1] - pad)
    right = min(alpha.width, box[2] + pad)
    bottom = min(alpha.height, box[3] + pad)
    cropped = alpha.crop((left, top, right, bottom))
    black = Image.new("L", cropped.size, 0)
    return Image.merge("RGBA", (black, black, black, cropped)), (left, top)


def extract_sample(page_image, box, page_height, scale):
    """Extract one character sample.

    Returns a dict. On success it has an "image" plus the data for the
    metadata file; if the box has no handwriting it has "missing" instead.
    """
    crop_box = pdf_rect_to_pixel_box(box, page_height, scale, config.CROP_INSET_PT)
    alpha = ink_to_alpha(page_image.crop(crop_box))
    ink_pixels = count_ink(alpha)
    if ink_pixels < config.MIN_INK_PIXELS:
        return {"missing": f"no handwriting found ({ink_pixels} ink pixels)"}

    image, (trim_left, trim_top) = trim_alpha(alpha)
    # Where the printed baseline falls inside the trimmed PNG (rows from top).
    # It can lie outside the image, e.g. for a letter floating above it.
    baseline_from_top = pdf_y_to_pixel_y(box["baseline_y"], page_height, scale) - crop_box[1] - trim_top
    return {
        "image": image,
        "touches_edge": touches_edge(alpha),
        "record": {
            "char": box["char"],
            "sample": box["sample"],
            "page": box["page"],
            "file": glyph_filename(box["char"], box["sample"]),
            "pdf_bbox": {k: box[k] for k in ("x0", "y0", "x1", "y1")},
            "baseline_y_pdf": box["baseline_y"],
            "crop_px": dict(zip(("left", "top", "right", "bottom"), crop_box)),
            "trim_offset_px": {"left": trim_left, "top": trim_top},
            "image_size_px": {"width": image.width, "height": image.height},
            "baseline_from_top_px": round(baseline_from_top, 2),
            "baseline_from_bottom_px": round(image.height - baseline_from_top, 2),
            "ink_pixels": ink_pixels,
        },
    }


def extract_all(document, layout):
    """Extract every box in the layout.

    Returns (glyphs, missing, warnings). Each glyph is the dict from
    extract_sample(); `missing` lists samples with no handwriting.
    """
    scale = config.EXTRACT_DPI / 72  # points -> pixels
    glyphs, missing, warnings = [], [], []
    for info in layout["pages"]:
        page_image = render_page(document[info["page"] - 1], scale)
        for box in layout["boxes"]:
            if box["page"] != info["page"]:
                continue
            result = extract_sample(page_image, box, info["height"], scale)
            label = f"'{box['char']}' sample {box['sample']} (page {box['page']})"
            if "missing" in result:
                missing.append({"char": box["char"], "sample": box["sample"],
                                "page": box["page"], "reason": result["missing"]})
                warnings.append(f"{label}: {result['missing']}")
                continue
            if result["touches_edge"]:
                warnings.append(f"{label}: ink touches the crop edge, a stroke may be cut off")
            glyphs.append(result)
    return glyphs, missing, warnings


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------

def save_results(glyphs, missing, layout, pdf_path, out_dir):
    """Write the PNGs and glyph_metadata.json. Returns the metadata dict."""
    out_dir.mkdir(parents=True, exist_ok=True)
    # A sample that is blank this time must not leave a file from an earlier run.
    for item in missing:
        (out_dir / glyph_filename(item["char"], item["sample"])).unlink(missing_ok=True)
    for glyph in glyphs:
        glyph["image"].save(out_dir / glyph["record"]["file"])

    metadata = {
        "meta": {
            "source_pdf": str(pdf_path),
            "layout_device": layout["meta"]["device"]["name"],
            "template_version": layout["meta"].get("template_version", 1),
            "units": "pdf_bbox and baseline_y_pdf are PDF points, origin bottom-left. "
                     "All *_px values are pixels, origin top-left, y down.",
            "baseline_note": "baseline_from_top_px is the row of the printed baseline measured from "
                             "the top of the PNG; it can be larger than the image height or negative "
                             "when the handwriting sits above or below the guide. Place a glyph so "
                             "that this row lands on the text baseline.",
            "file_note": "'file' is relative to this metadata file.",
            "dpi": config.EXTRACT_DPI,
            "pixels_per_point": config.EXTRACT_DPI / 72,
            "crop_inset_pt": config.CROP_INSET_PT,
            "ink_cutoff": config.INK_CUTOFF,
            "ink_alpha": config.INK_ALPHA,
            "min_ink_pixels": config.MIN_INK_PIXELS,
            "trim_padding_px": config.TRIM_PADDING_PX,
            "expected_samples": len(layout["boxes"]),
            "extracted_samples": len(glyphs),
            "missing_samples": len(missing),
        },
        "glyphs": [g["record"] for g in glyphs],
        "missing": missing,
    }
    with open(out_dir / config.GLYPH_METADATA_NAME, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
        f.write("\n")
    return metadata


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Extract handwriting glyphs from a filled template PDF.")
    parser.add_argument("pdf", help="filled-in template exported from the reMarkable")
    parser.add_argument("--user", required=True, help="name for the glyph folder, e.g. amaya")
    parser.add_argument("--layout", default=None, help="layout JSON (default: chosen from the PDF page count)")
    parser.add_argument("--no-contact-sheet", action="store_true", help="skip the preview images")
    return parser.parse_args(argv)


def extract_to_directory(pdf_path, layout_path, out_dir, source_label=None):
    """Validate a filled-in template and write its glyphs + metadata into `out_dir`.

    Returns (metadata, warnings). `source_label` is the text recorded as the source PDF
    (default: pdf_path); `layout_path` None picks the layout from the PDF's page count; the app passes a stable name instead of a temporary path.
    """
    document = open_pdf(pdf_path)
    layout = load_layout(layout_path or layout_path_for_pdf(document))
    notes = validate_pdf(document, layout)
    glyphs, missing, warnings = extract_all(document, layout)
    warnings = notes + warnings
    metadata = save_results(glyphs, missing, layout, source_label or pdf_path, out_dir)
    return metadata, warnings


def run(args):
    # The name becomes a folder name, so keep it to plain characters.
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.user):
        raise ExtractError("--user may only contain letters, digits, '_' and '-'")

    out_dir = config.GLYPHS_DIR / args.user
    metadata, warnings = extract_to_directory(args.pdf, args.layout, out_dir)
    missing = metadata["missing"]

    for warning in warnings:
        print(f"WARNING: {warning}")
    print(f"Extracted {metadata['meta']['extracted_samples']} of {metadata['meta']['expected_samples']} samples into {out_dir}")
    if missing:
        names = ", ".join(f"{m['char']}_{m['sample']}" for m in missing)
        print(f"Missing {len(missing)} samples: {names}")
    print(f"Metadata:  {out_dir / config.GLYPH_METADATA_NAME}")

    if not args.no_contact_sheet:
        import contact_sheet  # only needed for the preview
        for path in contact_sheet.make_sheets(out_dir, args.user, metadata):
            print(f"Contact sheet: {path}")
    return 0 if not missing else 2


def main(argv=None):
    try:
        return run(parse_args(argv))
    except ExtractError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
