"""Generate the handwriting sample template (template.pdf) and its coordinate
file (template_layout.json).

Usage:
    python template.py

The layout is calculated once by build_layout(). Both the PDF and the JSON are
produced from that single result, so the two cannot drift apart.

Coordinates are PDF points with the origin at the bottom-left of each page
(x right, y up). Every rectangle is {x0, y0, x1, y1} with x0 < x1 and y0 < y1.
"""

import json
import math

from reportlab.pdfgen import canvas

import config


def r(value):
    """Round a coordinate so the PDF and the JSON hold identical numbers."""
    return round(value, config.COORD_DECIMALS)


def make_rect(x0, y_top, width, height):
    """Build a rect dict from its left edge, top edge and size.

    Layout is easiest to think about top-down, but the stored y values are
    measured up from the bottom of the page.
    """
    return {"x0": r(x0), "y0": r(y_top - height), "x1": r(x0 + width), "y1": r(y_top)}


def column_geometry():
    """Return (column width, box width) worked out from the page settings."""
    usable_width = config.PAGE_WIDTH - config.MARGIN_LEFT - config.MARGIN_RIGHT
    column_width = (usable_width - (config.COLUMNS - 1) * config.COLUMN_GAP) / config.COLUMNS
    boxes_width = column_width - config.LABEL_WIDTH - config.LABEL_GAP
    box_width = (boxes_width - (config.SAMPLES_PER_CHAR - 1) * config.BOX_GAP) / config.SAMPLES_PER_CHAR
    if box_width <= 0:
        raise ValueError("Boxes have no width left: reduce margins, gaps or label width in config.py")
    return column_width, box_width


def character_boxes(page_number, chars, grid_top, box_height=None):
    """Lay out `chars` in COLUMNS columns, filled top to bottom.

    Returns (boxes, grid_bottom), where grid_bottom is the lowest y used.
    """
    column_width, box_width = column_geometry()
    box_height = box_height or config.BOX_HEIGHT
    rows = math.ceil(len(chars) / config.COLUMNS)
    grid_height = rows * box_height + (rows - 1) * config.ROW_GAP
    available = grid_top - config.MARGIN_BOTTOM
    if grid_height > available:
        raise ValueError(
            f"Page {page_number}: {rows} rows need {grid_height}pt but only "
            f"{available}pt is available. Reduce BOX_HEIGHT or ROW_GAP in config.py"
        )

    boxes = []
    for index, char in enumerate(chars):
        column = index // rows
        row = index % rows
        column_x = config.MARGIN_LEFT + column * (column_width + config.COLUMN_GAP)
        top = grid_top - row * (box_height + config.ROW_GAP)
        for sample in range(config.SAMPLES_PER_CHAR):
            x0 = column_x + config.LABEL_WIDTH + config.LABEL_GAP + sample * (box_width + config.BOX_GAP)
            box = make_rect(x0, top, box_width, box_height)
            box.update({
                "page": page_number,
                "char": char,
                "sample": sample + 1,  # samples are numbered 1..3
                "baseline_y": r(box["y0"] + config.BASELINE_FRACTION * box_height),
            })
            boxes.append(box)
    return boxes, grid_top - grid_height


def sentence_area(page_number, section_top):
    """Lay out the sample sentence area below the digit grid."""
    area_top = section_top - config.SENTENCE_HEADING_HEIGHT
    height = config.SENTENCE_LINES * config.SENTENCE_LINE_PITCH
    if area_top - height < config.MARGIN_BOTTOM:
        raise ValueError(
            "Sample sentence area does not fit on the page. Reduce SENTENCE_LINES, "
            "SENTENCE_LINE_PITCH or SECTION_GAP in config.py"
        )
    width = config.PAGE_WIDTH - config.MARGIN_LEFT - config.MARGIN_RIGHT
    area = make_rect(config.MARGIN_LEFT, area_top, width, height)
    area["page"] = page_number
    area["text"] = config.SAMPLE_SENTENCE
    area["baseline_ys"] = [
        r(area_top - (line + 1) * config.SENTENCE_LINE_PITCH
          + config.BASELINE_FRACTION * config.SENTENCE_LINE_PITCH)
        for line in range(config.SENTENCE_LINES)
    ]
    return area


def build_layout(version=None):
    """Calculate every page, box and the sentence area. Draws nothing.

    `version` picks a template version from config.TEMPLATE_VERSIONS (default: the newest).
    """
    version = version or config.DEFAULT_TEMPLATE_VERSION
    if version not in config.TEMPLATE_VERSIONS:
        raise ValueError(f"Unknown template version {version!r}")
    definition = config.TEMPLATE_VERSIONS[version]
    config.validate_device(config.DEVICE)
    grid_top = config.PAGE_HEIGHT - config.MARGIN_TOP - config.HEADER_HEIGHT

    pages, boxes, sentence = [], [], None
    for number, page in enumerate(definition["pages"], start=1):
        page_boxes, grid_bottom = character_boxes(number, page["chars"], grid_top, page.get("box_height"))
        boxes.extend(page_boxes)
        pages.append({
            "page": number,
            "title": page["title"],
            "width": config.PAGE_WIDTH,
            "height": config.PAGE_HEIGHT,
            **({"note": page["note"]} if page.get("note") else {}),
        })
        if number == definition["sentence_page"]:
            sentence = sentence_area(number, grid_bottom - config.SECTION_GAP)

    return {
        "meta": {
            "schema_version": 1,
            **({"template_version": version} if version > 1 else {}),   # V1 files predate this field
            "device": {
                "key": config.DEVICE,
                "name": config.DEVICE_PROFILE["name"],
                "display_px": list(config.DEVICE_PROFILE["display_px"]),
            },
            "units": "PDF points (1/72 inch)",
            "coordinate_system": (
                "Origin at the bottom-left corner of each page; x increases to the "
                "right, y increases upward. Rects are {x0, y0, x1, y1} with x0 < x1 "
                "and y0 < y1. baseline_y is the y of the printed baseline guide."
            ),
            "page_count": len(pages),
            "box_count": len(boxes),
            "samples_per_char": config.SAMPLES_PER_CHAR,
            "box_border_width": config.BOX_BORDER_WIDTH,
        },
        "pages": pages,
        "boxes": boxes,
        "sample_sentence": sentence,
    }


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

def draw_rect(pdf, rect, line_width, gray):
    pdf.setLineWidth(line_width)
    pdf.setStrokeGray(gray)
    pdf.rect(rect["x0"], rect["y0"], rect["x1"] - rect["x0"], rect["y1"] - rect["y0"], stroke=1, fill=0)


def draw_baseline(pdf, x0, x1, y):
    pdf.setLineWidth(config.BASELINE_WIDTH)
    pdf.setStrokeGray(config.BASELINE_GRAY)
    pdf.line(x0, y, x1, y)


def draw_header_and_footer(pdf, page, page_count):
    top = config.PAGE_HEIGHT - config.MARGIN_TOP
    pdf.setFillGray(config.TEXT_GRAY)
    pdf.setFont(config.FONT_NAME_BOLD, config.TITLE_FONT_SIZE)
    pdf.drawString(config.MARGIN_LEFT, top - config.TITLE_FONT_SIZE, page["title"])
    pdf.setFont(config.FONT_NAME, config.INSTRUCTION_FONT_SIZE)
    pdf.setFillGray(config.PROMPT_GRAY)
    pdf.drawString(
        config.MARGIN_LEFT,
        top - config.TITLE_FONT_SIZE - config.INSTRUCTION_FONT_SIZE - 3,
        page.get("note") or "Write each character inside its box, resting on the light baseline.",
    )
    pdf.setFont(config.FONT_NAME, config.FOOTER_FONT_SIZE)
    pdf.drawString(
        config.MARGIN_LEFT,
        config.FOOTER_Y,
        f"{config.DEVICE_PROFILE['name']} handwriting template - page {page['page']} of {page_count}",
    )


def draw_character_boxes(pdf, boxes):
    pdf.setFont(config.FONT_NAME_BOLD, config.LABEL_FONT_SIZE)
    for box in boxes:
        draw_rect(pdf, box, config.BOX_BORDER_WIDTH, config.BOX_BORDER_GRAY)
        draw_baseline(pdf, box["x0"], box["x1"], box["baseline_y"])
        if box["sample"] == 1:
            # The label sits in the gap to the left of the first box.
            label_x = box["x0"] - config.LABEL_GAP - config.LABEL_WIDTH / 2
            label_y = (box["y0"] + box["y1"]) / 2 - config.LABEL_FONT_SIZE * 0.35
            pdf.setFillGray(config.TEXT_GRAY)
            pdf.setFont(config.FONT_NAME_BOLD, config.LABEL_FONT_SIZE)
            pdf.drawCentredString(label_x, label_y, box["char"])


def draw_sentence_area(pdf, area):
    pdf.setFillGray(config.TEXT_GRAY)
    pdf.setFont(config.FONT_NAME_BOLD, config.SENTENCE_HEADING_FONT_SIZE)
    heading_y = area["y1"] + config.SENTENCE_HEADING_HEIGHT - config.SENTENCE_HEADING_FONT_SIZE - 2
    pdf.drawString(area["x0"], heading_y, "Sample sentence")
    pdf.setFillGray(config.PROMPT_GRAY)
    pdf.setFont(config.FONT_NAME, config.SENTENCE_PROMPT_FONT_SIZE)
    pdf.drawString(area["x0"], heading_y - config.SENTENCE_PROMPT_FONT_SIZE - 4,
                   f'Write naturally, in your normal handwriting: "{area["text"]}"')
    draw_rect(pdf, area, config.BOX_BORDER_WIDTH, config.BOX_BORDER_GRAY)
    for y in area["baseline_ys"]:
        draw_baseline(pdf, area["x0"], area["x1"], y)


def write_pdf(layout, path):
    pdf = canvas.Canvas(str(path), pagesize=(config.PAGE_WIDTH, config.PAGE_HEIGHT))
    pdf.setTitle(f"Handwriting template - {config.DEVICE_PROFILE['name']}")
    for page in layout["pages"]:
        draw_header_and_footer(pdf, page, len(layout["pages"]))
        draw_character_boxes(pdf, [b for b in layout["boxes"] if b["page"] == page["page"]])
        if layout["sample_sentence"] and layout["sample_sentence"]["page"] == page["page"]:
            draw_sentence_area(pdf, layout["sample_sentence"])
        pdf.showPage()
    pdf.save()


def write_json(layout, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2, ensure_ascii=False)
        f.write("\n")


def main():
    layout = build_layout()
    version = layout["meta"].get("template_version", 1)
    folder = config.TEMPLATES_DIR / f"v{version}"
    folder.mkdir(parents=True, exist_ok=True)
    for target in (config.TEMPLATE_PDF, config.LAYOUT_JSON, folder / "template.pdf", folder / "template_layout.json"):
        if target.parent == folder and target.exists() and version == 1:
            raise SystemExit(f"{target} is the preserved V1 layout and is never overwritten")
    write_pdf(layout, config.TEMPLATE_PDF)
    write_json(layout, config.LAYOUT_JSON)
    write_pdf(layout, folder / "template.pdf")
    write_json(layout, folder / "template_layout.json")
    print(f"Device:     {config.DEVICE_PROFILE['name']} ({config.PAGE_WIDTH} x {config.PAGE_HEIGHT} pt)")
    print(f"Pages:      {len(layout['pages'])}")
    print(f"Boxes:      {len(layout['boxes'])} character boxes + 1 sample sentence area")
    print(f"PDF:        {config.TEMPLATE_PDF}")
    print(f"Layout:     {config.LAYOUT_JSON}")


if __name__ == "__main__":
    main()
