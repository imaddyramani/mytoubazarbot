from pathlib import Path
import io

from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link

BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / "assets"
SOURCE_FOOTER = (BASE_DIR / "assets" / "mytourbazar_footer.png").resolve()

def _verify_footer_source():
    """Never fall back to another project/version's footer."""
    if not SOURCE_FOOTER.is_file():
        raise FileNotFoundError(
            "Footer artwork not found. Expected exactly: "
            + str(SOURCE_FOOTER)
        )
    return SOURCE_FOOTER

# Existing MyTourBazar footer destinations.
WHATSAPP_LINK = "https://wa.me/919425259086"
CALL_9826_LINK = "tel:+919826659086"
CALL_9753_LINK = "tel:+919753359086"
EMAIL_LINK = "mailto:sales@mytourbazar.com"
WEBSITE_LINK = "https://www.mytourbazar.com"
INSTAGRAM_LINK = "https://www.instagram.com/mytourbazar?igsh=MXNtOWk4dG1hdWc3Nw%3D%3D&utm_source=qr"
GOOGLE_REVIEW_LINK = "https://rb.gy/whbhlt"

# Coordinates are in the cropped footer artwork coordinate system.
# The supplied design is 1833 x 435 after removing only its large blank border.
LINK_BOXES = [
    (25, 105, 400, 225, WHATSAPP_LINK),
    (410, 105, 760, 225, CALL_9826_LINK),
    (755, 105, 1100, 225, CALL_9753_LINK),
    (1090, 105, 1460, 225, EMAIL_LINK),
    (1440, 105, 1820, 225, WEBSITE_LINK),
    (25, 235, 440, 435, INSTAGRAM_LINK),
    (1400, 235, 1820, 435, GOOGLE_REVIEW_LINK),
]


def _prepare_footer_image():
    """Load the supplied footer and remove only its blank outer border."""
    _verify_footer_source()
    img = Image.open(SOURCE_FOOTER).convert("RGBA")
    rgb = img.convert("RGB")
    pix = rgb.load()
    w, h = rgb.size
    xs, ys = [], []
    for yy in range(h):
        for xx in range(w):
            r, g, b = pix[xx, yy]
            if min(r, g, b) < 245:
                xs.append(xx)
                ys.append(yy)
    if xs:
        pad = 4
        box = (
            max(0, min(xs) - pad), max(0, min(ys) - pad),
            min(w, max(xs) + 1 + pad), min(h, max(ys) + 1 + pad),
        )
        img = img.crop(box)
    return img


def _footer_geometry(page_w, page_h):
    img = _prepare_footer_image()
    iw, ih = img.size
    side = 10 * 72 / 25.4
    bottom = 4 * 72 / 25.4
    max_w = page_w - 2 * side
    # Keep the footer visually strong but leave comfortable whitespace around it.
    max_h = min(60 * 72 / 25.4, page_h * 0.28)
    scale = min(max_w / iw, max_h / ih)
    draw_w = iw * scale
    draw_h = ih * scale
    x = (page_w - draw_w) / 2
    y = bottom
    return img, scale, x, y, draw_w, draw_h, bottom


def _make_footer_page(page_w, page_h):
    """Create a clean same-size page containing only the footer artwork."""
    img, scale, x, y, draw_w, draw_h, bottom = _footer_geometry(page_w, page_h)
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_w, page_h))
    c.drawImage(ImageReader(img), x, y, width=draw_w, height=draw_h,
                preserveAspectRatio=True, mask="auto")
    c.showPage()
    c.save()
    buf.seek(0)
    return PdfReader(buf).pages[0], (img.size[0], img.size[1], scale, x, y)


def _add_footer_links(writer, page_index, geometry):
    iw, ih, scale, x, y = geometry
    for x1, y1, x2, y2, url in LINK_BOXES:
        rect = (
            x + x1 * scale,
            y + (ih - y2) * scale,
            x + x2 * scale,
            y + (ih - y1) * scale,
        )
        writer.add_annotation(page_index, Link(rect=rect, url=url, border=[0, 0, 0]))


def add_footer_to_pdf(input_path, output_path):
    """Add the supplied footer to the final page without hiding itinerary data.

    If the final page does not have enough free space, a clean additional page of
    the exact same paper size is appended. No existing itinerary element is
    resized, squeezed, or covered.
    """
    reader = PdfReader(str(input_path))
    if not reader.pages:
        raise ValueError("Cannot add footer to an empty PDF")

    last_index = len(reader.pages) - 1
    last = reader.pages[last_index]
    page_w = float(last.mediabox.width)
    page_h = float(last.mediabox.height)

    # Determine the lowest existing content boundary. Text blocks are preferred;
    # if extraction fails, conservatively treat the page as full.
    try:
        import fitz
        doc = fitz.open(str(input_path))
        fpage = doc[last_index]
        blocks = fpage.get_text("blocks")
        max_y = max((float(b[3]) for b in blocks if len(b) >= 4), default=page_h)
        doc.close()
    except Exception:
        max_y = page_h

    img, scale, x, y, draw_w, draw_h, bottom = _footer_geometry(page_w, page_h)
    safety = 5 * 72 / 25.4
    free_space = page_h - max_y - bottom - safety

    writer = PdfWriter()
    if free_space >= draw_h:
        # Preserve all original pages and merge artwork onto the final page.
        overlay_buf = io.BytesIO()
        c = canvas.Canvas(overlay_buf, pagesize=(page_w, page_h))
        c.drawImage(ImageReader(img), x, y, width=draw_w, height=draw_h,
                    preserveAspectRatio=True, mask="auto")
        c.showPage(); c.save(); overlay_buf.seek(0)
        overlay_page = PdfReader(overlay_buf).pages[0]
        for p in reader.pages:
            writer.add_page(p)
        writer.pages[last_index].merge_page(overlay_page)
        _add_footer_links(writer, last_index, (img.size[0], img.size[1], scale, x, y))
    else:
        for p in reader.pages:
            writer.add_page(p)
        footer_page, geometry = _make_footer_page(page_w, page_h)
        writer.add_page(footer_page)
        _add_footer_links(writer, len(writer.pages) - 1, geometry)

    with open(output_path, "wb") as f:
        writer.write(f)
