from pathlib import Path
import re
from html import escape
import base64
from weasyprint import HTML

MYTOURBAZAR_LOGO_URL = "https://share.google/UUxbVDVNxkIgplZio"

def esc(v):
    return escape(str(v or ""))

def logo_uri(path):
    if not path or not Path(path).exists():
        return ""
    return "data:image/png;base64," + base64.b64encode(Path(path).read_bytes()).decode()

STANDARD_POLICIES = {
    "TERMS & CONDITIONS:-": [
        "No refund will be made for any unused accommodation, missed meals, transportation segments, sightseeing tours or any other service.",
        "No refund shall be claimed, if the services & amenities of the hotel were not up to your expectations, it will be considered on a case to case basis.",
    ],
    "AMENDMENT POLICY:-": [
        "All Changes must be communicated in writing.",
        "In order to prepone and postpone the tour, kindly contact us 25 days prior to the travel date.",
        "MyTourBazar does not charge for prepone and postpone once.",
        "10% of total package cost will be charged for Postponing & prepone second time.",
        "We do not accept any changes in plan within 20 days of travel date.",
        "Any changes in the tour package will depend on subject to availability.",
        "The validity of “Postponing Packages” is 6months from the date of booking.",
    ],
    "PAYMENT POLICY:-": [
        "50 % of the total package is Compulsory to confirm the booking.",
        "Remaining 50% of the package cost should be paid before check in.",
        "Packages can be booked by taking a token amount. And next instalment must be paid as instructed by our team.",
    ],
    "CANCELLATION POLICY": [
        "Booking must be cancelled 25 days prior to the planned date of arrival.",
        "20% of total amount will be deducted for cancellation received up to 20days prior to arrival.",
        "50% will be deducted for cancellation received up to 7 days prior to arrival.",
        "Full amount will be deducted for cancellation received within 7days prior to arrival.",
        "Cancellation Service Charge: INR 1000 per person.",
        "5% GST of Received Amount will be deducted.",
    ],
}



def flight_clipart(kind="takeoff"):
    """Return a small, clean transparent PNG flight icon for the transit table."""
    filename = "flight_landing.png" if kind == "landing" else "flight_takeoff.png"
    path = Path(__file__).resolve().parent / "assets" / filename
    if not path.exists():
        return ""
    uri = "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    return f"<img class=\"flight-clipart\" src=\"{uri}\" alt=\"{kind} flight\">"

def generate_pdf(data, output_path, logo_path=None, page_size="A4", text_scale_override=None, logo_scale_override=None):
    logo = logo_uri(logo_path)

    # ---------- Transit ----------
    # Group connected public-transport sectors belonging to the same date/journey.
    # Example: IndiGo 6E-594 / 6E-273 | Raipur → Mumbai → Rajkot.
    from collections import OrderedDict
    grouped = OrderedDict()
    for x in data.get("transit", []):
        date = str(x.get("date") or "").strip()
        journey = str(x.get("journey_type") or "").strip().lower()
        key = (date, journey)
        grouped.setdefault(key, []).append(x)

    transit_rows = ""
    grouped_items = list(grouped.items())
    flight_group_indexes = [i for i, ((_, _), items) in enumerate(grouped_items) if any(
        str(x.get("segment_mode") or "").strip().lower() == "flight" or x.get("carrier") or x.get("airline")
        for x in items
    )]
    first_flight_group = flight_group_indexes[0] if flight_group_indexes else None
    last_flight_group = flight_group_indexes[-1] if flight_group_indexes else None

    for group_index, ((_, _), items) in enumerate(grouped_items):
        first = items[0]
        mode = str(first.get("segment_mode") or "").strip()
        carrier_values = []
        number_values = []
        route_parts = []

        for x in items:
            carrier = str(x.get("carrier") or x.get("airline") or "").strip()
            number = str(x.get("flight_number") or x.get("train_number") or "").strip()
            if carrier and carrier not in carrier_values:
                carrier_values.append(carrier)
            if number and number not in number_values:
                number_values.append(number)

            route = str(x.get("route") or "").strip()
            if route:
                # Normalize common separators to a clean arrow chain.
                route = route.replace("→", " → ").replace("->", " → ").replace("–>", " → ")
                parts = [part.strip() for part in route.split("→") if part.strip()]
                for part in parts:
                    if not route_parts or route_parts[-1].lower() != part.lower():
                        route_parts.append(part)
            else:
                a = str(x.get("from") or "").strip()
                b = str(x.get("to") or "").strip()
                if a and (not route_parts or route_parts[-1].lower() != a.lower()):
                    route_parts.append(a)
                if b and (not route_parts or route_parts[-1].lower() != b.lower()):
                    route_parts.append(b)

        aircraft_values=[]
        for x in items:
            aircraft=str(x.get("aircraft") or "").strip()
            if aircraft and aircraft not in aircraft_values:
                aircraft_values.append(aircraft)
        if carrier_values and number_values:
            segment = f"{carrier_values[0]} {' / '.join(number_values)}"
        elif carrier_values:
            segment = carrier_values[0]
        elif number_values:
            segment = " / ".join(number_values)
        else:
            segment = mode
        if aircraft_values:
            segment += f" ({' / '.join(aircraft_values)})"

        is_flight = mode.lower() == "flight"
        segment_icon = ""
        if is_flight:
            journey_text = " ".join(str(x.get("journey_type") or "") for x in items).lower()
            if any(k in journey_text for k in ("return", "inbound", "arrival")):
                icon_kind = "landing"
            elif any(k in journey_text for k in ("outbound", "departure", "onward")):
                icon_kind = "takeoff"
            elif group_index == last_flight_group and last_flight_group != first_flight_group:
                icon_kind = "landing"
            else:
                icon_kind = "takeoff"
            segment_icon = flight_clipart(icon_kind)
        elif mode.lower() == "train":
            segment_icon = "🚆 "

        route = " → ".join(route_parts)
        if not route:
            route = str(first.get("route") or "")

        dep, arr = first, items[-1]
        departure = esc(dep.get("departure") or "")
        arrival = esc(arr.get("arrival") or "")
        dep_airport=str(dep.get("from_airport") or "").strip()
        arr_airport=str(arr.get("to_airport") or "").strip()
        dep_terminal=str(dep.get("departure_terminal") or "").strip()
        arr_terminal=str(arr.get("arrival_terminal") or "").strip()
        if dep_airport or dep_terminal:
            dep_label=dep_airport
            if dep_terminal:
                dep_label += (" " if dep_label else "") + "(Terminal " + dep_terminal.replace("Terminal","").strip() + ")"
            departure += "<br><span class='transit-airport'>" + esc(dep_label) + "</span>"
        if arr_airport or arr_terminal:
            arr_label=arr_airport
            if arr_terminal:
                arr_label += (" " if arr_label else "") + "(Terminal " + arr_terminal.replace("Terminal","").strip() + ")"
            arrival += "<br><span class='transit-airport'>" + esc(arr_label) + "</span>"
        transit_rows += (
            f"<tr><td>{esc(first.get('date'))}</td>"
            f"<td>{segment_icon}{esc(segment)}</td>"
            f"<td>{esc(route)}</td>"
            f"<td>{departure}</td>"
            f"<td>{arrival}</td></tr>"
        )
    transit_section = ""
    if transit_rows:
        transit_section = f"""
        <div class='section'>TRANSIT &amp; CONNECTION SCHEDULE</div>
        <table class='schedule transit'><thead><tr>
        <th>DATE</th><th>SEGMENT &amp; MODE</th><th>ROUTE DETAILS</th><th>DEPARTURE</th><th>ARRIVAL</th>
        </tr></thead><tbody>{transit_rows}</tbody></table>
        <div class='transit-ticket-note'><strong>NOTE -</strong> Please check the original ticket copies for reliable information</div>"""
    elif data.get("transit_done_by_self"):
        transit_section = """
        <div class='section'>TRANSIT &amp; CONNECTION SCHEDULE</div>
        <table class='schedule transit'><tbody>
        <tr><td colspan='5' class='transit-self'>Done by Self</td></tr>
        </tbody></table>"""

    # ---------- Accommodation ----------
    hotel_rows=[]
    hotels_data=data.get("hotels", []) or []
    current_option=None

    def _star_count(text):
        """Extract an explicit hotel rating, with Premium represented as +0.5.

        Examples supplied by the owner such as ``3 star premium`` and
        ``4-star premium`` render as 3½ and 4½ visual stars. Premium never creates
        a rating by itself; an explicit star rating must be present in the same
        field/segment.
        """
        txt = str(text or "").strip()
        if not txt:
            return None

        # Keep field boundaries: callers may join several accommodation fields using |.
        # This prevents a 3-star hotel with a separate Premium room type from being
        # accidentally upgraded to 3.5 stars.
        segments=[x.strip() for x in txt.split('|') if x.strip()] or [txt]
        words = {"one":1, "two":2, "three":3, "four":4, "five":5}
        for seg in segments:
            count=None
            m = re.search(r"\b([1-5])\s*[★⭐]", seg)
            if m:
                count=int(m.group(1))
            if count is None:
                m = re.search(r"(?<![A-Za-z])([★⭐]{1,5})(?![A-Za-z])", seg)
                if m:
                    count=len(m.group(1))
            if count is None:
                m = re.search(r"\b([1-5])\s*[- ]?stars?\b", seg, re.I)
                if m:
                    count=int(m.group(1))
            if count is None:
                m = re.search(r"(?<![0-9])([1-5])\s*\*", seg)
                if m:
                    count=int(m.group(1))
            if count is None:
                m = re.search(r"\b(one|two|three|four|five)\s*[- ]?stars?\b", seg, re.I)
                if m:
                    count=words[m.group(1).lower()]
            if count is not None:
                # Premium only adds the half star when it is part of the same stated
                # hotel-rating field, e.g. "3 star premium". Cap at 5.
                premium=bool(re.search(r"\bpremium\b", seg, re.I))
                return min(5.0, float(count) + (0.5 if premium and count < 5 else 0.0))
        return None

    def _stars_html(count):
        if not count:
            return ""
        count=float(count)
        full=int(count)
        half=(count-full) >= 0.49
        stars='★' * full
        if half:
            stars += "<span class='half-star'>★</span>"
        label=(f"{full + 0.5:g}" if half else f"{full}")
        return f" <span class='gold-stars' aria-label='{label} star hotel'>{stars}</span>"

    def _strip_star_tokens(text):
        """Keep the room/category text clean; ratings belong only in headings."""
        txt = str(text or "")
        txt = re.sub(r"\b[1-5]\s*[- ]?stars?\b", "", txt, flags=re.I)
        txt = re.sub(r"\b(one|two|three|four|five)\s*[- ]?stars?\b", "", txt, flags=re.I)
        txt = re.sub(r"(?<![0-9])[1-5]\s*[★⭐*]", "", txt)
        txt = re.sub(r"[★⭐]{1,5}", "", txt)
        txt = re.sub(r"\s{2,}", " ", txt)
        return txt.strip(" -–—,/")

    # Group accommodation by genuine option. Option 1 is never printed; Option 2+ is printed only
    # when an alternate supplier accommodation/package is actually supplied. Stars are printed at the
    # option heading only, never beside the hotel name or day-by-day section.
    grouped_hotels=[]
    for x in hotels_data:
        opt=str(x.get("option") or "").strip()
        normalized='1' if opt.lower() in ('','option 1','1','primary','default') else opt.replace('Option ','').strip()
        key=normalized or '1'
        bucket=next((b for b in grouped_hotels if b[0]==key), None)
        if bucket is None:
            grouped_hotels.append((key,[x]))
        else:
            bucket[1].append(x)

    if not grouped_hotels and hotels_data:
        grouped_hotels=[('1',hotels_data)]

    option_star_counts = {}
    for opt, rows in grouped_hotels:
        # Search all accommodation-related fields because suppliers place the rating
        # in different locations (hotel name, room/category, heading, notes, etc.).
        combined = ' | '.join(
            str(r.get(k) or '')
            for r in rows
            for k in ('hotel_name', 'room_category', 'meal_plan', 'hotel_category', 'star_category', 'category', 'notes')
        )
        stars = _star_count(combined)
        option_star_counts[opt] = stars

        heading = 'ACCOMMODATION' if opt == '1' else f'OPTION {esc(opt)}'
        meal_heading = next((str(r.get('meal_plan') or '').strip() for r in rows if str(r.get('meal_plan') or '').strip()), '')
        if meal_heading:
            heading += f' ({esc(meal_heading)})'
        # Stars belong ONLY in the accommodation heading, never in the cost table
        # or day-by-day experience.
        heading += _stars_html(stars)
        # Option 1 is represented by the main ACCOMMODATION section heading.
        # Only alternate options need an in-table OPTION N divider, preventing a
        # duplicated ACCOMMODATION row when there is only one supplier option.
        if opt != '1':
            hotel_rows.append(f"<tr class='option-row'><td colspan='6'>{heading}</td></tr>")
        for x in rows:
            # V161: Room Category and Total Rooms are separate customer-facing facts.
            # Room Category contains the actual category/type (Premium, Standard, Deluxe,
            # Non AC, Executive, Suite, etc.). Total Rooms contains only the rooming setup.
            _room_parts=[]
            for _rv in (x.get('room_type'), x.get('room_category')):
                _clean=_strip_star_tokens(_rv).strip()
                # Keep the supplier category, but remove a redundant trailing word "Room".
                _clean=re.sub(r'\s+rooms?$', '', _clean, flags=re.I).strip()
                if _clean and _clean.lower() not in [p.lower() for p in _room_parts]:
                    _room_parts.append(_clean)
            clean_room_category = ' • '.join(_room_parts) or '—'

            rooms_text = str(x.get('rooms') or '').strip()
            if rooms_text:
                # Professional compact rooming display without changing the quantity.
                rooms_text = re.sub(r'(?i)\bdouble\s+(?:sharing|occupancy|rooms?)\b', 'DBL', rooms_text)
                rooms_text = re.sub(r'(?i)\bdbl\s+rooms?\b', 'DBL', rooms_text)
                rooms_text = re.sub(r'(?i)\bextra\s+(?:bed|mattress|mat)\b', 'EB', rooms_text)
                rooms_text = re.sub(r'(?i)\btriple\s+occupancy\b', 'Triple Sharing', rooms_text)
                rooms_text = re.sub(r'\s*\+\s*', ' + ', rooms_text)
                rooms_text = re.sub(r'\s{2,}', ' ', rooms_text).strip()
                # Uppercase the standard abbreviations only.
                rooms_text = re.sub(r'(?i)\bdbl\b', 'DBL', rooms_text)
                rooms_text = re.sub(r'(?i)\beb\b', 'EB', rooms_text)
            # If the package explicitly carries an Extra Bed count and the row already has
            # a room count but no EB token, append it instead of mixing it into Room Category.
            try:
                _global_eb=int(data.get('extra_bed_count') or 0)
            except Exception:
                _global_eb=0
            if rooms_text and _global_eb > 0 and not re.search(r'(?i)\bEB\b|extra\s+(?:bed|mattress|mat)', rooms_text):
                rooms_text += f' + {_global_eb} EB'
            total_rooms_display = rooms_text or '—'

            hotel_rows.append(
                f"<tr><td>{esc(x.get('dates'))}</td><td>{esc(x.get('destination'))}</td>"
                f"<td><b>{esc(x.get('hotel_name'))}</b></td><td>{esc(clean_room_category)}</td>"
                f"<td>{esc(total_rooms_display)}</td><td>{esc(x.get('meal_plan'))}</td></tr>"
            )
    hotels = "".join(hotel_rows) or "<tr><td colspan='6'>No accommodation details provided.</td></tr>"

    accommodation_heading = str(data.get("accommodation_heading") or "ACCOMMODATION SCHEDULE").upper()
    # The main accommodation header receives the primary option's rating. For
    # multiple options, each OPTION N row above also receives its own rating.
    primary_stars = option_star_counts.get('1')
    if not primary_stars:
        primary_stars = _star_count(accommodation_heading)
    # Keep the heading clean: the numeric/star rating is represented visually by
    # the golden glyphs, not duplicated as text.
    accommodation_heading = _strip_star_tokens(accommodation_heading).strip(' -–—,/:')
    stars_html = _stars_html(primary_stars)

    # Logistics Hotel Type field:
    # - one consistent star category => show real golden stars
    # - more than one category => show 'Multi Category'
    # - no supplier category => leave blank (never guess)
    detected_hotel_types = []
    for _opt, _rows in grouped_hotels:
        for _h in _rows:
            _combined = ' | '.join(str(_h.get(k) or '') for k in (
                'hotel_category', 'star_category', 'category', 'room_category', 'room_type', 'hotel_name', 'notes'
            ))
            _sc = _star_count(_combined)
            if _sc:
                detected_hotel_types.append(('star', float(_sc)))
            else:
                _explicit = str(_h.get('hotel_category') or _h.get('star_category') or _h.get('category') or '').strip()
                if _explicit:
                    detected_hotel_types.append(('text', _strip_star_tokens(_explicit).lower()))

    _unique_hotel_types = []
    for _t in detected_hotel_types:
        if _t not in _unique_hotel_types:
            _unique_hotel_types.append(_t)

    if len(_unique_hotel_types) > 1:
        hotel_type_html = "<span class='hotel-type-multi'>Multi Category</span>"
    elif len(_unique_hotel_types) == 1 and _unique_hotel_types[0][0] == 'star':
        hotel_type_html = _stars_html(_unique_hotel_types[0][1]).strip()
    elif len(_unique_hotel_types) == 1:
        hotel_type_html = esc(str(_unique_hotel_types[0][1]).title())
    else:
        hotel_type_html = ''

    # ---------- Package costing ----------
    def _num(v):
        try:
            return float(re.sub(r"[^0-9.\-]", "", str(v or "0").replace(',', '')))
        except Exception:
            return 0.0
    adult_n=int(data.get('adult_count') or 0)
    child_n=int(data.get('child_count') or 0)
    cwb_n=int(data.get('child_cwb_count') or 0)
    cnb_n=int(data.get('child_cnb_count') or 0)
    eb_n=int(data.get('extra_bed_count') or 0)
    has_split_child_cost=bool(
        cwb_n or cnb_n or any(
            _num(c.get('per_child_cwb'))>0 or _num(c.get('per_child_cnb'))>0
            for c in (data.get('package_costs') or [])
        )
    )
    cost_rows=[]
    for c in (data.get('package_costs') or []):
        pa=_num(c.get('per_adult')); pc=_num(c.get('per_child')); pcw=_num(c.get('per_child_cwb')); pcn=_num(c.get('per_child_cnb')); peb=_num(c.get('per_extra_bed'))
        generic_child_n=0 if has_split_child_cost else child_n
        supplier_calc=(pa*adult_n)+(pc*generic_child_n)+(pcw*cwb_n)+(pcn*cnb_n)+(peb*eb_n)
        supplier_raw=_num(c.get('supplier_total')) or _num(c.get('total_cost'))
        supplier_total=supplier_calc if supplier_calc>0 else supplier_raw
        markup_total=_num(c.get('markup_total'))
        final_raw=_num(c.get('final_total'))
        final_total=final_raw if final_raw>0 else (supplier_total+markup_total if supplier_total>0 else supplier_raw)
        c['calculated_total']=final_total
        c['supplier_total']=supplier_total
        c['final_total']=final_total
        def _rate_cell(value, count):
            raw=str(value or '').strip()
            numeric=_num(value)
            # Cost table keeps Adult/Child/CWB/CNB/EB fields visible, but missing
            # or zero supplier/customer data must read as --, never as a real INR 0.
            if not raw or numeric<=0:
                return '--'
            if count > 0:
                return f"{esc(value)} × {count}"
            return esc(value)
        adult_cell=_rate_cell(c.get('per_adult'), adult_n)
        child_cell=_rate_cell(c.get('per_child'), generic_child_n)
        cwb_cell=_rate_cell(c.get('per_child_cwb'), cwb_n)
        cnb_cell=_rate_cell(c.get('per_child_cnb'), cnb_n)
        eb_cell=_rate_cell(c.get('per_extra_bed'), eb_n)
        child_td='' if has_split_child_cost else f'<td>{child_cell}</td>'
        cost_rows.append(f"<tr><td><b>{esc(c.get('option') or 'Package')}</b></td><td>{adult_cell}</td>{child_td}<td>{cwb_cell}</td><td>{cnb_cell}</td><td>{eb_cell}</td><td class='cost-total'><b>{esc(c.get('currency') or 'INR')} {final_total:,.0f}</b></td></tr>")
    cost_section=''
    if data.get('show_cost', True) and cost_rows:
        child_th='' if has_split_child_cost else '<th>PER CHILD</th>'
        cost_section=f"""<div class='cost-box'><div class='cost-heading'>PACKAGE COST</div><table class='cost-table'><thead><tr><th>PACKAGE / OPTION</th><th>PER ADULT</th>{child_th}<th>CHILD (CWB)</th><th>CHILD (CNB)</th><th>EXTRA BED (EB)</th><th class='cost-total-head'>TOTAL AMOUNT</th></tr></thead><tbody>{''.join(cost_rows)}</tbody></table><div class='cost-footnote'>CWB = Child With Bed &nbsp; | &nbsp; CNB = Child No Bed &nbsp; | &nbsp; EB = Extra Bed</div></div>"""

    special_notes=[]
    policy_text=str(data.get('special_notes') or '').strip()
    if policy_text:
        special_notes=[x.strip(' •-') for x in policy_text.splitlines() if x.strip()]
    special_html=''
    if special_notes:
        special_html="<div class='section'>SPECIAL NOTES</div><div class='special-notes'><ul>"+''.join(f"<li>{esc(x)}</li>" for x in special_notes)+"</ul></div>"

    # ---------- Day-wise ----------
    day_blocks = []
    for x in data.get("days", []):
        optional = x.get("optional_activities") or []
        optional_html = ""
        if optional:
            optional_items = "".join(f"<li>{esc(a)}</li>" for a in optional)
            optional_html = f"<div class='optional-activities'><b>OPTIONAL ACTIVITIES:</b> {' &nbsp;•&nbsp; '.join(esc(a) for a in optional)}</div>"
        day_blocks.append(
            f"<div class='day'><h3>{esc(x.get('day'))} | {esc(x.get('date'))} — {esc(x.get('title'))}</h3>"
            f"<p>{esc(x.get('description'))}</p>{optional_html}<div class='day-meta'><table class='day-meta-table'><tr>"
            f"<td class='stay-line'><b>STAY:</b> {esc(x.get('stay'))}</td>"
            f"<td class='meal-line'><b>MEAL PLAN:</b> {esc(x.get('meal_plan'))}</td></tr></table></div></div>"
        )
    days = "".join(day_blocks) or "<p>No day-wise details provided.</p>"

    inc = "".join(f"<li>{esc(x)}</li>" for x in data.get("inclusions", [])) or "<li>Not specified.</li>"
    exc = "".join(f"<li>{esc(x)}</li>" for x in data.get("exclusions", [])) or "<li>Not specified.</li>"
    logo_html = f"<a href='{MYTOURBAZAR_LOGO_URL}'><img src='{logo}'></a>" if logo else ""
    total_text = sum(len(str(x.get('description',''))) for x in data.get('days', [])) + sum(len(str(x.get('hotel_name',''))) + len(str(x.get('room_category',''))) for x in data.get('hotels', []))
    total_rows = len(data.get('days', [])) + len(data.get('hotels', [])) + len(data.get('transit', []))
    if total_text < 900 and total_rows <= 8:
        density_class = 'itinerary-roomy'
    elif total_text < 1800 and total_rows <= 14:
        density_class = 'itinerary-normal'
    else:
        density_class = 'itinerary-compact'

    greeting = str(data.get("greeting") or "").strip()
    if greeting:
        # Convert a model-generated first-line greeting into a clean paragraph while preserving text.
        greeting_html = esc(greeting).replace("\n", "<br>")
    else:
        greeting_html = (
            f"<b>Dear {esc(data.get('client_name'))},</b><br>"
            f"Greetings from <b>MyTourBazar</b>! We are pleased to present your customized travel proposal."
        )

    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<style>
@page {{size:{page_size};margin:13mm 12mm 15mm 12mm}}
body{{font-family:Arial,'Helvetica Neue',sans-serif;color:#30363b;font-size:9pt;line-height:1.38;margin:0}}
.banner{{display:flex;align-items:center;justify-content:space-between;border-bottom:3px solid #f39a21;padding:0 0 2px;margin-bottom:3px}}
.logo img{{width:1.90in;height:1.90in;object-fit:contain;display:block}}.flight-clipart{{width:22px;height:18px;object-fit:contain;vertical-align:-4px;margin-right:5px}}
.head{{text-align:right;color:#153b5c}}
.label{{font-size:7.4pt;letter-spacing:1.2px;font-weight:bold;color:#6c7780}}
.client{{font-size:15pt;font-weight:700;text-transform:uppercase;margin-top:2px}}
.title{{font-size:8.7pt;font-weight:600;margin-top:2px}}
.greet{{background:#fbfbfa;border-left:4px solid #f39a21;padding:10px 12px;margin:0 0 24px;text-align:justify}}
.section{{color:#153b5c;display:block;text-transform:uppercase;font-size:9.6pt;font-weight:700;letter-spacing:.2px;padding-bottom:5px;margin:15px 0 9px;border-bottom:2px solid #f39a21;line-height:1.05}}
.section:after{{content:'';display:block;width:0}}
table{{width:100%;border-collapse:collapse;margin-bottom:15px;page-break-inside:auto}}
.schedule th{{background:#173d5d;color:#fff;padding:6px 7px;text-align:left;font-size:7.7pt;font-weight:700;letter-spacing:.2px;border:1px solid #d5dadd}}
.schedule td{{border:1px solid #d7dbde;padding:6px 7px;font-size:7.9pt;vertical-align:middle}}
.schedule tr:nth-child(even){{background:#fafafa}}.option-row td{{background:#eef3f6;color:#173d5d;font-weight:700;text-transform:uppercase;letter-spacing:.4px}}.gold-stars{{color:#d9a300;letter-spacing:1px;font-size:10pt;font-weight:700;white-space:nowrap}}.half-star{{display:inline-block;width:.56em;overflow:hidden;white-space:nowrap;vertical-align:baseline;color:#d9a300;letter-spacing:0}}.cost-footnote{{font-size:6.8pt;color:#7a7f83;padding:4px 7px 6px;font-style:italic}}.cost-box{{border:1.5px solid #f39a21;border-radius:6px;overflow:hidden;margin:14px 0 16px;page-break-inside:avoid}}.cost-heading{{background:#f39a21;color:#fff;font-size:10pt;font-weight:700;padding:7px 10px;letter-spacing:.4px}}.cost-table{{margin:0;width:100%;border-collapse:collapse}}.cost-table th{{background:#eef3f6;color:#153b5c;padding:7px 6px;font-size:7.4pt;border:1px solid #d7dbde;text-align:left}}.cost-table td{{padding:7px 6px;font-size:8pt;border:1px solid #d7dbde}}.cost-table th.cost-total-head,.cost-table td.cost-total{{text-align:center;color:#153b5c;vertical-align:middle}}.special-notes{{background:#fff8e8;border:1px solid #f0c56a;border-radius:5px;padding:8px 12px;margin-bottom:14px}}.special-notes li{{font-size:8pt;margin-bottom:4px}}
.logistics{{table-layout:fixed;width:100%;max-width:100%;border:1px solid #d7dbde;border-radius:6px;overflow:hidden;box-sizing:border-box;page-break-inside:avoid}}
.logistics td{{box-sizing:border-box;min-width:0;overflow-wrap:anywhere;word-break:break-word}}
.logistics td.labelcell{{background:#f7f7f6;color:#444;font-weight:700;width:18%;max-width:18%;white-space:normal;padding:5px 5px;font-size:7.1pt}}
.logistics td.valuecell{{width:32%;max-width:32%;padding:5px 6px;font-size:7.4pt;overflow-wrap:anywhere;word-break:break-word;white-space:normal;overflow:hidden}}
.logistics td.hoteltypecell{{padding:5px 6px;font-size:8.2pt;white-space:normal;vertical-align:middle}}.hoteltypecell .gold-stars{{font-size:11pt;letter-spacing:1.4px;color:#d9a300}}.hotel-type-multi{{font-weight:700;color:#173d5d}}
.option-row td{{background:#eef3f6;color:#173d5d;font-weight:700;text-transform:uppercase;letter-spacing:.35px;padding:6px 8px}}
.accommodation{{table-layout:fixed;width:100%}}.accommodation th,.accommodation td{{box-sizing:border-box;overflow-wrap:anywhere;word-break:normal}}.accommodation th:nth-child(1),.accommodation td:nth-child(1){{width:13%}}.accommodation th:nth-child(2),.accommodation td:nth-child(2){{width:14%}}.accommodation th:nth-child(3),.accommodation td:nth-child(3){{width:25%}}.accommodation th:nth-child(4),.accommodation td:nth-child(4){{width:17%}}.accommodation th:nth-child(5),.accommodation td:nth-child(5){{width:14%}}.accommodation th:nth-child(6),.accommodation td:nth-child(6){{width:13%}}
.transit th:nth-child(1),.transit td:nth-child(1){{width:13%}}.transit th:nth-child(2),.transit td:nth-child(2){{width:25%}}.transit th:nth-child(3),.transit td:nth-child(3){{width:35%}}.transit th:nth-child(4),.transit td:nth-child(4),.transit th:nth-child(5),.transit td:nth-child(5){{width:13.5%}}
.journey{{font-size:7pt;color:#6b7379}}
.day{{page-break-inside:avoid;border-bottom:1px dashed #cfd4d7;padding:0 0 11px;margin:0 0 14px}}
.day h3{{font-size:9.2pt;color:#153b5c;margin:0 0 7px;font-weight:700}}
.day p{{margin:0 0 9px;text-align:justify}}
.policy-wrap{{margin-top:2px}}
.policy-block{{page-break-inside:avoid;margin:0 0 8px}}
.policy-title{{color:#153b5c;font-size:8.9pt;font-weight:700;padding:0 0 4px;margin:0 0 3px;border-bottom:1px dashed #cfd4d7}}
.policy-block ul{{margin:0;padding-left:18px}}
.policy-block li{{margin:0 0 3px;font-size:7.7pt;color:#5d6469;line-height:1.35}}
.day-meta{{background:#eef3f6;padding:0;font-size:7.7pt;line-height:1.35}}.day-meta-table{{width:100%;border-collapse:collapse;margin:0}}.day-meta-table td{{width:50%;padding:6px 8px;border:0;vertical-align:middle}}.day-meta-table td:last-child{{text-align:right}}.optional-activities{{background:#fff4a8;border:1px solid #e2c84a;border-radius:4px;padding:6px 9px;margin:8px 0 9px;page-break-inside:avoid;color:#4b4300;font-size:7.7pt;line-height:1.35}}.optional-activities b{{font-size:7.8pt;letter-spacing:.3px}}.stay-line{{color:#30363b}}.meal-line{{color:#f39a21}}
.grid{{display:table;width:100%;table-layout:fixed}}.box{{display:table-cell;width:50%;vertical-align:top;border:1px solid #d7dbde;padding:9px}}
.box:first-child{{border-right:0}}.box-title{{color:#153b5c;font-weight:700;font-size:9pt}}
li{{margin-bottom:4px;font-size:8pt}}.policies{{font-size:7.8pt;white-space:pre-wrap}}
.footer{{border-top:2px solid #f39a21;margin-top:15px;padding-top:9px;text-align:center;color:#4b555c;font-size:7.7pt}}
.footer h2{{margin:0;color:#153b5c;letter-spacing:1.5px;font-size:11pt}}.tag{{color:#f39a21;font-style:italic;margin:2px 0 4px}}
 .itinerary-roomy{{font-size:9.5pt}}.itinerary-roomy .schedule th{{font-size:8.2pt;padding:7px 8px}}.itinerary-roomy .schedule td{{font-size:8.4pt;padding:7px 8px}}.itinerary-roomy .day h3{{font-size:9.8pt}}.itinerary-roomy .day p{{font-size:9.4pt}}.itinerary-roomy .section{{font-size:10pt}} .itinerary-normal{{font-size:9pt}} .itinerary-compact{{font-size:8.5pt}}.itinerary-compact .schedule th{{font-size:7.3pt;padding:5px 6px}}.itinerary-compact .schedule td{{font-size:7.5pt;padding:5px 6px}}.itinerary-compact .day p{{font-size:8.2pt}}
.transit-self{{text-align:center!important;vertical-align:middle!important;height:58px;font-weight:700}}.transit-airport{{font-size:7.5pt;color:#555}}.transit-ticket-note{{margin:2mm 1mm 3mm;padding:2mm 2.6mm;background:#fff7ed;border-left:3px solid #f4a62a;color:#5a4931;font-size:7.8pt;line-height:1.35}}</style></head><body class='{density_class}'>
<div class='banner'><div class='logo'>{logo_html}</div><div class='head'>
<div class='label'>{esc(data.get('document_title') or 'OFFICIAL TOUR ITINERARY')}</div><div class='client'>{esc(data.get('client_name'))}</div>
<div class='title'>{esc(data.get('travel_dates'))} | {esc(data.get('tour_title'))} {('(' + esc(data.get('duration')) + ')') if data.get('duration') else ''}</div>
</div></div>
<div class='greet'>{greeting_html}</div>

<div class='section'>LOGISTICS PROFILE &amp; DETAILS</div>
<table class='schedule logistics'><tbody>
<tr><td class='labelcell'>Guest Name:</td><td class='valuecell'>{esc(data.get('client_name'))}</td>
<td class='labelcell'>Duration:</td><td class='valuecell'>{esc(data.get('duration'))}</td></tr>
<tr><td class='labelcell'>Passenger Profile:</td><td class='valuecell'>{esc(data.get('guest_profile') or data.get('guests'))}</td>
<td class='labelcell'>Travel Dates:</td><td class='valuecell'>{esc(data.get('travel_dates'))}</td></tr>
<tr><td class='labelcell'>Vehicle Assigned:</td><td class='valuecell'>{esc(data.get('vehicle'))}</td>
<td class='labelcell'>Pickup Hub:</td><td class='valuecell'>{esc(data.get('pickup'))}</td></tr>
<tr><td class='labelcell'>Drop Hub:</td><td class='valuecell'>{esc(data.get('drop'))}</td>
<td class='labelcell'>Persons:</td><td class='valuecell'>{esc(data.get('guests') or data.get('guest_profile'))}</td></tr>
<tr><td class='labelcell'>Hotel Type:</td><td class='hoteltypecell' colspan='3'>{hotel_type_html}</td></tr>
</tbody></table>

{transit_section}

<div class='section'>{esc(accommodation_heading)}{stars_html}</div>
<table class='schedule accommodation'><thead><tr><th>DATES</th><th>DESTINATION</th><th>RESORT &amp; HOTEL NAME</th><th>ROOM CATEGORY</th><th>TOTAL ROOMS</th><th>MEAL PLAN</th></tr></thead>
<tbody>{hotels}</tbody></table>
<div class='cost-footnote'>CWB = Child With Bed &nbsp;•&nbsp; CNB = Child No Bed &nbsp;•&nbsp; EB = Extra Bed</div>

{cost_section}

<div class='section'>DAY-BY-DAY EXPERIENCE</div>{days}

<div class='section'>PACKAGE SUMMARY</div>
<div class='grid'><div class='box'><div class='box-title'>✓ INCLUSIONS</div><ul>{inc}</ul></div>
<div class='box'><div class='box-title'>✕ EXCLUSIONS</div><ul>{exc}</ul></div></div>

{special_html}

</body></html>"""
    html = html.replace("size:A4", f"size:{page_size}")
    HTML(string=html).write_pdf(str(output_path))
    return output_path
