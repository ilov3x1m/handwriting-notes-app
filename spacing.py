"""Measure the writer's natural spacing from the sample sentence.

Usage:
    python spacing.py samples/remarkable_test.pdf --user amaya

Reads the sentence area (page 3) of a filled-in template and writes
glyphs/<user>/style.json with three measurements:

  letter spacing  - gap between neighbouring letters inside a word
  word spacing    - gap between words on the same line
  baseline        - how far the letters' bottoms wander around the writing line

How it works (all on the sentence area only, never the whole page):
  1. Render the page, crop the sentence area 1 pt inside its border, and treat
     pixels darker than config.INK_CUTOFF as ink. The printed baseline guides
     are lighter than the cutoff, so they are not ink.
  2. Rows with ink -> text lines. Within a line, columns with ink -> "regions"
     (one letter, several touching letters, or a letter's loose strokes).
  3. Empty columns between regions are gaps. The sentence text tells us how
     many gaps must be word gaps (words - 1, minus one per line break), so the
     widest gaps of that count are word gaps and the rest are letter gaps.
  4. Each region's lowest ink row is one baseline observation.

Units: results are in PDF points (1/72 inch). Pixels are converted using
pixels_per_point, which is saved in the file. A pixel row is converted to a
height above the printed guide so no y-axis flip is left for later modules.
"""

import argparse
import datetime
import json
import os
import re
import sys

import numpy as np

import config
import extract
from extract import ExtractError

QUALITY_LABELS = {
    "good": "Count matches the sentence text and the two kinds of gap are clearly separated.",
    "approximate": "A value was measured but is known to be biased; see the method and warnings.",
    "insufficient": "Too few observations. The value is null and must not be used.",
}


# ---------------------------------------------------------------------------
# Small 1-D helpers
# ---------------------------------------------------------------------------

def find_runs(flags):
    """Return (start, stop) pairs for each run of True values in a 1-D array."""
    padded = np.concatenate(([False], np.asarray(flags, dtype=bool), [False]))
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2])]


def robust_std(values):
    """Standard deviation estimated from the median absolute deviation.

    A few outliers (letters with descenders) barely move it, unlike np.std.
    """
    values = np.asarray(values, dtype=float)
    return float(1.4826 * np.median(np.abs(values - np.median(values))))


def ink_mask(gray_array):
    """Boolean mask of handwriting pixels (darker than the ink cutoff)."""
    return gray_array < config.INK_CUTOFF


# ---------------------------------------------------------------------------
# Segmentation
# ---------------------------------------------------------------------------

def find_text_lines(mask, merge_gap_px):
    """Return (top, bottom) pixel rows of each text line, bottom exclusive.

    Ink rows separated by less than `merge_gap_px` are one line, so an "i" dot
    does not become a line of its own.
    """
    lines = []
    for top, bottom in find_runs(mask.any(axis=1)):
        if lines and top - lines[-1][1] < merge_gap_px:
            lines[-1] = (lines[-1][0], bottom)
        else:
            lines.append((top, bottom))
    return lines


def find_regions(mask, line, min_gap_px):
    """Split one text line into horizontal ink regions.

    Each region is a dict with its column span and the lowest/highest ink row
    (absolute rows of the mask). Columns separated by fewer than `min_gap_px`
    empty columns stay in one region.
    """
    top, bottom = line
    spans = []
    for left, right in find_runs(mask[top:bottom].any(axis=0)):
        if spans and left - spans[-1][1] < min_gap_px:
            spans[-1] = (spans[-1][0], right)
        else:
            spans.append((left, right))
    regions = []
    for left, right in spans:
        rows = np.flatnonzero(mask[top:bottom, left:right].any(axis=1))
        regions.append({"left": left, "right": right,
                        "top": top + int(rows[0]), "bottom": top + int(rows[-1]) + 1})
    return regions


def classify_gaps(lines, expected_word_gaps):
    """Mark every gap between regions as a "word" or "letter" gap.

    The widest `expected_word_gaps` gaps (over all lines) are word gaps. The
    line break is not a gap at all, because each line is analysed separately.
    Adds a "kind" and "width_px" to each gap dict in line["gaps"].
    """
    all_gaps = []
    for line in lines:
        line["gaps"] = []
        for index in range(len(line["regions"]) - 1):
            width = line["regions"][index + 1]["left"] - line["regions"][index]["right"]
            gap = {"after_region": index, "width_px": width, "kind": "letter"}
            line["gaps"].append(gap)
            all_gaps.append(gap)
    for gap in sorted(all_gaps, key=lambda g: -g["width_px"])[:max(0, expected_word_gaps)]:
        gap["kind"] = "word"


def split_words(lines, text_words):
    """Group each line's regions into words and attach the text word if counts agree.

    Returns a list of word dicts: line index, column span, visible letter gaps
    and the expected number of letter pairs (from the text).
    """
    words = []
    for line_index, line in enumerate(lines):
        start = 0
        breaks = [g["after_region"] for g in line["gaps"] if g["kind"] == "word"]
        for end in breaks + [len(line["regions"]) - 1]:
            regions = line["regions"][start:end + 1]
            visible = [g for g in line["gaps"] if start <= g["after_region"] < end and g["kind"] == "letter"]
            words.append({"line": line_index, "left": regions[0]["left"], "right": regions[-1]["right"],
                          "visible_gaps": len(visible), "regions": len(regions), "text": None, "pairs": None})
            start = end + 1
    if len(words) == len(text_words):  # only label words when the count matches the text
        for word, text in zip(words, text_words):
            word["text"] = text
            word["pairs"] = max(0, len(text) - 1)
    return words


# ---------------------------------------------------------------------------
# Measurements (all inputs in pixels, outputs in points)
# ---------------------------------------------------------------------------

def stats(values_pt):
    values = np.asarray(values_pt, dtype=float)
    return {"mean_pt": round(float(values.mean()), 3), "median_pt": round(float(np.median(values)), 3),
            "std_pt": round(float(values.std()), 3),
            "min_pt": round(float(values.min()), 3), "max_pt": round(float(values.max()), 3)}


def gap_widths_pt(lines, kind, scale):
    """Widths in points of every gap of one kind ("word" or "letter")."""
    return [g["width_px"] / scale for line in lines for g in line["gaps"] if g["kind"] == kind]


def word_spacing_result(lines, expected_gaps, scale, warnings):
    word_gaps = gap_widths_pt(lines, "word", scale)
    letter_gaps = gap_widths_pt(lines, "letter", scale)
    result = {
        "unit": "pt",
        "method": ("Empty pixel columns between ink regions on each text line. Because the sentence text "
                   "says how many gaps must be word gaps, the widest gaps of that number are word gaps. "
                   "The jump from one line to the next is never measured."),
        "observations": len(word_gaps),
        "expected_word_gaps": expected_gaps,
        "gaps_pt": [round(g, 3) for g in sorted(word_gaps)],
    }
    if len(word_gaps) < config.SPACING_MIN_WORD_GAPS:
        warnings.append("Too few word gaps to measure word spacing.")
        result.update({"quality": "insufficient", "separation_ratio": None})
        return result
    result.update(stats(word_gaps))
    ratio = min(word_gaps) / max(letter_gaps) if letter_gaps else None
    result["separation_ratio"] = None if ratio is None else round(ratio, 2)
    clear = len(word_gaps) == expected_gaps and (ratio is None or ratio >= config.SPACING_WORD_SEPARATION_RATIO)
    result["quality"] = "good" if clear else "approximate"
    if not clear:
        warnings.append("Word gaps are not clearly larger than letter gaps; word spacing may be mis-assigned.")
    return result


def letter_spacing_result(lines, words, scale, warnings):
    letter_gaps = gap_widths_pt(lines, "letter", scale)
    result = {
        "unit": "pt",
        "method": ("Empty pixel columns between ink regions inside a word. Letters that touch or overlap "
                   "leave no gap, so they cannot be measured this way. The sentence text gives the number of "
                   "letter pairs per word; pairs with no visible gap are counted as 0 pt in estimate_pt. "
                   "Loose strokes of one letter can add a gap, so this is an approximation."),
        "observations": len(letter_gaps),
        "interpretation": ("Ink-to-ink distance between neighbouring letters, measured between the ink "
                           "edges of the glyphs. The saved glyph PNGs carry trim_padding_px of transparent "
                           "padding on every side; the renderer must subtract it when placing PNGs."),
    }
    if len(letter_gaps) < config.SPACING_MIN_LETTER_GAPS:
        warnings.append("Too few visible letter gaps to measure letter spacing.")
        result.update({"quality": "insufficient", "estimate_pt": None})
        return result

    result.update(stats(letter_gaps))
    result["visible_gap_count"] = len(letter_gaps)
    if all(w["pairs"] is not None for w in words):
        # Per word: at least as many slots as letter pairs, more if extra stroke gaps were seen.
        slots = sum(max(w["pairs"], w["visible_gaps"]) for w in words)
        result["expected_letter_pairs"] = sum(w["pairs"] for w in words)
        result["unresolved_pairs"] = sum(max(0, w["pairs"] - w["visible_gaps"]) for w in words)
        result["extra_stroke_gaps"] = sum(max(0, w["visible_gaps"] - w["pairs"]) for w in words)
        result["estimate_pt"] = round(sum(letter_gaps) / slots, 3)
        clean = result["unresolved_pairs"] == 0 and result["extra_stroke_gaps"] == 0
        result["quality"] = "good" if clean else "approximate"
        if result["unresolved_pairs"]:
            warnings.append(f"{result['unresolved_pairs']} of {result['expected_letter_pairs']} letter pairs "
                            "touch or overlap and have no measurable gap (counted as 0 pt).")
        if result["extra_stroke_gaps"]:
            warnings.append(f"{result['extra_stroke_gaps']} extra gaps were found inside words "
                            "(probably loose strokes of one letter).")
    else:
        result["estimate_pt"] = result["mean_pt"]
        result["quality"] = "approximate"
        warnings.append("Detected word count does not match the sentence text, so letter pairs were not "
                        "counted; estimate_pt is the mean of the visible gaps only.")
    return result


def baseline_result(lines, guide_rows, scale, warnings):
    """Per line: where letter bottoms sit relative to the printed guide, and their spread."""
    per_line, pooled, all_offsets = [], [], []
    for line in lines:
        bottoms = np.array([r["bottom"] for r in line["regions"]], dtype=float)
        median_bottom = float(np.median(bottoms))
        # The printed guide this line was written for is the one nearest its letter bottoms.
        guide_index = int(np.argmin([abs(row - median_bottom) for row in guide_rows])) if guide_rows else None
        line["guide_index"] = guide_index
        line["baseline_row"] = median_bottom
        # Height above the guide: positive = ink bottom above the printed line.
        offsets = [(guide_rows[guide_index] - b) / scale for b in bottoms] if guide_rows else []
        centred = (bottoms - median_bottom) / scale
        pooled.extend(centred.tolist())
        all_offsets.extend(offsets)
        per_line.append({
            "guide_index": guide_index,
            "observations": len(bottoms),
            "median_bottom_above_guide_pt": round(float(np.median(offsets)), 3) if offsets else None,
            "robust_std_pt": round(robust_std(centred), 3),
        })

    result = {
        "unit": "pt",
        "method": ("Each ink region's lowest ink row is one observation. Within each line the observations "
                   "are centred on that line's median, pooled, and summarised with a robust standard "
                   "deviation (1.4826 x median absolute deviation) so that a few descenders (g, j, p, q, y) "
                   "do not dominate. std_pt is the plain standard deviation of the same values."),
        "observations": len(pooled),
        "sign_convention": "median_bottom_above_guide_pt > 0 means the letters sit above the printed guide.",
        "per_line": per_line,
        "median_bottom_above_guide_pt": round(float(np.median(all_offsets)), 3) if all_offsets else None,
    }
    if len(pooled) < config.SPACING_MIN_BASELINE_OBSERVATIONS:
        warnings.append("Too few ink regions to measure baseline variation.")
        result.update({"quality": "insufficient", "robust_std_pt": None, "std_pt": None})
        return result
    result["robust_std_pt"] = round(robust_std(pooled), 3)
    result["std_pt"] = round(float(np.std(pooled)), 3)
    result["quality"] = "approximate"
    warnings.append("Baseline variation uses region bottoms: descenders and touching letters make it approximate.")
    return result


# ---------------------------------------------------------------------------
# Putting it together
# ---------------------------------------------------------------------------

def sentence_words(text):
    """Words of the prompt with punctuation removed ("dog." -> "dog")."""
    return [w for w in (re.sub(r"[^A-Za-z0-9]", "", w) for w in text.split()) if w]


def analyse_image(gray, text, guide_rows, scale):
    """Analyse a grayscale crop of the sentence area.

    `guide_rows` are the printed baseline guides as pixel rows of this crop.
    Returns (measurements dict, segmentation dict, warnings list).
    """
    mask = ink_mask(np.asarray(gray))
    if int(mask.sum()) < config.SPACING_MIN_INK_PIXELS:
        raise ExtractError("No handwriting found in the sample sentence area "
                           f"({int(mask.sum())} ink pixels).")

    merge_px = config.SPACING_LINE_MERGE_GAP_PT * scale
    lines = [{"rows": rows} for rows in find_text_lines(mask, merge_px)]
    for line in lines:
        line["regions"] = find_regions(mask, line["rows"], config.SPACING_MIN_GAP_PX)

    text_words = sentence_words(text)
    expected_word_gaps = (len(text_words) - 1) - (len(lines) - 1)
    classify_gaps(lines, expected_word_gaps)
    words = split_words(lines, text_words)

    warnings = []
    if expected_word_gaps < 0:
        warnings.append("More text lines than words; word spacing cannot be measured.")
    measurements = {
        "letter_spacing": letter_spacing_result(lines, words, scale, warnings),
        "word_spacing": word_spacing_result(lines, max(0, expected_word_gaps), scale, warnings),
        "baseline_variation": baseline_result(lines, guide_rows, scale, warnings),
    }
    segmentation = {"lines": lines, "words": words, "guide_rows": guide_rows,
                    "expected_word_gaps": expected_word_gaps, "text_words": text_words}
    return measurements, segmentation, warnings


def crop_sentence_area(document, layout, scale):
    """Render the sentence page and crop the area. Returns (gray crop, guide rows, crop box)."""
    area = layout.get("sample_sentence")
    if not area:
        raise ExtractError("Layout has no sample_sentence area")
    for key in ("page", "x0", "y0", "x1", "y1", "text", "baseline_ys"):
        if key not in area:
            raise ExtractError(f"Layout sample_sentence is missing '{key}'")
    page_info = next((p for p in layout["pages"] if p["page"] == area["page"]), None)
    if page_info is None:
        raise ExtractError(f"Layout has no page {area['page']} for the sample sentence")

    page_image = extract.render_page(document[area["page"] - 1], scale)
    left, top, right, bottom = extract.pdf_rect_to_pixel_box(area, page_info["height"], scale,
                                                             config.CROP_INSET_PT)
    guide_rows = [extract.pdf_y_to_pixel_y(y, page_info["height"], scale) - top for y in area["baseline_ys"]]
    return page_image.crop((left, top, right, bottom)), guide_rows, (left, top, right, bottom)


def glyph_baseline_reference(glyph_dir, warnings):
    """Median height of the isolated glyphs' ink bottoms above their printed guide.

    Read from glyph_metadata.json (never from file names). Lets the renderer
    compare how the writer sits on the guide in boxes vs in the sentence.
    """
    path = glyph_dir / config.GLYPH_METADATA_NAME
    try:
        with open(path, encoding="utf-8") as f:
            metadata = json.load(f)
        scale = metadata["meta"]["pixels_per_point"]
        pad = metadata["meta"]["trim_padding_px"]
        bottoms = [(g["baseline_from_bottom_px"] - pad) / scale for g in metadata["glyphs"]]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        warnings.append(f"Could not read {config.GLYPH_METADATA_NAME}; run extract.py first. "
                        "The comparison with the isolated glyphs was skipped.")
        return None
    return {
        "unit": "pt",
        "median_glyph_bottom_above_guide_pt": round(float(np.median(bottoms)), 3),
        "observations": len(bottoms),
        "source": f"{config.GLYPH_METADATA_NAME} (baseline_from_bottom_px minus trim padding, over all glyphs)",
    }


def build_style(user, pdf_path, layout, measurements, segmentation, warnings, glyph_reference, scale):
    area = layout["sample_sentence"]
    lines = segmentation["lines"]
    baseline = measurements["baseline_variation"]
    if glyph_reference and baseline.get("median_bottom_above_guide_pt") is not None:
        baseline["relation_to_glyph_samples"] = {
            "sentence_median_bottom_above_guide_pt": baseline["median_bottom_above_guide_pt"],
            "glyph_median_bottom_above_guide_pt": glyph_reference["median_glyph_bottom_above_guide_pt"],
            "note": "Both are heights of ink bottoms above the printed guide, so they share the "
                    "convention used by baseline_from_top_px in glyph_metadata.json.",
        }
    return {
        "schema_version": 1,
        "user": user,
        "source_pdf": str(pdf_path),
        "device": layout["meta"]["device"],
        "analysis_date": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
        "units": {
            "length": "PDF points (1/72 inch)",
            "pixels_per_point": scale,
            "dpi": config.EXTRACT_DPI,
            "note": "All measurements are already in points; pixels were converted with pixels_per_point. "
                    "Heights are measured upward from the printed guide, matching the PDF's y direction.",
        },
        "sentence": {
            "text": area["text"],
            "page": area["page"],
            "area_pdf": {k: area[k] for k in ("x0", "y0", "x1", "y1")},
            "text_lines_detected": len(lines),
            "words_in_text": len(segmentation["text_words"]),
            "words_detected": len(segmentation["words"]),
        },
        "letter_spacing": measurements["letter_spacing"],
        "word_spacing": measurements["word_spacing"],
        "baseline_variation": baseline,
        "glyph_reference": glyph_reference,
        "quality_labels": QUALITY_LABELS,
        "renderer_notes": [
            "Place each glyph so that its baseline_from_top_px row (glyph_metadata.json) sits on the text baseline.",
            "Use letter_spacing.estimate_pt as the default ink-to-ink gap between letters of a word, "
            "and word_spacing.mean_pt for the gap between words.",
            "baseline_variation.robust_std_pt is the measured natural vertical spread; use it only as the "
            "size of any added variation, not as an offset.",
        ],
        "warnings": warnings,
    }


def write_json_atomic(path, data):
    """Write JSON via a temporary file so a failure never leaves a half-written style.json."""
    temp = path.with_suffix(".tmp")
    with open(temp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(temp, path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Measure handwriting spacing from the sample sentence.")
    parser.add_argument("pdf", help="filled-in template exported from the reMarkable")
    parser.add_argument("--user", required=True, help="glyph folder name, e.g. amaya")
    parser.add_argument("--layout", default=None, help="layout JSON (default: chosen from the PDF page count)")
    parser.add_argument("--no-preview", action="store_true", help="skip the diagnostic image")
    return parser.parse_args(argv)


def build_style_from_pdf(pdf_path, layout_path, user, glyph_dir, source_label=None):
    """Measure the sample sentence of a filled-in template and return the style dict.

    Returns (style, gray crop, segmentation, pixels per point). Nothing is written, so
    callers decide where style.json goes. glyph_dir must already hold glyph_metadata.json.
    """
    document = extract.open_pdf(pdf_path)
    layout = extract.load_layout(layout_path or extract.layout_path_for_pdf(document))
    extract.validate_pdf(document, layout)

    scale = config.EXTRACT_DPI / 72
    gray, guide_rows, crop_box = crop_sentence_area(document, layout, scale)
    measurements, segmentation, warnings = analyse_image(gray, layout["sample_sentence"]["text"], guide_rows, scale)
    glyph_reference = glyph_baseline_reference(glyph_dir, warnings)
    style = build_style(user, source_label or pdf_path, layout, measurements, segmentation, warnings, glyph_reference, scale)
    return style, gray, segmentation, scale


def run(args):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.user):
        raise ExtractError("--user may only contain letters, digits, '_' and '-'")

    glyph_dir = config.GLYPHS_DIR / args.user
    style, gray, segmentation, scale = build_style_from_pdf(args.pdf, args.layout, args.user, glyph_dir)
    warnings = style["warnings"]

    glyph_dir.mkdir(parents=True, exist_ok=True)
    style_path = glyph_dir / config.STYLE_NAME
    write_json_atomic(style_path, style)

    word, letter, base = style["word_spacing"], style["letter_spacing"], style["baseline_variation"]
    print(f"Lines detected: {style['sentence']['text_lines_detected']}   words detected: {style['sentence']['words_detected']}")
    print(f"Word spacing:   {word.get('mean_pt')} pt mean over {word['observations']} gaps  [{word['quality']}]")
    print(f"Letter spacing: {letter.get('estimate_pt')} pt estimate ({letter['observations']} visible gaps)  [{letter['quality']}]")
    print(f"Baseline:       {base.get('robust_std_pt')} pt robust std over {base['observations']} regions  [{base['quality']}]")
    for warning in warnings:
        print(f"WARNING: {warning}")
    print(f"Style: {style_path}")

    if not args.no_preview:
        import spacing_preview  # only needed for the diagnostic image
        print(f"Preview: {spacing_preview.write_preview(gray, segmentation, style, args.user, scale)}")
    return 0


def main(argv=None):
    try:
        return run(parse_args(argv))
    except ExtractError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
