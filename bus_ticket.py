import json, base64
from pathlib import Path
import re
from html import escape
from google import genai
from google.genai import types
from weasyprint import HTML

from print_settings import apply_css_settings
from ai_retry import call_with_high_demand_retry
from performance_utils import extract_pdf_text

MYTOURBAZAR_LOGO_URL = "https://share.google/UUxbVDVNxkIgplZio"
SCHEMA={"type":"object","properties":{
 "booking_id":{"type":"string"},"booking_date":{"type":"string"},"pnr":{"type":"string"},"status":{"type":"string"},"mobile":{"type":"string"},
 "operator":{"type":"string"},"bus_number":{"type":"string"},"bus_type":{"type":"string"},
 "dep_time":{"type":"string"},"dep_city":{"type":"string"},"dep_date":{"type":"string"},"boarding_point":{"type":"string"},
 "arr_time":{"type":"string"},"arr_city":{"type":"string"},"arr_date":{"type":"string"},"drop_point":{"type":"string"},
 "duration":{"type":"string"},"passengers":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"},"title":{"type":"string"},"seat":{"type":"string"},"type":{"type":"string"},"dob":{"type":"string"},"boarding":{"type":"string"}},"required":["name","title","seat","type","dob","boarding"]}},
 "base_fare":{"type":"number"},"taxes":{"type":"number"}
},"required":["booking_id","booking_date","pnr","status","mobile","operator","bus_number","bus_type","dep_time","dep_city","dep_date","boarding_point","arr_time","arr_city","arr_date","drop_point","duration","passengers","base_fare","taxes"]}

PROMPT='''You are MyTourBazar's bus booking itinerary extraction assistant. Read ALL supplied PDFs, screenshots and text. Extract the confirmed bus booking exactly. Preserve passenger names, passenger titles, booking ID, PNR, operator, bus number, bus type, dates, times, cities, boarding/drop points, duration and seat numbers. For each passenger preserve an explicit title such as Mr., Mrs., Ms., Master or Miss. Keep that title ONLY in the title field; the name field must not repeat it. Example: `Mr. Govind Sinha` -> title `Mr.` and name `Govind Sinha`. If no title is printed, use smart contextual inference only when supported by the source itself; never guess gender from a name alone. Otherwise use Mr./Ms. for an adult, Child for a child and Infant for an infant. Preserve DOB whenever printed; never invent it. For children and infants, return DOB whenever present. Do not invent missing facts. Extract original supplier fare base_fare and taxes as INR numbers; if unavailable use 0. Return only JSON matching the schema.'''

def extract_bus_ticket(file_parts, source_text, api_key, model):
    client=genai.Client(api_key=api_key); contents=[PROMPT]
    if source_text: contents.append('\nSOURCE TEXT:\n'+source_text)
    paths=[]
    try:
        for item in file_parts:
            p=Path(item['path']); paths.append(p)
            if p.suffix.lower()=='.pdf':
                local=extract_pdf_text(p,60000)
                if len(local.strip())>=350:
                    contents.append('\nLOCAL SELECTABLE BUS PDF TEXT:\n'+local)
                    continue
            contents.append(types.Part.from_bytes(data=p.read_bytes(),mime_type=item['mime_type']))
        r=call_with_high_demand_retry(lambda: client.models.generate_content(model=model,contents=contents,config=types.GenerateContentConfig(response_mime_type='application/json',response_schema=SCHEMA,temperature=0)))
        if not r.text: raise RuntimeError('Gemini returned an empty bus ticket response.')
        return json.loads(r.text)
    finally:
        for p in paths:
            try:p.unlink(missing_ok=True)
            except:pass

def distribute_fare(updated_total, original_base, original_tax):
    total=float(updated_total); ob=max(float(original_base or 0),0); ot=max(float(original_tax or 0),0)
    if ob+ot>0: base=round(total*ob/(ob+ot)); tax=round(total-base)
    else: base=round(total*0.75); tax=round(total-base)
    return base,tax

def _esc(v): return escape(str(v or '—'))
def _passenger_display_name(person):
    person=person or {}
    name=str(person.get("name") or "").strip()
    title=str(person.get("title") or "").strip()
    if not title:
        ptype=str(person.get("type") or "").lower()
        title="Child" if ptype.startswith("child") else "Infant" if ptype.startswith("infant") else "Mr./Ms."
    title_re=re.compile(r"^((?:Mr|Mrs|Ms|Miss|Master|Mstr|Dr|Prof|Child|Infant)\.?)(?:\s+)", re.I)
    m=title_re.match(name)
    if m:
        shown=m.group(1)
        rest=name[m.end():].strip()
        while title_re.match(rest):
            mm=title_re.match(rest)
            rest=rest[mm.end():].strip()
        return f"{shown} {rest}".strip()
    return f"{title} {name}".strip()

def _logo_uri(path):
    if not path or not Path(path).exists(): return ''
    return 'data:image/png;base64,'+base64.b64encode(Path(path).read_bytes()).decode()

def generate_bus_ticket(data, updated_total, output_path, logo_path=None, page_size="A4", text_scale_override=None, logo_scale_override=None):
    fare_available=updated_total is not None and float(updated_total)>0
    compact = False
    roomy = False
    if fare_available:
        base,tax=distribute_fare(updated_total,data.get('base_fare',0),data.get('taxes',0))
    else:
        base=tax=None
    logo=_logo_uri(logo_path); logo_html=f'<a href="{MYTOURBAZAR_LOGO_URL}"><img src="{logo}" class="logo"></a>' if logo else ''
    fare_html = (f'<div class="fare"><table><tr><td style="width:33%"><strong>Base Fare:</strong> INR {base:,}</td><td style="width:34%;text-align:center"><strong>Taxes &amp; Fees:</strong> INR {tax:,}</td><td style="width:33%;text-align:right"><strong>Total Fare:</strong> <span style="color:#e65100;font-size:10.5pt">INR {int(updated_total):,}</span></td></tr></table></div>' if fare_available else '')
    pax_rows=''.join(f'''<tr><td><strong>{_esc(_passenger_display_name(p))}</strong></td><td>{_esc(p.get("seat"))}</td><td>{_esc(p.get("type"))}</td><td>{_esc(p.get("dob") if p.get("dob") else "—")}</td><td>{_esc(p.get("boarding"))}</td></tr>''' for p in data.get('passengers',[]))
    # Keep the standard design size. Long bus tickets naturally paginate instead
    # of shrinking the typography and layout.
    density_class = 'normal'
    html=f'''<!doctype html><html><head><meta charset="utf-8"><style>
@page {{size:{page_size};margin:12mm 12mm 8mm 12mm}}*{{box-sizing:border-box}}body{{font-family:Calibri,Arial,sans-serif;margin:0;padding:0;color:#1a252f;font-size:10pt;line-height:1.42;min-height:0;position:relative;display:block}}.header{{width:100%;border-bottom:3px solid #f39a21;padding-bottom:6px;margin-bottom:10px}}.header td{{vertical-align:middle}}.logo{{width:1.35in;height:1.25in;object-fit:contain;display:block}}h1{{margin:0;color:#002b49;font-size:16pt;text-align:right;letter-spacing:.5px}}.sub{{text-align:right;color:#f39a21;font-weight:700;font-size:8.5pt}}.box{{background:#f0f5fa;border:1.5px solid #a0b8cd;border-radius:6px;padding:8px 10px;margin-bottom:12px}}table{{width:100%;border-collapse:collapse}}.ref td{{padding:3px 6px;font-size:9pt}}.label{{font-weight:700;width:18%}}.value{{color:#002b49;font-weight:800;width:32%}}.title{{font-size:9.5pt;font-weight:800;color:#fff;background:#002b49;padding:5px 10px;border-radius:4px 4px 0 0;text-transform:uppercase}}.data{{border:1.5px solid #a0b8cd}}.data th{{background:#e2eef8;color:#002b49;font-size:8.5pt;padding:6px 10px;text-align:left}}.data td{{padding:6px 10px;border-bottom:1px solid #d8e3ed;font-size:9pt;vertical-align:middle}}.time{{font-size:10.5pt;font-weight:800;color:#002b49}}.small{{font-size:8pt;color:#444}}.tiny{{font-size:7.5pt;color:#f39a21;font-weight:700}}.status{{background:#2e7d32;color:#fff;padding:2px 6px;border-radius:4px;font-size:8pt;font-weight:800}}.fare{{background:#f0f5fa;border:1.5px solid #a0b8cd;border-radius:6px;padding:10px 10px;margin-top:14px;margin-bottom:12px}}.terms{{border:1.5px solid #a0b8cd;border-radius:6px;padding:8px 10px;background:#fbfdfe}}.terms-title{{font-weight:800;color:#002b49;border-bottom:1.5px solid #c8d6e5;padding-bottom:2px;margin-bottom:4px}}li{{margin-bottom:2px;font-size:8pt}}.box,.fare,.terms,.data tr{{break-inside:avoid;page-break-inside:avoid}}.title{{break-after:avoid;page-break-after:avoid}}.normal{{font-size:10pt;padding:0}}.roomy{{font-size:10.8pt;padding:0}}.roomy .box{{padding:10px 12px;margin-bottom:14px}}.roomy .title{{font-size:10.5pt;padding:7px 11px}}.roomy .data th{{font-size:9.7pt;padding:8px 11px}}.roomy .data td{{font-size:10.2pt;padding:8px 11px}}.roomy .time{{font-size:12pt}}.roomy .small{{font-size:9pt}}.roomy .tiny{{font-size:8.5pt}}.roomy .fare{{margin-top:18px;margin-bottom:14px;padding:11px}}.compact{{font-size:9.2pt;padding:0}}.compact .box{{padding:7px 9px;margin-bottom:8px}}.compact .title{{font-size:9.2pt;padding:5px 9px}}.compact .data th{{font-size:8.5pt;padding:5px 7px}}.compact .data td{{font-size:8.8pt;padding:5px 7px}}.compact .time{{font-size:10pt}}.compact .small{{font-size:7.8pt}}.compact .tiny{{font-size:7.3pt}}.compact .fare{{margin-top:9px;margin-bottom:8px;padding:7px}}.compact .terms{{padding:6px 8px}}.compact li{{font-size:7.8pt;margin-bottom:1px}}
</style></head><body class="{density_class}"><table class="header"><tr><td style="width:45%">{logo_html}</td><td style="width:55%"><h1>BUS TICKET ITINERARY</h1><div class="sub">Bus Booking Confirmation</div></td></tr></table>
<div class="box"><table class="ref"><tr><td class="label">Booking ID:</td><td class="value">{_esc(data.get('booking_id'))}</td><td class="label">Booking Date:</td><td class="value">{_esc(data.get('booking_date'))}</td></tr><tr><td class="label">Bus PNR:</td><td class="value">{_esc(data.get('pnr'))}</td><td class="label">Status:</td><td class="value"><span class="status">{_esc(data.get('status') or 'CONFIRMED')}</span></td></tr><tr><td class="label">Mobile No:</td><td class="value">{_esc(data.get('mobile'))}</td><td class="label">Bus Operator:</td><td class="value">{_esc(data.get('operator'))}</td></tr></table></div>
<div class="title">Bus Details &amp; Schedule</div><table class="data"><thead><tr><th>Bus &amp; Type</th><th>Departure / Boarding</th><th>Arrival / Drop</th><th>Duration</th></tr></thead><tbody><tr><td><strong>{_esc(data.get('operator'))}</strong><br><span class="small">{_esc(data.get('bus_number'))} · {_esc(data.get('bus_type'))}</span></td><td><span class="time">{_esc(data.get('dep_time'))}</span><br><strong>{_esc(data.get('dep_city'))}</strong><br><span class="small">{_esc(data.get('dep_date'))}</span><br><span class="tiny">BOARDING: {_esc(data.get('boarding_point'))}</span></td><td><span class="time">{_esc(data.get('arr_time'))}</span><br><strong>{_esc(data.get('arr_city'))}</strong><br><span class="small">{_esc(data.get('arr_date'))}</span><br><span class="tiny">DROP: {_esc(data.get('drop_point'))}</span></td><td><strong>{_esc(data.get('duration'))}</strong></td></tr></tbody></table><div style="margin:6px 0 12px;font-size:9pt"><strong>Route:</strong> {_esc(data.get('dep_city'))} → {_esc(data.get('arr_city'))}</div>
<div class="title">Passenger Detail &amp; Seat Information</div><table class="data"><thead><tr><th>Passenger Name</th><th>Seat</th><th>Type</th><th>DOB</th><th>Boarding</th></tr></thead><tbody>{pax_rows or '<tr><td colspan="5">—</td></tr>'}</tbody></table>
{fare_html}
<div class="terms"><div class="terms-title">General Instructions &amp; Information</div><ul><li>Please report at the boarding point at least 30 minutes before departure.</li><li>Carry a valid photo ID matching the passenger details on the booking.</li><li>Boarding point and operator instructions should be checked before departure.</li><li>Seat availability and bus amenities are subject to the confirmed booking.</li><li>For assistance during travel, contact MyTourBazar support.</li></ul></div>
</body></html>'''
    html = html.replace("size:A4", f"size:{page_size}")
    html = apply_css_settings(html, kind="bus", text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    HTML(string=html).write_pdf(str(output_path)); return base,tax
