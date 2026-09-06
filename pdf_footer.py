"""Add existing footer artwork and links without copying PDF page trees."""
import io
import logging
import fitz


def add_footer(input_path, output_path, geometry, links):
    log=logging.getLogger('mytourbazar.pdf')
    log.info('PDF_STAGE footer_start')
    with fitz.open(str(input_path)) as doc:
        if not len(doc): raise ValueError('Cannot add footer to empty PDF')
        page=doc[-1]; w,h=page.rect.width,page.rect.height
        image,iw,ih,scale,x,y,dw,dh=geometry(w,h)
        try:
            with io.BytesIO() as buffer:
                image.save(buffer,format='PNG'); raw=buffer.getvalue()
        finally: image.close()
        blocks=page.get_text('blocks',flags=fitz.TEXTFLAGS_BLOCKS & ~fitz.TEXT_PRESERVE_IMAGES)
        max_y=max((float(b[3]) for b in blocks),default=h)
        if h-max_y-y-5*72/25.4 < dh:
            page=doc.new_page(width=w,height=h)
        top=h-y-dh
        page.insert_image(fitz.Rect(x,top,x+dw,top+dh),stream=raw)
        for x1,y1,x2,y2,url in links:
            page.insert_link({'kind':fitz.LINK_URI,'uri':url,
                              'from':fitz.Rect(x+x1*scale,top+y1*scale,x+x2*scale,top+y2*scale)})
        doc.save(str(output_path),deflate=True)
    log.info('PDF_STAGE footer_complete')
