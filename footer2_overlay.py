from pathlib import Path
import io
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

BASE_DIR = Path(__file__).resolve().parent
SOURCE_FOOTER2 = (BASE_DIR / 'assets' / 'mytourbazar_footer2_clean.png').resolve()

CALL_LINK = 'tel:+919425259086'
WHATSAPP_LINK = 'https://wa.me/919425259086'
EMAIL_LINK = 'mailto:sales@mytourbazar.com'
WEBSITE_LINK = 'https://www.mytourbazar.com'
INSTAGRAM_LINK = 'https://www.instagram.com/mytourbazar?igsh=MXNtOWk4dG1hdWc3Nw%3D%3D&utm_source=qr'
GOOGLE_REVIEW_LINK = 'https://rb.gy/whbhlt'

# Coordinates are in the processed footer2 image coordinate system (2071 x 496).
# Each hotspot covers the corresponding clickable column of the supplied design.
LINK_BOXES = [
    (775, 150, 985, 435, CALL_LINK),
    (985, 150, 1210, 435, WHATSAPP_LINK),
    (1210, 150, 1460, 435, EMAIL_LINK),
    (1460, 150, 1670, 435, WEBSITE_LINK),
    (1670, 150, 1855, 435, INSTAGRAM_LINK),
    (1855, 150, 2065, 435, GOOGLE_REVIEW_LINK),
]


def _verify():
    if not SOURCE_FOOTER2.is_file():
        raise FileNotFoundError(f'Footer 2 artwork not found: {SOURCE_FOOTER2}')
    return SOURCE_FOOTER2


def _geometry(page_w, page_h):
    _verify()
    img = Image.open(SOURCE_FOOTER2).convert('RGBA')
    iw, ih = img.size
    side = 10 * 72 / 25.4
    bottom = 4 * 72 / 25.4
    max_w = page_w - 2 * side
    # Footer2 is intentionally larger than the compact contact bar but still leaves
    # comfortable whitespace around the page content.
    max_h = min(76 * 72 / 25.4, page_h * 0.31)
    scale = min(max_w / iw, max_h / ih)
    draw_w = iw * scale
    draw_h = ih * scale
    x = (page_w - draw_w) / 2
    y = bottom
    return img, iw, ih, scale, x, y, draw_w, draw_h


def _footer_page(page_w, page_h):
    img, iw, ih, scale, x, y, w, h = _geometry(page_w, page_h)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img), x, y, width=w, height=h, preserveAspectRatio=True, mask='auto')
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


def add_footer2_to_pdf(input_path, output_path):
    """Append/overlay the supplied Footer 2 design without shrinking itinerary content."""
    reader = PdfReader(str(input_path))
    if not reader.pages:
        raise ValueError('Cannot add Footer 2 to an empty PDF')

    last_index = len(reader.pages) - 1
    last = reader.pages[last_index]
    page_w = float(last.mediabox.width)
    page_h = float(last.mediabox.height)
    img, iw, ih, scale, x, y, draw_w, draw_h = _geometry(page_w, page_h)

    try:
        import fitz
        doc = fitz.open(str(input_path))
        blocks = doc[last_index].get_text('blocks')
        max_y = max((float(b[3]) for b in blocks if len(b) >= 4), default=page_h)
        doc.close()
    except Exception:
        max_y = page_h

    safety = 5 * 72 / 25.4
    free_space = page_h - max_y - y - safety

    writer = PdfWriter()
    for p in reader.pages:
        writer.add_page(p)

    if free_space >= draw_h:
        buf = io.BytesIO()
        c = canvas.Canvas(buf, pagesize=(page_w, page_h))
        c.drawImage(ImageReader(img), x, y, width=draw_w, height=draw_h,
                    preserveAspectRatio=True, mask='auto')
        c.showPage(); c.save(); buf.seek(0)
        overlay = PdfReader(buf).pages[0]
        writer.pages[last_index].merge_page(overlay)
        _add_links(writer, last_index, (iw, ih, scale, x, y))
    else:
        footer_page, geometry = _footer_page(page_w, page_h)
        writer.add_page(footer_page)
        _add_links(writer, len(writer.pages) - 1, geometry)

    with open(output_path, 'wb') as fh:
        writer.write(fh)
