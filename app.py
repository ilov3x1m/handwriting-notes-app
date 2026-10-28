"""Streamlit app: make handwritten notes from slides, and create handwriting profiles.

Start it with:   streamlit run app.py

This file only draws screens and reacts to clicks. The real work (reading slides, checking
profiles, rendering, building a profile) is done by app_core.py, which calls the existing
pipeline: slides.py, render.py, extract.py, spacing.py and template.py.
"""

import hashlib
from pathlib import Path

import streamlit as st

import app_core as core
import config

st.set_page_config(page_title="Handwritten Notes", page_icon=str(Path(__file__).with_name("icon.png")), layout="wide", initial_sidebar_state="collapsed")

# Theme colours come from .streamlit/config.toml. In dark mode the accent is a light blue, so primary
# buttons need dark text on it (Streamlit's default white text gives only ~2:1 contrast there).
st.markdown(
    "<style>@media (prefers-color-scheme: dark) {"
    " button[data-testid^='stBaseButton-primary'], button[data-testid^='stBaseButton-primary'] * "
    "{ color: #14171C !important; } }"
    # Streamlit fades captions to ~3.9:1 on the light paper colour; use the theme's secondary text colours instead.
    " [data-testid='stCaptionContainer'], [data-testid='stImageCaption'], [data-testid='stFileChip'] div div:last-child { color: #5B6675 !important; opacity: 1 !important; }"
    " @media (prefers-color-scheme: dark) { [data-testid='stCaptionContainer'], [data-testid='stImageCaption'], [data-testid='stFileChip'] div div:last-child "
    "{ color: #A3ABB8 !important; } }"
    # Pill-shaped buttons (round icon buttons); secondary buttons get a soft neutral grey fill.
    " button[data-testid^='stBaseButton-'] { border-radius: 999px; }"
    " button[data-testid='stBaseButton-secondary'] { background-color: #EEEDEA; }"
    " @media (prefers-color-scheme: dark) { button[data-testid='stBaseButton-secondary'] { background-color: #252A32; } }"
    # A fixed-width font makes bullet markers and indentation in the extracted text easy to review.
    " div[data-testid='stTextArea'] textarea { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }"
    "</style>",
    unsafe_allow_html=True)

PAGE_NOTES = "Make handwritten notes"
PAGE_PROFILE = "Create a handwriting profile"
CONFIRM_REPLACE = "Replace my edits and extract again"
CONFIRM_KEEP = "Keep my edits"

# Streamlit forgets the value of any widget that is not drawn during a run, for example the editor,
# the seed and the profile choice while the OTHER screen is showing. These widget values are copied
# into ordinary session keys ("kept_<key>") at the end of every run and put back when they are missing.
REMEMBERED_WIDGETS = ("notes_text", "seed", "profile_choice", "preview_page", "profile_name")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

@st.cache_data(show_spinner=False)
def cached_profiles(signature):
    """Profile list, re-checked only when a profile's files change (signature)."""
    return core.list_profiles()


@st.cache_data(show_spinner=False, max_entries=60)
def cached_preview(pdf_key, page_index, _pdf_bytes):
    return core.render_preview(_pdf_bytes, page_index)


def start_state():
    defaults = {"workspace": None, "extraction": None, "source_sha": None, "result": None,
                "busy": False, "created": None, "cleaned": False,
                "confirm_reextract": False, "profile_file": None,
                "notes_uploader_shown": False, "profile_uploader_shown": False,
                "notes_upload_had_file": False, "profile_upload_had_file": False}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    if not st.session_state["cleaned"]:
        core.cleanup_stale_workspaces()
        st.session_state["cleaned"] = True


def confirm_replace():
    st.session_state.update(confirm_reextract=False, replace_confirmed=True)


def keep_edits():
    st.session_state["confirm_reextract"] = False


def restore_widgets():
    """Put back widget values Streamlit dropped while the other screen was showing."""
    for key in REMEMBERED_WIDGETS:
        kept = st.session_state.get("kept_" + key)
        if key not in st.session_state and kept is not None:
            st.session_state[key] = kept


def remember_widgets():
    for key in REMEMBERED_WIDGETS:
        if key in st.session_state:
            st.session_state["kept_" + key] = st.session_state[key]


def plain(text):
    """Text without line-ending or trailing-space differences, for deciding whether it was edited."""
    return (text or "").replace("\r\n", "\n").rstrip()


def has_edits():
    """Has the person changed the extracted text? (Re-extracting would throw their changes away.)"""
    extraction = st.session_state["extraction"]
    if not extraction:
        return False
    current = st.session_state.get("notes_text", extraction["notes_text"])
    return plain(current) != plain(extraction["notes_text"])


def show_error(error, headline="Something went wrong"):
    """An error the person can act on, with technical details tucked away."""
    where = f" ({error.step})" if getattr(error, "step", None) else ""
    st.error(f"**{headline}{where}.** {getattr(error, 'message', str(error))}")
    details = getattr(error, "details", None)
    if details:
        with st.expander("Technical details (for troubleshooting)"):
            st.code(details)


def reset_job():
    """Forget the current document, its text and its result, and delete its temporary folder."""
    core.cleanup_workspace(st.session_state["workspace"])
    st.session_state.update(workspace=None, extraction=None, source_sha=None, result=None, confirm_reextract=False)
    for key in ("notes_text", "preview_page"):
        st.session_state.pop(key, None)
        st.session_state.pop("kept_" + key, None)


def replace_job(workspace, extraction, sha):
    """Swap in a freshly extracted document. The old folder is only deleted now that the new one worked."""
    old_workspace = st.session_state["workspace"]
    st.session_state.update(workspace=workspace, extraction=extraction, source_sha=sha, result=None,
                            confirm_reextract=False)
    st.session_state["notes_text"] = extraction["notes_text"]
    st.session_state["kept_notes_text"] = extraction["notes_text"]
    for key in ("preview_page", "kept_preview_page"):
        st.session_state.pop(key, None)
    core.cleanup_workspace(old_workspace)


def show_stage(slot, step, total, label):
    """One-line progress bar saying where the person is in the current workflow."""
    slot.progress(step / total, text=f"Step {step} of {total}: {label}")


def step_progress(container):
    """Progress callback that writes 'Step n of 6' lines into a status box."""
    def show(number, label):
        container.write(f"Step {number} of {len(core.PROFILE_STEPS)}: {label}...")
    return show


# ---------------------------------------------------------------------------
# Screen 1: make handwritten notes
# ---------------------------------------------------------------------------

def choose_profile():
    st.subheader("1. Choose your handwriting")
    profiles = cached_profiles(core.profile_signature())
    ready = [p.name for p in profiles if p.ready]
    unusable = [p for p in profiles if not p.ready]
    if unusable:
        with st.expander(f"{len(unusable)} profile(s) cannot be used"):
            for profile in unusable:
                st.write(f"**{profile.name}**: {profile.problems[0]}")
    if not ready:
        st.warning("There is no handwriting profile to use yet. Open **Create a handwriting profile** in the "
                   "left menu, fill in the template on your reMarkable, and upload it.")
        return None
    if st.session_state.get("profile_choice") not in ready:      # first visit, or the remembered profile is gone
        st.session_state["profile_choice"] = "amaya" if "amaya" in ready else ready[0]
    name = st.selectbox("Handwriting profile", ready, key="profile_choice",
                        help="The handwriting that will be used to write your notes.")
    st.caption(f"Your notes will be written in the **{name}** handwriting.")
    return name


def stored_upload():
    """The copy of the current document in this session's own job folder (fixed name), if it is still there."""
    extraction, workspace = st.session_state["extraction"], st.session_state["workspace"]
    if extraction and workspace:
        path = Path(workspace) / f"upload.{extraction['kind']}"
        if path.is_file():
            return path
    return None


def upload_and_extract():
    st.subheader("2. Upload your slides")
    upload = st.file_uploader("PowerPoint (.pptx) or PDF (.pdf)", type=["pptx", "pdf"], key="upload",
                              max_upload_size=config.APP_MAX_UPLOAD_MB)
    st.caption("PDF files must contain real, selectable text. Scanned pages (pictures of text) cannot be read.")
    state = st.session_state
    data = upload.getvalue() if upload is not None else None
    sha = hashlib.sha256(data).hexdigest() if data else None
    # Streamlit empties the uploader by itself while the other screen is showing, so "empty" only means the person
    # removed the file if the uploader was on screen last run AND held a file then.
    removed = upload is None and state["notes_uploader_was_shown"] and state["notes_upload_had_file"]
    state["notes_uploader_shown"], state["notes_upload_had_file"] = True, upload is not None
    stored = stored_upload()

    notice = None
    if state["extraction"] or state["result"]:
        if upload is None:
            if not removed or has_edits():
                notice = "keep"                              # keep the job; edits are never discarded silently
            else:
                reset_job()                                  # nothing edited and the file was removed: start fresh
                stored = None
        elif state["source_sha"] != sha:
            if has_edits():
                notice = "different"                         # keep the edits until the person confirms replacing them
            else:
                reset_job()                                  # a different file and nothing to lose
                stored = None
    if notice and state["extraction"]:
        name = state["extraction"]["display_name"]
        if notice == "keep":
            st.info(f"Your file **{name}** is still loaded, with your text. Choose a different file only if you "
                    "want to replace it.")
        else:
            st.info("You chose a different file. The text below is still from the previous file and your edits are "
                    "kept. Press **Extract text** to replace it with the new file; you will be asked to confirm.")

    can_extract = upload is not None or stored is not None   # the copy in this session's job folder needs no re-upload
    clicked = st.button("Extract text", type="primary", disabled=not can_extract or state["busy"])
    confirmed = state.pop("replace_confirmed", False)
    if clicked and state["extraction"] and has_edits():
        state["confirm_reextract"] = True                    # ask before throwing the edits away
        clicked = False
    if state["confirm_reextract"]:
        if not can_extract or not has_edits():
            state["confirm_reextract"] = False               # nothing left to protect
        else:
            st.warning("**You have edited the text.** Extracting again will replace your edits with a fresh copy "
                       "from the file.")
            left, right = st.columns(2)
            left.button(CONFIRM_REPLACE, type="primary", on_click=confirm_replace)
            right.button(CONFIRM_KEEP, on_click=keep_edits)

    if clicked or confirmed:
        if upload is not None:
            name, content, content_sha = upload.name, data, sha
        else:
            name, content, content_sha = state["extraction"]["display_name"], stored.read_bytes(), state["source_sha"]
        state["busy"] = True
        failure = None
        try:
            with st.status("Reading your file...", expanded=True) as status:
                st.write("Checking the file...")
                workspace = core.new_workspace()             # a new folder; the old job stays untouched until this works
                st.write("Extracting text...")
                try:
                    extraction = core.extract_notes(workspace, name, content)
                except Exception as error:                   # expected problems and surprises are both shown kindly
                    core.cleanup_workspace(workspace)
                    failure = core.as_app_error(error)
                    status.update(label="Could not read the file", state="error", expanded=True)
                else:
                    replace_job(workspace, extraction, content_sha)
                    status.update(label="Finished reading the file", state="complete", expanded=False)
            if failure:                                      # shown OUTSIDE the progress box, where it cannot be collapsed away
                show_error(failure, "The file could not be read")
                if state["extraction"]:
                    st.info("Your previous text and results were kept.")
        finally:
            state["busy"] = False
    return state["extraction"]


def review_text(extraction, profile):
    st.subheader("3. Check and edit the text")
    summary = extraction["document"]["summary"]
    unit = "slides" if extraction["kind"] == "pptx" else "pages"
    st.write(f"Found **{summary['elements']}** pieces of text on **{summary['slides_with_text']}** of "
             f"**{summary['slides']}** {unit} in *{extraction['display_name']}*.")
    if extraction["kind"] == "pdf":
        st.info("This is a PDF, so headings, bullet points and indentation were **guessed** from how the text is "
                "laid out. Please check them below. (PowerPoint files carry this structure directly.)")
    summary_of = core.summarize_warnings(extraction["warnings"], unit)
    if summary_of["attention"]:
        where = f" Check {summary_of['attention_where']}." if summary_of["attention_where"] else ""
        st.warning("**Some content may be missing or uncertain.** Text inside pictures, charts, SmartArt or scanned "
                   "images is not included below, and some pages may have no readable text. "
                   f"Add anything you need yourself.{where}")
        with st.expander("Details for each problem"):
            for item in summary_of["attention"]:
                st.write(f"- {item['message']}" + (f" ({item['where']})" if item["where"] else ""))
    if summary_of["notes"]:
        with st.expander(f"{len(summary_of['notes'])} extraction note(s)"):
            for item in summary_of["notes"]:
                st.write(f"- {item['message']}" + (f" ({item['where']})" if item["where"] else ""))
    st.caption("This is exactly what will be written. A line starting with * is a bullet; spaces at the start "
               "indent a line; a blank line leaves a gap. You can correct, add or delete anything.")
    text = st.text_area("Text to write by hand", key="notes_text", height=520)
    verdict = core.check_text(text, profile)
    for message in verdict["errors"]:
        st.error(message)
    for message in verdict["warnings"]:
        st.warning(message)
    return text, verdict


def generate(extraction, profile, text, verdict):
    st.subheader("4. Make the handwritten PDF")
    st.session_state.setdefault("seed", config.APP_DEFAULT_SEED)
    with st.expander("Advanced options"):
        seed = st.number_input("Handwriting variation (seed)", min_value=0, max_value=999999, step=1, key="seed",
                               help="The same text with the same number always gives the same pages. "
                                    "Change it to get a slightly different look.")
    disabled = not verdict["ok"] or st.session_state["busy"]
    if not verdict["ok"]:
        st.caption("Add some text above to turn this button on." if not text.strip() else
                   "Fix the problem shown above to turn this button on.")
    if st.button("Generate handwritten PDF", type="primary", disabled=disabled):
        current = st.session_state["result"]
        fingerprint = core.text_fingerprint(text)
        if current and (current["profile"], current["seed"], current["text_hash"]) == (profile, seed, fingerprint):
            st.info("These pages are already up to date. See the preview below.")
            return
        st.session_state["busy"] = True
        failure = None
        try:
            with st.status("Making your handwritten notes...", expanded=True) as status:
                st.write("Checking the handwriting profile...")
                st.write("Rendering handwritten pages...")
                try:
                    result = core.generate_pdf(st.session_state["workspace"], profile, text, seed)
                except Exception as error:
                    failure = core.as_app_error(error)
                    status.update(label="The PDF could not be made", state="error", expanded=True)
                else:
                    st.write("Preparing the preview...")
                    st.session_state["result"] = result
                    st.session_state["preview_page"] = 1
                    status.update(label="Finished making the pages", state="complete", expanded=False)
            if failure:
                show_error(failure, "The PDF could not be made")
        finally:
            st.session_state["busy"] = False


def previous_page():
    st.session_state["preview_page"] = max(1, st.session_state["preview_page"] - 1)


def next_page(count):
    st.session_state["preview_page"] = min(count, st.session_state["preview_page"] + 1)


def preview_and_download(extraction, profile, text):
    result = st.session_state["result"]
    if not result:
        return
    st.subheader("5. Preview and download")
    current = (profile, st.session_state.get("seed"), core.text_fingerprint(text))
    up_to_date = (result["profile"], result["seed"], result["text_hash"]) == current
    count = result["page_count"]
    if up_to_date:
        st.success(f"Your notes are ready: **{count}** page{'s' if count != 1 else ''} "
                   f"(reMarkable 2 size, {config.PAGE_WIDTH:g} x {config.PAGE_HEIGHT:g} pt).")
    else:
        st.warning("The text, handwriting or seed has changed since this PDF was made. "
                   "Press **Generate handwritten PDF** again to update it. The download is hidden until then.")
    for message in result["warnings"]:
        st.warning(message)

    st.session_state["preview_page"] = min(max(1, st.session_state.get("preview_page", 1)), count)
    page = st.session_state["preview_page"]
    if up_to_date:
        st.download_button("Download handwritten PDF", data=result["pdf"], file_name=core.download_name(extraction["display_name"]),
                           mime="application/pdf", type="primary", width="stretch")
    key = f"{result['text_hash']}-{result['seed']}-{result['profile']}"
    st.image(cached_preview(key, page - 1, result["pdf"]), caption=f"Page {page} of {count}", width="stretch",
             alt=f"Preview of page {page} of {count} of your handwritten notes")
    left, middle, right = st.columns([1, 1, 1])
    left.button("Previous page", on_click=previous_page, disabled=count == 1, width="stretch")
    middle.number_input("Page", min_value=1, max_value=count, step=1, key="preview_page")
    right.button("Next page", on_click=next_page, args=(count,), disabled=count == 1, width="stretch")


def notes_body():
    """Draws the notes screen. Returns (step, label) for the progress line."""
    profile = choose_profile()
    if profile is None:
        return 1, "Choose your handwriting"
    extraction = upload_and_extract()
    if extraction is None:
        return 2, "Upload your slides"
    editor, output = st.columns(2, gap="large")        # side by side on wide screens, stacked on narrow ones
    with editor:
        text, verdict = review_text(extraction, profile)
    with output:
        generate(extraction, profile, text, verdict)
        preview_and_download(extraction, profile, text)
    result = st.session_state["result"]
    if result and (result["profile"], result["seed"], result["text_hash"]) == (
            profile, st.session_state.get("seed"), core.text_fingerprint(text)):
        return 4, "Download your notes"
    return 3, "Check the text and make the PDF"


def page_notes():
    st.caption("Turn a PowerPoint or PDF into notes written in your own handwriting.")
    slot = st.empty()                                  # filled last, so it reflects what this run just did
    step, label = notes_body()
    show_stage(slot, step, 4, label)


# ---------------------------------------------------------------------------
# Screen 2: create a handwriting profile
# ---------------------------------------------------------------------------

def use_profile_now(name):
    """Button callback: go to the notes screen with the new profile chosen."""
    st.session_state["page"] = PAGE_NOTES
    st.session_state["profile_choice"] = st.session_state["kept_profile_choice"] = name


def page_profile():
    st.caption("A profile is a copy of your handwriting. You fill in a template on your reMarkable 2, and the app "
               "turns it into a profile you can choose when making notes.")
    slot = st.empty()
    profiles = cached_profiles(core.profile_signature())
    if profiles:
        st.write("**Existing profiles:** " + ", ".join(f"{p.name} ({'ready' if p.ready else 'not usable'})" for p in profiles))
        st.caption("Existing profiles are never changed. A new profile needs a new name.")

    st.subheader("1. Download the template")
    st.download_button("Download blank template (PDF)", data=core.template_pdf_bytes(),
                       file_name="handwriting_template.pdf", mime="application/pdf")
    st.subheader("2. Open it on your reMarkable 2")
    st.write("Copy the PDF to your reMarkable (with the reMarkable app or USB) and open it. The template has "
             "**4 pages** of boxes. Do not add, remove or resize pages.")
    st.subheader("3. Complete the handwriting samples")
    st.markdown(
        "- Pages 1 and 2: write each letter **three times**, one copy per box, resting on the light line.\n"
        "- Page 3: write the digits, then write the sample sentence **in your normal handwriting**.\n"
        "- Page 4: write each punctuation mark and symbol three times, where it normally sits "
        "(commas hang below the line, quotes sit high).\n"
        "- Fill **every** box.")
    st.subheader("4. Export the completed PDF")
    st.write("On the reMarkable, export the finished document as a **PDF** (keep all 4 pages and the original size) "
             "and save it to this computer.")
    st.subheader("5. Upload the PDF")
    upload = st.file_uploader("Completed template (PDF)", type=["pdf"], key="profile_upload",
                              max_upload_size=config.APP_MAX_UPLOAD_MB)
    state = st.session_state
    removed = upload is None and state["profile_uploader_was_shown"] and state["profile_upload_had_file"]
    state["profile_uploader_shown"], state["profile_upload_had_file"] = True, upload is not None
    if upload is not None:
        state["profile_file"] = {"name": core.safe_display_name(upload.name), "data": upload.getvalue()}
    elif removed:
        state["profile_file"] = None                         # the person removed it themselves
    chosen = state["profile_file"]
    if chosen and upload is None:
        st.info(f"Using the file you chose earlier: **{chosen['name']}**. Choose another file to replace it.")

    st.subheader("6. Name and create the profile")
    name = st.text_input("Profile name", key="profile_name", placeholder="for example: sam",
                         help="Letters, digits, '-' and '_'. Cannot match an existing profile (capital letters do not matter).")
    name_ok = False
    if name.strip():
        try:
            core.validate_new_profile_name(name)
            st.caption("This name is available.")
            name_ok = True
        except core.AppError as error:
            st.error(error.message)

    if st.button("Create profile", type="primary", disabled=not (name_ok and chosen is not None) or st.session_state["busy"]):
        st.session_state["busy"] = True
        workspace = core.new_workspace()
        failure = None
        try:
            with st.status("Creating your profile...", expanded=True) as status:
                try:
                    created = core.create_profile(name, chosen["data"], workspace, progress=step_progress(st))
                except Exception as error:
                    st.session_state["created"] = None
                    failure = core.as_app_error(error)
                    status.update(label="The profile was not created", state="error", expanded=True)
                else:
                    st.session_state["created"] = created
                    st.session_state["profile_file"] = None      # used up: a new profile needs a new export
                    status.update(label="Finished checking and saving", state="complete", expanded=False)
            if failure:
                show_error(failure, "The profile could not be created")
                st.info("Nothing was saved. Fix the problem above and try again.")
        finally:
            core.cleanup_workspace(workspace)
            st.session_state["busy"] = False

    created = st.session_state["created"]
    if created:
        ready = not core.check_profile(core.profile_directory(created["name"]))
        if ready:
            st.success(f"The profile **{created['name']}** is ready.")
            st.button("Use this profile now", type="primary", on_click=use_profile_now, args=(created["name"],))
        st.write(f"**{created['samples']} of {created['expected']}** handwriting samples were found.")
        for message in created["warnings"]:
            st.warning(message)
        st.image(core.glyph_preview_png(created["name"]), width="stretch",
                 caption="One sample of every character, labelled underneath. Check that they look right.",
                 alt=f"Overview of the handwriting profile {created['name']}: one labelled sample of every character")
        style = created["style"]
        with st.expander("Details (spacing measurements)"):
            columns = st.columns(2)
            columns[0].metric("Word gap", f"{style['word_spacing'].get('mean_pt')} pt")
            columns[1].metric("Letter gap", f"{style['letter_spacing'].get('estimate_pt')} pt")
    if created:
        show_stage(slot, 6, 6, "Profile created")
    elif chosen is not None:
        show_stage(slot, 5, 6, "Name the profile and create it")
    else:
        show_stage(slot, 1, 6, "Download the template, fill it in and upload the PDF")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start_state()
    restore_widgets()
    # Remember whether each uploader was on screen LAST run, then mark it as not shown for this run.
    st.session_state["notes_uploader_was_shown"] = st.session_state["notes_uploader_shown"]
    st.session_state["profile_uploader_was_shown"] = st.session_state["profile_uploader_shown"]
    st.session_state["notes_uploader_shown"] = st.session_state["profile_uploader_shown"] = False
    st.markdown("**Handwritten Notes**")
    page = st.radio("What would you like to do?", [PAGE_NOTES, PAGE_PROFILE], key="page", horizontal=True,
                    label_visibility="collapsed")
    if page == PAGE_NOTES:
        page_notes()
    else:
        page_profile()
    remember_widgets()


main()
