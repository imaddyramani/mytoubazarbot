from pathlib import Path
import io
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

BASE_DIR = Path(__file__).resolve().parent
SOURCE_BAR = (BASE_DIR / "assets" / "mytourbazar_contact_bar_orange.png").resolve()

WHATSAPP_LINK = "https://wa.me/919425259086"
CALL_9826_LINK = "tel:+919826659086"
CALL_9753_LINK = "tel:+919753359086"
EMAIL_LINK = "mailto:sales@mytourbazar.com"
WEBSITE_LINK = "https://www.mytourbazar.com"

# Coordinates are based on the supplied 1800 x 100 orange contact bar.
# The visible fields are WhatsApp/mobile, Email and Web.
LINK_BOXES = [
    # Left: WhatsApp mobile number
    (0, 0, 600, 100, WHATSAPP_LINK),
    # Centre: email
    (600, 0, 1200, 100, EMAIL_LINK),
    # Right: website
    (1200, 0, 1800, 100, WEBSITE_LINK),
]

def _verify():
    if not SOURCE_BAR.is_file():
        raise FileNotFoundError(f"Contact bar artwork not found: {SOURCE_BAR}")
    return SOURCE_BAR


def _geometry(page_w, page_h):
    _verify()
    img = Image.open(SOURCE_BAR).convert("RGBA")
    iw, ih = img.size
    side = 10 * 72 / 25.4
    bottom = 4 * 72 / 25.4
    max_w = page_w - 2 * side
    max_h = min(22 * 72 / 25.4, page_h * 0.12)
    scale = min(max_w / iw, max_h / ih)
    w = iw * scale
    h = ih * scale
    x = (page_w - w) / 2
    y = bottom
    return img, iw, ih, scale, x, y, w, h


def _footer_page(page_w, page_h):
    img, iw, ih, scale, x, y, w, h = _geometry(page_w, page_h)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img), x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
    c.showPage(); c.save(); buf.seek(0)
    return PdfReader(buf).pages[0], (iw, ih, scale, x, y)


def _add_links(writer, page_index, geometry):
    iw, ih, scale, x, y = geometry
    for x1, y1, x2, y2, url in LINK_BOXES:
        rect = (
            x + x1 * scale,
            y + (ih - y2) * scale,
            x + x2 * scale,
            y + (ih - y1) * scale,
        )
        writer.add_annotation(page_index, Link(rect=rect, url=url, border=[0, 0, 0]))


def add_contact_bar_to_pdf(input_path, output_path):
    """Add the orange MyTourBazar contact bar without shrinking or covering ticket data."""
    reader = PdfReader(str(input_path))
    if not reader.pages:
        raise ValueError("Cannot add contact bar to an empty PDF")

    last_index = len(reader.pages) - 1
    last = reader.pages[last_index]
    page_w = float(last.mediabox.width)
    page_h = float(last.mediabox.height)
    img, iw, ih, scale, x, y, w, h = _geometry(page_w, page_h)

    # Detect existing text area. If the bar would overlap the final content,
    # append a clean same-size page instead of squeezing or covering anything.
    try:
        import fitz
        doc = fitz.open(str(input_path))
        blocks = doc[last_index].get_text("blocks")
        max_y = max((float(b[3]) for b in blocks if len(b) >= 4), default=page_h)
        doc.close()
    except Exception:
        max_y = page_h

    safety = 5 * 72 / 25.4
    free_space = page_h - max_y - y - safety

    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)

    if free_space >= h:
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(page_w, page_h))
        c.drawImage(ImageReader(img), x, y, width=w, height=h, preserveAspectRatio=True, mask="auto")
        c.showPage(); c.save(); buf.seek(0)
        overlay = PdfReader(buf).pages[0]
        writer.pages[last_index].merge_page(overlay)
        _add_links(writer, last_index, (iw, ih, scale, x, y))
    else:
        page, geom = _footer_page(page_w, page_h)
        writer.add_page(page)
        _add_links(writer, len(writer.pages) - 1, geom)

    with open(output_path, "wb") as fh:
        writer.write(fh)
