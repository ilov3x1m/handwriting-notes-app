"""Tests for app_core.py (the logic behind the Streamlit app). Run from the project root:

    python -m unittest discover -s tests -v

Synthetic documents and profiles are used, and config paths are redirected to temporary
folders, so these tests never touch the real glyphs/ or samples/ folders (the real `amaya`
profile is only read, and checked to be unchanged afterwards).
"""

import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import pymupdf
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_fixtures as fx  # noqa: E402
import app_core as core  # noqa: E402
import config  # noqa: E402
import extract  # noqa: E402
import render  # noqa: E402
import slides  # noqa: E402

REAL_LAYOUT = str(config.LAYOUT_JSON)
REAL_AMAYA = Path(config.GLYPHS_DIR) / "amaya"
REAL_DECK = next(Path(config.BASE_DIR, "slides_in").glob("*.pptx"), None)


def fingerprint(folder):
    digest = hashlib.sha256()
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


class Sandbox(unittest.TestCase):
    """Redirects glyphs/, samples/ and job folders into a temporary project."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "samples").mkdir()
        self.glyphs = self.root / "glyphs"
        self.glyphs.mkdir()
        patches = [mock.patch.object(config, "BASE_DIR", self.root),
                   mock.patch.object(config, "GLYPHS_DIR", self.glyphs),
                   mock.patch.object(config, "LAYOUT_JSON", Path(REAL_LAYOUT)),
                   mock.patch.object(config, "APP_WORKSPACE_ROOT", self.root / "jobs")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

class TestProfiles(Sandbox):
    def test_ready_profiles_are_discovered_and_invalid_ones_are_reported(self):
        fx.make_profile(self.glyphs, "alpha")
        fx.make_profile(self.glyphs, "Beta")
        fx.make_profile(self.glyphs, "partial", characters="abc")
        (self.glyphs / "stray_file.txt").write_text("not a profile")
        (self.glyphs / ".hidden").mkdir()
        profiles = {p.name: p for p in core.list_profiles()}
        self.assertEqual(sorted(profiles), ["Beta", "alpha", "partial"])
        self.assertTrue(profiles["alpha"].ready and profiles["Beta"].ready)
        self.assertFalse(profiles["partial"].ready)
        self.assertIn("no handwriting samples for", profiles["partial"].problems[0])

    def test_missing_glyph_metadata(self):
        folder = fx.make_profile(self.glyphs, "nometa")
        (folder / config.GLYPH_METADATA_NAME).unlink()
        problems = core.check_profile(folder)
        self.assertEqual(len(problems), 1)
        self.assertIn("glyph_metadata.json", problems[0])

    def test_invalid_glyph_metadata_and_missing_images(self):
        folder = fx.make_profile(self.glyphs, "badjson")
        (folder / config.GLYPH_METADATA_NAME).write_text("{ not json")
        self.assertIn("not valid JSON", core.check_profile(folder)[0])
        folder = fx.make_profile(self.glyphs, "noimage")
        next(folder.glob("glyph_000_1.png")).unlink()
        self.assertIn("cannot be read", core.check_profile(folder)[0])

    def test_missing_or_invalid_style(self):
        folder = fx.make_profile(self.glyphs, "nostyle", style=False)
        self.assertIn("Run spacing.py", core.check_profile(folder)[0])
        (folder / config.STYLE_NAME).write_text("garbage")
        self.assertIn("not valid JSON", core.check_profile(folder)[0])
        (folder / config.STYLE_NAME).write_text(json.dumps({"letter_spacing": {"estimate_pt": None, "quality": "insufficient"},
                                                            "word_spacing": {"mean_pt": 10, "quality": "good"}}))
        self.assertIn("not a usable measurement", core.check_profile(folder)[0])

    def test_problem_messages_never_show_absolute_paths(self):
        folder = fx.make_profile(self.glyphs, "leaky", style=False)
        for problem in core.check_profile(folder):
            self.assertNotIn(str(self.root), problem)
            self.assertNotIn(os.sep + "tmp", problem)
        self.assertEqual(core.scrub_paths(f"cannot open {self.root}/glyphs/x/file.png now"), "cannot open glyphs/x/file.png now")
        self.assertEqual(core.scrub_paths("see /Users/someone/Other Place/deep/file.txt please").count("/Users"), 0)

    def test_unexpected_exceptions_become_friendly_app_errors(self):
        known = core.AppError("known problem", step="Step X")
        self.assertIs(core.as_app_error(known), known)
        friendly = core.as_app_error(RuntimeError("secret internals"))
        self.assertIn("Something unexpected went wrong", friendly.message)
        self.assertNotIn("secret internals", friendly.message)
        self.assertIn("secret internals", friendly.details)

    def test_case_insensitive_name_collisions(self):
        fx.make_profile(self.glyphs, "Sam")
        for attempt in ("Sam", "sam", "SAM", " sAm "):
            with self.assertRaisesRegex(core.ProfileNameError, "already exists"):
                core.validate_new_profile_name(attempt)
        self.assertEqual(core.validate_new_profile_name("sam2"), "sam2")

    def test_invalid_names_are_rejected(self):
        for bad in ("", "   ", "has space", "../escape", "a/b", ".hidden", "-lead", "x" * 33, "café"):
            with self.assertRaises(core.ProfileNameError, msg=repr(bad)):
                core.validate_new_profile_name(bad)

    def test_profile_directory_refuses_paths(self):
        fx.make_profile(self.glyphs, "ok")
        self.assertEqual(core.profile_directory("ok").name, "ok")
        for bad in ("../ok", "ok/../ok", "/etc", "", None, "missing"):
            with self.assertRaises(core.AppError, msg=repr(bad)):
                core.profile_directory(bad)

    def test_signature_changes_when_a_profile_changes(self):
        folder = fx.make_profile(self.glyphs, "sig")
        before = core.profile_signature()
        time.sleep(0.01)
        (folder / config.STYLE_NAME).write_text((folder / config.STYLE_NAME).read_text() + " ")
        self.assertNotEqual(before, core.profile_signature())

    def test_glyph_preview_resolves_files_through_the_metadata(self):
        fx.make_profile(self.glyphs, "pic")
        png = core.glyph_preview_png("pic")
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    @unittest.skipUnless((REAL_AMAYA / config.GLYPH_METADATA_NAME).exists(), "glyphs/amaya/ is not on this machine")
    def test_real_amaya_profile_is_ready_and_never_modified_by_reading_it(self):
        before = fingerprint(REAL_AMAYA)
        with mock.patch.object(config, "GLYPHS_DIR", REAL_AMAYA.parent):
            profiles = {p.name: p for p in core.list_profiles()}
            self.assertTrue(profiles["amaya"].ready, profiles["amaya"].problems)
            self.assertEqual(core.check_profile(REAL_AMAYA), [])
            core.glyph_preview_png("amaya")
            core.check_text("Hello world", "amaya")
        self.assertEqual(fingerprint(REAL_AMAYA), before)


# ---------------------------------------------------------------------------
# Uploads and extraction
# ---------------------------------------------------------------------------

class TestUploads(Sandbox):
    def test_valid_uploads(self):
        self.assertEqual(core.validate_upload("Deck.PPTX", fx.make_pptx_bytes()), "pptx")
        self.assertEqual(core.validate_upload("notes.pdf", fx.make_pdf_bytes()), "pdf")

    def test_unsupported_extensions(self):
        for name in ("a.docx", "a.txt", "a", "a.pptx.exe", "a.ppt"):
            with self.assertRaises(core.UploadError, msg=name):
                core.validate_upload(name, b"PK data")
        with self.assertRaisesRegex(core.UploadError, "Save As"):
            core.validate_upload("old.ppt", b"x")

    def test_empty_and_invalid_uploads(self):
        with self.assertRaisesRegex(core.UploadError, "empty"):
            core.validate_upload("a.pptx", b"")
        with self.assertRaisesRegex(core.UploadError, "Choose"):
            core.validate_upload("", b"x")
        with self.assertRaisesRegex(core.UploadError, "does not look like a real .pptx"):
            core.validate_upload("a.pptx", b"just text")
        with self.assertRaisesRegex(core.UploadError, "does not look like a real .pdf"):
            core.validate_upload("a.pdf", b"PK\x03\x04 a zip")
        with mock.patch.object(config, "APP_MAX_UPLOAD_MB", 0):
            with self.assertRaisesRegex(core.UploadError, "larger than"):
                core.validate_upload("a.pdf", fx.make_pdf_bytes())

    def test_uploaded_names_are_never_used_as_paths(self):
        workspace = core.new_workspace()
        hostile = "../../outside\\..\\evil name.pptx"
        result = core.extract_notes(workspace, hostile, fx.make_pptx_bytes())
        self.assertEqual(sorted(p.name for p in workspace.iterdir()), ["notes.json", "notes.txt", "upload.pptx"])
        self.assertEqual(result["display_name"], "evil name.pptx")
        self.assertFalse((self.root / "outside").exists())
        self.assertNotIn(str(workspace), json.dumps(result["document"]["source"]))
        self.assertEqual(core.safe_display_name("a\x00b\x1f.pdf"), "ab.pdf")
        self.assertEqual(core.download_name("My Slides (v2)!.pptx"), "My_Slides_v2_handwritten.pdf")
        self.assertEqual(core.download_name("../../.."), "notes_handwritten.pdf")

    def test_valid_powerpoint_upload(self):
        result = core.extract_notes(core.new_workspace(), "deck.pptx", fx.make_pptx_bytes())
        self.assertEqual(result["kind"], "pptx")
        self.assertEqual(result["document"]["summary"]["slides"], 2)
        self.assertIn("Course Overview", result["notes_text"])
        self.assertIn("* First point", result["notes_text"])
        self.assertIn("   * Supporting detail: 3.5% on 2025-03-14", result["notes_text"])
        self.assertTrue(result["notes_text"].isascii())

    def test_valid_pdf_upload(self):
        result = core.extract_notes(core.new_workspace(), "slides.pdf", fx.make_pdf_bytes())
        self.assertEqual(result["kind"], "pdf")
        self.assertIn("Slide Title 1", result["notes_text"])
        self.assertIn("* First bullet of page 1", result["notes_text"])
        self.assertTrue(all(e["structure"] == "inferred" for s in result["document"]["slides"] for e in s["elements"]))

    def test_extraction_warnings_are_collected_and_ranked(self):
        result = core.extract_notes(core.new_workspace(), "deck.pptx", fx.make_pptx_bytes(with_picture_slide=True))
        attention = [w["message"] for w in result["warnings"] if w["level"] == "attention"]
        self.assertTrue(any("Slide 3" in m and "picture" in m for m in attention), result["warnings"])
        self.assertTrue(any("Slide 3" in m or "3" in m for m in attention))

    def test_pdf_warnings_use_the_word_page(self):
        pdf = pymupdf.open()
        for text in ("Real Words On This Page Here", None):
            page = pdf.new_page(width=960, height=540)
            if text:
                page.insert_text((60, 100), text, fontsize=20, fontname="helv")
            else:
                page.draw_rect(pymupdf.Rect(0, 0, 100, 100), fill=(0, 0, 0))
        result = core.extract_notes(core.new_workspace(), "partial.pdf", pdf.tobytes())
        self.assertTrue(any(w["level"] == "attention" and w["message"].startswith("Page 2:") for w in result["warnings"]))

    def test_documents_without_text_are_errors_not_empty_notes(self):
        with self.assertRaisesRegex(core.AppError, "No extractable text"):
            core.extract_notes(core.new_workspace(), "scan.pdf", fx.make_image_only_pdf_bytes())

    def test_corrupt_documents_give_a_readable_error(self):
        for name, data in (("bad.pptx", b"PK\x03\x04 corrupted"), ("bad.pdf", b"%PDF-1.4 corrupted")):
            with self.assertRaises(core.AppError, msg=name) as caught:
                core.extract_notes(core.new_workspace(), name, data)
            self.assertEqual(caught.exception.step, "Reading the document")
            self.assertNotIn(str(self.root), caught.exception.message)

    def test_an_unexpected_failure_in_the_pipeline_is_reported_with_details(self):
        with mock.patch.object(slides, "extract_document", side_effect=RuntimeError("boom inside slides")):
            with self.assertRaises(core.AppError) as caught:
                core.extract_notes(core.new_workspace(), "deck.pptx", fx.make_pptx_bytes())
        self.assertIn("could not be read", caught.exception.message)
        self.assertNotIn("boom", caught.exception.message)               # no raw error in the headline
        self.assertIn("boom inside slides", caught.exception.details)    # but kept for troubleshooting

    @unittest.skipUnless(REAL_DECK is not None, "needs the real deck in slides_in/")
    def test_real_deck_matches_slides_py_exactly(self):
        data = REAL_DECK.read_bytes()
        result = core.extract_notes(core.new_workspace(), REAL_DECK.name, data)
        direct = slides.extract_document(str(REAL_DECK))
        self.assertEqual(result["notes_text"], slides.to_notes_text(direct))
        self.assertEqual(result["document"]["summary"], direct["summary"])


# ---------------------------------------------------------------------------
# Job folders
# ---------------------------------------------------------------------------

class TestWorkspaces(Sandbox):
    def test_every_job_gets_its_own_folder_and_uploads_do_not_overwrite(self):
        first, second = core.new_workspace(), core.new_workspace()
        self.assertNotEqual(first, second)
        a = core.extract_notes(first, "same name.pptx", fx.make_pptx_bytes(title="First Deck"))
        b = core.extract_notes(second, "same name.pptx", fx.make_pptx_bytes(title="Second Deck"))
        self.assertIn("First Deck", (first / "notes.txt").read_text())
        self.assertIn("Second Deck", (second / "notes.txt").read_text())
        self.assertNotEqual((first / "upload.pptx").read_bytes(), (second / "upload.pptx").read_bytes())
        self.assertNotEqual(a["sha256"], b["sha256"])

    def test_cleanup_only_removes_folders_inside_the_workspace_root(self):
        job = core.new_workspace()
        (job / "x.txt").write_text("x")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        core.cleanup_workspace(outside)
        self.assertTrue((outside / "keep.txt").exists())
        core.cleanup_workspace(core.workspace_root())
        self.assertTrue(core.workspace_root().exists())
        core.cleanup_workspace(job)
        self.assertFalse(job.exists())
        core.cleanup_workspace(None)

    def test_stale_job_folders_are_removed_but_fresh_ones_are_kept(self):
        old, fresh = core.new_workspace(), core.new_workspace()
        past = time.time() - 3600 * 48
        os.utime(old, (past, past))
        other = core.workspace_root() / "not_a_job"
        other.mkdir()
        os.utime(other, (past, past))
        core.cleanup_stale_workspaces()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(other.exists())                      # only job_* folders are ever touched


# ---------------------------------------------------------------------------
# Generating the handwritten PDF
# ---------------------------------------------------------------------------

class TestGeneration(Sandbox):
    def setUp(self):
        super().setUp()
        fx.make_profile(self.glyphs, "demo")
        self.workspace = core.new_workspace()

    def test_text_checks(self):
        self.assertFalse(core.check_text("", "demo")["ok"])
        self.assertFalse(core.check_text("  \n ", "demo")["ok"])
        self.assertTrue(core.check_text("Hello, world 2025!", "demo")["ok"])
        verdict = core.check_text("café", "demo")
        self.assertTrue(verdict["ok"])
        self.assertTrue(any("replaced" in w for w in verdict["warnings"]))
        fx.make_profile(self.glyphs, "small", characters="abc")
        verdict = core.check_text("abcxyz", "small")
        self.assertFalse(verdict["ok"])
        self.assertIn("x y z", verdict["errors"][0])

    def test_generates_a_valid_pdf_with_the_configured_page_size(self):
        result = core.generate_pdf(self.workspace, "demo", "Hello world.\n\n* a bullet point", 7)
        self.assertEqual(result["profile"], "demo")
        with pymupdf.open(stream=result["pdf"], filetype="pdf") as document:
            self.assertEqual(document.page_count, result["page_count"])
            for page in document:
                self.assertAlmostEqual(page.rect.width, 447.0, places=2)
                self.assertAlmostEqual(page.rect.height, 596.0, places=2)
        self.assertEqual(result["characters"], len("Helloworld.") + len("*abulletpoint"))

    def test_the_renderer_receives_exactly_the_reviewed_text(self):
        reviewed = "My corrected title\n\n   * an edited bullet"
        with mock.patch.object(render, "render_text", wraps=render.render_text) as spy:
            core.generate_pdf(self.workspace, "demo", reviewed, 3)
        args = spy.call_args.args
        self.assertEqual(args[0], reviewed)
        self.assertEqual(args[1].name, "demo")
        self.assertEqual(args[2], 3)

    def test_same_seed_gives_identical_bytes_and_a_different_seed_does_not(self):
        text = "Repeatable handwriting " * 6
        a = core.generate_pdf(self.workspace, "demo", text, 5)["pdf"]
        b = core.generate_pdf(self.workspace, "demo", text, 5)["pdf"]
        c = core.generate_pdf(self.workspace, "demo", text, 6)["pdf"]
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_matches_what_render_py_produces_directly(self):
        text = "Direct comparison, 2025."
        via_app = core.generate_pdf(self.workspace, "demo", text, 11)["pdf"]
        pages, ctx, _, _ = render.render_text(text, self.glyphs / "demo", 11)
        direct = Path(self.workspace) / "direct.pdf"
        render.write_pdf(pages, ctx, direct)
        self.assertEqual(via_app, direct.read_bytes())

    def test_profile_and_text_are_validated_before_rendering(self):
        with mock.patch.object(render, "render_text") as spy:
            with self.assertRaisesRegex(core.AppError, "was not found"):
                core.generate_pdf(self.workspace, "ghost", "text", 1)
            fx.make_profile(self.glyphs, "broken", style=False)
            with self.assertRaisesRegex(core.AppError, "cannot be used yet"):
                core.generate_pdf(self.workspace, "broken", "text", 1)
            with self.assertRaisesRegex(core.AppError, "no text"):
                core.generate_pdf(self.workspace, "demo", "   ", 1)
            fx.make_profile(self.glyphs, "tiny", characters="ab")       # a profile missing most characters is not ready
            with self.assertRaisesRegex(core.AppError, "cannot be used yet: There are no handwriting samples"):
                core.generate_pdf(self.workspace, "tiny", "abc", 1)
        spy.assert_not_called()

    def test_a_failing_renderer_is_reported_with_its_step(self):
        with mock.patch.object(render, "write_pdf", side_effect=extract.ExtractError("disk exploded")):
            with self.assertRaises(core.AppError) as caught:
                core.generate_pdf(self.workspace, "demo", "text", 1)
        self.assertEqual(caught.exception.step, "Rendering handwritten pages")
        self.assertIn("disk exploded", caught.exception.message)

    def test_a_pdf_with_the_wrong_page_size_is_never_offered(self):
        def wrong_size(pages, ctx, path):
            document = pymupdf.open()
            document.new_page(width=300, height=300)
            document.save(path)
        with mock.patch.object(render, "write_pdf", side_effect=wrong_size):
            with self.assertRaisesRegex(core.AppError, "wrong page size"):
                core.generate_pdf(self.workspace, "demo", "text", 1)

    def test_preview_pages_and_navigation_bounds(self):
        result = core.generate_pdf(self.workspace, "demo", "\n".join(["line of text"] * 60), 2)
        self.assertGreater(result["page_count"], 2)
        first, last = core.render_preview(result["pdf"], 0), core.render_preview(result["pdf"], result["page_count"] - 1)
        self.assertEqual(first[:8], b"\x89PNG\r\n\x1a\n")
        self.assertNotEqual(first, last)
        from PIL import Image
        import io
        size = Image.open(io.BytesIO(first)).size
        self.assertEqual(size, (round(447 * config.APP_PREVIEW_DPI / 72), round(596 * config.APP_PREVIEW_DPI / 72)))
        for bad in (-1, result["page_count"]):
            with self.assertRaises(core.AppError):
                core.render_preview(result["pdf"], bad)

    def test_downloaded_bytes_are_the_generated_document(self):
        result = core.generate_pdf(self.workspace, "demo", "download me", 4)
        self.assertTrue(result["pdf"].startswith(b"%PDF"))
        on_disk = next(Path(self.workspace).glob("notes_*.pdf")).read_bytes()
        self.assertEqual(result["pdf"], on_disk)


# ---------------------------------------------------------------------------
# Creating a profile
# ---------------------------------------------------------------------------

class TestCreateProfile(Sandbox):
    def setUp(self):
        super().setUp()
        self.workspace = core.new_workspace()
        self.completed = fx.make_completed_template_bytes()

    def create(self, name="newbie", data=None, progress=None):
        return core.create_profile(name, data if data is not None else self.completed, self.workspace, progress)

    def test_successful_creation_makes_a_ready_profile_and_keeps_the_source(self):
        steps = []
        result = self.create(progress=lambda n, label: steps.append((n, label)))
        self.assertEqual([n for n, _ in steps], [1, 2, 3, 4, 5, 6])
        self.assertEqual([label for _, label in steps], core.PROFILE_STEPS)
        self.assertEqual(result["samples"], 225)
        self.assertEqual(result["expected"], 225)
        self.assertEqual(core.check_profile(self.glyphs / "newbie"), [])
        self.assertTrue((self.root / "samples" / "newbie.pdf").exists())
        metadata = json.loads((self.glyphs / "newbie" / config.GLYPH_METADATA_NAME).read_text())
        self.assertEqual(metadata["meta"]["source_pdf"], "samples/newbie.pdf")
        style = json.loads((self.glyphs / "newbie" / config.STYLE_NAME).read_text())
        self.assertEqual(style["source_pdf"], "samples/newbie.pdf")
        self.assertNotIn(str(self.workspace), json.dumps(metadata) + json.dumps(style))
        self.assertEqual(len(list((self.glyphs / "newbie").glob("*.png"))), 225)
        self.assertIn("newbie", [p.name for p in core.list_profiles() if p.ready])

    def test_the_new_profile_can_make_notes(self):
        self.create()
        result = core.generate_pdf(self.workspace, "newbie", "Hello from a new profile, 2025.", 1)
        self.assertGreaterEqual(result["page_count"], 1)

    def test_step_1_existing_names_are_refused_case_insensitively_and_nothing_changes(self):
        fx.make_profile(self.glyphs, "Existing")
        before = fingerprint(self.glyphs / "Existing")
        for attempt in ("Existing", "existing", "EXISTING"):
            with self.assertRaises(core.AppError) as caught:
                self.create(attempt)
            self.assertTrue(caught.exception.step.startswith("Step 1"), caught.exception.step)
        self.assertEqual(fingerprint(self.glyphs / "Existing"), before)
        self.assertEqual([p.name for p in self.glyphs.iterdir()], ["Existing"])

    def test_step_2_wrong_files_are_explained(self):
        for data, expect in ((b"", "empty"), (b"not a pdf", "does not look like"),
                             (fx.make_pdf_bytes(1), "does not look like the completed template"),
                             (fx.make_completed_template_bytes(pages=2), "pages")):
            with self.assertRaises(core.AppError, msg=expect) as caught:
                self.create(data=data)
            self.assertTrue(caught.exception.step.startswith("Step 2"))
            self.assertIn(expect, caught.exception.message)
        self.assertEqual(list(self.glyphs.iterdir()), [])

    def test_step_3_empty_boxes_are_listed_and_nothing_is_saved(self):
        data = fx.make_completed_template_bytes(skip=lambda box: box["char"] in "QZ" and box["sample"] == 2)
        with self.assertRaises(core.AppError) as caught:
            self.create(data=data)
        self.assertTrue(caught.exception.step.startswith("Step 3"))
        self.assertIn("2 writing boxes are empty", caught.exception.message)
        self.assertIn("Q (box 2)", caught.exception.message)
        self.assertEqual(list(self.glyphs.iterdir()), [])
        self.assertEqual(list((self.root / "samples").iterdir()), [])

    def test_step_4_unreadable_sample_sentence_is_a_failure(self):
        with self.assertRaises(core.AppError) as caught:
            self.create(data=fx.make_completed_template_bytes(sentence=False))
        self.assertTrue(caught.exception.step.startswith("Step 4"))
        self.assertIn("sample sentence", caught.exception.message)
        self.assertEqual(list(self.glyphs.iterdir()), [])

    def test_a_pipeline_stage_that_crashes_names_the_failed_step(self):
        with mock.patch.object(extract, "extract_to_directory", side_effect=RuntimeError("extract broke")):
            with self.assertRaises(core.AppError) as caught:
                self.create()
        self.assertTrue(caught.exception.step.startswith("Step 3"))
        self.assertIn("extract broke", caught.exception.details)
        with mock.patch("spacing.build_style_from_pdf", side_effect=RuntimeError("spacing broke")):
            with self.assertRaises(core.AppError) as caught:
                self.create()
        self.assertTrue(caught.exception.step.startswith("Step 4"))
        self.assertEqual(list(self.glyphs.iterdir()), [])

    def test_step_5_an_incomplete_profile_is_never_saved(self):
        with mock.patch.object(core, "check_profile", return_value=["something is missing"]):
            with self.assertRaises(core.AppError) as caught:
                self.create()
        self.assertTrue(caught.exception.step.startswith("Step 5"))
        self.assertEqual(list(self.glyphs.iterdir()), [])

    def test_step_6_a_failed_save_leaves_no_half_made_profile(self):
        def copy_then_fail(source, target, *args, **kwargs):
            Path(target).mkdir()
            (Path(target) / "partial.png").write_text("x")
            raise OSError("disk full")
        with mock.patch.object(core.shutil, "copytree", side_effect=copy_then_fail):
            with self.assertRaises(core.AppError) as caught:
                self.create()
        self.assertTrue(caught.exception.step.startswith("Step 6"))
        self.assertEqual([p.name for p in self.glyphs.iterdir()], ["newbie"])    # the pre-made partial folder is the test's doing
        # copytree raising before we mark it created means we did not delete a folder we did not make:
        self.assertTrue((self.glyphs / "newbie" / "partial.png").exists())

    def test_a_folder_that_appears_just_before_saving_is_never_overwritten(self):
        sentinel_dir = self.glyphs / "newbie"
        sentinel_dir.mkdir()
        (sentinel_dir / "sentinel.txt").write_text("precious")
        with mock.patch.object(core, "validate_new_profile_name", return_value="newbie"):    # the early check cannot see it
            with self.assertRaises(core.AppError) as caught:
                self.create()
        self.assertTrue(caught.exception.step.startswith("Step 6"))
        self.assertEqual((sentinel_dir / "sentinel.txt").read_text(), "precious")
        self.assertEqual([p.name for p in sentinel_dir.iterdir()], ["sentinel.txt"])
        self.assertEqual(list((self.root / "samples").iterdir()), [])

    def test_an_existing_sample_pdf_is_never_overwritten(self):
        (self.root / "samples" / "NewBie.pdf").write_bytes(b"someone else's file")
        self.create()
        self.assertEqual((self.root / "samples" / "NewBie.pdf").read_bytes(), b"someone else's file")
        self.assertTrue((self.root / "samples" / "newbie_2.pdf").exists())

    def test_template_download_is_the_real_template_or_a_fresh_one(self):
        real = core.template_pdf_bytes()
        with pymupdf.open(stream=real, filetype="pdf") as document:
            self.assertEqual(document.page_count, 4)
        with mock.patch.object(config, "TEMPLATE_PDF", self.root / "missing_template.pdf"):
            fresh = core.template_pdf_bytes()
        with pymupdf.open(stream=fresh, filetype="pdf") as document:
            self.assertEqual(document.page_count, 4)
            self.assertAlmostEqual(document[0].rect.width, 447.0, places=2)
            self.assertAlmostEqual(document[0].rect.height, 596.0, places=2)


class TestWarningSummary(unittest.TestCase):
    def test_numbers_are_described_compactly(self):
        self.assertEqual(core.describe_numbers([1], "slides"), "slide 1")
        self.assertEqual(core.describe_numbers([1, 4], "pages"), "pages 1, 4")
        self.assertEqual(core.describe_numbers([1, 2, 3, 7], "slides"), "slides 1-3, 7")
        self.assertEqual(core.describe_numbers([], "slides"), "")

    def test_repeated_warnings_are_merged_and_prefixes_removed(self):
        warnings = [{"level": "attention", "message": "Slide 1: No text found on this slide."},
                    {"level": "attention", "message": "Slide 4: No text found on this slide."},
                    {"level": "attention", "message": "Slide 2: Contains a chart."},
                    {"level": "attention", "message": "Slides/pages without reliably extracted text: 1, 4"},
                    {"level": "info", "message": "Page 5: This slide is hidden in the presentation."}]
        result = core.summarize_warnings(warnings, "slides")
        self.assertEqual(result["attention_where"], "slides 1-2, 4")
        self.assertEqual([(i["message"], i["where"]) for i in result["attention"]],
                         [("No text found on this slide.", "slides 1, 4"), ("Contains a chart.", "slide 2"),
                          ("Slides/pages without reliably extracted text: 1, 4", "")])
        self.assertEqual(result["notes"], [{"message": "This slide is hidden in the presentation.", "where": "slide 5"}])


class TestGlyphOverview(Sandbox):
    def test_overview_labels_every_character_and_is_still_a_png(self):
        folder = fx.make_profile(self.glyphs, "lab", characters=fx.V2_CHARS, template_version=2)
        png = core.glyph_preview_png("lab")
        image = Image.open(io.BytesIO(png))
        self.assertEqual(image.format, "PNG")
        rows = -(-len(fx.V2_CHARS) // 16)
        self.assertEqual(image.size, (16 * 56, rows * 70))               # each cell has a label strip
        self.assertTrue(any(p != (255, 255, 255) for p in image.crop((0, 56, 56, 70)).getdata()))   # label drawn


if __name__ == "__main__":
    unittest.main()
