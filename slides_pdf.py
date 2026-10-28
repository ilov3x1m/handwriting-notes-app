"""Text extraction for text-based PDF slides (used by slides.py).

A PDF stores positioned text, not titles or bullets, so everything here beyond the
text itself is INFERRED. Every element is marked structure="inferred" in the JSON.
The heuristics, and where they fail:

  title      The largest text in the top part of the page, if it is clearly larger
             than the body text (config.PDF_TITLE_MIN_RATIO). Fails when a slide has
             no larger title, or when a big decorative word sits above the real title.
  heading    A single-line, short, non-bullet line that is larger than the body text,
             or bold when the body text is not. Fails for emphasised short sentences.
  bullet     A line starting with a bullet character ("•", "–", "-", "*", ...) or a
             number/letter marker ("1.", "2)", "a)"). Bullets drawn as graphics, not
             characters, are NOT detected and show up as plain paragraphs.
  level      Bullet marker x-positions are grouped (config.PDF_LEVEL_TOLERANCE) and
             numbered left to right. Fails when indentation is irregular.
  wrapped    A line directly under the previous one (tight vertical gap, no new marker,
             same size) continues that paragraph. Fails for loosely spaced wrapped text.
  order      Top to bottom; a page that clearly has two columns is read left column
             first. Three or more columns, or text wrapped around images, may be misordered.
  footers    Page numbers and short lines repeated in the top/bottom strip on at least
             config.PDF_RUNNING_MIN_PAGES pages are dropped and counted in a warning.

Pages are never silently emptied: a page with no selectable text is reported as
"no_text", and a page that is mostly an image or has garbled text is "uncertain".
OCR is not attempted.
"""

import collections
import re

import config
from extract import ExtractError

BULLET_LINE = re.compile(rf"^\s*([{re.escape(config.PDF_BULLET_CHARS)}])\s+(\S.*)$")
NUMBER_LINE = re.compile(r"^\s*(\d{1,2}[.)]|[a-z][.)]|\(\d{1,2}\)|\([a-z]\))\s+(\S.*)$")
PAGE_NUMBER = re.compile(r"^\d{1,3}(\s*(/|of)\s*\d{1,3})?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Reading lines from a page
# ---------------------------------------------------------------------------

def page_lines(page):
    """Every non-empty text line on a page with its size, boldness and position.

    Uses character-level data so a line like "• Some text" also records where the text
    itself starts (text_x0), not just where the bullet glyph is. Wrapped lines of a
    bullet start at that text position (a hanging indent).
    """
    lines = []
    for block_number, block in enumerate(page.get_text("rawdict")["blocks"]):
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            chars = [c for span in line["spans"] for c in span["chars"]]
            text = "".join(c["c"] for c in chars)
            if not text.strip():
                continue
            sizes, bold_chars, counted = collections.Counter(), 0, 0
            for span in line["spans"]:
                n = sum(1 for c in span["chars"] if c["c"].strip())
                sizes[round(span["size"], 1)] += n
                counted += n
                if span["flags"] & 16 or "bold" in span["font"].lower():
                    bold_chars += n
            x0, y0, x1, y1 = line["bbox"]
            text_x0 = x0
            match = BULLET_LINE.match(text) or NUMBER_LINE.match(text)
            if match:
                text_x0 = chars[match.start(2)]["bbox"][0]
            lines.append({"text": text.strip(), "x0": x0, "y0": y0, "x1": x1, "y1": y1,
                          "size": sizes.most_common(1)[0][0], "bold": counted > 0 and bold_chars > counted / 2,
                          "block": block_number, "marker_x0": None, "text_x0": text_x0})
    return merge_lone_markers(lines)


def merge_lone_markers(lines):
    """Join a bullet character that was stored as its own line with the text beside it."""
    markers = [l for l in lines if len(l["text"]) == 1 and l["text"] in config.PDF_BULLET_CHARS]
    removed = set()
    for marker in markers:
        centre = (marker["y0"] + marker["y1"]) / 2
        beside = [l for l in lines if l is not marker and l["x0"] >= marker["x1"] - 1
                  and l["y0"] - marker["size"] * 0.5 <= centre <= l["y1"] + marker["size"] * 0.5]
        if beside:
            target = min(beside, key=lambda l: l["x0"] - marker["x1"])
            target["text"] = f"{marker['text']} {target['text']}"
            target["marker_x0"] = marker["x0"]
            removed.add(id(marker))
    return [l for l in lines if id(l) not in removed]


def drop_duplicates(lines):
    """Remove a line whose text is identical to an earlier, overlapping line (e.g. a drop shadow)."""
    kept = []
    for line in lines:
        key = " ".join(line["text"].lower().split())
        duplicate = False
        for other in kept:
            if " ".join(other["text"].lower().split()) != key:
                continue
            width = min(line["x1"], other["x1"]) - max(line["x0"], other["x0"])
            height = min(line["y1"], other["y1"]) - max(line["y0"], other["y0"])
            small = min((line["x1"] - line["x0"]) * (line["y1"] - line["y0"]),
                        (other["x1"] - other["x0"]) * (other["y1"] - other["y0"]))
            if width > 0 and height > 0 and small > 0 and width * height / small > 0.5:
                duplicate = True
                break
        if not duplicate:
            kept.append(line)
    return kept


def find_running_lines(pages_lines, page_sizes):
    """Lines to drop: page numbers, and short lines repeated in the top/bottom strip on many pages."""
    counts = collections.Counter()
    for lines, (_, height) in zip(pages_lines, page_sizes):
        seen = set()
        for line in lines:
            if line["y0"] < config.PDF_EDGE_ZONE * height or line["y1"] > (1 - config.PDF_EDGE_ZONE) * height:
                seen.add(re.sub(r"\d+", "#", line["text"].lower()))
        counts.update(seen)
    minimum = max(config.PDF_RUNNING_MIN_PAGES, len(pages_lines) // 2)
    return {key for key, n in counts.items() if n >= minimum and len(pages_lines) >= config.PDF_RUNNING_MIN_PAGES}


def remove_running_lines(lines, height, running):
    kept, dropped = [], []
    for line in lines:
        in_edge = line["y0"] < config.PDF_EDGE_ZONE * height or line["y1"] > (1 - config.PDF_EDGE_ZONE) * height
        key = re.sub(r"\d+", "#", line["text"].lower())
        if in_edge and (key in running or PAGE_NUMBER.match(line["text"])):
            dropped.append(line["text"])
        else:
            kept.append(line)
    return kept, dropped


# ---------------------------------------------------------------------------
# Structure inference for one page
# ---------------------------------------------------------------------------

def body_statistics(lines):
    """Typical (character-weighted median) text size, and whether most text is bold."""
    weighted = sorted((l["size"], len(l["text"])) for l in lines)
    half, total = sum(n for _, n in weighted) / 2, 0
    size = weighted[-1][0]
    for value, n in weighted:
        total += n
        if total >= half:
            size = value
            break
    bold = sum(len(l["text"]) for l in lines if l["bold"]) > 0.6 * sum(len(l["text"]) for l in lines)
    return size, bold


def find_title(lines, body_size, page_height):
    """Return (title lines, remaining lines).

    The title is the TOP-MOST group of text that is clearly larger than the document's body
    text (config.PDF_TITLE_MIN_RATIO), wherever on the page it sits. Taking the top-most group
    rather than the biggest text matters: a sub-heading can be set larger than the title.
    A title may wrap over several lines of the same size; those lines can run below the
    seed line (config.PDF_TITLE_ZONE limits where the seed may start).
    """
    large = sorted((l for l in lines if l["size"] >= config.PDF_TITLE_MIN_RATIO * body_size
                    and l["y0"] < config.PDF_TITLE_ZONE * page_height), key=lambda l: (l["y0"], l["x0"]))
    if not large:
        return [], lines
    seed = large[0]
    title = [seed]
    for line in large[1:]:
        same_size = abs(line["size"] - seed["size"]) <= 0.05 * seed["size"]
        below = line["y0"] > title[-1]["y0"] + 0.5 * seed["size"]       # the next line, not one beside it
        close = line["y0"] - title[-1]["y1"] < 0.8 * seed["size"]
        if same_size and below and close:
            title.append(line)
    return title, [l for l in lines if all(l is not t for t in title)]


def top_to_bottom(lines):
    return sorted(lines, key=lambda l: (round(l["y0"] / 2), l["x0"]))


def split_columns(lines, page_width, depth=0):
    """Lines grouped into columns, left to right; a single group unless the page really has columns.

    Columns are found by the widest gap between the left edges of lines (at least
    config.PDF_COLUMN_GAP of the page width) with no line crossing the gap. Each side is split
    again, so three or four columns work too.
    """
    if len(lines) < 4 or depth >= 3:
        return [lines]
    ordered = sorted(lines, key=lambda l: l["x0"])
    gap, index = max((ordered[i + 1]["x0"] - ordered[i]["x0"], i) for i in range(len(ordered) - 1))
    if gap < config.PDF_COLUMN_GAP * page_width:
        return [lines]
    left, right = ordered[:index + 1], ordered[index + 1:]
    if len(left) < 2 or len(right) < 2 or max(l["x1"] for l in left) >= min(l["x0"] for l in right):
        return [lines]
    return split_columns(left, page_width, depth + 1) + split_columns(right, page_width, depth + 1)


def split_marker(text):
    """Return (marker, rest) for a bullet/numbered line, or (None, text)."""
    match = BULLET_LINE.match(text)
    if match:
        return "bullet", match.group(2)
    match = NUMBER_LINE.match(text)
    if match:
        marker = match.group(1)
        if marker.startswith("("):            # "(2)" -> "2)", which the renderer treats as a list marker
            marker = marker[1:-1] + ")"
        return marker, match.group(2)
    return None, text


def join_wrapped(first, second):
    """Join two lines of one paragraph; a trailing hyphen keeps the word together without a space."""
    return first + second if first.endswith("-") else f"{first} {second}"


def continues(previous, line, tolerance):
    """Does `line` continue the paragraph/bullet `previous` (a wrapped line)?"""
    if previous is None or previous["lines"] >= 12:
        return False
    gap = line["y0"] - previous["y1"]
    same_size = abs(line["size"] - previous["size"]) <= 0.1 * previous["size"]
    tight = gap <= 0.15 * previous["size"] or (line["block"] == previous["block"] and gap <= 0.4 * previous["size"])
    aligned = previous["marker_x0"] - tolerance <= line["x0"] <= previous["text_x0"] + tolerance
    return same_size and tight and aligned


def build_items(lines, page_width):
    """Group ordered lines into paragraphs/bullets, joining wrapped lines."""
    tolerance = 0.02 * page_width
    items = []
    for line in lines:
        previous = items[-1] if items else None
        marker, rest = split_marker(line["text"])
        # A wrapped line can start with something that looks like a number, e.g. "(1) showing...".
        # If it sits indented under the text of the bullet above (not at the marker), it is a wrap.
        # Real bullet characters are never treated this way.
        looks_numbered = marker not in (None, "bullet")
        wrapped_number = (looks_numbered and previous is not None and previous["type"] == "bullet"
                          and line["x0"] > previous["marker_x0"] + tolerance and continues(previous, line, tolerance))
        if marker is not None and not wrapped_number:
            items.append({"type": "bullet", "marker": None if marker == "bullet" else marker, "text": rest,
                          "marker_x0": line["marker_x0"] if line["marker_x0"] is not None else line["x0"],
                          "text_x0": line["text_x0"], "size": line["size"], "bold": line["bold"],
                          "block": line["block"], "y1": line["y1"], "lines": 1})
            continue
        if continues(previous, line, tolerance):
            previous["text"] = join_wrapped(previous["text"], line["text"])
            previous["y1"] = line["y1"]
            previous["lines"] += 1
            continue
        items.append({"type": "paragraph", "marker": None, "text": line["text"], "marker_x0": line["x0"],
                      "text_x0": line["x0"], "size": line["size"], "bold": line["bold"],
                      "block": line["block"], "y1": line["y1"], "lines": 1})
    return items


def assign_levels(items, page_width):
    """Nesting level of each bullet = rank of its marker x-position among the page's bullets."""
    tolerance = config.PDF_LEVEL_TOLERANCE * page_width
    positions = sorted(i["marker_x0"] for i in items if i["type"] == "bullet")
    clusters = []
    for x in positions:
        if not clusters or x - clusters[-1] > tolerance:
            clusters.append(x)
    for item in items:
        if item["type"] == "bullet":
            rank = max(i for i, c in enumerate(clusters) if item["marker_x0"] >= c - tolerance)
            item["level"] = min(rank, config.SLIDES_MAX_LEVEL)
        else:
            item["level"] = 0


def mark_headings(items, body_size, body_bold):
    for item in items:
        if item["type"] != "paragraph" or item["lines"] != 1:
            continue
        larger = item["size"] >= config.PDF_HEADING_MIN_RATIO * body_size
        emphasised = item["bold"] and not body_bold and item["size"] >= 0.98 * body_size
        if (larger or emphasised) and len(item["text"].split()) <= config.PDF_HEADING_MAX_WORDS:
            item["type"] = "heading"


def analyse_page(lines, page_width, page_height, document_body_size):
    """Raw slide fields (title, title_source, elements) for one page's remaining lines."""
    lines = drop_duplicates(lines)
    body_size, body_bold = body_statistics(lines)
    # Compare against the whole document's body text, so a page holding only a title still has one.
    title_lines, rest = find_title(lines, document_body_size, page_height)
    title = " ".join(l["text"] for l in sorted(title_lines, key=lambda l: (l["y0"], l["x0"]))) if title_lines else None
    if rest:
        body_size, body_bold = body_statistics(rest)  # the title no longer counts as "body"
    else:
        body_size = document_body_size
    items = []
    for column in split_columns(rest, page_width) if rest else []:
        column_items = build_items(top_to_bottom(column), page_width)
        assign_levels(column_items, page_width)       # nesting is judged within each column
        mark_headings(column_items, body_size, body_bold)
        items.extend(column_items)
    elements = []
    for item in items:
        element = {"type": item["type"], "text": item["text"], "level": item["level"], "structure": "inferred"}
        if item.get("marker"):
            element["marker"] = item["marker"]
        elements.append(element)
    return title, ("inferred_font_size" if title else "none"), elements


def page_problem(page, lines, chars, page_number):
    """Why a page's text may be incomplete, or None."""
    if chars < config.PDF_MIN_PAGE_CHARS:
        return "no_text", f"No selectable text on page {page_number}. It may be an image; OCR is not supported."
    text = "".join(l["text"] for l in lines)
    bad = sum(1 for c in text if c == "�" or "" <= c <= "")
    if text and bad / len(text) > config.PDF_GARBLED_RATIO:
        return "uncertain", f"Page {page_number} has many unreadable characters ({bad} of {len(text)}); the PDF's fonts may lack text mapping."
    area = page.rect.width * page.rect.height
    covered = min(1.0, sum((b[2] - b[0]) * (b[3] - b[1]) for b in
                           (info["bbox"] for info in page.get_image_info())) / area) if area else 0
    if covered >= config.PDF_IMAGE_COVERAGE and chars < config.PDF_IMAGE_LOW_TEXT_CHARS:
        return "uncertain", (f"Page {page_number} is mostly an image ({covered:.0%}) with little text; "
                             "text inside the image is not extracted.")
    return None, None


def extract_pdf(path):
    import pymupdf
    try:
        document = pymupdf.open(path)
    except Exception as error:
        raise ExtractError(f"{path} could not be opened as a PDF ({type(error).__name__}: {error}).")
    if document.needs_pass:
        raise ExtractError(f"{path} is password protected.")
    try:
        pages = list(document)
        raw_lines = [page_lines(page) for page in pages]
        sizes = [(p.rect.width, p.rect.height) for p in pages]
        running = find_running_lines(raw_lines, sizes)

        slides, dropped_all, cleaned = [], [], []
        for lines, (_, height) in zip(raw_lines, sizes):
            kept, dropped = remove_running_lines(lines, height, running)
            cleaned.append(kept)
            dropped_all.extend(dropped)
        every_line = [l for kept in cleaned for l in kept]
        document_body_size = body_statistics(every_line)[0] if every_line else 0
        for number, (page, lines, (width, height)) in enumerate(zip(pages, cleaned, sizes), start=1):
            chars = sum(len(l["text"].replace(" ", "")) for l in lines)
            warnings = []
            status, problem = page_problem(page, lines, chars, number)
            if problem:
                warnings.append(problem)
            if status == "no_text":
                slides.append({"number": number, "title": None, "title_source": "none", "status": status,
                               "elements": [], "warnings": warnings})
                continue
            title, title_source, elements = analyse_page(lines, width, height, document_body_size)
            slides.append({"number": number, "title": title, "title_source": title_source, "status": status or "ok",
                           "elements": elements, "warnings": warnings})
    finally:
        document.close()

    doc_warnings = []
    if dropped_all:
        examples = ", ".join(repr(t) for t in sorted(set(dropped_all))[:4])
        doc_warnings.append(f"Dropped {len(dropped_all)} page-number or repeated header/footer lines (e.g. {examples}).")
    method = "PyMuPDF text positions + layout heuristics (font size, indentation, bullet characters)"
    note = ("A PDF has no title or bullet markup, so titles, headings, bullets and nesting levels are INFERRED from "
            "font size, position and bullet characters and may not match the original presentation. "
            "Pages without selectable text are reported, not read (no OCR).")
    return slides, method, note, doc_warnings
