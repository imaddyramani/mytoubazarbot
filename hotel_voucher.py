import json
import base64
import re
from pathlib import Path
from html import escape
from google import genai
from google.genai import types
from weasyprint import HTML

from print_settings import apply_css_settings
from ai_retry import call_with_high_demand_retry
from performance_utils import extract_pdf_text

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


def extract_hotel_voucher(file_parts, source_text, api_key, model):
    client = genai.Client(api_key=api_key)
    contents = [HOTEL_VOUCHER_PROMPT]
    if source_text:
        contents.append("\nSOURCE TEXT:\n" + str(source_text)[:18000])
    opened = []
    try:
        for item in file_parts:
            path = Path(item["path"])
            opened.append(path)
            if path.suffix.lower()=='.pdf':
                local=_fast_hotel_pdf_text(path)
                if len(local.strip())>=350:
                    contents.append("\nLOCAL SELECTABLE HOTEL PDF TEXT:\n"+local)
                    continue
            contents.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=item["mime_type"]))
        response = call_with_high_demand_retry(lambda: client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=HOTEL_VOUCHER_SCHEMA,
                temperature=0,
                max_output_tokens=2800,
            ),
        ))
        if not response.text:
            raise RuntimeError("Gemini returned an empty hotel voucher response.")
        return json.loads(response.text)
    finally:
        # Source lifecycle is owned by bot.process_hotel_voucher. Keeping the file
        # alive until the structured result has been committed prevents delayed
        # fare/cost callbacks from seeing a stale missing path.
        pass


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
    query = "+".join(x for x in [data.get("hotel_name"), hotel_city] if x)
    maps_url = f"https://www.google.com/maps/search/?api=1&query={query.replace(' ', '+')}" if query else ""
    maps_html = f'<a href="{_esc(maps_url)}" class="map-link">📍 View on Google Maps</a>' if maps_url else "Not available"

    terms = data.get("terms") or []
    terms_html = "".join(f"<li>{_esc(item)}</li>" for item in terms)
    if not terms_html:
        terms_html = "<li>Please present a valid government-approved photo ID at check-in.</li>"

    reservation = data.get("reservation_id") or "—"
    nights = data.get("nights") or "—"
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
<tr><td style="font-weight:bold">Address:</td><td>{_esc(address) or '—'}</td></tr>
<tr><td style="font-weight:bold">Directions:</td><td>{maps_html}</td></tr>
</table></div>
</div>
<div class="section" style="margin-top:10px"><div class="section-title">Booking Details &amp; Itinerary Breakdown</div>
<table class="details-table"><tr><th>Category</th><th>Information</th></tr>
<tr><td><strong>Check-in</strong></td><td>{_esc(check_in)} | { _esc(nights) } Nights</td></tr>
<tr><td><strong>Check-out</strong></td><td>{_esc(check_out)}</td></tr>
<tr><td><strong>Room Type</strong></td><td>{_esc(room)}</td></tr>
<tr><td><strong>Occupancy Summary</strong></td><td>{_esc(occupancy)}</td></tr>
<tr><td><strong>Meal Plan</strong></td><td>{_esc(meal)}</td></tr>
</table></div>
{adaptive_cost_html}{fare_html}
<div class="section" style="margin-top:10px"><div class="section-title">Terms &amp; Instructions</div><ul>{terms_html}</ul></div>

</body></html>"""
    html = html.replace("size:A4", f"size:{page_size}")
    html = apply_css_settings(html, kind="hotel", text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    HTML(string=html, base_url=str(Path(output_path).parent)).write_pdf(str(output_path))
    return output_path
