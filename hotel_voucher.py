import json
import base64
import re
from datetime import datetime
from pathlib import Path
from html import escape
from pdf_render import write_pdf

from print_settings import apply_css_settings
from performance_utils import extract_pdf_text, collect_local_document_text
from hotel_location import google_maps_url, resolve_hotel_location

MYTOURBAZAR_LOGO_URL = "https://share.google/UUxbVDVNxkIgplZio"
HOTEL_VOUCHER_SCHEMA = {
    "type": "object",
    "properties": {
        "reservation_id": {"type": "string"},
        "guest_name": {"type": "string"},
        "mobile": {"type": "string"},
        "hotel_name": {"type": "string"},
        "hotel_address": {"type": "string"},
        "hotel_city": {"type": "string"},
        "check_in": {"type": "string"},
        "check_out": {"type": "string"},
        "nights": {"type": "string"},
        "room_type": {"type": "string"},
        "occupancy_summary": {"type": "string"},
        "room_count": {"type": "number"},
        "extra_bed_count": {"type": "number"},
        "meal_plan": {"type": "string"},
        "base_fare": {"type": "number"},
        "taxes": {"type": "number"},
        "terms": {"type": "array", "items": {"type": "string"}},
        "cost_components": {"type": "array", "items": {"type": "object", "properties": {"description":{"type":"string"},"quantity":{"type":"number"},"rate":{"type":"number"},"nights":{"type":"number"},"total":{"type":"number"}},"required":["description","quantity","rate","nights","total"]}},
    },
    "required": [
        "reservation_id", "guest_name", "mobile", "hotel_name", "hotel_address",
        "hotel_city", "check_in", "check_out", "nights", "room_type",
        "occupancy_summary", "room_count", "extra_bed_count", "meal_plan", "base_fare", "taxes", "terms", "cost_components"
    ]
}

HOTEL_VOUCHER_PROMPT = """
You are MyTourBazar's hotel confirmation voucher data extraction assistant.

Read all supplied hotel confirmation text, PDFs and screenshots. Extract the confirmed hotel
booking facts exactly. Do not invent booking numbers, dates, room types, guest counts, meal plans,
addresses, or charges.

Fields:
- reservation_id: booking/reservation/confirmation ID if visible, otherwise empty.
- guest_name: guest name exactly as shown.
- mobile: guest/contact mobile if visible, otherwise empty.
- hotel_name: confirmed property name.
- hotel_address: confirmed address if visible.
- hotel_city: city of the hotel.
- check_in/check_out: preserve the source's date style where possible, but make it readable.
- nights: number of nights if visible or safely derivable from check-in/out.
- room_type: confirmed room/category and number of rooms if stated.
- occupancy_summary: confirmed rooms/pax/extra-person/extra-mattress details if present.
- room_count: confirmed number of rooms. Return 0 only when not available.
- extra_bed_count: confirmed number of extra beds/extra mattresses. Return 0 when none or not available.
- meal_plan: confirmed meal plan such as CP / MAP / AP / Bed & Breakfast.
- base_fare/taxes: extract only if an actual supplier amount is explicitly printed. If unavailable, return 0.
- terms: extract the supplier's guest-facing hotel instructions/terms when present. If no terms are
  supplied, return a short safe list of common check-in instructions WITHOUT inventing hotel-specific
  fees or policies.
- cost_components: if the supplier gives a breakdown, extract each actual component such as room, breakfast, dinner, extra mattress, extra bed or supplement with quantity, rate, nights and total. If no breakdown exists, return an empty array.

Return ONLY JSON matching the supplied schema.
"""

def _fast_hotel_pdf_text(path):
    """Keep hotel confirmation/cost pages while skipping long policy sections."""
    try:
        import fitz
        doc=fitz.open(str(path)); pages=[]
        for i,page in enumerate(doc):
            text=page.get_text('text') or ''; low=text.lower(); score=0
            score += 5 if re.search(r'\b(?:reservation|confirmation|booking)\s+(?:id|number|reference|no)\b',low) else 0
            score += 5 if re.search(r'\b(?:guest\s+(?:name|details)|lead\s+guest|check[- ]?in|check[- ]?out)\b',low) else 0
            score += 4 if re.search(r'\b(?:hotel\s+(?:name|address)|room\s+(?:type|category|details)|meal\s+plan|occupancy)\b',low) else 0
            score += 3 if re.search(r'\b(?:fare|rate|tax|grand\s+total|amount\s+(?:paid|payable))\b',low) else 0
            if re.search(r'\b(?:terms\s*(?:&|and)\s*conditions|privacy\s+policy|cancellation\s+policy)\b',low) and score<5: score-=8
            pages.append((score,i,text))
        if len(pages)<=5: chosen=pages
        else: chosen=[x for x in pages if x[0]>=3][:8]
        if not chosen: chosen=pages[:3]+pages[-2:]
        doc.close()
        return '\n\n'.join(x[2] for x in chosen)[:24000]
    except Exception:
        return extract_pdf_text(path,24000)

def _hotel_local_value(text,labels,max_len=140):
    label='|'.join(labels)
    m=re.search(r'(?im)^\s*(?:'+label+r')\s*[:#\-]?\s*([^\r\n|]{1,'+str(max_len)+r'})',text)
    if not m:
        # OCR and many supplier PDFs put a label and its value on separate lines.
        line_match=re.search(r'(?im)^\s*(?:'+label+r')\s*[:#\-]?\s*$',text)
        if not line_match: return ''
        tail=text[line_match.end():]
        for candidate in tail.splitlines()[:4]:
            candidate=re.sub(r'\s+',' ',candidate).strip(' :-|')
            if not candidate: continue
            if re.fullmatch(r'(?i)(?:Guest|Mobile|Hotel|Property|Address|City|Destination|Check[\s-]*in|Check[\s-]*out|Nights?|Room|Occupancy|Meal\s*Plan)',candidate):
                continue
            return candidate[:max_len]
        return ''
    value=re.sub(r'\s+',' ',m.group(1)).strip(' :-|')
    value=re.split(
        r'(?i)\s+(?=(?:Guest|Mobile|Hotel|Property|Address|City|Destination|Check[\s-]*in|'
        r'Check[\s-]*out|Nights?|Room\s*(?:Type|Category|Count)|Occupancy|Pax|Meal\s*Plan|'
        r'Board\s*Basis|Base\s*(?:Fare|Amount)|Tax(?:es)?|GST|Grand\s*Total|Total\s*Amount)\s*:)',
        value,maxsplit=1
    )[0]
    return value.strip(' :-|')

def _hotel_local_amount(text,labels):
    value=_hotel_local_value(text,labels,80)
    m=re.search(r'(?:INR|Rs\.?|₹)?\s*([0-9][0-9,]*(?:\.\d+)?)',value,re.I)
    return float(m.group(1).replace(',','')) if m else 0.0

_DATE_TOKEN=(r'(?:[0-3]?\d[\s./-]+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
             r'Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?|\d{1,2})[\s,./-]+\d{2,4}|'
             r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|'
             r'Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+[0-3]?\d,?\s+\d{4})')

def _hotel_label_date(text,label):
    m=re.search(r'(?is)\b(?:'+label+r')\b.{0,80}?('+_DATE_TOKEN+r')',str(text or ''))
    return re.sub(r'\s+',' ',m.group(1)).strip() if m else ''

def _hotel_terms(text):
    lines=[re.sub(r'\s+',' ',x).strip(' •-*\t') for x in str(text or '').splitlines()]
    out=[]; active=False
    for line in lines:
        if re.search(r'(?i)^(?:hotel\s+)?(?:terms|important\s+information|instructions|polic(?:y|ies))\b',line):
            active=True; continue
        if active and re.match(r'^[A-Z][A-Z &/]{4,}$',line):
            break
        if active and len(line)>=12 and line not in out:
            out.append(line)
            if len(out)>=8: break
    return out

def _hotel_date(value):
    raw=re.sub(r'(?i)(\d)(st|nd|rd|th)\b',r'\1',str(value or ''))
    raw=re.sub(r'[(),|]',' ',raw); raw=re.sub(r'\s+',' ',raw).strip()
    patterns=(r'\d{1,2}[\s./-]+[A-Za-z]{3,9}[\s,./-]+\d{2,4}',r'\d{4}-\d{1,2}-\d{1,2}',r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}')
    values=[raw]
    for pattern in patterns:
        match=re.search(pattern,raw,re.I)
        if match: values.insert(0,match.group(0))
    for item in values:
        clean=re.sub(r'\s+',' ',item.replace(',',' ')).strip()
        for fmt in ('%d %b %Y','%d %B %Y','%d-%b-%Y','%d-%B-%Y','%d-%b-%y','%d-%B-%y','%Y-%m-%d','%d-%m-%Y','%d/%m/%Y','%d/%m/%y'):
            try: return datetime.strptime(clean,fmt)
            except ValueError: pass
    return None

def _derive_nights(check_in,check_out,current=''):
    match=re.search(r'\d+',str(current or ''))
    if match and int(match.group())>0: return str(int(match.group()))
    start,end=_hotel_date(check_in),_hotel_date(check_out)
    if start and end and (end-start).days>0: return str((end-start).days)
    return ''

def _infer_hotel_name(text,current=''):
    if str(current or '').strip(): return str(current).strip()
    blocked=re.compile(r'(?i)\b(?:voucher|confirmation|reservation|booking|invoice|guest|room\s+type|hotel\s+information)\b')
    property_word=re.compile(r'(?i)\b(?:hotel|resort|residency|palace|inn|suites?|retreat|lodge|hostel|villa)\b')
    candidates=[]
    for line in str(text or '').splitlines():
        line=re.sub(r'\s+',' ',line).strip(' :-|')
        if not 3<=len(line)<=100 or blocked.search(line) or not property_word.search(line): continue
        score=3 + (2 if len(line.split())<=8 else 0) + (1 if line[:1].isupper() else 0)
        candidates.append((score,line))
    return max(candidates,default=(0,''))[1]

def _extract_hotel_local(text):
    raw=str(text or '')
    rooms=_hotel_local_value(raw,[r'(?:No\.?\s*of\s*)?Rooms?',r'Room\s*Count'],20)
    extra=_hotel_local_value(raw,[r'Extra\s*(?:Bed|Mattress)(?:\s*Count)?'],20)
    try: room_count=int(re.search(r'\d+',rooms).group()) if re.search(r'\d+',rooms) else 0
    except Exception: room_count=0
    try: extra_count=int(re.search(r'\d+',extra).group()) if re.search(r'\d+',extra) else 0
    except Exception: extra_count=0
    base=_hotel_local_amount(raw,[r'Base\s*(?:Fare|Amount)',r'Room\s*(?:Fare|Charges?|Total)'])
    taxes=_hotel_local_amount(raw,[r'Tax(?:es)?',r'GST'])
    total=_hotel_local_amount(raw,[r'Grand\s*Total',r'Total\s*Amount',r'Amount\s*Payable'])
    if not base and total: base=max(0,total-taxes)
    check_in=_hotel_local_value(raw,[r'Check[\s-]*in(?:\s*Date)?',r'Arrival\s*Date'])
    check_out=_hotel_local_value(raw,[r'Check[\s-]*out(?:\s*Date)?',r'Departure\s*Date'])
    if not re.search(r'\d',check_in) or re.search(r'(?i)check[\s-]*out',check_in):
        check_in=_hotel_label_date(raw,r'Check[\s-]*in(?:\s*Date)?|Arrival\s*Date')
    if not re.search(r'\d',check_out) or re.search(r'(?i)check[\s-]*in',check_out):
        check_out=_hotel_label_date(raw,r'Check[\s-]*out(?:\s*Date)?|Departure\s*Date')
    pair=re.search(r'(?im)^.*\bcheck[\s-]*in\b.*\bcheck[\s-]*out\b.*\n([^\n]+)',raw)
    if pair:
        paired_dates=re.findall(_DATE_TOKEN,pair.group(1),re.I)
        if len(paired_dates)>=2:
            check_in=re.sub(r'\s+',' ',paired_dates[0]).strip()
            check_out=re.sub(r'\s+',' ',paired_dates[1]).strip()
    room_type=_hotel_local_value(raw,[r'Room\s*(?:Type|Category)',r'Accommodation'])
    if not room_type:
        m=re.search(r'(?im)^\s*((?:Deluxe|Superior|Standard|Executive|Premium|Suite|Family|Double|Twin)[^\n]{0,70}\bRoom\b[^\n]{0,30})$',raw)
        if m: room_type=re.sub(r'\s+',' ',m.group(1)).strip()
    occupancy=_hotel_local_value(raw,[r'Occupancy(?:\s*Summary)?',r'Pax(?=\s*:)'])
    if not occupancy:
        m=re.search(r'(?i)\b\d+\s*Adults?\b(?:\s*[,;+&]\s*\d+\s*(?:Children|Child|Infants?))?',raw)
        if m: occupancy=m.group(0)
    meal=_hotel_local_value(raw,[r'Meal\s*Plan',r'Board\s*Basis',r'Meals?'])
    if not meal:
        m=re.search(r'(?i)\b(?:CPAI?|MAPAI?|APAI?|EP|Room\s*Only|Bed\s*(?:&|and)\s*Breakfast|Breakfast\s+Included|Half\s*Board|Full\s*Board)\b',raw)
        if m: meal=m.group(0)
    nights=_derive_nights(check_in,check_out,_hotel_local_value(raw,[r'(?:No\.?\s*of\s*)?Nights?'],20))
    if room_count<=0:
        match=re.search(r'(?i)\b(\d+)\s*Rooms?\b',' '.join((room_type,occupancy)))
        room_count=int(match.group(1)) if match else 0
    if extra_count<=0:
        match=re.search(r'(?i)\b(\d+)\s*(?:Extra\s*(?:Beds?|Mattresses?)|EB)\b',' '.join((room_type,occupancy,raw)))
        extra_count=int(match.group(1)) if match else 0
    hotel_name=_infer_hotel_name(raw,_hotel_local_value(raw,[r'Hotel\s*Name',r'Hotel(?=\s*(?::|$))',r'Property\s*Name',r'Property(?=\s*(?::|$))']))
    hotel_address=_hotel_local_value(raw,[r'Hotel\s*Address',r'Property\s*Address',r'Address'])
    hotel_city=_hotel_local_value(raw,[r'Hotel\s*City',r'City',r'Destination',r'Location'])
    costs=[]
    if base>0: costs.append({'description':'Room Charges','quantity':1,'rate':base,'nights':1,'total':base})
    if taxes>0: costs.append({'description':'Taxes and Fees','quantity':1,'rate':taxes,'nights':1,'total':taxes})
    return {
        'reservation_id':_hotel_local_value(raw,[r'(?:Reservation|Confirmation|Booking)\s*(?:ID|Number|No\.?|Reference)']),
        'guest_name':_hotel_local_value(raw,[r'(?:Lead\s*)?Guest\s*Name',r'Guest(?=\s*:)',r'Booked\s*For']),
        'mobile':_hotel_local_value(raw,[r'(?:Guest|Customer|Contact)\s*(?:Mobile|Phone)',r'Mobile\s*(?:No\.?|Number)?']),
        'hotel_name':hotel_name,
        'hotel_address':hotel_address,
        'hotel_city':hotel_city,
        'check_in':check_in,
        'check_out':check_out,
        'nights':nights,
        'room_type':room_type,
        'occupancy_summary':occupancy,
        'room_count':room_count,'extra_bed_count':extra_count,
        'meal_plan':meal,
        'base_fare':base,'taxes':taxes,'terms':_hotel_terms(raw),'cost_components':costs,
    }


def extract_hotel_voucher(file_parts, source_text, api_key, model):
    text=collect_local_document_text(file_parts,source_text,max_chars=45000)
    data=_extract_hotel_local(text)
    if data.get('hotel_name') and data.get('hotel_city') and not data.get('hotel_address'):
        location=resolve_hotel_location(data['hotel_name'],data['hotel_city'])
        data['hotel_address']=location.get('address') or ''
        data['maps_url']=location.get('maps_url') or ''
    return data


def _esc(v):
    return escape(str(v or ""))


def _logo_uri(path):
    if not path or not Path(path).exists():
        return ""
    encoded = base64.b64encode(Path(path).read_bytes()).decode("utf-8")
    return "data:image/png;base64," + encoded


def generate_hotel_voucher(data, output_path, logo_path=None, fare=None, page_size="A4", text_scale_override=None, logo_scale_override=None):
    logo = _logo_uri(logo_path)
    logo_html = f'<a href="{MYTOURBAZAR_LOGO_URL}"><img src="{logo}" class="logo" alt="MyTourBazar Logo"></a>' if logo else ""
    address = data.get("hotel_address") or ""
    hotel_city = data.get("hotel_city") or ""
    if data.get('hotel_name') and hotel_city and not address:
        location=resolve_hotel_location(data.get('hotel_name'),hotel_city)
        address=location.get('address') or ''
        data['hotel_address']=address
        data['maps_url']=location.get('maps_url') or ''
    # If the free address directory cannot resolve the property, never leave the
    # guest with a blank location: show the searchable hotel/city combination.
    display_address=address or ', '.join(x for x in (str(data.get('hotel_name') or '').strip(),hotel_city) if x)
    maps_url = data.get('maps_url') or google_maps_url(data.get('hotel_name'),hotel_city,address)
    maps_html = f'<a href="{_esc(maps_url)}" class="map-link">📍 View on Google Maps</a>' if maps_url else "Not available"

    terms = data.get("terms") or []
    terms_html = "".join(f"<li>{_esc(item)}</li>" for item in terms)
    if not terms_html:
        terms_html = "<li>Please present a valid government-approved photo ID at check-in.</li>"

    reservation = data.get("reservation_id") or "—"
    nights = _derive_nights(data.get('check_in'),data.get('check_out'),data.get('nights')) or "—"
    data['nights']=nights
    components=data.get("cost_components") or []
    cost_rows=[]
    for comp in components:
        try: qty=float(comp.get("quantity") or 0); rate=float(comp.get("rate") or 0); nights_c=float(comp.get("nights") or 1); total_c=float(comp.get("total") or (qty*rate*nights_c))
        except Exception: continue
        cost_rows.append(f'<tr><td>{_esc(comp.get("description"))}</td><td>{qty:g}</td><td>INR {rate:,.0f}</td><td>{nights_c:g}</td><td style="text-align:right"><b>INR {total_c:,.0f}</b></td></tr>')
    adaptive_cost_html=''
    if cost_rows:
        grand_total = 0.0
        for comp in components:
            try:
                qty=float(comp.get("quantity") or 0); rate=float(comp.get("rate") or 0); nights_c=float(comp.get("nights") or 1)
                total_c=float(comp.get("total") or (qty*rate*nights_c))
                grand_total += total_c
            except Exception:
                continue
        adaptive_cost_html=(
            '<div class="fare"><div style="font-weight:bold;margin-bottom:6px;color:#2c3e50">HOTEL COST</div>'
            '<table style="width:100%;border-collapse:collapse"><tr><th style="text-align:left">COMPONENT</th><th>QTY</th><th>RATE</th><th>NIGHTS</th><th style="text-align:right">TOTAL</th></tr>'
            + ''.join(cost_rows) +
            f'<tr><td colspan="4" style="text-align:right;font-weight:bold;padding-top:9px;border-top:2px solid #a0b8cd">GRAND TOTAL</td><td style="text-align:right;font-weight:bold;font-size:11pt;color:#e65100;padding-top:9px;border-top:2px solid #a0b8cd">INR {grand_total:,.0f}</td></tr>'
            '</table></div>'
        )
    else:
        adaptive_cost_html=''

    hotel_cost=data.get('customer_hotel_cost') or {}
    if hotel_cost:
        # Customer Hotel cost is a transparent per-night room/EB calculation.
        adaptive_cost_html=''
        def _money(v):
            try: return f"INR {float(v):,.0f}"
            except Exception: return ''
        room_rate=hotel_cost.get('room_rate_per_night',hotel_cost.get('per_room'))
        room_count=int(float(hotel_cost.get('rooms') or data.get('room_count') or 1))
        night_count=int(float(hotel_cost.get('nights') or 0))
        eb_rate=hotel_cost.get('eb_rate_per_night',hotel_cost.get('eb'))
        eb_count=int(float(hotel_cost.get('extra_beds') or data.get('extra_bed_count') or 0))
        room_total=hotel_cost.get('room_total')
        eb_total=hotel_cost.get('eb_total')
        total=hotel_cost.get('total')

        cost_rows=[]
        if room_rate is not None:
            cost_rows.append(
                '<tr><td><strong>Room Rate / Night</strong></td>'
                f'<td>{_money(room_rate)}</td><td>{room_count}</td><td>{night_count}</td>'
                f'<td style="text-align:right"><b>{_money(room_total)}</b></td></tr>'
            )
        if eb_rate is not None and float(eb_rate or 0)>0:
            cost_rows.append(
                '<tr><td><strong>Extra Bed Rate / Night</strong></td>'
                f'<td>{_money(eb_rate)}</td><td>{eb_count}</td><td>{night_count}</td>'
                f'<td style="text-align:right"><b>{_money(eb_total)}</b></td></tr>'
            )
        if cost_rows:
            fare_html=(
                '<div class="fare"><div style="font-weight:bold;margin-bottom:6px;color:#2c3e50">HOTEL COST</div>'
                '<table class="hotel-cost-table"><tr><th style="text-align:left">COST TYPE</th><th>RATE / NIGHT</th>'
                '<th>QTY</th><th>NIGHTS</th><th style="text-align:right">SUBTOTAL</th></tr>'
                + ''.join(cost_rows) +
                f'<tr class="hotel-total-row"><td colspan="4" style="text-align:right"><strong>TOTAL HOTEL COST</strong></td>'
                f'<td class="total" style="text-align:right"><strong>{_money(total)}</strong></td></tr></table></div>'
            )
        else:
            fare_html=(
                '<div class="fare"><table class="hotel-cost-table"><tr class="hotel-total-row">'
                '<td><strong>TOTAL HOTEL COST</strong></td>'
                f'<td class="total" style="text-align:right"><strong>{_money(total)}</strong></td></tr></table></div>'
            )
    else:
        # Supplier room calculations already include their own GRAND TOTAL. Do not
        # append a second generic Total Fare element to Hotel vouchers.
        fare_html=''
    check_in = data.get("check_in") or "—"
    check_out = data.get("check_out") or "—"
    room = data.get("room_type") or "—"
    occupancy = data.get("occupancy_summary") or "—"
    meal = data.get("meal_plan") or "—"
    try: extra_beds=int(float(data.get('extra_bed_count') or 0))
    except Exception: extra_beds=0
    extra_bed_row=(f'<tr><td><strong>Extra Bed</strong></td><td>{extra_beds}</td></tr>' if extra_beds>0 else '')

    content_score = (len(str(data.get('hotel_address') or '')) + len(str(data.get('occupancy_summary') or '')) + len(str(data.get('hotel_name') or '')) + len(terms) * 55)
    if content_score <= 280:
        density_class = 'roomy'
    elif content_score <= 520:
        density_class = 'normal'
    else:
        density_class = 'compact'
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><style>
@page {{ size:{page_size}; margin: 14mm 12mm 8mm 12mm; }}
body {{ font-family:Calibri, Arial, Helvetica, sans-serif; color:#2c3e50; line-height:1.4; margin:0; padding:0; box-sizing:border-box; min-height:277mm; display:flex; flex-direction:column; }}
.header {{ border-bottom:2px solid #2c3e50; padding-bottom:8px; display:flex; justify-content:space-between; align-items:center; }}
.logo {{ width:1.35in; height:1.25in; object-fit:contain; display:block; }}
.reservation-id-wrapper {{ text-align:right; }}
.reservation-id-label {{ font-size:12px; color:#555; }}
.reservation-id {{ font-size:24px; font-weight:bold; color:#2c3e50; }}
.title {{ text-align:center; background:#2c3e50; color:white; padding:8px; font-size:16px; font-weight:bold; text-transform:uppercase; border-radius:3px; margin-top:10px; }}
.grid {{ display:flex; gap:10px; margin-top:10px; }} .grid .section {{ flex:1; }}
.section {{ padding:12px; border:1px solid #dcdde1; background:#fcfcfc; border-radius:3px; }}
.section-title {{ font-weight:bold; color:#2c3e50; border-bottom:1.5px solid #bdc3c7; margin-bottom:8px; padding-bottom:4px; font-size:13px; text-transform:uppercase; }}
.info-table,.details-table {{ width:100%; border-collapse:collapse; }}
.info-table td {{ padding:5px 6px; font-size:12px; vertical-align:top; }}
.details-table th,.details-table td {{ padding:8px; text-align:left; border-bottom:1px solid #e1e1e1; font-size:12px; }}
.details-table th {{ background:#ecf0f1; }}
ul {{ margin:4px 0; padding-left:18px; font-size:11px; }} li {{ margin-bottom:5px; }}

.normal{{font-size:11pt}}.roomy{{font-size:11.5pt}}.compact{{font-size:10pt}}
.roomy .section{{padding:13px}}.roomy .section-title{{font-size:13.5px}}.roomy .info-table td,.roomy .details-table th,.roomy .details-table td{{font-size:12.5px;padding:6px 7px}}
.normal .info-table td,.normal .details-table th,.normal .details-table td{{font-size:12px}}
.compact .section{{padding:8px}}.compact .section-title{{font-size:11.5px}}.compact .info-table td,.compact .details-table th,.compact .details-table td{{font-size:10px;padding:4px 5px}}.compact ul{{font-size:9px}}.compact li{{margin-bottom:3px}}
.fare {{ background:#f0f5fa; border:1.5px solid #a0b8cd; border-radius:6px; padding:10px; margin-top:14px; margin-bottom:12px; }} .fare table {{ width:100%; border-collapse:collapse; }} .hotel-cost-table th,.hotel-cost-table td{{padding:6px 7px;border-bottom:1px solid #d7e2eb;font-size:9.2pt;text-align:center}} .hotel-cost-table th{{background:#e5eff7;color:#123b58;font-weight:900}} .hotel-cost-table .hotel-total-row td{{border-bottom:0;background:#eef5fa;padding-top:8px;font-size:10pt}} .total {{ color:#e65100; font-size:10.5pt; }}
.map-link {{ color:#2c3e50; text-decoration:none; font-weight:bold; display:inline-block; padding:4px 8px; background:#f0f0f0; border-radius:4px; font-size:11px; }}

.mtb-contact-footer{{bottom:-10mm}}
</style></head><body class="{density_class}">
<div class="header"><div>{logo_html}</div><div class="reservation-id-wrapper"><div class="reservation-id-label">Reservation ID</div><div class="reservation-id">{_esc(reservation)}</div></div></div>
<div class="title">Hotel Confirmation Voucher</div>
<div class="grid">
<div class="section"><div class="section-title">Guest Details</div><table class="info-table">
<tr><td style="font-weight:bold;width:30%">Guest:</td><td>{_esc(data.get('guest_name'))}</td></tr>
<tr><td style="font-weight:bold">Mobile:</td><td>{_esc(data.get('mobile')) or '—'}</td></tr>
</table></div>
<div class="section"><div class="section-title">Hotel Information</div><table class="info-table">
<tr><td style="font-weight:bold;width:25%">Hotel:</td><td>{_esc(data.get('hotel_name'))}</td></tr>
<tr><td style="font-weight:bold">Address:</td><td>{_esc(display_address) or '—'}</td></tr>
<tr><td style="font-weight:bold">Directions:</td><td>{maps_html}</td></tr>
</table></div>
</div>
<div class="section" style="margin-top:10px"><div class="section-title">Booking Details &amp; Itinerary Breakdown</div>
<table class="details-table"><tr><th>Category</th><th>Information</th></tr>
<tr><td><strong>Check-in</strong></td><td>{_esc(check_in)}</td></tr>
<tr><td><strong>Total Nights</strong></td><td>{_esc(nights)}</td></tr>
<tr><td><strong>Check-out</strong></td><td>{_esc(check_out)}</td></tr>
<tr><td><strong>Room Type</strong></td><td>{_esc(room)}</td></tr>
{extra_bed_row}
<tr><td><strong>Occupancy Summary</strong></td><td>{_esc(occupancy)}</td></tr>
<tr><td><strong>Meal Plan</strong></td><td>{_esc(meal)}</td></tr>
</table></div>
{adaptive_cost_html}{fare_html}
<div class="section" style="margin-top:10px"><div class="section-title">Terms &amp; Instructions</div><ul>{terms_html}</ul></div>

</body></html>"""
    html = html.replace("size:A4", f"size:{page_size}")
    html = apply_css_settings(html, kind="hotel", text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    write_pdf(html, output_path, base_url=str(Path(output_path).parent.resolve()))
    return output_path
