from pathlib import Path
import io
from PIL import Image
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import shutil
from pypdf import PdfReader, PdfWriter

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
    if scale != 1.0:
        nw=max(1,int(im.width*scale)); nh=max(1,int(im.height*scale))
        im=im.resize((nw,nh),Image.Resampling.LANCZOS)
    return im


def add_watermark_to_pdf(input_path, output_path, enabled=True, opacity=0.04, scale=1.0):
    reader=PdfReader(str(input_path))
    if not reader.pages:
        raise ValueError("Cannot watermark an empty PDF")
    if not enabled:
        writer=PdfWriter()
        for p in reader.pages: writer.add_page(p)
        with open(output_path,'wb') as f: writer.write(f)
        return
    img=_transparent_logo(float(opacity), float(scale))
    if img is None:
        shutil.copyfile(input_path, output_path); return
    writer=PdfWriter()
    for page in reader.pages:
        w=float(page.mediabox.width); h=float(page.mediabox.height)
        # Use a comfortable central watermark size. Scale 100% means the base mark is 35% of page width.
        target_w=min(w*0.35*float(scale), w*0.60)
        ratio=img.height/img.width
        target_h=target_w*ratio
        if target_h>h*0.55:
            target_h=h*0.55; target_w=target_h/ratio
        x=(w-target_w)/2; y=(h-target_h)/2
        buf=io.BytesIO(); c=canvas.Canvas(buf,pagesize=(w,h))
        c.drawImage(ImageReader(img),x,y,width=target_w,height=target_h,preserveAspectRatio=True,mask='auto')
        c.showPage(); c.save(); buf.seek(0)
        overlay=PdfReader(buf).pages[0]
        # Watermark first, then original page over it, so data is always on top.
        overlay.merge_page(page)
        writer.add_page(overlay)
    with open(output_path,'wb') as f: writer.write(f)
