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
    from pdf_footer import add_footer
    return add_footer(input_path, output_path, _geometry, LINK_BOXES)
