"""UI-flow tests for app.py using Streamlit's built-in AppTest (no browser needed).

    python -m unittest discover -s tests -v

The app script runs in-process, so config paths are redirected to temporary folders and
the app sees a small synthetic profile set. The last class runs the real Chaucer deck
through the real `amaya` profile and is skipped if either is missing.
"""

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import pymupdf
import streamlit as st
from streamlit.testing.v1 import AppTest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app_fixtures as fx  # noqa: E402
import app_core as core  # noqa: E402
import config  # noqa: E402
import render  # noqa: E402

APP = str(Path(config.BASE_DIR) / "app.py")
REAL_LAYOUT = str(config.LAYOUT_JSON)
REAL_AMAYA = Path(config.GLYPHS_DIR) / "amaya"
REAL_DECK = next(Path(config.BASE_DIR, "slides_in").glob("*.pptx"), None)
NOTES, PROFILE = "Make handwritten notes", "Create a handwriting profile"
LONG_TEXT = "\n".join(["A line of handwritten text for the page."] * 70)


def button(at, label):
    return next(b for b in at.button if b.label == label)


def texts(elements):
    return [e.value for e in elements]


class AppCase(unittest.TestCase):
    """Runs app.py against a temporary project with profiles 'sam' (ready) and 'Broken' (unusable)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "samples").mkdir()
        self.glyphs = self.root / "glyphs"
        self.glyphs.mkdir()
        fx.make_profile(self.glyphs, "sam")
        fx.make_profile(self.glyphs, "Broken", style=False)
        for patch in (mock.patch.object(config, "BASE_DIR", self.root), mock.patch.object(config, "GLYPHS_DIR", self.glyphs),
                      mock.patch.object(config, "LAYOUT_JSON", Path(REAL_LAYOUT)),
                      mock.patch.object(config, "APP_WORKSPACE_ROOT", self.root / "jobs")):
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._tmp.cleanup)

    def start(self):
        at = AppTest.from_file(APP, default_timeout=120).run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    def upload(self, at, name, data):
        at.file_uploader[0].upload(name, data)
        return at.run()

    def extract(self, name="deck.pptx", data=None):
        at = self.start()
        self.upload(at, name, fx.make_pptx_bytes() if data is None else data)
        button(at, "Extract text").click()
        return at.run()

    def generate(self, at, text=None, seed=None):
        if text is not None:
            at.text_area[0].set_value(text)
        if seed is not None:
            at.number_input[0].set_value(seed)
        at.run()
        button(at, "Generate handwritten PDF").click()
        return at.run()


# ---------------------------------------------------------------------------
# Start-up and profile selection
# ---------------------------------------------------------------------------

class TestStartupAndProfiles(AppCase):
    def test_app_starts_and_shows_the_first_screen(self):
        at = self.start()
        self.assertIn("**Handwritten Notes**", [m.value for m in at.markdown])      # compact header, no repeated heading
        self.assertEqual(len(at.header), 0)
        self.assertEqual(len(at.sidebar.radio), 0)                                  # navigation is in the main area
        self.assertEqual(texts(at.subheader), ["1. Choose your handwriting", "2. Upload your slides"])
        self.assertEqual(at.radio(key="page").options, [NOTES, PROFILE])
        self.assertTrue(button(at, "Extract text").disabled)        # nothing uploaded yet

    def test_only_ready_profiles_can_be_selected_and_unusable_ones_are_explained(self):
        at = self.start()
        self.assertEqual(at.selectbox[0].options, ["sam"])
        labels = [e.label for e in at.expander]
        self.assertTrue(any("cannot be used" in label for label in labels), labels)
        explained = " ".join(m.value for m in at.markdown)
        self.assertIn("Broken", explained)
        self.assertIn("Run spacing.py", explained)
        self.assertNotIn(str(self.root), explained)

    def test_no_usable_profile_explains_how_to_create_one(self):
        for folder in list(self.glyphs.iterdir()):
            import shutil
            shutil.rmtree(folder)
        at = self.start()
        self.assertTrue(any("Create a handwriting profile" in w.value for w in at.warning))
        self.assertEqual(len(at.file_uploader), 0)                  # nothing to upload to yet

    def test_amaya_is_preferred_when_present(self):
        fx.make_profile(self.glyphs, "amaya")
        at = self.start()
        self.assertEqual(at.selectbox[0].value, "amaya")


# ---------------------------------------------------------------------------
# Uploading and extracting
# ---------------------------------------------------------------------------

class TestUploadAndExtract(AppCase):
    def test_powerpoint_upload_fills_the_editable_text(self):
        at = self.extract()
        self.assertEqual(len(at.error), 0, texts(at.error))
        self.assertEqual(texts(at.subheader)[2], "3. Check and edit the text")
        text = at.text_area[0].value
        self.assertIn("Course Overview", text)
        self.assertIn("* First point", text)
        self.assertIn("   * Supporting detail: 3.5% on 2025-03-14", text)
        self.assertFalse(button(at, "Generate handwritten PDF").disabled)
        self.assertEqual(at.session_state["extraction"]["kind"], "pptx")
        self.assertFalse(any("guessed" in i.value for i in at.info))        # PowerPoint structure is read, not guessed

    def test_pdf_upload_says_the_structure_was_guessed(self):
        at = self.extract("slides.pdf", fx.make_pdf_bytes())
        self.assertTrue(any("guessed" in i.value for i in at.info), texts(at.info))
        self.assertIn("Slide Title 1", at.text_area[0].value)
        self.assertEqual(at.session_state["extraction"]["kind"], "pdf")

    def test_warnings_about_unreadable_content_are_prominent(self):
        at = self.extract(data=fx.make_pptx_bytes(with_picture_slide=True))
        warnings = texts(at.warning)
        self.assertEqual(len(warnings), 1, warnings)                       # one summary, not one box per problem
        self.assertIn("missing or uncertain", warnings[0])
        self.assertIn("slide 3", warnings[0])
        details = " ".join(m.value for m in at.markdown)
        self.assertIn("picture", details)
        self.assertNotIn("Slide 3:", details)                               # no repeated prefix

    def test_progress_line_follows_the_workflow_and_seed_is_under_advanced(self):
        at = self.start()
        self.assertEqual(at.progress[0].proto.text, "Step 2 of 4: Upload your slides")
        at = self.extract()
        self.assertEqual(at.progress[0].proto.text, "Step 3 of 4: Check the text and make the PDF")
        self.assertEqual([e.label for e in at.expander if "Advanced" in e.label], ["Advanced options"])
        self.assertEqual(at.number_input(key="seed").value, config.APP_DEFAULT_SEED)      # the seed widget still exists
        button(at, "Generate handwritten PDF").click()
        at.run()
        self.assertEqual(at.progress[0].proto.text, "Step 4 of 4: Download your notes")
        self.assertEqual(len(at.success), 1, texts(at.success))

    def test_the_uploader_only_accepts_powerpoint_and_pdf_files(self):
        # Streamlit enforces this list in the browser and again on the server, so a .docx never reaches
        # the app; app_core.validate_upload (tested separately) rejects anything else with a clear message.
        at = self.start()
        self.assertEqual(at.file_uploader[0].proto.type, [".pptx", ".pdf"])

    def test_a_file_whose_contents_do_not_match_its_extension_is_rejected(self):
        at = self.extract("report.pdf", b"PK\x03\x04 this is really a zip file")
        self.assertTrue(any("does not look like a real .pdf" in e.value for e in at.error), texts(at.error))
        self.assertEqual(len(at.text_area), 0)
        self.assertIsNone(at.session_state["extraction"])

    def test_empty_upload_is_rejected(self):
        at = self.extract("empty.pptx", b"")
        self.assertTrue(any("empty" in e.value for e in at.error), texts(at.error))
        self.assertEqual(len(at.text_area), 0)

    def test_corrupt_documents_give_a_readable_error_and_no_notes(self):
        for name, data in (("broken.pptx", b"PK\x03\x04 not really a deck"), ("broken.pdf", b"%PDF-1.4 junk")):
            at = self.extract(name, data)
            self.assertTrue(any("could not be read" in e.value for e in at.error), (name, texts(at.error)))
            self.assertEqual(len(at.text_area), 0)
            self.assertNotIn(str(self.root), " ".join(texts(at.error)))

    def test_a_document_without_text_never_becomes_empty_notes(self):
        at = self.extract("scan.pdf", fx.make_image_only_pdf_bytes())
        self.assertTrue(any("No extractable text" in e.value for e in at.error), texts(at.error))
        self.assertEqual(len(at.text_area), 0)
        self.assertEqual(len([b for b in at.button if b.label == "Generate handwritten PDF"]), 0)

    def test_a_failing_extraction_stage_shows_the_failure_and_offers_no_generation(self):
        with mock.patch.object(core, "extract_notes", side_effect=core.AppError("The reader crashed.", step="Reading the document",
                                                                              details="Traceback: boom")):
            at = self.extract()
        self.assertTrue(any("The reader crashed." in e.value for e in at.error), texts(at.error))
        self.assertIsNone(at.session_state["extraction"])
        self.assertEqual(len(at.text_area), 0)

    def test_choosing_a_different_file_discards_the_old_text_and_its_folder(self):
        at = self.extract(data=fx.make_pptx_bytes(title="First Deck"))
        first_workspace = at.session_state["workspace"]
        self.assertTrue(Path(first_workspace).exists())
        at.file_uploader[0].clear()
        self.upload(at, "other.pptx", fx.make_pptx_bytes(title="Second Deck"))
        self.assertIsNone(at.session_state["extraction"])                   # must be extracted again
        self.assertFalse(Path(first_workspace).exists())
        self.assertEqual(len(at.text_area), 0)
        button(at, "Extract text").click()
        at.run()
        self.assertIn("Second Deck", at.text_area[0].value)
        self.assertNotIn("First Deck", at.text_area[0].value)

    def test_two_sessions_use_separate_folders(self):
        first, second = self.extract(), self.extract()
        a, b = first.session_state["workspace"], second.session_state["workspace"]
        self.assertNotEqual(a, b)
        self.assertTrue(Path(a).exists() and Path(b).exists())
        self.assertEqual(sorted(p.name for p in Path(a).iterdir()), ["notes.json", "notes.txt", "upload.pptx"])


# ---------------------------------------------------------------------------
# Editing, generating, previewing, downloading
# ---------------------------------------------------------------------------

class TestGenerate(AppCase):
    def test_generation_is_off_until_there_is_text(self):
        at = self.extract()
        at.text_area[0].set_value("   ")
        at.run()
        self.assertTrue(button(at, "Generate handwritten PDF").disabled)
        at.text_area[0].set_value("Some words")
        at.run()
        self.assertFalse(button(at, "Generate handwritten PDF").disabled)

    def test_the_renderer_gets_the_edited_text_not_the_original(self):
        at = self.extract()
        original = at.session_state["extraction"]["notes_text"]
        edited = "My corrected title\n\n* a point I added\n   * and a nested one"
        with mock.patch.object(render, "render_text", wraps=render.render_text) as spy:
            at = self.generate(at, edited)
        self.assertEqual(spy.call_args.args[0], edited)
        self.assertNotEqual(spy.call_args.args[0], original)
        self.assertEqual(at.session_state["result"]["text_hash"], core.text_fingerprint(edited))

    def test_successful_generation_shows_the_result_and_a_download_for_the_right_pdf(self):
        at = self.extract()
        captured = {}
        real = st.download_button

        def spy(label, data=None, file_name=None, **kwargs):
            captured.update(label=label, data=data, file_name=file_name, mime=kwargs.get("mime"))
            return real(label, data=data, file_name=file_name, **kwargs)

        with mock.patch.object(st, "download_button", side_effect=spy):
            at = self.generate(at, "Hello from the app, 2025.", seed=9)
        self.assertEqual(len(at.error), 0, texts(at.error))
        self.assertTrue(any("Your notes are ready" in s.value for s in at.success), texts(at.success))
        self.assertEqual(len(at.image), 1)
        self.assertEqual(captured["label"], "Download handwritten PDF")
        self.assertEqual(captured["file_name"], "deck_handwritten.pdf")
        self.assertEqual(captured["mime"], "application/pdf")
        # The offered bytes are exactly what the pipeline makes for that text, profile and seed.
        expected = core.generate_pdf(core.new_workspace(), "sam", "Hello from the app, 2025.", 9)["pdf"]
        self.assertEqual(captured["data"], expected)
        with pymupdf.open(stream=captured["data"], filetype="pdf") as document:
            self.assertEqual({(round(p.rect.width, 1), round(p.rect.height, 1)) for p in document}, {(447.0, 596.0)})

    def test_multi_page_navigation(self):
        at = self.extract()
        shown = []
        real = st.image

        def spy(data, *args, **kwargs):
            shown.append((data, kwargs.get("caption")))
            return real(data, *args, **kwargs)

        with mock.patch.object(st, "image", side_effect=spy):
            at = self.generate(at, LONG_TEXT)
            pages = at.session_state["result"]["page_count"]
            pdf = at.session_state["result"]["pdf"]
            self.assertGreater(pages, 2)
            self.assertEqual(at.session_state["preview_page"], 1)
            self.assertEqual(shown[-1], (core.render_preview(pdf, 0), f"Page 1 of {pages}"))
            button(at, "Next page").click()
            at.run()
            self.assertEqual(at.session_state["preview_page"], 2)
            self.assertEqual(shown[-1], (core.render_preview(pdf, 1), f"Page 2 of {pages}"))
            at.number_input(key="preview_page").set_value(pages)
            at.run()
            self.assertEqual(shown[-1], (core.render_preview(pdf, pages - 1), f"Page {pages} of {pages}"))
            button(at, "Previous page").click()
            at.run()
            self.assertEqual(at.session_state["preview_page"], pages - 1)
        self.assertNotEqual(core.render_preview(pdf, 0), core.render_preview(pdf, 1))   # the pages really differ

    def test_a_single_page_result_has_nothing_to_navigate(self):
        at = self.generate(self.extract(), "Just one short page")
        self.assertEqual(at.session_state["result"]["page_count"], 1)
        self.assertTrue(button(at, "Previous page").disabled)
        self.assertTrue(button(at, "Next page").disabled)

    def test_clicking_generate_again_with_nothing_changed_does_not_render_again(self):
        at = self.generate(self.extract(), "Same text twice")
        with mock.patch.object(render, "render_text", wraps=render.render_text) as spy:
            button(at, "Generate handwritten PDF").click()
            at.run()
        spy.assert_not_called()
        self.assertTrue(any("already up to date" in i.value for i in at.info), texts(at.info))

    def test_an_edit_after_generating_hides_the_download_until_regenerated(self):
        at = self.generate(self.extract(), "First version")
        self.assertEqual(len(at.get("download_button")), 1)
        at.text_area[0].set_value("Second version")
        at.run()
        self.assertTrue(any("has changed since this PDF was made" in w.value for w in at.warning), texts(at.warning))
        self.assertEqual(len(at.get("download_button")), 0)
        button(at, "Generate handwritten PDF").click()
        at.run()
        self.assertEqual(len(at.get("download_button")), 1)
        self.assertEqual(at.session_state["result"]["text_hash"], core.text_fingerprint("Second version"))

    def test_changing_the_seed_makes_new_pages(self):
        at = self.generate(self.extract(), "Seed test text", seed=1)
        first = at.session_state["result"]["pdf"]
        at = self.generate(at, seed=2)
        self.assertNotEqual(at.session_state["result"]["pdf"], first)
        self.assertEqual(at.session_state["result"]["seed"], 2)

    def test_a_failing_render_shows_an_error_and_never_a_success_state(self):
        at = self.extract()
        with mock.patch.object(core, "generate_pdf", side_effect=core.AppError("The renderer failed.", step="Rendering handwritten pages")):
            at = self.generate(at, "Some text")
        self.assertTrue(any("The PDF could not be made" in e.value and "Rendering handwritten pages" in e.value for e in at.error), texts(at.error))
        self.assertIsNone(at.session_state["result"])
        self.assertEqual(len(at.success), 0)
        self.assertEqual(len(at.get("download_button")), 0)
        self.assertFalse(at.session_state["busy"])                           # not stuck in "working"

    def test_an_unexpected_crash_is_shown_kindly_and_the_app_stays_usable(self):
        at = self.extract()
        with mock.patch.object(core, "generate_pdf", side_effect=RuntimeError("unexpected internal failure")):
            at.text_area[0].set_value("x y z")
            at.run()
            button(at, "Generate handwritten PDF").click()
            at.run()
        self.assertEqual(len(at.exception), 0)                                   # no raw traceback box
        self.assertTrue(any("Something unexpected went wrong" in e.value for e in at.error), texts(at.error))
        self.assertNotIn("unexpected internal failure", " ".join(texts(at.error)))   # kept out of the headline
        self.assertFalse(at.session_state["busy"])                                # not stuck in "working"
        self.assertIsNone(at.session_state["result"])
        self.assertFalse(button(at, "Generate handwritten PDF").disabled)         # can try again

    def test_an_unexpected_crash_while_reading_a_file_is_shown_kindly(self):
        with mock.patch.object(core, "extract_notes", side_effect=RuntimeError("reader exploded")):
            at = self.extract()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("Something unexpected went wrong" in e.value for e in at.error), texts(at.error))
        self.assertIsNone(at.session_state["extraction"])


# ---------------------------------------------------------------------------
# Creating a profile
# ---------------------------------------------------------------------------

class TestProfileScreen(AppCase):
    def open(self):
        at = self.start()
        at.radio(key="page").set_value(PROFILE).run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    def test_screen_lists_existing_profiles_and_the_steps(self):
        at = self.open()
        steps = texts(at.subheader)
        self.assertEqual([s[:2] for s in steps], ["1.", "2.", "3.", "4.", "5.", "6."])
        self.assertIn("Step 1 of 6", at.progress[0].proto.text)
        shown = " ".join(m.value for m in at.markdown)
        self.assertIn("sam (ready)", shown)
        self.assertIn("Broken (not usable)", shown)
        self.assertEqual(len(at.get("download_button")), 1)                  # the blank template
        self.assertTrue(button(at, "Create profile").disabled)

    def test_name_checks_including_case_insensitive_collisions(self):
        at = self.open()
        for taken in ("sam", "SAM", "Sam"):
            at.text_input(key="profile_name").set_value(taken).run()
            self.assertTrue(any("already exists" in e.value for e in at.error), (taken, texts(at.error)))
        at.text_input(key="profile_name").set_value("bad name!").run()
        self.assertTrue(any("Use only letters" in e.value for e in at.error))
        at.text_input(key="profile_name").set_value("fresh").run()
        self.assertEqual(len(at.error), 0)
        self.assertTrue(any("available" in c.value for c in at.caption))

    def test_creating_a_profile_end_to_end(self):
        at = self.open()
        at.file_uploader(key="profile_upload").upload("my template export.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("fresh").run()
        self.assertFalse(button(at, "Create profile").disabled)
        button(at, "Create profile").click()
        at.run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        self.assertEqual(len(at.error), 0, texts(at.error))
        self.assertTrue(any("fresh" in s.value and "is ready" in s.value for s in at.success), texts(at.success))
        self.assertEqual(core.check_profile(self.glyphs / "fresh"), [])
        self.assertEqual(len(at.image), 1)                                   # the glyph overview
        # the new profile is now selectable on the first screen, the old ones are untouched
        at.radio(key="page").set_value(NOTES).run()
        self.assertEqual(at.selectbox[0].options, ["fresh", "sam"])

    def test_use_this_profile_now_selects_it_on_the_notes_screen(self):
        at = self.open()
        at.file_uploader(key="profile_upload").upload("t.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("fresh").run()
        button(at, "Create profile").click()
        at.run()
        self.assertEqual(at.progress[0].proto.text, "Step 6 of 6: Profile created")
        self.assertEqual(len(at.success), 1, texts(at.success))                  # one confirmation
        button(at, "Use this profile now").click()
        at.run()
        self.assertEqual(at.radio(key="page").value, NOTES)
        self.assertEqual(at.selectbox(key="profile_choice").value, "fresh")
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])

    def test_a_failed_creation_explains_the_step_and_saves_nothing(self):
        at = self.open()
        blank_boxes = fx.make_completed_template_bytes(skip=lambda box: box["char"] == "M")
        at.file_uploader(key="profile_upload").upload("t.pdf", blank_boxes)
        at.text_input(key="profile_name").set_value("failing").run()
        button(at, "Create profile").click()
        at.run()
        errors = " ".join(texts(at.error))
        self.assertIn("Step 3", errors)
        self.assertIn("Extract the handwriting samples", errors)
        self.assertIn("writing boxes are empty", errors)
        self.assertTrue(any("Nothing was saved" in i.value for i in at.info))
        self.assertEqual(len(at.success), 0)
        self.assertFalse((self.glyphs / "failing").exists())
        self.assertIsNone(at.session_state["created"])
        self.assertFalse(at.session_state["busy"])

    def test_an_unexpected_crash_during_creation_is_shown_kindly_and_saves_nothing(self):
        at = self.open()
        at.file_uploader(key="profile_upload").upload("t.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("crashy").run()
        with mock.patch.object(core, "create_profile", side_effect=RuntimeError("creator exploded")):
            button(at, "Create profile").click()
            at.run()
        self.assertEqual(len(at.exception), 0)
        self.assertTrue(any("Something unexpected went wrong" in e.value for e in at.error), texts(at.error))
        self.assertTrue(any("Nothing was saved" in i.value for i in at.info))
        self.assertFalse((self.glyphs / "crashy").exists())
        self.assertFalse(at.session_state["busy"])

    def test_an_existing_profile_cannot_be_overwritten_from_the_screen(self):
        before = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((self.glyphs / "sam").iterdir()))).hexdigest()
        at = self.open()
        at.file_uploader(key="profile_upload").upload("t.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("SAM").run()
        self.assertTrue(button(at, "Create profile").disabled)               # cannot even be submitted
        after = hashlib.sha256(b"".join(p.read_bytes() for p in sorted((self.glyphs / "sam").iterdir()))).hexdigest()
        self.assertEqual(before, after)


# ---------------------------------------------------------------------------
# Milestone 7.1: user work survives navigation and is never lost by accident
# ---------------------------------------------------------------------------

REPLACE, KEEP = "Replace my edits and extract again", "Keep my edits"


def fingerprint(folder):
    digest = hashlib.sha256()
    for path in sorted(Path(folder).rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(folder).as_posix().encode() + path.read_bytes())
    return digest.hexdigest()


class PreserveWork(AppCase):
    def spy_extract(self, side_effect=None):
        """Count calls to core.extract_notes for the whole test (optionally replacing it)."""
        patcher = mock.patch.object(core, "extract_notes", side_effect=side_effect or core.extract_notes)
        spy = patcher.start()
        self.addCleanup(patcher.stop)
        return spy

    def go(self, at, screen):
        at.radio(key="page").set_value(screen).run()
        self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
        return at

    def extracted(self, data=None, name="deck.pptx"):
        """App on the notes screen with a deck already extracted."""
        return self.extract(name, data)

    def edit(self, at, text="My edited title\n\n* a bullet I wrote myself"):
        at.text_area[0].set_value(text).run()
        return text

    def shown_text(self, at):
        return " ".join(texts(at.info) + texts(at.warning) + texts(at.error) + [m.value for m in at.markdown])


class TestWorkSurvivesNavigation(PreserveWork):
    def test_upload_extraction_edits_profile_seed_and_result_survive_a_trip_to_the_other_screen(self):
        fx.make_profile(self.glyphs, "zed")
        spy = self.spy_extract()
        at = self.extracted(fx.make_pptx_bytes(with_picture_slide=True))
        original = at.session_state["extraction"]["notes_text"]
        workspace = at.session_state["workspace"]
        attention = [w["message"] for w in at.session_state["extraction"]["warnings"] if w["level"] == "attention"]
        self.assertTrue(attention)

        at.selectbox(key="profile_choice").set_value("zed")
        at.number_input(key="seed").set_value(11)
        edited = self.edit(at)
        button(at, "Generate handwritten PDF").click()
        at.run()
        pdf = at.session_state["result"]["pdf"]

        self.go(at, PROFILE)
        self.go(at, NOTES)

        self.assertEqual(at.session_state["extraction"]["notes_text"], original)       # original extraction kept
        self.assertEqual(at.session_state["workspace"], workspace)                      # same per-session folder
        self.assertTrue(Path(workspace).exists())
        self.assertEqual(at.text_area[0].value, edited)                                 # the user's edits
        self.assertEqual(at.selectbox(key="profile_choice").value, "zed")               # the selected profile
        self.assertEqual(at.number_input(key="seed").value, 11)                         # the seed
        self.assertEqual(at.session_state["result"]["pdf"], pdf)                        # the generated pages
        self.assertTrue(any("missing or uncertain" in w for w in texts(at.warning)))    # extraction summary still shown
        self.assertFalse(any("has changed since this PDF was made" in w for w in texts(at.warning)))   # not falsely stale
        self.assertEqual(len(at.get("download_button")), 1)                             # and the download is still offered
        self.assertFalse(button(at, "Generate handwritten PDF").disabled)
        self.assertEqual(spy.call_count, 1)                                             # never re-read the file

    def test_returning_does_not_ask_for_the_file_again_or_extract_again(self):
        spy = self.spy_extract()
        at = self.extracted()
        self.go(at, PROFILE)
        self.go(at, NOTES)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(at.error), 0)
        self.assertTrue(any("still loaded" in i.value and "deck.pptx" in i.value for i in at.info), texts(at.info))
        self.assertEqual(len(at.text_area), 1)                                          # the review step is still on screen
        self.assertFalse(button(at, "Generate handwritten PDF").disabled)

    def test_every_later_run_after_returning_keeps_the_job_too(self):
        """An empty uploader must not be mistaken for 'the file was removed' on the runs AFTER the return."""
        at = self.extracted()                                                           # note: the text is NOT edited
        workspace = at.session_state["workspace"]
        self.go(at, PROFILE)
        self.go(at, NOTES)
        for _ in range(3):
            at.run()                                                                    # ordinary reruns (typing, clicking...)
            self.assertIsNotNone(at.session_state["extraction"])
            self.assertEqual(at.session_state["workspace"], workspace)
            self.assertEqual(len(at.text_area), 1)
        button(at, "Generate handwritten PDF").click()
        at.run()
        self.assertIsNotNone(at.session_state["result"])                                # and generating still works

    def test_extracting_again_after_returning_uses_the_stored_copy_without_a_new_upload(self):
        spy = self.spy_extract()
        at = self.extracted()
        self.go(at, PROFILE)
        self.go(at, NOTES)
        self.assertEqual(len(at.file_uploader[0].value or []), 0)                       # the uploader really is empty
        self.assertFalse(button(at, "Extract text").disabled)
        button(at, "Extract text").click()
        at.run()
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(len(at.error), 0, texts(at.error))
        self.assertEqual(sorted(p.name for p in Path(at.session_state["workspace"]).iterdir()),
                         ["notes.json", "notes.txt", "upload.pptx"])                    # still only the fixed, safe file name

    def test_several_round_trips_keep_everything(self):
        at = self.extracted()
        edited = self.edit(at)
        for _ in range(3):
            self.go(at, PROFILE)
            self.go(at, NOTES)
        self.assertEqual(at.text_area[0].value, edited)

    def test_selecting_the_same_file_again_keeps_the_extraction(self):
        spy = self.spy_extract()
        data = fx.make_pptx_bytes()
        at = self.extracted(data)
        edited = self.edit(at)
        self.go(at, PROFILE)
        self.go(at, NOTES)
        self.upload(at, "deck.pptx", data)                                              # same bytes: not a new document
        self.assertEqual(at.text_area[0].value, edited)
        self.assertEqual(spy.call_count, 1)

    def test_removing_an_unedited_file_still_discards_the_job_and_nothing_comes_back(self):
        at = self.extracted()
        workspace = at.session_state["workspace"]
        at.file_uploader[0].clear()
        at.run()
        self.assertIsNone(at.session_state["extraction"])                               # nothing was edited: start fresh, as before
        self.assertFalse(Path(workspace).exists())
        self.go(at, PROFILE)
        self.go(at, NOTES)
        self.assertIsNone(at.session_state["extraction"])                               # and a trip away does not resurrect it
        self.assertEqual(len(at.text_area), 0)


class TestConfirmBeforeReplacingEdits(PreserveWork):
    def test_extracting_again_after_editing_asks_first(self):
        spy = self.spy_extract()
        at = self.extracted()
        edited = self.edit(at)
        button(at, "Extract text").click()
        at.run()
        self.assertTrue(any("replace your edits" in w.value.lower() for w in at.warning), texts(at.warning))
        self.assertEqual([b.label for b in at.button if b.label in (REPLACE, KEEP)], [REPLACE, KEEP])
        self.assertEqual(spy.call_count, 1)                                             # nothing was replaced yet
        self.assertEqual(at.text_area[0].value, edited)

    def test_cancelling_keeps_the_text_the_upload_and_the_results(self):
        spy = self.spy_extract()
        at = self.extracted()
        workspace = at.session_state["workspace"]
        edited = self.edit(at)
        button(at, "Generate handwritten PDF").click()
        at.run()
        pdf = at.session_state["result"]["pdf"]
        button(at, "Extract text").click()
        at.run()
        button(at, KEEP).click()
        at.run()
        self.assertEqual(at.text_area[0].value, edited)
        self.assertEqual(at.session_state["workspace"], workspace)
        self.assertEqual(at.session_state["result"]["pdf"], pdf)
        self.assertEqual(len([b for b in at.button if b.label in (REPLACE, KEEP)]), 0)  # the question is gone
        self.assertEqual(spy.call_count, 1)
        self.assertFalse(button(at, "Extract text").disabled)                           # the upload is still there

    def test_confirming_extracts_again_and_replaces_the_edits(self):
        spy = self.spy_extract()
        at = self.extracted()
        old_workspace = at.session_state["workspace"]
        original = at.session_state["extraction"]["notes_text"]
        self.edit(at)
        button(at, "Extract text").click()
        at.run()
        button(at, REPLACE).click()
        at.run()
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(len(at.error), 0, texts(at.error))
        self.assertEqual(at.text_area[0].value, original)                               # fresh text, edits gone
        self.assertNotEqual(at.session_state["workspace"], old_workspace)
        self.assertFalse(Path(old_workspace).exists())                                  # old folder cleaned up
        self.assertTrue(Path(at.session_state["workspace"]).exists())
        self.assertIsNone(at.session_state["result"])
        self.assertEqual(len([b for b in at.button if b.label in (REPLACE, KEEP)]), 0)

    def test_unedited_text_is_re_extracted_without_asking(self):
        spy = self.spy_extract()
        at = self.extracted()
        button(at, "Extract text").click()
        at.run()
        self.assertEqual(spy.call_count, 2)
        self.assertEqual(len([b for b in at.button if b.label in (REPLACE, KEEP)]), 0)
        self.assertFalse(any("replace your edits" in w.value.lower() for w in at.warning))

    def test_whitespace_only_differences_are_not_treated_as_edits(self):
        spy = self.spy_extract()
        at = self.extracted()
        at.text_area[0].set_value(at.session_state["extraction"]["notes_text"].rstrip() + "\n\n").run()
        button(at, "Extract text").click()
        at.run()
        self.assertEqual(spy.call_count, 2)

    def test_a_failed_re_extraction_keeps_the_edits_and_every_result(self):
        real = core.extract_notes
        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise core.AppError("The second read failed.", step="Reading the document")
            return real(*args, **kwargs)

        self.spy_extract(flaky)
        at = self.extracted()
        workspace = at.session_state["workspace"]
        extraction = at.session_state["extraction"]
        edited = self.edit(at)
        button(at, "Generate handwritten PDF").click()
        at.run()
        pdf = at.session_state["result"]["pdf"]
        button(at, "Extract text").click()
        at.run()
        button(at, REPLACE).click()
        at.run()
        self.assertTrue(any("The second read failed." in e.value for e in at.error), texts(at.error))
        self.assertTrue(any("previous text" in i.value.lower() and "kept" in i.value.lower() for i in at.info), texts(at.info))
        self.assertEqual(at.text_area[0].value, edited)                                 # edits intact
        self.assertIs(at.session_state["extraction"], extraction)
        self.assertEqual(at.session_state["workspace"], workspace)
        self.assertTrue(Path(workspace).exists())                                       # the old folder was not deleted
        self.assertEqual(at.session_state["result"]["pdf"], pdf)                        # results still usable
        self.assertEqual(len(at.get("download_button")), 1)
        self.assertFalse(at.session_state["busy"])
        self.assertEqual(len(list(Path(core.workspace_root()).glob("job_*"))), 1)      # the failed attempt left no folder

    def test_choosing_a_different_file_after_editing_does_not_discard_the_edits(self):
        spy = self.spy_extract()
        at = self.extracted(fx.make_pptx_bytes(title="First Deck"))
        edited = self.edit(at)
        at.file_uploader[0].clear()
        self.upload(at, "other.pptx", fx.make_pptx_bytes(title="Second Deck"))
        self.assertEqual(at.text_area[0].value, edited)                                 # still the user's text
        self.assertTrue(any("different file" in i.value.lower() for i in at.info), texts(at.info))
        button(at, "Extract text").click()
        at.run()
        self.assertEqual(spy.call_count, 1)                                             # asked first
        self.assertTrue(any(b.label == REPLACE for b in at.button))
        button(at, REPLACE).click()
        at.run()
        self.assertEqual(spy.call_count, 2)
        self.assertIn("Second Deck", at.text_area[0].value)

    def test_removing_the_file_after_editing_does_not_discard_the_edits(self):
        at = self.extracted()
        edited = self.edit(at)
        at.file_uploader[0].clear()
        at.run()
        self.assertEqual(at.text_area[0].value, edited)


class TestStaleDownloadGuardStillWorks(PreserveWork):
    def generated(self):
        fx.make_profile(self.glyphs, "zed")
        at = self.generate(self.extracted(), "Guard test text", seed=3)
        self.assertEqual(len(at.get("download_button")), 1)
        return at

    def assert_stale(self, at):
        self.assertTrue(any("has changed since this PDF was made" in w.value for w in at.warning), texts(at.warning))
        self.assertEqual(len(at.get("download_button")), 0)

    def test_changing_the_text(self):
        at = self.generated()
        at.text_area[0].set_value("Different text").run()
        self.assert_stale(at)

    def test_changing_the_profile(self):
        at = self.generated()
        at.selectbox(key="profile_choice").set_value("zed").run()
        self.assert_stale(at)

    def test_changing_the_seed(self):
        at = self.generated()
        at.number_input(key="seed").set_value(4).run()
        self.assert_stale(at)

    def test_changes_made_before_leaving_are_still_detected_when_back(self):
        at = self.generated()
        at.number_input(key="seed").set_value(9).run()                                  # becomes stale, then we leave
        self.go(at, PROFILE)
        self.go(at, NOTES)
        self.assertEqual(at.number_input(key="seed").value, 9)
        self.assert_stale(at)
        button(at, "Generate handwritten PDF").click()
        at.run()
        self.assertEqual(len(at.get("download_button")), 1)                             # fresh again after regenerating


class TestProfileScreenKeepsItsInputs(PreserveWork):
    def open_profile(self):
        at = self.start()
        return self.go(at, PROFILE)

    def test_name_and_chosen_template_survive_visiting_the_notes_screen(self):
        at = self.open_profile()
        at.file_uploader(key="profile_upload").upload("remarkable export.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("kept_name").run()
        self.go(at, NOTES)
        self.go(at, PROFILE)
        self.assertEqual(at.text_input(key="profile_name").value, "kept_name")
        self.assertTrue(any("remarkable export.pdf" in i.value for i in at.info), texts(at.info))
        self.assertFalse(button(at, "Create profile").disabled)

    def test_a_profile_can_be_created_from_the_remembered_file(self):
        at = self.open_profile()
        at.file_uploader(key="profile_upload").upload("t.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("fresh2").run()
        self.go(at, NOTES)
        self.go(at, PROFILE)
        button(at, "Create profile").click()
        at.run()
        self.assertEqual(len(at.error), 0, texts(at.error))
        self.assertEqual(core.check_profile(self.glyphs / "fresh2"), [])

    def test_removing_the_template_forgets_it(self):
        at = self.open_profile()
        at.file_uploader(key="profile_upload").upload("t.pdf", fx.make_completed_template_bytes())
        at.text_input(key="profile_name").set_value("gone_name").run()
        at.file_uploader(key="profile_upload").clear()
        at.run()
        self.assertTrue(button(at, "Create profile").disabled)


class TestNothingElseIsTouched(PreserveWork):
    def test_existing_profiles_and_sources_are_unchanged_by_the_whole_workflow(self):
        fx.make_profile(self.glyphs, "zed")
        before = {p.name: fingerprint(p) for p in self.glyphs.iterdir()}
        deck = fx.make_pptx_bytes()
        at = self.extracted(deck)
        self.edit(at)
        self.go(at, PROFILE)
        self.go(at, NOTES)
        button(at, "Extract text").click()
        at.run()
        button(at, REPLACE).click()
        at.run()
        button(at, "Generate handwritten PDF").click()
        at.run()
        self.assertEqual({p.name: fingerprint(p) for p in self.glyphs.iterdir() if p.name in before}, before)
        self.assertEqual(sorted(p.name for p in self.glyphs.iterdir()), sorted(before))     # nothing was added either
        self.assertEqual(list((self.root / "samples").iterdir()), [])
        # the upload only ever lives in this session's own job folder, under a fixed name
        workspace = Path(at.session_state["workspace"])
        self.assertIn(core.workspace_root(), workspace.parents)
        self.assertEqual(sorted(p.name for p in workspace.glob("upload.*")), ["upload.pptx"])


@unittest.skipUnless(REAL_DECK is not None and (REAL_AMAYA / config.GLYPH_METADATA_NAME).exists(),
                     "needs the Chaucer deck in slides_in/ and glyphs/amaya/")
class TestRealWorkSurvivesNavigation(unittest.TestCase):
    def test_real_deck_and_real_profile_round_trip(self):
        def tree(folder):
            digest = hashlib.sha256()
            for path in sorted(Path(folder).rglob("*")):
                if path.is_file():
                    digest.update(path.name.encode() + path.read_bytes())
            return digest.hexdigest()

        before = (tree(REAL_AMAYA), hashlib.sha256(REAL_DECK.read_bytes()).hexdigest())
        with tempfile.TemporaryDirectory() as jobs, mock.patch.object(config, "APP_WORKSPACE_ROOT", Path(jobs)):
            at = AppTest.from_file(APP, default_timeout=180).run()
            at.file_uploader[0].upload(REAL_DECK.name, REAL_DECK.read_bytes())
            at.run()
            button(at, "Extract text").click()
            at.run()
            edited = at.session_state["extraction"]["notes_text"].replace("Geoffrey Chaucer", "Geoffrey Chaucer (my edit)", 1)
            at.text_area[0].set_value(edited).run()
            button(at, "Generate handwritten PDF").click()
            at.run()
            pdf = at.session_state["result"]["pdf"]
            at.radio(key="page").set_value(PROFILE).run()
            at.radio(key="page").set_value(NOTES).run()
            self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
            self.assertEqual(at.text_area[0].value, edited)
            self.assertEqual(at.session_state["result"]["pdf"], pdf)
            self.assertEqual(len(at.get("download_button")), 1)
            self.assertEqual(at.session_state["extraction"]["document"]["summary"]["slides"], 14)
        self.assertEqual((tree(REAL_AMAYA), hashlib.sha256(REAL_DECK.read_bytes()).hexdigest()), before)


# ---------------------------------------------------------------------------
# Real data: the actual Chaucer deck with the actual amaya profile
# ---------------------------------------------------------------------------

@unittest.skipUnless(REAL_DECK is not None and (REAL_AMAYA / config.GLYPH_METADATA_NAME).exists(),
                     "needs the Chaucer deck in slides_in/ and glyphs/amaya/")
class TestRealEndToEnd(unittest.TestCase):
    def test_real_deck_through_the_app_with_the_real_profile(self):
        def tree(folder):
            digest = hashlib.sha256()
            for path in sorted(Path(folder).rglob("*")):
                if path.is_file():
                    digest.update(path.name.encode() + path.read_bytes())
            return digest.hexdigest()

        profile_before, deck_before = tree(REAL_AMAYA), hashlib.sha256(REAL_DECK.read_bytes()).hexdigest()
        with tempfile.TemporaryDirectory() as jobs, mock.patch.object(config, "APP_WORKSPACE_ROOT", Path(jobs)):
            at = AppTest.from_file(APP, default_timeout=180).run()
            self.assertEqual(len(at.exception), 0, [e.value for e in at.exception])
            self.assertEqual(at.selectbox[0].value, "amaya")
            at.file_uploader[0].upload(REAL_DECK.name, REAL_DECK.read_bytes())
            at.run()
            button(at, "Extract text").click()
            at.run()
            self.assertEqual(len(at.error), 0, texts(at.error))
            extraction = at.session_state["extraction"]
            self.assertEqual(extraction["document"]["summary"], {"slides": 14, "slides_with_text": 14, "elements": 83})
            self.assertEqual(at.text_area[0].value, extraction["notes_text"])

            button(at, "Generate handwritten PDF").click()
            at.run()
            self.assertEqual(len(at.error), 0, texts(at.error))
            result = at.session_state["result"]
            self.assertEqual(result["page_count"], 21)
            self.assertEqual(len(at.get("download_button")), 1)
            self.assertEqual(len(at.image), 1)

            # the app's PDF is identical to running slides.py then render.py directly with the same seed
            pages, ctx, _, _ = render.render_text(extraction["notes_text"], REAL_AMAYA, config.APP_DEFAULT_SEED)
            direct = Path(jobs) / "direct.pdf"
            render.write_pdf(pages, ctx, direct)
            self.assertEqual(result["pdf"], direct.read_bytes())
            with pymupdf.open(stream=result["pdf"], filetype="pdf") as document:
                self.assertEqual(document.page_count, 21)
                self.assertTrue(all(abs(p.rect.width - 447) < 0.01 and abs(p.rect.height - 596) < 0.01 for p in document))
        self.assertEqual(tree(REAL_AMAYA), profile_before)                   # the real profile was only read
        self.assertEqual(hashlib.sha256(REAL_DECK.read_bytes()).hexdigest(), deck_before)


if __name__ == "__main__":
    unittest.main()
