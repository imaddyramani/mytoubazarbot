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
    from PIL import ImageChops
    w,h=img.size
    # Equivalent near-white crop, without two huge Python coordinate lists.
    with img.convert('RGB') as rgb:
        r,g,b=rgb.split()
        with ImageChops.darker(r,g) as rg:
            with ImageChops.darker(rg,b) as minimum:
                with minimum.point(lambda value:255 if value<245 else 0) as mask:
                    bounds=mask.getbbox()
        r.close(); g.close(); b.close()
    if bounds:
        pad = 4
        box = (
            max(0, bounds[0] - pad), max(0, bounds[1] - pad),
            min(w, bounds[2] + pad), min(h, bounds[3] + pad),
        )
        cropped=img.crop(box); img.close(); img=cropped
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
    from pdf_footer import add_footer
    def geometry(w,h):
        image,scale,x,y,dw,dh,bottom=_footer_geometry(w,h)
        return image,*image.size,scale,x,y,dw,dh
    return add_footer(input_path, output_path, geometry, LINK_BOXES)
