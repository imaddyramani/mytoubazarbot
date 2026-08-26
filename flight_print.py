from pathlib import Path
from html import escape
import base64
import re
from weasyprint import HTML
from print_settings import apply_css_settings

MYTOURBAZAR_LOGO_URL = "https://share.google/UUxbVDVNxkIgplZio"
BASE_DIR = Path(__file__).resolve().parent
AIRLINE_LOGO_DIR = BASE_DIR / "assets" / "airline_logos"
TAKEOFF_CLIPART = BASE_DIR / "assets" / "flight_takeoff.png"

AIRLINE_CODE_MAP = {
    "6E": "indigo", "AI": "air_india", "IX": "air_india_express", "QP": "akasa_air",
    "SG": "spicejet", "UK": "vistara", "G8": "go_first", "I5": "air_asia",
    "EK": "emirates", "QR": "qatar_airways", "EY": "etihad_airways", "GF": "gulf_air",
    "FZ": "flydubai", "WY": "oman_air", "KU": "kuwait_airways", "G9": "air_arabia",
    "SQ": "singapore_airlines", "MH": "malaysia_airlines", "TG": "thai_airways",
    "UL": "sri_lankan_airlines", "CX": "cathay_pacific", "BA": "british_airways",
    "LH": "lufthansa", "KL": "klm", "AF": "air_france", "TK": "turkish_airlines",
    "SV": "saudia", "RJ": "royal_jordanian", "AIH": "airindia",
}


def _text(v):
    value=str(v).strip() if v is not None and str(v).strip() else ""
    return "" if value.lower() in {'-', '--', 'n/a', 'na', 'none', 'null', 'unknown', 'not specified', 'not available', 'not provided', 'not mentioned'} else value

def _esc(v):
    return escape(_text(v))

def _display_person_name(person):
    person=person or {}
    name=_text(person.get("name"))
    title=_text(person.get("title"))
    title_re=re.compile(r"^((?:Mr|Mrs|Ms|Miss|Master|Mstr|Dr|Prof|Child|Infant)\.?)(?:\s+)", re.I)
    m=title_re.match(name)
    if m:
        shown=m.group(1)
        rest=name[m.end():].strip()
        while title_re.match(rest):
            mm=title_re.match(rest)
            rest=rest[mm.end():].strip()
        return f"{shown} {rest}".strip()
    return f"{title} {name}".strip() if title else name

def _optional_paren(v):
    value=_text(v)
    return f" ({_esc(value)})" if value else ""

def _optional_line(v, cls="muted", suffix=""):
    value=_text(v)
    return f'<span class="{cls}">{_esc(value)}{suffix}</span><br>' if value else ""

def _uri(path):
    if not path or not Path(path).exists(): return ""
    return "data:image/png;base64,"+base64.b64encode(Path(path).read_bytes()).decode()


def _slug(value):
    value=_text(value).lower()
    value=value.replace("&", " and ")
    value=re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_")

def _flight_code_candidates(flight_number):
    flight_number=_text(flight_number).upper().replace(" ", "")
    if not flight_number:
        return []
    m=re.match(r"([A-Z0-9]{2,3})", flight_number)
    if not m:
        return []
    code=m.group(1)
    return [code] + ([AIRLINE_CODE_MAP[code]] if code in AIRLINE_CODE_MAP else [])

def _airline_logo_path(airline_name, flight_number=""):
    ordered=[]
    seen=set()
    name_slug=_slug(airline_name)
    for cand in [name_slug, name_slug.replace("airlines", "airways"), name_slug.replace("airways", "airlines"), *_flight_code_candidates(flight_number)]:
        cand=_slug(cand)
        if cand and cand not in seen:
            seen.add(cand)
            ordered.append(cand)
    if AIRLINE_LOGO_DIR.exists():
        for cand in ordered:
            for ext in (".png", ".jpg", ".jpeg", ".webp", ".svg"):
                file=AIRLINE_LOGO_DIR / f"{cand}{ext}"
                if file.exists():
                    return file
        for file in AIRLINE_LOGO_DIR.iterdir():
            if not file.is_file():
                continue
            stem=_slug(file.stem)
            if stem in ordered or any(stem.startswith(c) or c.startswith(stem) for c in ordered):
                return file
    if TAKEOFF_CLIPART.exists():
        return TAKEOFF_CLIPART
    return None

def _airline_logo_html(airline_name, flight_number=""):
    file=_airline_logo_path(airline_name, flight_number)
    uri=_uri(str(file)) if file else ""
    if not uri:
        return ""
    extra=" fallback" if file == TAKEOFF_CLIPART else ""
    alt=escape(_text(airline_name) or "Airline")
    return f'<div class="airline-logo-wrap"><img class="airline-logo{extra}" src="{uri}" alt="{alt} logo"></div>'

def _payment_breakdown(data, updated_total):
    """Return a professional line-by-line payment breakdown.

    Supplier labels/order are preserved. When the owner enters a new final selling
    fare, the difference/markup is distributed proportionally across every charge
    line so the displayed components add up exactly to the requested total.
    """
    rows=[]
    for item in (data.get("payment_items") or []):
        label=_text(item.get("label"))
        try: amount=float(item.get("amount") or 0)
        except Exception: amount=0
        if label and amount >= 0:
            rows.append({"label":label,"amount":amount})
    if not rows:
        try: base=float(data.get("base_fare") or 0)
        except Exception: base=0
        try: tax=float(data.get("taxes") or 0)
        except Exception: tax=0
        if base > 0: rows.append({"label":"Base Fare","amount":base})
        if tax > 0: rows.append({"label":"Taxes & Fees","amount":tax})
    source_sum=sum(x["amount"] for x in rows)
    try: supplier_total=float(data.get("gross_total") or 0)
    except Exception: supplier_total=0
    if supplier_total <= 0:
        supplier_total=source_sum
    # If the source grand total is higher than the extracted line sum, keep the
    # document arithmetically honest instead of silently losing money. This is a
    # fallback only; the extractor is instructed to recover every supplier line.
    if supplier_total > 0 and supplier_total-source_sum > 0.5:
        rows.append({"label":"Other Supplier Charges","amount":supplier_total-source_sum})
        source_sum=supplier_total
    if updated_total is None:
        return rows, supplier_total
    try: target=float(updated_total)
    except Exception: target=supplier_total
    if target < 0: target=0
    denominator=supplier_total if supplier_total > 0 else source_sum
    if rows and denominator > 0:
        factor=target/denominator
        adjusted=[]
        for row in rows:
            adjusted.append({"label":row["label"],"amount":round(row["amount"]*factor)})
        # absorb rounding in the final line so printed rows equal the exact requested total
        if adjusted:
            diff=round(target)-sum(x["amount"] for x in adjusted)
            adjusted[-1]["amount"] += diff
        return adjusted, target
    if target > 0:
        return [{"label":"Total Fare","amount":round(target)}], target
    return [], target

def generate_flight_ticket(data, updated_total, output_path, logo_path=None, page_size="A4", text_scale_override=None, logo_scale_override=None):
    segs=data.get('segments') or []
    passengers=data.get('passengers') or []
    base=tax=None
    payment_rows, payment_total = _payment_breakdown(data, updated_total)
    if updated_total is not None:
        try:
            source_base=float(data.get('base_fare',0) or 0); source_tax=float(data.get('taxes',0) or 0)
            compatibility_total=float(data.get('gross_total') or 0) or (source_base+source_tax)
            if compatibility_total>0:
                base=round(float(updated_total)*source_base/compatibility_total) if source_base>0 else 0
                tax=round(float(updated_total)-base) if source_tax>0 else 0
        except Exception:
            pass

    logo=_uri(logo_path)
    logo_html=f'<a href="{MYTOURBAZAR_LOGO_URL}"><img class="brand-logo" src="{logo}"></a>' if logo else ''
    customer_mobile=_text(data.get('mobile'))  # STRICT: no fallback to airline/PNR/other numbers.
    pax_rows=''.join(
        f'<tr><td>{i+1}</td><td><strong>{_esc(_display_person_name(p))}</strong></td>'
        f'<td>{_esc(p.get("ticket_number"))}</td>'
        f'<td>{_esc(p.get("type") or "Adult")}</td>'
        f'<td>{_esc(p.get("dob"))}</td>'
        f'<td>{_esc(p.get("baggage"))}</td></tr>' for i,p in enumerate(passengers)
    ) or '<tr><td colspan="6">No passenger details found.</td></tr>'

    rows=[]
    for i,s in enumerate(segs):
        airline=_text(s.get("flight"))
        number=_text(s.get("flight_number"))
        aircraft=_text(s.get("aircraft"))
        number_display = _esc(number) + ((" (" + _esc(aircraft) + ")") if aircraft and number else "")
        identity=_esc(airline or number)
        if airline and number_display:
            identity=f"{_esc(airline)}<br><span class='flight-id'>{number_display}</span>"
        elif number_display:
            identity=number_display
        elif aircraft and airline:
            identity=f"{_esc(airline)} <span class='muted'>({_esc(aircraft)})</span>"
        identity=identity or "Flight details"
        dep_city=_text(s.get("dep_city"))
        arr_city=_text(s.get("arr_city"))
        route=f"{_esc(dep_city)} to {_esc(arr_city)}" if dep_city and arr_city else (_esc(dep_city or arr_city) if dep_city or arr_city else "")
        dep_place=_esc(dep_city)+_optional_paren(s.get("dep_code")) if dep_city else _esc(s.get("dep_code"))
        arr_place=_esc(arr_city)+_optional_paren(s.get("arr_code")) if arr_city else _esc(s.get("arr_code"))
        dep_time=_text(s.get("dep_time"))
        arr_time=_text(s.get("arr_time"))
        dep_time_html=f'<span class="time">{_esc(dep_time)} hrs</span><br>' if dep_time else ""
        arr_time_html=f'<span class="time">{_esc(arr_time)} hrs</span><br>' if arr_time else ""
        dep_place_html=f'<strong>{dep_place}</strong><br>' if dep_place else ""
        arr_place_html=f'<strong>{arr_place}</strong><br>' if arr_place else ""
        dep_terminal=_text(s.get('dep_terminal'))
        arr_terminal=_text(s.get('arr_terminal'))
        dep_airport=_text(s.get('dep_airport'))
        arr_airport=_text(s.get('arr_airport'))
        # V173 TERMINAL ENDPOINT LOCK:
        # Never append a free-floating terminal value in the renderer. The full
        # validated airport endpoint text is authoritative. Genuine terminals are
        # already bound to the correct airport by the extractor.
        def airport_with_terminal(airport, terminal):
            airport=_text(airport)
            if airport:
                return _optional_line(airport, 'airport-full')
            return ''
        dep_airport_html=airport_with_terminal(dep_airport, dep_terminal)
        arr_airport_html=airport_with_terminal(arr_airport, arr_terminal)
        duration=_text(s.get("duration"))
        stops=_text(s.get('stops'))
        duration_bits=[]
        if duration: duration_bits.append(f'<span class="duration-main">{_esc(duration)}</span>')
        if stops: duration_bits.append(f'<span class="stops">{_esc(stops)}</span>')
        duration_html='<td class="duration">'+('<br>'.join(duration_bits) if duration_bits else '')+'</td>'
        airline_logo_html = _airline_logo_html(airline, number)
        cabin=_text(s.get('cabin'))
        cabin_html=f'<br><span class="flight-meta">Cabin: {_esc(cabin)}</span>' if cabin else ''
        rows.append(f'''<tr class="flight-row">
          <td>{airline_logo_html}<strong>{identity}</strong>{cabin_html}</td>
          <td>{dep_time_html}{dep_place_html}{_optional_line(s.get("dep_date"))}{dep_airport_html}</td>
          {duration_html}
          <td>{arr_time_html}{arr_place_html}{_optional_line(s.get("arr_date"))}{arr_airport_html}</td>
        </tr>''')
        if i < len(segs)-1:
            lay=segs[i].get('layover') or segs[i].get('layover_time') or ''
            if lay:
                rows.append(f'<tr class="layover"><td colspan="4">Layover in {_esc(s.get("arr_city"))}: {_esc(lay)}{(" | "+_esc(s.get("connection_note"))) if s.get("connection_note") else ""}</td></tr>')
    flight_rows=''.join(rows) or '<tr><td colspan="4">No confirmed flight sector found.</td></tr>'

    fare_html=''
    if updated_total is not None and payment_rows:
        detail_rows=''.join(
            f'<tr><td class="pay-label">{_esc(row.get("label"))}</td><td class="pay-amount">INR {int(round(float(row.get("amount") or 0))):,}</td></tr>'
            for row in payment_rows
        )
        fare_html=(
            '<div class="fare"><div class="fare-title">PAYMENT DETAILS</div>'
            '<table class="payment-table">'+detail_rows+
            f'<tr class="payment-total"><td>Total Fare</td><td>INR {int(round(float(payment_total or updated_total or 0))):,}</td></tr>'
            '</table></div>'
        )

    terms=data.get('general_instructions') or data.get('instructions') or [
        'All passengers including children and infants must present valid photo identity proof at check-in.',
        'For infant passengers, it is mandatory to carry the Date of Birth certificate.',
        'Flight timings are subject to change without prior notice. Please recheck with carrier prior to departure.',
        'Changes/Cancellations to booking must be made at least 6 hours prior to scheduled departure time or as per airline policy.',
        "We are not responsible for any Flight delay/Cancellation from airline's end.",
        'Please check with the respective airline for updated flight and terminal information.'
    ]
    if isinstance(terms,str): terms=[terms]
    terms_html=''.join(f'<li>{_esc(x)}</li>' for x in terms)

    booking_date=_text(data.get('booking_date'))
    booked_on_html=(
        f'<div class="booked-on">Booked on: {_esc(booking_date)}</div>'
        if booking_date else ''
    )

    html=f'''<!doctype html><html><head><meta charset="utf-8"><style>
@page{{size:{page_size};margin:12mm 10mm 8mm 10mm}}*{{box-sizing:border-box}}body{{margin:0;color:#12324b;font-family:"MTBLiberationSerifBold",Georgia,serif;font-size:10.2pt;line-height:1.28}}.top{{height:33mm;position:relative;border-bottom:2.5px solid #f4a62a;margin-bottom:5mm}}.brand{{position:absolute;left:0;top:0;width:45%;height:26mm;display:flex;align-items:center}}.brand-logo{{max-width:1.55in;max-height:1.05in;object-fit:contain}}.heading{{position:absolute;right:0;top:3mm;text-align:right;color:#073450}}.heading h1{{margin:0;font-size:21pt;letter-spacing:.3px}}.heading div{{font-size:10.5pt;margin-top:2mm}}.heading .booked-on{{font-size:8.6pt;margin-top:1.1mm;color:#5d646a;font-weight:700}}.meta{{background:#f0f5fa;border:1.5px solid #9cb6cc;border-radius:6px;padding:5mm 6mm;margin-bottom:5mm}}.meta table,.pax,.flights{{width:100%;border-collapse:collapse}}.meta td{{padding:1.4mm 1mm;font-size:10.2pt}}.meta .lab{{font-weight:700;white-space:nowrap}}.meta .val{{font-weight:800;color:#063655}}.section{{background:#063655;color:#fff;padding:2.6mm 4mm;font-size:12pt;letter-spacing:.1px;border-radius:3px 3px 0 0;margin-top:4mm}}.pax,.flights{{border:1.5px solid #9cb6cc}}.pax th,.flights th{{background:#dceaf5;color:#123b58;padding:2.8mm 3mm;text-align:left;font-size:9.7pt;border-bottom:1px solid #9cb6cc}}.pax td{{padding:2.7mm 2.5mm;border-bottom:1px solid #d3e0eb;font-size:9.4pt;color:#1f2c35;vertical-align:top}}.pax th:nth-child(1),.pax td:nth-child(1){{width:6%}}.pax th:nth-child(2),.pax td:nth-child(2){{width:31%}}.pax th:nth-child(3),.pax td:nth-child(3){{width:22%}}.pax th:nth-child(4),.pax td:nth-child(4){{width:13%}}.pax th:nth-child(5),.pax td:nth-child(5){{width:13%}}.pax th:nth-child(6),.pax td:nth-child(6){{width:15%}}.flights td{{padding:4mm 3.5mm;border-bottom:1px solid #d3e0eb;vertical-align:top;font-size:9.7pt;color:#26323a;white-space:normal;overflow:visible;word-break:normal;overflow-wrap:anywhere}}.airport-full{{display:inline;white-space:normal;overflow:visible;word-break:normal;overflow-wrap:anywhere;line-height:1.35;color:#5d646a}}.terminal-line{{display:inline-block;margin-top:.8mm;font-size:8.8pt;font-weight:800;color:#123b58;white-space:normal}}.airline-logo-wrap{{width:88px;height:36px;display:flex;align-items:center;justify-content:flex-start;margin:0 0 2.5mm 0;overflow:hidden}}.airline-logo{{display:block;max-width:88px;max-height:36px;object-fit:contain;object-position:left center}}.airline-logo.fallback{{opacity:.88;padding:2px}}.flight-id{{font-size:8.4pt;color:#5d646a}}.flight-meta{{display:inline-block;margin-top:.7mm;font-size:8.7pt;font-weight:600;color:#5d646a}}.flights .flight-row td:first-child{{width:29%}}.flights .flight-row td:nth-child(2){{width:27%}}.flights .flight-row td:nth-child(3){{width:15%;text-align:center}}.flights .flight-row td:nth-child(4){{width:29%}}.time{{font-size:13pt;font-weight:900;color:#123b58}}.muted{{font-family:Georgia,serif;color:#5d646a;font-size:8.7pt}}.duration{{font-weight:800;color:#123b58;text-align:center;vertical-align:middle!important}}.duration-main{{display:block;font-size:10.3pt;color:#123b58;line-height:1.25}}.stops{{display:block;margin-top:1.3mm;font-size:8.8pt;color:#5d646a;line-height:1.25}}.terminal{{display:inline-block;margin-top:1.2mm;color:#073450;font-size:8.8pt}}.dash{{color:#9cb6cc;letter-spacing:1px}}.layover td{{background:#edf4fa;text-align:center;padding:2.5mm;font-size:9.6pt;font-weight:800;color:#123b58}}.fare{{margin-top:4mm;background:#f7fafc;border:1.5px solid #9cb6cc;border-radius:6px;padding:0;overflow:hidden;font-size:9.6pt;break-inside:avoid;page-break-inside:avoid}}.fare-title{{background:#e5eff7;color:#123b58;font-size:10.2pt;font-weight:900;padding:2.4mm 3.5mm;border-bottom:1px solid #b8cad9;letter-spacing:.15px}}.payment-table{{width:100%;border-collapse:collapse}}.payment-table td{{padding:2.1mm 3.5mm;border-bottom:1px solid #d7e2eb;vertical-align:middle}}.payment-table .pay-label{{text-align:left;font-weight:700;color:#334b5c}}.payment-table .pay-amount{{text-align:right;font-weight:800;color:#123b58;white-space:nowrap}}.payment-table .payment-total td{{border-bottom:0;background:#eef5fa;font-size:10.6pt;font-weight:900;color:#073450;padding-top:2.7mm;padding-bottom:2.7mm}}.payment-table .payment-total td:last-child{{text-align:right;color:#e65100;white-space:nowrap}}.terms{{border:1.5px solid #9cb6cc;border-radius:6px;padding:4mm 5mm;margin-top:5mm}}.terms h3{{margin:0 0 2mm;color:#123b58;font-size:11.5pt;border-bottom:1px solid #c6d5e2;padding-bottom:2mm}}.terms ul{{margin:0;padding-left:5mm}}.terms li{{margin:1.8mm 0;font-size:9pt;color:#26323a}}.keep{{break-inside:avoid;page-break-inside:avoid}}tr{{break-inside:avoid;page-break-inside:avoid}}strong{{font-weight:800}}
</style></head><body>
<div class="top"><div class="brand">{logo_html}</div><div class="heading"><h1>E-TICKET ITINERARY</h1><div>Flight Booking Confirmation</div>{booked_on_html}</div></div>
<div class="meta"><table><tr><td class="lab">Trip ID:</td><td class="val">{_esc(data.get('booking_id') or data.get('trip_id'))}</td><td class="lab">Travel Date:</td><td class="val">{_esc(data.get('travel_date') or (segs[0].get('dep_date') if segs else ''))}</td></tr><tr><td class="lab">Airline PNR:</td><td class="val">{_esc(data.get('airline_pnr'))}</td><td class="lab">GDS PNR:</td><td class="val">{_esc(data.get('gds_pnr'))}</td></tr><tr><td class="lab">Status:</td><td class="val">{_esc(data.get('status') or 'CONFIRMED')}</td><td class="lab">Customer Mobile:</td><td class="val">{_esc(customer_mobile)}</td></tr></table></div>
<div class="section">PASSENGER INFORMATION</div><table class="pax"><thead><tr><th>#</th><th>Passenger Name</th><th>Ticket Number</th><th>Type</th><th>DOB</th><th>Baggage</th></tr></thead><tbody>{pax_rows}</tbody></table>
<div class="section">FLIGHT DETAILS &amp; SCHEDULE{(' (CONNECTING ITINERARY)' if len(segs)>1 else '')}</div><table class="flights"><thead><tr><th>Flight &amp; Aircraft</th><th>Departure</th><th>Duration &amp; Stops</th><th>Arrival</th></tr></thead><tbody>{flight_rows}</tbody></table>
{fare_html}<div class="terms keep"><h3>General Instructions</h3><ul>{terms_html}</ul></div>
</body></html>'''
    html=apply_css_settings(html, kind="flight", text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    HTML(string=html).write_pdf(str(output_path))
    return base,tax
