from pathlib import Path
import io
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import shutil
import logging
import fitz

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_LOGO = BASE_DIR / "data" / "logo_default.png"
USER_LOGO = BASE_DIR / "data" / "logo.png"
WATERMARK_LOGO = BASE_DIR / "assets" / "mytourbazar_watermark.png"


def _logo_path():
    # Watermark is intentionally independent from the printable logo.
    # This keeps the supplied transparent MTB watermark unchanged even when the user
    # changes the document logo later.
    if WATERMARK_LOGO.is_file():
        return WATERMARK_LOGO
    return USER_LOGO if USER_LOGO.is_file() else DEFAULT_LOGO


def _transparent_logo(opacity: float, scale: float):
    p = _logo_path()
    if not p.is_file():
        return None
    im = Image.open(p).convert("RGBA")
    # Remove near-white background so the mark sits behind page data rather than as a white rectangle.
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r,g,b,a = px[x,y]
            if r > 238 and g > 238 and b > 238:
                px[x,y] = (r,g,b,0)
            else:
                px[x,y] = (r,g,b,int(a * opacity))
    # Scale is applied to placement on the PDF, not to bitmap pixels.
    return im


def add_watermark_to_pdf(input_path, output_path, enabled=True, opacity=0.04, scale=1.0):
    if not enabled:
        shutil.copyfile(input_path, output_path)
        return
    img=_transparent_logo(float(opacity), float(scale))
    if img is None:
        shutil.copyfile(input_path, output_path); return
    logger=logging.getLogger('mytourbazar.pdf')
    logger.info('PDF_STAGE watermark_start')
    try:
        with io.BytesIO() as buffer:
            img.save(buffer,format='PNG')
            raw=buffer.getvalue()
        ratio=img.height/img.width
    finally:
        img.close()
    with fitz.open(str(input_path)) as doc:
        if not len(doc):
            raise ValueError('Cannot watermark an empty PDF')
        xref=0
        for page in doc:
            w,h=page.rect.width,page.rect.height
            target_w=min(w*0.35*float(scale),w*0.60)
            target_h=target_w*ratio
            if target_h>h*0.55:
                target_h=h*0.55; target_w=target_h/ratio
            x,y=(w-target_w)/2,(h-target_h)/2
            # Reuse a single image object; retain original pages and links.
            xref=page.insert_image(fitz.Rect(x,y,x+target_w,y+target_h),
                                   stream=raw if not xref else None,xref=xref,overlay=False)
        doc.save(str(output_path),deflate=True)
    logger.info('PDF_STAGE watermark_complete')
