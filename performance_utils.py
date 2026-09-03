from __future__ import annotations
from pathlib import Path
from datetime import datetime
import re


def extract_image_text(path, max_chars=30000):
    """Local OCR for screenshots; never calls an external AI service."""
    try:
        import pytesseract
        from PIL import Image, ImageOps
        image=Image.open(path)
        image=ImageOps.exif_transpose(image).convert('L')
        if max(image.size)>2400:
            image.thumbnail((2400,2400))
        return (pytesseract.image_to_string(image,config='--psm 6') or '')[:max_chars]
    except Exception:
        return ''


def extract_pdf_text_with_local_ocr(path,max_chars=60000,max_ocr_pages=7):
    """Use embedded text first; OCR a bounded first/last page set only if scanned."""
    text=extract_pdf_text(path,max_chars)
    if len(re.sub(r'\s+',' ',text).strip())>=120:
        return text[:max_chars]
    try:
        import fitz, pytesseract
        from PIL import Image
        doc=fitz.open(str(path)); n=len(doc)
        indices=list(range(min(4,n)))
        if n>4:
            indices += list(range(max(4,n-3),n))
        chunks=[]
        for i in list(dict.fromkeys(indices))[:max_ocr_pages]:
            pix=doc[i].get_pixmap(matrix=fitz.Matrix(1.7,1.7),alpha=False)
            image=Image.frombytes('RGB',(pix.width,pix.height),pix.samples)
            chunks.append(pytesseract.image_to_string(image,config='--psm 6') or '')
            if sum(len(x) for x in chunks)>=max_chars: break
        doc.close()
        return '\n'.join(chunks)[:max_chars]
    except Exception:
        return text[:max_chars]


def collect_local_document_text(file_parts,source_text='',max_chars=60000):
    """Collect PDF/image facts locally for Air, Bus and Hotel workflows."""
    chunks=[str(source_text or '')]
    for item in file_parts or []:
        path=Path(item.get('path') or '')
        if not path.exists(): continue
        if path.suffix.lower()=='.pdf':
            value=extract_pdf_text_with_local_ocr(path,max_chars=max_chars)
        else:
            value=extract_image_text(path,max_chars=max_chars)
        if value: chunks.append(value)
        if sum(len(x) for x in chunks)>=max_chars: break
    return '\n\n'.join(chunks)[:max_chars]

try:
    from pypdf import PdfReader
except Exception:
    PdfReader = None


def extract_pdf_text(path, max_chars=120000):
    if not PdfReader:
        return ""
    try:
        reader=PdfReader(str(path))
        chunks=[]
        for page in reader.pages:
            try:
                t=page.extract_text() or ""
            except Exception:
                t=""
            if t:
                chunks.append(t)
            if sum(len(x) for x in chunks) >= max_chars:
                break
        return "\n".join(chunks)[:max_chars]
    except Exception:
        return ""


def pdf_is_text_rich(path, threshold=450):
    return len(re.sub(r"\s+", " ", extract_pdf_text(path, 5000)).strip()) >= threshold


def prepare_supplier_for_ai(file_paths, source_text="", max_chars=120000, preserve_pdf_layout=True):
    """Prepare supplier sources without throwing away PDF layout.

    Selectable text is still extracted locally because it is fast and searchable, but
    Tour quotations frequently encode hotels, dates, costs and day plans in tables.
    Plain text alone loses the cell/row relationships.  Keep the original PDF attached
    by default so the extraction model can verify the locally prepared text visually.
    """
    text=str(source_text or "")
    parts=[]
    local_pdf_count=0
    visual_count=0
    for f in file_paths or []:
        p=Path(f)
        if p.suffix.lower()=='.pdf':
            t=extract_pdf_text(p,max_chars=max_chars)
            if len(re.sub(r"\s+"," ",t).strip()) >= 450:
                local_pdf_count += 1
                text += f"\n\nLOCAL SELECTABLE PDF TEXT ({p.name}):\n{t}"
                if preserve_pdf_layout:
                    parts.append({"path":str(p),"mime_type":"application/pdf"})
            else:
                visual_count += 1
                parts.append({"path":str(p),"mime_type":"application/pdf"})
        else:
            visual_count += 1
            parts.append({"path":str(p),"mime_type":"image/jpeg"})
    return parts, text, {"local_pdfs":local_pdf_count,"visual_sources":visual_count}


def _norm_terminal(v):
    v=str(v or '').strip()
    if not v: return ''
    m=re.search(r"(?:terminal|term\.?|t)\s*[-:]?\s*([0-9A-Z]+)",v,re.I)
    if m: return 'Terminal '+m.group(1).upper()
    return v


def _time_candidates(text):
    out=[]
    for m in re.finditer(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", text):
        t=f"{int(m.group(1)):02d}:{m.group(2)}"
        if t not in out: out.append(t)
    return out


def _date_candidate(text):
    pats=[
        r"\b\d{1,2}/[A-Za-z]{3}/\d{4}\b",
        r"\b\d{1,2}[-/]\d{1,2}[-/]\d{2,4}\b",
        r"\b\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4}\b",
    ]
    for p in pats:
        m=re.search(p,text)
        if m:return m.group(0)
    return ''


def _flight_matches(text):
    # Airline IATA/ICAO-like code + flight digits; reject common date/time fragments.
    out=[]
    for m in re.finditer(r"(?<![A-Z0-9])([A-Z][A-Z0-9]|[A-Z0-9][A-Z])\s*[- ]?\s*(\d{2,4})(?!\d)", text.upper()):
        code,num=m.group(1),m.group(2)
        if code in {'AM','PM','IN','RS','NO','ID'}: continue
        val=f"{code} {num}"
        if val not in [x[0] for x in out]: out.append((val,m.start()))
    return out


def _iata_with_airports(text):
    # Strong pattern CODE(full airport text) from many supplier PDFs.
    hits=[]
    for m in re.finditer(r"\b([A-Z]{3})\s*\(([^\)]{3,120})\)", text.upper(), re.S):
        code=m.group(1); desc=' '.join(m.group(2).split())
        hits.append((code,desc,m.start(),m.end()))
    if len(hits)>=2:return hits
    # fallback standalone IATA codes near airport/city context
    seen=[]
    for m in re.finditer(r"\b([A-Z]{3})\b",text.upper()):
        code=m.group(1)
        if code in {'PNR','GST','INR','DOB','PDF','USA','THE','AND','FOR','AIR'}:continue
        if code not in [x[0] for x in seen]:seen.append((code,'',m.start(),m.end()))
    return seen


def _line_value(window, label):
    m=re.search(rf"\b{label}\b\s*[:\-]?\s*([^\n\r]{{1,80}})",window,re.I)
    return m.group(1).strip() if m else ''


def parse_transit_text_local(text):
    """Best-effort deterministic parser for common selectable airline-ticket text.

    Returns only sectors with a flight number and at least two route endpoints/codes.
    It intentionally does not invent missing terminals/airports.
    """
    raw=str(text or '').replace('\x00',' ')
    compact='\n'.join(line.strip() for line in raw.splitlines() if line.strip())
    matches=_flight_matches(compact)
    rows=[]
    if not matches:
        return {"transit":[],"local_confidence":0.0}

    positions=[p for _,p in matches]+[len(compact)]
    for idx,(flight_no,pos) in enumerate(matches):
        start=max(0,pos-500)
        end=min(len(compact), positions[idx+1]+900 if idx+1<len(matches) else pos+1500)
        window=compact[start:end]
        iatas=_iata_with_airports(window)
        if len(iatas)<2:
            # arrow/city route fallback
            route_m=re.search(r"\b([A-Za-z][A-Za-z .'-]{2,30})\s*(?:→|->| TO )\s*([A-Za-z][A-Za-z .'-]{2,30})\b",window,re.I)
            if not route_m: continue
            from_code=to_code=''; from_name=route_m.group(1).strip(); to_name=route_m.group(2).strip()
        else:
            from_code,from_desc=iatas[0][0],iatas[0][1]
            to_code,to_desc=iatas[1][0],iatas[1][1]
            from_name=from_desc or from_code; to_name=to_desc or to_code
        times=_time_candidates(window)
        dep=times[0] if times else ''
        arr=times[1] if len(times)>1 else ''
        terminals=re.findall(r"\bTerminal\s*[-:]?\s*([0-9A-Z]+)\b",window,re.I)
        dep_term='Terminal '+terminals[0].upper() if terminals else ''
        arr_term='Terminal '+terminals[1].upper() if len(terminals)>1 else ''
        aircraft=_line_value(window,r"Aircraft(?:\s*Type)?")
        cabin=_line_value(window,r"Cabin")
        pnr=''
        pm=re.search(r"Airline\s+PNR\s*[:\-]?\s*([A-Z0-9]{5,10})",window,re.I) or re.search(r"\bPNR\s*[:\-]?\s*([A-Z0-9]{5,10})",window,re.I)
        if pm: pnr=pm.group(1).upper()
        carrier=''
        # Look immediately around the flight number for carrier text.
        fnre=re.escape(flight_no).replace(r'\ ',r'\s*[- ]?\s*')
        cm=re.search(rf"([A-Za-z][A-Za-z &.-]{{2,35}})\s+{fnre}",window,re.I)
        if cm: carrier=' '.join(cm.group(1).split())
        if not carrier:
            # Common airline names near the sector; keep source text only.
            for name in ('Air India Express','Air India','IndiGo','SpiceJet','Akasa Air','Emirates','Qatar Airways','Etihad Airways','flydubai','Air Arabia','Singapore Airlines','Malaysia Airlines','Thai Airways','SriLankan Airlines','British Airways','Lufthansa','KLM','Air France','Turkish Airlines','Saudia'):
                if re.search(re.escape(name),window,re.I): carrier=name; break
        row={
            'date':_date_candidate(window),'segment_mode':'Flight','journey_type':'Connection',
            'carrier':carrier,'flight_number':flight_no,
            'route':f"{from_code or from_name} → {to_code or to_name}",
            'from':from_code or from_name,'to':to_code or to_name,
            'departure':dep,'arrival':arr,
            'from_airport':from_name,'to_airport':to_name,
            'departure_terminal':dep_term,'arrival_terminal':arr_term,
            'aircraft':aircraft,'pnr':pnr,
        }
        if cabin: row['cabin']=cabin
        rows.append(row)

    # de-dupe exact sector identities
    out=[];seen=set()
    for r in rows:
        key=(r.get('date',''),r.get('flight_number',''),r.get('from',''),r.get('to',''),r.get('departure',''))
        if key in seen:continue
        seen.add(key);out.append(r)
    confidence=0.0
    if out:
        fields=0; possible=len(out)*5
        for r in out:
            fields += bool(r.get('flight_number'))+bool(r.get('from'))+bool(r.get('to'))+bool(r.get('departure'))+bool(r.get('arrival'))
        confidence=fields/possible if possible else 0.0
    return {'transit':out,'local_confidence':confidence}


def parse_transit_files_local(paths, source_text=''):
    text=str(source_text or '')
    for p in paths or []:
        p=Path(p)
        if p.suffix.lower()=='.pdf':
            t=extract_pdf_text(p,60000)
            if t:text += f"\n\n--- {p.name} ---\n{t}"
    return parse_transit_text_local(text)


def apply_missing_accommodation_locally(data, reply_text, missing_fields=None):
    """Apply simple category/room-count/room-type replies without AI."""
    import copy
    d=copy.deepcopy(data or {})
    hotels=d.get('accommodation') or d.get('hotels') or []
    if not hotels:return d,False
    text=str(reply_text or '').strip()
    changed=False
    star=''
    sm=re.search(r"\b([1-5])\s*(?:star|\*)\b",text,re.I)
    if sm:star=sm.group(1)+' Star'
    rooms=''
    rm=re.search(r"\b(\d+)\s*(?:room|rooms)\b",text,re.I)
    if rm:rooms=rm.group(1)
    room_type=''
    tm=re.search(r"\b(deluxe|super\s*deluxe|premium|executive|standard|family|suite|superior|classic|club|villa|cottage)\b(?:\s+room)?",text,re.I)
    if tm:room_type=' '.join(w.capitalize() for w in tm.group(1).split())
    for h in hotels:
        if star and not str(h.get('hotel_category') or h.get('star_category') or h.get('category') or '').strip():
            h['hotel_category']=star;changed=True
        if rooms and not str(h.get('rooms') or '').strip():h['rooms']=rooms;changed=True
        if room_type and not str(h.get('room_type') or h.get('room_category') or '').strip():h['room_type']=room_type;changed=True
    if 'accommodation' in d:d['accommodation']=hotels
    elif 'hotels' in d:d['hotels']=hotels
    return d,changed
