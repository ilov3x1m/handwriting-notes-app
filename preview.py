"""Render template.pdf to PNGs and draw the JSON rectangles on top.

Usage:
    python preview.py

Writes, for each page, into output/preview/:
    page_N.png     the PDF exactly as rendered
    overlay_N.png  the same page with every JSON rectangle drawn in colour

Red = character boxes, blue = sample sentence area, green = baseline y values.
If the exported coordinates are right, the red/blue lines sit exactly on the
gray printed borders. Any gap, offset or size mismatch shows as a second line.
"""

import json

import pymupdf
from PIL import Image, ImageDraw

import config

RED = (230, 0, 0)
BLUE = (0, 90, 255)
GREEN = (0, 160, 0)


def to_pixels(rect, scale):
    """Convert a bottom-left-origin rect (points) to top-left-origin pixels.

    This is the one place the y axis is flipped.
    """
    return (
        rect["x0"] * scale,
        (config.PAGE_HEIGHT - rect["y1"]) * scale,
        rect["x1"] * scale,
        (config.PAGE_HEIGHT - rect["y0"]) * scale,
    )


def main():
    with open(config.LAYOUT_JSON, encoding="utf-8") as f:
        layout = json.load(f)

    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    scale = config.PREVIEW_DPI / 72  # points -> pixels
    document = pymupdf.open(config.TEMPLATE_PDF)

    for page_info in layout["pages"]:
        number = page_info["page"]
        pixmap = document[number - 1].get_pixmap(matrix=pymupdf.Matrix(scale, scale))
        plain_path = config.PREVIEW_DIR / f"page_{number}.png"
        pixmap.save(plain_path)

        image = Image.open(plain_path).convert("RGB")
        draw = ImageDraw.Draw(image)
        for box in layout["boxes"]:
            if box["page"] == number:
                draw.rectangle(to_pixels(box, scale), outline=RED, width=1)
        area = layout["sample_sentence"]
        if area and area["page"] == number:
            draw.rectangle(to_pixels(area, scale), outline=BLUE, width=1)
            for y in area["baseline_ys"]:
                line = {"x0": area["x0"], "x1": area["x1"], "y0": y, "y1": y}
                draw.line(to_pixels(line, scale), fill=GREEN, width=1)
        overlay_path = config.PREVIEW_DIR / f"overlay_{number}.png"
        image.save(overlay_path)
        print(f"Page {number}: {plain_path.name}, {overlay_path.name}")

    print(f"Saved to {config.PREVIEW_DIR}")


if __name__ == "__main__":
    main()
