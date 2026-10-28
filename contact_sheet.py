"""Draw contact sheets of extracted glyphs for checking them by eye.

Usage:
    python contact_sheet.py amaya

(extract.py also calls make_sheets() after every run.)

Each cell shows the crop area that extract.py cut from the template box, so
you can see at a glance:
    gray frame   the crop edge. Ink touching it means a stroke was cut off.
    red line     the printed baseline, taken from glyph_metadata.json.
    blue frame   the trimmed PNG. Lots of space inside = excess whitespace.
    pink cell    a missing sample.
One PNG is written per template page into output/preview/.
"""

import json
import sys

from PIL import Image, ImageDraw, ImageFont

import config

SHEET_SCALE = 0.5  # shrink the 288 dpi glyphs so a whole page fits on screen
LABEL_WIDTH = 22
CELL_GAP = 4
GROUP_GAP = 14
ROW_GAP = 5
HEADER_HEIGHT = 44
COLUMNS = 2  # matches the template: two columns of characters
BACKGROUND = (255, 255, 255)
CROP_EDGE = (150, 150, 150)
BASELINE = (230, 0, 0)
TRIM_BOX = (0, 90, 255)
MISSING_FILL = (255, 215, 215)


def load_font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow without sized default fonts
        return ImageFont.load_default()


def cell_size(glyph_records):
    """Cell size in sheet pixels, taken from the crop size used for extraction."""
    crop = glyph_records[0]["crop_px"] if glyph_records else {"left": 0, "top": 0, "right": 212, "bottom": 124}
    return (round((crop["right"] - crop["left"]) * SHEET_SCALE),
            round((crop["bottom"] - crop["top"]) * SHEET_SCALE))


def draw_glyph_cell(sheet, origin, size, record, glyph_dir):
    """Draw one extracted glyph inside its crop frame."""
    x, y = origin
    cell = Image.new("RGB", size, BACKGROUND)
    draw = ImageDraw.Draw(cell)

    # Baseline row inside the crop = trim offset + baseline row inside the PNG.
    baseline_row = (record["trim_offset_px"]["top"] + record["baseline_from_top_px"]) * SHEET_SCALE
    draw.line([(0, baseline_row), (size[0], baseline_row)], fill=BASELINE, width=1)

    glyph = Image.open(glyph_dir / record["file"]).convert("RGBA")
    shrunk = glyph.resize((max(1, round(glyph.width * SHEET_SCALE)),
                           max(1, round(glyph.height * SHEET_SCALE))), Image.LANCZOS)
    left = round(record["trim_offset_px"]["left"] * SHEET_SCALE)
    top = round(record["trim_offset_px"]["top"] * SHEET_SCALE)
    cell.paste(shrunk, (left, top), shrunk)
    draw.rectangle([left, top, left + shrunk.width - 1, top + shrunk.height - 1], outline=TRIM_BOX)
    draw.rectangle([0, 0, size[0] - 1, size[1] - 1], outline=CROP_EDGE)
    sheet.paste(cell, (x, y))


def draw_missing_cell(sheet, origin, size, font):
    x, y = origin
    draw = ImageDraw.Draw(sheet)
    draw.rectangle([x, y, x + size[0] - 1, y + size[1] - 1], fill=MISSING_FILL, outline=BASELINE)
    draw.text((x + size[0] / 2, y + size[1] / 2), "MISSING", fill=BASELINE, font=font, anchor="mm")


def make_sheet(page, records, missing, glyph_dir, title):
    """Build the contact sheet image for one template page."""
    size = cell_size(records)
    chars = sorted({r["char"] for r in records} | {m["char"] for m in missing})
    rows = -(-len(chars) // COLUMNS)
    group_width = LABEL_WIDTH + 3 * size[0] + 2 * CELL_GAP
    width = COLUMNS * group_width + (COLUMNS - 1) * GROUP_GAP + 2 * GROUP_GAP
    height = HEADER_HEIGHT + rows * (size[1] + ROW_GAP) + GROUP_GAP

    sheet = Image.new("RGB", (width, height), BACKGROUND)
    draw = ImageDraw.Draw(sheet)
    font, small = load_font(16), load_font(12)
    draw.text((GROUP_GAP, 6), title, fill=(0, 0, 0), font=font)
    draw.text((GROUP_GAP, 26), "gray = crop edge   red = baseline   blue = trimmed PNG   pink = missing",
              fill=(90, 90, 90), font=small)

    by_key = {(r["char"], r["sample"]): r for r in records}
    missing_keys = {(m["char"], m["sample"]) for m in missing}
    for index, char in enumerate(chars):
        column, row = divmod(index, rows)
        x0 = GROUP_GAP + column * (group_width + GROUP_GAP)
        y0 = HEADER_HEIGHT + row * (size[1] + ROW_GAP)
        draw.text((x0 + LABEL_WIDTH / 2, y0 + size[1] / 2), char, fill=(0, 0, 0), font=font, anchor="mm")
        for sample in (1, 2, 3):
            origin = (x0 + LABEL_WIDTH + (sample - 1) * (size[0] + CELL_GAP), y0)
            if (char, sample) in by_key:
                draw_glyph_cell(sheet, origin, size, by_key[(char, sample)], glyph_dir)
            elif (char, sample) in missing_keys:
                draw_missing_cell(sheet, origin, size, small)
    return sheet


def make_sheets(glyph_dir, user, metadata):
    """Write one contact sheet per page. Returns the list of file paths."""
    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    pages = sorted({g["page"] for g in metadata["glyphs"]} | {m["page"] for m in metadata["missing"]})
    paths = []
    for page in pages:
        records = [g for g in metadata["glyphs"] if g["page"] == page]
        missing = [m for m in metadata["missing"] if m["page"] == page]
        title = f"{user} - page {page}: {len(records)} extracted, {len(missing)} missing"
        sheet = make_sheet(page, records, missing, glyph_dir, title)
        path = config.PREVIEW_DIR / f"glyphs_{user}_page{page}.png"
        sheet.save(path)
        paths.append(path)
    return paths


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python contact_sheet.py <user>")
    user = sys.argv[1]
    glyph_dir = config.GLYPHS_DIR / user
    try:
        with open(glyph_dir / config.GLYPH_METADATA_NAME, encoding="utf-8") as f:
            metadata = json.load(f)
    except FileNotFoundError:
        sys.exit(f"No {config.GLYPH_METADATA_NAME} in {glyph_dir}. Run extract.py first.")
    for path in make_sheets(glyph_dir, user, metadata):
        print(path)


if __name__ == "__main__":
    main()
