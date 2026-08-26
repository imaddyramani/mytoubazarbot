import os
import logging
import asyncio
from pathlib import Path
from datetime import datetime
import time
import shutil
import copy
import re
import types
from urllib.parse import quote

from dotenv import load_dotenv
from pypdf import PdfReader, PdfWriter
from telegram import (
    Update, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.error import BadRequest
from telegram.ext import (
    Application, ApplicationHandlerStop, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, ContextTypes, filters
)

from extractor import extract_itinerary_from_parts, extract_transit_from_parts
from template import generate_pdf
from hotel_voucher import extract_hotel_voucher, generate_hotel_voucher
from flight_extractor import extract_flight_ticket
from flight_print import generate_flight_ticket
from watermark_overlay import add_watermark_to_pdf
from bus_ticket import extract_bus_ticket, generate_bus_ticket
from editor import apply_edit
from smart_assistant import classify as ai_classify, chat as ai_chat, enhance_package_itinerary, agent_plan, generate_package_from_brief
from reference_manager import create_reference, save_record, load_record, list_records, update_record, import_existing_pdfs
from footer_overlay import add_footer_to_pdf
from footer_bar_overlay import add_contact_bar_to_pdf
from footer2_overlay import add_footer2_to_pdf
from print_settings import load_settings, save_settings, set_font, adjust_text_scale, adjust_logo_scale, get_logo_scale, toggle_button, reset_settings, FONT_OPTIONS, button_enabled, set_default_terms, set_default_footer, get_default_footer, set_tour_last_page, get_tour_last_page
from ai_retry import set_retry_notifier, reset_retry_notifier
from performance_utils import prepare_supplier_for_ai, parse_transit_files_local, apply_missing_accommodation_locally
from voice_edit import transcribe_voice_note

# V55: LOCAL FOOTER SOURCE
# The footer can ONLY come from this bot's own assets folder.
BOT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BOT_DIR, "assets")
FOOTER_IMAGE = os.path.join(ASSETS_DIR, "mytourbazar_footer.png")

def get_local_footer_path():
    path = os.path.normpath(FOOTER_IMAGE)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            "Footer artwork not found. Expected exactly: " + path
        )
    return path

# --- MTB AIRLINE LOGO INLINE ENHANCEMENT ---
from pathlib import Path as _MTBPath
import re as _MTBre

_MTB_AIRLINE_LOGO_DIR = _MTBPath(__file__).resolve().parent / "assets" / "airline_logos"

def _mtb_airline_logo_candidates(airline_text):
    s = (airline_text or "").strip().lower()
    s = _MTBre.sub(r"[^a-z0-9]+", "_", s).strip("_")
    aliases = {
        "6e": ["indigo", "6e", "indigo_air"],
        "indigo": ["indigo", "6e", "indigo_air"],
        "ai": ["air_india", "airindia", "ai"],
        "air_india": ["air_india", "airindia", "ai"],
        "ix": ["air_india_express", "airindiaexpress", "ix"],
        "air_india_express": ["air_india_express", "airindiaexpress", "ix"],
        "sg": ["spicejet", "sg"],
        "spicejet": ["spicejet", "sg"],
        "qp": ["akasa_air", "akasa", "qp"],
        "akasa": ["akasa_air", "akasa", "qp"],
        "uk": ["vistara", "uk"],
        "vistara": ["vistara", "uk"],
        "ek": ["emirates", "ek"],
        "emirates": ["emirates", "ek"],
        "qr": ["qatar_airways", "qatar", "qr"],
        "qatar": ["qatar_airways", "qatar", "qr"],
        "sq": ["singapore_airlines", "singapore", "sq"],
        "ai_exp": ["air_india_express", "airindiaexpress", "ix"],
    }
    vals = aliases.get(s, []) + [s]
    out = []
    for v in vals:
        for ext in (".png", ".webp", ".jpg", ".jpeg"):
            out.append(v + ext)
    return list(dict.fromkeys(out))

def mtb_find_airline_logo(airline_text):
    """Find the best local airline logo by airline name or flight code."""
    if not _MTB_AIRLINE_LOGO_DIR.exists():
        return None
    candidates = _mtb_airline_logo_candidates(airline_text)
    files = {p.name.lower(): p for p in _MTB_AIRLINE_LOGO_DIR.iterdir() if p.is_file()}
    for name in candidates:
        if name.lower() in files:
            return str(files[name.lower()])
    # Flexible fallback: compare normalized names.
    normalized = _MTBre.sub(r"[^a-z0-9]", "", (airline_text or "").lower())
    if normalized:
        for p in files.values():
            stem = _MTBre.sub(r"[^a-z0-9]", "", p.stem.lower())
            if stem and (stem in normalized or normalized in stem):
                return str(p)
    return None

def mtb_airline_logo_html(airline_text, alt=None):
    """Return a larger, vertically centered inline logo for EVERY flight row."""
    path = mtb_find_airline_logo(airline_text)
    if not path:
        return ""
    import base64 as _MTBbase64
    ext = _MTBPath(path).suffix.lower()
    mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
    data = _MTBbase64.b64encode(_MTBPath(path).read_bytes()).decode("ascii")
    label = alt or airline_text or "Airline"
    # Larger than the previous version and centered in the flight-detail cell.
    return (
        f'<div class="mtb-airline-logo-wrap">'
        f'<img class="mtb-airline-logo" src="data:{mime};base64,{data}" '
        f'alt="{label}" title="{label}">'
        f'</div>'
    )
# --- END MTB AIRLINE LOGO INLINE ENHANCEMENT ---


load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite").strip()

ADMIN_USER_IDS = {
    int(x.strip()) for x in os.getenv("ADMIN_USER_IDS", "").split(",")
    if x.strip().isdigit()
}

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
GENERATED_DIR = DATA_DIR / "generated"
TEMP_DIR = DATA_DIR / "incoming"
DATA_DIR.mkdir(exist_ok=True)
GENERATED_DIR.mkdir(exist_ok=True)
TEMP_DIR.mkdir(exist_ok=True)
# Default MyTourBazar logo supplied with the bot.
# A logo uploaded later through 🖼️ Set Logo overrides this for the current run.
LOGO_PATH = DATA_DIR / "logo_default.png"
USER_LOGO_PATH = DATA_DIR / "logo.png"
ASSETS_DIR = BASE_DIR / "assets"
ASSETS_DIR.mkdir(exist_ok=True)
TERMS2_PDF_PATH = DATA_DIR / "TERMS_CONDITIONS.pdf"
B2B_TERMS_PDF_PATH = DATA_DIR / "B2B.pdf"
TOUR_WITHOUT_FOOTER_PDF_PATH = DATA_DIR / "without_footer.pdf"
TOUR_NON_GOOGLE_TERMS_PDF_PATH = DATA_DIR / "T&C NON GOOGLE.pdf"
import_existing_pdfs(GENERATED_DIR)


def append_pdf_pages(base_pdf, appendix_pdf, output_pdf):
    """Merge the generated package itinerary followed by the supplied appendix PDF."""
    writer = PdfWriter()
    for source in (base_pdf, appendix_pdf):
        reader = PdfReader(str(source))
        for page in reader.pages:
            writer.add_page(page)
    with open(output_pdf, "wb") as fh:
        writer.write(fh)


_B2B_BRAND_PATTERN = re.compile(
    r"(?i)(?:sales@mytourbazar\.com|www\.mytourbazar\.com|mytourbazar\.com|@mytourbazar|my\s*tour\s*bazar|mytourbazar)"
)


def _smart_requested_b2b(text):
    """True only when the owner explicitly asks for a B2B / white-label Tour output."""
    low = str(text or "").lower()
    return bool(re.search(
        r"\b(?:b\s*2\s*b|business\s*[- ]?to\s*[- ]?business|white\s*[- ]?label|agency\s*[- ]?neutral|unbranded)\b",
        low,
        re.I,
    ))


def _is_b2b_tour(data=None, context=None, record=None):
    data = data or {}
    record = record or {}
    if bool(data.get("b2b") or data.get("brand_neutral")):
        return True
    if bool(record.get("b2b")):
        return True
    if context is not None and bool(context.user_data.get("pending_b2b")):
        return True
    return False


def _b2b_replace_text(value):
    """Remove every MyTourBazar brand reference from B2B-visible text."""
    text = str(value or "")
    # Contact-style brand strings must not become malformed e-mails/domains.
    text = re.sub(r"(?i)\bsales@mytourbazar\.com\b", "our company", text)
    text = re.sub(r"(?i)\b(?:www\.)?mytourbazar\.com\b", "our company", text)
    text = re.sub(r"(?i)@mytourbazar\b", "our company", text)
    text = re.sub(r"(?i)\bmy\s*tour\s*bazar\b", "our company", text)
    text = re.sub(r"(?i)\bmytourbazar\b", "our company", text)
    return text


def _b2b_neutralize_data(data, mode=None):
    """Deep-copy Tour data and make every customer-visible string B2B white-label."""
    def scrub(obj):
        if isinstance(obj, dict):
            return {k: scrub(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        if isinstance(obj, tuple):
            return tuple(scrub(v) for v in obj)
        if isinstance(obj, str):
            return _b2b_replace_text(obj)
        return obj

    d = scrub(copy.deepcopy(data or {}))
    d["b2b"] = True
    d["brand_neutral"] = True
    d["agency_removed"] = True
    if mode:
        d["document_mode"] = str(mode).lower()
    return d


def _b2b_greeting(data, mode):
    guest = str((data or {}).get("client_name") or "Guest").strip()
    mode = str(mode or "itinerary").lower()
    if mode == "quotation":
        return (
            f"Dear {guest},\n\nGreetings from our company! We are delighted to present this official "
            "tour quotation prepared especially for your travel requirements. The following proposal "
            "summarizes the planned destinations, accommodation, transportation, sightseeing experiences, "
            "inclusions and exclusions for your consideration. We look forward to arranging a comfortable "
            "and memorable journey for you and your family.\n\nPlease review the itinerary and package "
            "details carefully, and feel free to contact our company for any clarification or amendment before confirmation."
        )
    if mode == "voucher":
        return (
            f"Dear {guest},\n\nGreetings from our company! Thank you for choosing our company for your journey. "
            "Please find below your official tour voucher containing the confirmed travel plan, accommodation "
            "schedule, services and day-wise arrangements. Kindly keep this voucher available during your "
            "journey and review the included services and travel instructions before departure.\n\n"
            "Our company wishes you a smooth, comfortable and memorable trip."
        )
    return (
        f"Dear {guest},\n\nGreetings from our company! Please find below your carefully planned day-wise "
        "travel itinerary, including accommodation, transportation, sightseeing experiences, inclusions and "
        "exclusions for a smooth and comfortable journey."
    )


def _sanitize_b2b_terms_pdf(source_pdf, output_pdf):
    """Create a temporary B2B terms PDF with no MyTourBazar text.

    This is fail-closed: if a visible/extractable MyTourBazar reference remains,
    the B2B print is stopped instead of leaking the brand into a white-label PDF.
    """
    source_pdf = Path(source_pdf)
    output_pdf = Path(output_pdf)
    try:
        import fitz  # PyMuPDF is already used by the MyTourBazar bot stack.
    except Exception as exc:
        raise RuntimeError(
            "B2B terms sanitizing needs PyMuPDF. Refusing to append an unsanitized B2B terms page."
        ) from exc

    doc = fitz.open(str(source_pdf))
    # Long/contact forms first, then brand-name variants.
    replacements = [
        ("sales@mytourbazar.com", "our company"),
        ("www.mytourbazar.com", "our company"),
        ("mytourbazar.com", "our company"),
        ("@mytourbazar", "our company"),
        ("MY TOUR BAZAR", "our company"),
        ("My Tour Bazar", "our company"),
        ("MYTOURBAZAR", "our company"),
        ("MyTourBazar", "our company"),
        ("mytourbazar", "our company"),
    ]

    for page in doc:
        found = []
        occupied = []
        for needle, replacement in replacements:
            for rect in page.search_for(needle):
                # Avoid overlapping replacement rectangles when one long token
                # also contains a shorter brand token.
                if any(rect.intersects(prev) for prev in occupied):
                    continue
                occupied.append(rect)
                found.append((rect, replacement))
                page.add_redact_annot(rect, fill=(1, 1, 1))
        if found:
            page.apply_redactions()
            for rect, replacement in found:
                fontsize = max(6.0, min(11.0, rect.height * 0.72))
                page.insert_textbox(
                    rect,
                    replacement,
                    fontsize=fontsize,
                    fontname="helv",
                    color=(0, 0, 0),
                    align=0,
                )

    doc.save(str(output_pdf), garbage=4, deflate=True)
    doc.close()

    verify = fitz.open(str(output_pdf))
    extracted = "\n".join(page.get_text("text") for page in verify)
    verify.close()
    if _B2B_BRAND_PATTERN.search(extracted):
        output_pdf.unlink(missing_ok=True)
        raise RuntimeError(
            "B2B terms still contain a MyTourBazar reference after sanitizing. "
            "The PDF was stopped to protect white-label branding."
        )
    return output_pdf


def terms_pdf_path(choice=None):
    choice = choice or get_tour_last_page()
    return {
        'without_footer': TOUR_WITHOUT_FOOTER_PDF_PATH,
        'tc_non_google': TOUR_NON_GOOGLE_TERMS_PDF_PATH,
    }.get(choice, TOUR_NON_GOOGLE_TERMS_PDF_PATH)

def terms_label(choice=None):
    return {
        'without_footer': 'Without Footer',
        'tc_non_google': 'T&C NON GOOGLE',
        'b2b': 'B2B',
    }.get(choice or get_tour_last_page(), 'T&C NON GOOGLE')

def tour_terms_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton('📜 Use T&C NON GOOGLE', callback_data='tour_terms:non_google')]])

def append_selected_terms(base_pdf, terms_choice=None, output_pdf=None):
    if output_pdf is None:
        output_pdf = terms_choice
    choice = terms_choice or get_tour_last_page()
    temporary_b2b_terms = None
    if choice == 'b2b':
        terms_path = B2B_TERMS_PDF_PATH
    else:
        terms_path = terms_pdf_path(choice)
    if not terms_path.exists():
        raise FileNotFoundError(f'Tour last-page file not found: {terms_path}')
    try:
        if choice == 'b2b':
            temporary_b2b_terms = GENERATED_DIR / f"_b2b_terms_clean_{int(time.time()*1000)}.pdf"
            terms_path = _sanitize_b2b_terms_pdf(terms_path, temporary_b2b_terms)
        append_pdf_pages(base_pdf, terms_path, output_pdf)
        return Path(output_pdf)
    finally:
        if temporary_b2b_terms is not None:
            temporary_b2b_terms.unlink(missing_ok=True)


def _apply_footer_mode(input_pdf, output_pdf, mode):
    if mode == 'bar':
        add_contact_bar_to_pdf(input_pdf, output_pdf)
    elif mode == 'design':
        add_footer_to_pdf(input_pdf, output_pdf)
    elif mode == 'footer2':
        add_footer2_to_pdf(input_pdf, output_pdf)
    else:
        shutil.copyfile(input_pdf, output_pdf)


WAITING_GUEST_NAME = 1
WAITING_SOURCE = 2
WAITING_EXTRA_INCLUSION = 3
WAITING_EXTRA_EXCLUSION = 4
WAITING_FLIGHT_IMAGE = 5
HOTEL_VOUCHER_INPUT = 10
FLIGHT_TICKET_INPUT = 20
FLIGHT_FARE_INPUT = 21
BUS_TICKET_INPUT = 30
BUS_FARE_INPUT = 31
EDIT_REF_INPUT = 40
EDIT_INSTRUCTION = 41
SMART_INPUT = 50
AUTO_PRINT_SECONDS = 5

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("mytourbazar_bot")


class ReplyActionFilter(filters.MessageFilter):
    """Route replies to bot messages through the edit/command router first.

    If the replied-to message contains an MTB reference it becomes a document edit.
    If it is a fare prompt it becomes fare input. Otherwise the normal ConversationHandler
    still receives the message, so replying to workflow prompts remains fully usable.
    """
    name = "ReplyActionFilter"

    def filter(self, message):
        replied = getattr(message, "reply_to_message", None)
        if not replied:
            return False
        sender = getattr(replied, "from_user", None)
        return bool(sender and getattr(sender, "is_bot", False))


REPLY_ACTION_FILTER = ReplyActionFilter()


async def safe_callback_edit(query, text, **kwargs):
    """Safely update a callback's originating message.

    Callback buttons can be attached to photo/document/media messages.
    Telegram's editMessageText only edits text messages, so fall back to a
    fresh reply when the originating message has no text/caption.
    """
    message = getattr(query, "message", None)
    if message is None:
        return None
    text = str(text or "Processing...")
    existing_text = getattr(message, "text", None)
    existing_caption = getattr(message, "caption", None)
    if not existing_text and not existing_caption:
        return await message.reply_text(text, **kwargs)
    try:
        return await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        reason = str(exc).lower()
        if any(x in reason for x in (
            "there is no text in the message to edit",
            "message can't be edited",
            "message to edit not found",
            "message is not modified",
        )):
            return await message.reply_text(text, **kwargs)
        logger.warning("Callback message edit failed: %s", exc)
        return await message.reply_text(text, **kwargs)
    except Exception as exc:
        logger.warning("Unexpected callback message edit failure: %s", exc)
        try:
            return await message.reply_text(text, **kwargs)
        except Exception:
            logger.exception("Could not send callback fallback message")
            return None


async def safe_status_edit(status_message, chat_message, text, **kwargs):
    """Edit ONE bot-owned status message in place. Never create a fallback message.

    This is deliberately strict for workflow progress: a failed edit must not create
    a second progress message, otherwise the chat becomes a stream of duplicate
    status messages. Telegram permits editing messages sent by the bot itself.
    """
    text = str(text or "Processing...")
    if status_message is None:
        return None

    # Always address the original bot message by chat_id/message_id. This avoids
    # accidentally editing the user's uploaded document or a stale Message object.
    chat_id = getattr(status_message, "chat_id", None) or getattr(chat_message, "chat_id", None)
    message_id = getattr(status_message, "message_id", None)
    if chat_id is None or message_id is None:
        logger.warning("Status message has no chat/message id; progress update skipped")
        return status_message

    try:
        await chat_message.get_bot().edit_message_text(
            chat_id=chat_id, message_id=message_id, text=text, **kwargs
        )
    except BadRequest as exc:
        reason = str(exc).lower()
        if "message is not modified" not in reason:
            logger.warning("Could not edit single status message %s/%s: %s", chat_id, message_id, exc)
    except Exception as exc:
        logger.warning("Unexpected single status edit failure %s/%s: %s", chat_id, message_id, exc)
    return status_message


def _safe_filename_part(value, fallback="Document"):
    value = str(value or "").strip()
    value = value.replace("/", "-").replace("\\", "-")
    value = "".join(c if c.isalnum() or c in " ._-&()" else "_" for c in value)
    value = "_".join(value.split())
    value = value.strip("._- _")
    return value or fallback


def _ddmm(value, fallback="0000"):
    import re
    text = str(value or "")
    months={m.lower():i for i,m in enumerate(["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"],1)}
    patterns=[
        r"\b(\d{1,2})[\s/-]+([A-Za-z]{3,9})[\s,/-]+(\d{2,4})\b",
        r"\b(\d{1,2})[\s/-]+(\d{1,2})[\s/-]+(\d{2,4})\b",
        r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b",
    ]
    for pat in patterns:
        m=re.search(pat,text,re.I)
        if not m: continue
        try:
            if m.group(2).isalpha():
                day=int(m.group(1)); month=months[m.group(2)[:3].lower()]
            elif len(m.group(1))==4:
                month=int(m.group(2)); day=int(m.group(3))
            else:
                day=int(m.group(1)); month=int(m.group(2))
            return f"{day:02d}{month:02d}"
        except Exception: pass
    return fallback


def _airport_code(value):
    import re
    text=str(value or "")
    m=re.search(r"\(([A-Za-z]{3})\)",text)
    if m: return m.group(1).upper()
    codes=re.findall(r"\b[A-Za-z]{3}\b",text)
    return codes[-1].upper() if codes else "XXX"


def _package_filename(data):
    return f"Tour- {_safe_filename_part(data.get('tour_title'),'Tour Itinerary')}_{_safe_filename_part(data.get('client_name'),'Guest')}_{_safe_filename_part(data.get('destination'),'Destination')}_{_ddmm(data.get('travel_dates'))}.pdf"


def _title_for_person(person):
    person = person or {}
    title = str(person.get("title") or person.get("passenger_title") or "").strip()
    if title:
        return title.replace(".", "")
    name = str(person.get("name") or person.get("full_name") or "").strip()
    m = re.match(r"^(Mr|Mrs|Ms|Miss|Master|Mstr|Child|Infant)\.?\s+", name, re.I)
    return m.group(1) if m else ""

def _full_name_without_title(person):
    person = person or {}
    name = str(person.get("name") or person.get("full_name") or "").strip()
    # Supplier/AI combinations sometimes return title="Mr." AND name="Mr. Govind Sinha".
    # Strip every repeated leading honorific here so filenames never become Mr_Mr_....
    honorific = r"^(?:Mr|Mrs|Ms|Miss|Master|Mstr|Dr|Prof|Child|Infant)\.?\s+"
    while re.match(honorific, name, flags=re.I):
        name = re.sub(honorific, "", name, count=1, flags=re.I).strip()
    return name or "Guest"

def _filename_person(person):
    title = _title_for_person(person)
    name = _full_name_without_title(person)
    if title:
        return f"{_safe_filename_part(title)}_{_safe_filename_part(name)}"
    return _safe_filename_part(name)

def _flight_filename(data):
    passengers = data.get("passengers") or []
    person = passengers[0] if passengers else {"name": data.get("guest_name") or "Guest"}
    guest = _filename_person(person)
    segs = data.get("segments") or []
    arr = _safe_filename_part(segs[-1].get("arr_city") or segs[-1].get("arr_airport") or "Arrival") if segs else "Arrival"
    return f"Air_{guest}_{_safe_filename_part(arr)}.pdf"

def _bus_filename(data):
    passengers = data.get("passengers") or []
    person = passengers[0] if passengers else {"name": data.get("guest_name") or "Guest"}
    guest = _filename_person(person)
    arr = _safe_filename_part(data.get("arr_city") or data.get("arrival_city") or "Arrival")
    return f"Bus_{guest}_{arr}.pdf"

def _hotel_filename(data):
    guest_obj = {"name": data.get("guest_name") or "Guest", "title": data.get("guest_title") or data.get("title") or ""}
    guest = _filename_person(guest_obj)
    city = _safe_filename_part(data.get("hotel_city") or data.get("city") or "City")
    return f"Hotel_{guest}_{city}.pdf"


def is_allowed(update: Update) -> bool:
    return not ADMIN_USER_IDS or update.effective_user.id in ADMIN_USER_IDS


def main_keyboard():
    rows=[]
    if button_enabled("main_tour") or button_enabled("main_air"):
        row=[]
        if button_enabled("main_tour"): row.append("🗺️ Tour Guide")
        if button_enabled("main_air"): row.append("✈️ Air Print")
        rows.append(row)
    if button_enabled("main_bus") or button_enabled("main_hotel"):
        row=[]
        if button_enabled("main_bus"): row.append("🚌 Bus Print")
        if button_enabled("main_hotel"): row.append("🏨 Hotel Print")
        rows.append(row)
    rows.append(["🤖 Auto Creation"])
    if button_enabled("main_ai"):
        rows.append(["🤖 AI Assistant / New Request"])
    # V160: saved-reference browsing is intentionally hidden. Generated documents
    # are edited only from their own Modify & Regenerate / Voice-Text Edit buttons.
    if button_enabled("main_settings"):
        rows.append(["⚙️ Settings"])
    rows.append(["❌ Cancel"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def generated_document_keyboard(reference, kind=None):
    rows = []
    if kind == 'package':
        # V156: restore the proven Tour print controls directly under the PDF.
        # Changes are staged through the existing mod_* callbacks and applied by Done.
        if button_enabled('page_size_controls'):
            rows.append([
                InlineKeyboardButton('📐 Page Size', callback_data=f'mod_size:{reference}'),
                InlineKeyboardButton('🧾 Footer', callback_data=f'mod_footer_menu:{reference}')
            ])
        rows.append([InlineKeyboardButton('⚡ Auto Size', callback_data=f'autofit:{reference}')])
        rows.append([InlineKeyboardButton('🏢 B2B', callback_data=f'mod_b2b:{reference}')])
        rows.append([InlineKeyboardButton('✅ Done • Make Again', callback_data=f'mod_done:{reference}')])
        rows.append([InlineKeyboardButton('🛠️ Modify & Regenerate', callback_data=f'modify:{reference}')])
    elif kind in ('flight', 'bus', 'hotel'):
        if button_enabled('make_changes'):
            rows.append([InlineKeyboardButton('🎙️ Voice / Text Edit', callback_data=f'voice_edit:{reference}')])
        rows.append([InlineKeyboardButton('⚡ Quick Auto Fit', callback_data=f'autofit:{reference}')])
        rows.append([InlineKeyboardButton('🛠️ Modify & Regenerate', callback_data=f'modify:{reference}')])
    else:
        if button_enabled('make_changes'):
            rows.append([InlineKeyboardButton('🤖 Smart Make Changes', callback_data=f'edit_generated:{reference}')])
        rows.append([InlineKeyboardButton('🛠️ Modify & Regenerate', callback_data=f'modify:{reference}')])
    return InlineKeyboardMarkup(rows)

def ready_keyboard():
    return main_keyboard()



def _display_filename(name, max_len=42):
    name = str(name or "Document")
    if len(name) <= max_len:
        return name
    return name[:max_len-3] + "..."


def _record_caption(reference, prefix, extra=""):
    # V160: reference IDs stay internal. The owner edits the document from the
    # buttons attached to that PDF, so there is no need to expose MTBxx in chat.
    text = str(prefix or "")
    if extra:
        text += f"\n{extra}"
    return text


# ------------------------------------------------------------
# Adaptive print/edit controls
# ------------------------------------------------------------
PAGE_SIZE_OPTIONS = ("A5", "A4", "Letter", "Legal", "A3")

def _normalize_page_size(value):
    raw = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    aliases = {
        "a5":"A5", "a4":"A4", "a3":"A3",
        "letter":"Letter", "usletter":"Letter",
        "legal":"Legal", "uslegal":"Legal",
        "auto":"auto", "automatic":"auto",
    }
    return aliases.get(raw, "")

def _parse_hotel_cost_input(value, supplier_total=0):
    """Hotel cost uses Per Room + optional EB + Total, not Air/Bus fare fields."""
    raw=str(value or '').strip().replace('₹','').replace(',','')
    if not raw:
        raise ValueError('Please enter hotel costing. Example: Room 8500, EB 1200, Total 18200.')
    def pick(patterns):
        for pat in patterns:
            m=re.search(pat,raw,re.I)
            if m: return float(m.group(1))
        return None
    per_room=pick([r'\b(?:per\s*)?room(?:\s*cost)?\b\s*(?:[:=\-]|is|cost)?\s*([0-9]+(?:\.[0-9]+)?)',r'([0-9]+(?:\.[0-9]+)?)\s*(?:per\s*)?room\b'])
    eb=pick([r'\b(?:eb|extra\s*bed)\b\s*(?:[:=\-]|is|cost)?\s*([0-9]+(?:\.[0-9]+)?)',r'([0-9]+(?:\.[0-9]+)?)\s*(?:eb|extra\s*bed)\b'])
    total=pick([r'\b(?:total|grand\s*total|final)\b\s*(?:[:=\-]|is|cost)?\s*([0-9]+(?:\.[0-9]+)?)',r'([0-9]+(?:\.[0-9]+)?)\s*(?:total|grand\s*total|final)\b'])
    if per_room is None and eb is None and total is None:
        m=re.fullmatch(r'\s*([+-])?\s*([0-9]+(?:\.[0-9]+)?)\s*',raw)
        if not m:
            raise ValueError('Hotel cost format: Room 8500, EB 1200, Total 18200.')
        amount=float(m.group(2)); sign=m.group(1)
        if sign:
            base=float(supplier_total or 0)
            if base<=0: raise ValueError('A + / - hotel markup needs a supplier total. Enter Room/Total directly.')
            amount=base+amount if sign=='+' else base-amount
        per_room=amount; total=amount
    if total is None:
        total=(per_room or 0)+(eb or 0)
    return {'per_room':per_room,'eb':eb,'total':total,'currency':'INR'}


def _parse_markup_input(value, supplier_total=0):
    """Parse a fare entry from the owner.

    Supported forms:
      8500   -> final fare
      +500   -> supplier fare + 500
      -300   -> supplier fare - 300
    """
    text = str(value or "").strip().replace(",", "")
    if not text:
        raise ValueError("Please enter a fare, +markup, or -markup.")
    m = re.fullmatch(r"([+-])?\s*₹?\s*(\d+(?:\.\d+)?)", text)
    if not m:
        raise ValueError("Invalid fare. Use 8500, +500, or -300.")
    amount = float(m.group(2))
    sign = m.group(1)
    if sign:
        base = float(supplier_total or 0)
        if base <= 0:
            raise ValueError("A + / - markup needs a supplier fare. Enter the final fare directly instead.")
        fare = base + amount if sign == "+" else base - amount
    else:
        fare = amount
    if fare < 0:
        raise ValueError("Updated fare cannot be negative.")
    return fare

def _supplier_total(data):
    """Return the supplier payable total without dropping surcharges/ancillaries."""
    try:
        gross=float((data or {}).get("gross_total",0) or 0)
        if gross>0:
            return gross
        items=(data or {}).get("payment_items") or []
        total=sum(float(x.get("amount") or 0) for x in items if isinstance(x,dict))
        if total>0:
            return total
        return float((data or {}).get("base_fare",0) or 0)+float((data or {}).get("taxes",0) or 0)
    except Exception:
        return 0.0

def _parse_reply_controls(instruction, current_fare, data, current_footer=False, current_logo=True, current_page_size="auto"):
    """Parse non-content commands before Gemini. Remaining text is sent to Gemini."""
    original = str(instruction or "").strip()
    text = original
    lower = text.lower()
    changed = False
    controls = {
        "fare": current_fare,
        "footer": current_footer,
        "footer_mode": "design" if current_footer else "none",
        "logo": current_logo,
        "page_size": current_page_size or "auto",
    }

    # Footer 2 commands: use the supplied Travel Contact Card artwork.
    if (re.fullmatch(r"\s*(footer\s*2|footer2|use footer 2|use footer2|contact card)\s*", lower)
            or re.search(r"\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:footer\s*2|footer2|contact card)\b", lower)):
        controls["footer"] = True; controls["footer_mode"] = "footer2"; changed = True
        text = re.sub(r"(?i)\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:footer\s*2|footer2|contact card)\b", "", text)
        text = re.sub(r"(?i)^\s*(footer\s*2|footer2|use footer 2|use footer2|contact card)\s*$", "", text)

    # Smart footer/design commands. These intentionally work as natural-language shortcuts
    # on any Air/Bus/Hotel reference: "design" switches to the full footer design, while
    # "contact bar" switches to the compact contact bar.
    if (re.fullmatch(r"\s*(footer\s*1|footer1|old design|use footer 1)\s*", lower)
            or re.search(r"\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:footer\s*1|footer1|old design)\b", lower)):
        controls["footer"] = True; controls["footer_mode"] = "design"; changed = True
        text = re.sub(r"(?i)\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:footer\s*1|footer1|old design)\b", "", text)
        text = re.sub(r"(?i)^\s*(footer\s*1|footer1|old design|use footer 1)\s*$", "", text)
    elif (re.fullmatch(r"\s*(footer\s*2|footer2|new design|use footer 2)\s*", lower)
            or re.search(r"\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:footer\s*2|footer2|new design)\b", lower)):
        controls["footer"] = True; controls["footer_mode"] = "footer2"; changed = True
        text = re.sub(r"(?i)\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:footer\s*2|footer2|new design)\b", "", text)
        text = re.sub(r"(?i)^\s*(footer\s*2|footer2|new design|use footer 2)\s*$", "", text)
    elif (re.fullmatch(r"\s*(design|use design|footer design)\s*", lower)
            or re.search(r"\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+design\b", lower)):
        controls["footer"] = True; controls["footer_mode"] = "design"; changed = True
        text = re.sub(r"(?i)\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+design\b", "", text)
        text = re.sub(r"(?i)^\s*(design|use design|footer design)\s*$", "", text)
    elif (re.fullmatch(r"\s*(contact bar|footer bar|use contact bar|use footer bar)\s*", lower)
          or re.search(r"\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:contact|footer)\s+bar\b", lower)):
        controls["footer"] = True; controls["footer_mode"] = "bar"; changed = True
        text = re.sub(r"(?i)\b(?:switch|change|replace|use|make)\s+(?:it|the footer)\s+(?:to|as|with)\s+(?:contact|footer)\s+bar\b", "", text)
        text = re.sub(r"(?i)^\s*(contact bar|footer bar|use contact bar|use footer bar)\s*$", "", text)

    # Footer controls.  Supported edit commands:
    #   add footer bar / add contact bar
    #   add footer design
    #   remove footer / without footer
    if re.search(r"\b(?:remove|delete|without|no)\s+(?:the\s+)?footer\b", lower) or re.search(r"\bfooter\s+(?:remove|off)\b", lower):
        controls["footer"] = False; controls["footer_mode"] = "none"; changed = True
        text = re.sub(r"(?i)\b(?:remove|delete|without|no)\s+(?:the\s+)?footer\b", "", text)
        text = re.sub(r"(?i)\bfooter\s+(?:remove|off)\b", "", text)
    elif re.search(r"\b(?:add|include|with|put|use)\s+(?:the\s+)?(?:mytourbazar\s+)?(?:contact\s+)?footer\s+bar\b", lower) or re.search(r"\b(?:add|use)\s+contact\s+bar\b", lower):
        controls["footer"] = True; controls["footer_mode"] = "bar"; changed = True
        text = re.sub(r"(?i)\b(?:add|include|with|put|use)\s+(?:the\s+)?(?:mytourbazar\s+)?(?:contact\s+)?footer\s+bar\b", "", text)
        text = re.sub(r"(?i)\b(?:add|use)\s+contact\s+bar\b", "", text)
    elif re.search(r"\b(?:add|include|with|put|use)\s+(?:the\s+)?footer\s+design\b", lower):
        controls["footer"] = True; controls["footer_mode"] = "design"; changed = True
        text = re.sub(r"(?i)\b(?:add|include|with|put|use)\s+(?:the\s+)?footer\s+design\b", "", text)
    elif re.search(r"\b(?:add|include|with|put|use)\s+(?:the\s+)?footer\b", lower) or re.search(r"\bfooter\s+(?:add|on)\b", lower):
        controls["footer"] = True; controls["footer_mode"] = "design"; changed = True
        text = re.sub(r"(?i)\b(?:add|include|with|put)\s+(?:the\s+)?footer\b", "", text)
        text = re.sub(r"(?i)\bfooter\s+(?:add|on)\b", "", text)

    # MyTourBazar logo controls. Airline logos are independent and are never disabled by this.
    if re.search(r"\b(remove|delete|without|no)\s+(the\s+)?(mytourbazar\s+)?logo\b", lower) or re.search(r"\blogo\s+(remove|off)\b", lower):
        controls["logo"] = False; changed = True
        text = re.sub(r"(?i)\b(remove|delete|without|no)\s+(the\s+)?(mytourbazar\s+)?logo\b", "", text)
        text = re.sub(r"(?i)\blogo\s+(remove|off)\b", "", text)
    elif re.search(r"\b(add|include|with|put|use)\s+(the\s+)?(mytourbazar\s+)?logo\b", lower) or re.search(r"\blogo\s+(add|on)\b", lower):
        controls["logo"] = True; changed = True
        text = re.sub(r"(?i)\b(add|include|with|put|use)\s+(the\s+)?(mytourbazar\s+)?logo\b", "", text)
        text = re.sub(r"(?i)\blogo\s+(add|on)\b", "", text)

    # Page-size commands. Supports: "page size A3", "change page to legal", "A3", "auto page size".
    size = ""
    m = re.search(r"(?i)\b(?:page\s*size|paper\s*size|page|paper)\s*(?:to|=|:)??\s*(a5|a4|a3|letter|legal|auto|automatic)\b", text)
    if m: size = _normalize_page_size(m.group(1))
    elif re.fullmatch(r"(?i)\s*(a5|a4|a3|letter|legal|auto|automatic)\s*", text): size = _normalize_page_size(text)
    if size:
        controls["page_size"] = size; changed = True
        text = re.sub(r"(?i)\b(?:page\s*size|paper\s*size|page|paper)\s*(?:to|=|:)??\s*(a5|a4|a3|letter|legal|auto|automatic)\b", "", text)
        text = re.sub(r"(?i)^\s*(a5|a4|a3|letter|legal|auto|automatic)\s*$", "", text)

    # Global print font controls. These are handled before Gemini so a request such as
    # "make the font Liberation Serif Bold" changes the actual print renderer, not the
    # itinerary data.
    font_match = None
    low_for_font = text.lower()
    for _font_name in FONT_OPTIONS:
        if _font_name.lower() in low_for_font:
            font_match = _font_name
            break
    if font_match:
        set_font(font_match)
        changed = True
        text = re.sub(re.escape(font_match), '', text, flags=re.I)

    # Global print text-size controls.
    if re.search(r"(?i)\b(?:increase|enlarge|bigger|larger|up)\b.*\b(?:font|text|print)\s*size\b|\b(?:increase|enlarge|make)\s+(?:the\s+)?(?:font|text)\b", text):
        adjust_text_scale(0.05); changed = True
        text = re.sub(r"(?i)\b(?:increase|enlarge|bigger|larger|up)\b.*?(?:font|text)(?:\s*size)?\b", '', text)
    elif re.search(r"(?i)\b(?:decrease|reduce|smaller|down)\b.*\b(?:font|text)\s*size\b|\b(?:decrease|reduce|make)\s+(?:the\s+)?(?:font|text)\b", text):
        adjust_text_scale(-0.05); changed = True
        text = re.sub(r"(?i)\b(?:decrease|reduce|smaller|down)\b.*?(?:font|text)(?:\s*size)?\b", '', text)

    # Fare/cost controls.
    if re.search(r"(?i)\b(remove|delete|without|no)\s+(the\s+)?(fare|cost)\b", text) or re.search(r"(?i)\b(fare|cost)\s+(remove|off)\b", text) or "print without fare" in lower:
        controls["fare"] = None; changed = True
        text = re.sub(r"(?i)\b(remove|delete|without|no)\s+(the\s+)?(fare|cost)\b", "", text)
        text = re.sub(r"(?i)\b(fare|cost)\s+(remove|off)\b", "", text)
        text = re.sub(r"(?i)print\s+without\s+fare", "", text)
    else:
        # Explicit final fare: "add cost 8500", "fare 8500", "update fare to 9000".
        m = re.search(r"(?i)\b(?:add|set|update|change|replace)\s+(?:the\s+)?(?:cost|fare)\s*(?:to|=)?\s*₹?\s*([0-9][0-9,]*(?:\.\d+)?)\b", text)
        if not m:
            m = re.search(r"(?i)\b(?:cost|fare)\s*(?:to|=)\s*₹?\s*([0-9][0-9,]*(?:\.\d+)?)\b", text)
        if m:
            controls["fare"] = float(m.group(1).replace(",", "")); changed = True
            text = text[:m.start()] + text[m.end():]
        else:
            # A bare +500/-500 is a markup adjustment. Base it on current printed fare,
            # otherwise use the supplier fare.
            m = re.fullmatch(r"\s*([+-])\s*₹?\s*([0-9][0-9,]*(?:\.\d+)?)\s*", text)
            if m:
                base = float(current_fare or 0) or _supplier_total(data)
                amount = float(m.group(2).replace(",", ""))
                controls["fare"] = base + amount if m.group(1) == "+" else base - amount
                if controls["fare"] < 0: raise ValueError("Updated fare cannot be negative.")
                changed = True; text = ""
            else:
                # Natural form: "add 500" / "increase fare by 500" / "decrease cost by 300".
                m = re.search(r"(?i)\b(?:add|increase|decrease|reduce)\s+(?:the\s+)?(?:fare|cost)?\s*(?:by)?\s*₹?\s*([+-]?\d[\d,]*(?:\.\d+)?)\b", text)
                if m and re.search(r"(?i)\b(?:add|increase|decrease|reduce)\b", m.group(0)):
                    base = float(current_fare or 0) or _supplier_total(data)
                    amount = float(m.group(1).replace(",", ""))
                    verb = m.group(0).lower()
                    controls["fare"] = base - amount if "decrease" in verb or "reduce" in verb else base + amount
                    if controls["fare"] < 0: raise ValueError("Updated fare cannot be negative.")
                    changed = True; text = text[:m.start()] + text[m.end():]

    return controls, text.strip(" ,;\n"), changed


def _generate_ticket_base(kind, data, fare, output_path, logo_path, page_size, text_scale_override=None, logo_scale_override=None):
    if kind == "flight":
        return generate_flight_ticket(data, fare, output_path, logo_path, page_size=page_size, text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    if kind == "bus":
        return generate_bus_ticket(data, fare, output_path, logo_path, page_size=page_size, text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    if kind == "hotel":
        return generate_hotel_voucher(data, output_path, logo_path, fare=fare, page_size=page_size, text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    raise RuntimeError(f"Unsupported document type: {kind}")


def _generate_adaptive_ticket(kind, data, fare, output_path, logo_path=None, requested_size="auto", text_scale_override=None, logo_scale_override=None):
    """Generate without shrinking the design to force a single page.

    ``auto`` now means the normal A4 layout.  If the content is longer, the
    renderer is allowed to flow naturally onto page 2+ so typography and
    spacing remain professional.  A5/A4/Letter/Legal/A3 can still be selected
    explicitly through the reply controls.
    """
    size = _normalize_page_size(requested_size) or "auto"
    candidate = "A4" if size == "auto" else size
    _generate_ticket_base(kind, data, fare, output_path, logo_path, candidate, text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    return candidate


def files_list_keyboard(page=0, per_page=10):
    records = list_records()
    total_pages = max(1, (len(records) + per_page - 1) // per_page)
    page = max(0, min(page, total_pages - 1))
    start = page * per_page
    rows = records[start:start + per_page]
    buttons = []
    for r in rows:
        label = f"{r.get('reference','?')} • {r.get('type','document').title()} • {_display_filename(r.get('filename',''))}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"select_ref:{r.get('reference','')}")])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"files_page:{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"files_page:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("✏️ Enter Reference Number", callback_data="enter_ref")])
    return InlineKeyboardMarkup(buttons)


async def show_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    records = list_records()
    if not records:
        await update.message.reply_text(
            "📂 *MyTourBazar Files*\n\nNo generated files have been saved yet.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
        return
    await update.message.reply_text(
        "📂 *MyTourBazar Files*\n\nSelect a reference to edit that document.\nLatest files are shown first.",
        parse_mode="Markdown", reply_markup=files_list_keyboard(0)
    )


async def edit_by_ref_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    context.user_data["awaiting_edit_ref"] = True
    await update.message.reply_text(
        "✏️ *Edit an Existing Document*\n\n"
        "This old reference-edit shortcut has been removed.\n\nUse *Modify & Regenerate* on the generated PDF instead.",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )



REF_RE = re.compile(r"(?:\bMTB[-_ ]?\d{1,}\b|\b\d{3,}\b)", re.I)

def normalize_reference(value):
    m = REF_RE.search(str(value or ""))
    if not m: return ""
    digits = re.sub(r"\D", "", m.group(0))
    return f"MTB{int(digits):02d}" if digits else ""

def _telegram_message_key(message):
    if message is None:
        return ""
    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None) or getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    if chat_id is None or message_id is None:
        return ""
    return f"{chat_id}:{message_id}"


def _register_reference_message(reference, message):
    """Remember the exact Telegram output message for reliable Reply-to-edit routing.

    The IDs stay internal in the local record; nothing is shown in the Telegram caption.
    """
    key = _telegram_message_key(message)
    if not reference or not key:
        return
    record = load_record(reference)
    if not record:
        return
    keys = [str(x) for x in (record.get("telegram_message_keys") or []) if str(x).strip()]
    if key not in keys:
        keys.append(key)
    record["telegram_message_keys"] = keys[-40:]
    update_record(reference, record)


def find_reference_for_reply(replied):
    message_key = _telegram_message_key(replied)
    if message_key:
        for r in list_records():
            ref = r.get("reference")
            rec = load_record(ref) or {}
            if message_key in [str(x) for x in (rec.get("telegram_message_keys") or [])]:
                return ref
    source = " ".join(filter(None, [getattr(replied, "text", None), getattr(replied, "caption", None), getattr(getattr(replied, "document", None), "file_name", None)]))
    ref = normalize_reference(source)
    if ref and load_record(ref): return ref
    filename = getattr(getattr(replied, "document", None), "file_name", None)
    for r in list_records():
        if filename and r.get("filename") == filename: return r.get("reference")
    low = source.lower()
    for r in list_records():
        rec=load_record(r.get("reference")) or {}
        data=rec.get("data") or {}
        hay=" ".join(str(data.get(k,"")) for k in ("client_name","destination","tour_title","hotel_name","hotel_city","guest_name")) + " " + str(r.get("filename",""))
        if low and low in hay.lower(): return r.get("reference")
    return ""

async def reply_reference_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route owner replies. Fare-entry replies are handled first so Telegram's
    reply-to-message feature cannot get swallowed by a ConversationHandler.
    Otherwise, a reply to a generated MTB reference is treated as an AI edit."""
    msg = update.message
    if not msg:
        return

    # V167: replying to any currently displayed Tour draft behaves like Smart
    # Modify & Regenerate for that draft. A normal (non-reply) message can still
    # follow the existing final-submission workflow.
    replied = getattr(msg, "reply_to_message", None)
    if replied and context.user_data.get('itinerary'):
        draft_ids = {int(x) for x in (context.user_data.get('tour_draft_message_ids') or []) if str(x).isdigit()}
        replied_id = getattr(replied, 'message_id', None)
        if replied_id in draft_ids:
            instruction = (msg.text or '').strip()
            if instruction:
                variant = _tour_reply_variant_command(instruction)
                if variant:
                    await _reply_draft_tour_variant(msg, context, variant)
                else:
                    await perform_draft_edit(update, context, instruction)
                raise ApplicationHandlerStop

    # V154: the editable Tour draft is a one-shot final submission. This global
    # router is registered before receive_extra_text, so it must explicitly hand the
    # message to the Tour finalizer instead of swallowing a Telegram Reply.
    if _tour_v2_active(context) and context.user_data.get('tour_v2_phase') == 'awaiting_edited_final':
        instruction=(msg.text or '').strip()
        if instruction:
            await _tour_v2_process_edited_final(msg, context, instruction)
            raise ApplicationHandlerStop

    # Final Tour PDF name gate must also win over generic reply/edit routing.
    # The global reply router sees every normal text message before receive_extra_text.
    if context.user_data.get('awaiting_tour_print_name'):
        text = (msg.text or '').strip()
        if text == '❌ Cancel':
            context.user_data.pop('awaiting_tour_print_name', None)
            context.user_data.pop('pending_tour_pdf_request', None)
            await msg.reply_text('❌ Tour PDF print cancelled. The draft is still available.', reply_markup=main_keyboard())
            raise ApplicationHandlerStop
        data = context.user_data.get('itinerary') or {}
        if text == '⏭️ Print Without Name':
            data['client_name'] = ''
        elif text:
            data['client_name'] = text
            context.user_data['guest_name'] = text
            await msg.reply_text(f'✅ Guest name added: *{text}*', parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
        else:
            await msg.reply_text('Please enter the Guest / Client Name, or tap ⏭️ Print Without Name.', reply_markup=pending_tour_name_keyboard())
            raise ApplicationHandlerStop
        context.user_data['itinerary'] = data
        context.user_data.pop('awaiting_tour_print_name', None)
        await _finish_pending_tour_pdf(msg, context)
        raise ApplicationHandlerStop

    # V159: the legacy Tour markup session has been removed. Tour selling costs are
    # changed only through Modify & Regenerate (text or voice) as direct customer rates.
    for _legacy_key in (
        'pending_tour_markup_print','pending_tour_markup_input','pending_tour_markup_mode',
        'pending_tour_markup_snapshot','pending_tour_markup_candidate'
    ):
        context.user_data.pop(_legacy_key, None)

    # Draft Smart Edit: once the owner taps Smart Edit Draft, a normal message or a
    # Telegram reply is treated as an edit to the in-memory draft. Nothing is printed yet.
    if context.user_data.get('editing_current_itinerary'):
        instruction = (msg.text or '').strip()
        if instruction:
            await perform_draft_edit(update, context, instruction)
            raise ApplicationHandlerStop
        return

    # IMPORTANT: The fare prompt is sent after the Air/Bus/Hotel ConversationHandler
    # has ended. If the owner replies to that prompt, Telegram still supplies
    # reply_to_message. Handle the pending fare before looking for an MTB reference.
    pending_kind = context.user_data.get("pending_fare_kind")
    if pending_kind in ("flight", "bus", "hotel"):
        _cancel_auto_print(context)
        instruction = (msg.text or "").strip()
        if instruction:
            supplier_total = float(context.user_data.get("pending_fare_supplier_total", 0) or 0)
            try:
                fare = _parse_markup_input(instruction, supplier_total)
            except ValueError as exc:
                await msg.reply_text(
                    f"❌ {exc}\n\nUse `8500`, `+500`, or `-300` as applicable.",
                    parse_mode="Markdown",
                )
                return True

            context.user_data.pop("pending_fare_kind", None)
            context.user_data[f"pending_{pending_kind}_fare"] = fare
            try:
                await ask_footer_choice(msg, context, pending_kind)
            except Exception as exc:
                logger.exception("PDF generation from fare reply failed")
                await msg.reply_text(
                    f"❌ PDF generation failed.\n\nReason: `{str(exc)[:800]}`",
                    parse_mode="Markdown", reply_markup=main_keyboard()
                )
            raise ApplicationHandlerStop

    # Smart Make Changes button: once a reference is selected, the next natural-language
    # message is treated as the edit instruction even if the owner does not use Telegram's
    # Reply action. This is intentionally open-ended; Gemini handles arbitrary changes.
    active_reference = context.user_data.get("editing_reference")
    if active_reference:
        # V160: once Modify & Regenerate/Voice-Text Edit is tapped, the next text
        # belongs to that exact saved PDF. Never send it through old saved-file/ref-ID
        # discovery and never let an active ConversationHandler reinterpret it.
        instruction = (msg.text or "").strip()
        if instruction == "❌ Cancel":
            context.user_data.pop("editing_reference", None)
            context.user_data.pop("voice_edit_reference", None)
            await msg.reply_text("❌ Edit cancelled.", reply_markup=main_keyboard())
            raise ApplicationHandlerStop
        if instruction:
            await perform_saved_edit(update, context, instruction)
            raise ApplicationHandlerStop
        return

    if not msg.reply_to_message:
        return
    replied = msg.reply_to_message
    reference = find_reference_for_reply(replied)
    if not reference:
        return
    record = load_record(reference)
    if not record:
        await msg.reply_text(f"❌ I found {reference} in the replied message, but that reference is no longer available.", reply_markup=main_keyboard())
        return
    instruction = (msg.text or "").strip()
    if not instruction:
        await msg.reply_text(f"✏️ I found *{reference}*. Please tell me what you want changed.", parse_mode="Markdown")
        return
    # Lightweight reply actions for generated Tour PDFs. These are intentionally
    # handled before the general Smart Edit path so a simple reply like +10000 or
    # Start Date: 20/09/2026 does exactly the requested action.
    if record.get("type") == "package":
        # V170 Smart Reply: replying to a generated Tour PDF with only a
        # detail/output request is an output conversion, not a generic content edit.
        variant = _tour_reply_variant_command(instruction)
        if variant:
            await _reply_saved_tour_variant(msg, context, reference, record, variant)
            raise ApplicationHandlerStop

        data = dict(record.get("data") or {})
        low = instruction.lower()
        # Detect an explicit start-date reply before using the parsed match.
        dm = re.search(r'(?i)\b(?:start\s*date|travel\s*date|date)\s*[:=-]\s*(\d{1,2}[/-]\d{1,2}[/-]\d{2,4})\b', instruction)
        if dm:
            new_date=dm.group(1)
            data['start_date']=new_date
            if not str(data.get('travel_dates') or '').strip():
                data['travel_dates']=new_date
            record['data']=data
            save_record(reference, record)
            context.user_data['itinerary']=data
            await msg.reply_text(f"📅 Tour start date set to *{new_date}*. Regenerating the Tour PDF now.", parse_mode='Markdown')
            context.user_data['pending_tour_pdf_detail']=data.get('detail_level') or 'basic'
            context.user_data['pending_tour_pdf_no_cost']=False
            context.user_data['pending_tour_document_mode']=data.get('document_mode') or 'itinerary'
            await generate_tour_pdf_final(msg, context, data, data.get('detail_level') or 'basic', False, reference=reference)
            raise ApplicationHandlerStop
    context.user_data["editing_reference"] = reference
    await perform_saved_edit(update, context, instruction)
    raise ApplicationHandlerStop

async def _begin_edit(update, context, reference):
    raw = str(reference or "").strip()
    reference = normalize_reference(raw) or raw.upper()
    record = load_record(reference)
    if not record:
        low=raw.lower()
        matches=[]
        for r in list_records():
            rec=load_record(r.get("reference")) or {}
            data=rec.get("data") or {}
            hay=" ".join([str(r.get("filename","")),str(data.get("client_name","")),str(data.get("guest_name","")),str(data.get("destination","")),str(data.get("tour_title","")),str(data.get("hotel_name","")),str(data.get("hotel_city",""))]).lower()
            if low and low in hay: matches.append(r.get("reference"))
        if len(matches)==1:
            reference=matches[0]; record=load_record(reference)
        elif matches:
            await update.message.reply_text("🔎 I found multiple matching references: " + ", ".join(matches[:10]) + "\nPlease send the exact reference number.", reply_markup=main_keyboard()); return
    if not record:
        await update.message.reply_text(
            "❌ That saved document is no longer available. Please use *Modify & Regenerate* on the PDF you want to change.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
        return
    context.user_data["editing_reference"] = reference
    context.user_data["awaiting_edit_ref"] = False
    await update.message.reply_text(
        f"✏️ *Editing {reference}*\n\n"
        f"Send one normal message or voice note describing the changes in your own words.\n\n"
        f"Examples:\n"
        f"• `Adult cost 43700 and CWB 32000.`\n"
        f"• `Keep adult at 43700, child without bed 26000, and change Munnar room to Premium Valley View.`\n"
        f"• `Change Day 2 sightseeing to include Dwarkadhish Temple and Bet Dwarka.`\n"
        f"• `Raipur to Nagpur by Vande Bharat, Nagpur to Goa by flight, and Delhi to Raipur by bus.`\n"
        f"• `Change passenger name to Mr. Amit Sharma.`\n"
        f"• `Detailed itinerary` → reply to a Tour PDF to get the detailed PDF directly.\n"
        f"• `Detailed WhatsApp` → get only the detailed WhatsApp version.\n"
        f"• `Detailed draft` → return to an editable detailed draft first.\n\n"
        f"For Tour costing, tell me the final customer rate naturally - there is no separate markup system. "
        f"Gemini will understand the intent, preserve the rest, and regenerate the PDF.",
        parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
    )


def _package_edit_is_cost_only(instruction, parsed_rates=None):
    """True when a Modify & Regenerate reply contains only customer costing.

    Keep cost-only edits local so Gemini cannot rebuild the tour and drop the
    owner's Adult/CWB/CNB/EB selling rates before PDF rendering.
    """
    raw=str(instruction or '').strip()
    rates=parsed_rates or {}
    if not raw or not rates:
        return False
    cleaned=raw.replace('₹',' ')
    aliases=(
        'child without bed','child no bed','child with bed','extra bed',
        'per adult','per child','adult','adults','adt','cwb','cnb','eb','child','children',
        'rate','cost','price','fare','is','should be','will be','at','for','per','rs','inr'
    )
    for phrase in sorted(aliases,key=len,reverse=True):
        cleaned=re.sub(rf'(?i)\b{re.escape(phrase)}\b',' ',cleaned)
    cleaned=re.sub(r'[0-9][0-9,]*(?:\.[0-9]+)?',' ',cleaned)
    cleaned=re.sub(r'[\s,;:/=+\-–—]+','',cleaned)
    return cleaned == ''


def _hotel_edit_is_cost_only(instruction, hotel_cost=None):
    """True when a Hotel Voice/Text edit contains only customer room costing.

    Explicit room/EB/total amounts do not need Gemini; they can be applied directly
    to the structured Hotel cost box and regenerated reliably.
    """
    raw=str(instruction or '').strip()
    if not raw or not hotel_cost:
        return False
    cleaned=raw.replace('₹',' ')
    phrases=(
        'extra mattress','extra bed','grand total','customer cost','customer rate',
        'hotel cost','hotel rate','per room cost','per room','room cost','room rate',
        'room','eb','total','cost','rate','price','fare','is','should be','will be',
        'keep','make','set','change','add','at','for','rs','inr'
    )
    for phrase in sorted(phrases,key=len,reverse=True):
        cleaned=re.sub(rf'(?i)\b{re.escape(phrase)}\b',' ',cleaned)
    cleaned=re.sub(r'[0-9][0-9,]*(?:\.[0-9]+)?',' ',cleaned)
    cleaned=re.sub(r'[\s,;:/=+\-–—]+','',cleaned)
    return cleaned == ''


async def perform_saved_edit(update, context, instruction):
    reference = context.user_data.get("editing_reference")
    record = load_record(reference) if reference else None
    if not record:
        context.user_data.pop("editing_reference", None)
        await update.message.reply_text("❌ That saved document is no longer available. Please tap *Modify & Regenerate* on the PDF you want to change.", parse_mode="Markdown", reply_markup=main_keyboard())
        return
    status = await update.message.reply_text(
        "✏️ *Updating your document...*\n\n████░░░░░░░░░░░ 25%\n\n🔍 Understanding your requested changes...",
        parse_mode="Markdown"
    )
    try:
        doc_type = record.get("type", "package")
        old_data = record.get("data")
        old_fare = record.get("fare")
        current_footer = bool(record.get("footer", False)) if doc_type != "package" else False
        current_footer_mode = record.get("footer_mode", "design" if current_footer else "none") if doc_type != "package" else "none"
        current_logo = bool(record.get("logo_enabled", True))
        current_page_size = record.get("page_size", "auto") or "auto"
        current_text_scale = float(record.get("text_scale") or load_settings().get("text_scale", 1.0))
        current_logo_scale = float(record.get("logo_scale") or get_logo_scale(doc_type))

        # Older PDFs created before the richer settings were stored are still editable.
        if not old_data:
            source_pdf = GENERATED_DIR / str(record.get("filename", ""))
            if not source_pdf.exists():
                raise RuntimeError("The saved PDF file is no longer present on this computer.")
            temp_pdf = TEMP_DIR / f"legacy_{reference}.pdf"
            shutil.copy2(source_pdf, temp_pdf)
            part = [{"path": str(temp_pdf), "mime_type": "application/pdf"}]
            if doc_type == "package":
                old_data = await _run_ai_with_retry_status(update.message, lambda: asyncio.to_thread(extract_itinerary_from_parts, part, "", GEMINI_API_KEY, GEMINI_MODEL), status=status)
            elif doc_type == "flight":
                old_data = await _run_ai_with_retry_status(update.message, lambda: asyncio.to_thread(extract_flight_ticket, part, "", GEMINI_API_KEY, GEMINI_MODEL), status=status)
            elif doc_type == "bus":
                old_data = await _run_ai_with_retry_status(update.message, lambda: asyncio.to_thread(extract_bus_ticket, part, "", GEMINI_API_KEY, GEMINI_MODEL), status=status)
            elif doc_type == "hotel":
                old_data = await _run_ai_with_retry_status(update.message, lambda: asyncio.to_thread(extract_hotel_voucher, part, "", GEMINI_API_KEY, GEMINI_MODEL), status=status)
            else:
                raise RuntimeError(f"Unsupported legacy document type: {doc_type}")
            if old_fare is None and doc_type in ("flight", "bus", "hotel"):
                source = _supplier_total(old_data)
                # Legacy files do not know whether the old print intentionally hid fare. Preserve
                # the old behavior by treating a real extracted fare as the current fare.
                old_fare = source if source > 0 else None
            record["data"] = old_data
            record["fare"] = old_fare

        package_rates = {}
        ai_package_rates = {}
        package_cost_only = False
        hotel_cost_update = None
        hotel_cost_only = False
        if doc_type == 'hotel' and re.search(r'(?i)\b(?:per\s*room|room\s*(?:cost|rate|price)|hotel\s*(?:cost|rate|price)|customer\s*(?:cost|rate|price)|extra\s*(?:bed|mattress)|\beb\b|grand\s*total|total\s*(?:cost|price|fare))\b', str(instruction or '')):
            try:
                hotel_cost_update = _parse_hotel_cost_input(instruction, float(old_fare or 0) or _supplier_total(old_data))
            except Exception:
                hotel_cost_update = None
            hotel_cost_only = _hotel_edit_is_cost_only(instruction, hotel_cost_update)
        if doc_type == 'package':
            try:
                package_rates = _tour_v2_parse_costs(instruction)
            except Exception:
                package_rates = {}
            package_cost_only = _package_edit_is_cost_only(instruction, package_rates)

        controls, remaining, controls_changed = _parse_reply_controls(
            instruction, old_fare, old_data, current_footer, current_logo, current_page_size
        )
        if not re.search(r"\b(?:footer|contact\s+card|contact\s+bar|design)\b", instruction, re.I):
            controls["footer_mode"] = current_footer_mode if current_footer_mode in ("bar","design","footer2") else _default_footer_mode(doc_type)
            controls["footer"] = True

        new_data = old_data
        new_fare = controls["fare"]
        requested_detail = _itinerary_detail_command(instruction) if doc_type == "package" else None
        package_whatsapp = bool(doc_type == "package" and re.search(r"\b(?:whatsapp|text itinerary|text version)\b", instruction, re.I))
        package_pdf = bool(doc_type == "package" and re.search(r"\b(?:pdf|print)\b", instruction, re.I))

        # Cost-only Hotel edits are deterministic/local. A direct room/EB/total
        # change does not need Gemini and immediately updates the structured Hotel cost box.
        if doc_type == 'hotel' and hotel_cost_only and hotel_cost_update:
            new_data = copy.deepcopy(old_data or {})
            new_data['customer_hotel_cost'] = hotel_cost_update
            remaining = ''
            controls_changed = True
        # Cost-only Tour edits are deterministic/local. `Adult 43700` must never
        # pass through the general AI editor, because that can recreate the tour
        # object and lose package_costs/show_cost before rendering.
        elif doc_type == 'package' and package_cost_only and package_rates:
            new_data = _tour_v2_apply_costs(copy.deepcopy(old_data), package_rates)
            new_data = _normalize_guest_counts(new_data)
            new_data['show_cost'] = True
            remaining = ''
            controls_changed = True
        elif requested_detail:
            new_data = await _run_ai_with_retry_status(
                update.message,
                lambda: asyncio.to_thread(enhance_package_itinerary, old_data, GEMINI_API_KEY, GEMINI_MODEL, requested_detail),
                status=status,
            )
            new_data["client_name"] = old_data.get("client_name", "")
            new_data["detail_level"] = requested_detail
            ai_fare = None
        elif remaining:
            new_data, ai_fare = await _run_ai_with_retry_status(
                update.message,
                lambda: asyncio.to_thread(apply_edit, doc_type, old_data, remaining, GEMINI_API_KEY, GEMINI_MODEL, old_fare),
                status=status,
            )
            if ai_fare is not None:
                new_fare = ai_fare
        elif not controls_changed:
            # No recognized control and no text left should never silently do nothing.
            raise ValueError("I could not understand the edit. Try: 'add footer', 'remove logo', 'add cost +500', or 'page size A3'.")

        if doc_type == 'hotel' and hotel_cost_update:
            new_data = copy.deepcopy(new_data or old_data or {})
            new_data['customer_hotel_cost'] = hotel_cost_update
            # Hotel customer costing is a structured room calculation, not an Air/Bus fare box.
            # Keep supplier fare data untouched; the Hotel renderer reads customer_hotel_cost.
        if doc_type == 'package' and package_rates:
            # Explicit numeric rates are owner-authored customer selling rates. Re-apply
            # them after Gemini so a mixed edit can never erase Adult/CWB/CNB/EB values.
            new_data = _tour_v2_apply_costs(new_data, package_rates)
            new_data['show_cost'] = True
        elif doc_type == 'package':
            # If the local parser did not understand the wording, trust Gemini's semantic
            # edit and reconcile only the rate fields that actually changed. This handles
            # normal/Hinglish voice phrasing without introducing a markup workflow.
            new_data, ai_package_rates = _tour_reconcile_ai_customer_costs(old_data,new_data,instruction)

        if doc_type == 'package' and record.get('b2b'):
            new_data = _apply_tour_document_mode_fields(
                new_data,
                record.get('document_mode') or new_data.get('document_mode') or 'itinerary',
                b2b=True,
            )

        if doc_type == "package" and package_whatsapp and not package_pdf:
            new_data["detail_level"] = requested_detail or new_data.get("detail_level") or "detailed"
            record.update({"data": new_data, "detail_level": new_data["detail_level"]})
            update_record(reference, record)
            await reply_text_chunked(update.message, build_whatsapp_itinerary(new_data, new_data["detail_level"]), parse_mode="Markdown")
            await update.message.reply_text("📱 WhatsApp itinerary ready. You can reply with `PDF itinerary`, `basic itinerary`, `detailed itinerary`, or another change.", parse_mode="Markdown", reply_markup=tour_output_keyboard())
            context.user_data.pop("editing_reference", None)
            return

        if doc_type == "package":
            filename = _package_filename(new_data)
        elif doc_type == "flight":
            filename = _flight_filename(new_data)
        elif doc_type == "bus":
            filename = _bus_filename(new_data)
        elif doc_type == "hotel":
            filename = _hotel_filename(new_data)
        else:
            filename = record.get("filename") or "document.pdf"
        old_filename = record.get("filename")
        pdf_path = GENERATED_DIR / filename

        _effective_package_rates = package_rates or ai_package_rates
        if doc_type == 'package' and _effective_package_rates:
            _cost_labels={'per_adult':'Adult','per_child':'Child','per_child_cwb':'CWB','per_child_cnb':'CNB','per_extra_bed':'EB'}
            _cost_note=', '.join(f"{_cost_labels.get(k,k)} ₹{float(v):,.0f}" for k,v in _effective_package_rates.items())
            _regen_text=f"💰 *Customer costing understood:* {_cost_note}\n\n████████░░░░░░░ 55%\n\n📄 Regenerating Tour..."
        else:
            _regen_text=f"✏️ *Changes understood.*\n\n████████░░░░░░░ 55%\n\n📄 Regenerating {doc_type.replace('_',' ').title()}..."
        await safe_status_edit(status, update.message, _regen_text, parse_mode="Markdown")

        logo_path = LOGO_PATH if controls["logo"] and LOGO_PATH.exists() else None
        chosen_size = _normalize_page_size(controls["page_size"]) or "auto"

        if doc_type == "package":
            edit_b2b = bool(record.get('b2b'))
            if edit_b2b:
                new_data = _apply_tour_document_mode_fields(new_data, record.get('document_mode') or new_data.get('document_mode') or 'itinerary', b2b=True)
                logo_path = None
                controls['footer_mode'] = 'none'
                controls['footer'] = False
                controls['logo'] = False
            base_pdf = GENERATED_DIR / f"_edit_base_{reference}.pdf"
            chosen_size = chosen_size if chosen_size != "auto" else "A4"
            await asyncio.to_thread(generate_pdf, new_data, base_pdf, logo_path, chosen_size, text_scale_override=current_text_scale, logo_scale_override=current_logo_scale)
            combined_pdf = GENERATED_DIR / f"_edit_combined_{reference}.pdf"
            terms_choice = 'b2b' if edit_b2b else (record.get('terms_choice') or get_tour_last_page())
            await asyncio.to_thread(append_selected_terms, base_pdf, terms_choice, combined_pdf)
            base_pdf.unlink(missing_ok=True)
            base_pdf = combined_pdf
            wm_path = GENERATED_DIR / f"_edit_wm_{reference}.pdf"
            ws = load_settings()
            await asyncio.to_thread(add_watermark_to_pdf, base_pdf, wm_path, ws['buttons'].get('watermark', True) and not edit_b2b, ws.get('watermark_opacity', 0.04), ws.get('watermark_scale', 1.0))
            base_pdf.unlink(missing_ok=True)
            if edit_b2b:
                shutil.copyfile(wm_path, pdf_path)
            else:
                await asyncio.to_thread(_apply_footer_mode, wm_path, pdf_path, controls.get('footer_mode') or _default_footer_mode('package'))
            wm_path.unlink(missing_ok=True)
        else:
            base_pdf = GENERATED_DIR / f"_edit_base_{reference}.pdf"
            chosen_size = await asyncio.to_thread(
                _generate_adaptive_ticket, doc_type, new_data, new_fare, base_pdf, logo_path, chosen_size, text_scale_override=current_text_scale, logo_scale_override=current_logo_scale
            )
            wm_path = GENERATED_DIR / f"_edit_wm_{reference}.pdf"
            ws = load_settings()
            await asyncio.to_thread(add_watermark_to_pdf, base_pdf, wm_path, ws['buttons'].get('watermark', True), ws.get('watermark_opacity', 0.04), ws.get('watermark_scale', 1.0))
            base_pdf.unlink(missing_ok=True); base_pdf = wm_path
            if controls["footer_mode"] == "bar":
                await asyncio.to_thread(add_contact_bar_to_pdf, base_pdf, pdf_path)
            elif controls["footer_mode"] == "footer2":
                await asyncio.to_thread(add_footer2_to_pdf, base_pdf, pdf_path)
            elif controls["footer"]:
                await asyncio.to_thread(add_footer_to_pdf, base_pdf, pdf_path)
            else:
                shutil.copyfile(base_pdf, pdf_path)
            base_pdf.unlink(missing_ok=True)

        record.update({
            "filename": pdf_path.name,
            "data": new_data,
            "fare": new_fare,
            "footer": True,
            "footer_mode": controls.get("footer_mode") or _default_footer_mode(doc_type),
            "logo_enabled": bool(controls["logo"]),
            "page_size": chosen_size,
            "text_scale": current_text_scale,
            "logo_scale": current_logo_scale,
            "terms_choice": record.get("terms_choice") if doc_type == "package" else record.get("terms_choice"),
            "legacy": False,
        })
        update_record(reference, record)
        if old_filename and old_filename != pdf_path.name:
            old_path = GENERATED_DIR / old_filename
            if old_path.exists(): old_path.unlink(missing_ok=True)

        await safe_status_edit(status, update.message,
            f"✅ *Document updated successfully.*\n\n████████████████ 100%\n\n📐 Page size: {chosen_size}\n🖼️ Logo: {'ON' if controls['logo'] else 'OFF'}" + (f"\n📌 Footer: {'ON' if controls['footer'] else 'OFF'}" if doc_type != 'package' else ""),
            parse_mode="Markdown"
        )
        # Always tell the owner what the AI actually changed. This is especially useful
        # when the edit was made by replying directly to a Telegram message.
        if doc_type == 'package':
            notes = _draft_change_notes(old_data, new_data)
        else:
            notes = [f"• Updated {doc_type.replace('_',' ')} according to your instruction: {_value_preview(instruction, 180)}"]
        await update.message.reply_text('📝 *Noted — changes made:*\n' + '\n'.join(notes), parse_mode='Markdown')
        with open(pdf_path, "rb") as fh:
            sent_pdf = await update.message.reply_document(
                document=fh,
                filename=pdf_path.name,
                caption=_record_caption(reference, (f"📄 Updated B2B {doc_type.replace('package','tour').replace('_',' ').title()}" if doc_type == 'package' and record.get('b2b') else f"📄 Updated MyTourBazar {doc_type.replace('package','tour').replace('_',' ').title()}"), f"Page size: {chosen_size}"),
                reply_markup=generated_document_keyboard(reference, doc_type),
            )
        _register_reference_message(reference, sent_pdf)
        if doc_type == "package" and requested_detail and package_whatsapp:
            await update.message.reply_text(build_whatsapp_itinerary(new_data, requested_detail), parse_mode="Markdown")
        context.user_data.pop("editing_reference", None)
    except Exception as exc:
        logger.exception("Saved document edit failed")
        await safe_status_edit(status, update.message, f"❌ *Update failed*\n\nReason: `{str(exc)[:900]}`", parse_mode="Markdown")

async def receive_voice_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Natural voice-note editing for saved documents, especially Tour PDFs.

    A selected saved-document edit ALWAYS wins over Auto Creation intake. This is
    important after an Auto-Created Tour is printed: tapping Modify & Regenerate
    must route the next voice note to that saved PDF, never back into batch intake.
    """
    msg=update.message
    if not msg or not msg.voice:
        return

    # V167: Telegram Reply itself is an edit selector. Replying with a voice note
    # to a generated PDF edits that exact saved document; replying to a Tour draft
    # edits the current draft. No Modify button is required first.
    replied=getattr(msg,'reply_to_message',None)
    reply_reference=find_reference_for_reply(replied) if replied else ''
    draft_ids={int(x) for x in (context.user_data.get('tour_draft_message_ids') or []) if str(x).isdigit()}
    reply_is_draft=bool(replied and getattr(replied,'message_id',None) in draft_ids and context.user_data.get('itinerary'))

    if reply_is_draft:
        tg_file=await context.bot.get_file(msg.voice.file_id)
        path=TEMP_DIR/f"voice_draft_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.ogg"
        status=await msg.reply_text('🎙️ *Listening to your draft changes...*', parse_mode='Markdown')
        try:
            await tg_file.download_to_drive(path)
            transcript=await _run_ai_with_retry_status(
                msg,
                lambda: asyncio.to_thread(transcribe_voice_note, path, GEMINI_API_KEY, GEMINI_MODEL, msg.voice.mime_type or 'audio/ogg'),
                status=status,
            )
            await safe_status_edit(status,msg,'✅ *Voice note understood.* Applying it to this draft...',parse_mode='Markdown')
            await msg.reply_text('🎙️ *I understood:*\n' + transcript, parse_mode='Markdown')
            variant = _tour_reply_variant_command(transcript)
            if variant:
                await _reply_draft_tour_variant(msg, context, variant)
            else:
                await perform_draft_edit(update, context, transcript)
        except Exception as exc:
            logger.exception('Voice draft reply edit failed')
            await safe_status_edit(status,msg,f'⚠️ Voice draft edit could not be completed. Resend it or type the same change.\n\nReason: {str(exc)[:500]}')
        finally:
            try: path.unlink(missing_ok=True)
            except Exception: pass
        return

    # Highest-priority route: an explicit Modify/Voice-Text edit target, or a
    # Telegram Reply to a previously generated PDF.
    reference=reply_reference or context.user_data.get('editing_reference') or context.user_data.get('voice_edit_reference')
    if reference and load_record(reference):
        tg_file=await context.bot.get_file(msg.voice.file_id)
        path=TEMP_DIR/f"voice_edit_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.ogg"
        status=await msg.reply_text('🎙️ *Listening to your changes...*\n\nI will turn this voice note into the edit instruction and regenerate the same PDF.', parse_mode='Markdown')
        try:
            await tg_file.download_to_drive(path)
            transcript=await _run_ai_with_retry_status(
                msg,
                lambda: asyncio.to_thread(transcribe_voice_note, path, GEMINI_API_KEY, GEMINI_MODEL, msg.voice.mime_type or 'audio/ogg'),
                status=status,
            )
            await safe_status_edit(status,msg,'✅ *Voice note understood.*\n\nApplying the changes now...',parse_mode='Markdown')
            await msg.reply_text('🎙️ *I understood:*\n' + transcript, parse_mode='Markdown')
            saved = load_record(reference) or {}
            variant = _tour_reply_variant_command(transcript) if saved.get('type') == 'package' else None
            if variant:
                context.user_data.pop('editing_reference', None)
                await _reply_saved_tour_variant(msg, context, reference, saved, variant)
            else:
                context.user_data['editing_reference']=reference
                await perform_saved_edit(update, context, transcript)
        except Exception as exc:
            logger.exception('Voice edit failed')
            context.user_data['editing_reference']=reference
            await safe_status_edit(status,msg,f'⚠️ Voice edit could not be completed. You can resend the voice note or type the same change; /start is not required.\n\nReason: {str(exc)[:500]}')
        finally:
            try: path.unlink(missing_ok=True)
            except Exception: pass
        return

    # No saved edit target: when Auto Creation intake is active, add this voice
    # note to the current mixed-source batch.
    if context.user_data.get('auto_creation'):
        tg_file=await context.bot.get_file(msg.voice.file_id)
        path=TEMP_DIR/f"voice_auto_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.ogg"
        status=await msg.reply_text('🎙️ *Auto Creation voice note received.*\n\nUnderstanding your tour instructions...', parse_mode='Markdown')
        try:
            await tg_file.download_to_drive(path)
            transcript=await _run_ai_with_retry_status(
                msg,
                lambda: asyncio.to_thread(transcribe_voice_note, path, GEMINI_API_KEY, GEMINI_MODEL, msg.voice.mime_type or 'audio/ogg'),
                status=status,
            )
            context.user_data['smart_text']=(context.user_data.get('smart_text','')+'\n'+transcript).strip()
            await safe_status_edit(status,msg,'✅ *Voice note understood.*\n\n'+transcript,parse_mode='Markdown')
            ack=await _source_ack_message(update, context, 'Send another file/text/voice within 5 seconds if needed; otherwise I will combine the Auto Creation batch automatically.', reply_markup=auto_creation_keyboard())
            _schedule_source_auto_process(update,context,'auto_creation',lambda: smart_process(_SyntheticUpdate(_BotMessageProxy(context.bot,update.effective_chat.id),update.effective_user.id),context),prompt_message=ack)
        except Exception as exc:
            logger.exception('Auto Creation voice note failed')
            await safe_status_edit(status,msg,f'⚠️ Could not understand the voice note. You can resend it or type the same instruction.\n\nReason: {str(exc)[:500]}')
        finally:
            try: path.unlink(missing_ok=True)
            except Exception: pass
        return

    # V168: when AI Assistant is active, a normal voice note is a free-form
    # create/edit request. No prefix and no separate voice button is required.
    if context.user_data.get("smart_mode"):
        return await smart_voice(update, context)

    await msg.reply_text('🎙️ Voice editing is ready after you tap *Modify & Regenerate* on a generated PDF, or press *🤖 AI Assistant / New Request* and speak naturally.', parse_mode='Markdown', reply_markup=main_keyboard())
    return
def voucher_keyboard():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)


def media_keyboard():
    return ReplyKeyboardMarkup(
        [["➕ Add Another Page", "✅ Done"]],
        resize_keyboard=True
    )

def source_keyboard():
    return ReplyKeyboardMarkup(
        [["📄 Send PDF / 📝 Text"], ["📸 Send Screenshot"], ["✈️ Flight Screenshot", "✍️ Flight Text"], ["✅ Done"]],
        resize_keyboard=True
    )


async def _render_ticket_with_automatic_fit(kind, data, fare, logo_path, footer_mode, clean=False, requested_size="auto", logo_scale_override=None):
    """Fast Air/Bus/Hotel renderer.

    V127 removes the old A4/Legal/Letter x five-scale trial loop. Normal printing now
    renders only once. If an automatic A4 output is one page before the footer but the
    footer alone pushes it to page 2, the bot performs ONE quick A4 retry at 90% text.
    This keeps Auto Fit protection without making every print wait through many renders.
    """
    if kind not in ("flight", "bus", "hotel"):
        raise RuntimeError("Unsupported document type:")
    filename={"flight":_flight_filename,"bus":_bus_filename,"hotel":_hotel_filename}[kind](data)
    final=GENERATED_DIR / filename
    ws=load_settings()
    watermark_enabled=bool(ws.get('buttons',{}).get('watermark',True)) and not clean
    watermark_opacity=float(ws.get('watermark_opacity',0.04))
    watermark_scale=float(ws.get('watermark_scale',1.5))
    explicit=_normalize_page_size(requested_size)
    paper=explicit if explicit and explicit!='auto' else 'A4'

    async def render_once(scale=None, suffix='fast'):
        base=GENERATED_DIR / f"_{suffix}_{kind}_{filename}_base.pdf"
        wm=GENERATED_DIR / f"_{suffix}_{kind}_{filename}_wm.pdf"
        candidate=GENERATED_DIR / f"_{suffix}_{kind}_{filename}_final.pdf"
        try:
            await asyncio.to_thread(_generate_ticket_base,kind,data,fare,base,logo_path,paper,
                                    text_scale_override=scale,logo_scale_override=logo_scale_override)
            base_pages=_pdf_page_count(base)
            await asyncio.to_thread(add_watermark_to_pdf,base,wm,watermark_enabled,watermark_opacity,watermark_scale)
            if footer_mode=='bar': await asyncio.to_thread(add_contact_bar_to_pdf,wm,candidate)
            elif footer_mode=='design': await asyncio.to_thread(add_footer_to_pdf,wm,candidate)
            elif footer_mode=='footer2': await asyncio.to_thread(add_footer2_to_pdf,wm,candidate)
            else: shutil.copyfile(wm,candidate)
            final_pages=_pdf_page_count(candidate)
            shutil.move(str(candidate),str(final))
            return base_pages,final_pages
        finally:
            base.unlink(missing_ok=True); wm.unlink(missing_ok=True); candidate.unlink(missing_ok=True)

    base_pages,final_pages=await render_once(None,'fast1')
    selected_scale=None
    # At most ONE fit retry. No page-size scanning and no repeated 95/90/85/80/75 loops.
    if ((not explicit or explicit=='auto') and paper=='A4' and base_pages==1 and final_pages>1
            and footer_mode!='none' and not clean):
        _,final_pages2=await render_once(0.90,'fast2')
        selected_scale=0.90
    return final,paper,selected_scale


async def _print_ticket_final(message, context, kind, footer_mode="none", clean=False, add_footer=False):
    """Generate Air/Bus/Hotel with automatic footer-aware fitting.

    The automatic fit is part of normal generation, not a button-only feature.
    Modify & Regenerate keeps the reliable page-size/footer/content controls.
    Font/Logo +/- buttons are intentionally not shown; Auto Size remains available.
    """
    if kind not in ("flight", "bus", "hotel"):
        raise RuntimeError("Unsupported document type.")
    data_key = {"flight":"pending_flight_data", "bus":"pending_bus_data", "hotel":"pending_hotel_data"}[kind]
    fare_key = {"flight":"pending_flight_fare", "bus":"pending_bus_fare", "hotel":"pending_hotel_fare"}[kind]
    data = context.user_data.get(data_key)
    if not data:
        raise RuntimeError(f"No {kind} data is available.")
    fare = context.user_data.get(fare_key)
    page_size = context.user_data.get("pending_page_size", "auto") or "auto"
    logo_enabled = False if clean else context.user_data.get("pending_logo_enabled", True)
    logo_path = LOGO_PATH if logo_enabled and LOGO_PATH.exists() else None
    if footer_mode in (None, ""):
        footer_mode = _default_footer_mode(kind)
    if add_footer and footer_mode == "none":
        footer_mode = _default_footer_mode(kind)
    if clean:
        footer_mode = "none"

    filename = {"flight": _flight_filename, "bus": _bus_filename, "hotel": _hotel_filename}[kind](data)
    pdf_path = GENERATED_DIR / filename
    logo_scale_override = context.user_data.get("pending_logo_scale")

    pdf_path, chosen_size, selected_scale = await _render_ticket_with_automatic_fit(
        kind, data, fare, logo_path, footer_mode, clean=clean,
        requested_size=page_size, logo_scale_override=logo_scale_override
    )

    # Reopen the final PDF only after footer placement so the reference record reflects
    # the actual output settings.
    if kind == "flight":
        # The Air PDF now carries the complete line-by-line payment breakdown.
        # Keep the Telegram caption clean instead of reducing it back to Base/Taxes.
        caption_detail = (f"Updated Total Fare: INR {fare:,.0f}" if fare else "")
    elif kind == "bus":
        base, tax = (0, 0)
        if fare:
            try:
                source_base = float(data.get("base_fare", 0) or 0)
                source_tax = float(data.get("taxes", 0) or 0)
                total_source = source_base + source_tax
                if total_source > 0:
                    base = round(float(fare) * source_base / total_source)
                    tax = round(float(fare) - base)
            except Exception:
                pass
        caption_detail = (f"Updated Fare: INR {fare:,.0f} | Base: INR {base:,.0f} | Taxes: INR {tax:,.0f}" if fare else "")
    else:
        caption_detail = (f"Hotel Total: INR {fare:,.0f}" if fare else "")

    reference = create_reference()
    save_record(reference, {
        "type": kind,
        "filename": pdf_path.name,
        "data": data,
        "fare": fare,
        "footer": footer_mode != "none",
        "footer_mode": footer_mode,
        "logo_enabled": bool(logo_enabled),
        "logo_scale": logo_scale_override,
        "page_size": chosen_size,
        "text_scale": selected_scale,
    })
    with open(pdf_path, "rb") as fh:
        sent_pdf = await message.reply_document(
            fh,
            filename=pdf_path.name,
            caption=_record_caption(
                reference,
                f"{'✈️' if kind=='flight' else '🚌' if kind=='bus' else '🏨'} MyTourBazar {kind.title()}",
                ((caption_detail + "\n") if caption_detail else "") + f"Page size: {chosen_size}"
            ),
            parse_mode='Markdown',
            reply_markup=generated_document_keyboard(reference, kind)
        )
    _register_reference_message(reference, sent_pdf)
    for key in (data_key, fare_key, "pending_fare_kind", "pending_fare_supplier_total", "pending_footer_kind", "pending_page_size", "pending_logo_enabled", "pending_logo_scale"):
        context.user_data.pop(key, None)
    await message.reply_text("✅ Ready for the next request.", reply_markup=ready_keyboard())

def _cancel_auto_print(context):
    task = context.user_data.pop("_auto_print_task", None)
    if task and not task.done():
        task.cancel()


def _default_footer_mode(kind):
    mode = get_default_footer('package' if kind == 'package' else kind)
    return mode if mode in ('bar','design','footer2') else 'footer2'


async def _safe_message_text_edit(message, text, reply_markup=None):
    """Strict single-message editor. Never falls back to sending another status message."""
    if message is None:
        return False
    bot = None
    try:
        bot = message.get_bot()
    except Exception:
        bot = getattr(message, "_bot", None)
    chat_id = getattr(message, "chat_id", None)
    message_id = getattr(message, "message_id", None)
    try:
        if bot is not None and chat_id is not None and message_id is not None:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id,
                text=str(text), reply_markup=reply_markup
            )
            return True
        if getattr(message, "text", None) or getattr(message, "caption", None):
            await message.edit_text(str(text), reply_markup=reply_markup)
            return True
    except BadRequest as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Single-message edit failed: %s", exc)
    except Exception as exc:
        logger.warning("Single-message edit failed: %s", exc)
    return False


async def _auto_print_after_countdown(message, context, kind, supplier_total, prompt_message):
    """After 5 seconds with no fare-button click, complete the print automatically.

    Existing supplier fare -> print the supplier/original fare.
    No supplier fare -> print without fare.
    Footer -> current default footer preference.
    """
    token = object()
    context.user_data["_auto_print_token"] = token
    try:
        for remaining in range(AUTO_PRINT_SECONDS, 0, -1):
            if context.user_data.get("_auto_print_token") is not token:
                return
            await _safe_message_text_edit(
                prompt_message,
                (
                    (f"💰 Supplier fare found: INR {supplier_total:,.0f}.\n\n" if supplier_total > 0 else "⚠️ No supplier fare was found.\n\n")
                    + f"Choose an option below.\n\n⏳ Auto-printing in {remaining}s if you do nothing."
                ),
                reply_markup=fare_missing_keyboard(kind, supplier_total),
            )
            await asyncio.sleep(1)
        if context.user_data.get("_auto_print_token") is not token:
            return
        context.user_data.pop("_auto_print_task", None)
        context.user_data.pop("_auto_print_token", None)
        fare_key = f"pending_{kind}_fare"
        context.user_data[fare_key] = supplier_total if supplier_total > 0 else None
        context.user_data["pending_fare_kind"] = None
        context.user_data["pending_footer_kind"] = kind
        footer_mode = _default_footer_mode(kind)
        await _safe_message_text_edit(
            prompt_message,
            "⏳ 5 seconds elapsed. Completing the print automatically...",
            reply_markup=None,
        )
        await _print_ticket_final(
            message, context, kind,
            footer_mode=footer_mode,
            clean=(footer_mode == "none"),
        )
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.exception("Automatic 5-second print failed")
        await _safe_message_text_edit(
            prompt_message,
            f"❌ *Automatic print failed.*\n\nReason: `{str(exc)[:800]}`",
            reply_markup=None,
        )
    finally:
        if context.user_data.get("_auto_print_token") is token:
            context.user_data.pop("_auto_print_token", None)
            context.user_data.pop("_auto_print_task", None)


async def send_fare_choice_with_countdown(message, context, kind, supplier_total, status_message=None):
    """Use the SAME status message for fare selection and the automatic print countdown."""
    _cancel_auto_print(context)
    context.user_data["pending_fare_supplier_total"] = float(supplier_total or 0)
    if status_message is None:
        status_message = context.user_data.get("_source_status_message")
    if status_message is None:
        status_message = await message.reply_text("Preparing fare options...")
    context.user_data["_source_status_message"] = status_message

    text = (
        f"💰 *Supplier fare found: INR {supplier_total:,.0f}.*\n\n"
        if supplier_total > 0 else
        "⚠️ *No supplier fare was found.*\n\n"
    ) + "Choose *Add Cost* to enter a final fare or choose a print option.\n\n" + f"⏳ Auto-printing in {AUTO_PRINT_SECONDS}s if you do nothing."
    await _safe_message_text_edit(status_message, text, reply_markup=fare_missing_keyboard(kind, supplier_total))
    context.user_data["_auto_print_task"] = context.application.create_task(
        _auto_print_after_countdown(message, context, kind, float(supplier_total or 0), status_message)
    )
    return status_message


def fare_missing_keyboard(kind, supplier_total=0):
    """Show only fare actions that are valid for the extracted supplier data.

    If no supplier fare exists, the customer must NOT be offered an
    "original fare" option. They can only add a final cost or print without fare.
    """
    first = []
    if button_enabled("add_cost"):
        first.append(InlineKeyboardButton("➕ Add Cost", callback_data=f"fare_add:{kind}"))
    if button_enabled("print_without_fare"):
        first.append(InlineKeyboardButton("🖨️ Print Without Fare", callback_data=f"fare_none:{kind}"))
    rows = [first] if first else []
    if float(supplier_total or 0) > 0 and button_enabled("print_original_fare"):
        rows.append([InlineKeyboardButton("💰 Print Original Fare", callback_data=f"fare_original:{kind}")])
    return InlineKeyboardMarkup(rows)

def footer_choice_keyboard():
    # Legacy compatibility; footer is now selected from /settings or post-generation Modify.
    return modify_footer_keyboard(None, None)


async def ask_footer_choice(message, context, kind):
    # V94: footer is a persistent per-service setting. No footer choice is requested before printing.
    context.user_data['pending_footer_kind'] = kind
    footer_mode = _default_footer_mode(kind)
    context.user_data.setdefault('pending_page_size', 'auto')
    context.user_data.setdefault('pending_logo_enabled', True)
    await message.reply_text(f'⏳ Generating {kind.title()} PDF with {footer_mode} footer...')
    await _print_ticket_final(message, context, kind, footer_mode=footer_mode, clean=False)


def confirmation_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Generate PDF", callback_data="generate"),
            InlineKeyboardButton("➕ Add Inclusion", callback_data="add_inclusion"),
        ],
        [
            InlineKeyboardButton("➕ Add Exclusion", callback_data="add_exclusion"),
            InlineKeyboardButton("✈️ Add Flight / Ticket", callback_data="add_flight"),
        ],
        [
            InlineKeyboardButton("✍️ Add Flight Details in Text", callback_data="add_flight_text"),
        ],
        [
            InlineKeyboardButton("🖨️ Generate Without Cost", callback_data="generate_no_cost"),
        ],
        [
            InlineKeyboardButton("🔄 Re-enter", callback_data="reenter"),
        ]
    ])



def ask_tour_terms_message(message, context, detail, no_cost=False):
    context.user_data['pending_tour_pdf_detail'] = detail
    context.user_data['pending_tour_pdf_no_cost'] = bool(no_cost)
    return message.reply_text('📜 The final Tour PDF uses the default MyTourBazar T&C page.', parse_mode='Markdown')


def tour_output_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 WhatsApp • Basic", callback_data="tour_output:whatsapp:basic"),
         InlineKeyboardButton("📱 WhatsApp • Detailed", callback_data="tour_output:whatsapp:detailed")],
        [InlineKeyboardButton("📄 PDF • Basic", callback_data="tour_output:pdf:basic"),
         InlineKeyboardButton("📄 PDF • Detailed", callback_data="tour_output:pdf:detailed")],
        [InlineKeyboardButton("✏️ Smart Edit Draft", callback_data="draft_edit")],
    ])


def tour_pdf_mode_keyboard(detail):
    detail = 'detailed' if str(detail).lower() == 'detailed' else 'basic'
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧾 Tour Quotation", callback_data=f"tour_output_mode:{detail}:quotation"),
         InlineKeyboardButton("🎫 Tour Voucher", callback_data=f"tour_output_mode:{detail}:voucher")],
        [InlineKeyboardButton("⬅️ Back to Draft Outputs", callback_data="draft_done")],
    ])


def pending_tour_name_keyboard():
    return ReplyKeyboardMarkup(
        [["⏭️ Print Without Name"], ["❌ Cancel"]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )


def tour_transit_choice_keyboard(has_transit=False):
    rows=[]
    if has_transit:
        rows.append([InlineKeyboardButton("✅ Use Detected Transit", callback_data="tour_transit:use")])
    rows.append([InlineKeyboardButton("✈️ Add / Replace Transit", callback_data="tour_transit:add")])
    rows.append([InlineKeyboardButton("⏭️ Skip • Done by Self", callback_data="tour_transit:skip")])
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="tour_transit:cancel")])
    return InlineKeyboardMarkup(rows)


def tour_transit_input_keyboard():
    return ReplyKeyboardMarkup([["✅ Done Transit"],["⏭️ Skip Transit"],["❌ Cancel"]],
                               resize_keyboard=True, one_time_keyboard=False)


async def _continue_tour_pdf_after_transit(message, context):
    data=context.user_data.get("itinerary") or {}
    if not str(data.get("client_name") or "").strip():
        context.user_data["awaiting_tour_print_name"]=True
        await message.reply_text(
            "👤 *Guest / Client Name is blank.*\n\n"
            "Type the name now and I will add it immediately before printing.\n\n"
            "Or tap *⏭️ Print Without Name* to continue with a generic Guest heading.",
            parse_mode="Markdown", reply_markup=pending_tour_name_keyboard())
        return
    await _finish_pending_tour_pdf(message, context)


async def _process_pending_tour_transit(message, context):
    files=list(context.user_data.get("pending_tour_transit_files") or [])
    text=str(context.user_data.get("pending_tour_transit_text") or "").strip()
    if not files and not text:
        await message.reply_text("❌ Send at least one flight PDF/screenshot/text, or tap Skip Transit.",
                                 reply_markup=tour_transit_input_keyboard())
        return
    parts=[{"path":str(x),"mime_type":"application/pdf" if str(x).lower().endswith(".pdf") else "image/jpeg"} for x in files]
    status=await message.reply_text("✈️ *Reading all transit files...*\n\nCombining PDFs, screenshots and text and detecting all sectors.",
                                    parse_mode="Markdown")
    try:
        result=await _run_with_progress(status, message, lambda: asyncio.to_thread(extract_transit_from_parts, parts, text, GEMINI_API_KEY, GEMINI_MODEL), ['✈️ Reading transit sectors and terminals...','🔎 Checking connecting flight details...'], 25, 92)
        rows=result.get("transit") or []
        data=context.user_data.get("itinerary") or {}
        if rows:
            data["transit"]=rows; data["transit_done_by_self"]=False
            await safe_status_edit(status,message,f"✅ *Transit recognized.*\n\n{len(rows)} sector(s) added. Terminals and aircraft are preserved whenever supplied.",parse_mode="Markdown")
        else:
            data["transit"]=[]; data["transit_done_by_self"]=True
            await safe_status_edit(status,message,"ℹ️ No confirmed transit found. The box will show *Done by Self*.",parse_mode="Markdown")
        context.user_data["itinerary"]=data
        for k in ("awaiting_tour_transit_input","pending_tour_transit_files","pending_tour_transit_text"):
            context.user_data.pop(k,None)
        await _continue_tour_pdf_after_transit(message,context)
    except Exception as exc:
        logger.exception("Tour transit extraction failed")
        await safe_status_edit(status,message,f"❌ *Transit extraction failed*\n\nReason: `{str(exc)[:700]}`",parse_mode="Markdown")
        await message.reply_text("Send another transit source or tap Skip Transit.",reply_markup=tour_transit_input_keyboard())


class _BotMessageProxy:
    """Small Message-like adapter used by delayed auto-processing tasks."""
    def __init__(self, bot, chat_id):
        self._bot = bot
        self.chat_id = chat_id

    def get_bot(self):
        return self._bot

    async def reply_text(self, text, **kwargs):
        return await self._bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def reply_document(self, document, **kwargs):
        return await self._bot.send_document(chat_id=self.chat_id, document=document, **kwargs)


class _SyntheticUpdate:
    def __init__(self, message, user_id):
        self.message = message
        self.effective_user = type("User", (), {"id": user_id})()


async def _source_ack_message(update, context, text, reply_markup=None):
    """Create/maintain the ONE editable source-progress message.

    IMPORTANT: this message must NEVER carry a normal Telegram ReplyKeyboardMarkup.
    Telegram only allows editMessageText on messages with no reply markup or with
    an inline keyboard. The service/action keyboard remains the chat's separate
    reply keyboard, so the status message itself stays editable for the entire
    countdown/extraction/generation workflow.
    """
    existing = context.user_data.get('_source_status_message')
    bot = update.message.get_bot()

    if existing is not None:
        try:
            await bot.edit_message_text(
                chat_id=existing.chat_id,
                message_id=existing.message_id,
                text=text,
                parse_mode='Markdown',
                reply_markup=None,
            )
            return existing
        except Exception as exc:
            logger.warning(
                "Existing source status is not editable; creating a clean status message %s/%s: %s",
                getattr(existing, 'chat_id', None),
                getattr(existing, 'message_id', None),
                exc,
            )

    # NEVER attach reply_markup here.  A ReplyKeyboardMarkup makes the message
    # non-editable and was the root cause of the repeated 400 errors in V99.
    msg = await update.message.reply_text(text, parse_mode='Markdown')
    context.user_data['_source_status_message'] = msg
    return msg

def _cancel_source_auto_process(context):
    task = context.user_data.pop("_source_auto_task", None)
    if task and not task.done():
        task.cancel()


async def _source_countdown(prompt_message, context, workflow, process_callback):
    """Show a visible 5→1 countdown on a bot-owned message, then process."""
    token = object()
    context.user_data["_source_auto_token"] = token
    try:
        for remaining in range(AUTO_PRINT_SECONDS, 0, -1):
            if context.user_data.get("_source_auto_token") is not token:
                return
            if workflow in ("flight", "bus", "hotel", "direct_smart", "auto_creation"):
                text = (
                    "⏳ *Auto-processing in " + str(remaining) + "s...*\n\n"
                    "Send another page/source to reset the timer. Otherwise I will process automatically."
                )
            else:
                text = (
                    "⏳ *Auto-processing in " + str(remaining) + "s...*\n\n"
                    "Send another source to reset the timer, or tap *✅ Done* to process now."
                )
            await _safe_message_text_edit(prompt_message, text, reply_markup=None)
            await asyncio.sleep(1)

        if context.user_data.get("_source_auto_token") is not token:
            return
        context.user_data.pop("_source_auto_token", None)
        context.user_data.pop("_source_auto_task", None)
        context.user_data["_source_auto_processed"] = workflow
        await _safe_message_text_edit(prompt_message, "✅ *5 seconds elapsed — processing now...*", reply_markup=None)
        await process_callback()
    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Automatic source processing failed for %s", workflow)
    finally:
        if context.user_data.get("_source_auto_token") is token:
            context.user_data.pop("_source_auto_token", None)
            context.user_data.pop("_source_auto_task", None)


def _schedule_source_auto_process(update, context, workflow, process_callback, prompt_message=None):
    _cancel_source_auto_process(context)
    context.user_data['_source_auto_processed'] = None
    if prompt_message is not None:
        context.user_data['_source_status_message'] = prompt_message

    async def _runner():
        try:
            msg = context.user_data.get('_source_status_message') or prompt_message
            if msg is None:
                msg = await update.message.reply_text('⏳ Auto-processing in 5s...')
                context.user_data['_source_status_message'] = msg
            await _source_countdown(msg, context, workflow, process_callback)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception('Could not start source countdown for %s', workflow)
    context.user_data['_source_auto_task'] = context.application.create_task(_runner())


def bus_ticket_keyboard():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

async def bus_ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    await update.message.reply_text(
        "🚌 *Send the bus supplier itinerary now.*\n\n"
        "Drop a PDF, image or paste text. I will process it automatically after 5 seconds. "
        "If the booking has more pages, send them within the countdown and I will include them together.\n\n"
        "Tap *❌ Cancel* only if you want to stop.",
        parse_mode="Markdown", reply_markup=bus_ticket_keyboard()
    )
    return BUS_TICKET_INPUT

async def process_bus_ticket(update, context):
    if context.user_data.get('_source_processing') == 'bus':
        return ConversationHandler.END if 'bus' != 'tour' else None
    context.user_data['_source_processing'] = 'bus'
    _cancel_source_auto_process(context)
    files=context.user_data.get('bus_ticket_files',[]); txt=context.user_data.get('bus_ticket_text','')
    if not files and not txt:
        await update.message.reply_text('Please send a bus PDF, image or text. Tap ❌ Cancel to stop.', reply_markup=bus_ticket_keyboard()); return BUS_TICKET_INPUT
    status=await update.message.reply_text('🚌 *Reading bus booking documents...*\n\n████░░░░░░░░░░░ 25%\n\n🔍 Extracting passenger, PNR, route and fare details...',parse_mode='Markdown',reply_markup=ReplyKeyboardRemove())
    context.user_data['_source_status_message']=status
    try:
        parts=[{'path':p,'mime_type':'application/pdf' if p.lower().endswith('.pdf') else 'image/jpeg'} for p in files]
        data=await _run_with_progress(status, update.message, lambda: asyncio.to_thread(extract_bus_ticket,parts,txt,GEMINI_API_KEY,GEMINI_MODEL), ['🚌 Reading bus booking pages...','🔍 Extracting passenger, PNR, route and fare...'], 25, 92)
        context.user_data['pending_bus_data']=data
        supplier_total=_supplier_total(data)
        context.user_data['pending_bus_fare']=None
        context.user_data['pending_fare_supplier_total']=supplier_total
        await safe_status_edit(status, update.message, '🚌 *Bus details extracted.*\n\n████████████████ 100%\n\n💰 Choose the fare option before I generate the ticket.',parse_mode='Markdown')
        context.user_data['_source_processing'] = None
        await send_fare_choice_with_countdown(update.message, context, 'bus', supplier_total, status_message=status)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception('Bus ticket extraction failed')
        context.user_data['_source_processing'] = None
        await safe_status_edit(status, update.message, f'❌ Bus ticket creation failed: {str(exc)[:700]}',parse_mode='Markdown')
        return ConversationHandler.END

async def bus_ticket_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text=(update.message.text or '').strip()
    if text == "✈️ Air Print":
        return await flight_ticket_start(update, context)
    if text == "🚌 Bus Print":
        return await bus_ticket_start(update, context)
    if text == "🏨 Hotel Print":
        return await hotel_voucher_start(update, context)
    if text in ("🗺️ Tour Itinerary", "🗺️ Tour Guide"):
        return await new_itinerary(update, context)
    if text in ("🤖 AI Assistant", "🤖 AI Assistant / New Request"):
        return await smart_ai_start(update, context)
    if text == '❌ Cancel': return await cancel(update,context)
    if text == '📄 Send Bus PDF':
        await update.message.reply_text('📄 Send the bus booking confirmation PDF.', reply_markup=bus_ticket_keyboard()); return BUS_TICKET_INPUT
    if text == '📸 Send Bus Screenshot':
        await update.message.reply_text('📸 Send one or more bus booking screenshots.', reply_markup=bus_ticket_keyboard()); return BUS_TICKET_INPUT
    if text == '✍️ Send Bus Text':
        await update.message.reply_text('✍️ Paste the bus booking details. You can send multiple messages.', reply_markup=bus_ticket_keyboard()); return BUS_TICKET_INPUT
    if text == '✅ Done':
        _cancel_source_auto_process(context)
        if context.user_data.get('_source_auto_processed') == 'bus':
            return ConversationHandler.END
        return await process_bus_ticket(update, context)
    context.user_data['bus_ticket_text']=(context.user_data.get('bus_ticket_text','')+'\n'+text).strip()
    msg=await _source_ack_message(update, context, '📝 Bus booking text received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.', reply_markup=bus_ticket_keyboard())
    _schedule_source_auto_process(update, context, 'bus', lambda: process_bus_ticket(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return BUS_TICKET_INPUT

async def bus_ticket_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    photo=update.message.photo[-1]; f=await context.bot.get_file(photo.file_id)
    path=TEMP_DIR/f"bus_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"; await f.download_to_drive(path)
    context.user_data.setdefault('bus_ticket_files',[]).append(str(path))
    msg=await _source_ack_message(update, context, '📸 Bus booking screenshot received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.', reply_markup=bus_ticket_keyboard())
    _schedule_source_auto_process(update, context, 'bus', lambda: process_bus_ticket(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return BUS_TICKET_INPUT

async def bus_ticket_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    doc=update.message.document; mime=(doc.mime_type or '').lower(); name=doc.file_name or 'bus.pdf'
    if not (name.lower().endswith('.pdf') or mime=='application/pdf'):
        await update.message.reply_text('Please send the bus confirmation as PDF, screenshot, or text.', reply_markup=bus_ticket_keyboard()); return BUS_TICKET_INPUT
    f=await context.bot.get_file(doc.file_id); safe=''.join(c if c.isalnum() or c in '._-' else '_' for c in name); path=TEMP_DIR/f"bus_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S}_{safe}"; await f.download_to_drive(path)
    context.user_data.setdefault('bus_ticket_files',[]).append(str(path))
    msg=await _source_ack_message(update, context, '📄 Bus booking PDF received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.', reply_markup=bus_ticket_keyboard())
    _schedule_source_auto_process(update, context, 'bus', lambda: process_bus_ticket(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return BUS_TICKET_INPUT

async def bus_ticket_fare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    raw=(update.message.text or '').replace(',','').replace('INR','').replace('₹','').strip()
    try: fare=float(raw)
    except ValueError:
        await update.message.reply_text('❌ Please enter only the updated fare amount, e.g. `2500`.', parse_mode='Markdown'); return BUS_FARE_INPUT
    if fare<=0:
        await update.message.reply_text('❌ Fare must be greater than zero.'); return BUS_FARE_INPUT
    context.user_data['pending_bus_fare']=fare
    try:
        await ask_footer_choice(update.message, context, 'bus')
    except Exception as exc:
        logger.exception('Bus PDF generation failed')
        await update.message.reply_text(f'❌ PDF generation failed.\n\nReason: `{str(exc)[:800]}`', parse_mode='Markdown', reply_markup=main_keyboard())
    return ConversationHandler.END

def flight_ticket_keyboard():
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)

async def flight_ticket_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END
    # Flight tickets do not require a separate guest-name step.
    # Always cancel any stale auto-processing task BEFORE clearing state.
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    await update.message.reply_text(
        "✈️ *Send the flight supplier itinerary now.*\n\n"
        "Drop a PDF, image or paste text. I will process it automatically after 5 seconds. "
        "If the booking has more pages, send them within the countdown and I will include them together.\n\n"
        "Tap *❌ Cancel* only if you want to stop.",
        parse_mode="Markdown", reply_markup=flight_ticket_keyboard()
    )
    return FLIGHT_TICKET_INPUT

async def process_flight_ticket(update, context):
    if context.user_data.get('_source_processing') == 'flight':
        return ConversationHandler.END if 'flight' != 'tour' else None
    context.user_data['_source_processing'] = 'flight'
    _cancel_source_auto_process(context)
    files=context.user_data.get('flight_ticket_files',[]); txt=context.user_data.get('flight_ticket_text','')
    if not files and not txt:
        await update.message.reply_text('Please send a flight PDF, image or text. Tap ❌ Cancel to stop.', reply_markup=flight_ticket_keyboard()); return FLIGHT_TICKET_INPUT
    status=await update.message.reply_text('✈️ *Reading flight documents...*\n\n████░░░░░░░░░░░ 25%\n\n🔍 Extracting passenger, PNR, flight sectors and fare details...',parse_mode='Markdown',reply_markup=ReplyKeyboardRemove())
    context.user_data['_source_status_message']=status
    try:
        parts=[{'path':p,'mime_type':'application/pdf' if p.lower().endswith('.pdf') else 'image/jpeg'} for p in files]
        data=await _run_with_progress(status, update.message, lambda: asyncio.to_thread(extract_flight_ticket,parts,txt,GEMINI_API_KEY,GEMINI_MODEL), ['✈️ Reading flight booking pages...','🔍 Extracting passenger, PNR, sectors and fare...'], 25, 92)
        context.user_data['pending_flight_data']=data
        supplier_total=_supplier_total(data)
        context.user_data['pending_flight_fare']=None
        context.user_data['pending_fare_supplier_total']=supplier_total
        context.user_data['_source_processing'] = None
        await safe_status_edit(status, update.message, '✅ *Flight details extracted from supplier source.*\n\n████████████████ 100%\n\n🏢 Full airport wording preserved\n🚪 Terminal printed whenever supplied\n⏱️ Duration printed whenever supplied\n\n💰 Choose the fare option before I generate the ticket.',parse_mode='Markdown')
        await send_fare_choice_with_countdown(update.message, context, 'flight', supplier_total, status_message=status)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception('Flight ticket extraction failed')
        context.user_data['_source_processing'] = None
        await safe_status_edit(status, update.message, f'❌ Flight ticket creation failed: {str(exc)[:700]}',parse_mode='Markdown')
        return ConversationHandler.END

async def flight_ticket_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    text=(update.message.text or '').strip()
    if text == "✈️ Air Print":
        return await flight_ticket_start(update, context)
    if text == "🚌 Bus Print":
        return await bus_ticket_start(update, context)
    if text == "🏨 Hotel Print":
        return await hotel_voucher_start(update, context)
    if text in ("🗺️ Tour Itinerary", "🗺️ Tour Guide"):
        return await new_itinerary(update, context)
    if text in ("🤖 AI Assistant", "🤖 AI Assistant / New Request"):
        return await smart_ai_start(update, context)
    if text == '❌ Cancel': return await cancel(update,context)
    if text == '📄 Send Flight PDF':
        await update.message.reply_text('📄 Send the flight confirmation PDF.', reply_markup=flight_ticket_keyboard()); return FLIGHT_TICKET_INPUT
    if text == '📸 Send Flight Screenshot':
        await update.message.reply_text('📸 Send one or more flight screenshots.', reply_markup=flight_ticket_keyboard()); return FLIGHT_TICKET_INPUT
    if text == '✍️ Send Flight Text':
        await update.message.reply_text('✍️ Paste flight details. You can send multiple messages.', reply_markup=flight_ticket_keyboard()); return FLIGHT_TICKET_INPUT
    if text == '✅ Done':
        _cancel_source_auto_process(context)
        if context.user_data.get('_source_auto_processed') == 'flight':
            return ConversationHandler.END
        return await process_flight_ticket(update, context)
    context.user_data['flight_ticket_text']=(context.user_data.get('flight_ticket_text','')+'\n'+text).strip()
    msg=await _source_ack_message(update, context, '📝 Flight text received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.', reply_markup=flight_ticket_keyboard())
    _schedule_source_auto_process(update, context, 'flight', lambda: process_flight_ticket(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return FLIGHT_TICKET_INPUT

async def flight_ticket_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    photo=update.message.photo[-1]; f=await context.bot.get_file(photo.file_id)
    path=TEMP_DIR/f"flight_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"; await f.download_to_drive(path)
    context.user_data.setdefault('flight_ticket_files',[]).append(str(path))
    msg=await _source_ack_message(update, context, '📸 Flight screenshot received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.', reply_markup=flight_ticket_keyboard())
    _schedule_source_auto_process(update, context, 'flight', lambda: process_flight_ticket(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return FLIGHT_TICKET_INPUT

async def flight_ticket_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    doc=update.message.document; mime=(doc.mime_type or '').lower(); name=doc.file_name or 'flight.pdf'
    if not (name.lower().endswith('.pdf') or mime=='application/pdf'):
        await update.message.reply_text('Please send the flight confirmation as PDF, screenshot, or text.', reply_markup=flight_ticket_keyboard()); return FLIGHT_TICKET_INPUT
    f=await context.bot.get_file(doc.file_id); safe=''.join(c if c.isalnum() or c in '._-' else '_' for c in name); path=TEMP_DIR/f"flight_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S}_{safe}"; await f.download_to_drive(path)
    context.user_data.setdefault('flight_ticket_files',[]).append(str(path))
    msg=await _source_ack_message(update, context, '📄 Flight PDF received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.', reply_markup=flight_ticket_keyboard())
    _schedule_source_auto_process(update, context, 'flight', lambda: process_flight_ticket(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return FLIGHT_TICKET_INPUT


async def flight_ticket_fare(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    raw=(update.message.text or '').replace(',','').replace('INR','').replace('₹','').strip()
    try: fare=float(raw)
    except ValueError:
        await update.message.reply_text('❌ Please enter only the updated fare amount, e.g. `8500`.', parse_mode='Markdown'); return FLIGHT_FARE_INPUT
    if fare<=0:
        await update.message.reply_text('❌ Fare must be greater than zero.'); return FLIGHT_FARE_INPUT
    context.user_data['pending_flight_fare']=fare
    try:
        await ask_footer_choice(update.message, context, 'flight')
    except Exception as exc:
        logger.exception('Flight PDF generation failed')
        await update.message.reply_text(f'❌ PDF generation failed.\n\nReason: `{str(exc)[:800]}`', parse_mode='Markdown', reply_markup=main_keyboard())
    return ConversationHandler.END

async def hotel_voucher_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END

    # Cancel any stale workflow timer before resetting this service.
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    context.user_data["voucher_files"] = []
    context.user_data["voucher_text"] = ""
    await update.message.reply_text(
        "🏨 *Send the hotel supplier itinerary now.*\n\n"
        "Drop a PDF, image or paste text. I will automatically read the reservation, guest, hotel, room, occupancy, meal plan and terms. "
        "If there are multiple pages, send them within the 5-second countdown.\n\n"
        "Tap *❌ Cancel* only if you want to stop.",
        parse_mode="Markdown",
        reply_markup=voucher_keyboard(),
    )
    return HOTEL_VOUCHER_INPUT


async def hotel_voucher_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text=(update.message.text or "").strip()
    if text == "✈️ Air Print":
        return await flight_ticket_start(update, context)
    if text == "🚌 Bus Print":
        return await bus_ticket_start(update, context)
    if text == "🏨 Hotel Print":
        return await hotel_voucher_start(update, context)
    if text in ("🗺️ Tour Itinerary", "🗺️ Tour Guide"):
        return await new_itinerary(update, context)
    if text in ("🤖 AI Assistant", "🤖 AI Assistant / New Request"):
        return await smart_ai_start(update, context)
    if text == "❌ Cancel":
        return await cancel(update, context)
    if text == "📄 Send Hotel PDF":
        await update.message.reply_text("📄 Send the hotel confirmation PDF now.", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT
    if text == "📸 Send Hotel Screenshot":
        await update.message.reply_text("📸 Send the hotel confirmation screenshot now. You can send multiple pages.", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT
    if text == "✍️ Send Hotel Text":
        await update.message.reply_text("✍️ Paste the hotel confirmation details now. You can send multiple messages.", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT
    if text == "✅ Done":
        _cancel_source_auto_process(context)
        if context.user_data.get('_source_auto_processed') == 'hotel':
            return ConversationHandler.END
        await process_hotel_voucher(update, context)
        return ConversationHandler.END
    if len(text) > 50000:
        await update.message.reply_text("This text is too long for one Telegram message. Please send it in parts or as a PDF.", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT
    context.user_data["voucher_text"] = (context.user_data.get("voucher_text", "") + "\n" + text).strip()
    msg=await _source_ack_message(update, context, "📝 Hotel details received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.", reply_markup=voucher_keyboard())
    _schedule_source_auto_process(update, context, 'hotel', lambda: process_hotel_voucher(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
    return HOTEL_VOUCHER_INPUT


async def hotel_voucher_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    try:
        photo=update.message.photo[-1]
        tg_file=await context.bot.get_file(photo.file_id)
        filename=TEMP_DIR / f"voucher_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
        await tg_file.download_to_drive(filename)
        context.user_data.setdefault("voucher_files", []).append(str(filename))
        msg=await update.message.reply_text("📸 Hotel screenshot received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.", reply_markup=voucher_keyboard())
        _schedule_source_auto_process(update, context, 'hotel', lambda: process_hotel_voucher(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
        return HOTEL_VOUCHER_INPUT
    except Exception as exc:
        logger.exception("Hotel voucher photo failed")
        await update.message.reply_text(f"❌ Could not read the hotel screenshot: {exc}", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT


async def hotel_voucher_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    doc=update.message.document
    filename_lower=(doc.file_name or "").lower()
    mime=(doc.mime_type or "").lower()
    if not (filename_lower.endswith(".pdf") or mime == "application/pdf"):
        await update.message.reply_text("Please send the hotel confirmation as a PDF, screenshot, or text.", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT
    try:
        tg_file=await context.bot.get_file(doc.file_id)
        safe_name="".join(c if c.isalnum() or c in "._-" else "_" for c in (doc.file_name or "hotel_voucher.pdf"))
        path=TEMP_DIR / f"voucher_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
        await tg_file.download_to_drive(path)
        context.user_data.setdefault("voucher_files", []).append(str(path))
        msg=await _source_ack_message(update, context, "📄 Hotel PDF received. Send another page/source within 5 seconds if needed; otherwise I will process automatically.", reply_markup=voucher_keyboard())
        _schedule_source_auto_process(update, context, 'hotel', lambda: process_hotel_voucher(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
        return HOTEL_VOUCHER_INPUT
    except Exception as exc:
        logger.exception("Hotel voucher PDF failed")
        await update.message.reply_text(f"❌ Could not read the hotel PDF: {exc}", reply_markup=voucher_keyboard())
        return HOTEL_VOUCHER_INPUT


async def process_hotel_voucher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('_source_processing') == 'hotel':
        return ConversationHandler.END if 'hotel' != 'tour' else None
    context.user_data['_source_processing'] = 'hotel'
    _cancel_source_auto_process(context)
    if not GEMINI_API_KEY:
        await update.message.reply_text("❌ GEMINI_API_KEY is not configured in .env.", reply_markup=main_keyboard())
        return
    files=context.user_data.get("voucher_files", [])
    source_text=context.user_data.get("voucher_text", "")
    if not files and not source_text:
        await update.message.reply_text("Please send a hotel PDF, image or text. Tap ❌ Cancel to stop.", reply_markup=voucher_keyboard())
        return
    status=await update.message.reply_text("🏨 *Reading hotel confirmation...*\n\n░░░░░░░░░░░░░░░░ 0%", parse_mode="Markdown")
    context.user_data['_source_status_message']=status
    try:
        await safe_status_edit(status, update.message, "🏨 *Reading hotel confirmation...*\n\n████░░░░░░░░░░░ 25%\n\n🔍 Extracting booking details...", parse_mode="Markdown")
        parts=[]
        for f in files:
            mime="application/pdf" if f.lower().endswith(".pdf") else "image/jpeg"
            parts.append({"path":f,"mime_type":mime})
        data=await _run_with_progress(status, update.message, lambda: asyncio.to_thread(extract_hotel_voucher, parts, source_text, GEMINI_API_KEY, GEMINI_MODEL), ['🏨 Reading hotel confirmation pages...','🔍 Extracting guest, reservation, rooms and stay details...'], 25, 92)
        # The extraction is complete and structured data is now self-contained. Clear the
        # source list before fare/costing actions so a delayed callback can never try to
        # reopen a deleted incoming voucher file.
        for _src in list(files):
            try: Path(_src).unlink(missing_ok=True)
            except Exception: pass
        context.user_data['voucher_files']=[]
        await safe_status_edit(status, update.message, "🏨 *Hotel details extracted successfully.*\n\n████████████████ 100%\n\n💰 Choose the fare option before I generate the voucher.", parse_mode="Markdown")
        context.user_data['pending_hotel_data']=data
        context.user_data['pending_hotel_fare']=None
        context.user_data['pending_fare_supplier_total']=_supplier_total(data)
        context.user_data['_source_processing'] = None
        await send_fare_choice_with_countdown(update.message, context, 'hotel', context.user_data.get('pending_fare_supplier_total', 0), status_message=status)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception("Hotel voucher extraction failed")
        context.user_data['_source_processing'] = None
        await safe_status_edit(status, update.message, f"❌ *Hotel voucher creation failed*\n\nReason: `{str(exc)[:800]}`", parse_mode="Markdown")
        await update.message.reply_text("Please try 🏨 Hotel Print again.", reply_markup=main_keyboard())



def smart_source_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["✈️ Air Print", "🏨 Hotel Print"],
            ["🚌 Bus Print", "⚡ Process Now"],
            ["➕ Send Another", "🤖 AI Assistant / New Request"],
            ["❌ Cancel"],
        ],
        resize_keyboard=True,
    )

def direct_drop_keyboard():
    """Minimal keyboard for the /start drag-and-drop path."""
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)


def auto_creation_keyboard():
    """Auto Creation intentionally has no workflow buttons beyond Cancel."""
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True)


async def auto_creation_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start one mixed-source Tour batch: supplier package + tickets + notes/voice."""
    if not is_allowed(update):
        return ConversationHandler.END
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    context.user_data["smart_mode"] = True
    context.user_data["smart_force_kind"] = "package"
    context.user_data["auto_creation"] = True
    context.user_data["smart_files"] = []
    context.user_data["smart_text"] = ""
    await update.message.reply_text(
        "🤖 *AUTO CREATION • SMART TOUR BUILDER*\n\n"
        "Send everything related to *one client / one tour* together. You can mix:\n"
        "• Supplier Tour PDF / image / text\n"
        "• Flight ticket PDF / screenshot\n"
        "• Train ticket\n"
        "• Bus ticket\n"
        "• Hotel confirmation\n"
        "• Extra instructions as normal text or voice note\n\n"
        "I will match passenger names, travel dates, routes, hotels and transport, remove duplicate sectors, "
        "and build one proper *day-wise MyTourBazar Tour itinerary*.\n\n"
        "Send multiple items one after another. Processing starts automatically a few seconds after the last item.",
        parse_mode="Markdown",
        reply_markup=auto_creation_keyboard(),
    )
    return SMART_INPUT


def _smart_parts(context):
    parts = []
    for f in context.user_data.get("smart_files", []):
        mime = "application/pdf" if str(f).lower().endswith(".pdf") else "image/jpeg"
        parts.append({"path": str(f), "mime_type": mime})
    return parts


async def start_fresh(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    context.user_data["smart_mode"] = True
    context.user_data["smart_files"] = []
    context.user_data["smart_text"] = ""
    await update.message.reply_text(
        "🆕 *Fresh start*\n\n"
        "What do you want me to make or change? Tell me in your own words — short details are enough.\n\n"
        "Example: `Make a 4 night / 5 day Goa package for Mr. Amit, 2 adults, 3-star hotels, breakfast, private cab, North & South Goa sightseeing.`\n\n"
        "You can also send a supplier *PDF, screenshot or text* and I will recognize whether it is Tour, Air, Bus or Hotel automatically.\n"
        "✈️ No flight/train/bus mentioned = no transit will be added.\n"
        "🏨 No hotel name mentioned = no hotel name will be invented; only the requested category can be shown.",
        parse_mode="Markdown",
        reply_markup=smart_source_keyboard(),
    )
    return SMART_INPUT



async def smart_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    context.user_data["smart_mode"] = True
    context.user_data["smart_files"] = []
    context.user_data["smart_text"] = ""
    await update.message.reply_text(
        "🤖 *MyTourBazar AI Assistant • Quick Client Itinerary*\n\n"
        "For a quick client enquiry, just send the details naturally — no prefix is required. "
        "I will create the full day-wise itinerary myself and open the same Tour draft/output workflow.\n\n"
        "For example:\n"
        "• `Make a 4 night / 5 day Goa package for Mr. Amit, 2 adults, 3-star hotels, breakfast, private cab, North & South Goa sightseeing.`\n"
        "• `Make a 5 night / 6 day Kashmir family package, 4 adults, 3-star hotels, breakfast and private vehicle.`\n"
        "• `Edit MTB12 and change Day 3 sightseeing.`\n\n"
        "I will automatically understand the destination, duration, hotel category, meals, vehicle, sightseeing and other details.\n"
        "✈️ If you do not mention a flight/train/bus, I will NOT add one.\n"
        "🏨 If you do not give a hotel name, I will NOT invent one; I can show only the requested hotel category.\n\n"
        "📥 You can still drop supplier PDFs/screenshots/text when you actually want supplier extraction.\n\n"
        "🎙️ *Voice also works here:* after pressing AI Assistant, simply send a voice note and ask naturally — no prefix or command is required.\n\n"
        "Or use the quick Air / Hotel / Bus buttons below when you want to force a specific type.",
        parse_mode="Markdown",
        reply_markup=smart_source_keyboard(),
    )
    return SMART_INPUT


def _smart_mtb_edit_request(text):
    """Return (reference, instruction) for a natural MTB edit request, else ("","")."""
    raw = str(text or "").strip()
    ref_match = re.search(r"\bMTB\s*[-#]?\s*(\d+)\b", raw, re.I)
    if not ref_match:
        return "", ""
    has_edit = bool(re.search(
        r"\b(edit|modify|change|update|replace|remove|delete|add|revise|correct|fix)\b",
        raw, re.I
    ))
    if not has_edit:
        return "", ""
    return f"MTB{int(ref_match.group(1)):02d}", raw


def _looks_like_new_tour_brief(text):
    """Catch quick client Tour briefs with or without command words.

    Examples:
      Goa 4N 5D, Mr Amit, 2 adults, 3 star, breakfast, private cab
      Kashmir 5 nights 6 days, 4 adults, 3 star, breakfast
      Make a Goa package...
    """
    t = str(text or "").strip()
    low = t.lower()
    if not low:
        return False

    # A saved-reference edit is not a new quick itinerary.
    if _smart_mtb_edit_request(t)[0]:
        return False

    # Strong duration forms: 4N 5D / 4N/5D / 4 nights 5 days / 5 days.
    duration_pair = bool(re.search(
        r"\b\d+\s*(?:n|night|nights)\s*(?:[/+\-& ]+)?\s*\d+\s*(?:d|day|days)\b",
        low, re.I
    ))
    duration_single = bool(re.search(r"\b\d+\s*(?:d|day|days|n|night|nights)\b", low, re.I))
    duration = duration_pair or duration_single

    pax = bool(re.search(
        r"\b(?:\d+\s*(?:adult|adults|pax|person|persons|child|children|infant|infants)|couple|family)\b",
        low, re.I
    ))
    hotel = bool(re.search(
        r"\b[1-5]\s*(?:star|stars|\*)\b|\b(?:hotel|hotels|resort|resorts|accommodation)\b",
        low, re.I
    ))
    service = bool(re.search(
        r"\b(?:breakfast|dinner|lunch|meal|meals|cab|vehicle|car|transfer|transfers|"
        r"sightseeing|pickup|drop|private|shared|cp|map|mapai)\b",
        low, re.I
    ))
    create_word = bool(re.search(
        r"\b(?:make|create|prepare|plan|build|design|draft|itinerary|package|tour|holiday|trip|quotation|quote|voucher)\b",
        low, re.I
    ))

    # The quick format does not require "make/package/tour".
    # Duration + one more travel signal is enough.
    if duration and (pax or hotel or service):
        return True

    return bool(create_word and (duration or pax or hotel or service))


def _smart_requested_tour_mode(text):
    low = str(text or "").lower()
    if re.search(r"\b(?:tour\s+)?voucher\b", low):
        return "voucher"
    if re.search(r"\bquotation\b|\bquote\b", low):
        return "quotation"
    return ""


def _smart_requested_tour_detail(text):
    low = str(text or "").lower()
    if re.search(r"\b(detailed|detail|full[- ]?length|elaborate)\b", low):
        return "detailed"
    if re.search(r"\b(basic|short|brief)\b", low):
        return "basic"
    return "basic"


def _looks_like_supplier_material(text):
    """Return True when text looks like supplier/booking source material, not an owner brief.

    Text-only Smart Assistant input used to be treated as a NEW TOUR brief whenever the
    classifier returned package. That caused supplier tour text to be regenerated as a
    generic destination plan instead of being extracted. This detector keeps supplier
    text on the source-extraction path while preserving natural-language tour requests.
    """
    t = str(text or "").lower()
    if not t:
        return False
    supplier_markers = (
        "supplier", "booking confirmation", "confirmation number", "reservation number",
        "booking reference", "booking id", "pnr", "ticket number", "e-ticket", "eticket",
        "passenger name", "traveller name", "travel date", "departure", "arrival",
        "boarding", "terminal", "baggage", "fare", "base fare", "taxes",
        "hotel confirmation", "check-in", "check in", "check-out", "check out",
        "room type", "room category", "meal plan", "hotel contact", "property address",
        "bus operator", "boarding point", "dropping point", "drop point", "seat number",
        "service number", "day 1", "day 2", "day 3", "sightseeing", "inclusions",
        "exclusions", "accommodation schedule", "package cost", "per adult",
        "cwb", "cnb", "extra bed", "option 2"
    )
    hits = sum(1 for marker in supplier_markers if marker in t)
    # A reasonably long structured source is almost certainly supplier material.
    line_count = len([x for x in t.splitlines() if x.strip()])
    return hits >= 2 or line_count >= 12 or len(t) >= 900


async def smart_process(update, context):
    if not GEMINI_API_KEY:
        await update.message.reply_text("❌ GEMINI_API_KEY is not configured in .env.", reply_markup=main_keyboard())
        return ConversationHandler.END
    text = context.user_data.get("smart_text", "").strip()
    parts = _smart_parts(context)
    if not text and not parts:
        await update.message.reply_text("Send a PDF, image or text first.", reply_markup=direct_drop_keyboard() if context.user_data.get('_direct_drop_mode') else smart_source_keyboard())
        return SMART_INPUT
    # Reuse the single source/status message created when the supplier file/text was received.
    # Never create a second progress message if one already exists.
    status = context.user_data.get("_source_status_message")
    if status is None:
        status = await update.message.reply_text(
            "🤖 *Understanding your request...*\n\n████░░░░░░░░░░░ 25%\n\n⚡ Checking only the relevant booking pages and ignoring supplier terms/offers...",
            parse_mode="Markdown"
        )
        context.user_data["_source_status_message"] = status
    try:
        # Supplier material uses one fast source-classification call before service extraction.
        # Owner-only natural-language requests use the autonomous planner, so buttons are not the limit.
        supplier_text = bool(text and _looks_like_supplier_material(text))
        forced_kind = str(context.user_data.get("smart_force_kind") or "").lower()
        if forced_kind in ("flight", "bus", "hotel", "package"):
            result = {"kind": forced_kind, "confidence": 1.0, "reason": f"{forced_kind.title()} mode was selected manually.", "reference": "", "instruction": text}
        elif parts or supplier_text:
            result = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(ai_classify, parts, text, GEMINI_API_KEY, GEMINI_MODEL), ["🤖 AI is identifying the supplier document type...", "🔎 Reading the supplied material..."], 20, 48)
        else:
            # V168: deterministic no-prefix routing before Gemini planning.
            # This prevents natural Tour briefs such as "Goa 4N/5D..." from being
            # incorrectly redirected to Tour Guide, while MTB edits still win.
            direct_ref, direct_instruction = _smart_mtb_edit_request(text)
            if direct_ref:
                result = {
                    "kind": "edit", "confidence": 1.0,
                    "reason": "Existing MyTourBazar reference edit detected.",
                    "reference": direct_ref, "instruction": direct_instruction or text,
                }
            elif _looks_like_new_tour_brief(text):
                result = {
                    "kind": "package", "confidence": 1.0,
                    "reason": "New Tour itinerary/quotation brief detected.",
                    "reference": "", "instruction": text,
                }
            else:
                plan = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(agent_plan, text, text, GEMINI_API_KEY, GEMINI_MODEL), ["🧠 AI is understanding what you want...", "🧭 Selecting the correct MyTourBazar workflow..."], 10, 30)
                action = str(plan.get("action", "ask_user"))
                if action == "edit_document" and plan.get("reference"):
                    result = {"kind":"edit", "confidence":0.99, "reason":plan.get("reason","Existing document change requested."), "reference":plan.get("reference",""), "instruction":plan.get("instruction") or text}
                elif action == "generate_brief":
                    result = {"kind":"package", "confidence":0.99, "reason":plan.get("reason","New tour itinerary requested."), "reference":"", "instruction":plan.get("instruction") or text}
                elif action == "chat":
                    result = {"kind":"chat", "confidence":0.99, "reason":plan.get("reason","Normal assistant request."), "reference":"", "instruction":text}
                else:
                    result = {"kind":"unknown", "confidence":0.0, "reason":plan.get("needs_user_input") or "I need more information.", "reference":"", "instruction":text}
        kind = str(result.get("kind", "unknown")).lower()
        conf = float(result.get("confidence", 0) or 0)
        reason = result.get("reason", "")
        forced_kind = str(context.user_data.get("smart_force_kind") or "").lower()
        if forced_kind in ("flight", "bus", "hotel", "package"):
            kind = forced_kind
            conf = 1.0
            reason = f"{forced_kind.title()} mode was selected manually."
        ref = str(result.get("reference", "") or "").upper()
        instruction = str(result.get("instruction", "") or text).strip()
        await safe_status_edit(status, update.message, 
            f"🤖 *I understood this as: {kind.upper()}*\n\n████████░░░░░░░ 55%\n\n{reason or 'Preparing the correct MyTourBazar workflow...'}",
            parse_mode="Markdown",
        )

        if kind == "edit" and ref:
            record = load_record(ref)
            if not record:
                await safe_status_edit(status, update.message, f"❌ I understood the reference as `{ref}`, but that reference was not found.", parse_mode="Markdown")
                await update.message.reply_text("Please tap *Modify & Regenerate* on the PDF you want to change.", parse_mode="Markdown", reply_markup=main_keyboard())
                return ConversationHandler.END
            context.user_data["editing_reference"] = ref
            await safe_status_edit(status, update.message, f"✏️ *Reference {ref} identified.*\n\n████████████████ 100%\n\nApplying your requested changes...", parse_mode="Markdown")
            await perform_saved_edit(update, context, instruction)
            return ConversationHandler.END

        if kind == "chat":
            answer = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(ai_chat, text, GEMINI_API_KEY, GEMINI_MODEL), ['💬 AI is preparing your reply...'], 60, 92)
            await safe_status_edit(status, update.message, answer or "I’m ready. Tell me what you want me to do.", parse_mode=None)
            await update.message.reply_text("Send another request or supplier document.", reply_markup=main_keyboard())
            return ConversationHandler.END

        if kind == "unknown" or conf < 0.45:
            answer = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(ai_chat, text or "I sent a supplier document but its type could not be identified.", GEMINI_API_KEY, GEMINI_MODEL), ['💬 AI is reviewing what you sent...'], 60, 92)
            await safe_status_edit(status, update.message, "🤔 *I need a little more information.*\n\n" + (answer or "Please tell me whether this is a tour, flight, bus or hotel document."))
            await update.message.reply_text("You can send another file/text or use a print button.", reply_markup=main_keyboard())
            return ConversationHandler.END

        if kind == "package":
            smart_files = [str(x) for x in context.user_data.get("smart_files", [])]
            smart_text_value = text
            auto_creation = bool(context.user_data.get("auto_creation"))
            if auto_creation:
                smart_text_value = (
                    "AUTO CREATION MODE. Treat all supplied items as one candidate client-tour batch. "
                    "Use the supplier package/day plan as the main itinerary source when one is supplied. Match flight/train/bus/hotel tickets to the tour using passenger names, travel dates, route continuity, destination, pickup/drop and package dates. "
                    "If there is no supplier day plan but the owner explicitly asks to CREATE a tour for a stated destination/duration, you may build a sensible day-wise sightseeing plan from normal travel knowledge; never invent confirmed hotel names, bookings, prices, tickets or transport facts. "
                    "Deduplicate repeated tickets/sectors. Do not merge a clearly mismatched passenger/date/route. Never invent missing confirmed facts. "
                    "Build one proper day-wise itinerary and weave matched arrival/departure transport naturally into the relevant day descriptions while ALSO preserving every real public-transport sector in transit. "
                    "Infer outward/connection/return direction from dates and route sequence without requiring Onward/Return labels.\n\nOWNER NOTES:\n"
                    + smart_text_value
                ).strip()
            # V168: AI Assistant can create a brand-new Tour directly from a natural
            # owner brief. Supplier extraction remains a separate path.
            if not auto_creation and not smart_files and smart_text_value and not _looks_like_supplier_material(smart_text_value):
                requested_detail = _smart_requested_tour_detail(smart_text_value)
                requested_mode = _smart_requested_tour_mode(smart_text_value)
                requested_b2b = _smart_requested_b2b(smart_text_value)

                await safe_status_edit(
                    status,
                    update.message,
                    "🗺️ *New Tour brief understood.*\n\n████████░░░░░░░░ 55%\n\n✨ Building a professional day-wise itinerary from your instructions...",
                    parse_mode="Markdown",
                )

                data = await _run_with_progress(
                    status,
                    update.message,
                    lambda: asyncio.to_thread(
                        generate_package_from_brief,
                        smart_text_value,
                        GEMINI_API_KEY,
                        GEMINI_MODEL,
                        requested_detail,
                    ),
                    [
                        "🧭 Planning the destination flow...",
                        "🏨 Applying your hotel category, meals and vehicle...",
                        "🗺️ Building the requested day-wise sightseeing plan...",
                    ],
                    55,
                    92,
                )

                data = _normalize_guest_counts(data or {})
                data["detail_level"] = requested_detail
                data["document_mode"] = requested_mode or "itinerary"
                data["show_cost"] = bool(data.get("package_costs"))
                if requested_b2b:
                    data = _b2b_neutralize_data(data, requested_mode or "itinerary")
                    data["greeting"] = _b2b_greeting(data, requested_mode or "itinerary")

                # V169: Quick Client Itinerary must enter the SAME normal Tour
                # draft/review/output workflow used by Tour Guide after extraction.
                # This gives Modify & Regenerate, Basic/Detailed WhatsApp,
                # Basic/Detailed PDF, then Quotation/Voucher selection.
                context.user_data.clear()
                context.user_data["quick_ai_tour"] = True
                context.user_data["smart_owner_brief"] = True
                context.user_data["source_text"] = smart_text_value
                context.user_data["itinerary"] = data
                context.user_data["_source_status_message"] = status
                context.user_data["pending_tour_document_mode"] = requested_mode or "itinerary"
                context.user_data["smart_requested_document_mode"] = requested_mode
                context.user_data["pending_tour_pdf_no_cost"] = not bool(
                    data.get("show_cost") and data.get("package_costs")
                )
                if requested_b2b:
                    context.user_data["pending_b2b"] = True
                    context.user_data["pending_clean_agency"] = True
                    context.user_data["pending_tour_last_page"] = "b2b"

                await safe_status_edit(
                    status,
                    update.message,
                    "✅ *Quick client itinerary created.*\n\n"
                    "████████████████ 100%\n\n" +
                    ("B2B white-label mode is active. " if requested_b2b else "") +
                    "Review the client draft below. You can Modify & Regenerate it, "
                    "send Basic/Detailed WhatsApp, or create Basic/Detailed PDF and then choose Quotation/Voucher.",
                    parse_mode="Markdown",
                )

                await continue_tour_preprint_options(update.message, context, data)
                return ConversationHandler.END

            # Supplier package material goes directly into the authoritative source
            # extractor. Do not stop at classification or wait for a guest name first:
            # the supplier itself may contain the guest name. If it doesn't, the
            # extractor will leave it blank and we can ask afterwards.
            context.user_data.clear()
            context.user_data["tour_v2"] = True
            context.user_data["tour_v2_phase"] = "source"
            context.user_data["auto_creation"] = auto_creation
            context.user_data["_source_status_message"] = status
            context.user_data["media_files"] = smart_files
            context.user_data["source_text"] = smart_text_value
            if auto_creation:
                context.user_data["pending_tour_last_page"] = "without_footer"
                context.user_data["pending_tour_footer_mode"] = "none"
            context.user_data["flight_files"] = []
            context.user_data["flight_text"] = ""
            context.user_data["extra_inclusions"] = []
            context.user_data["extra_exclusions"] = []
            await safe_status_edit(status, update.message, "🗺️ *Tour supplier material recognized.*\n\n████████████████ 100%\n\n📥 Sending the complete source to the Tour extractor now...", parse_mode="Markdown")
            await process_sources(update, context)
            # Missing guest name does not block the draft; it is handled only before PDF printing.
            return ConversationHandler.END

        if kind == "flight":
            files = [str(x) for x in context.user_data.get("smart_files", [])]
            data = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(extract_flight_ticket, _smart_parts(context), text, GEMINI_API_KEY, GEMINI_MODEL), ["✈️ Reading passenger, PNR and flight sectors...", "🔍 Extracting airport, terminal and duration details..."], 60, 92)
            context.user_data.clear()
            context.user_data["_source_status_message"] = status
            context.user_data["pending_flight_data"] = data
            supplier_total = _supplier_total(data)
            context.user_data["smart_mode"] = False
            context.user_data["pending_flight_fare"] = None
            context.user_data["pending_fare_supplier_total"] = supplier_total
            await safe_status_edit(status, update.message, "✅ *Air Print extracted from supplier source.*\n\n████████████████ 100%\n\n🏢 Full airport text preserved\n🚪 Terminal kept whenever supplied\n⏱️ Duration kept whenever supplied", parse_mode="Markdown")
            await send_fare_choice_with_countdown(update.message, context, "flight", supplier_total, status_message=status)
            return ConversationHandler.END

        if kind == "bus":
            data = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(extract_bus_ticket, _smart_parts(context), text, GEMINI_API_KEY, GEMINI_MODEL), ["🚌 Reading passenger, PNR and route details...", "🔍 Extracting fare and journey details..."], 60, 92)
            context.user_data.clear()
            context.user_data["_source_status_message"] = status
            context.user_data["pending_bus_data"] = data
            supplier_total = _supplier_total(data)
            await safe_status_edit(status, update.message, "🚌 *Bus Print recognized and extracted.*\n\n████████████████ 100%", parse_mode="Markdown")
            context.user_data["smart_mode"] = False
            context.user_data["pending_bus_fare"] = None
            context.user_data["pending_fare_supplier_total"] = supplier_total
            await send_fare_choice_with_countdown(update.message, context, "bus", supplier_total, status_message=status)
            return ConversationHandler.END

        if kind == "hotel":
            context.user_data["voucher_files"] = [str(x) for x in context.user_data.get("smart_files", [])]
            context.user_data["voucher_text"] = text
            await safe_status_edit(status, update.message, "🏨 *Hotel Print recognized.*\n\n████████████████ 100%\n\nExtracting booking details, then I will ask for fare / markup before printing.", parse_mode="Markdown")
            await process_hotel_voucher(update, context)
            return ConversationHandler.END

    except Exception as exc:
        logger.exception("Smart AI processing failed")
        await safe_status_edit(status, update.message, f"❌ *AI processing failed*\n\nReason: `{str(exc)[:800]}`", parse_mode="Markdown")
        await update.message.reply_text("Try 🆕 Start Fresh and send the supplier material again.", reply_markup=main_keyboard())
        return ConversationHandler.END


async def smart_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    text = (update.message.text or "").strip()
    if text == "🤖 Auto Creation":
        return await auto_creation_start(update, context)
    if text == "🆕 Start Fresh":
        return await smart_ai_start(update, context)
    if text == "❌ Cancel":
        return await cancel(update, context)
    if text in ("✈️ Air Print", "🚌 Bus Print", "🏨 Hotel Print"):
        forced = {"✈️ Air Print":"flight", "🚌 Bus Print":"bus", "🏨 Hotel Print":"hotel"}[text]
        context.user_data["smart_force_kind"] = forced
        label = {"flight":"Air", "bus":"Bus", "hotel":"Hotel"}[forced]
        await update.message.reply_text(
            f"{text.split()[0]} *{label} mode selected.*\n\nDrop the supplier PDF/screenshot/text here. I will still read all pages automatically.",
            parse_mode="Markdown", reply_markup=smart_source_keyboard()
        )
        return SMART_INPUT
    if text == "⚡ Process Now":
        _cancel_source_auto_process(context)
        return await smart_process(update, context)
    if text == "➕ Send Another":
        await update.message.reply_text("📎 Send the next supplier PDF/screenshot or paste more text.", reply_markup=smart_source_keyboard())
        return SMART_INPUT
    context.user_data["smart_text"] = (context.user_data.get("smart_text", "") + "\n" + text).strip()

    # V169 QUICK CLIENT ITINERARY:
    # When AI Assistant receives a natural no-prefix Tour brief, process it
    # immediately as a new client itinerary. Do not treat it as supplier text
    # and do not wait for the source-batch timer.
    if (
        context.user_data.get("smart_mode")
        and not context.user_data.get("auto_creation")
        and not context.user_data.get("smart_files")
        and _looks_like_new_tour_brief(context.user_data.get("smart_text", ""))
        and not _looks_like_supplier_material(context.user_data.get("smart_text", ""))
    ):
        _cancel_source_auto_process(context)
        status = await update.message.reply_text(
            "⚡ *Quick client itinerary request received.*\n\n"
            "🧠 I’ll build the day-wise Tour itinerary myself from these details and then open the normal Tour workflow.",
            parse_mode="Markdown",
        )
        context.user_data["_source_status_message"] = status
        return await smart_process(update, context)

    if context.user_data.get("auto_creation"):
        msg=await _source_ack_message(
            update, context,
            '📝 Auto Creation note received. Send another file/text/voice within 5 seconds if needed; otherwise I will combine the batch automatically.',
            reply_markup=auto_creation_keyboard())
        _schedule_source_auto_process(update,context,'auto_creation',lambda: smart_process(_SyntheticUpdate(_BotMessageProxy(context.bot,update.effective_chat.id),update.effective_user.id),context),prompt_message=msg)
    else:
        msg=await _source_ack_message(update, context, '📝 Source text received.\n\nSend another source to reset the timer, or tap ⚡ Process Now.',reply_markup=smart_source_keyboard())
        _schedule_source_auto_process(update,context,'smart',lambda: smart_process(_SyntheticUpdate(_BotMessageProxy(context.bot,update.effective_chat.id),update.effective_user.id),context),prompt_message=msg)
    return SMART_INPUT


async def smart_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """No-prefix AI Assistant voice request.

    Press AI Assistant, send a voice note, and the transcript is routed through the
    exact same free-form text planner used by smart_text/smart_process.
    """
    if not is_allowed(update):
        return ConversationHandler.END
    msg = update.message
    if not msg or not msg.voice:
        return SMART_INPUT
    if not GEMINI_API_KEY:
        await msg.reply_text("❌ GEMINI_API_KEY is not configured.", reply_markup=main_keyboard())
        return ConversationHandler.END

    tg_file = await context.bot.get_file(msg.voice.file_id)
    path = TEMP_DIR / f"voice_ai_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.ogg"
    status = await msg.reply_text(
        "🎙️ *Listening to your AI Assistant request...*\\n\\nYou can speak naturally — no command or prefix is required.",
        parse_mode="Markdown",
    )
    try:
        await tg_file.download_to_drive(path)
        transcript = await _run_ai_with_retry_status(
            msg,
            lambda: asyncio.to_thread(
                transcribe_voice_note,
                path,
                GEMINI_API_KEY,
                GEMINI_MODEL,
                msg.voice.mime_type or "audio/ogg",
            ),
            status=status,
        )
        transcript = str(transcript or "").strip()
        if not transcript:
            raise RuntimeError("Voice transcription was empty.")

        await safe_status_edit(
            status,
            msg,
            "✅ *Voice request understood.*\\n\\n🧠 Building the requested itinerary / action now...",
            parse_mode="Markdown",
        )
        await msg.reply_text("🎙️ *I understood:*\\n" + transcript, parse_mode="Markdown")

        context.user_data["smart_mode"] = True
        context.user_data.setdefault("smart_files", [])
        context.user_data["smart_text"] = transcript
        context.user_data["_source_status_message"] = status
        return await smart_process(update, context)

    except Exception as exc:
        logger.exception("AI Assistant voice request failed")
        await safe_status_edit(
            status,
            msg,
            f"⚠️ *Voice request could not be completed.*\\n\\nReason: `{str(exc)[:600]}`\\n\\nYou can resend the voice note or type the same request.",
            parse_mode="Markdown",
        )
        return SMART_INPUT
    finally:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


async def smart_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    photo=update.message.photo[-1]
    tg_file=await context.bot.get_file(photo.file_id)
    path=TEMP_DIR/f"smart_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
    await tg_file.download_to_drive(path)
    context.user_data.setdefault('smart_files',[]).append(str(path))
    direct=bool(context.user_data.get('_direct_drop_mode'))
    auto=bool(context.user_data.get('auto_creation'))
    if auto:
        n=len(context.user_data.get('smart_files') or [])
        ack=f'📸 Auto Creation source received ({n}). Send another file/text/voice within 5 seconds if needed; otherwise I will combine the batch automatically.'
        kb=auto_creation_keyboard(); workflow='auto_creation'
    else:
        ack=('📸 Supplier file received. Send another page/source within 5 seconds if needed; otherwise I will identify and process it automatically.'
             if direct else '📸 Supplier file received.\n\nSend another source to reset the timer, or tap ⚡ Process Now.')
        kb=direct_drop_keyboard() if direct else smart_source_keyboard(); workflow='direct_smart' if direct else 'smart'
    msg=await _source_ack_message(update, context, ack, reply_markup=kb)
    _schedule_source_auto_process(update,context,workflow,lambda: smart_process(_SyntheticUpdate(_BotMessageProxy(context.bot,update.effective_chat.id),update.effective_user.id),context),prompt_message=msg)
    return SMART_INPUT

async def smart_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    doc=update.message.document; mime=(doc.mime_type or '').lower(); name=doc.file_name or 'supplier.pdf'
    if not (name.lower().endswith('.pdf') or mime=='application/pdf'):
        await update.message.reply_text('Please send a PDF, screenshot, or text.',reply_markup=smart_source_keyboard()); return SMART_INPUT
    tg_file=await context.bot.get_file(doc.file_id)
    safe=''.join(c if c.isalnum() or c in '._-' else '_' for c in name)
    path=TEMP_DIR/f"smart_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S}_{safe}"
    await tg_file.download_to_drive(path)
    context.user_data.setdefault('smart_files',[]).append(str(path))
    direct=bool(context.user_data.get('_direct_drop_mode'))
    auto=bool(context.user_data.get('auto_creation'))
    if auto:
        n=len(context.user_data.get('smart_files') or [])
        ack=f'📄 Auto Creation source received ({n}). Send another file/text/voice within 5 seconds if needed; otherwise I will combine the batch automatically.'
        kb=auto_creation_keyboard(); workflow='auto_creation'
    else:
        ack=('📄 Supplier file received. Send another page/source within 5 seconds if needed; otherwise I will identify and process it automatically.'
             if direct else '📄 Supplier file received.\n\nSend another source to reset the timer, or tap ⚡ Process Now.')
        kb=direct_drop_keyboard() if direct else smart_source_keyboard(); workflow='direct_smart' if direct else 'smart'
    msg=await _source_ack_message(update, context, ack, reply_markup=kb)
    _schedule_source_auto_process(update,context,workflow,lambda: smart_process(_SyntheticUpdate(_BotMessageProxy(context.bot,update.effective_chat.id),update.effective_user.id),context),prompt_message=msg)
    return SMART_INPUT


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END

    # /START is a true workflow reset. Never allow a previous countdown/task to
    # survive into the newly selected Air/Bus/Hotel/Tour workflow.
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()

    await update.message.reply_text(
        "✈️ *MyTourBazar Print Bot*\n\n"
        "📥 Drop an *Air, Bus or Hotel* supplier itinerary here — PDF, image or text — and I will create the MyTourBazar print automatically.\n\n"
        "🗺️ For a normal *Tour itinerary*, tap *Tour Guide* below.\n\n"
        "🤖 For a supplier package plus multiple client flight/train/bus/hotel tickets, tap *Auto Creation* and send them together.\n\n"
        "You can also use the service buttons when you want to force Air, Bus or Hotel mode.",
        parse_mode="Markdown",
        reply_markup=main_keyboard(),
    )
    return ConversationHandler.END


async def stop_bot_workflow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END
    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    await update.message.reply_text('⏹️ *Current process stopped.*\n\nThe bot is still running and ready for the next request.', parse_mode='Markdown', reply_markup=main_keyboard())
    return ConversationHandler.END

def guest_name_keyboard():
    # Keep cancellation visible during every guest/client-name prompt.
    # The conversation fallback maps this button to the same /cancel handler.
    return ReplyKeyboardMarkup([["❌ Cancel"]], resize_keyboard=True, one_time_keyboard=False)


async def new_itinerary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        await update.message.reply_text("Sorry, this bot is private.")
        return ConversationHandler.END

    _cancel_auto_print(context)
    _cancel_source_auto_process(context)
    context.user_data.clear()
    context.user_data["media_files"] = []
    context.user_data["flight_files"] = []
    context.user_data["flight_text"] = ""
    context.user_data["source_text"] = ""
    context.user_data["guest_name"] = ""
    context.user_data["extra_inclusions"] = []
    context.user_data["extra_exclusions"] = []
    context.user_data["tour_v2"] = True
    context.user_data["tour_v2_phase"] = "source"

    await update.message.reply_text(
        "🗺️ *Tour Itinerary • New Workflow*\n\n"
        "Drop the supplier text, PDF or image. AI will extract all available details first. "
        "If an important accommodation detail is genuinely missing, I will ask you in a simple normal message before creating the draft.",
        parse_mode="Markdown",
        reply_markup=source_keyboard(),
    )
    return WAITING_SOURCE


async def receive_tour_source_without_guest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return ConversationHandler.END
    try:
        context.user_data.setdefault('media_files', []); context.user_data.setdefault('flight_files', []); context.user_data.setdefault('flight_text', '')
        context.user_data.setdefault('extra_inclusions', []); context.user_data.setdefault('extra_exclusions', [])
        if update.message.photo:
            tg_file=await context.bot.get_file(update.message.photo[-1].file_id)
            path=TEMP_DIR/f"tour_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
            await tg_file.download_to_drive(path); context.user_data['media_files'].append(str(path))
            await _source_ack_message(update, context, '📸 Supplier screenshot received. Extracting it now…')
        else:
            doc=update.message.document; name=doc.file_name or 'supplier.pdf'; mime=(doc.mime_type or '').lower()
            if not (name.lower().endswith('.pdf') or mime=='application/pdf'):
                await update.message.reply_text('Please send a PDF, screenshot, or supplier text.')
                return WAITING_GUEST_NAME
            tg_file=await context.bot.get_file(doc.file_id); safe=''.join(c if c.isalnum() or c in '._-' else '_' for c in name)
            path=TEMP_DIR/f"tour_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S}_{safe}"
            await tg_file.download_to_drive(path); context.user_data['media_files'].append(str(path))
            await _source_ack_message(update, context, '📄 Supplier PDF received. Extracting it now…')
        await process_sources(update, context)
        return ConversationHandler.END
    except Exception as exc:
        logger.exception('Tour source before guest failed')
        await update.message.reply_text(f'❌ Could not read supplier source: {str(exc)[:500]}')
        return WAITING_GUEST_NAME


async def receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if text == "✈️ Air Print":
        return await flight_ticket_start(update, context)
    if text == "🚌 Bus Print":
        return await bus_ticket_start(update, context)
    if text == "🏨 Hotel Print":
        return await hotel_voucher_start(update, context)
    if text in ("🗺️ Tour Itinerary", "🗺️ Tour Guide"):
        return await new_itinerary(update, context)
    if text in ("🤖 AI Assistant", "🤖 AI Assistant / New Request"):
        return await smart_ai_start(update, context)

    # V154: once the editable Tour draft has been sent, the NEXT text message is
    # the final submission. The Tour ConversationHandler may still be in WAITING_SOURCE
    # because auto-processing happened in the background; do not let that message be
    # appended as new supplier material and create another draft loop.
    if _tour_v2_active(context) and context.user_data.get("tour_v2_phase") == "awaiting_edited_final":
        await _tour_v2_process_edited_final(update.message, context, text)
        return ConversationHandler.END

    if text == "🖼️ Set Logo":
        return await set_logo(update, context)
    if text in ("📂 My Files", "✏️ Edit by Ref"):
        await update.message.reply_text(
            "That old shortcut has been removed. Open the generated PDF you want to change and tap *Modify & Regenerate* or *Voice / Text Edit*.",
            parse_mode="Markdown", reply_markup=main_keyboard()
        )
        return ConversationHandler.END
    if text == "🏨 Hotel Voucher":
        return await hotel_voucher_start(update, context)
    if text == "🚌 Bus Ticket Itinerary":
        return await bus_ticket_start(update, context)
    if text in ("🤖 AI Assistant", "🤖 AI Assistant / New Request"):
        return await smart_ai_start(update, context)
    if text == "🆕 Start Fresh":
        return await smart_ai_start(update, context)

    if text in ("❌ Cancel", "📄 Send PDF / 📝 Text", "📸 Send Screenshot"):
        if text == "❌ Cancel":
            return await cancel(update, context)
        await update.message.reply_text(
            "Please send the actual itinerary PDF, paste the itinerary text, or send a screenshot.",
            reply_markup=source_keyboard()
        )
        return WAITING_SOURCE

    if text == "➕ Add Another Page":
        await update.message.reply_text(
            "📎 Send the next file/page. Tap *✅ Done* when all supplier material is sent.",
            parse_mode="Markdown",
            reply_markup=source_keyboard()
        )
        return WAITING_SOURCE

    if text == "✈️ Flight Screenshot":
        context.user_data["awaiting_flight"] = True
        await update.message.reply_text(
            "✈️ Send the flight screenshot now. You can send onward and return screenshots one after another. "
            "I will identify and separate every flight automatically.",
            reply_markup=ReplyKeyboardRemove()
        )
        return WAITING_SOURCE

    if text == "✍️ Flight Text":
        context.user_data["awaiting_flight"] = True
        await update.message.reply_text(
            "✍️ *Enter the flight / train details in text.*\n\n"
            "You can paste onward and return details together or send them one by one. "
            "You do not need to format them perfectly — Gemini will read and organize the details automatically.\n\n"
            "Example:\n`01 Oct: IndiGo 6E-594 Raipur → Mumbai 09:30 AM – 11:25 AM\n"
            "01 Oct: IndiGo 6E-273 Mumbai → Rajkot 01:10 PM – 03:50 PM\n"
            "05 Oct: IndiGo 6E-233 Rajkot → Mumbai 09:15 AM – 11:05 AM\n"
            "05 Oct: IndiGo 6E-5345 Mumbai → Raipur 02:15 PM – 04:25 PM`\n\n"
            "When finished, tap *✅ Done*.",
            parse_mode="Markdown",
            reply_markup=source_keyboard()
        )
        return WAITING_SOURCE

    if text == "✅ Done":
        _cancel_source_auto_process(context)
        if context.user_data.get('_source_auto_processed') in ('tour','smart'):
            return ConversationHandler.END
        await process_sources(update, context)
        return ConversationHandler.END

    if len(text) > 50000:
        await update.message.reply_text("This text is too long for one Telegram message. Please send it as a PDF instead.")
        return WAITING_SOURCE

    if context.user_data.get("awaiting_flight"):
        context.user_data["flight_text"] = (context.user_data.get("flight_text", "") + "\n" + text).strip()
        context.user_data["awaiting_flight"] = False
        msg=await update.message.reply_text(
            "✈️ Flight text received. You can send another flight screenshot/text, or tap *✅ Done*.",
            parse_mode="Markdown",
            reply_markup=source_keyboard()
        )
        _schedule_source_auto_process(update, context, 'tour', lambda: process_sources(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
        return WAITING_SOURCE

    context.user_data["source_text"] = (context.user_data.get("source_text", "") + "\n" + text).strip()
    msg=await _source_ack_message(update, context, "📝 Text received.\n\nSend another source to reset the timer, or tap *✅ Done*.",reply_markup=source_keyboard())
    _schedule_source_auto_process(update,context,'tour',lambda: process_sources(_SyntheticUpdate(_BotMessageProxy(context.bot,update.effective_chat.id),update.effective_user.id),context),prompt_message=msg)
    return WAITING_SOURCE


async def receive_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END

    try:
        if context.user_data.get("waiting_for_logo"):
            return await receive_logo(update, context)
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        filename = TEMP_DIR / f"{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
        await tg_file.download_to_drive(filename)

        if context.user_data.get("awaiting_flight"):
            context.user_data.setdefault("flight_files", []).append(str(filename))
            context.user_data["awaiting_flight"] = True
            msg=await _source_ack_message(update, context, "✈️ Flight screenshot received. Send another flight screenshot/text, or tap *✅ Done*.", reply_markup=source_keyboard())
            _schedule_source_auto_process(update, context, 'tour', lambda: process_sources(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
        else:
            context.user_data.setdefault("media_files", []).append(str(filename))
            msg=await _source_ack_message(update, context, "📸 Supplier screenshot received. Send more material, or tap *✅ Done*.", reply_markup=source_keyboard())
            _schedule_source_auto_process(update, context, 'tour', lambda: process_sources(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
        return WAITING_SOURCE
    except Exception as exc:
        logger.exception("Photo download failed")
        await update.message.reply_text(f"❌ Could not read the image: {exc}")
        return WAITING_SOURCE


async def receive_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END

    doc = update.message.document
    filename_lower = (doc.file_name or "").lower()
    mime = (doc.mime_type or "").lower()

    if not (filename_lower.endswith(".pdf") or mime == "application/pdf"):
        await update.message.reply_text("Please send a PDF, image, or paste the itinerary text.")
        return WAITING_SOURCE

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        safe_name = "".join(c if c.isalnum() or c in "._-" else "_" for c in (doc.file_name or "supplier.pdf"))
        path = TEMP_DIR / f"{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S}_{safe_name}"
        await tg_file.download_to_drive(path)
        context.user_data.setdefault("media_files", []).append(str(path))
        msg=await _source_ack_message(update, context, "📄 Supplier PDF received. Send more material if needed, or tap *✅ Done*.", reply_markup=source_keyboard())
        _schedule_source_auto_process(update, context, 'tour', lambda: process_sources(_SyntheticUpdate(_BotMessageProxy(context.bot, update.effective_chat.id), update.effective_user.id), context), prompt_message=msg)
        return WAITING_SOURCE
    except Exception as exc:
        logger.exception("PDF download failed")
        await update.message.reply_text(f"❌ Could not read the PDF: {exc}")
        return WAITING_SOURCE


async def media_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END

    text = update.message.text

    if text == "❌ Cancel":
        return await cancel(update, context)

    if text == "➕ Add Another Page":
        await update.message.reply_text(
            "📸 Send the next screenshot/page.",
            reply_markup=media_keyboard()
        )
        return WAITING_SOURCE

    if text == "✅ Done":
        files = context.user_data.get("media_files", [])
        if not files:
            await update.message.reply_text(
                "No screenshot has been received yet. Please send a screenshot or PDF."
            )
            return WAITING_SOURCE

        await update.message.reply_text(
            "👤 *Whose itinerary are you preparing?*\n\n"
            "Please enter the Guest/Client Name.\n"
            "Example: `Mr. Amit Sharma & Family`",
            parse_mode="Markdown",
            reply_markup=guest_name_keyboard(),
        )
        return WAITING_GUEST_NAME

    return WAITING_SOURCE


async def receive_guest_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return ConversationHandler.END

    guest_name = (update.message.text or "").strip()
    if not guest_name or guest_name in ("❌ Cancel",):
        await update.message.reply_text("Please enter the Guest / Client Name.", reply_markup=guest_name_keyboard())
        return WAITING_GUEST_NAME
    if _looks_like_supplier_material(guest_name):
        context.user_data['source_text']=guest_name
        context.user_data.setdefault('media_files', []); context.user_data.setdefault('flight_files', []); context.user_data.setdefault('flight_text','')
        context.user_data.setdefault('extra_inclusions', []); context.user_data.setdefault('extra_exclusions', [])
        await update.message.reply_text('📥 Supplier text received. I’m extracting it as Tour/Air/Bus/Hotel source material now.')
        await process_sources(update, context)
        return ConversationHandler.END

    context.user_data["guest_name"] = guest_name
    if context.user_data.get("ai_tour_pending"):
        data = context.user_data.get("itinerary", {})
        data["client_name"] = guest_name
        context.user_data["itinerary"] = data
        context.user_data.pop("ai_tour_pending", None)
        await update.message.reply_text(build_confirmation(data), parse_mode="Markdown", reply_markup=confirmation_keyboard())
        return ConversationHandler.END
    if context.user_data.get("smart_supplier_pending"):
        context.user_data.pop("smart_supplier_pending", None)
        await update.message.reply_text("👤 Guest name saved. Now I’ll build the final itinerary from the supplier material.", reply_markup=ReplyKeyboardRemove())
        await process_sources(update, context)
        return ConversationHandler.END
    await update.message.reply_text(
        f"👤 Guest name saved: *{guest_name}*\n\n"
        "Now send *everything you have* — supplier PDF, supplier text, screenshots, hotel details, "
        "or flight screenshots.\n\n"
        "You do NOT need to tell me what each file is. Gemini will identify it automatically.\n\n"
        "When you have finished sending all material, tap *✅ Done*.",
        parse_mode="Markdown",
        reply_markup=source_keyboard(),
    )
    return WAITING_SOURCE


def tour_special_notes_keyboard():
    return InlineKeyboardMarkup([[InlineKeyboardButton('➕ Add Special Notes', callback_data='tour_special_notes:add'),
                                  InlineKeyboardButton('➡️ Skip Special Notes', callback_data='tour_special_notes:skip')]])

def tour_cost_keyboard():
    # Legacy pre-print cost menu is intentionally empty. Customer costing is handled
    # only through Modify & Regenerate as direct final selling rates.
    return InlineKeyboardMarkup([])

def _money(value):
    try:
        return f"₹{_num_cost(value):,.0f}"
    except Exception:
        return "₹0"

def _ensure_supplier_costs(data):
    """Store the extracted package cost as the internal supplier cost by default."""
    data = copy.deepcopy(data or {})
    costs = data.get('package_costs') or []
    adults=int(data.get('adult_count') or 0)
    child=int(data.get('child_count') or 0)
    cwb=int(data.get('child_cwb_count') or 0)
    cnb=int(data.get('child_cnb_count') or 0)
    eb=int(data.get('extra_bed_count') or 0)
    for c in costs:
        if _num_cost(c.get('supplier_total')) > 0:
            continue
        pa=_num_cost(c.get('per_adult'))
        pc=_num_cost(c.get('per_child'))
        pcw=_num_cost(c.get('per_child_cwb'))
        pcn=_num_cost(c.get('per_child_cnb'))
        peb=_num_cost(c.get('per_extra_bed'))
        calculated=(pa*adults)+(pc*child)+(pcw*cwb)+(pcn*cnb)+(peb*eb)
        supplier=calculated if calculated > 0 else _num_cost(c.get('total_cost'))
        if supplier > 0:
            c['supplier_total']=f'{supplier:,.0f}'
    return data

def custom_cost_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('☑️ Done', callback_data='tour_custom_cost:done')],
        [InlineKeyboardButton('❌ Cancel', callback_data='tour_custom_cost:cancel')],
    ])


def _custom_cost_fields(data):
    """Return the cost fields that have a real passenger count."""
    fields = []
    counts = [
        ('adult', 'per_adult', 'Adult', int(data.get('adult_count') or 0)),
        ('child', 'per_child', 'Child', int(data.get('child_count') or 0)),
        ('cwb', 'per_child_cwb', 'Child CWB', int(data.get('child_cwb_count') or 0)),
        ('cnb', 'per_child_cnb', 'Child CNB', int(data.get('child_cnb_count') or 0)),
        ('eb', 'per_extra_bed', 'Extra Bed (EB)', int(data.get('extra_bed_count') or 0)),
    ]
    for key, field, label, count in counts:
        if count > 0:
            fields.append((key, field, label, count))
    return fields


def _custom_cost_summary(data):
    costs = data.get('package_costs') or []
    lines = ['🧾 *Custom Cost Preview*', '']
    counts = {
        'per_adult': int(data.get('adult_count') or 0),
        'per_child': int(data.get('child_count') or 0),
        'per_child_cwb': int(data.get('child_cwb_count') or 0),
        'per_child_cnb': int(data.get('child_cnb_count') or 0),
        'per_extra_bed': int(data.get('extra_bed_count') or 0),
    }
    labels = {
        'per_adult': 'Adult', 'per_child': 'Child', 'per_child_cwb': 'Child CWB',
        'per_child_cnb': 'Child CNB', 'per_extra_bed': 'Extra Bed (EB)'
    }
    for c in costs:
        lines.append(f"*{c.get('option') or 'Package'}*")
        total = 0.0
        for field, count in counts.items():
            rate = _num_cost(c.get(field))
            if rate > 0 and count > 0:
                amount = rate * count
                total += amount
                lines.append(f"• {labels[field]}: {_money(rate)} × {count} = *{_money(amount)}*")
        lines.append(f"• *Total Cost: {_money(total)}*")
        lines.append('')
    return '\n'.join(lines).rstrip()


def _apply_custom_cost_field(data, field, amount):
    data = copy.deepcopy(data or {})
    costs = data.get('package_costs') or []
    if not costs:
        raise ValueError('No package cost is available.')
    amount = float(amount)
    if amount < 0:
        raise ValueError('Cost cannot be negative.')
    # Custom Cost is intentionally a direct replacement of the selected per-person rate.
    for c in costs:
        c[field] = f'{amount:,.0f}'
        adults=int(data.get('adult_count') or 0); child=int(data.get('child_count') or 0)
        cwb=int(data.get('child_cwb_count') or 0); cnb=int(data.get('child_cnb_count') or 0); eb=int(data.get('extra_bed_count') or 0)
        total = (_num_cost(c.get('per_adult'))*adults + _num_cost(c.get('per_child'))*child +
                 _num_cost(c.get('per_child_cwb'))*cwb + _num_cost(c.get('per_child_cnb'))*cnb +
                 _num_cost(c.get('per_extra_bed'))*eb)
        c['supplier_total'] = f'{total:,.0f}'
        c['total_cost'] = f'{total:,.0f}'
        c['final_total'] = f'{total:,.0f}'
        c['markup_total'] = '0'
    data['markup_total'] = '0'
    return data


def _finalize_custom_cost(data):
    data = copy.deepcopy(data or {})
    costs = data.get('package_costs') or []
    if not costs:
        raise ValueError('No package cost is available.')
    adults=int(data.get('adult_count') or 0); child=int(data.get('child_count') or 0)
    cwb=int(data.get('child_cwb_count') or 0); cnb=int(data.get('child_cnb_count') or 0); eb=int(data.get('extra_bed_count') or 0)
    for c in costs:
        total = (_num_cost(c.get('per_adult'))*adults + _num_cost(c.get('per_child'))*child +
                 _num_cost(c.get('per_child_cwb'))*cwb + _num_cost(c.get('per_child_cnb'))*cnb +
                 _num_cost(c.get('per_extra_bed'))*eb)
        c['supplier_total'] = f'{total:,.0f}'
        c['total_cost'] = f'{total:,.0f}'
        c['final_total'] = f'{total:,.0f}'
        c['markup_total'] = '0'
    data['markup_total'] = '0'
    data['show_cost'] = True
    return data


def _tour_policy_preview(text):
    lines=[x.strip(' •-') for x in str(text or '').splitlines() if x.strip()]
    if not lines:
        return ''
    return '\n'.join(f'• {x}' for x in lines[:10])

async def continue_tour_preprint_options(message, context, data):
    """Final draft checkpoint with Smart Edit plus Basic/Detailed WhatsApp/PDF shortcuts."""
    data = _ensure_supplier_costs(data)
    # Supplier cost is internal by default. Keep it in the saved working data so the
    # saved supplier cost may remain internal, but customer costing is changed only through Modify & Regenerate.
    context.user_data['itinerary'] = data
    context.user_data['pending_tour_cost_decided'] = True
    context.user_data['pending_tour_pdf_no_cost'] = True
    context.user_data['pending_tour_document_mode'] = data.get('document_mode') or 'itinerary'
    context.user_data['pending_tour_markup_print'] = None
    await _send_draft_review(message, context, data)
    return True

async def _run_with_progress(status, chat_message, work, labels, start_pct=30, end_pct=58):
    """Run AI/supplier work with one live status message.

    Gemini 503/high-demand errors are retried inside the extractor before temporary
    source files are cleaned up. A contextvar notifier lets this loop temporarily
    replace the normal progress animation with a visible retry countdown.
    """
    retry_state = {"attempt": 0, "until": 0.0, "reason": ""}

    def _retry_notifier(attempt, delay, exc):
        retry_state["attempt"] = int(attempt)
        retry_state["until"] = time.monotonic() + int(delay)
        retry_state["reason"] = str(exc)[:220]

    token = set_retry_notifier(_retry_notifier)
    task = asyncio.create_task(work())
    tick = 0
    try:
        while not task.done():
            now = time.monotonic()
            retry_until = float(retry_state.get("until") or 0)
            if retry_until > now:
                remaining = max(1, int(round(retry_until - now)))
                attempt = int(retry_state.get("attempt") or 1)
                pulse = "🔄" if remaining % 2 else "⏳"
                await safe_status_edit(
                    status, chat_message,
                    f"⚠️ *AI model is experiencing high demand*\n\n"
                    f"{pulse} Retrying automatically • Retry #{attempt}\n"
                    f"⏱️ Next attempt in about *{remaining}s*\n\n"
                    "I’ll keep retrying in the background until the request is delivered successfully.",
                    parse_mode='Markdown'
                )
                await asyncio.sleep(1.0)
                continue

            pct = min(end_pct - 1, start_pct + tick * 3)
            filled = min(16, round(pct / 100 * 16))
            bar = '█' * filled + '░' * (16 - filled)
            label = labels[tick % len(labels)]
            await safe_status_edit(status, chat_message, f"🤖 *Processing your supplier material...*\n\n{bar} {pct}%\n\n{label}", parse_mode='Markdown')
            tick += 1
            await asyncio.sleep(1.5)
        return await task
    except Exception:
        if not task.done():
            task.cancel()
        raise
    finally:
        reset_retry_notifier(token)

async def _run_ai_with_retry_status(chat_message, work, status=None):
    """Run an AI task and only take over the UI if Gemini reports high demand.

    This is used for edit/detail operations that already have their own status text.
    It preserves that normal text, but on a 503 it shows a live retry countdown and
    keeps waiting until the underlying Gemini call succeeds.
    """
    retry_state = {"attempt": 0, "until": 0.0}

    def _retry_notifier(attempt, delay, exc):
        retry_state["attempt"] = int(attempt)
        retry_state["until"] = time.monotonic() + int(delay)

    token = set_retry_notifier(_retry_notifier)
    task = asyncio.create_task(work())
    retry_message = status
    created_retry_message = False
    try:
        while not task.done():
            now = time.monotonic()
            retry_until = float(retry_state.get("until") or 0)
            if retry_until > now:
                if retry_message is None:
                    retry_message = await chat_message.reply_text(
                        "⚠️ *AI model is experiencing high demand*\n\n🔄 Retrying automatically...",
                        parse_mode="Markdown",
                    )
                    created_retry_message = True
                remaining = max(1, int(round(retry_until - now)))
                attempt = int(retry_state.get("attempt") or 1)
                pulse = "🔄" if remaining % 2 else "⏳"
                await safe_status_edit(
                    retry_message, chat_message,
                    f"⚠️ *AI model is experiencing high demand*\n\n"
                    f"{pulse} Retrying automatically • Retry #{attempt}\n"
                    f"⏱️ Next attempt in about *{remaining}s*\n\n"
                    "I’ll keep retrying until the request is delivered successfully.",
                    parse_mode="Markdown",
                )
                await asyncio.sleep(1.0)
            else:
                await asyncio.sleep(0.6)
        result = await task
        if created_retry_message and retry_message is not None:
            await safe_status_edit(retry_message, chat_message, "✅ *AI responded.* Continuing your request...", parse_mode="Markdown")
        return result
    except Exception:
        if not task.done():
            task.cancel()
        raise
    finally:
        reset_retry_notifier(token)


def _normalize_guest_counts(data):
    """Fill passenger counts from explicit AI fields or the guests text when possible."""
    data = data or {}
    def n(key):
        try: return max(0, int(data.get(key) or 0))
        except Exception: return 0
    adults, child, cwb, cnb, eb = n('adult_count'), n('child_count'), n('child_cwb_count'), n('child_cnb_count'), n('extra_bed_count')
    raw = str(data.get('guests') or '')
    if adults<=0:
        m=re.search(r'(\d+)\s*adult', raw, re.I); adults=int(m.group(1)) if m else adults
    if child<=0:
        m=re.search(r'(\d+)\s*(?:generic\s+)?child(?![^,;]*(?:with\s*bed|no\s*bed|cwb|cnb))', raw, re.I); child=int(m.group(1)) if m else child
    if cwb<=0:
        m=re.search(r'(\d+)\s*(?:child[^,;]*?(?:with\s*bed|cwb)|cwb)', raw, re.I); cwb=int(m.group(1)) if m else cwb
    if cnb<=0:
        m=re.search(r'(\d+)\s*(?:child[^,;]*?(?:no\s*bed|cnb)|cnb)', raw, re.I); cnb=int(m.group(1)) if m else cnb
    if eb<=0:
        m=re.search(r'(\d+)\s*(?:extra\s*bed|eb)', raw, re.I); eb=int(m.group(1)) if m else eb
    data['adult_count']=adults; data['child_count']=child; data['child_cwb_count']=cwb; data['child_cnb_count']=cnb; data['extra_bed_count']=eb
    data['guest_profile']=f'{adults} Adult(s) • {child} Child • {cwb} Child CWB • {cnb} Child CNB • {eb} Extra Bed(s)'
    return data

def _num_cost(v):
    try:
        return float(re.sub(r"[^0-9.\-]", "", str(v or "0").replace(",", "")))
    except Exception:
        return 0.0


def _apply_category_values(data, changes):
    data=dict(data or {})
    for c in data.get('package_costs') or []:
        for key,(op,delta) in changes.items():
            field={'adult':'per_adult','cwb':'per_child_cwb','cnb':'per_child_cnb','eb':'per_extra_bed'}[key]
            base=_num_cost(c.get(field))
            value=delta if op=='=' else (base+delta if op=='+' else base-delta)
            c[field]=f'{value:,.0f}'
    return data


# =========================
# V145 SIMPLIFIED TOUR WORKFLOW
# =========================
def _tour_v2_active(context):
    return bool(context.user_data.get("tour_v2"))


def _tour_v2_missing_details(data):
    """Ask only about practical accommodation facts that are genuinely absent."""
    missing=[]
    hotels=data.get("hotels") or []
    if hotels:
        def _has_category(h):
            explicit=str(h.get("hotel_category") or h.get("star_category") or h.get("category") or "").strip()
            if explicit: return True
            combined=" ".join(str(h.get(k) or "") for k in ("hotel_name","room_category","room_type","option"))
            return bool(re.search(r"\b[1-5]\s*(?:star|\*)\b|\b(?:deluxe|premium|luxury|standard|budget)\b",combined,re.I))
        if any(not _has_category(h) for h in hotels):
            missing.append("hotel category")
        if any(not str(h.get("rooms") or "").strip() for h in hotels):
            missing.append("number of rooms")
        if any(not str(h.get("room_type") or h.get("room_category") or "").strip() for h in hotels):
            missing.append("room type")
    return missing


def _tour_v2_stage_keyboard(stage):
    labels={
        "onward": "⏭️ No Onward Journey",
        "return": "⏭️ No Return Journey",
        "connection": "⏭️ No Connecting Journey",
    }
    return ReplyKeyboardMarkup([[labels[stage]],["❌ Cancel"]], resize_keyboard=True, one_time_keyboard=False)


def _tour_v2_output_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📱 Basic WhatsApp", callback_data="tour_output:whatsapp:basic"),
         InlineKeyboardButton("📱 Detailed WhatsApp", callback_data="tour_output:whatsapp:detailed")],
        [InlineKeyboardButton("📄 Basic PDF", callback_data="tour_output:pdf:basic"),
         InlineKeyboardButton("📄 Detailed PDF", callback_data="tour_output:pdf:detailed")],
    ])

def _tour_v2_set_journey_type(rows, stage):
    out=[]
    for idx,row in enumerate(rows or []):
        r=dict(row or {})
        r["_v2_stage"]=stage
        if stage=="connection":
            r["journey_type"]="Connection"
        elif stage=="onward":
            r["journey_type"]="Onward" if idx==0 else "Connection"
        elif stage=="return":
            r["journey_type"]="Return" if idx==0 else "Connection"
        out.append(r)
    return out


async def _tour_v2_show_draft(message, context, prefix=None):
    data=context.user_data.get("itinerary") or {}
    # Use the existing dynamic draft renderer but do not show the old workflow buttons.
    body=build_confirmation(data)
    if prefix:
        body=f"{prefix}\n\n{body}"
    await reply_text_chunked(message, body, parse_mode="Markdown")


async def _tour_v2_ask_onward(message, context):
    context.user_data["tour_v2_phase"]="onward"
    await message.reply_text(
        "✈️ *Add onward journey*\n\nSend the onward flight/train/bus part as an image, PDF or normal text. I will extract it and place it in the Transit section.\n\nIf there is no onward journey to add, tap No Onward Journey.",
        parse_mode="Markdown", reply_markup=_tour_v2_stage_keyboard("onward"))


async def _tour_v2_ask_return(message, context):
    context.user_data["tour_v2_phase"]="return"
    await message.reply_text(
        "↩️ *Add return journey*\n\nSend the return journey as an image, PDF or normal text. I will add it beside the onward journey in the Transit section.\n\nIf there is no return journey, tap No Return Journey.",
        parse_mode="Markdown", reply_markup=_tour_v2_stage_keyboard("return"))


async def _tour_v2_ask_connection(message, context):
    context.user_data["tour_v2_phase"]="connection"
    await message.reply_text(
        "🔄 *Any connecting journey / transit?*\n\nIf yes, send the connecting-flight PDF, screenshot or text. Multiple sectors in the same source will all be extracted and added.\n\nIf there is no connecting journey, tap No Connecting Journey.",
        parse_mode="Markdown", reply_markup=_tour_v2_stage_keyboard("connection"))


async def _tour_v2_show_outputs(message, context):
    context.user_data["tour_v2_phase"]="choose_output"
    await message.reply_text("✅ Draft is ready. Choose what you want to generate:", reply_markup=ReplyKeyboardRemove())
    await message.reply_text("Choose Basic WhatsApp, Detailed WhatsApp, Basic PDF or Detailed PDF.", reply_markup=_tour_v2_output_keyboard())


async def _tour_v2_after_initial_extract(message, context, data):
    """V155 simple Tour flow: supplier → draft → output choice.

    No forced transit/costing steps are inserted before printing. Missing or changed
    costing/transit can be added later from the single Modify & Regenerate action.
    """
    data=copy.deepcopy(data or {})
    # Supplier/internal cost is hidden by default. For a Tour created directly from
    # the owner's AI Assistant brief, any explicitly supplied price is customer-authored
    # and may remain visible.
    if context.user_data.get('smart_owner_brief'):
        data['show_cost']=bool(data.get('package_costs'))
    else:
        data['show_cost']=False
    context.user_data['itinerary']=data
    context.user_data['tour_v2_phase']='choose_output'
    context.user_data.pop('tour_v2_missing',None)
    for _k in ('pending_fare_kind','pending_fare_supplier_total','awaiting_tour_transit_choice',
               'awaiting_tour_transit_input','pending_tour_transit_files','pending_tour_transit_text',
               'pending_tour_pdf_request','tour_v2_output','post_cost_reference','post_transit_reference','post_transit_pending'):
        context.user_data.pop(_k,None)
    await _tour_v2_show_draft(message, context, '✅ AI extraction complete. Review the draft below.')
    await _tour_v2_show_outputs(message, context)


def _tour_patch_transit_from_text(raw, existing=None):
    """Parse simple owner-written Journey / Transit lines locally.

    Examples:
      Onward: Raipur to Delhi | 12:20 - 14:35
      Return: DEL to RPR | AI 1730 | 22:30 - 23:55
    Does not invent fields that are not written.
    """
    source=str(raw or '')
    block=source
    m=re.search(r'(?is)\bJourney\s*/\s*Transit\s*:\s*(.*?)(?=\n\s*(?:Package\s+Cost|Hotels?|Days?|Inclusions?|Exclusions?|PRINT\s+TYPE|DETAIL)\s*:|\Z)',source)
    if m:
        block=m.group(1)
    rows=[]
    for line in block.splitlines():
        line=line.strip().lstrip('•*- ').strip()
        if not line: continue
        lm=re.match(r'(?i)^(onward|return|connection|transit|journey)\s*(?:\d+)?\s*(?::|-)?\s*(.+)$',line)
        if not lm: continue
        jt=lm.group(1).title()
        rest=lm.group(2).strip()
        parts=[x.strip() for x in rest.split('|')]
        route_part=parts[0] if parts else rest
        service=''
        time_part=''
        if len(parts)>=3:
            service=parts[1]
            time_part=' | '.join(parts[2:])
        elif len(parts)==2:
            # If second part contains times treat it as timing, otherwise service.
            if re.search(r'\b\d{1,2}[:.]\d{2}\b.*(?:-|→|to).*\b\d{1,2}[:.]\d{2}\b',parts[1],re.I):
                time_part=parts[1]
            else:
                service=parts[1]
        rm=re.search(r'(?i)^(.+?)\s*(?:→|->|\bto\b)\s*(.+?)$',route_part)
        frm=to=''
        if rm:
            frm=rm.group(1).strip(' ,')
            to=rm.group(2).strip(' ,')
            # A natural reply often keeps times on the same line; do not absorb them into locations.
            frm=re.sub(r'\s+\b(?:[01]?\d|2[0-3])[:.]\d{2}\b.*$','',frm).strip(' ,-')
            to=re.sub(r'\s+\b(?:[01]?\d|2[0-3])[:.]\d{2}\b.*$','',to).strip(' ,-')
        else:
            codes=re.findall(r'\b[A-Z]{3}\b',route_part.upper())
            if len(codes)>=2:
                frm,to=codes[0],codes[1]
        times=re.findall(r'\b(?:[01]?\d|2[0-3])[:.]([0-5]\d)\b',time_part or rest)
        # Preserve actual matched full strings, not only minutes.
        full_times=re.findall(r'\b(?:[01]?\d|2[0-3])[:.]\d{2}\b',time_part or rest)
        dep=full_times[0].replace('.',':') if full_times else ''
        arr=full_times[1].replace('.',':') if len(full_times)>1 else ''
        flight_no=''
        fm=re.search(r'\b([A-Z]{2,3})\s*[- ]?\s*(\d{2,4})\b',service.upper())
        if fm:
            flight_no=f'{fm.group(1)} {fm.group(2)}'
        row={
            'journey_type': jt,
            'segment_mode': 'Flight' if flight_no else ('Train' if re.search(r'(?i)\btrain\b',service) else 'Transit'),
            'route': f'{frm} → {to}' if frm and to else route_part,
            'from': frm,
            'to': to,
            'departure': dep,
            'arrival': arr,
        }
        if flight_no: row['flight_number']=flight_no
        elif service: row['carrier']=service
        rows.append(row)
    return rows


def _tour_core_draft_signature(raw):
    """Return the non-cost/non-transit core of an editable Tour draft.

    This lets a resent full draft print locally when the owner only changed costing,
    transit or PRINT TYPE/DETAIL. Hotel/day-plan edits still fall through to Gemini.
    """
    s=str(raw or '')
    marker=re.search(r'(?i)AI-completed itinerary draft\s*:',s)
    if marker:
        s=s[marker.start():]
    # Journey and costing are parsed deterministically elsewhere, so exclude them
    # from the core-comparison used to decide whether Gemini is necessary.
    s=re.sub(r'(?is)\*?Journey\s*/\s*Transit\s*:\*?.*?(?=\n\s*\*?Package\s+Cost\s*:\*?|\Z)','',s)
    s=re.sub(r'(?is)\*?Package\s+Cost\s*:\*?.*?(?=\n\s*Edit this final draft|\Z)','',s)
    s=re.sub(r'(?is)\n\s*Edit this final draft.*$','',s)
    # Controls/instruction prose are not itinerary facts.
    s=re.sub(r'(?im)^.*(?:PRINT\s+TYPE|DETAIL\s*:|EDIT OR REPLY NATURALLY|I will understand the reply|For costing:|For journeys/transits).*$', '', s)
    s=re.sub(r'[*_`]+','',s)
    s=re.sub(r'\s+',' ',s).strip().lower()
    return s


def _tour_reply_is_simple_cost_transit_patch(raw):
    """True when the owner reply only changes costing/transit/print controls.
    Such replies should never invoke Gemini or rebuild the draft.
    """
    s=str(raw or '')
    # Full draft markers imply there may be hotel/day edits that need the smart editor.
    if re.search(r'(?im)^\s*\*?(?:Guest|Tour|Destination|Hotels?\s*\(|Days?\s*\(|AI Inclusions|AI Exclusions)\*?\s*:',s):
        return False
    allowed_signal=bool(re.search(r'(?i)\b(?:Journey\s*/\s*Transit|Package\s+Cost|Onward\b|Return\b|Inbound\b|Outbound\b|Connection\b|Transit\b|Adult\b|CWB\b|CNB\b|EB\b|extra\s*bed|child\s+with\s+bed|child\s+(?:no|without)\s+bed|PRINT\s+TYPE|DETAIL)\b',s))
    return allowed_signal or _looks_like_freeform_transit_lines(s)


async def _tour_v2_process_edited_final(message, context, edited_text):
    """Apply the owner's final Tour reply and print directly.

    Simple costing/transit replies are processed entirely locally. Gemini is used
    only when the owner actually edits hotels, day plans or other free-form draft data.
    """
    current=copy.deepcopy(context.user_data.get('itinerary') or {})
    if not current:
        context.user_data.pop('tour_v2_phase',None)
        await message.reply_text('❌ The current Tour draft expired. Please send the supplier file again.',reply_markup=main_keyboard())
        return

    raw=str(edited_text or '').strip()
    low=raw.lower()
    # Preserve the current selected/default mode unless the reply explicitly changes it.
    current_mode=str(current.get('document_mode') or context.user_data.get('pending_tour_document_mode') or 'quotation').lower()
    current_detail=str(current.get('detail_level') or context.user_data.get('pending_tour_pdf_detail') or 'basic').lower()
    mode='voucher' if re.search(r'(?i)\bprint\s*type\s*[:=-]?\s*voucher\b|\btour\s+voucher\b',raw) else ('quotation' if re.search(r'(?i)\bprint\s*type\s*[:=-]?\s*quotation\b|\btour\s+quotation\b',raw) else current_mode)
    detail='detailed' if re.search(r'(?i)\bdetail\s*[:=-]?\s*detailed\b|\bdetailed\s+(?:pdf|itinerary|plan)\b',raw) else ('basic' if re.search(r'(?i)\bdetail\s*[:=-]?\s*basic\b|\bbasic\s+(?:pdf|itinerary|plan)\b',raw) else current_detail)
    if mode not in ('voucher','quotation'): mode='quotation'
    if detail not in ('basic','detailed'): detail='basic'

    status=await message.reply_text(
        '🧠 *Reading your final Tour changes...*\n\n████░░░░░░░░░░░ 25%\n\nApplying transit and costing...',
        parse_mode='Markdown')
    try:
        data=copy.deepcopy(current)

        # 1) Costing is always parsed locally FIRST and becomes authoritative.
        has_explicit_cost = bool(
            re.search(r'(?i)\b(?:adult|cwb|cnb|eb|extra\s*bed|child\s+with\s+bed|child\s+no\s+bed)\b[^\n]{0,35}\d', raw)
            or re.search(r'(?i)\d[^\n]{0,20}\b(?:adult|cwb|cnb|eb|extra\s*bed)\b', raw)
        )
        rates={}
        if has_explicit_cost:
            try: rates=_tour_v2_parse_costs(raw)
            except Exception: rates={}
        if rates:
            data=_tour_v2_apply_costs(data,rates)

        # 2) Natural Transit reply. No prefix, colon, pipe or template is required.
        # If the owner says Onward/Return/Connection (or otherwise clearly talks about
        # a journey), Gemini gets the raw reply and extracts every real sector.
        local_transit=[]
        transit_changed=False
        clear_transit=bool(re.search(r'(?i)\b(?:no\s+transit|no\s+journey|skip\s+transit|done\s+by\s+self)\b',raw))
        transit_signal=bool(re.search(r'(?i)\b(?:onward|return|inbound|outbound|connection|transit|flight|train|rail)\b',raw)) or _looks_like_freeform_transit_lines(raw)
        if clear_transit:
            data['transit']=[]
            data['transit_done_by_self']=True
            transit_changed=True
        elif transit_signal:
            # A copied final draft already has a clean Journey / Transit block. Parse
            # that block locally so resending the draft does not need another Gemini
            # call. Free-form line-by-line shorthand still uses the smart AI parser.
            local_backup=_tour_patch_transit_from_text(raw,data.get('transit'))
            structured_transit_block=bool(re.search(r'(?i)Journey\s*/\s*Transit\s*:',raw))
            if structured_transit_block and local_backup:
                local_transit=local_backup
            else:
                try:
                    await safe_status_edit(status,message,'🧠 *Understanding your journey reply...*\n\n██████░░░░░░░░░░ 38%\n\nReading the line-by-line travel sectors...',parse_mode='Markdown')
                    parsed=await _run_ai_with_retry_status(
                        message,
                        lambda: asyncio.to_thread(extract_transit_from_parts,[],raw,GEMINI_API_KEY,GEMINI_MODEL),
                        status=status)
                    local_transit=list((parsed or {}).get('transit') or [])
                except Exception:
                    logger.exception('Natural transit AI parsing failed; using local backup')
                    local_transit=local_backup
                if not local_transit:
                    local_transit=local_backup
            if local_transit:
                data['transit']=local_transit
                data['transit_done_by_self']=False
                transit_changed=True

        # 3) Only call the general Tour editor for real hotel/day-plan/content edits.
        # If the owner resent the full draft but only changed costing/transit/controls,
        # compare the non-cost/non-transit core and print locally without Gemini.
        core_unchanged = (_tour_core_draft_signature(raw) == _tour_core_draft_signature(build_confirmation(current)))
        simple_final_patch = _tour_reply_is_simple_cost_transit_patch(raw) or core_unchanged
        if not simple_final_patch:
            await safe_status_edit(status,message,'🧠 *Reading your final Tour changes...*\n\n████████░░░░░░░░ 50%\n\nApplying hotel/day-plan edits...',parse_mode='Markdown')
            instruction=(
                'The user has copied, edited and resent the FULL FINAL TOUR DRAFT. Treat the edited text as authoritative. '
                'Update the current tour data to match it. Preserve any current factual field that is not contradicted. '
                'Do not invent missing facts. Costing is already handled locally and must not be removed. '
                'Transit already parsed locally must not be removed unless the edited text explicitly changes it.\n\n'
                'EDITED FINAL DRAFT:\n'+raw
            )
            updated,_=await _run_ai_with_retry_status(
                message,
                lambda: asyncio.to_thread(apply_edit,'package',data,instruction,GEMINI_API_KEY,GEMINI_MODEL,None),
                status=status)
            ai_data=_normalize_guest_counts(updated or data)
            # Re-apply deterministic owner-entered costing/transit after AI so AI can never erase them.
            if rates:
                ai_data=_tour_v2_apply_costs(ai_data,rates)
            if local_transit:
                ai_data['transit']=local_transit
                ai_data['transit_done_by_self']=False
            if clear_transit:
                ai_data['transit']=[]; ai_data['transit_done_by_self']=True
            data=ai_data

        # Never erase existing customer costing merely because this edit did not mention price.
        if not rates and current.get('show_cost') and current.get('package_costs'):
            data['package_costs']=copy.deepcopy(current.get('package_costs'))
            data['show_cost']=True

        # Dynamic costing: when the owner starts adding customer costing, require only
        # the categories that actually exist in this package. Do not ask for irrelevant
        # CWB/CNB/EB categories, and do not print an incomplete cost table by accident.
        if data.get('show_cost'):
            missing_costs=_missing_required_package_cost_fields(data)
            if missing_costs:
                context.user_data['itinerary']=data
                context.user_data['tour_v2_phase']='awaiting_edited_final'
                labels=[label for _,label in missing_costs]
                need=', '.join(labels[:-1]) + (f" and {labels[-1]}" if len(labels)>1 else labels[0])
                accepted=[]
                if rates:
                    lab={'per_adult':'Adult','per_child':'Child','per_child_cwb':'CWB','per_child_cnb':'CNB','per_extra_bed':'EB'}
                    accepted=[f"{lab.get(k,k)} ₹{float(v):,.0f}" for k,v in rates.items()]
                prefix=("I saved " + ', '.join(accepted) + ". ") if accepted else ''
                await safe_status_edit(status,message,f"💰 {prefix}This package also needs {need}.\n\nReply naturally with the remaining rate(s). No /start and no special format is required.")
                return

        if detail=='detailed' and str(data.get('detail_level') or '').lower()!='detailed':
            await safe_status_edit(status,message,'✨ *Changes applied.*\n\n██████████░░░░░░ 65%\n\nExpanding the day plan to Detailed level...',parse_mode='Markdown')
            old_name=str(data.get('client_name') or '')
            saved_costs=copy.deepcopy(data.get('package_costs'))
            saved_show=bool(data.get('show_cost'))
            saved_transit=copy.deepcopy(data.get('transit'))
            data=await _run_ai_with_retry_status(
                message,lambda: asyncio.to_thread(enhance_package_itinerary,data,GEMINI_API_KEY,GEMINI_MODEL,'detailed'),status=status)
            data['client_name']=old_name or str(data.get('client_name') or '')
            if saved_costs:
                data['package_costs']=saved_costs; data['show_cost']=saved_show
            if saved_transit:
                data['transit']=saved_transit

        data['detail_level']=detail
        data['document_mode']=mode
        context.user_data['itinerary']=data
        context.user_data['pending_tour_document_mode']=mode
        context.user_data['pending_tour_pdf_detail']=detail
        context.user_data['pending_tour_pdf_no_cost']=not bool(data.get('show_cost') and data.get('package_costs'))
        context.user_data['tour_v2_phase']='printing_direct'

        # Show what was actually accepted, rather than sending another draft.
        accepted=[]
        if rates:
            labels={'per_adult':'Adult','per_child_cwb':'CWB','per_child_cnb':'CNB','per_extra_bed':'EB','per_child':'Child'}
            accepted.append('Costing: '+', '.join(f"{labels.get(k,k)} ₹{float(v):,.0f}" for k,v in rates.items()))
        if data.get('transit'):
            accepted.append(f"Transit: {len(data.get('transit') or [])} journey sector(s) understood and saved")
        progress_note='\n'.join('• '+x for x in accepted) if accepted else '• Draft changes saved'
        await safe_status_edit(
            status,message,
            f'✅ *Changes applied.*\n\n{progress_note}\n\n██████████████░░ 88%\n\nGenerating {detail.title()} Tour {"Voucher" if mode=="voucher" else "Quotation"}...',
            parse_mode='Markdown')

        ref,_=await generate_tour_pdf_final(message,context,data,detail,not bool(data.get('show_cost') and data.get('package_costs')))
        context.user_data['tour_v2_phase']='complete'
        await safe_status_edit(status,message,'✅ *PDF delivered successfully.*\n\n████████████████ 100%',parse_mode='Markdown')
        await message.reply_text('✅ Ready for the next supplier file.',reply_markup=main_keyboard())
    except Exception as exc:
        logger.exception('Direct edited Tour draft failed')
        context.user_data['tour_v2_phase']='awaiting_edited_final'
        await safe_status_edit(status,message,f'⚠️ I could not finish this Tour update. Your current draft is still active — reply again; /start is not required.\n\nReason: {str(exc)[:600]}',parse_mode='Markdown')


async def _tour_v2_apply_missing_reply(message, context, reply_text):
    data=context.user_data.get("itinerary") or {}
    missing=context.user_data.get("tour_v2_missing") or []
    updated,changed=apply_missing_accommodation_locally(data,reply_text,missing)
    if changed:
        context.user_data["itinerary"]=_normalize_guest_counts(updated)
        context.user_data.pop("tour_v2_missing",None)
        await message.reply_text("✅ Missing hotel/room details added locally.")
        await _tour_v2_show_draft(message, context, "✅ Draft updated.")
        await _tour_v2_show_outputs(message, context)
        return
    status=await message.reply_text("🤖 This reply needs interpretation, using AI only for this correction...")
    instruction=(
        "The user is supplying only missing accommodation information for the current tour draft. "
        "Apply these facts to the appropriate hotel rows. Do not change unrelated itinerary facts. "
        f"Missing fields were: {', '.join(missing)}. User reply: {reply_text}"
    )
    updated,_=await _run_ai_with_retry_status(message, lambda: asyncio.to_thread(apply_edit,"package",data,instruction,GEMINI_API_KEY,GEMINI_MODEL,None), status=status)
    context.user_data["itinerary"]=_normalize_guest_counts(updated or data)
    context.user_data.pop("tour_v2_missing",None)
    await safe_status_edit(status,message,"✅ Missing details added to the draft.")
    await _tour_v2_show_draft(message, context, "✅ Draft updated.")
    await _tour_v2_show_outputs(message, context)


async def _tour_v2_extract_journey(message, context, stage, file_path=None, source_text=""):
    parts=[]; paths=[]
    if file_path:
        paths=[str(file_path)]
        parts=[{"path":str(file_path),"mime_type":"application/pdf" if str(file_path).lower().endswith(".pdf") else "image/jpeg"}]
    status=await message.reply_text("⚡ Reading the journey locally first...")
    try:
        local=await asyncio.to_thread(parse_transit_files_local,paths,source_text)
        rows=local.get("transit") or []
        used_ai=False
        if not rows:
            used_ai=True
            await safe_status_edit(status,message,"🤖 Local parser could not confidently read this source. Using AI fallback once...")
            result=await _run_with_progress(status,message,lambda: asyncio.to_thread(extract_transit_from_parts,parts,source_text,GEMINI_API_KEY,GEMINI_MODEL),["✈️ Extracting sectors, airports, terminals and timings...","🔎 Organizing journey details..."],25,92)
            rows=result.get("transit") or []
        else:
            for q in paths:
                try: Path(q).unlink(missing_ok=True)
                except Exception: pass
        rows=_tour_v2_set_journey_type(rows,stage)
        data=context.user_data.get("itinerary") or {}
        existing=list(data.get("transit") or [])
        stage_label={"onward":"Onward","return":"Return","connection":"Connection"}[stage]
        existing=[r for r in existing if str(r.get("_v2_stage") or "").lower()!=stage.lower()]
        data["transit"]=existing+rows
        context.user_data["itinerary"]=data
        mode="AI fallback" if used_ai else "local parser"
        await safe_status_edit(status,message,f"✅ {stage_label} journey added • {len(rows)} sector(s) • {mode}.")
        await _tour_v2_show_draft(message,context,f"✅ {stage_label} journey added.")
        if stage=="onward": await _tour_v2_ask_return(message,context)
        elif stage=="return": await _tour_v2_ask_connection(message,context)
        else: await _tour_v2_show_outputs(message,context)
    except Exception as exc:
        logger.exception("Tour V2 journey extraction failed")
        await safe_status_edit(status,message,f"❌ Could not extract this journey: {str(exc)[:600]}")
        await message.reply_text("Please resend the journey source, or use the No Journey button.",reply_markup=_tour_v2_stage_keyboard(stage))



def _post_transit_keyboard():
    return ReplyKeyboardRemove()


_PACKAGE_COST_FIELDS = [
    ("per_adult", "Adult", "adult_count"),
    ("per_child", "Child", "child_count"),
    ("per_child_cwb", "CWB", "child_cwb_count"),
    ("per_child_cnb", "CNB", "child_cnb_count"),
    ("per_extra_bed", "EB", "extra_bed_count"),
]

def _required_package_cost_fields(data):
    """Return only customer-rate categories that actually exist in this package."""
    d=_normalize_guest_counts(copy.deepcopy(data or {}))
    required=[]
    for field,label,count_key in _PACKAGE_COST_FIELDS:
        try: count=int(d.get(count_key) or 0)
        except Exception: count=0
        if count>0:
            required.append((field,label))
    # Most tours have adults. If passenger counts were not extractable, asking Adult is
    # safer and simpler than showing every possible category.
    if not required:
        required=[("per_adult","Adult")]
    return required

def _package_cost_prompt(data):
    labels=[label for _,label in _required_package_cost_fields(data)]
    if len(labels)==1:
        need=labels[0]
    elif len(labels)==2:
        need=f"{labels[0]} and {labels[1]}"
    else:
        need=', '.join(labels[:-1]) + f" and {labels[-1]}"
    return f"Please send the customer rate for {need} in one normal message. No heading or fixed format is required."

def _missing_required_package_cost_fields(data):
    rows=list((data or {}).get('package_costs') or [])
    row=rows[0] if rows else {}
    missing=[]
    for field,label in _required_package_cost_fields(data):
        value=str(row.get(field) or '').replace(',','').strip()
        try: ok=float(value)>0
        except Exception: ok=False
        if not ok:
            missing.append((field,label))
    return missing

def _looks_like_freeform_transit_lines(text):
    """Detect short line-by-line flight/train sectors without requiring prefixes."""
    raw=str(text or '').strip()
    if not raw:
        return False
    lines=[x.strip() for x in raw.splitlines() if x.strip()]
    # A full itinerary draft can contain many unrelated lines. Here we only need to
    # recognize compact sector replies or an explicit Journey/Transit block.
    explicit=bool(re.search(r'(?i)journey\s*/\s*transit|transit\s*:',raw))
    hits=0
    for line in lines:
        codes=re.findall(r'\b[A-Z]{3}\b',line.upper())
        times=re.findall(r'\b(?:[01]?\d|2[0-3])[:.]?[0-5]\d\b',line)
        service=bool(re.search(r'\b[A-Z]{2,3}\s*[- ]?\s*\d{2,4}\b',line.upper()) or re.search(r'(?i)\b(?:train|rail|flight)\b',line))
        route_words=bool(re.search(r'(?i)\b(?:to|→|->)\b',line))
        if (len(codes)>=2 and (len(times)>=1 or service)) or (service and len(times)>=2) or (route_words and len(times)>=2):
            hits+=1
    return explicit or hits>=1

def _package_has_customer_costing(data):
    """True only when every rate category that exists in this package has a customer rate."""
    data=data or {}
    if not data.get('show_cost'):
        return False
    return not bool(_missing_required_package_cost_fields(data))


def _format_transit_preview(rows):
    """Human-readable preview matching the fields used by the PDF transit table."""
    blocks=[]
    for idx,row in enumerate(rows or [],1):
        r=row or {}
        jt=str(r.get('journey_type') or 'Transit').strip().title()
        mode=str(r.get('segment_mode') or 'Flight').strip().title()
        carrier=str(r.get('carrier') or r.get('airline') or '').strip()
        number=str(r.get('flight_number') or r.get('train_number') or '').strip()
        service=' '.join(x for x in (carrier,number) if x).strip() or mode
        route=str(r.get('route') or '').strip()
        if not route:
            a=str(r.get('from') or '').strip(); b=str(r.get('to') or '').strip()
            route=' → '.join(x for x in (a,b) if x)
        dep=str(r.get('departure') or '').strip(); arr=str(r.get('arrival') or '').strip()
        dep_air=str(r.get('from_airport') or '').strip(); arr_air=str(r.get('to_airport') or '').strip()
        dep_t=str(r.get('departure_terminal') or '').strip(); arr_t=str(r.get('arrival_terminal') or '').strip()
        if dep_t and 'terminal' not in dep_t.lower(): dep_t='Terminal '+dep_t
        if arr_t and 'terminal' not in arr_t.lower(): arr_t='Terminal '+arr_t
        dep_extra=' • '.join(x for x in (dep_air,dep_t) if x)
        arr_extra=' • '.join(x for x in (arr_air,arr_t) if x)
        date=str(r.get('date') or '').strip()
        aircraft=str(r.get('aircraft') or '').strip()
        pnr=str(r.get('pnr') or '').strip()
        lines=[f"*{jt} {idx} • {mode}*", f"{service}"]
        if route: lines.append(f"Route: {route}")
        if date: lines.append(f"Date: {date}")
        if dep or dep_extra: lines.append(f"Departure: {dep or '—'}" + (f" • {dep_extra}" if dep_extra else ''))
        if arr or arr_extra: lines.append(f"Arrival: {arr or '—'}" + (f" • {arr_extra}" if arr_extra else ''))
        if aircraft: lines.append(f"Aircraft: {aircraft}")
        if pnr: lines.append(f"PNR: {pnr}")
        blocks.append('\n'.join(lines))
    return '\n\n'.join(blocks)

def _post_transit_confirm_keyboard(reference, needs_costing):
    rows=[]
    if needs_costing:
        rows.append([InlineKeyboardButton('💰 Add Costing', callback_data=f'post_transit_cost:{reference}'),
                     InlineKeyboardButton('📄 Make PDF', callback_data=f'post_transit_make:{reference}')])
    else:
        rows.append([InlineKeyboardButton('📄 Make PDF', callback_data=f'post_transit_make:{reference}')])
    rows.append([InlineKeyboardButton('✏️ Re-enter Transit', callback_data=f'post_transit:{reference}')])
    return InlineKeyboardMarkup(rows)

async def _regenerate_saved_package(message, reference, record, data, caption):
    record=copy.deepcopy(record or {})
    record['data']=data
    update_record(reference,record)
    page_size=record.get('page_size') or 'A4'
    footer_mode=record.get('footer_mode') or _default_footer_mode('package')
    logo_enabled=bool(record.get('logo_enabled',True)) and not bool(record.get('agency_removed',False))
    record['_clean_agency']=bool(record.get('agency_removed',False))
    final,scale,filename=await _render_saved_pdf(
        reference,record,data,'package',record.get('fare'),page_size,footer_mode,logo_enabled,
        text_scale_override=record.get('text_scale'),logo_scale_override=record.get('logo_scale'),
        auto_fit=False,last_page=record.get('terms_choice') or get_tour_last_page())
    record.pop('_clean_agency',None)
    record.update({'filename':filename,'data':data,'text_scale':scale})
    update_record(reference,record)
    with open(final,'rb') as fh:
        sent_pdf=await message.reply_document(document=fh,filename=filename,caption=_record_caption(reference,caption),parse_mode='Markdown',reply_markup=generated_document_keyboard(reference,'package'))
    _register_reference_message(reference, sent_pdf)

async def _process_post_generated_transit_text(message, context, source_text):
    reference=context.user_data.get("post_transit_reference")
    record=load_record(reference) if reference else None
    if not record or record.get("type")!="package":
        context.user_data.pop("post_transit_reference",None)
        context.user_data.pop("post_transit_pending",None)
        await message.reply_text("❌ The saved Tour is no longer available. Please use the latest generated PDF.",reply_markup=main_keyboard()); return
    status=await message.reply_text("✈️ Reading your transit details...")
    try:
        # Fast local parser first. AI is used only when the short/mixed text cannot be
        # confidently understood locally (airport-code-only, train, round trip, etc.).
        local=await asyncio.to_thread(parse_transit_files_local,[],source_text)
        rows=local.get("transit") or []
        confidence=float(local.get("local_confidence") or 0)
        raw_lower=str(source_text or '').lower()
        # Force the smarter pass for patterns where a deterministic parser can look
        # confident while assigning the wrong sector (round trips, multiple sectors,
        # compact T2/T3 terminal notation, or trains).
        routes=[(str(r.get('from') or '').upper(),str(r.get('to') or '').upper()) for r in rows]
        line_count=len([ln for ln in str(source_text or '').splitlines() if ln.strip()])
        suspicious=(line_count>1 or len(rows)>1 or len(set(routes))<len(routes) or
                    bool(re.search(r"\b(return|round\s*trip|back|inbound|train|rail)\b",raw_lower,re.I)) or
                    bool(re.search(r"\bT[0-9A-Z]{1,3}\b",str(source_text or ''),re.I)))
        if not rows or confidence < 0.72 or suspicious:
            await safe_status_edit(status,message,"🤖 Understanding your short / mixed transit reply...")
            result=await _run_ai_with_retry_status(
                message,
                lambda: asyncio.to_thread(extract_transit_from_parts,[],source_text,GEMINI_API_KEY,GEMINI_MODEL),
                status=status)
            ai_rows=result.get("transit") or []
            if ai_rows:
                rows=ai_rows
        if not rows:
            await safe_status_edit(status,message,"❌ I could not understand a transit sector. Please reply again in any short form, for example: `DEL 6:30 RPR 8:15 AI1729`.")
            # IMPORTANT: keep post_transit_reference alive so the very next reply retries.
            return

        data=copy.deepcopy(record.get("data") or {})
        existing=list(data.get("transit") or [])
        seen={(str(r.get('date','')).lower(),str(r.get('flight_number','')).lower(),str(r.get('from','')).lower(),str(r.get('to','')).lower(),str(r.get('departure','')).lower()) for r in existing}
        added=[]
        for i,row in enumerate(rows):
            r=dict(row or {})
            if not r.get('journey_type'):
                jt=str(r.get('type') or '').lower()
                if 'return' in jt: r['journey_type']='Return'
                elif 'connect' in jt: r['journey_type']='Connection'
                else: r['journey_type']='Onward' if not existing and i==0 else 'Connection'
            key=(str(r.get('date','')).lower(),str(r.get('flight_number','')).lower(),str(r.get('from','')).lower(),str(r.get('to','')).lower(),str(r.get('departure','')).lower())
            if key not in seen:
                seen.add(key); existing.append(r); added.append(r)
        if not added and rows:
            # User may intentionally re-enter/replace the same transit. Show the parsed
            # result rather than leaving the workflow in a dead state.
            added=rows
        data['transit']=existing
        data['transit_done_by_self']=False

        preview=_format_transit_preview(added)
        context.user_data['post_transit_pending']={'reference':reference,'data':data,'preview':preview}
        context.user_data.pop('post_transit_reference',None)
        await safe_status_edit(status,message,"✅ Transit understood.")
        await message.reply_text(
            "✈️ *Transit details that will be printed*\n\n" + preview,
            parse_mode='Markdown')

        if _package_has_customer_costing(data):
            await message.reply_text("💰 Costing is already available. Regenerating the PDF now...")
            await _regenerate_saved_package(message,reference,record,data,'📄 Tour PDF regenerated with transit')
            context.user_data.pop('post_transit_pending',None)
            await message.reply_text("✅ Transit added and PDF regenerated.",reply_markup=main_keyboard())
        else:
            await message.reply_text(
                "Costing has not been added yet. " + _package_cost_prompt(data) + " You can add it now, or make the PDF without costing.",
                reply_markup=_post_transit_confirm_keyboard(reference,True))
    except Exception as exc:
        logger.exception('Post-generated transit update failed')
        # Clear only pending parsed data; keep the original reference so the next text
        # reply automatically retries instead of forcing /start.
        context.user_data.pop('post_transit_pending',None)
        context.user_data['post_transit_reference']=reference
        await safe_status_edit(status,message,f"⚠️ Transit could not be completed. Please reply again; your Tour is still active.\n\nReason: {str(exc)[:400]}")

async def _process_post_generated_costing(message, context, cost_text):
    reference=context.user_data.get('post_cost_reference')
    record=load_record(reference) if reference else None
    if not record or record.get('type')!='package':
        context.user_data.pop('post_cost_reference',None)
        await message.reply_text('❌ Saved Tour reference is no longer available.',reply_markup=main_keyboard()); return
    try:
        rates=_tour_v2_parse_costs(cost_text)
    except ValueError as exc:
        await message.reply_text(f"❌ I could not read a rate from that reply.\n\n{_package_cost_prompt(record.get('data') or {})}")
        return
    status=await message.reply_text('💰 Saving your customer costing...')
    try:
        data=_tour_v2_apply_costs(record.get('data') or {},rates)
        data['show_cost']=True
        missing=_missing_required_package_cost_fields(data)
        if missing:
            record['data']=data; update_record(reference,record)
            context.user_data['post_cost_reference']=reference
            labels=[label for _,label in missing]
            need=', '.join(labels[:-1]) + (f" and {labels[-1]}" if len(labels)>1 else labels[0])
            await safe_status_edit(status,message,f"✅ Saved. This package still needs {need}.\n\nReply naturally with the remaining rate(s).")
            return
        await safe_status_edit(status,message,'💰 All required customer rates are available. Regenerating the PDF...')
        await _regenerate_saved_package(message,reference,record,data,'📄 Tour PDF regenerated with costing')
        context.user_data.pop('post_cost_reference',None)
        await safe_status_edit(status,message,'✅ Costing added successfully.')
        await message.reply_text('Ready.',reply_markup=main_keyboard())
    except Exception as exc:
        logger.exception('Post-generated costing failed')
        await safe_status_edit(status,message,f"❌ Costing regeneration failed: {str(exc)[:600]}")


async def _process_post_generated_hotel_costing(message, context, cost_text):
    reference=context.user_data.get('post_hotel_cost_reference')
    record=load_record(reference) if reference else None
    if not record or record.get('type')!='hotel':
        context.user_data.pop('post_hotel_cost_reference',None)
        await message.reply_text('❌ Saved Hotel reference is no longer available.',reply_markup=main_keyboard()); return
    try:
        supplier=float(record.get('fare') or 0)
        hotel_cost=_parse_hotel_cost_input(cost_text,supplier)
    except ValueError as exc:
        await message.reply_text(f'❌ {exc}\n\nExample: `Room 8500, EB 1200, Total 18200`',parse_mode='Markdown')
        return
    status=await message.reply_text('🏨 Adding Hotel costing and regenerating the voucher...')
    try:
        data=copy.deepcopy(record.get('data') or {})
        data['customer_hotel_cost']=hotel_cost
        total=float(hotel_cost.get('total') or 0) or None
        page_size=record.get('page_size') or 'A4'
        footer_mode=record.get('footer_mode') or (_default_footer_mode('hotel') if record.get('footer') else 'none')
        logo_enabled=bool(record.get('logo_enabled',True)) and not bool(record.get('agency_removed',False))
        record['_clean_agency']=bool(record.get('agency_removed',False))
        final,selected_scale,filename=await _render_saved_pdf(reference,record,data,'hotel',total,page_size,footer_mode,logo_enabled,text_scale_override=record.get('text_scale'),logo_scale_override=record.get('logo_scale'),auto_fit=False)
        record.pop('_clean_agency',None)
        record.update({'filename':filename,'data':data,'fare':total,'text_scale':selected_scale})
        update_record(reference,record)
        context.user_data.pop('post_hotel_cost_reference',None)
        await safe_status_edit(status,message,'✅ Hotel costing added successfully.')
        with open(final,'rb') as fh:
            sent_pdf=await message.reply_document(fh,filename=filename,caption=_record_caption(reference,'🏨 Updated MyTourBazar Hotel',f'Hotel Total: INR {total:,.0f}' if total else ''),parse_mode='Markdown',reply_markup=generated_document_keyboard(reference,'hotel'))
        _register_reference_message(reference, sent_pdf)
        await message.reply_text('✅ Ready for the next request.',reply_markup=main_keyboard())
    except Exception as exc:
        logger.exception('Post-generated Hotel costing failed')
        context.user_data['post_hotel_cost_reference']=reference
        await safe_status_edit(status,message,f'⚠️ Hotel costing could not be regenerated. Reply again; /start is not required.\n\nReason: {str(exc)[:500]}')


def _tour_v2_parse_costs(text):
    raw=str(text or "").replace("₹","").replace(",","")
    aliases={
        "per_adult":["per adult","adult","adults","adt"],
        "per_child_cnb":["cnb","child no bed","child without bed"],
        "per_child_cwb":["cwb","child with bed"],
        "per_extra_bed":["extra bed","eb"],
        "per_child":["child","children"],
    }
    found={}
    for field,names in aliases.items():
        for name in sorted(names,key=len,reverse=True):
            m=re.search(
                rf"\b{re.escape(name)}\b\s*(?:(?:rate|cost|price|fare)\s*)?(?:(?:is|should\s+be|will\s+be|at|[:=\-])\s*)?(?:rs\.?\s*)?([0-9]+(?:\.\d+)?)",
                raw,re.I)
            if not m:
                m=re.search(rf"(?:rs\.?\s*)?([0-9]+(?:\.\d+)?)\s*(?:for|per|is\s+for)?\s*\b{re.escape(name)}\b",raw,re.I)
            if m:
                _amount=float(m.group(1))
                # Tiny values are almost always guest counts (e.g. "4 adults"), not selling rates.
                if _amount >= 100:
                    found[field]=_amount; break
    if not found:
        raise ValueError("I could not read the costing. Example: Adult 25000, CNB 12000, CWB 18000, EB 8000")
    return found


def _tour_v2_apply_costs(data, rates):
    """Apply only the customer cost categories explicitly mentioned.

    The first customer-cost reply removes hidden supplier pricing. Later replies are
    incremental: e.g. entering only CWB keeps an already-saved Adult rate unchanged.
    """
    data=copy.deepcopy(data or {})
    costs=copy.deepcopy(data.get("package_costs") or [{"option":"Package","currency":"INR"}])
    if not costs:
        costs=[{"option":"Package","currency":"INR"}]
    row=costs[0]
    if not bool(data.get("show_cost")):
        # First customer rate: discard all supplier/internal price fields before saving it.
        for _field in ("per_adult","per_child","per_child_cwb","per_child_cnb","per_extra_bed",
                       "supplier_total","total_cost","final_total","markup_total"):
            row.pop(_field,None)
    else:
        # Existing customer rates remain, but supplier/markup totals never leak back in.
        for _field in ("supplier_total","total_cost","final_total","markup_total"):
            row.pop(_field,None)
    for k,v in (rates or {}).items():
        if float(v or 0)>0:
            row[k]=f"{v:,.0f}"
    row["currency"]="INR"
    data["package_costs"]=costs
    data["show_cost"]=bool(data.get("show_cost") or rates)
    return data


def _tour_reconcile_ai_customer_costs(old_data, new_data, instruction):
    """Convert Gemini-understood Tour cost edits into clean customer selling rates.

    The old supplier/markup workflow is intentionally not used. When Gemini understands a
    natural request such as "make adult forty three thousand seven hundred" or a mixed
    hotel+cost edit, only the changed customer rate fields are copied into the cost box.
    """
    old_data=old_data or {}
    new_data=copy.deepcopy(new_data or {})
    text=str(instruction or '')
    # Explicit hide/remove wording wins and does not destroy saved rates.
    if re.search(r'(?i)\b(?:hide|remove|delete|do\s*not\s*show|without)\b.{0,20}\b(?:cost|costing|price|rate)\b', text):
        new_data['show_cost']=False
        return new_data, {}

    fields=('per_adult','per_child','per_child_cwb','per_child_cnb','per_extra_bed')
    old_rows=old_data.get('package_costs') or []
    new_rows=new_data.get('package_costs') or []
    old_row=old_rows[0] if old_rows and isinstance(old_rows[0],dict) else {}
    new_row=new_rows[0] if new_rows and isinstance(new_rows[0],dict) else {}
    changed={}
    for field in fields:
        ov=_num_cost(old_row.get(field))
        nv=_num_cost(new_row.get(field))
        if nv > 0 and abs(nv-ov) >= 0.5:
            changed[field]=nv

    # Gemini may explicitly enable the customer cost box even when a requested value
    # happens to equal the supplier value. In that case, preserve all positive rates it
    # returned as customer-authored rates.
    if not changed and bool(new_data.get('show_cost')) and not bool(old_data.get('show_cost')):
        for field in fields:
            nv=_num_cost(new_row.get(field))
            if nv > 0:
                changed[field]=nv

    if changed:
        # Base the first customer-cost update on the OLD show_cost state so hidden supplier
        # prices are scrubbed before the customer values are saved. Other AI edits remain.
        new_data['show_cost']=bool(old_data.get('show_cost'))
        new_data=_tour_v2_apply_costs(new_data,changed)
        new_data['show_cost']=True
    return new_data, changed


async def _tour_v2_finish_selected_output(message, context, cost_text):
    try:
        rates=_tour_v2_parse_costs(cost_text)
    except ValueError as exc:
        await message.reply_text(f"❌ {exc}\n\nSend the costing again in a normal message.")
        return
    data=_tour_v2_apply_costs(context.user_data.get("itinerary") or {},rates)
    choice=context.user_data.get("tour_v2_output") or {"output":"whatsapp","detail":"basic"}
    detail=choice.get("detail","basic"); output=choice.get("output","whatsapp")
    if str(data.get("detail_level") or "basic").lower()!=detail:
        status=await message.reply_text(f"✨ Preparing the {detail} itinerary...")
        old_name=str(data.get("client_name") or "")
        data=await _run_ai_with_retry_status(message,lambda: asyncio.to_thread(enhance_package_itinerary,data,GEMINI_API_KEY,GEMINI_MODEL,detail),status=status)
        data["client_name"]=old_name or str(data.get("client_name") or "")
        data["detail_level"]=detail
        await safe_status_edit(status,message,"✅ Itinerary detail level ready.")
    context.user_data["itinerary"]=data
    context.user_data["tour_v2_phase"]="complete"
    if output=="whatsapp":
        await reply_text_chunked(message,build_whatsapp_itinerary(data,detail),parse_mode="Markdown")
        await message.reply_text("✅ Final WhatsApp itinerary generated.",reply_markup=main_keyboard())
    else:
        context.user_data["pending_tour_pdf_detail"]=detail
        context.user_data["pending_tour_pdf_no_cost"]=False
        context.user_data["pending_tour_document_mode"]="itinerary"
        await generate_tour_pdf_final(message,context,data,detail,False)

async def process_sources(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get('_source_processing') == 'tour':
        return ConversationHandler.END if 'tour' != 'tour' else None
    context.user_data['_source_processing'] = 'tour'
    _cancel_source_auto_process(context)
    if not GEMINI_API_KEY:
        await update.message.reply_text(
            "❌ GEMINI_API_KEY is not configured in .env.",
            reply_markup=main_keyboard()
        )
        return

    # Always create the live progress message at the BOTTOM of the chat when real
    # processing begins. Editing the earlier "source received" acknowledgement made
    # the owner scroll upward to watch progress.
    status = await update.message.reply_text(
        "🚀 *Preparing your MyTourBazar itinerary...*\n\n"
        "░░░░░░░░░░░░░░░░ 0%\n⏱️ 00:00\n\n"
        "📥 Preparing supplier material...", parse_mode="Markdown"
    )
    context.user_data['_source_status_message'] = status
    started = time.monotonic()

    async def progress(pct, label):
        elapsed = int(time.monotonic() - started)
        mm, ss = divmod(elapsed, 60)
        filled = min(16, round(pct / 100 * 16))
        bar = "█" * filled + "░" * (16 - filled)
        try:
            await safe_status_edit(status, update.message, 
                f"🤖 *Creating your MyTourBazar itinerary...*\n\n"
                f"{bar} {pct}%\n⏱️ {mm:02d}:{ss:02d}\n\n{label}",
                parse_mode="Markdown"
            )
        except Exception:
            pass

    try:
        await progress(10, "📥 Reading supplier material...")
        text = context.user_data.get("source_text", "")
        guest_name = context.user_data.get("guest_name", "")
        text = f"AUTHORITATIVE GUEST / CLIENT NAME: {guest_name}\n\n" + text
        files = context.user_data.get("media_files", [])
        flight_files = context.user_data.get("flight_files", [])
        flight_text = context.user_data.get("flight_text", "")

        # PERFORMANCE MODE: selectable PDFs are converted to local text and are not sent
        # as multimodal files. Only scanned/visual sources are uploaded to AI.
        parts, text, perf_stats = prepare_supplier_for_ai(files, text, max_chars=120000)

        # Flight screenshots still need visual interpretation.
        for f in flight_files:
            parts.append({"path": f, "mime_type": "image/jpeg"})

        if flight_files or flight_text:
            text += (
                "\n\nFLIGHT EVIDENCE BELOW/IN THE TEXT IS EXPLICIT FLIGHT EVIDENCE. "
                "Extract EVERY separate flight segment. Keep onward and return flights as separate transit objects."
                f"\n{flight_text}"
            )

        await progress(30, f"⚡ Local-first preprocessing • {perf_stats.get('local_pdfs',0)} text PDF(s), {perf_stats.get('visual_sources',0)} visual source(s)...")
        data = await _run_with_progress(status, update.message, lambda: asyncio.to_thread(extract_itinerary_from_parts, parts, text, GEMINI_API_KEY, GEMINI_MODEL), ["🧠 Structuring locally prepared supplier facts...", "📑 Organizing hotels, sightseeing, meals and transport...", "✨ Building the itinerary draft..."], 30, 58)

        explicit_guest = str(context.user_data.get("guest_name") or "").strip()
        if explicit_guest:
            data["client_name"] = explicit_guest
        else:
            data["client_name"] = str(data.get("client_name") or "").strip()
        data = _normalize_guest_counts(data)
        data["detail_level"] = "basic"
        for _day in data.get("days", []):
            _day.setdefault("optional_activities", [])

        await progress(58, "🏨 Organizing accommodation, transport and day-wise itinerary...")
        await asyncio.sleep(0.2)
        await progress(72, "🗺️ AI-writing detailed day-wise sightseeing descriptions...")
        await asyncio.sleep(0.2)
        await progress(84, "🧳 AI-building professional package inclusions from the itinerary...")
        await asyncio.sleep(0.2)
        await progress(92, "🚫 AI-building professional customer-facing exclusions...")
        await asyncio.sleep(0.2)

        # Apply any user-added inclusions/exclusions.
        data.setdefault("inclusions", [])
        data.setdefault("exclusions", [])
        for item in context.user_data.get("extra_inclusions", []):
            if item and item not in data["inclusions"]:
                data["inclusions"].append(item)
        for item in context.user_data.get("extra_exclusions", []):
            if item and item not in data["exclusions"]:
                data["exclusions"].append(item)

        await progress(98, "✨ Finalizing itinerary for your review...")
        await asyncio.sleep(0.2)

        data['show_cost'] = bool(data.get('package_costs'))
        context.user_data["itinerary"] = data
        context.user_data['_source_processing'] = None
        context.user_data['pending_special_notes_decided'] = False
        context.user_data['pending_tour_cost_decided'] = False
        elapsed = int(time.monotonic() - started)
        mm, ss = divmod(elapsed, 60)

        completion_note = (
            "Extraction is complete. Edit/resend the final draft once; the bot will apply your costing/transit changes and print directly."
            if _tour_v2_active(context) else
            "Review the draft below. Supplier cost stays internal; choose your output when ready."
        )
        await safe_status_edit(status, update.message, 
            f"✅ *Itinerary preparation complete!*\n\n"
            f"████████████████ 100%\n⏱️ {mm:02d}:{ss:02d}\n\n" + completion_note,
            parse_mode="Markdown"
        )
        if _tour_v2_active(context):
            await _tour_v2_after_initial_extract(update.message, context, data)
        else:
            await continue_tour_preprint_options(update.message, context, data)

    except Exception as exc:
        logger.exception("Extraction/enhancement failed")
        context.user_data['_source_processing'] = None
        elapsed = int(time.monotonic() - started)
        mm, ss = divmod(elapsed, 60)
        await safe_status_edit(status, update.message, 
            f"❌ *Itinerary preparation failed*\n\n⏱️ {mm:02d}:{ss:02d}\n\nReason: `{str(exc)[:800]}`",
            parse_mode="Markdown"
        )
        await update.message.reply_text("Please try 🗺️ Tour Guide again.", reply_markup=main_keyboard())


def _itinerary_detail_command(text):
    """Return basic/detailed when the owner explicitly requests an itinerary detail level."""
    low = re.sub(r"\s+", " ", str(text or "").lower().strip())
    basic_patterns = (
        r"\b(?:basic|short|brief)\s+(?:day[- ]?wise\s+)?(?:day\s+)?(?:itinerary|plan)\b",
        r"\b(?:basic|short|brief)\s+(?:pdf|whatsapp|draft)\b",
    )
    detailed_patterns = (
        r"\b(?:detailed|detail|full|expanded|more\s+detailed|more\s+detail)\s+(?:day[- ]?wise\s+)?(?:day\s+)?(?:itinerary|plan)\b",
        r"\b(?:detailed|detail|full|expanded)\s+(?:pdf|whatsapp|draft|quotation|quote|voucher)\b",
    )
    if any(re.search(p, low, re.I) for p in basic_patterns) or low in {
        "basic itinerary", "basic plan", "basic day plan", "short itinerary", "basic"
    }:
        return "basic"
    if any(re.search(p, low, re.I) for p in detailed_patterns) or low in {
        "detailed itinerary", "detailed plan", "detailed day plan",
        "more details", "make it detailed", "detailed"
    }:
        return "detailed"
    return None


def _tour_reply_variant_command(text):
    """Parse a *pure* Tour output/detail reply.

    This intentionally activates only for commands such as:
      detailed itinerary
      detailed day plan
      detailed draft
      detailed whatsapp
      detailed pdf
      detailed quotation
      basic itinerary

    A normal edit such as "change Day 3 and make it detailed" is left to the
    existing Modify & Regenerate AI editor.
    """
    low = re.sub(r"\s+", " ", str(text or "").lower().strip())
    if not low:
        return None

    detail = _itinerary_detail_command(low)
    if not detail:
        if re.search(r"\b(?:detailed|detail|expanded|full)\b", low):
            detail = "detailed"
        elif re.search(r"\b(?:basic|short|brief)\b", low):
            detail = "basic"

    output = None
    if re.search(r"\b(?:whatsapp|whats\s*app|text\s+version|text\s+itinerary)\b", low):
        output = "whatsapp"
    elif re.search(r"\b(?:pdf|print)\b", low):
        output = "pdf"
    elif re.search(r"\bdraft\b", low):
        output = "draft"

    mode = None
    if re.search(r"\b(?:quotation|quote)\b", low):
        mode = "quotation"
    elif re.search(r"\bvoucher\b", low):
        mode = "voucher"

    if not (detail or output or mode):
        return None

    # Strip the small command vocabulary. If meaningful edit words remain, this
    # is not just an output/detail request and should use the normal AI editor.
    cleaned = low
    removable = [
        r"\bplease\b", r"\bkindly\b", r"\bgive\b", r"\bsend\b", r"\bshow\b",
        r"\bmake\b", r"\bcreate\b", r"\bgenerate\b", r"\bconvert\b",
        r"\bchange\b", r"\bturn\b", r"\bregenerate\b",
        r"\bme\b", r"\bit\b", r"\bthis\b", r"\bthe\b", r"\ba\b", r"\ban\b",
        r"\bto\b", r"\binto\b", r"\bas\b", r"\bversion\b", r"\bof\b",
        r"\bbasic\b", r"\bshort\b", r"\bbrief\b",
        r"\bdetailed\b", r"\bdetail\b", r"\bexpanded\b", r"\bfull\b",
        r"\bmore\b", r"\bdetails\b",
        r"\bday[- ]?wise\b", r"\bday\b", r"\bplan\b", r"\bitinerary\b",
        r"\bpdf\b", r"\bprint\b", r"\bwhatsapp\b", r"\bwhats\s*app\b",
        r"\btext\b", r"\bdraft\b", r"\bquotation\b", r"\bquote\b", r"\bvoucher\b",
    ]
    for pat in removable:
        cleaned = re.sub(pat, " ", cleaned, flags=re.I)
    cleaned = re.sub(r"[^a-z0-9]+", "", cleaned)

    if cleaned:
        return None

    return {"detail": detail, "output": output, "mode": mode}


async def _tour_variant_data(message, data, detail, status=None):
    """Return the requested Basic/Detailed variant while preserving saved metadata."""
    old = copy.deepcopy(data or {})
    desired = detail or str(old.get("detail_level") or "basic").lower()
    desired = "detailed" if desired == "detailed" else "basic"

    if str(old.get("detail_level") or "").lower() == desired:
        new_data = copy.deepcopy(old)
    else:
        new_data = await _run_ai_with_retry_status(
            message,
            lambda: asyncio.to_thread(
                enhance_package_itinerary,
                old,
                GEMINI_API_KEY,
                GEMINI_MODEL,
                desired,
            ),
            status=status,
        )
        # The enhancement schema intentionally contains itinerary fields only.
        # Merge it over the old object so customer costing / document metadata
        # that are outside the AI schema are never lost.
        merged = copy.deepcopy(old)
        merged.update(new_data or {})
        new_data = merged

    new_data["detail_level"] = desired
    if str(old.get("client_name") or "").strip():
        new_data["client_name"] = old.get("client_name")
    if "package_costs" in old:
        new_data["package_costs"] = copy.deepcopy(old.get("package_costs"))
    if "show_cost" in old:
        new_data["show_cost"] = old.get("show_cost")
    if old.get("document_mode"):
        new_data["document_mode"] = old.get("document_mode")
    if old.get("b2b") or old.get("brand_neutral"):
        new_data = _b2b_neutralize_data(new_data, new_data.get("document_mode") or "itinerary")
        new_data["greeting"] = _b2b_greeting(new_data, new_data.get("document_mode") or "itinerary")
    return _normalize_guest_counts(new_data)


def _apply_tour_document_mode_fields(data, mode, b2b=False):
    """Apply Tour title/greeting rules, including strict B2B white-label mode."""
    d = copy.deepcopy(data or {})
    mode = str(mode or d.get("document_mode") or "itinerary").lower()
    if mode not in ("quotation", "voucher", "itinerary"):
        mode = "itinerary"
    d["document_mode"] = mode
    b2b = bool(b2b or d.get("b2b") or d.get("brand_neutral"))
    guest = d.get("client_name") or "Guest"

    if mode == "quotation":
        d["document_title"] = "OFFICIAL TOUR QUOTATION"
    elif mode == "voucher":
        d["document_title"] = "OFFICIAL TOUR VOUCHER"
    else:
        d["document_title"] = "OFFICIAL TOUR ITINERARY"

    if b2b:
        d = _b2b_neutralize_data(d, mode)
        d["greeting"] = _b2b_greeting(d, mode)
        return d

    if mode == "quotation":
        d["greeting"] = (
            f"Dear {guest},\n\nGreetings from MyTourBazar! We are delighted to present this official "
            "tour quotation prepared especially for your travel requirements. The following proposal "
            "summarizes the planned destinations, accommodation, transportation, sightseeing experiences, "
            "inclusions and exclusions for your consideration. We look forward to arranging a comfortable "
            "and memorable journey for you and your family.\n\nPlease review the itinerary and package "
            "details carefully, and feel free to contact us for any clarification or amendment before confirmation."
        )
    elif mode == "voucher":
        d["greeting"] = (
            f"Dear {guest},\n\nGreetings from MyTourBazar! Thank you for choosing us for your journey. "
            "Please find below your official tour voucher containing the confirmed travel plan, accommodation "
            "schedule, services and day-wise arrangements. Kindly keep this voucher available during your "
            "journey and review the included services and travel instructions before departure.\n\n"
            "We wish you a smooth, comfortable and memorable trip."
        )
    else:
        d["greeting"] = (
            f"Dear {guest},\n\nGreetings from MyTourBazar! We are pleased to present your carefully planned "
            "travel itinerary. This document brings together the accommodation schedule, transportation "
            "arrangements, sightseeing experiences, inclusions and exclusions so that you have a clear "
            "day-by-day plan for your journey.\n\nWe look forward to assisting you throughout your travel "
            "and making your trip comfortable, smooth and memorable."
        )
    return d


async def _reply_saved_tour_variant(message, context, reference, record, command):
    """Reply to a generated Tour PDF with Basic/Detailed PDF/WhatsApp/Draft."""
    old_data = copy.deepcopy(record.get("data") or {})
    if not old_data:
        return False

    detail = command.get("detail") or record.get("detail_level") or old_data.get("detail_level") or "basic"
    detail = "detailed" if str(detail).lower() == "detailed" else "basic"
    output = command.get("output") or "pdf"   # PDF reply defaults to another PDF.
    mode = command.get("mode") or record.get("document_mode") or old_data.get("document_mode") or "itinerary"

    status = await message.reply_text(
        f"🤖 *Preparing {detail.title()} Tour {output.title()}...*\\n\\n"
        "████████░░░░░░░ 55%\\n\\n"
        "Expanding or shortening only the day plan while preserving the confirmed Tour facts.",
        parse_mode="Markdown",
    )

    try:
        new_data = await _tour_variant_data(message, old_data, detail, status=status)
        b2b = bool(record.get("b2b") or old_data.get("b2b") or old_data.get("brand_neutral"))
        if b2b:
            new_data = _b2b_neutralize_data(new_data, mode)
            new_data["greeting"] = _b2b_greeting(new_data, mode)
        new_data["document_mode"] = mode

        if output == "draft":
            context.user_data["itinerary"] = new_data
            context.user_data["source_text"] = record.get("source_text", "")
            if b2b:
                context.user_data["pending_b2b"] = True
                context.user_data["pending_clean_agency"] = True
                context.user_data["pending_tour_last_page"] = "b2b"
            await safe_status_edit(
                status, message,
                f"✅ *{detail.title()} draft ready.*\\n\\n████████████████ 100%\\n\\n"
                "Use the normal Tour buttons below for WhatsApp or PDF.",
                parse_mode="Markdown",
            )
            await _send_draft_review(
                message, context, new_data,
                prefix=f"📝 *{detail.title()} draft created from the replied PDF.*",
            )
            return True

        # Keep the richer data in the saved reference even when only WhatsApp is requested.
        record["data"] = copy.deepcopy(new_data)
        record["detail_level"] = detail
        record["document_mode"] = mode

        if output == "whatsapp":
            update_record(reference, record)
            context.user_data["itinerary"] = new_data
            await safe_status_edit(
                status, message,
                f"✅ *{detail.title()} WhatsApp itinerary ready.*\\n\\n████████████████ 100%",
                parse_mode="Markdown",
            )
            await reply_text_chunked(
                message,
                build_whatsapp_itinerary(new_data, detail),
                parse_mode="Markdown",
            )
            await message.reply_text(
                "You can reply again with `detailed PDF`, `basic PDF`, `detailed draft`, or another request.",
                parse_mode="Markdown",
            )
            return True

        # PDF: preserve the exact saved print personality of the replied PDF.
        stored_data = _apply_tour_document_mode_fields(new_data, mode, b2b=b2b)
        render_data = copy.deepcopy(stored_data)

        if "no_cost" in record:
            no_cost = bool(record.get("no_cost"))
        else:
            no_cost = not bool(
                stored_data.get("show_cost") and stored_data.get("package_costs")
            )
        if no_cost:
            render_data["show_cost"] = False
            render_data.pop("package_costs", None)

        clean = bool(record.get("agency_removed", False) or record.get("b2b", False))
        footer_mode = "none" if clean else (
            record.get("footer_mode") or _default_footer_mode("package")
        )
        page_size = record.get("page_size") or "A4"
        logo_enabled = bool(record.get("logo_enabled", True)) and not clean
        text_scale = float(record.get("text_scale") or load_settings().get("text_scale", 1.0))
        logo_scale = float(record.get("logo_scale") or get_logo_scale("package"))
        last_page = record.get("terms_choice") or get_tour_last_page()

        render_record = copy.deepcopy(record)
        render_record["_clean_agency"] = clean

        final_pdf, selected_scale, filename = await _render_saved_pdf(
            reference,
            render_record,
            render_data,
            "package",
            None,
            page_size,
            footer_mode,
            logo_enabled,
            text_scale_override=text_scale,
            logo_scale_override=logo_scale,
            auto_fit=False,
            last_page=last_page,
        )

        record.update({
            "filename": filename,
            "data": stored_data,
            "detail_level": detail,
            "document_mode": mode,
            "page_size": page_size,
            "footer": footer_mode != "none",
            "footer_mode": footer_mode,
            "logo_enabled": logo_enabled,
            "text_scale": selected_scale if selected_scale is not None else text_scale,
            "logo_scale": logo_scale,
            "terms_choice": last_page,
            "agency_removed": clean,
            "no_cost": no_cost,
        })
        update_record(reference, record)

        await safe_status_edit(
            status, message,
            f"✅ *{detail.title()} PDF ready.*\\n\\n████████████████ 100%\\n\\n"
            f"The replied PDF has been regenerated as a {detail.lower()} "
            f"{'quotation' if mode == 'quotation' else 'voucher' if mode == 'voucher' else 'itinerary'}.",
            parse_mode="Markdown",
        )

        title = (
            "Tour Quotation" if mode == "quotation"
            else "Tour Voucher" if mode == "voucher"
            else "Tour Itinerary"
        )
        with open(final_pdf, "rb") as fh:
            sent_pdf = await message.reply_document(
                document=fh,
                filename=filename,
                caption=(f"📄 {detail.title()} B2B {title}" if b2b else f"📄 {detail.title()} MyTourBazar {title}"),
                reply_markup=generated_document_keyboard(reference, "package"),
            )
        _register_reference_message(reference, sent_pdf)
        return True
    except Exception as exc:
        logger.exception("Reply Tour variant generation failed")
        await safe_status_edit(
            status, message,
            f"❌ *Could not create the requested Tour version.*\\n\\nReason: `{str(exc)[:900]}`",
            parse_mode="Markdown",
        )
        return True


async def _reply_draft_tour_variant(message, context, command):
    """Reply to a Tour draft: default is another draft; explicit WhatsApp/PDF is honored."""
    old_data = copy.deepcopy(context.user_data.get("itinerary") or {})
    if not old_data:
        return False

    detail = command.get("detail") or old_data.get("detail_level") or "basic"
    detail = "detailed" if str(detail).lower() == "detailed" else "basic"
    output = command.get("output") or "draft"  # Draft reply defaults to draft first.
    mode = command.get("mode")

    status = await message.reply_text(
        f"🤖 *Preparing {detail.title()} version from this draft...*\\n\\n"
        "████████░░░░░░░ 55%",
        parse_mode="Markdown",
    )
    try:
        new_data = await _tour_variant_data(message, old_data, detail, status=status)
        draft_b2b = bool(old_data.get("b2b") or old_data.get("brand_neutral") or context.user_data.get("pending_b2b"))
        if draft_b2b:
            new_data = _b2b_neutralize_data(new_data, mode or new_data.get("document_mode") or "itinerary")
            new_data["greeting"] = _b2b_greeting(new_data, mode or new_data.get("document_mode") or "itinerary")
            context.user_data["pending_b2b"] = True
            context.user_data["pending_clean_agency"] = True
            context.user_data["pending_tour_last_page"] = "b2b"
        context.user_data["itinerary"] = new_data

        if output == "whatsapp":
            await safe_status_edit(
                status, message,
                f"✅ *{detail.title()} WhatsApp itinerary ready.*\\n\\n████████████████ 100%",
                parse_mode="Markdown",
            )
            await reply_text_chunked(
                message,
                build_whatsapp_itinerary(new_data, detail),
                parse_mode="Markdown",
            )
            await message.reply_text(
                "🧭 *Tour draft actions*",
                parse_mode="Markdown",
                reply_markup=draft_review_keyboard(),
            )
            return True

        if output == "pdf":
            await safe_status_edit(
                status, message,
                f"✅ *{detail.title()} day plan ready for PDF.*\\n\\n████████████████ 100%",
                parse_mode="Markdown",
            )
            if mode in ("quotation", "voucher"):
                await _prepare_tour_pdf_request(message, context, detail, mode)
            else:
                await message.reply_text(
                    f"📄 *{detail.title()} PDF selected.*\\n\\n"
                    "Choose whether this should be a Tour Quotation or Tour Voucher.",
                    parse_mode="Markdown",
                    reply_markup=tour_pdf_mode_keyboard(detail),
                )
            return True

        # Default for a reply to a draft: show the updated draft first.
        await safe_status_edit(
            status, message,
            f"✅ *{detail.title()} draft ready.*\\n\\n████████████████ 100%\\n\\n"
            "Now choose Modify & Regenerate, WhatsApp, Basic PDF or Detailed PDF.",
            parse_mode="Markdown",
        )
        await _send_draft_review(
            message, context, new_data,
            prefix=f"📝 *{detail.title()} day plan prepared.*",
        )
        return True
    except Exception as exc:
        logger.exception("Draft reply Tour variant failed")
        context.user_data["itinerary"] = old_data
        await safe_status_edit(
            status, message,
            f"❌ *Could not prepare the requested draft version.*\\n\\nReason: `{str(exc)[:900]}`",
            parse_mode="Markdown",
        )
        return True


def build_whatsapp_itinerary(data, detail_level=None):
    """Build a clean WhatsApp-ready text version of a tour itinerary."""
    detail_level = detail_level or data.get("detail_level") or "basic"
    guest = str(data.get("client_name") or "Guest").strip()
    lines = [
        "━━━━━━━━━━━━━━━━━━━━",
        "🗺️ TOUR ITINERARY",
        "━━━━━━━━━━━━━━━━━━━━",
        f"👤 Guest: {guest}",
        f"📍 Destination: {data.get('destination') or '—'}",
        f"🗓️ Dates: {data.get('travel_dates') or '—'}",
        f"⏱️ Duration: {data.get('duration') or '—'}",
    ]
    if data.get("guests"):
        lines.append(f"👨‍👩‍👧 Guests: {data.get('guests')}")
    if data.get("vehicle"):
        lines.append(f"🚗 Vehicle: {data.get('vehicle')}")
    lines += ["", "📅 DAY-WISE PLAN"]
    for day in data.get("days", []):
        lines += [
            "",
            f"*{day.get('day','')} | {day.get('title','')}*",
            str(day.get("description") or "").strip(),
        ]
        opts = day.get("optional_activities") or []
        if detail_level == "detailed" and opts:
            lines.append("💛 Optional activities (at own cost):")
            lines.extend(f"• {x}" for x in opts)
        if day.get("stay"):
            lines.append(f"🏨 Stay: {day.get('stay')}")
        if day.get("meal_plan"):
            lines.append(f"🍽️ Meal: {day.get('meal_plan')}")
    if data.get("transit"):
        lines += ["", "✈️ JOURNEY / TRANSIT DETAILS"]
        for t in data.get("transit", []):
            jt=str(t.get("journey_type") or "Journey")
            route=str(t.get("route") or ((str(t.get("from") or "") + " → " + str(t.get("to") or "")).strip(" →")))
            carrier=" ".join(x for x in [str(t.get("carrier") or "").strip(),str(t.get("flight_number") or "").strip()] if x)
            timing=" - ".join(x for x in [str(t.get("departure") or "").strip(),str(t.get("arrival") or "").strip()] if x)
            lines.append(f"• {jt}: {route}" + (f" | {carrier}" if carrier else "") + (f" | {timing}" if timing else ""))
    if data.get("hotels"):
        lines += ["", "🏨 ACCOMMODATION"]
        for h in data.get("hotels", []):
            hotel_name = h.get("hotel_name") or "Hotel category as selected"
            room_category = str(h.get('room_type') or h.get('room_category') or '').strip()
            rooms = str(h.get('rooms') or '').strip()
            parts=[hotel_name]
            if room_category: parts.append(f"Category: {room_category}")
            if rooms: parts.append(f"Total Rooms: {rooms}")
            if h.get('meal_plan'): parts.append(f"Meal: {h.get('meal_plan')}")
            lines.append(f"• {h.get('destination','')} — " + " | ".join(parts))
    if data.get("inclusions"):
        lines += ["", "✅ INCLUSIONS"]
        lines.extend(f"• {x}" for x in data.get("inclusions", []))
    if data.get("exclusions"):
        lines += ["", "❌ EXCLUSIONS"]
        lines.extend(f"• {x}" for x in data.get("exclusions", []))
    costs=data.get("package_costs") or []
    if costs and data.get("show_cost", True):
        lines += ["", "💰 PACKAGE COST"]
        c=costs[0]
        for label,key in [("Adult","per_adult"),("Child","per_child"),("CNB","per_child_cnb"),("CWB","per_child_cwb"),("Extra Bed","per_extra_bed")]:
            if str(c.get(key) or "").strip(): lines.append(f"• {label}: INR {c.get(key)}")
    mode=str(data.get('document_mode') or 'itinerary').lower()
    b2b=bool(data.get('b2b') or data.get('brand_neutral'))
    if b2b:
        if mode == 'quotation':
            lines += ["", f"Dear {guest},", "Greetings from our company! Please find below your official tour quotation prepared around the requested travel plan, accommodation, sightseeing, inclusions and exclusions. We hope the proposal gives you a clear understanding of the planned journey, and our company will be happy to assist with any clarification or amendment before confirmation."]
        elif mode == 'voucher':
            lines += ["", f"Dear {guest},", "Greetings from our company! Please find below your official tour voucher containing the confirmed travel plan, accommodation, services and day-wise arrangements. Kindly keep the voucher available during your journey and review the included services before departure."]
        else:
            lines += ["", f"Dear {guest},", "Greetings from our company! Please find below your carefully planned day-wise travel itinerary, including accommodation, transportation, sightseeing experiences, inclusions and exclusions for a smooth and comfortable journey."]
        lines += ["", "Thank you for choosing our company!"]
    else:
        if mode == 'quotation':
            lines += ["", "Dear Guest,", "Greetings from MyTourBazar! Please find below your official tour quotation prepared around the requested travel plan, accommodation, sightseeing, inclusions and exclusions. We hope the proposal gives you a clear understanding of the planned journey and we will be happy to assist with any clarification or amendment before confirmation."]
        elif mode == 'voucher':
            lines += ["", "Dear Guest,", "Greetings from MyTourBazar! Please find below your official tour voucher containing the confirmed travel plan, accommodation, services and day-wise arrangements. Kindly keep the voucher available during your journey and review the included services before departure."]
        else:
            lines += ["", f"Dear {guest},", "Greetings from MyTourBazar! Please find below your carefully planned day-wise travel itinerary, including accommodation, transportation, sightseeing experiences, inclusions and exclusions for a smooth and comfortable journey."]
        lines += [
            "",
            "Thank you for choosing MyTourBazar!",
            "Aapke Safar Ka Saathi",
            "",
            "MYTOURBAZAR",
            "📞 +91 9425259086",
            "✉️ sales@mytourbazar.com",
            "🌐 www.mytourbazar.com",
        ]
    return "\n".join(lines)

async def reply_text_chunked(message, text, **kwargs):
    """Send long Telegram text safely in multiple messages, preferring line boundaries."""
    text = str(text or "")
    limit = 3800
    if len(text) <= limit:
        return [await message.reply_text(text, **kwargs)]
    parts = []
    current = ""
    for line in text.splitlines(True):
        if len(current) + len(line) <= limit:
            current += line
        else:
            if current.strip():
                parts.append(current.rstrip())
            while len(line) > limit:
                cut = line.rfind(" ", 0, limit)
                if cut < 100:
                    cut = limit
                parts.append(line[:cut].rstrip())
                line = line[cut:].lstrip()
            current = line
    if current.strip():
        parts.append(current.rstrip())
    sent = []
    for part in parts:
        kw = dict(kwargs)
        # Multi-part Markdown can break if a formatting span crosses a boundary.
        # Send long chunks as plain text rather than fail the entire operation.
        if len(parts) > 1:
            kw.pop("parse_mode", None)
        sent.append(await message.reply_text(part, **kw))
    return sent


def build_confirmation(d):
    def val(key, fallback="Not found"):
        v = d.get(key, "")
        return str(v).strip() if str(v).strip() else fallback

    hotels = d.get("hotels", [])
    days = d.get("days", [])
    transit = d.get("transit", [])
    inclusions = d.get("inclusions", [])
    exclusions = d.get("exclusions", [])

    hotel_text = "\n".join(
        f"• {h.get('destination','')} — {h.get('hotel_name','')} "
        f"(Category: {h.get('room_type') or h.get('room_category') or '—'} • Total Rooms: {h.get('rooms') or '—'}{(' • Meal: ' + str(h.get('meal_plan'))) if h.get('meal_plan') else ''})"
        for h in hotels[:8]
    ) or "Not found"

    day_text = "\n".join(
        f"• {x.get('day','')} — {x.get('title','')}"
        for x in days[:12]
    ) or "Not found"

    inc_text = "\n".join(f"• {x}" for x in inclusions[:12]) or "None generated"
    exc_text = "\n".join(f"• {x}" for x in exclusions[:12]) or "None generated"
    costs = d.get('package_costs') or []
    if costs and d.get('show_cost'):
        if d.get('markup_total'):
            cost_text = "\n".join(f"• {c.get('option','Package')}: Final ₹{c.get('final_total') or c.get('total_cost','—')}" for c in costs)
            cost_text += f"\n• Markup added: ₹{d.get('markup_total')}"
        else:
            cost_text = "\n".join(f"• {c.get('option','Package')}: Adult {c.get('per_adult','—')} | Child {c.get('per_child','—')} | CWB {c.get('per_child_cwb','—')} | CNB {c.get('per_child_cnb','—')} | EB {c.get('per_extra_bed','—')}" for c in costs)
    else:
        needed=[label for _,label in _required_package_cost_fields(d)]
        cost_text = "Customer costing not added yet — " + _package_cost_prompt(d)

    if transit:
        transit_text = "\n".join(
            f"• {t.get('journey_type') or 'Journey'}: {t.get('route') or (str(t.get('from') or '') + ' → ' + str(t.get('to') or ''))} "
            f"| {(str(t.get('carrier') or '') + ' ' + str(t.get('flight_number') or '')).strip()} "
            f"| {t.get('departure') or ''} - {t.get('arrival') or ''}"
            for t in transit[:16]
        )
    else:
        transit_text = "No journey/transit details added"

    return (
        "📋 *AI-completed itinerary draft:*\n\n"
        f"*Guest:* {val('client_name')}\n"
        f"*Tour:* {val('tour_title')}\n"
        f"*Destination:* {val('destination')}\n"
        f"*Dates:* {val('travel_dates')}\n"
        f"*Duration:* {val('duration')}\n"
        f"*Guests:* {val('guests')}\n"
        f"*Vehicle:* {val('vehicle')}\n"
        f"*Pickup:* {val('pickup')}\n"
        f"*Drop:* {val('drop')}\n\n"
        f"*Hotels ({len(hotels)}):*\n{hotel_text}\n\n"
        f"*Days ({len(days)}):*\n{day_text}\n\n"
        f"*AI Inclusions ({len(inclusions)}):*\n{inc_text}\n\n"
        f"*AI Exclusions ({len(exclusions)}):*\n{exc_text}\n\n"
        f"*Journey / Transit:*\n{transit_text}\n\n"
        f"*Package Cost:*\n{cost_text}\n\n"
        "Edit this final draft if required. Your next text reply is the final submission and will generate the PDF directly."
    )



def draft_review_keyboard():
    """Draft actions: edit, WhatsApp preview, PDF detail choice, or confirm Done."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🛠️ Modify & Regenerate', callback_data='draft_edit')],
        [InlineKeyboardButton('📱 Basic WhatsApp', callback_data='tour_output:whatsapp:basic'),
         InlineKeyboardButton('📱 Detailed WhatsApp', callback_data='tour_output:whatsapp:detailed')],
        [InlineKeyboardButton('📄 Basic PDF', callback_data='tour_output:pdf:basic'),
         InlineKeyboardButton('📄 Detailed PDF', callback_data='tour_output:pdf:detailed')],
        [InlineKeyboardButton('✅ Done', callback_data='draft_done')],
    ])


def _value_preview(value, limit=90):
    if isinstance(value, (dict, list)):
        return str(value)[:limit]
    text = str(value or '').strip().replace('\n', ' ')
    return text if len(text) <= limit else text[:limit-3] + '...'


def _draft_change_notes(old, new):
    """Create a concise, human-readable 'Noted' summary without another AI call."""
    old = old or {}
    new = new or {}
    notes = []
    labels = {
        'client_name': 'Guest name', 'destination': 'Destination', 'tour_title': 'Tour title',
        'travel_dates': 'Travel dates', 'duration': 'Duration', 'guests': 'Guests',
        'vehicle': 'Vehicle', 'pickup': 'Pickup', 'drop': 'Drop', 'detail_level': 'Detail level',
    }
    for key, label in labels.items():
        if _value_preview(old.get(key)) != _value_preview(new.get(key)):
            notes.append(f'• {label}: {_value_preview(old.get(key)) or "blank"} → {_value_preview(new.get(key)) or "blank"}')

    old_days = old.get('days') or []
    new_days = new.get('days') or []
    max_days = max(len(old_days), len(new_days))
    for i in range(max_days):
        a = old_days[i] if i < len(old_days) else None
        b = new_days[i] if i < len(new_days) else None
        if a is None:
            notes.append(f'• Day {i+1}: added')
            continue
        if b is None:
            notes.append(f'• Day {i+1}: removed')
            continue
        for key, label in [('title','title'),('description','plan'),('stay','stay'),('meal_plan','meal plan')]:
            if _value_preview(a.get(key), 120) != _value_preview(b.get(key), 120):
                notes.append(f'• Day {i+1} {label}: updated')
                break

    old_hotels = old.get('hotels') or []
    new_hotels = new.get('hotels') or []
    if old_hotels != new_hotels:
        notes.append(f'• Accommodation: updated ({len(old_hotels)} → {len(new_hotels)} option(s))')

    for key, label in [('inclusions','Inclusions'),('exclusions','Exclusions'),('transit','Flight / transit details'),('special_notes','Special notes')]:
        if old.get(key) != new.get(key):
            notes.append(f'• {label}: updated')

    if old.get('package_costs') != new.get('package_costs'):
        notes.append('• Package costing: updated')

    if not notes:
        notes.append('• The requested edit was processed, but no visible field difference was detected.')
    return notes[:12]


def _draft_review_text(data):
    return build_confirmation(data)


async def _send_draft_review(message, context, data, prefix=None):
    """Render the current dynamic draft with live output shortcuts."""
    context.user_data['itinerary'] = _ensure_supplier_costs(data)
    text = _draft_review_text(context.user_data['itinerary'])
    if prefix:
        text = f'{prefix}\n\n{text}'
    chunks = []
    limit = 3800
    if len(text) <= limit:
        chunks = [text]
    else:
        current = ''
        for line in text.splitlines(True):
            if len(current) + len(line) <= limit:
                current += line
            else:
                if current.strip(): chunks.append(current.rstrip())
                current = line
        if current.strip(): chunks.append(current.rstrip())
    sent=[]
    for i, chunk in enumerate(chunks):
        sent.append(await message.reply_text(chunk, parse_mode='Markdown' if len(chunks)==1 else None))
    # Put the live draft actions on a fresh dynamic message. This prevents stale
    # inline buttons from earlier workflow stages from remaining attached to the draft.
    sent.append(await message.reply_text('🧭 *Tour draft actions*\n\n• Modify & Regenerate the draft\n• Basic / Detailed WhatsApp\n• Basic / Detailed PDF → Quotation / Voucher', parse_mode='Markdown', reply_markup=draft_review_keyboard()))
    # Keep a rolling set so Telegram Reply on any recent draft message is routed
    # straight into the draft editor without requiring the Smart Edit button.
    prior=[int(x) for x in (context.user_data.get('tour_draft_message_ids') or []) if str(x).isdigit()]
    for _m in sent:
        _mid=getattr(_m,'message_id',None)
        if _mid is not None and int(_mid) not in prior:
            prior.append(int(_mid))
    context.user_data['tour_draft_message_ids']=prior[-100:]
    return sent


async def perform_draft_edit(update, context, instruction):
    """Edit the in-memory draft only. No PDF is generated until Done is pressed."""
    data = context.user_data.get('itinerary')
    if not data:
        context.user_data.pop('editing_current_itinerary', None)
        await update.message.reply_text('❌ The current draft is no longer available. Please start the Tour workflow again.', reply_markup=main_keyboard())
        return
    old_data = copy.deepcopy(data)
    status = await update.message.reply_text('✏️ *Updating the draft...*\n\n████████░░░░░░░ 55%\n\n🔍 Understanding your requested change...', parse_mode='Markdown')
    try:
        requested_detail = _itinerary_detail_command(instruction)
        if requested_detail:
            new_data = await _run_ai_with_retry_status(update.message, lambda: asyncio.to_thread(enhance_package_itinerary, old_data, GEMINI_API_KEY, GEMINI_MODEL, requested_detail), status=status)
            new_data['client_name'] = old_data.get('client_name', '')
            new_data['detail_level'] = requested_detail
        else:
            new_data, _ = await _run_ai_with_retry_status(update.message, lambda: asyncio.to_thread(apply_edit, 'package', old_data, instruction, GEMINI_API_KEY, GEMINI_MODEL, None), status=status)
        new_data = _ensure_supplier_costs(new_data)
        if old_data.get('b2b') or old_data.get('brand_neutral') or context.user_data.get('pending_b2b'):
            new_data = _apply_tour_document_mode_fields(
                new_data,
                old_data.get('document_mode') or new_data.get('document_mode') or 'itinerary',
                b2b=True,
            )
            context.user_data['pending_b2b'] = True
            context.user_data['pending_clean_agency'] = True
            context.user_data['pending_tour_last_page'] = 'b2b'
        context.user_data['itinerary'] = new_data
        context.user_data['pending_tour_markup_print'] = None
        context.user_data.pop('editing_current_itinerary', None)
        await safe_status_edit(status, update.message, '✅ *Draft updated.*\n\n████████████████ 100%\n\n📝 I noted the changes below. Review the updated draft before generating the PDF.', parse_mode='Markdown')
        notes = _draft_change_notes(old_data, new_data)
        await update.message.reply_text('📝 *Noted — changes made:*\n' + '\n'.join(notes), parse_mode='Markdown')
        await _send_draft_review(update.message, context, new_data)
    except Exception as exc:
        logger.exception('Draft smart edit failed')
        context.user_data['itinerary'] = old_data
        context.user_data.pop('editing_current_itinerary', None)
        await safe_status_edit(status, update.message, f'❌ *Draft update failed*\n\nReason: `{str(exc)[:900]}`', parse_mode='Markdown')
        await update.message.reply_text('You can try the Smart Edit Draft button again.', reply_markup=draft_review_keyboard())


async def _finish_pending_tour_pdf(message, context):
    req = context.user_data.get('pending_tour_pdf_request') or {}
    data = context.user_data.get('itinerary') or {}
    if not data or not req:
        await message.reply_text('❌ The pending Tour PDF request expired. Please use the draft PDF button again.', reply_markup=draft_review_keyboard())
        return
    detail = req.get('detail') or 'basic'
    mode = req.get('mode') or 'quotation'
    no_cost = bool(req.get('no_cost', True))
    context.user_data['pending_tour_pdf_detail'] = detail
    context.user_data['pending_tour_pdf_no_cost'] = no_cost
    context.user_data['pending_tour_document_mode'] = mode
    data['document_mode'] = mode
    context.user_data['itinerary'] = data
    await message.reply_text(f"⏳ Generating {detail.title()} Tour {'Quotation' if mode=='quotation' else 'Voucher'}...")
    try:
        ref, _ = await generate_tour_pdf_final(message, context, data, detail, no_cost)
        context.user_data.pop('pending_tour_pdf_request', None)
        context.user_data.pop('awaiting_tour_print_name', None)
        for k in ('pending_special_notes_decided','pending_tour_cost_decided','pending_special_notes','pending_tour_document_mode','pending_tour_pdf_detail','pending_tour_pdf_no_cost'):
            context.user_data.pop(k, None)
        # A completed Auto Creation batch is no longer an active intake session.
        # The saved record keeps auto_creation=True, but the next normal upload is new work.
        for k in ('auto_creation','smart_mode','smart_force_kind','smart_files','smart_text','_source_status_message','_source_auto_processed'):
            context.user_data.pop(k, None)
        _cancel_source_auto_process(context)
        await message.reply_text('✅ PDF generated successfully. Ready for a new upload or use the PDF buttons to modify it.', parse_mode='Markdown', reply_markup=ready_keyboard())
    except Exception as exc:
        logger.exception('Pending Tour PDF generation failed')
        await message.reply_text(f'❌ Tour PDF generation failed.\n\nReason: `{str(exc)[:800]}`', parse_mode='Markdown', reply_markup=main_keyboard())


async def _prepare_tour_pdf_request(message, context, detail, mode):
    data = context.user_data.get('itinerary') or {}
    if not data:
        await message.reply_text('❌ No current Tour draft is available.', reply_markup=main_keyboard())
        return
    detail = 'detailed' if str(detail).lower() == 'detailed' else 'basic'
    mode = 'voucher' if str(mode).lower() == 'voucher' else 'quotation'
    if str(data.get('detail_level') or '').lower() != detail:
        old_name = str(data.get('client_name') or '').strip()
        status = await message.reply_text(f'🤖 Preparing the {detail} day plan...')
        data = await _run_ai_with_retry_status(message, lambda: asyncio.to_thread(enhance_package_itinerary, data, GEMINI_API_KEY, GEMINI_MODEL, detail), status=status)
        data['client_name'] = old_name or str(data.get('client_name') or '').strip()
        data['detail_level'] = detail
        if context.user_data.get('pending_b2b'):
            data = _apply_tour_document_mode_fields(data, mode, b2b=True)
        context.user_data['itinerary'] = data
        await safe_status_edit(status, message, f'✅ {detail.title()} day plan ready.')
    data['document_mode'] = mode
    context.user_data['itinerary'] = data
    context.user_data['pending_tour_pdf_request'] = {
        'detail': detail,
        'mode': mode,
        'no_cost': not bool(data.get('show_cost') and data.get('package_costs')) if _tour_v2_active(context) else bool(context.user_data.get('pending_tour_pdf_no_cost', True)),
    }
    if _tour_v2_active(context):
        context.user_data['pending_tour_document_mode']=mode
        context.user_data['tour_v2_phase']='printing_direct'
        await _finish_pending_tour_pdf(message,context)
        context.user_data['tour_v2_phase']='complete'
        return
    has_transit=bool(data.get("transit"))
    context.user_data["awaiting_tour_transit_choice"]=True
    if has_transit:
        prompt=("✈️ *Transit & Connection*\n\n"
                f"I found *{len(data.get('transit') or [])} transit sector(s)* in the supplier material.\n\n"
                "Use them, replace them with flight-ticket PDF(s)/screenshots/text, or skip.")
    else:
        prompt=("✈️ *Transit & Connection*\n\n"
                "No confirmed transit was found in the Tour draft.\n\n"
                "Add one-way/round-trip/connecting flights from one or multiple PDFs, screenshots or text.\n"
                "If you skip, the box will show *Done by Self* in the center.")
    await message.reply_text(prompt,parse_mode="Markdown",reply_markup=tour_transit_choice_keyboard(has_transit))


async def generate_tour_pdf_final(message, context, data, detail='basic', no_cost=False, reference=None):
    data = _normalize_guest_counts(dict(data))
    data['detail_level'] = detail or 'basic'
    document_mode = context.user_data.get('pending_tour_document_mode') or data.get('document_mode') or 'itinerary'
    b2b = _is_b2b_tour(data=data, context=context)
    data = _apply_tour_document_mode_fields(data, document_mode, b2b=b2b)
    # Keep supplier/package costing in the saved record for future markup edits,
    # while using a separate render copy when the customer PDF must hide cost.
    stored_data = copy.deepcopy(data)
    render_data = copy.deepcopy(data)
    if no_cost:
        render_data['show_cost'] = False
        render_data.pop('package_costs', None)
    filename = _package_filename(stored_data)
    base_pdf = GENERATED_DIR / f'_tour_base_{filename}'
    combined_pdf = GENERATED_DIR / f'_tour_terms_{filename}'
    wm_pdf = GENERATED_DIR / f'_tour_wm_{filename}'
    final_pdf = GENERATED_DIR / filename
    size = context.user_data.get('pending_tour_page_size', 'A4') or 'A4'
    footer_mode = context.user_data.get('pending_tour_footer_mode') or _default_footer_mode('package')
    logo_path = LOGO_PATH if LOGO_PATH.exists() else None
    clean = bool(b2b or context.user_data.get('pending_b2b', False) or context.user_data.get('pending_clean_agency', False))
    if clean:
        footer_mode = 'none'
        render_data['agency_removed'] = True
        stored_data['agency_removed'] = True
    last_page = context.user_data.get('pending_tour_last_page') or get_tour_last_page()
    if last_page == 'tc_default': last_page = 'tc_non_google'
    if b2b or context.user_data.get('pending_b2b'):
        last_page = 'b2b'
        render_data = _b2b_neutralize_data(render_data, document_mode)
        render_data['greeting'] = _b2b_greeting(render_data, document_mode)
        stored_data = _b2b_neutralize_data(stored_data, document_mode)
        stored_data['greeting'] = _b2b_greeting(stored_data, document_mode)
    await asyncio.to_thread(generate_pdf, render_data, base_pdf, None if clean else logo_path, size)
    await asyncio.to_thread(append_selected_terms, base_pdf, last_page, combined_pdf)
    base_pdf.unlink(missing_ok=True)
    ws = load_settings()
    await asyncio.to_thread(add_watermark_to_pdf, combined_pdf, wm_pdf, ws['buttons'].get('watermark', True) and not clean, ws.get('watermark_opacity', 0.04), ws.get('watermark_scale', 1.5))
    combined_pdf.unlink(missing_ok=True)
    await asyncio.to_thread(_apply_footer_mode, wm_pdf, final_pdf, footer_mode)
    wm_pdf.unlink(missing_ok=True)
    ref = reference or create_reference()
    save_record(ref, {'type':'package','filename':filename,'data':stored_data,'fare':None,'source_text':context.user_data.get('source_text',''),'detail_level':detail,'terms_choice':last_page,'document_mode':document_mode,'b2b':bool(b2b or context.user_data.get('pending_b2b')),'footer':footer_mode!='none','footer_mode':footer_mode,'logo_enabled':not clean,'page_size':size,'agency_removed':clean,'text_scale':float(load_settings().get('text_scale',1.0)),'logo_scale':float(get_logo_scale('package')),'auto_creation':bool(context.user_data.get('auto_creation')),'no_cost':bool(no_cost)})
    if b2b:
        caption_prefix = "📄 B2B"
    else:
        caption_prefix = "🤖 Auto-Created MyTourBazar" if context.user_data.get('auto_creation') else "📄 MyTourBazar"
    caption = f"{caption_prefix} {data.get('document_title','OFFICIAL TOUR ITINERARY').title()}\n\n📜 Last page: {terms_label(last_page)}"
    with open(final_pdf,'rb') as fh:
        sent_pdf=await message.reply_document(document=fh, filename=filename, caption=caption, parse_mode='Markdown', reply_markup=generated_document_keyboard(ref, 'package'))
    _register_reference_message(ref, sent_pdf)
    return ref, final_pdf


def modify_footer_keyboard(reference):
    rows=[[InlineKeyboardButton('🧩 Footer 1 (Old Design)', callback_data=f'mod_footer:{reference}:design'),
           InlineKeyboardButton('🧳 Footer 2 (New Design)', callback_data=f'mod_footer:{reference}:footer2')],
          [InlineKeyboardButton('🟧 Contact Bar', callback_data=f'mod_footer:{reference}:bar')],
          [InlineKeyboardButton('⬅️ Back', callback_data=f'modify:{reference}')]]
    return InlineKeyboardMarkup(rows)

def modify_keyboard(reference, kind):
    pending = context_dummy = None
    rows=[]
    if button_enabled('page_size_controls'):
        rows.append([InlineKeyboardButton('📐 Page Size', callback_data=f'mod_size:{reference}'),
                     InlineKeyboardButton('🧾 Footer', callback_data=f'mod_footer_menu:{reference}')])
    if kind == 'package':
        rows.append([InlineKeyboardButton('📝 Detailed Day Plan', callback_data=f'mod_detail:{reference}:detailed')])
        rows.append([InlineKeyboardButton('📜 Last Page', callback_data=f'mod_last_page:{reference}'),
                     InlineKeyboardButton('🏢 B2B Print', callback_data=f'mod_b2b:{reference}')])
        rows.append([InlineKeyboardButton('🧾 Quotation', callback_data=f'mod_mode:{reference}:quotation'),
                     InlineKeyboardButton('🎫 Voucher', callback_data=f'mod_mode:{reference}:voucher')])
    rows.append([InlineKeyboardButton('✅ Done • Make Again', callback_data=f'mod_done:{reference}')])
    return InlineKeyboardMarkup(rows)

def modify_last_page_keyboard(reference):
    current = get_tour_last_page()
    labels = [('tc_non_google','📜 T&C NON GOOGLE'),('without_footer','📄 Without Footer')]
    rows=[]
    for key,label in labels:
        prefix='✅ ' if current==key else ''
        rows.append([InlineKeyboardButton(prefix+label, callback_data=f'mod_last_page:{reference}:{key}')])
    rows.append([InlineKeyboardButton('⬅️ Back', callback_data=f'modify:{reference}')])
    return InlineKeyboardMarkup(rows)

def modify_size_keyboard(reference):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('A4', callback_data=f'mod_size:{reference}:A4'), InlineKeyboardButton('Letter', callback_data=f'mod_size:{reference}:Letter'), InlineKeyboardButton('Legal', callback_data=f'mod_size:{reference}:Legal')],
        [InlineKeyboardButton('⬅️ Back', callback_data=f'modify:{reference}')],
    ])

def _pdf_page_count(path):
    try:
        return len(PdfReader(str(path)).pages)
    except Exception:
        return 0


async def _render_saved_pdf(reference, record, data, kind, fare, page_size, footer_mode, logo_enabled, text_scale_override=None, logo_scale_override=None, auto_fit=False, last_page=None):
    filename = (_package_filename(data) if kind=='package' else _flight_filename(data) if kind=='flight' else _bus_filename(data) if kind=='bus' else _hotel_filename(data))
    final=GENERATED_DIR/filename
    scales=[text_scale_override] if text_scale_override is not None else [None]
    if auto_fit:
        current=float(load_settings().get('text_scale',1.0))
        scales=[round(current*x,2) for x in (1.00,0.95,0.90,0.85,0.80,0.75,0.70)]
    selected_scale=scales[-1] if scales else text_scale_override
    for attempt,scale in enumerate(scales):
        base=GENERATED_DIR/f'_modify_{reference}_base_{attempt}.pdf'
        wm=GENERATED_DIR/f'_modify_{reference}_wm_{attempt}.pdf'
        candidate=GENERATED_DIR/f'_modify_{reference}_final_{attempt}.pdf'
        try:
            logo_path=LOGO_PATH if logo_enabled and LOGO_PATH.exists() else None
            if kind=='package':
                render_data = copy.deepcopy(data)
                render_b2b = bool(record.get('b2b') or render_data.get('b2b') or render_data.get('brand_neutral'))
                if render_b2b:
                    render_data = _apply_tour_document_mode_fields(render_data, record.get('document_mode') or render_data.get('document_mode') or 'itinerary', b2b=True)
                    logo_path = None
                    record['_clean_agency'] = True
                    footer_mode = 'none'
                    last_page = 'b2b'
                await asyncio.to_thread(generate_pdf,render_data,base,logo_path,_normalize_page_size(page_size) or 'A4',text_scale_override=scale,logo_scale_override=logo_scale_override)
                combined=GENERATED_DIR/f'_modify_{reference}_terms_{attempt}.pdf'
                await asyncio.to_thread(append_selected_terms,base,last_page or record.get('terms_choice') or get_tour_last_page(),combined); base.unlink(missing_ok=True); base=combined
            else:
                await asyncio.to_thread(_generate_adaptive_ticket,kind,data,fare,base,logo_path,page_size,text_scale_override=scale,logo_scale_override=logo_scale_override)
            base_pages=_pdf_page_count(base)
            ws=load_settings()
            await asyncio.to_thread(add_watermark_to_pdf,base,wm,ws['buttons'].get('watermark',True) and not record.get('_clean_agency',False),ws.get('watermark_opacity',0.04),ws.get('watermark_scale',1.5))
            base.unlink(missing_ok=True)
            if footer_mode == 'bar' and not record.get('_clean_agency',False):
                await asyncio.to_thread(add_contact_bar_to_pdf,wm,candidate)
            elif footer_mode == 'footer2' and not record.get('_clean_agency',False):
                await asyncio.to_thread(add_footer2_to_pdf,wm,candidate)
            elif footer_mode == 'design' and not record.get('_clean_agency',False):
                await asyncio.to_thread(add_footer_to_pdf,wm,candidate)
            else:
                shutil.copyfile(wm,candidate)
            wm.unlink(missing_ok=True)
            final_pages=_pdf_page_count(candidate)
            # Auto Fit intervenes only when the data itself is one page and the selected footer caused an extra page.
            if auto_fit and base_pages == 1 and final_pages > 1 and footer_mode != 'none':
                # If the footer was the cause, smaller text should eventually let it overlay the final page.
                candidate.unlink(missing_ok=True)
                continue
            shutil.move(str(candidate),str(final))
            selected_scale=scale
            break
        finally:
            for q in (base,wm,candidate):
                try: q.unlink(missing_ok=True)
                except Exception: pass
    return final, selected_scale, filename


async def auto_fit_saved_ticket(query, context, reference):
    """Auto-size any generated MyTourBazar document while preserving its saved settings.

    For Tour/Auto Creation this also preserves the selected last-page asset and footer mode.
    The best candidate uses the fewest pages, then the largest readable font, then A4/Letter/Legal.
    """
    record = load_record(reference)
    if not record:
        await query.message.reply_text('❌ This saved document is no longer available.', reply_markup=main_keyboard())
        return
    kind = record.get('type', 'package')
    if kind not in ('package', 'flight', 'bus', 'hotel'):
        await safe_callback_edit(query, '⚡ Auto Size is not available for this document type.')
        return

    data = _normalize_guest_counts(copy.deepcopy(record.get('data') or {}))
    if kind == 'package' and record.get('b2b'):
        data = _apply_tour_document_mode_fields(data, record.get('document_mode') or data.get('document_mode') or 'itinerary', b2b=True)
    fare = record.get('fare')
    footer_mode = record.get('footer_mode') or (_default_footer_mode(kind) if record.get('footer') else 'none')
    logo_enabled = bool(record.get('logo_enabled', True)) and not bool(record.get('agency_removed', False))
    logo_scale = float(record.get('logo_scale') or get_logo_scale(kind))
    clean = bool(record.get('agency_removed', False))
    last_page = record.get('terms_choice') or get_tour_last_page()

    await safe_callback_edit(query, '⚡ *Auto Size is checking A4, Letter and Legal...*', parse_mode='Markdown')

    sizes = ['A4', 'Letter', 'Legal']
    current = float(record.get('text_scale') or load_settings().get('text_scale', 1.0))
    current = max(0.70, min(1.35, current))
    scales = [round(current * x, 2) for x in (1.00, 0.95, 0.90, 0.85, 0.80, 0.75, 0.70)]
    scales = list(dict.fromkeys(max(0.60, min(1.35, x)) for x in scales))

    best = None
    tmp_files = []
    try:
        for size_index, size in enumerate(sizes):
            for scale_index, scale in enumerate(scales):
                stem = f'_autofit_{reference}_{size}_{scale_index}'
                base = GENERATED_DIR / f'{stem}_base.pdf'
                combined = GENERATED_DIR / f'{stem}_terms.pdf'
                wm = GENERATED_DIR / f'{stem}_wm.pdf'
                candidate = GENERATED_DIR / f'{stem}_final.pdf'
                tmp_files.extend([base, combined, wm, candidate])
                try:
                    logo_path = LOGO_PATH if logo_enabled and LOGO_PATH.exists() else None
                    if kind == 'package':
                        await asyncio.to_thread(
                            generate_pdf, data, base, logo_path, size,
                            text_scale_override=scale, logo_scale_override=logo_scale
                        )
                        await asyncio.to_thread(append_selected_terms, base, last_page, combined)
                        base.unlink(missing_ok=True)
                        render_source = combined
                    else:
                        await asyncio.to_thread(
                            _generate_ticket_base, kind, data, fare, base, logo_path, size,
                            text_scale_override=scale, logo_scale_override=logo_scale
                        )
                        render_source = base

                    ws = load_settings()
                    await asyncio.to_thread(
                        add_watermark_to_pdf, render_source, wm,
                        ws['buttons'].get('watermark', True) and not clean,
                        ws.get('watermark_opacity', 0.04), ws.get('watermark_scale', 1.5)
                    )
                    if footer_mode == 'bar' and not clean:
                        await asyncio.to_thread(add_contact_bar_to_pdf, wm, candidate)
                    elif footer_mode == 'footer2' and not clean:
                        await asyncio.to_thread(add_footer2_to_pdf, wm, candidate)
                    elif footer_mode == 'design' and not clean:
                        await asyncio.to_thread(add_footer_to_pdf, wm, candidate)
                    else:
                        shutil.copyfile(wm, candidate)

                    final_pages = _pdf_page_count(candidate)
                    if final_pages <= 0:
                        continue
                    score = (final_pages, -scale, size_index)
                    if best is None or score < best['score']:
                        best = {'score': score, 'size': size, 'scale': scale, 'pages': final_pages, 'source': candidate}
                except Exception:
                    logger.exception('Auto Size candidate failed: %s %s %s', kind, size, scale)

        if not best:
            raise RuntimeError('Auto Size could not create a valid PDF candidate.')

        filename = (_package_filename(data) if kind == 'package' else
                    _flight_filename(data) if kind == 'flight' else
                    _bus_filename(data) if kind == 'bus' else _hotel_filename(data))
        final = GENERATED_DIR / filename
        shutil.copyfile(best['source'], final)
        record.update({
            'filename': final.name,
            'data': data,
            'page_size': best['size'],
            'text_scale': best['scale'],
            'logo_scale': logo_scale,
        })
        update_record(reference, record)

        await safe_callback_edit(
            query,
            f"✅ *Auto Size complete*\n\n📐 Page: *{best['size']}*\n🔤 Font scale: *{int(round(best['scale'] * 100))}%*\n📄 Pages: *{best['pages']}*",
            parse_mode='Markdown'
        )
        label = 'Tour' if kind == 'package' else kind.title()
        with open(final, 'rb') as fh:
            sent_pdf=await query.message.reply_document(
                document=fh,
                filename=final.name,
                caption=_record_caption(reference, f'📄 Auto-Sized MyTourBazar {label}', f"Page size: {best['size']} | Font: {int(round(best['scale']*100))}%"),
                reply_markup=generated_document_keyboard(reference, kind),
            )
        _register_reference_message(reference, sent_pdf)
    except Exception as exc:
        logger.exception('Auto Size failed')
        await query.message.reply_text(
            f'❌ Auto Size failed.\n\nReason: `{str(exc)[:800]}`',
            parse_mode='Markdown', reply_markup=generated_document_keyboard(reference, kind)
        )
    finally:
        for path in tmp_files:
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass


async def regenerate_saved_with_modifications(query, context, reference):
    record=load_record(reference)
    if not record:
        await query.message.reply_text('❌ This saved document is no longer available.', reply_markup=main_keyboard()); return
    pending=context.user_data.get(f'modify:{reference}', {})
    if not pending:
        await safe_callback_edit(query, 'No changes selected.'); return
    old_data=record.get('data') or {}
    kind=record.get('type','package')
    fare=record.get('fare')
    data=_normalize_guest_counts(dict(old_data))
    detail=pending.get('detail')
    if kind=='package' and detail and str(data.get('detail_level','')).lower()!=detail:
        data=await _run_ai_with_retry_status(query.message, lambda: asyncio.to_thread(enhance_package_itinerary,data,GEMINI_API_KEY,GEMINI_MODEL,detail))
        data['client_name']=old_data.get('client_name',''); data['detail_level']=detail
    page_size=pending.get('page_size') or record.get('page_size') or 'A4'
    footer_mode=pending.get('footer_mode') or record.get('footer_mode') or _default_footer_mode(kind)
    logo_enabled=record.get('logo_enabled',True) and not pending.get('clean_agency',False)
    logo_scale=float(pending.get('logo_scale') or record.get('logo_scale') or get_logo_scale(kind))
    font_scale=float(pending.get('font_scale') or record.get('text_scale') or load_settings().get('text_scale',1.0))
    b2b=bool(pending.get('b2b',False) or record.get('b2b',False)) if kind=='package' else False
    clean=bool(pending.get('clean_agency',False) or record.get('agency_removed',False) or b2b)
    if clean:
        footer_mode='none'
    if kind=='package':
        context.user_data['pending_tour_last_page']='b2b' if b2b else (pending.get('tour_last_page') or record.get('terms_choice') or get_tour_last_page())
        if context.user_data['pending_tour_last_page']=='tc_default': context.user_data['pending_tour_last_page']='tc_non_google'
        context.user_data['pending_b2b']=b2b
        context.user_data['pending_tour_document_mode']=pending.get('document_mode') or record.get('document_mode') or 'itinerary'
        if b2b:
            data=_apply_tour_document_mode_fields(data,context.user_data['pending_tour_document_mode'],b2b=True)
    # Use the selected per-service footer and keep it through page-size/font changes unless explicitly changed.
    record['_clean_agency']=clean
    final, selected_scale, filename=await _render_saved_pdf(reference,record,data,kind,fare,page_size,footer_mode,logo_enabled,text_scale_override=font_scale,logo_scale_override=logo_scale,auto_fit=False,last_page=context.user_data.get('pending_tour_last_page') or ('b2b' if pending.get('b2b') else record.get('terms_choice') or get_tour_last_page()))
    record.pop('_clean_agency',None)
    record.update({'filename':filename,'data':data,'page_size':page_size,'footer':footer_mode!='none','footer_mode':footer_mode,'detail_level':detail or record.get('detail_level','basic'),'logo_enabled':logo_enabled,'text_scale':selected_scale,'logo_scale':logo_scale,'agency_removed':clean,'terms_choice':context.user_data.get('pending_tour_last_page') or ('b2b' if pending.get('b2b') else record.get('terms_choice') or get_tour_last_page()),'document_mode':pending.get('document_mode') or record.get('document_mode','itinerary'),'b2b':bool(b2b)})
    update_record(reference,record)
    context.user_data.pop(f'modify:{reference}',None)
    await safe_callback_edit(query,'⏳ Regenerating with your selected changes...')
    with open(final,'rb') as fh:
        sent_pdf=await query.message.reply_document(document=fh,filename=filename,caption=_record_caption(reference,(f'📄 Updated B2B {kind.title()}' if b2b else f'📄 Updated MyTourBazar {kind.title()}'),f'Page size: {page_size}'),reply_markup=generated_document_keyboard(reference,kind))
    _register_reference_message(reference, sent_pdf)

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(update):
        await safe_callback_edit(query, "Sorry, this bot is private.")
        return

    if query.data.startswith("settings:"):
        await settings_callback(query, context)
        return


    if query.data.startswith("hotel_cost:"):
        reference=query.data.split(":",1)[1]
        record=load_record(reference)
        if not record or record.get('type')!='hotel':
            await query.message.reply_text('❌ This Hotel reference is no longer available.',reply_markup=main_keyboard()); return
        context.user_data['post_hotel_cost_reference']=reference
        # Clear generic fare state so the reply can never be misread as Air/Bus fare markup.
        context.user_data.pop('pending_fare_kind',None)
        context.user_data.pop('pending_fare_supplier_total',None)
        await query.message.reply_text(
            '🏨 *Add Hotel Cost*\n\nReply in one message, for example:\n`Room 8500, EB 1200, Total 18200`\n\nEB is optional. I will regenerate this same Hotel voucher from saved data.',
            parse_mode='Markdown',reply_markup=ReplyKeyboardRemove())
        return

    if query.data.startswith("post_transit_make:"):
        reference=query.data.split(":",1)[1]
        pending=context.user_data.get('post_transit_pending') or {}
        record=load_record(reference)
        if not record or record.get('type')!='package':
            context.user_data.pop('post_transit_pending',None)
            await query.message.reply_text("❌ This Tour reference is no longer available.",reply_markup=main_keyboard()); return
        data=pending.get('data') if pending.get('reference')==reference else record.get('data') or {}
        await safe_callback_edit(query,'⏳ Regenerating Tour PDF with the confirmed transit...')
        try:
            await _regenerate_saved_package(query.message,reference,record,data,'📄 Tour PDF regenerated with transit')
            context.user_data.pop('post_transit_pending',None)
            await query.message.reply_text('✅ Transit PDF ready.',reply_markup=main_keyboard())
        except Exception as exc:
            logger.exception('Post transit Make PDF failed')
            await query.message.reply_text(f"⚠️ PDF could not be regenerated. Your transit is still saved in this session; tap Make PDF again.\n\nReason: {str(exc)[:400]}",reply_markup=_post_transit_confirm_keyboard(reference,not _package_has_customer_costing(data)))
        return

    if query.data.startswith("post_transit_cost:"):
        reference=query.data.split(":",1)[1]
        pending=context.user_data.get('post_transit_pending') or {}
        record=load_record(reference)
        if not record or record.get('type')!='package':
            context.user_data.pop('post_transit_pending',None)
            await query.message.reply_text("❌ This Tour reference is no longer available.",reply_markup=main_keyboard()); return
        if pending.get('reference')==reference and pending.get('data'):
            record['data']=pending['data']
            update_record(reference,record)
        context.user_data.pop('post_transit_pending',None)
        context.user_data['post_cost_reference']=reference
        await query.message.reply_text(
            "💰 *Add Costing*\n\n" + _package_cost_prompt(record.get('data') or {}) + "\n\nAfter I have the applicable rates, I will regenerate the same PDF with Transit and Costing.",
            parse_mode='Markdown',reply_markup=ReplyKeyboardRemove())
        return

    if query.data.startswith("post_cost:"):
        reference=query.data.split(":",1)[1]
        record=load_record(reference)
        if not record or record.get("type")!="package":
            await query.message.reply_text("❌ This Tour reference is no longer available.",reply_markup=main_keyboard()); return
        context.user_data["post_cost_reference"]=reference
        await query.message.reply_text(
            "💰 *Add Costing*\n\n" + _package_cost_prompt(record.get('data') or {}) + "\n\nI will regenerate the same Tour PDF once the applicable customer rates are available.",
            parse_mode="Markdown",reply_markup=ReplyKeyboardRemove())
        return

    if query.data.startswith("post_transit:"):
        reference=query.data.split(":",1)[1]
        record=load_record(reference)
        if not record or record.get("type")!="package":
            await query.message.reply_text("❌ This Tour reference is no longer available.",reply_markup=main_keyboard()); return
        context.user_data["post_transit_reference"]=reference
        await query.message.reply_text(
            """✈️ *Add Transit*

Type the travel sectors naturally, preferably one sector per line. No Onward / Return / Transit prefix is required.

Airport codes, city names, flight/train numbers, dates, terminals and times can be mixed in any order. I will infer the journey sequence from the lines.

Example:
`RPR DEL AI1729 12:20 14:35`
`DEL DXB EK511 18:30 21:00`
`DXB DEL EK510 03:30 08:25`
`DEL RPR AI1730 10:20 12:05`

I will show the detailed Transit text I understood before regenerating the PDF.""",
            parse_mode="Markdown",reply_markup=ReplyKeyboardRemove())
        return

    if query.data.startswith("edit_generated:"):
        reference = query.data.split(":", 1)[1]
        if not load_record(reference):
            await query.message.reply_text("❌ That generated document is no longer available.", reply_markup=ready_keyboard())
            return
        context.user_data["editing_reference"] = reference
        await query.message.reply_text(
            f"🤖 *Smart editing {reference}*\n\nTell me naturally what you want changed. I will understand the request and update the correct part of the saved itinerary — you do not need to use a predefined command.\n\nThis button stays with this generated PDF, so you can press it again later to make more changes.\n\nExamples: change a passenger, rename a hotel, correct a date/time, modify baggage, change fare, remove/add footer or logo, change page size, rewrite a day plan, or combine several changes in one message.",
            parse_mode="Markdown", reply_markup=ready_keyboard()
        )
        return

    if query.data == "enter_ref":
        context.user_data["awaiting_edit_ref"] = True
        await query.message.reply_text(
            "✏️ *Enter the Reference Number*\n\nExample: `MTB01`",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
        )
        return

    if query.data.startswith("files_page:"):
        try:
            page = int(query.data.split(":", 1)[1])
        except ValueError:
            page = 0
        await query.edit_message_reply_markup(reply_markup=files_list_keyboard(page))
        return

    if query.data.startswith("size:"):
        await safe_callback_edit(query, "ℹ️ Page size is now changed from 🛠️ Modify & Regenerate on the generated PDF.")
        return

    if query.data.startswith("select_ref:"):
        reference = query.data.split(":", 1)[1]
        record = load_record(reference)
        if not record:
            await query.message.reply_text("❌ That saved document no longer exists. Please use the latest generated PDF.", reply_markup=main_keyboard())
            return
        context.user_data["editing_reference"] = reference
        await query.message.reply_text(
            f"✏️ *Selected {reference}*\n\nType the changes you want to make. You can change day plans, flights, hotels, meal plans, inclusions, exclusions, passenger details, fare, or other supported document details.",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
        )
        return

    if query.data == "cancel":
        context.user_data.clear()
        await safe_callback_edit(query, "❌ Cancelled.")
        await query.message.reply_text("Ready for the next itinerary.", reply_markup=main_keyboard())
        return

    if query.data == "reenter":
        context.user_data.clear()
        await safe_callback_edit(query, "🔄 Start again with 🗺️ Tour Guide.")
        await query.message.reply_text("Ready.", reply_markup=main_keyboard())
        return

    if query.data == "add_inclusion":
        await query.message.reply_text(
            "➕ *Add extra inclusion*\n\nType the inclusion exactly as you want it to appear.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["awaiting_extra"] = "inclusion"
        return

    if query.data == "add_exclusion":
        await query.message.reply_text(
            "➕ *Add extra exclusion*\n\nType the exclusion exactly as you want it to appear.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardRemove()
        )
        context.user_data["awaiting_extra"] = "exclusion"
        return

    if query.data == "add_flight":
        await query.message.reply_text(
            "✈️ *Add flight / train details*\n\n"
            "You can now send *screenshots or text*. You may send onward and return details separately or together. "
            "I will automatically identify every flight/train sector and keep connecting sectors separate.\n\n"
            "When finished, tap *✅ Done*.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["✍️ Flight Text"], ["✈️ Flight Screenshot"], ["✅ Done"]], resize_keyboard=True)
        )
        context.user_data["awaiting_flight"] = True
        return

    if query.data == "add_flight_text":
        await query.message.reply_text(
            "✍️ *Add Flight / Train Details in Text*\n\n"
            "Paste whatever details you have. No special format is required. You can send onward and return journeys together or in separate messages.\n\n"
            "Example:\n`01 Oct: IndiGo 6E-594 Raipur → Mumbai 09:30 AM – 11:25 AM\n"
            "01 Oct: IndiGo 6E-273 Mumbai → Rajkot 01:10 PM – 03:50 PM`\n\n"
            "Gemini will extract the operator, flight/train number, route, date, departure and arrival automatically.\n\n"
            "When finished, tap *✅ Done*.",
            parse_mode="Markdown",
            reply_markup=ReplyKeyboardMarkup([["✍️ Flight Text"], ["✈️ Flight Screenshot"], ["✅ Done"]], resize_keyboard=True)
        )
        context.user_data["awaiting_flight"] = True
        return

    if query.data == 'draft_edit':
        if not context.user_data.get('itinerary'):
            await query.message.reply_text('❌ No current draft is available. Please start the Tour workflow again.', reply_markup=main_keyboard())
            return
        context.user_data['editing_current_itinerary'] = True
        await query.message.reply_text(
            '🛠️ *Modify & Regenerate*\n\n'
            'Tell me exactly what you want changed. I will update the draft only — no PDF will be generated yet.\n\n'
            'Examples:\n'
            '• `Change Day 2 sightseeing to Sonmarg.`\n'
            '• `Change the hotel in Gulmarg to a 4 star option.`\n'
            '• `Correct the guest name to Mr. Amit Sharma.`\n'
            '• `Add private airport pickup to inclusions.`\n\n'
            'You can type normally or reply directly to this message.',
            parse_mode='Markdown', reply_markup=ReplyKeyboardRemove()
        )
        return

    if query.data == 'draft_done':
        data = context.user_data.get('itinerary')
        if not data:
            await query.message.reply_text('❌ No current draft is available. Please start the Tour workflow again.', reply_markup=main_keyboard())
            return
        context.user_data['itinerary'] = _ensure_supplier_costs(data)
        context.user_data['pending_tour_pdf_no_cost'] = True
        context.user_data.pop('editing_current_itinerary', None)
        await safe_callback_edit(
            query,
            '✅ *Draft confirmed.*\n\nChoose the output you want. For PDF, first choose Basic/Detailed and then Tour Quotation or Tour Voucher.',
            parse_mode='Markdown', reply_markup=tour_output_keyboard()
        )
        return

    if query.data == "tour_detail:basic" or query.data == "tour_detail:detailed":
        data = context.user_data.get("itinerary")
        if not data:
            await query.message.reply_text("No itinerary data is available. Please start the tour workflow again.", reply_markup=main_keyboard())
            return
        detail = query.data.split(":", 1)[1]
        try:
            status = await query.message.reply_text("🤖 Updating the day plans with Gemini...")
            new_data = await _run_ai_with_retry_status(query.message, lambda: asyncio.to_thread(enhance_package_itinerary, data, GEMINI_API_KEY, GEMINI_MODEL, detail), status=status)
            new_data["client_name"] = data.get("client_name", "")
            new_data["detail_level"] = detail
            if data.get('b2b') or data.get('brand_neutral') or context.user_data.get('pending_b2b'):
                new_data = _apply_tour_document_mode_fields(
                    new_data,
                    data.get('document_mode') or new_data.get('document_mode') or 'itinerary',
                    b2b=True,
                )
                context.user_data['pending_b2b'] = True
                context.user_data['pending_clean_agency'] = True
                context.user_data['pending_tour_last_page'] = 'b2b'
            context.user_data["itinerary"] = new_data
            await safe_status_edit(status, query.message, f"✅ {detail.title()} itinerary ready. Choose WhatsApp or PDF.")
            await query.message.reply_text(build_confirmation(new_data), parse_mode="Markdown")
            await query.message.reply_text("Choose the output you want.", reply_markup=tour_output_keyboard())
        except Exception as exc:
            logger.exception("Tour detail enhancement failed")
            await query.message.reply_text(f"❌ Could not update itinerary: {str(exc)[:700]}", reply_markup=main_keyboard())
        return

    if query.data == "tour_edit_current":
        data = context.user_data.get("itinerary")
        if not data:
            await query.message.reply_text("No current itinerary is available.", reply_markup=main_keyboard())
            return
        context.user_data["editing_current_itinerary"] = True
        await query.message.reply_text(
            "🛠️ *Modify & Regenerate*\n\nTell me naturally what you want changed. I will update the current draft only. After the updated draft is ready, you can again choose WhatsApp or PDF.",
            parse_mode="Markdown", reply_markup=ReplyKeyboardRemove()
        )
        return

    if query.data.startswith("tour_transit:"):
        action=query.data.split(":",1)[1]
        data=context.user_data.get("itinerary") or {}
        if action=="cancel":
            context.user_data.pop("awaiting_tour_transit_choice",None)
            await safe_callback_edit(query,"❌ Transit selection cancelled.",reply_markup=draft_review_keyboard()); return
        if action=="use":
            context.user_data.pop("awaiting_tour_transit_choice",None)
            data["transit_done_by_self"]=False; context.user_data["itinerary"]=data
            await safe_callback_edit(query,"✅ Detected transit will be used.")
            await _continue_tour_pdf_after_transit(query.message,context); return
        if action=="skip":
            context.user_data.pop("awaiting_tour_transit_choice",None)
            data["transit"]=[]; data["transit_done_by_self"]=True; context.user_data["itinerary"]=data
            await safe_callback_edit(query,"✅ Transit skipped. The box will show *Done by Self*.",parse_mode="Markdown")
            await _continue_tour_pdf_after_transit(query.message,context); return
        if action=="add":
            context.user_data.pop("awaiting_tour_transit_choice",None)
            context.user_data["awaiting_tour_transit_input"]=True
            context.user_data["pending_tour_transit_files"]=[]
            context.user_data["pending_tour_transit_text"]=""
            await safe_callback_edit(query,
                "✈️ *Add / Replace Transit*\n\n"
                "Send one or multiple flight-ticket PDFs, screenshots, unstructured one-way/round-trip text, or any mixture.\n\n"
                "I will split every sector, detect onward/connection/return, keep the full flight number, preserve terminal whenever supplied, "
                "and show aircraft in brackets when supplied.\n\nWhen finished, tap *✅ Done Transit*.",
                parse_mode="Markdown")
            await query.message.reply_text("Send the transit sources now.",reply_markup=tour_transit_input_keyboard()); return

    if query.data.startswith('voice_edit:'):
        reference=query.data.split(':',1)[1]
        record=load_record(reference)
        if not record:
            await query.message.reply_text('❌ Saved document not found.', reply_markup=main_keyboard()); return
        context.user_data['editing_reference']=reference
        context.user_data['voice_edit_reference']=reference
        kind=record.get('type','document')
        label={'flight':'Air','bus':'Bus','hotel':'Hotel','package':'Tour'}.get(kind,'Document')
        await safe_callback_edit(
            query,
            f'🎙️ *Voice / Text Edit*\n\nSend one normal *text message or voice note* with the changes you want in this {label} print. '
            'No prefix or fixed format is required. I will understand the instruction and regenerate the same PDF.\n\n'
            'Examples: `change passenger mobile to 9876543210`, `change room type to Deluxe`, or simply explain the change by voice.',
            parse_mode='Markdown'
        )
        return

    if query.data.startswith('autofit:'):
        reference=query.data.split(':',1)[1]
        try:
            await auto_fit_saved_ticket(query, context, reference)
        except Exception as exc:
            logger.exception('Auto Fit callback failed')
            await query.message.reply_text(f'❌ Auto Fit failed.\n\nReason: `{str(exc)[:800]}`', parse_mode='Markdown', reply_markup=main_keyboard())
        return

    if query.data.startswith('modify:') and query.data.count(':') == 1:
        reference=query.data.split(':',1)[1]; record=load_record(reference)
        if not record:
            await query.message.reply_text('❌ Saved document not found.', reply_markup=main_keyboard()); return
        if record.get('type') == 'package':
            # V155: one natural-language/voice edit entry for Tour. Gemini can change
            # hotels/day plans as well as mixed flight/train/bus transit. Costing labels
            # (Adult/CWB/CNB/EB) are preserved locally after the AI edit.
            context.user_data['editing_reference']=reference
            context.user_data['voice_edit_reference']=reference
            await safe_callback_edit(query,
                '🛠️ *Modify & Regenerate*\n\nSend one normal *text message or voice note* describing all changes. '
                'You can mix hotel/day-plan changes, customer costing and any number of flight/train/bus transit sectors.\n\n'
                'Examples:\n• `Adult 74000, CWB 52000`\n• `Change Munnar hotel to Amberdale 4 star`\n• `Raipur to Nagpur by train 07:15, Nagpur to Goa flight at 14:20, return Goa-Mumbai-Delhi by flight and Delhi-Raipur by bus.`\n\n'
                'No prefix or fixed format is required. I will understand the instruction and regenerate the same PDF.',
                parse_mode='Markdown')
            return
        context.user_data.setdefault(f'modify:{reference}',{})
        await safe_callback_edit(query,'🛠️ Modify this PDF — select what you want to change, then press Done • Make Again.',reply_markup=modify_keyboard(reference,record.get('type','package')))
        return
    if query.data.startswith('mod_font:'):
        _,reference,delta=query.data.split(':',2)
        pending=context.user_data.setdefault(f'modify:{reference}',{})
        saved=load_record(reference) or {}
        current=float(pending.get('font_scale') or saved.get('text_scale') or load_settings().get('text_scale',1.0))
        pending['font_scale']=round(max(.70,min(1.35,current+float(delta))),2)
        await safe_callback_edit(query,f"✅ Document font size: {int(round(pending['font_scale']*100))}%",reply_markup=modify_keyboard(reference,saved.get('type','package'))); return
    if query.data.startswith('mod_logo:'):
        _,reference,delta=query.data.split(':',2)
        pending=context.user_data.setdefault(f'modify:{reference}',{})
        saved=load_record(reference) or {}
        kind=saved.get('type','package')
        current=float(pending.get('logo_scale') or saved.get('logo_scale') or get_logo_scale(kind))
        pending['logo_scale']=round(max(.70,min(1.50,current+float(delta))),2)
        await safe_callback_edit(query,f"✅ Logo size: {int(round(pending['logo_scale']*100))}%",reply_markup=modify_keyboard(reference,kind)); return
    if query.data.startswith('mod_clean:'):
        reference=query.data.split(':',1)[1]; pending=context.user_data.setdefault(f'modify:{reference}',{}); pending['clean_agency']=not bool(pending.get('clean_agency',False))
        label='ON — agency details will be removed' if pending['clean_agency'] else 'OFF — normal agency details restored'
        await safe_callback_edit(query,f'🚫 Remove Agency Details: {label}',reply_markup=modify_keyboard(reference,(load_record(reference) or {}).get('type','package'))); return
    if query.data.startswith('mod_size:'):
        parts=query.data.split(':'); reference=parts[1]
        if len(parts)==2:
            await safe_callback_edit(query,'📐 Choose the new page size.',reply_markup=modify_size_keyboard(reference)); return
        size=parts[2]; context.user_data.setdefault(f'modify:{reference}',{})['page_size']=size
        await safe_callback_edit(query,f'✅ Page size selected: {size}.',reply_markup=modify_keyboard(reference,(load_record(reference) or {}).get('type','package'))); return
    if query.data.startswith('mod_footer_menu:'):
        reference=query.data.split(':',1)[1]
        await safe_callback_edit(query,'🧾 Choose the footer for this regenerated PDF.',reply_markup=modify_footer_keyboard(reference)); return
    if query.data.startswith('mod_footer:'):
        _,reference,mode=query.data.split(':',2)
        context.user_data.setdefault(f'modify:{reference}',{})['footer_mode']=mode
        label={'design':'Footer 1 (Old Design)','footer2':'Footer 2 (New Design)','bar':'Contact Bar'}[mode]
        await safe_callback_edit(query,f'✅ {label} selected.',reply_markup=modify_keyboard(reference,(load_record(reference) or {}).get('type','package'))); return
    if query.data.startswith('mod_detail:'):
        _,reference,detail=query.data.split(':',2)
        context.user_data.setdefault(f'modify:{reference}',{})['detail']=detail
        await safe_callback_edit(query,'✅ Detailed Day Plan selected.',reply_markup=modify_keyboard(reference,'package')); return
    if query.data.startswith('mod_last_page:'):
        parts=query.data.split(':')
        reference=parts[1]
        if len(parts)==2:
            await safe_callback_edit(query,'📜 Choose the Tour last page for this regeneration.',reply_markup=modify_last_page_keyboard(reference)); return
        choice=parts[2]
        context.user_data.setdefault(f'modify:{reference}',{})['tour_last_page']=choice
        await safe_callback_edit(query,f'✅ Last page selected: {terms_label(choice)}',reply_markup=modify_keyboard(reference,'package')); return

    if query.data.startswith('mod_b2b:'):
        reference=query.data.split(':',1)[1]
        pending=context.user_data.setdefault(f'modify:{reference}',{})
        pending['b2b']=True; pending['clean_agency']=True; pending['tour_last_page']='b2b'
        await safe_callback_edit(query,'🏢 B2B Print selected. This is strict white-label mode: MyTourBazar will be removed from greeting/body/footer/watermark/terms, and company references will use “our company”.',reply_markup=modify_keyboard(reference,'package')); return

    if query.data.startswith('mod_mode:'):
        _,reference,mode=query.data.split(':',2)
        context.user_data.setdefault(f'modify:{reference}',{})['document_mode']=mode
        label='Official Tour Quotation' if mode=='quotation' else 'Official Tour Voucher'
        await safe_callback_edit(query,f'✅ {label} selected.',reply_markup=modify_keyboard(reference,'package')); return

    if query.data.startswith('mod_done:'):
        reference=query.data.split(':',1)[1]
        try:
            await regenerate_saved_with_modifications(query,context,reference)
        except Exception as exc:
            logger.exception('Modify & Regenerate failed'); await query.message.reply_text(f'❌ Regeneration failed.\n\nReason: {str(exc)[:800]}',reply_markup=main_keyboard())
        return
    if query.data.startswith('mod_cancel:'):
        reference=query.data.split(':',1)[1]; context.user_data.pop(f'modify:{reference}',None)
        await safe_callback_edit(query,'❌ Changes cancelled.'); return

    if query.data.startswith("tour_terms:"):
        detail = context.user_data.get('pending_tour_pdf_detail') or 'basic'
        no_cost = bool(context.user_data.get('pending_tour_pdf_no_cost', False))
        data = context.user_data.get('itinerary')
        if not data:
            await query.message.reply_text('No itinerary data is available. Please start the Tour workflow again.', reply_markup=main_keyboard())
            return
        try:
            if str(data.get('detail_level','')).lower() != detail:
                data = await _run_ai_with_retry_status(query.message, lambda: asyncio.to_thread(enhance_package_itinerary, data, GEMINI_API_KEY, GEMINI_MODEL, detail))
                data['client_name'] = context.user_data.get('guest_name') or data.get('client_name','')
                context.user_data['itinerary'] = data
            await safe_callback_edit(query, '⏳ Generating the final Tour PDF with T&C NON GOOGLE...')
            await generate_tour_pdf_final(query.message, context, data, detail, no_cost)
            for k in ('pending_tour_pdf_detail','pending_tour_pdf_no_cost','pending_tour_page_size','pending_tour_footer_mode'):
                context.user_data.pop(k,None)
            await query.message.reply_text('✅ Ready for the next request.', reply_markup=ready_keyboard())
        except Exception as exc:
            logger.exception('Tour PDF generation failed')
            await query.message.reply_text(f'❌ Tour PDF generation failed.\n\nReason: {str(exc)[:800]}', reply_markup=main_keyboard())
        return

    if query.data.startswith('tour_special_notes:'):
        choice=query.data.split(':',1)[1]
        data=context.user_data.get('itinerary') or {}
        if choice=='add':
            data['special_notes']=context.user_data.get('pending_special_notes','')
        else:
            data['special_notes']=''
        context.user_data['pending_special_notes_decided']=True
        if data.get('package_costs'):
            await safe_callback_edit(query,'✅ Special Notes decision saved. Now choose how to handle the supplier cost.',reply_markup=tour_cost_keyboard())
        else:
            await safe_callback_edit(query,'✅ Special Notes decision saved. Choose the final Tour output.',reply_markup=tour_output_keyboard())
        return

    if query.data.startswith('tour_custom_cost:'):
        action_parts = query.data.split(':')
        action = action_parts[1] if len(action_parts) > 1 else ''
        if re.fullmatch(r'MTB\d+', action, re.I):
            ref = action.upper()
            rec = load_record(ref) or {}
            data = _ensure_supplier_costs(rec.get('data') or {})
            data = _normalize_guest_counts(data)
            fields = _custom_cost_fields(data)
            if not data.get('package_costs'):
                await safe_callback_edit(query, '❌ No package cost is available for this itinerary.')
                return
            context.user_data['pending_custom_cost_reference'] = ref
            context.user_data['pending_custom_cost_data'] = copy.deepcopy(data)
            context.user_data['pending_custom_cost_fields'] = fields
            context.user_data['pending_custom_cost_index'] = 0
            context.user_data['pending_custom_cost_input'] = True
            if fields:
                key, field, label, count = fields[0]
                await safe_callback_edit(query,
                    f'🧾 *Custom Cost*\n\n'
                    f'*{label}* — {count} passenger(s)\n'
                    f'Enter the direct per-person cost. Example: `1000`\n\n'
                    f'The PDF cost box will calculate: `1000 × {count} = {1000*count:,}`.',
                    reply_markup=custom_cost_keyboard(), parse_mode='Markdown')
            else:
                await safe_callback_edit(query,
                    '🧾 *Custom Cost*\n\nNo Adult/Child/CWB/CNB/EB count is available. You can still set a direct cost if you add passenger counts first.',
                    reply_markup=custom_cost_keyboard(), parse_mode='Markdown')
            return
        if action == 'done':
            data = context.user_data.get('pending_custom_cost_data')
            ref = context.user_data.get('pending_custom_cost_reference')
            if not data or not ref:
                await safe_callback_edit(query, '❌ Custom Cost session expired. Please open Custom Cost again.')
                return
            try:
                data = _finalize_custom_cost(data)
                rec = load_record(ref) or {}
                rec['data'] = data
                save_record(ref, rec)
                context.user_data['itinerary'] = data
                for k in ('pending_custom_cost_reference','pending_custom_cost_data','pending_custom_cost_fields','pending_custom_cost_index','pending_custom_cost_input'):
                    context.user_data.pop(k, None)
                # Reprint the same document immediately with the updated cost box.
                detail = data.get('detail_level') or 'basic'
                context.user_data['pending_tour_pdf_detail'] = detail
                context.user_data['pending_tour_pdf_no_cost'] = False
                context.user_data['pending_tour_document_mode'] = data.get('document_mode') or 'itinerary'
                await safe_callback_edit(query, '☑️ *Custom Cost saved.*\n\n' + _custom_cost_summary(data) + '\n\n⏳ Regenerating the PDF...', parse_mode='Markdown')
                await generate_tour_pdf_final(query.message, context, data, detail, False, reference=ref)
            except Exception as exc:
                logger.exception('Custom cost finalization failed')
                await safe_callback_edit(query, f'❌ Could not save Custom Cost.\n\nReason: `{str(exc)[:700]}`', parse_mode='Markdown')
            return
        if action == 'cancel':
            for k in ('pending_custom_cost_reference','pending_custom_cost_data','pending_custom_cost_fields','pending_custom_cost_index','pending_custom_cost_input'):
                context.user_data.pop(k, None)
            await safe_callback_edit(query, '❌ *Custom Cost cancelled.*\n\nNo cost was changed.', parse_mode='Markdown')
            return

    if query.data.startswith('tour_markup:'):
        # Backward compatibility for old Telegram messages created by earlier builds.
        # No markup session is started in V159.
        for _legacy_key in (
            'pending_tour_markup_print','pending_tour_markup_input','pending_tour_markup_mode',
            'pending_tour_markup_snapshot','pending_tour_markup_candidate'
        ):
            context.user_data.pop(_legacy_key, None)
        await safe_callback_edit(
            query,
            'ℹ️ *The old Tour markup system has been removed.*\n\n'
            'Use *Modify & Regenerate* on the Tour PDF and tell me the final customer costing naturally, by text or voice.',
            parse_mode='Markdown'
        )
        return

    if query.data.startswith('tour_cost:'):
        choice=query.data.split(':',1)[1]
        data=_ensure_supplier_costs(context.user_data.get('itinerary') or {})
        context.user_data['itinerary']=data
        if choice=='none':
            data['show_cost']=False
            context.user_data['pending_tour_cost_decided']=True
            await safe_callback_edit(query,'🖨️ *Print Without Cost selected.*\n\nThe internal supplier cost will be hidden from the customer PDF.',parse_mode='Markdown',reply_markup=tour_output_keyboard())
            return
        if choice in ('markup_print','markup'):
            await safe_callback_edit(query, 'ℹ️ The old markup system has been removed. Use *Modify & Regenerate* and tell me the final customer cost naturally by text or voice.', parse_mode='Markdown')
            return
        context.user_data['pending_tour_cost_decided']=True
        await safe_callback_edit(query,'✅ Cost preference saved. Choose the final Tour output.',reply_markup=tour_output_keyboard())
        return

    if query.data.startswith("tour_output_mode:"):
        parts = query.data.split(":")
        detail = parts[1] if len(parts) > 1 else 'basic'
        mode = parts[2] if len(parts) > 2 else 'quotation'
        await safe_callback_edit(query, f"✅ {detail.title()} PDF • {'Tour Quotation' if mode=='quotation' else 'Tour Voucher'} selected.", parse_mode='Markdown')
        if _tour_v2_active(context):
            # Stale buttons from older drafts stay safe: print directly and never reopen old Transit choices.
            data=copy.deepcopy(context.user_data.get('itinerary') or {})
            if not data:
                await query.message.reply_text('❌ No current Tour draft is available.',reply_markup=main_keyboard()); return
            if str(data.get('detail_level') or 'basic').lower()!=detail:
                status=await query.message.reply_text(f'✨ Preparing the {detail} itinerary...')
                old_name=str(data.get('client_name') or '')
                data=await _run_ai_with_retry_status(query.message,lambda: asyncio.to_thread(enhance_package_itinerary,data,GEMINI_API_KEY,GEMINI_MODEL,detail),status=status)
                data['client_name']=old_name or str(data.get('client_name') or '')
                data['detail_level']=detail
                await safe_status_edit(status,query.message,f'✅ {detail.title()} day plan ready.')
            data['document_mode']=mode
            context.user_data['itinerary']=data
            context.user_data['pending_tour_document_mode']=mode
            context.user_data['pending_tour_pdf_detail']=detail
            no_cost=not bool(data.get('show_cost') and data.get('package_costs'))
            context.user_data['pending_tour_pdf_no_cost']=no_cost
            context.user_data['tour_v2_phase']='printing_direct'
            ref,_=await generate_tour_pdf_final(query.message,context,data,detail,no_cost)
            context.user_data['tour_v2_phase']='complete'
            await query.message.reply_text('✅ PDF delivered. Ready for a new upload or use the buttons on the PDF to modify it.',parse_mode='Markdown',reply_markup=main_keyboard())
            return
        await _prepare_tour_pdf_request(query.message, context, detail, mode)
        return

    if query.data.startswith("tour_output:"):
        parts = query.data.split(":")
        output = parts[1]
        detail = parts[2]
        if _tour_v2_active(context):
            data=copy.deepcopy(context.user_data.get("itinerary") or {})
            if not data:
                await query.message.reply_text("No itinerary data is available. Please start the tour workflow again.", reply_markup=main_keyboard())
                return
            try:
                if str(data.get("detail_level") or "basic").lower()!=detail:
                    status=await query.message.reply_text(f"✨ Preparing the {detail} itinerary...")
                    old_name=str(data.get("client_name") or "")
                    data=await _run_ai_with_retry_status(query.message,lambda: asyncio.to_thread(enhance_package_itinerary,data,GEMINI_API_KEY,GEMINI_MODEL,detail),status=status)
                    data["client_name"]=old_name or str(data.get("client_name") or "")
                    data["detail_level"]=detail
                    await safe_status_edit(status,query.message,"✅ Itinerary detail level ready.")
                data["show_cost"]=bool(data.get("show_cost") and data.get("package_costs"))
                context.user_data["itinerary"]=data
                if output=="whatsapp":
                    await safe_callback_edit(query, f"✅ {detail.title()} WhatsApp selected. Generating now...")
                    await reply_text_chunked(query.message,build_whatsapp_itinerary(data,detail),parse_mode="Markdown")
                    await query.message.reply_text("✅ WhatsApp itinerary generated. You can choose another format below.",reply_markup=_tour_v2_output_keyboard())
                    return
                if output=="pdf":
                    requested_mode = str(context.user_data.get("smart_requested_document_mode") or "").lower()
                    if requested_mode in ("quotation", "voucher"):
                        await safe_callback_edit(
                            query,
                            f"📄 *{detail.title()} {'Tour Quotation' if requested_mode=='quotation' else 'Tour Voucher'} selected from your AI Assistant request.*\n\nGenerating now...",
                            parse_mode="Markdown",
                        )
                        await _prepare_tour_pdf_request(query.message, context, detail, requested_mode)
                    else:
                        await safe_callback_edit(
                            query,
                            f"📄 *{detail.title()} PDF selected.*\n\nNow choose whether this should be a Tour Voucher or Tour Quotation.",
                            parse_mode="Markdown", reply_markup=tour_pdf_mode_keyboard(detail)
                        )
                    return
            except Exception as exc:
                logger.exception("Tour V2 output failed")
                await query.message.reply_text(f"❌ Tour output failed: {str(exc)[:700]}",reply_markup=main_keyboard())
                return
        document_mode = parts[3] if len(parts) > 3 else None
        data = context.user_data.get("itinerary")
        if not data:
            await query.message.reply_text("No itinerary data is available. Please start the tour workflow again.", reply_markup=main_keyboard())
            return
        try:
            if output == "pdf":
                requested_mode = document_mode or str(context.user_data.get("smart_requested_document_mode") or "").lower()
                if requested_mode in ('quotation','voucher'):
                    await _prepare_tour_pdf_request(query.message, context, detail, requested_mode)
                else:
                    await safe_callback_edit(
                        query,
                        f"📄 *{detail.title()} PDF selected.*\n\nNow choose whether this should be a Tour Quotation or Tour Voucher.",
                        parse_mode="Markdown", reply_markup=tour_pdf_mode_keyboard(detail)
                    )
                return
            if str(data.get("detail_level", "")).lower() != detail:
                old_name = str(data.get('client_name') or '').strip()
                data = await _run_ai_with_retry_status(query.message, lambda: asyncio.to_thread(enhance_package_itinerary, data, GEMINI_API_KEY, GEMINI_MODEL, detail))
                data["client_name"] = old_name or str(data.get("client_name") or "").strip()
                data["detail_level"] = detail
                if data.get('b2b') or data.get('brand_neutral') or context.user_data.get('pending_b2b'):
                    data = _apply_tour_document_mode_fields(
                        data,
                        context.user_data.get('pending_tour_document_mode') or data.get('document_mode') or 'itinerary',
                        b2b=True,
                    )
                    context.user_data['pending_b2b'] = True
                    context.user_data['pending_clean_agency'] = True
                    context.user_data['pending_tour_last_page'] = 'b2b'
                context.user_data["itinerary"] = data
            if output == "whatsapp":
                await reply_text_chunked(query.message, build_whatsapp_itinerary(data, detail), parse_mode="Markdown")
                await query.message.reply_text("📱 WhatsApp itinerary sent. You can choose another format or make changes.", reply_markup=draft_review_keyboard())
                return
        except Exception as exc:
            logger.exception("Tour output generation failed")
            await query.message.reply_text(f"❌ Tour output failed: {str(exc)[:700]}", reply_markup=main_keyboard())
        return

    if query.data in ("generate", "generate_no_cost"):
        data = context.user_data.get("itinerary")
        if not data:
            await safe_callback_edit(query, "No itinerary data is available. Please start again.")
            return
        data = dict(data)
        detail = data.get('detail_level') or 'basic'
        no_cost = query.data == 'generate_no_cost'
        await safe_callback_edit(query, "⏳ Generating the final Tour PDF with the default T&C...")
        try:
            await generate_tour_pdf_final(query.message, context, data, detail, no_cost)
            await query.message.reply_text('✅ Ready for the next request.', reply_markup=ready_keyboard())
        except Exception as exc:
            logger.exception('Tour PDF generation failed')
            await query.message.reply_text(f'❌ Tour PDF generation failed.\n\nReason: {str(exc)[:800]}', reply_markup=main_keyboard())
        return

    if query.data in ("footer_bar", "footer_design", "footer2", "print_clean", "footer_yes", "footer_no"):
        await safe_callback_edit(query, 'ℹ️ Footer is now controlled by /settings and by Modify & Regenerate after a PDF is generated.')
        return

    if query.data.startswith("fare_original:"):
        _cancel_auto_print(context)
        kind = query.data.split(":", 1)[1]
        supplier_total = float(context.user_data.get("pending_fare_supplier_total", 0) or 0)
        if supplier_total <= 0:
            await safe_callback_edit(query,
                "❌ No original supplier fare was found.\n\n"
                "Use *Add Cost* or *Print Without Fare* instead.",
                parse_mode="Markdown"
            )
            return
        context.user_data[f"pending_{kind}_fare"] = supplier_total
        context.user_data.pop("pending_fare_kind", None)
        context.user_data["pending_footer_kind"] = kind
        await safe_callback_edit(query, f"💰 *Original supplier fare selected: INR {supplier_total:,.2f}.*\n\nGenerating with the saved {kind.title()} footer setting...", parse_mode="Markdown")
        await _print_ticket_final(query.message, context, kind, footer_mode=_default_footer_mode(kind))
        return

    if query.data.startswith("fare_add:"):
        _cancel_auto_print(context)
        kind=query.data.split(":",1)[1]
        context.user_data["pending_fare_kind"]=kind
        supplier_total=float(context.user_data.get("pending_fare_supplier_total", 0) or 0)
        if kind=='hotel':
            if supplier_total > 0:
                prompt=(f"🏨 *Supplier hotel total: INR {supplier_total:,.0f}.*\n\n"
                        "Enter Hotel customer costing as: `Room 8500, EB 1200, Total 18200`\n\nEB is optional. A single amount like `8500` is also accepted.")
            else:
                prompt=("🏨 *Add Hotel Cost*\n\nEnter: `Room 8500, EB 1200, Total 18200`\n\nEB is optional. A single amount like `8500` is also accepted.")
        elif supplier_total > 0:
            prompt=(f"💰 *Supplier fare: INR {supplier_total:,.0f}.*\n\n"
                    "Enter either a final fare or a + / - markup.\n\n"
                    "Examples:\n`8500` → final fare ₹8,500\n`+500` → supplier fare + ₹500\n`-300` → supplier fare - ₹300")
        else:
            prompt=("💰 *No supplier fare is available.*\n\n"
                    "Enter the final fare directly. Example: `8500`\n\n"
                    "You can also use + / - only when a supplier fare has been found.")
        await safe_callback_edit(query, prompt,parse_mode='Markdown')
        return

    if query.data.startswith("fare_none:"):
        _cancel_auto_print(context)
        kind=query.data.split(":",1)[1]
        context.user_data[f"pending_{kind}_fare"]=None
        context.user_data.pop("pending_fare_kind", None)
        await safe_callback_edit(query, '🖨️ *Fare will not be printed.*\n\nGenerating with the saved footer setting...', parse_mode='Markdown')
        context.user_data["pending_footer_kind"] = kind
        await _print_ticket_final(query.message, context, kind, footer_mode=_default_footer_mode(kind))
        return




def _footer_setting_label(mode):
    return {'design':'Footer 1 (Old Design)','footer2':'Footer 2 (New Design)','bar':'Contact Bar'}.get(mode, 'Footer 2 (New Design)')

def settings_footer_keyboard(kind):
    current=get_default_footer(kind)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(('✅ ' if current=='design' else '')+'Footer 1 • Old Design',callback_data=f'settings:footer:{kind}:design')],
        [InlineKeyboardButton(('✅ ' if current=='footer2' else '')+'Footer 2 • New Design',callback_data=f'settings:footer:{kind}:footer2')],
        [InlineKeyboardButton(('✅ ' if current=='bar' else '')+'Contact Bar',callback_data=f'settings:footer:{kind}:bar')],
        [InlineKeyboardButton('⬅️ Back to Settings',callback_data='settings:open')],
    ])

def settings_tour_last_page_keyboard():
    current=get_tour_last_page()
    opts=[('tc_non_google','📜 T&C NON GOOGLE'),('without_footer','📄 Without Footer')]
    rows=[]
    for key,label in opts:
        rows.append([InlineKeyboardButton(('✅ ' if current==key else '')+label,callback_data=f'settings:tour_last_page:{key}')])
    rows.append([InlineKeyboardButton('⬅️ Back to Settings',callback_data='settings:open')])
    return InlineKeyboardMarkup(rows)

def settings_logo_keyboard(kind):
    current=get_logo_scale(kind)
    label={'flight':'Air','bus':'Bus','hotel':'Hotel','package':'Tour'}[kind]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('➖ Logo Size',callback_data=f'settings:logo:{kind}:-0.10'), InlineKeyboardButton(f'{int(round(current*100))}%',callback_data='settings:noop'), InlineKeyboardButton('Logo Size ➕',callback_data=f'settings:logo:{kind}:0.10')],
        [InlineKeyboardButton('⬅️ Back to Settings',callback_data='settings:open')],
    ])


def settings_keyboard():
    s=load_settings()
    def mark(key,label): return f"{'✅' if s['buttons'].get(key,True) else '❌'} {label}"
    fd=s.get('footer_defaults',{})
    return InlineKeyboardMarkup([
        [InlineKeyboardButton('🔤 Font: '+s['font'],callback_data='settings:fonts')],
        [InlineKeyboardButton('➖ Text Size',callback_data='settings:size:-0.05'),InlineKeyboardButton(f"🔠 {int(round(s['text_scale']*100))}%",callback_data='settings:noop'),InlineKeyboardButton('➕ Text Size',callback_data='settings:size:0.05')],
        [InlineKeyboardButton('✈️ Air Footer: '+_footer_setting_label(fd.get('flight','footer2')),callback_data='settings:footer_menu:flight')],
        [InlineKeyboardButton('🚌 Bus Footer: '+_footer_setting_label(fd.get('bus','footer2')),callback_data='settings:footer_menu:bus')],
        [InlineKeyboardButton('🏨 Hotel Footer: '+_footer_setting_label(fd.get('hotel','footer2')),callback_data='settings:footer_menu:hotel')],
        [InlineKeyboardButton('🗺️ Tour Footer: '+_footer_setting_label(fd.get('package','footer2')),callback_data='settings:footer_menu:package')],
        [InlineKeyboardButton(f"✈️ Air Logo: {int(round(get_logo_scale('flight')*100))}%",callback_data='settings:logo_menu:flight'),InlineKeyboardButton(f"🚌 Bus Logo: {int(round(get_logo_scale('bus')*100))}%",callback_data='settings:logo_menu:bus')],
        [InlineKeyboardButton(f"🏨 Hotel Logo: {int(round(get_logo_scale('hotel')*100))}%",callback_data='settings:logo_menu:hotel'),InlineKeyboardButton(f"🗺️ Tour Logo: {int(round(get_logo_scale('package')*100))}%",callback_data='settings:logo_menu:package')],
        [InlineKeyboardButton(mark('make_changes','Smart Changes'),callback_data='settings:toggle:make_changes')],
        [InlineKeyboardButton(mark('add_cost','Add Cost'),callback_data='settings:toggle:add_cost'),InlineKeyboardButton(mark('print_without_fare','Without Fare'),callback_data='settings:toggle:print_without_fare')],
        [InlineKeyboardButton(mark('print_original_fare','Original Fare'),callback_data='settings:toggle:print_original_fare'),InlineKeyboardButton(mark('page_size_controls','Page Sizes'),callback_data='settings:toggle:page_size_controls')],
        [InlineKeyboardButton(mark('watermark','Watermark'),callback_data='settings:toggle:watermark')],
        [InlineKeyboardButton('◀️ Opacity',callback_data='settings:wm_opacity:-0.01'),InlineKeyboardButton(f"{int(round(s['watermark_opacity']*100))}%",callback_data='settings:noop'),InlineKeyboardButton('Opacity ▶️',callback_data='settings:wm_opacity:0.01')],
        [InlineKeyboardButton('◀️ Scale',callback_data='settings:wm_scale:-0.10'),InlineKeyboardButton(f"{int(round(s['watermark_scale']*100))}%",callback_data='settings:noop'),InlineKeyboardButton('Scale ▶️',callback_data='settings:wm_scale:0.10')],
        [InlineKeyboardButton('📜 Tour Last Page: '+terms_label(get_tour_last_page()),callback_data='settings:tour_last_page_menu')],
        [InlineKeyboardButton(mark('main_tour','Tour'),callback_data='settings:toggle:main_tour'),InlineKeyboardButton(mark('main_air','Air'),callback_data='settings:toggle:main_air')],
        [InlineKeyboardButton(mark('main_bus','Bus'),callback_data='settings:toggle:main_bus'),InlineKeyboardButton(mark('main_hotel','Hotel'),callback_data='settings:toggle:main_hotel')],
        [InlineKeyboardButton(mark('main_ai','AI Assistant'),callback_data='settings:toggle:main_ai')],
        [InlineKeyboardButton(mark('main_settings','Settings Button'),callback_data='settings:toggle:main_settings')],
        [InlineKeyboardButton('♻️ Reset Settings',callback_data='settings:reset')],
    ])

def settings_font_keyboard():
    current=load_settings()['font']
    rows=[[InlineKeyboardButton(('✅ ' if name==current else '')+name,callback_data=f'settings:font:{name}')] for name in FONT_OPTIONS]
    rows.append([InlineKeyboardButton('⬅️ Back to Settings',callback_data='settings:open')])
    return InlineKeyboardMarkup(rows)

async def settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update): return
    s=load_settings(); fd=s.get('footer_defaults',{})
    await update.message.reply_text(
        '⚙️ *MyTourBazar Print Settings*\n\n'
        f"Font: *{s['font']}*\nText size: *{int(round(s['text_scale']*100))}%*\n"
        f"✈️ Air Footer: *{_footer_setting_label(fd.get('flight','footer2'))}*\n"
        f"🚌 Bus Footer: *{_footer_setting_label(fd.get('bus','footer2'))}*\n"
        f"🏨 Hotel Footer: *{_footer_setting_label(fd.get('hotel','footer2'))}*\n"
        f"🗺️ Tour Footer: *{_footer_setting_label(fd.get('package','footer2'))}*\n"
        f"Logo sizes: Air {int(round(get_logo_scale('flight')*100))}% | Bus {int(round(get_logo_scale('bus')*100))}% | Hotel {int(round(get_logo_scale('hotel')*100))}% | Tour {int(round(get_logo_scale('package')*100))}%\n"
        f"Watermark: *{'ON' if s['buttons'].get('watermark') else 'OFF'}* | Opacity: *{int(round(s['watermark_opacity']*100))}%* | Scale: *{int(round(s['watermark_scale']*100))}%*\n"
        f"Tour last page: *{terms_label(get_tour_last_page())}*\n",parse_mode='Markdown',reply_markup=settings_keyboard())

async def settings_callback(query, context):
    if query.data=='settings:noop': await query.answer(); return
    if query.data=='settings:open': await query.edit_message_text('⚙️ *MyTourBazar Print Settings*',parse_mode='Markdown',reply_markup=settings_keyboard()); return
    if query.data=='settings:tour_last_page_menu':
        await query.edit_message_text('📜 *Tour Last Page Setting*\n\nChoose which page is appended after the Tour itinerary by default.',parse_mode='Markdown',reply_markup=settings_tour_last_page_keyboard()); return
    if query.data.startswith('settings:tour_last_page:'):
        choice=query.data.split(':',2)[2]
        set_tour_last_page(choice)
        await query.edit_message_text(f'✅ Tour last page changed to *{terms_label(choice)}*.',parse_mode='Markdown',reply_markup=settings_tour_last_page_keyboard()); return
    if query.data.startswith('settings:footer_menu:'):
        kind=query.data.split(':',2)[2]
        label={'flight':'Air','bus':'Bus','hotel':'Hotel','package':'Tour'}[kind]
        await query.edit_message_text(f'🧾 *{label} Footer Setting*\n\nChoose the footer used for all future {label} prints.',parse_mode='Markdown',reply_markup=settings_footer_keyboard(kind)); return
    if query.data.startswith('settings:footer:'):
        _,_,kind,mode=query.data.split(':',3)
        set_default_footer(kind,mode)
        await query.edit_message_text(f'✅ {kind.title()} footer changed to *{_footer_setting_label(mode)}*.',parse_mode='Markdown',reply_markup=settings_footer_keyboard(kind)); return
    if query.data.startswith('settings:logo_menu:'):
        kind=query.data.split(':',2)[2]; label={'flight':'Air','bus':'Bus','hotel':'Hotel','package':'Tour'}[kind]
        await query.edit_message_text(f'🖼️ *{label} Logo Size*\n\nUse + / − for all future {label} prints.',parse_mode='Markdown',reply_markup=settings_logo_keyboard(kind)); return
    if query.data.startswith('settings:logo:'):
        _,_,kind,delta=query.data.split(':',3); ss=adjust_logo_scale(kind,float(delta)); await query.edit_message_text(f"✅ {kind.title()} logo size: *{int(round(ss['logo_scales'][kind]*100))}%*",parse_mode='Markdown',reply_markup=settings_logo_keyboard(kind)); return
    if query.data=='settings:fonts': await query.edit_message_text('🔤 *Choose print font*',parse_mode='Markdown',reply_markup=settings_font_keyboard()); return
    if query.data.startswith('settings:font:'):
        name=query.data.split(':',2)[2]; set_font(name); await query.edit_message_text(f'✅ Print font changed to *{name}*.',parse_mode='Markdown',reply_markup=settings_keyboard()); return
    if query.data.startswith('settings:size:'):
        delta=float(query.data.split(':',2)[2]); ss=adjust_text_scale(delta); await query.edit_message_text(f"✅ Text size is now *{int(round(ss['text_scale']*100))}%*.",parse_mode='Markdown',reply_markup=settings_keyboard()); return
    if query.data.startswith('settings:wm_opacity:'):
        delta=float(query.data.split(':',2)[2]); ss=load_settings(); ss['watermark_opacity']=round(max(.01,min(.20,float(ss.get('watermark_opacity',.04))+delta)),2); save_settings(ss); await query.edit_message_text(f"✅ Watermark opacity: *{int(round(ss['watermark_opacity']*100))}%*",parse_mode='Markdown',reply_markup=settings_keyboard()); return
    if query.data.startswith('settings:wm_scale:'):
        delta=float(query.data.split(':',2)[2]); ss=load_settings(); ss['watermark_scale']=round(max(.5,min(2.0,float(ss.get('watermark_scale',1.5))+delta)),2); save_settings(ss); await query.edit_message_text(f"✅ Watermark scale: *{int(round(ss['watermark_scale']*100))}%*",parse_mode='Markdown',reply_markup=settings_keyboard()); return
    if query.data.startswith('settings:toggle:'):
        key=query.data.split(':',2)[2]; toggle_button(key); await query.edit_message_text('✅ Setting updated.',reply_markup=settings_keyboard()); return
    if query.data=='settings:reset': reset_settings(); await query.edit_message_text('♻️ Settings reset to defaults. Footer 2 is the default for all services.',reply_markup=settings_keyboard()); return


async def set_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    context.user_data["waiting_for_logo"] = True
    await update.message.reply_text(
        "🖼️ Send your MyTourBazar logo as an image now."
    )


async def receive_logo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    if not context.user_data.get("waiting_for_logo"):
        return

    photo = update.message.photo[-1]
    tg_file = await context.bot.get_file(photo.file_id)
    await tg_file.download_to_drive(USER_LOGO_PATH)
    global LOGO_PATH
    LOGO_PATH = USER_LOGO_PATH
    context.user_data["waiting_for_logo"] = False

    await update.message.reply_text(
        "✅ Logo saved successfully.",
        reply_markup=main_keyboard()
    )


async def receive_extra_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return

    text = (update.message.text or "").strip()

    if context.user_data.get("post_hotel_cost_reference"):
        await _process_post_generated_hotel_costing(update.message,context,text); return

    if context.user_data.get("post_cost_reference"):
        await _process_post_generated_costing(update.message,context,text); return

    if context.user_data.get("post_transit_reference"):
        if re.fullmatch(r"(?i)\s*(?:ok\s*)?(?:no\s+transit|no\s+journey|none|skip|cancel\s+transit)\s*", text or ''):
            context.user_data.pop('post_transit_reference',None)
            context.user_data.pop('post_transit_pending',None)
            await update.message.reply_text('✅ No transit added. Your Tour is still ready to use.',reply_markup=main_keyboard())
            return
        await _process_post_generated_transit_text(update.message,context,text); return

    if _tour_v2_active(context):
        phase=context.user_data.get("tour_v2_phase")
        if phase=="awaiting_edited_final":
            await _tour_v2_process_edited_final(update.message,context,text); return
        if phase=="missing_details":
            await _tour_v2_apply_missing_reply(update.message,context,text); return
        if phase in ("onward","return","connection"):
            skip_labels={"onward":"⏭️ No Onward Journey","return":"⏭️ No Return Journey","connection":"⏭️ No Connecting Journey"}
            if text=="❌ Cancel": return await cancel(update,context)
            if text==skip_labels[phase]:
                if phase=="onward": await _tour_v2_ask_return(update.message,context)
                elif phase=="return": await _tour_v2_ask_connection(update.message,context)
                else: await _tour_v2_show_outputs(update.message,context)
                return
            if text:
                await _tour_v2_extract_journey(update.message,context,phase,source_text=text); return
        if phase=="costing":
            await _tour_v2_finish_selected_output(update.message,context,text); return

    if context.user_data.get("awaiting_tour_transit_input"):
        if text=="❌ Cancel":
            for k in ("awaiting_tour_transit_input","pending_tour_transit_files","pending_tour_transit_text"):
                context.user_data.pop(k,None)
            await update.message.reply_text("❌ Transit entry cancelled.",reply_markup=draft_review_keyboard()); return
        if text=="⏭️ Skip Transit":
            data=context.user_data.get("itinerary") or {}
            data["transit"]=[]; data["transit_done_by_self"]=True; context.user_data["itinerary"]=data
            for k in ("awaiting_tour_transit_input","pending_tour_transit_files","pending_tour_transit_text"):
                context.user_data.pop(k,None)
            await update.message.reply_text("✅ Transit skipped. The box will show *Done by Self*.",parse_mode="Markdown",reply_markup=ReplyKeyboardRemove())
            await _continue_tour_pdf_after_transit(update.message,context); return
        if text=="✅ Done Transit":
            await _process_pending_tour_transit(update.message,context); return
        if text:
            context.user_data["pending_tour_transit_text"]=(str(context.user_data.get("pending_tour_transit_text") or "")+"\n"+text).strip()
            await update.message.reply_text("📝 Transit text received. Send more or tap *✅ Done Transit*.",parse_mode="Markdown",reply_markup=tour_transit_input_keyboard()); return

    if context.user_data.get('awaiting_tour_print_name'):
        if text == '❌ Cancel':
            context.user_data.pop('awaiting_tour_print_name', None)
            context.user_data.pop('pending_tour_pdf_request', None)
            await update.message.reply_text('❌ Tour PDF print cancelled. The draft is still available.', reply_markup=main_keyboard())
            return
        data = context.user_data.get('itinerary') or {}
        if text == '⏭️ Print Without Name':
            data['client_name'] = ''
        else:
            if not text:
                await update.message.reply_text('Please enter the Guest / Client Name, or tap ⏭️ Print Without Name.', reply_markup=pending_tour_name_keyboard())
                return
            data['client_name'] = text
            context.user_data['guest_name'] = text
            await update.message.reply_text(f'✅ Guest name added: *{text}*', parse_mode='Markdown', reply_markup=ReplyKeyboardRemove())
        context.user_data['itinerary'] = data
        context.user_data.pop('awaiting_tour_print_name', None)
        await _finish_pending_tour_pdf(update.message, context)
        return

    if context.user_data.get("awaiting_edit_ref"):
        await _begin_edit(update, context, text)
        return
    if context.user_data.get("editing_current_itinerary"):
        await perform_draft_edit(update, context, text)
        return
    if context.user_data.get("editing_reference"):
        await perform_saved_edit(update, context, text)
        return
    if context.user_data.get('pending_custom_cost_input'):
        text = (update.message.text or '').strip()
        fields = context.user_data.get('pending_custom_cost_fields') or []
        idx = int(context.user_data.get('pending_custom_cost_index') or 0)
        data = context.user_data.get('pending_custom_cost_data')
        if not data or idx >= len(fields):
            await update.message.reply_text('Tap ☑️ Done to finish Custom Cost, or ❌ Cancel.')
            return
        raw = text.replace('₹','').replace(',','').strip()
        if not re.fullmatch(r'\d+(?:\.\d+)?', raw):
            await update.message.reply_text('❌ Enter only the amount, for example `1000`.', parse_mode='Markdown', reply_markup=custom_cost_keyboard())
            return
        amount=float(raw)
        key, field, label, count = fields[idx]
        data = _apply_custom_cost_field(data, field, amount)
        context.user_data['pending_custom_cost_data'] = data
        idx += 1
        context.user_data['pending_custom_cost_index'] = idx
        if idx < len(fields):
            _, _, next_label, next_count = fields[idx]
            await update.message.reply_text(
                f'✅ {label}: *{_money(amount)} × {count} = {_money(amount*count)}*\n\n'
                f'*{next_label}* — {next_count} passenger(s)\n'
                f'Enter the direct per-person cost. Example: `1000`.',
                parse_mode='Markdown', reply_markup=custom_cost_keyboard())
        else:
            await update.message.reply_text(
                '✅ All direct rates entered.\n\n' + _custom_cost_summary(data) + '\n\nTap *☑️ Done* to put these amounts into the cost box and regenerate the PDF.',
                parse_mode='Markdown', reply_markup=custom_cost_keyboard())
        return
    # V159: no Tour markup text session. Any Tour cost change goes through the
    # saved-reference Modify & Regenerate path as a direct customer selling rate.

    if context.user_data.get("pending_fare_kind"):
        _cancel_auto_print(context)
        kind=context.user_data.pop("pending_fare_kind")
        supplier_total=float(context.user_data.get("pending_fare_supplier_total", 0) or 0)
        try:
            if kind=='hotel':
                hotel_cost=_parse_hotel_cost_input(text,supplier_total)
                hdata=copy.deepcopy(context.user_data.get('pending_hotel_data') or {})
                hdata['customer_hotel_cost']=hotel_cost
                context.user_data['pending_hotel_data']=hdata
                fare=float(hotel_cost.get('total') or 0) or None
            else:
                fare=_parse_markup_input(text, supplier_total)
        except ValueError as exc:
            context.user_data['pending_fare_kind']=kind
            hint='Room 8500, EB 1200, Total 18200' if kind=='hotel' else '8500, +500, or -300'
            await update.message.reply_text(f'❌ {exc}\n\nUse `{hint}` as applicable.',parse_mode='Markdown')
            return
        context.user_data[f'pending_{kind}_fare']=fare
        try:
            await ask_footer_choice(update.message, context, kind)
        except Exception as exc:
            logger.exception('PDF generation from markup input failed')
            await update.message.reply_text(f'❌ PDF generation failed.\n\nReason: `{str(exc)[:800]}`', parse_mode='Markdown', reply_markup=main_keyboard())
        return


    if text == "✍️ Flight Text" and context.user_data.get("awaiting_flight"):
        await update.message.reply_text(
            "✍️ Send the flight / train details as text. No special formatting is required. "
            "Send onward and return details together or separately, then tap *✅ Done*.",
            reply_markup=ReplyKeyboardMarkup([["✍️ Flight Text"], ["✈️ Flight Screenshot"], ["✅ Done"]], resize_keyboard=True)
        )
        return

    if text == "✈️ Flight Screenshot" and context.user_data.get("awaiting_flight"):
        await update.message.reply_text(
            "✈️ Send the flight screenshot now. You can send multiple screenshots, then tap *✅ Done*.",
            reply_markup=ReplyKeyboardMarkup([["✍️ Flight Text"], ["✈️ Flight Screenshot"], ["✅ Done"]], resize_keyboard=True)
        )
        return

    if context.user_data.get("awaiting_flight"):
        value = (update.message.text or "").strip()
        if value == "✅ Done":
            context.user_data["awaiting_flight"] = False
            await process_sources(update, context)
            return
        if value:
            context.user_data["flight_text"] = (context.user_data.get("flight_text", "") + "\n" + value).strip()
            context.user_data["awaiting_flight"] = True
            await update.message.reply_text(
                "✈️ Flight text received. Send another flight screenshot/text, or tap *✅ Done*.",
                reply_markup=confirmation_keyboard()
            )
        return

    mode = context.user_data.get("awaiting_extra")
    if not mode:
        # Direct supplier text is accepted even when the owner did not first open
        # AI Assistant. This restores the older V100 convenience: paste supplier
        # material and the bot automatically identifies Tour/Air/Bus/Hotel.
        if _looks_like_supplier_material(text) and not context.user_data.get('smart_mode'):
            _cancel_source_auto_process(context)
            context.user_data['smart_mode']=True
            context.user_data['smart_text']=text
            context.user_data['smart_files']=[]
            await smart_process(update, context)
            return
        # Outside a dedicated workflow, let the AI recognize what the user means.
        if context.user_data.get("smart_mode"):
            await smart_text(update, context)
        else:
            await smart_text(update, context)
        return
    value = (update.message.text or "").strip()
    if not value:
        return
    context.user_data.setdefault("extra_inclusions", [])
    context.user_data.setdefault("extra_exclusions", [])
    key = "extra_inclusions" if mode == "inclusion" else "extra_exclusions"
    context.user_data[key].append(value)
    context.user_data["awaiting_extra"] = None

    data = context.user_data.get("itinerary", {})
    data.setdefault("inclusions", [])
    data.setdefault("exclusions", [])
    target = data["inclusions"] if mode == "inclusion" else data["exclusions"]
    if value not in target:
        target.append(value)
    context.user_data["itinerary"] = data

    await update.message.reply_text(
        build_confirmation(data),
        parse_mode="Markdown",
        reply_markup=confirmation_keyboard()
    )


async def receive_flight_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    if not context.user_data.get("awaiting_flight"):
        return

    try:
        photo = update.message.photo[-1]
        tg_file = await context.bot.get_file(photo.file_id)
        filename = TEMP_DIR / f"flight_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
        await tg_file.download_to_drive(filename)
        context.user_data.setdefault("flight_files", []).append(str(filename))
        context.user_data["awaiting_flight"] = True

        await update.message.reply_text(
            "✈️ Flight screenshot received. Send another flight screenshot/text, or tap *Done* when finished.",
            reply_markup=ReplyKeyboardMarkup([["✍️ Flight Text"], ["✈️ Flight Screenshot"], ["✅ Done"]], resize_keyboard=True)
        )
    except Exception as exc:
        logger.exception("Flight screenshot failed")
        await update.message.reply_text(f"❌ Could not process the flight screenshot: {exc}")


async def receive_global_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route explicit workflow states first; otherwise a normal upload is always NEW work."""
    if context.user_data.get("post_transit_reference"):
        await update.message.reply_text("Type each flight/train sector on a new line. No Onward/Return prefix is required; I will infer the journey sequence.",reply_markup=ReplyKeyboardRemove())
        return

    if _tour_v2_active(context) and context.user_data.get("tour_v2_phase") in ("onward","return","connection"):
        phase=context.user_data.get("tour_v2_phase")
        try:
            photo=update.message.photo[-1]; tg_file=await context.bot.get_file(photo.file_id)
            path=TEMP_DIR / f"tour_v2_{phase}_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
            await tg_file.download_to_drive(path)
            await _tour_v2_extract_journey(update.message,context,phase,file_path=path)
        except Exception as exc:
            await update.message.reply_text(f"❌ Could not read journey screenshot: {str(exc)[:500]}")
        return
    if context.user_data.get("awaiting_tour_transit_input"):
        try:
            photo=update.message.photo[-1]; tg_file=await context.bot.get_file(photo.file_id)
            path=TEMP_DIR / f"tour_transit_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}.jpg"
            await tg_file.download_to_drive(path)
            context.user_data.setdefault("pending_tour_transit_files",[]).append(str(path))
            await update.message.reply_text("📸 Transit screenshot received. Send more or tap *✅ Done Transit*.",parse_mode="Markdown",reply_markup=tour_transit_input_keyboard())
        except Exception as exc:
            await update.message.reply_text(f"❌ Could not save transit screenshot: {str(exc)[:500]}",reply_markup=tour_transit_input_keyboard())
        return
    if context.user_data.get("waiting_for_logo"):
        return await receive_logo(update, context)
    if context.user_data.get("awaiting_flight"):
        return await receive_flight_photo(update, context)
    # V165: normal file/image drop after a completed print is always a NEW job.
    # Existing direct/Auto Creation batches are the only states allowed to accumulate files.
    if not context.user_data.get('_direct_drop_mode') and not context.user_data.get('auto_creation'):
        _cancel_auto_print(context)
        _cancel_source_auto_process(context)
        context.user_data.clear()
    context.user_data["smart_mode"] = True
    context.user_data["_direct_drop_mode"] = True
    context.user_data.setdefault("smart_files", [])
    return await smart_photo(update, context)


async def receive_global_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route explicit workflow states first; otherwise a normal upload is always NEW work."""
    if context.user_data.get("post_transit_reference"):
        await update.message.reply_text("Type each flight/train sector on a new line. No Onward/Return prefix is required; I will infer the journey sequence.",reply_markup=ReplyKeyboardRemove())
        return

    if _tour_v2_active(context) and context.user_data.get("tour_v2_phase") in ("onward","return","connection"):
        phase=context.user_data.get("tour_v2_phase")
        doc=update.message.document; name=doc.file_name or "journey.pdf"; mime=(doc.mime_type or "").lower()
        if not (name.lower().endswith(".pdf") or mime=="application/pdf"):
            await update.message.reply_text("Please send a PDF, screenshot or normal text."); return
        try:
            tg_file=await context.bot.get_file(doc.file_id)
            safe="".join(c if c.isalnum() or c in "._-" else "_" for c in name)
            path=TEMP_DIR / f"tour_v2_{phase}_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}_{safe}"
            await tg_file.download_to_drive(path)
            await _tour_v2_extract_journey(update.message,context,phase,file_path=path)
        except Exception as exc:
            await update.message.reply_text(f"❌ Could not read journey PDF: {str(exc)[:500]}")
        return
    if context.user_data.get("awaiting_tour_transit_input"):
        doc=update.message.document; name=doc.file_name or "transit.pdf"; mime=(doc.mime_type or "").lower()
        if not (name.lower().endswith(".pdf") or mime=="application/pdf"):
            await update.message.reply_text("Please send a flight-ticket PDF, screenshot or text.",reply_markup=tour_transit_input_keyboard()); return
        try:
            tg_file=await context.bot.get_file(doc.file_id)
            safe="".join(c if c.isalnum() or c in "._-" else "_" for c in name)
            path=TEMP_DIR / f"tour_transit_{update.effective_user.id}_{datetime.now():%Y%m%d_%H%M%S_%f}_{safe}"
            await tg_file.download_to_drive(path)
            context.user_data.setdefault("pending_tour_transit_files",[]).append(str(path))
            n=len(context.user_data.get("pending_tour_transit_files") or [])
            await update.message.reply_text(f"📄 Transit PDF received ({n}). Send more or tap *✅ Done Transit*.",parse_mode="Markdown",reply_markup=tour_transit_input_keyboard())
        except Exception as exc:
            await update.message.reply_text(f"❌ Could not save transit PDF: {str(exc)[:500]}",reply_markup=tour_transit_input_keyboard())
        return
    if not context.user_data.get('_direct_drop_mode') and not context.user_data.get('auto_creation'):
        _cancel_auto_print(context)
        _cancel_source_auto_process(context)
        context.user_data.clear()
    context.user_data["smart_mode"] = True
    context.user_data["_direct_drop_mode"] = True
    context.user_data.setdefault("smart_files", [])
    return await smart_document(update, context)


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _cancel_source_auto_process(context)
    _cancel_auto_print(context)
    for key in ('_source_processing','smart_mode','awaiting_edit_ref','editing_reference','editing_current_itinerary','pending_tour_markup_print','pending_fare_kind'):
        context.user_data.pop(key, None)
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled. Current workflow cleared.", reply_markup=main_keyboard())
    return ConversationHandler.END


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Unhandled exception", exc_info=context.error)




def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing in .env")

    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    voucher_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🏨 Hotel Print$"), hotel_voucher_start)],
        states={
            HOTEL_VOUCHER_INPUT: [
                MessageHandler(filters.PHOTO, hotel_voucher_photo),
                MessageHandler(filters.Document.PDF, hotel_voucher_document),
                MessageHandler(filters.TEXT & ~filters.COMMAND, hotel_voucher_text),
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(r"^❌ Cancel$"), cancel)],
        allow_reentry=True,
    )

    flight_ticket_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^✈️ Air Print$"), flight_ticket_start)],
        states={
            FLIGHT_TICKET_INPUT:[MessageHandler(filters.PHOTO,flight_ticket_photo),MessageHandler(filters.Document.PDF,flight_ticket_document),MessageHandler(filters.TEXT & ~filters.COMMAND,flight_ticket_text)],
            FLIGHT_FARE_INPUT:[MessageHandler(filters.TEXT & ~filters.COMMAND,flight_ticket_fare)],
        },
        fallbacks=[CommandHandler("cancel",cancel),MessageHandler(filters.Regex(r"^❌ Cancel$"),cancel)],allow_reentry=True)

    bus_ticket_conversation = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex(r"^🚌 Bus Print$"), bus_ticket_start)],
        states={
            BUS_TICKET_INPUT:[MessageHandler(filters.PHOTO,bus_ticket_photo),MessageHandler(filters.Document.PDF,bus_ticket_document),MessageHandler(filters.TEXT & ~filters.COMMAND,bus_ticket_text)],
            BUS_FARE_INPUT:[MessageHandler(filters.TEXT & ~filters.COMMAND,bus_ticket_fare)]
        },
        fallbacks=[CommandHandler("cancel",cancel),MessageHandler(filters.Regex(r"^❌ Cancel$"),cancel)],allow_reentry=True)

    smart_conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^🤖 AI Assistant / New Request$"), smart_ai_start),
            MessageHandler(filters.Regex(r"^🤖 Auto Creation$"), auto_creation_start),
        ],
        states={
            SMART_INPUT: [
                MessageHandler(filters.PHOTO, smart_photo),
                MessageHandler(filters.Document.PDF, smart_document),
                MessageHandler(filters.TEXT & ~filters.COMMAND, smart_text),
            ],
            WAITING_GUEST_NAME: [
                MessageHandler(filters.PHOTO, receive_tour_source_without_guest),
                MessageHandler(filters.Document.PDF, receive_tour_source_without_guest),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_guest_name),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(r"^❌ Cancel$"), cancel)],
        allow_reentry=True,
    )

    conversation = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^(?:🗺️ Tour Itinerary|🗺️ Tour Guide)$"), new_itinerary),
            CommandHandler("new", new_itinerary),
        ],
        states={
            WAITING_GUEST_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_guest_name),
            ],
            WAITING_SOURCE: [
                MessageHandler(filters.PHOTO, receive_photo),
                MessageHandler(filters.Document.PDF, receive_document),
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_text),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            MessageHandler(filters.Regex(r"^❌ Cancel$"), cancel),
        ],
        allow_reentry=True,
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("stop", stop_bot_workflow))
    app.add_handler(MessageHandler(filters.Regex(r"^▶️ /START$"), start), group=-2)
    app.add_handler(MessageHandler(filters.Regex(r"^⏹️ /STOP$"), stop_bot_workflow), group=-2)
    app.add_handler(CommandHandler(["settings", "setting"], settings_command))
    # Settings must win over every active ConversationHandler state. Otherwise a
    # pressed Settings reply-keyboard button can be consumed as supplier text and
    # accidentally start itinerary processing.
    async def _settings_menu_guard(update, context):
        await settings_command(update, context)
        raise ApplicationHandlerStop

    app.add_handler(MessageHandler(filters.Regex(r"^⚙️ Settings$"), _settings_menu_guard), group=-2)

    # MUST run before every ConversationHandler. This makes Telegram's Reply action
    # work for fare prompts and generated-reference messages, regardless of which
    # workflow is currently active. Non-reply messages are untouched.
    # V160: all normal text passes this lightweight guard before any ConversationHandler.
    # It only consumes the update when a Modify & Regenerate session (or a bot-message
    # reply) is active; otherwise it returns and normal menu/source routing continues.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_reference_edit), group=-1)

    # Register ALL inline callbacks, including Tour output buttons, before the
    # workflow handlers. This ensures every visible Tour button has a live route.
    app.add_handler(CallbackQueryHandler(
        callback_handler,
        pattern=r"^(settings:.*|tour_terms:.*|tour_special_notes:.*|tour_cost:.*|tour_markup:.*|tour_custom_cost:.*|tour_output:.*|tour_output_mode:.*|tour_transit:.*|post_transit:.*|post_transit_make:.*|post_transit_cost:.*|post_cost:.*|hotel_cost:.*|tour_edit_current$|draft_edit$|draft_done$|generate|generate_no_cost|reenter|cancel|add_inclusion|add_exclusion|add_flight|add_flight_text|fare_add:.*|fare_none:.*|fare_original:.*|size:.*|footer_bar|footer_design|footer2|print_clean|footer_yes|footer_no|edit_generated:.*|voice_edit:.*|autofit:.*|modify:.*|mod_size:.*|mod_font:.*|mod_logo:.*|mod_clean:.*|mod_footer_menu:.*|mod_footer:.*|mod_detail:.*|mod_last_page:.*|mod_b2b:.*|mod_mode:.*|mod_done:.*|mod_cancel:.*)$"
    ), group=-1)

    app.add_handler(voucher_conversation)
    app.add_handler(flight_ticket_conversation)
    app.add_handler(bus_ticket_conversation)
    app.add_handler(smart_conversation)
    app.add_handler(conversation)
    app.add_handler(MessageHandler(filters.Regex(r"^🖼️ Set Logo$"), set_logo))
    app.add_handler(MessageHandler(filters.VOICE, receive_voice_edit), group=-1)
    app.add_handler(MessageHandler(filters.PHOTO, receive_global_photo))
    app.add_handler(MessageHandler(filters.Document.PDF, receive_global_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, reply_reference_edit))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, receive_extra_text))
    app.add_handler(MessageHandler(filters.Regex(r"^❌ Cancel$"), cancel))
    app.add_error_handler(error_handler)

    logger.info("MyTourBazar Gemini multimodal itinerary bot is running.")
    app.run_polling()


if __name__ == "__main__":
    main()
