"""Everything the Streamlit app does that is not drawing screens.

app.py only shows things and collects clicks; this module does the work by calling the
existing pipeline (extract.py, spacing.py, slides.py, render.py, template.py). That keeps
it testable without a browser and means no pipeline logic is copied into the app.

Safety rules followed here
  * Uploaded file names are never used as paths. Uploads are saved as upload.pptx /
    upload.pdf inside a fresh, unique job folder.
  * Each job gets its own folder under workspace_root(); jobs cannot overwrite each other.
  * A new handwriting profile is built in a staging folder and copied into glyphs/ only
    after every check passes. An existing profile is never modified or replaced.
  * No shell commands are run; the pipeline is imported and called directly.
  * Messages shown to people have absolute file paths removed (scrub_paths).
"""

import collections
import hashlib
import io
import os
import re
import shutil
import string
import tempfile
import time
import traceback
from pathlib import Path

import pymupdf
from PIL import Image, ImageDraw, ImageFont

import config
import extract
import render
import slides
import spacing
import template
from extract import ExtractError

REQUIRED_CHARS = string.ascii_uppercase + string.ascii_lowercase + string.digits
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
ATTENTION_WORDS = ("picture", "image", "chart", "smartart", "diagram", "no text", "no selectable text",
                   "unreadable", "not extracted", "without reliably", "hidden")

Profile = collections.namedtuple("Profile", "name ready problems")


class AppError(Exception):
    """A problem to show the person, with the step that failed and optional technical details."""

    def __init__(self, message, step=None, details=None):
        super().__init__(message)
        self.message, self.step, self.details = message, step, details


class ProfileNameError(AppError):
    pass


class UploadError(AppError):
    pass


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def scrub_paths(message):
    """Remove absolute file system paths from a message so people only see file names."""
    text = str(message)
    # Longest first: the project or a job folder may live inside the temp folder.
    for prefix in sorted({str(config.BASE_DIR), str(workspace_root()), tempfile.gettempdir()}, key=len, reverse=True):
        text = text.replace(prefix.rstrip("/") + "/", "")
    return re.sub(r"(?<![\w.])/(?:[^\s'\"():,]+/)+([^\s'\"():,]+)", r"\1", text)


def as_app_error(error):
    """Any exception as an AppError: expected ones pass through, surprises become a friendly message."""
    if isinstance(error, AppError):
        return error
    return AppError("Something unexpected went wrong. Nothing was saved. If it keeps happening, "
                    "open the technical details below and share them.", details=technical_details(error))


def technical_details(error):
    """Traceback text for a troubleshooting panel (paths are left in, it is for developers)."""
    return "".join(traceback.format_exception(type(error), error, error.__traceback__))


# ---------------------------------------------------------------------------
# Job folders
# ---------------------------------------------------------------------------

def workspace_root():
    root = Path(config.APP_WORKSPACE_ROOT) if config.APP_WORKSPACE_ROOT else Path(tempfile.gettempdir()) / "handwriting_notes_app"
    root.mkdir(parents=True, exist_ok=True)
    return root


def new_workspace():
    """A fresh folder no other job shares."""
    return Path(tempfile.mkdtemp(prefix="job_", dir=workspace_root()))


def cleanup_workspace(path):
    """Delete a job folder, but only if it really is inside the workspace root."""
    if not path:
        return
    target, root = Path(path).resolve(), workspace_root().resolve()
    if target != root and root in target.parents:
        shutil.rmtree(target, ignore_errors=True)


def cleanup_stale_workspaces(max_age_hours=None):
    """Remove job folders left behind by sessions that ended long ago."""
    limit = time.time() - 3600 * (config.APP_STALE_WORKSPACE_HOURS if max_age_hours is None else max_age_hours)
    for child in workspace_root().iterdir():
        if child.name.startswith("job_") and child.is_dir() and child.stat().st_mtime < limit:
            shutil.rmtree(child, ignore_errors=True)


# ---------------------------------------------------------------------------
# Handwriting profiles
# ---------------------------------------------------------------------------

def profile_version(glyph_dir):
    """Template version a profile was made from (1 for profiles that predate the field)."""
    import json
    try:
        with open(Path(glyph_dir) / config.GLYPH_METADATA_NAME, encoding="utf-8") as f:
            version = json.load(f)["meta"].get("template_version", 1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return 1
    return version if version in config.TEMPLATE_VERSIONS else 1


def required_chars(version):
    """Characters a complete profile of this template version must have."""
    return REQUIRED_CHARS + (config.PUNCTUATION_CHARS if version >= 2 else "")


def check_profile(glyph_dir):
    """List of problems that stop a profile folder from being used; an empty list means it is ready."""
    glyph_dir = Path(glyph_dir)
    if not (glyph_dir / config.GLYPH_METADATA_NAME).is_file():
        return ["The glyph metadata file (glyph_metadata.json) is missing. Create this profile again from a completed template."]
    try:
        library = render.load_glyph_library(glyph_dir)
    except ExtractError as error:
        return [scrub_paths(error)]
    problems = []
    missing = [c for c in required_chars(profile_version(glyph_dir)) if c not in library["samples"]]
    if missing:
        problems.append("There are no handwriting samples for: " + " ".join(missing))
    try:
        render.load_style(glyph_dir)
    except ExtractError as error:
        problems.append(scrub_paths(error))
    return problems


def list_profiles():
    """Every folder in glyphs/ as a Profile(name, ready, problems), sorted by name."""
    root = Path(config.GLYPHS_DIR)
    if not root.is_dir():
        return []
    profiles = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if child.is_dir() and NAME_PATTERN.fullmatch(child.name):
            problems = check_profile(child)
            profiles.append(Profile(child.name, not problems, problems))
    return profiles


def profile_signature():
    """Changes whenever a profile's metadata changes; used by the app to cache list_profiles()."""
    root = Path(config.GLYPHS_DIR)
    if not root.is_dir():
        return (str(root),)
    stamps = [str(root)]
    for child in sorted(root.iterdir()):
        if child.is_dir():
            for name in (config.GLYPH_METADATA_NAME, config.STYLE_NAME):
                path = child / name
                stamps.append((child.name, name, path.stat().st_mtime_ns if path.exists() else None))
    return tuple(stamps)


def profile_directory(name):
    """glyphs/<name> for a profile that exists; refuses names that are not plain profile names."""
    if not NAME_PATTERN.fullmatch(name or ""):
        raise AppError("That is not a valid profile name.")
    path = Path(config.GLYPHS_DIR) / name
    if not path.is_dir():
        raise AppError(f"The handwriting profile '{name}' was not found.")
    return path


def profile_characters(name):
    """Characters a profile has samples for, read from its metadata file."""
    import json
    with open(profile_directory(name) / config.GLYPH_METADATA_NAME, encoding="utf-8") as f:
        return {g["char"] for g in json.load(f)["glyphs"]}


def validate_new_profile_name(name):
    """The cleaned name, or ProfileNameError. Names are case-insensitive because macOS is."""
    name = (name or "").strip()
    if not name:
        raise ProfileNameError("Enter a name for the new profile.", step=1)
    if len(name) > config.APP_PROFILE_NAME_MAX or not NAME_PATTERN.fullmatch(name):
        raise ProfileNameError(
            f"Use only letters, digits, '-' and '_' (up to {config.APP_PROFILE_NAME_MAX} characters), "
            "starting with a letter or digit.", step=1)
    root = Path(config.GLYPHS_DIR)
    existing = [d.name for d in root.iterdir() if d.is_dir()] if root.is_dir() else []
    clash = next((e for e in existing if e.casefold() == name.casefold()), None)
    if clash:
        raise ProfileNameError(f"A profile called '{clash}' already exists. Profile names are not case-sensitive, "
                               "so choose a different name.", step=1)
    return name


def glyph_preview_png(name):
    """A picture of one sample of every character in a profile, using the metadata to find the files."""
    import json
    directory = profile_directory(name)
    with open(directory / config.GLYPH_METADATA_NAME, encoding="utf-8") as f:
        records = json.load(f)["glyphs"]
    first = {}
    for record in records:
        first.setdefault(record["char"], record)
    chars = [c for c in required_chars(2) if c in first]
    columns, cell, label_height = 16, 56, 14
    row_height = cell + label_height
    sheet = Image.new("RGB", (columns * cell, -(-len(chars) // columns) * row_height), "white")
    draw, font = ImageDraw.Draw(sheet), ImageFont.load_default(size=12)
    for index, char in enumerate(chars):
        glyph = Image.open(directory / first[char]["file"]).convert("RGBA")
        glyph.thumbnail((cell - 10, cell - 10), Image.LANCZOS)
        x, y = (index % columns) * cell, (index // columns) * row_height
        sheet.paste(glyph, (x + (cell - glyph.width) // 2, y + (cell - glyph.height) // 2), glyph)
        cx, cy = x + cell / 2, y + cell + label_height / 2 - 2
        if char == "\u2022":                                  # the small default font has no bullet character
            draw.ellipse([cx - 2.5, cy - 2.5, cx + 2.5, cy + 2.5], fill=(60, 60, 60))
        else:
            draw.text((cx, cy), char, fill=(60, 60, 60), font=font, anchor="mm")
    buffer = io.BytesIO()
    sheet.save(buffer, "PNG")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Uploaded slides
# ---------------------------------------------------------------------------

def safe_display_name(filename):
    """A harmless name for showing and for naming downloads. Never used as a file path."""
    name = os.path.basename((filename or "").replace("\\", "/"))
    name = "".join(c for c in name if c.isprintable())
    return name[:80] or "document"


def validate_upload(filename, data):
    """Check an upload before it is processed. Returns "pptx" or "pdf", or raises UploadError."""
    if not filename:
        raise UploadError("Choose a PowerPoint (.pptx) or PDF (.pdf) file first.")
    suffix = os.path.splitext(safe_display_name(filename))[1].lower()
    if suffix == ".ppt":
        raise UploadError("Old .ppt files are not supported. In PowerPoint choose File > Save As > .pptx, then upload that.")
    if suffix not in config.APP_ALLOWED_UPLOADS:
        shown = suffix or "this kind of"
        raise UploadError(f"'{shown}' files are not supported. Upload a PowerPoint (.pptx) or a text-based PDF (.pdf).")
    if not data:
        raise UploadError("The file is empty. Choose a different file.")
    if len(data) > config.APP_MAX_UPLOAD_MB * 1024 * 1024:
        raise UploadError(f"The file is larger than {config.APP_MAX_UPLOAD_MB} MB.")
    kind = suffix[1:]
    header_ok = data[:2] == b"PK" if kind == "pptx" else b"%PDF" in data[:1024]
    if not header_ok:
        raise UploadError(f"This does not look like a real .{kind} file (its contents are not in that format). "
                          "It may be damaged or renamed from another type.")
    return kind


def collect_warnings(document):
    """Warnings from the extractor as a list of {"level": "attention"|"info", "message": str}.

    "attention" is for anything that probably means text is missing (pictures, charts,
    SmartArt, pages without text), so the screen can show it prominently.
    """
    label = "Slide" if document["source"]["format"] == "pptx" else "Page"
    found = []
    for slide in document["slides"]:
        for warning in slide["warnings"]:
            found.append((f"{label} {slide['number']}: {warning}", warning))
    for warning in document["warnings"]:
        found.append((warning, warning))
    return [{"level": "attention" if any(w in raw.lower() for w in ATTENTION_WORDS) else "info", "message": message}
            for message, raw in found]


_NUMBERED = re.compile(r"^(?:Slide|Page) (\d+): (.*)$", re.S)


def describe_numbers(numbers, unit):
    """[1, 2, 3, 7] -> 'slides 1-3, 7'. `unit` is 'slides' or 'pages'."""
    if not numbers:
        return ""
    runs, start, previous = [], numbers[0], numbers[0]
    for number in numbers[1:] + [None]:
        if number is not None and number == previous + 1:
            previous = number
            continue
        runs.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = number
    return (unit[:-1] if len(numbers) == 1 else unit) + " " + ", ".join(runs)


def summarize_warnings(warnings, unit):
    """Merge repeated slide/page warnings: one entry per distinct message, with the slides it applies to.

    Returns {"attention": [{"message", "where"}], "attention_where": str, "notes": [...]}; "where" is e.g.
    'slides 1, 4' (empty for warnings about the whole document).
    """
    groups = {"attention": {}, "info": {}}
    for warning in warnings:
        match = _NUMBERED.match(warning["message"])
        message, number = (match.group(2), int(match.group(1))) if match else (warning["message"], None)
        entry = groups[warning["level"]].setdefault(message, [])
        if number is not None and number not in entry:
            entry.append(number)
    def listing(group):
        return [{"message": m, "where": describe_numbers(sorted(n), unit)} for m, n in group.items()]
    every = sorted({n for numbers in groups["attention"].values() for n in numbers})
    return {"attention": listing(groups["attention"]), "attention_where": describe_numbers(every, unit),
            "notes": listing(groups["info"])}


def extract_notes(workspace, filename, data):
    """Read an uploaded .pptx/.pdf with slides.py. Returns a dict for the review screen."""
    kind = validate_upload(filename, data)
    path = Path(workspace) / f"upload.{kind}"          # the uploaded name is never used as a path
    path.write_bytes(data)
    try:
        document = slides.extract_document(str(path))
    except ExtractError as error:
        raise AppError(scrub_paths(error), step="Reading the document")
    except Exception as error:                          # a damaged file can fail in many ways
        raise AppError("The document could not be read. It may be damaged or in an unusual format.",
                       step="Reading the document", details=technical_details(error))
    display = safe_display_name(filename)
    document["source"]["file"] = display                # keep the temporary path out of everything shown
    document["source"]["path"] = display
    notes = slides.to_notes_text(document)
    slides.write_text(Path(workspace) / "notes.txt", notes)
    import json
    slides.write_text(Path(workspace) / "notes.json", json.dumps(document, indent=2) + "\n")
    return {"kind": kind, "display_name": display, "document": document, "notes_text": notes,
            "warnings": collect_warnings(document), "sha256": hashlib.sha256(data).hexdigest()}


# ---------------------------------------------------------------------------
# Making the handwritten PDF
# ---------------------------------------------------------------------------

def check_text(text, profile_name):
    """Can this text be written with this profile? Returns {"ok", "errors", "warnings"}."""
    errors, warnings = [], []
    if not (text or "").strip():
        return {"ok": False, "errors": ["There is no text to write. Type or paste some text."], "warnings": []}
    available = set()
    if profile_name:
        try:
            available = profile_characters(profile_name)
        except (AppError, OSError, KeyError, ValueError):
            available = set()
    normalized, notes = render.normalize_text(text, keep=render.BULLET if render.BULLET in available else "")
    warnings.extend(notes)
    if profile_name:
        missing = sorted({c for c in normalized if c.isalnum() and c not in available})
        if missing:
            errors.append("This profile has no handwriting for: " + " ".join(missing))
    return {"ok": not errors, "errors": errors, "warnings": warnings}


def text_fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generate_pdf(workspace, profile_name, text, seed):
    """Render the REVIEWED text with render.py. Returns the PDF bytes and facts about it."""
    directory = profile_directory(profile_name)
    problems = check_profile(directory)
    if problems:
        raise AppError(f"The profile '{profile_name}' cannot be used yet: {problems[0]}", step="Checking the handwriting profile")
    verdict = check_text(text, profile_name)
    if not verdict["ok"]:
        raise AppError(verdict["errors"][0], step="Checking the text")
    try:
        pages, ctx, warnings, normalized = render.render_text(text, directory, seed)
        path = Path(workspace) / f"notes_{text_fingerprint(text)[:12]}_{seed}.pdf"
        render.write_pdf(pages, ctx, path)
    except ExtractError as error:
        raise AppError(scrub_paths(error), step="Rendering handwritten pages")
    data = path.read_bytes()
    with pymupdf.open(stream=data, filetype="pdf") as document:
        sizes = {(round(p.rect.width, 2), round(p.rect.height, 2)) for p in document}
        count = document.page_count
    if sizes != {(float(config.PAGE_WIDTH), float(config.PAGE_HEIGHT))}:
        raise AppError("The generated PDF has the wrong page size, so it was not offered for download.",
                       step="Preparing the preview", details=str(sizes))
    return {"pdf": data, "page_count": count, "seed": seed, "profile": profile_name,
            "text_hash": text_fingerprint(text), "warnings": verdict["warnings"] + warnings,
            "characters": sum(len(line["items"]) for page in pages for line in page["lines"])}


def render_preview(pdf_bytes, page_index, dpi=None):
    """PNG bytes of one page of a PDF (0-based), at preview resolution."""
    scale = (dpi or config.APP_PREVIEW_DPI) / 72
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as document:
        if not 0 <= page_index < document.page_count:
            raise AppError("That page does not exist.")
        return document[page_index].get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")


def download_name(display_name):
    stem = os.path.splitext(safe_display_name(display_name))[0]
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "notes"
    return f"{stem}_handwritten.pdf"


# ---------------------------------------------------------------------------
# Creating a handwriting profile
# ---------------------------------------------------------------------------

PROFILE_STEPS = ["Check the profile name", "Check the uploaded PDF", "Extract the handwriting samples",
                 "Measure spacing from the sample sentence", "Validate the profile", "Save the profile"]


def template_pdf_bytes():
    """The blank template to download: the existing template.pdf, or one made with template.py."""
    if Path(config.TEMPLATE_PDF).is_file():
        data = Path(config.TEMPLATE_PDF).read_bytes()
        try:
            if pymupdf.open(stream=data, filetype="pdf").page_count == len(config.PAGES):   # must be the newest version
                return data
        except Exception:
            pass
    workspace = new_workspace()
    try:
        path = workspace / "template.pdf"
        template.write_pdf(template.build_layout(), path)
        return path.read_bytes()
    finally:
        cleanup_workspace(workspace)


def unique_sample_name(name):
    """File name for keeping the uploaded template in samples/ without touching an existing file."""
    folder = Path(config.BASE_DIR) / "samples"
    taken = {p.name.casefold() for p in folder.iterdir()} if folder.is_dir() else set()
    candidate, number = f"{name}.pdf", 2
    while candidate.casefold() in taken:
        candidate, number = f"{name}_{number}.pdf", number + 1
    return candidate


def create_profile(name, pdf_bytes, workspace, progress=None):
    """Build a handwriting profile from a completed template PDF.

    Calls progress(step_number, label) as each of PROFILE_STEPS starts. On failure raises
    AppError whose .step says which step failed; glyphs/ is only touched in the last step.
    """
    def start(number):
        if progress:
            progress(number, PROFILE_STEPS[number - 1])

    def fail(number, message, error=None):
        details = technical_details(error) if error is not None else None
        return AppError(message, step=f"Step {number}: {PROFILE_STEPS[number - 1]}", details=details)

    start(1)
    try:
        name = validate_new_profile_name(name)
    except ProfileNameError as error:
        raise fail(1, error.message)

    start(2)
    try:
        validate_upload("completed_template.pdf", pdf_bytes)
        upload = Path(workspace) / "completed_template.pdf"
        upload.write_bytes(pdf_bytes)
        document = extract.open_pdf(upload)
        layout_path = extract.layout_path_for_pdf(document)     # V1 or V2 by page count, never mixed
        if not layout_path.is_file():
            raise AppError("The template layout file (template_layout.json) is missing. Run template.py first.")
        layout = extract.load_layout(layout_path)
        extract.validate_pdf(document, layout)
    except UploadError as error:
        raise fail(2, error.message)
    except (AppError, ExtractError) as error:
        message = error.message if isinstance(error, AppError) else scrub_paths(error)
        raise fail(2, f"This does not look like the completed template. {message}")

    sample_name = unique_sample_name(name)
    label = f"samples/{sample_name}"
    staging = Path(workspace) / "staging" / name

    start(3)
    try:
        metadata, extract_warnings = extract.extract_to_directory(upload, layout_path, staging, source_label=label)
    except ExtractError as error:
        raise fail(3, scrub_paths(error))
    except Exception as error:
        raise fail(3, "Extracting the handwriting failed unexpectedly.", error)
    if metadata["missing"]:
        names = ", ".join(f"{m['char']} (box {m['sample']})" for m in metadata["missing"][:12])
        more = "" if len(metadata["missing"]) <= 12 else f" and {len(metadata['missing']) - 12} more"
        raise fail(3, f"{len(metadata['missing'])} writing boxes are empty: {names}{more}. "
                      "Fill in every box, export the PDF again and upload it.")

    start(4)
    try:
        style, _, _, _ = spacing.build_style_from_pdf(upload, layout_path, name, staging, source_label=label)
        spacing.write_json_atomic(staging / config.STYLE_NAME, style)
    except ExtractError as error:
        raise fail(4, f"The sample sentence on page 3 could not be measured: {scrub_paths(error)}")
    except Exception as error:
        raise fail(4, "Measuring the handwriting spacing failed unexpectedly.", error)

    start(5)
    problems = check_profile(staging)
    if problems or metadata["meta"]["extracted_samples"] != metadata["meta"]["expected_samples"]:
        raise fail(5, "The profile is not complete: " + "; ".join(problems or ["not every sample was extracted"]))

    start(6)
    target = Path(config.GLYPHS_DIR) / name
    created = False
    try:
        validate_new_profile_name(name)                      # check again, just before writing
        Path(config.GLYPHS_DIR).mkdir(parents=True, exist_ok=True)
        shutil.copytree(staging, target)                     # fails if the folder already exists
        created = True
        samples = Path(config.BASE_DIR) / "samples"
        samples.mkdir(exist_ok=True)
        shutil.copyfile(upload, samples / sample_name)       # keep the source PDF, as the other profiles do
        problems = check_profile(target)
        if problems:
            raise AppError("; ".join(problems))
    except Exception as error:
        if created:
            shutil.rmtree(target, ignore_errors=True)        # never leave a half-made profile behind
            (Path(config.BASE_DIR) / "samples" / sample_name).unlink(missing_ok=True)
        message = error.message if isinstance(error, AppError) else "The profile could not be saved."
        raise fail(6, message, None if isinstance(error, AppError) else error)

    return {"name": name, "samples": metadata["meta"]["extracted_samples"], "expected": metadata["meta"]["expected_samples"],
            "template_version": metadata["meta"]["template_version"], "style": style,
            "warnings": extract_warnings + style["warnings"], "sample_file": sample_name}
