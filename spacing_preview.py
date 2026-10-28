"""Diagnostic picture for spacing.py: the analysed sentence with every measurement drawn on it.

What you are looking at (also printed in the image header):
    blue band        a detected text line
    gray boxes       ink regions found by empty columns (one letter or several touching ones)
    light-blue bars  letter gaps (measured)
    orange bars      word gaps, labelled with their width in pt
    orange frames    detected words, labelled with the sentence text when the word count matches
    green line       the printed writing guide (a template line, NOT ink)
    red line         the line's measured baseline (median of the region bottoms)
    red dots         one baseline observation per region
    magenta dashes   letters that touch inside a word: their gap cannot be measured (an estimate)
"""

from PIL import Image, ImageDraw, ImageFont

import config

HEADER_HEIGHT = 96
LINE_BAND = (60, 120, 255, 45)
LETTER_GAP = (80, 170, 255, 110)
WORD_GAP = (255, 140, 0, 120)
WORD_FRAME = (255, 140, 0)
REGION_BOX = (150, 150, 150)
GUIDE = (0, 150, 0)
BASELINE = (230, 0, 0)
TOUCHING = (200, 0, 200)


def font(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def dashed_line(draw, start, end, fill, dash=10):
    (x0, y0), (x1, y1) = start, end
    x = x0
    while x < x1:
        draw.line([(x, y0), (min(x + dash, x1), y1)], fill=fill, width=2)
        x += dash * 2


def header_lines(style):
    letter, word, base = style["letter_spacing"], style["word_spacing"], style["baseline_variation"]
    return [
        f"{style['user']}  |  lines {style['sentence']['text_lines_detected']}, "
        f"words {style['sentence']['words_detected']} of {style['sentence']['words_in_text']}  |  "
        f"1 pt = {style['units']['pixels_per_point']:.0f} px",
        f"word gap: {word.get('mean_pt')} pt mean, {word['observations']} gaps [{word['quality']}]    "
        f"letter gap: {letter.get('estimate_pt')} pt est., {letter['observations']} visible [{letter['quality']}]    "
        f"baseline: {base.get('robust_std_pt')} pt robust std, {base['observations']} regions [{base['quality']}]",
        "blue band = text line  gray box = ink region  light-blue bar = letter gap  orange = word gap/word  "
        "green = printed guide (template)  red = measured baseline  magenta dashes = touching letters (estimate)",
    ]


def write_preview(gray, segmentation, style, user, scale):
    """Draw the diagnostic image and return its path."""
    width, height = gray.size
    canvas = Image.new("RGB", (width, height + HEADER_HEIGHT), (255, 255, 255))
    canvas.paste(gray.convert("RGB"), (0, HEADER_HEIGHT))
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shade = ImageDraw.Draw(overlay)
    small, label = font(14), font(18)

    def y(row):
        return row + HEADER_HEIGHT

    for line in segmentation["lines"]:
        top, bottom = line["rows"]
        shade.rectangle([0, y(top), width, y(bottom)], fill=LINE_BAND)
        for gap in line["gaps"]:
            left = line["regions"][gap["after_region"]]["right"]
            right = line["regions"][gap["after_region"] + 1]["left"]
            shade.rectangle([left, y(top), max(right, left + 1), y(bottom)],
                            fill=WORD_GAP if gap["kind"] == "word" else LETTER_GAP)
    canvas = Image.alpha_composite(canvas.convert("RGBA"), overlay).convert("RGB")
    draw = ImageDraw.Draw(canvas)

    for row_index, row in enumerate(segmentation["guide_rows"]):
        draw.line([(0, y(row)), (width, y(row))], fill=GUIDE, width=1)
        # Short label at the far left, in the margin before the handwriting starts.
        draw.text((6, y(row) - 3), f"guide {row_index + 1}", fill=GUIDE, font=small, anchor="lb")

    for index, line in enumerate(segmentation["lines"]):
        top, bottom = line["rows"]
        draw.text((6, y(top) - 4), f"line {index + 1}", fill=(0, 60, 200), font=label, anchor="lb")
        left, right = line["regions"][0]["left"], line["regions"][-1]["right"]
        draw.line([(left, y(line["baseline_row"])), (right, y(line["baseline_row"]))], fill=BASELINE, width=2)
        for region in line["regions"]:
            draw.rectangle([region["left"], y(region["top"]), region["right"] - 1, y(region["bottom"]) - 1],
                           outline=REGION_BOX)
            cx = (region["left"] + region["right"]) / 2
            draw.ellipse([cx - 4, y(region["bottom"]) - 4, cx + 4, y(region["bottom"]) + 4], fill=BASELINE)
        for gap in line["gaps"]:
            if gap["kind"] == "word":
                left_edge = line["regions"][gap["after_region"]]["right"]
                centre = left_edge + gap["width_px"] / 2
                draw.text((centre, y(top) - 4), f"{gap['width_px'] / scale:.1f} pt", fill=(200, 100, 0),
                          font=small, anchor="mb")

    for word in segmentation["words"]:
        line = segmentation["lines"][word["line"]]
        top, bottom = line["rows"]
        draw.rectangle([word["left"] - 2, y(top) - 2, word["right"] + 2, y(bottom) + 2], outline=WORD_FRAME, width=2)
        draw.text(((word["left"] + word["right"]) / 2, y(bottom) + 6),
                  word["text"] if word["text"] else "?", fill=(200, 100, 0), font=label, anchor="mt")
        if word["pairs"] is not None and word["pairs"] > word["visible_gaps"]:
            missing = word["pairs"] - word["visible_gaps"]
            dashed_line(draw, (word["left"], y(bottom) + 30), (word["right"], y(bottom) + 30), TOUCHING)
            draw.text(((word["left"] + word["right"]) / 2, y(bottom) + 34), f"{missing} touching (estimate)",
                      fill=TOUCHING, font=small, anchor="mt")

    for number, text in enumerate(header_lines(style)):
        draw.text((8, 6 + number * 28), text, fill=(0, 0, 0), font=small if number else label)

    config.PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
    path = config.PREVIEW_DIR / f"{config.SPACING_PREVIEW_PREFIX}_{user}.png"
    canvas.save(path)
    return path
