from pathlib import Path
from html import escape
import base64
import re
import io
import copy
from weasyprint import HTML
from PIL import Image, ImageChops
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


def _b2b_replace_text(value):
    text=str(value or "")
    text=re.sub(r"(?i)\bsales@mytourbazar\.com\b","our company",text)
    text=re.sub(r"(?i)\b(?:www\.)?mytourbazar\.com\b","our company",text)
    text=re.sub(r"(?i)@mytourbazar\b","our company",text)
    text=re.sub(r"(?i)\bmy\s*tour\s*bazar\b","our company",text)
    text=re.sub(r"(?i)\bmytourbazar\b","our company",text)
    return text


def _b2b_scrub_data(data):
    def scrub(obj):
        if isinstance(obj,dict):
            return {k:scrub(v) for k,v in obj.items()}
        if isinstance(obj,list):
            return [scrub(v) for v in obj]
        if isinstance(obj,tuple):
            return tuple(scrub(v) for v in obj)
        if isinstance(obj,str):
            return _b2b_replace_text(obj)
        return obj
    result=scrub(copy.deepcopy(data or {}))
    result["b2b"]=True
    result["brand_neutral"]=True
    return result



def _airport_source_display_html(airport, terminal=''):
    """Render locked supplier airport wording without semantic cleanup.

    V190 defense-in-depth: if an endpoint still contains obvious cross-column
    contamination, print blank rather than put unreliable information on a ticket.
    """
    airport=re.sub(r'\s+',' ',_text(airport)).strip()
    terminal=re.sub(r'\s+',' ',_text(terminal)).strip()

    suspicious = bool(airport and (
        re.search(r'(?i)\b(?:operated\s+by|fare\s*type|family\s*fare|cabin|duration|stops?|non\s*[- ]?stop|baggage|pnr)\b',airport)
        or re.search(r'(?i)(?:^|\s)by\s+[A-Z0-9]{2,3}(?:\s|$)',airport)
        or re.search(r'(?i)(?:^|\s)\d{1,2}\s*(?:h|hr|hrs|hour|hours)\b(?:\s*\d{0,2}\s*(?:m|min|mins|minute|minutes)\b)?',airport)
        or re.search(r'\b\d{1,2}:\d{2}\b',airport)
        or airport.endswith(('(', '[', '{', ':'))
    ))
    if suspicious:
        airport=''

    terminal_pat=(
        r'\b(?:Terminal\s*(?:No\.?\s*)?[A-Za-z0-9-]+|'
        r'T\s*[1-9]\d*[A-Za-z]?|[1-9][A-Za-z]?\s*Terminal|'
        r'(?:Domestic|International)\s+Terminal)\b'
    )

    embedded=re.search(terminal_pat,airport,re.I)
    if embedded and not terminal:
        terminal=re.sub(r'\s+',' ',embedded.group(0)).strip()

    display=airport
    if terminal and display:
        # Avoid printing the same source terminal twice. This does NOT alter any
        # city/airport wording.
        display=re.sub(re.escape(terminal), ' ', display, count=1, flags=re.I)
        # Handle equivalent embedded form if terminal formatting differs slightly.
        display=re.sub(terminal_pat, ' ', display, count=1, flags=re.I)
        display=re.sub(r'\s+',' ',display).strip()

    parts=[]
    if display:
        parts.append(f'<span class="airport-full">{_esc(display)}</span>')
    if terminal:
        parts.append(f'<span class="terminal-line">{_esc(terminal)}</span>')
    return '<br>'.join(parts) + ('<br>' if parts else '')


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

def _optimized_airline_logo_uri(file):
    """Crop transparent/near-white canvas padding so the visible airline logo prints larger.

    Original airline asset is NEVER overwritten. SVGs and any unreadable image fall
    back to the original URI.
    """
    if not file or not Path(file).exists():
        return ""
    file=Path(file)
    if file.suffix.lower()==".svg":
        return _uri(str(file))
    try:
        with Image.open(file) as source:
            im=source.convert("RGBA")

            # Visible alpha content.
            alpha=im.getchannel("A")
            alpha_mask=alpha.point(lambda a: 255 if a > 12 else 0)

            # Many airline PNG/JPG files have a large opaque WHITE canvas rather
            # than transparent padding. Keep pixels that differ noticeably from white.
            rgb=im.convert("RGB")
            white=Image.new("RGB", rgb.size, (255,255,255))
            diff=ImageChops.difference(rgb, white).convert("L")
            colour_mask=diff.point(lambda p: 255 if p > 16 else 0)

            # Only count coloured content that is actually visible.
            content_mask=ImageChops.multiply(colour_mask, alpha_mask)
            bbox=content_mask.getbbox() or alpha_mask.getbbox()

            if bbox:
                left,top,right,bottom=bbox
                # Tiny breathing room so strokes don't touch the crop edge.
                pad=max(2, int(max(right-left,bottom-top)*0.035))
                left=max(0,left-pad); top=max(0,top-pad)
                right=min(im.width,right+pad); bottom=min(im.height,bottom+pad)
                im=im.crop((left,top,right,bottom))

            # Preserve aspect ratio. Avoid huge embedded bitmaps while keeping
            # more than enough resolution for the PDF.
            max_side=420
            if max(im.size)>max_side:
                ratio=max_side/max(im.size)
                im=im.resize(
                    (max(1,int(im.width*ratio)),max(1,int(im.height*ratio))),
                    Image.Resampling.LANCZOS,
                )

            buf=io.BytesIO()
            im.save(buf,format="PNG",optimize=True)
            return "data:image/png;base64,"+base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return _uri(str(file))


def _airline_logo_html(airline_name, flight_number=""):
    file=_airline_logo_path(airline_name, flight_number)
    uri=_optimized_airline_logo_uri(file) if file else ""
    if not uri:
        return ""
    extra=" fallback" if file == TAKEOFF_CLIPART else ""
    alt=escape(_text(airline_name) or "Airline")
    return f'<div class="airline-logo-wrap"><img class="airline-logo{extra}" src="{uri}" alt="{alt} logo"></div>'


def _baggage_icon(kind):
    """Small deterministic vector clip-art; no emoji/font dependency."""
    if kind == "cabin":
        return (
            '<svg class="bag-icon cabin-bag" viewBox="0 0 24 24" aria-hidden="true">'
            '<path d="M7 7h10l2 4v9H5v-9l2-4z" fill="none" stroke="currentColor" stroke-width="1.8"/>'
            '<path d="M9 7V5.5C9 4.1 10.1 3 11.5 3h1C13.9 3 15 4.1 15 5.5V7" fill="none" stroke="currentColor" stroke-width="1.8"/>'
            '<path d="M5 12h14" fill="none" stroke="currentColor" stroke-width="1.4"/>'
            '</svg>'
        )
    return (
        '<svg class="bag-icon trolley-bag" viewBox="0 0 24 24" aria-hidden="true">'
        '<rect x="6" y="7" width="12" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="1.8"/>'
        '<path d="M10 7V4h4v3M9 11v5M15 11v5" fill="none" stroke="currentColor" stroke-width="1.6"/>'
        '<circle cx="9" cy="21" r="1.2" fill="currentColor"/><circle cx="15" cy="21" r="1.2" fill="currentColor"/>'
        '</svg>'
    )


def _canonical_baggage_pax_type(person, raw_baggage=""):
    """Canonical baggage label: Adult / Child / Infant only.

    Never prints generic `Passenger`. If the category cannot be supported from
    passenger/source context, return blank rather than guessing.
    """
    if isinstance(person,dict):
        raw=" ".join(
            _text(person.get(k))
            for k in ("type","title","name")
        ) + " " + _text(raw_baggage)
        title=_text(person.get("title")).lower().replace(".","")
    else:
        raw=_text(person) + " " + _text(raw_baggage)
        title=_text(person).lower().replace(".","")

    low=raw.lower()
    if re.search(r"\b(?:infant|inf|baby)\b",low):
        return "Infant"
    if re.search(r"\b(?:child|chd|cnn)\b",low):
        return "Child"
    if re.search(r"\b(?:adult|adt)\b",low):
        return "Adult"

    if title in ("master","mstr"):
        return "Child"
    if title in ("mr","mrs","ms","dr","prof"):
        return "Adult"
    if title=="infant":
        return "Infant"
    if title=="child":
        return "Child"
    return ""


def _normalize_weight_token(number, unit):
    try:
        n=float(number)
        number=str(int(n)) if n.is_integer() else str(n).rstrip("0").rstrip(".")
    except Exception:
        number=str(number).strip()

    u=str(unit or "").lower()
    if u.startswith("kg") or u.startswith("kilo"):
        return f"{number}kg"
    if u.startswith("pc") or u.startswith("piece"):
        suffix="pc" if number=="1" else "pcs"
        return f"{number}{suffix}"
    return f"{number}{unit}".strip()


def _baggage_allowance_tokens(text):
    """Extract source allowance tokens once, removing exact duplicates.

    Examples:
      15kgs 15kgs -> ["15kg"]
      7 KG 7kgs   -> ["7kg"]
      1 Piece     -> ["1pc"]
    """
    text=_text(text)
    found=[]
    seen=set()
    pattern=r"(?i)(\d+(?:\.\d+)?)\s*(kg|kgs|kilogram|kilograms|pc|pcs|piece|pieces)\b"
    for m in re.finditer(pattern,text):
        token=_normalize_weight_token(m.group(1),m.group(2))
        key=token.lower()
        if key not in seen:
            seen.add(key)
            found.append(token)
    return found


def _baggage_kind_chunks(raw):
    """Split baggage text by source labels without inventing cabin/check-in data."""
    raw=re.sub(r"\s+"," ",_text(raw)).strip()
    if not raw:
        return []

    check_re=re.compile(
        r"(?i)\b(?:checked(?:[- ]?in)?\s*(?:baggage|bag)?|"
        r"check[- ]?in\s*(?:baggage|bag)?|registered\s*(?:baggage|bag)?)\b"
    )
    cabin_re=re.compile(
        r"(?i)\b(?:cabin\s*(?:baggage|bag)?|hand\s*(?:baggage|bag)?|"
        r"carry[- ]?on\s*(?:baggage|bag)?)\b"
    )

    labels=[]
    for m in check_re.finditer(raw):
        labels.append((m.start(),m.end(),"checkin"))
    for m in cabin_re.finditer(raw):
        labels.append((m.start(),m.end(),"cabin"))
    labels.sort(key=lambda x:x[0])

    chunks=[]
    if labels:
        # If an allowance appears before the first CABIN label (common format:
        # `Adult 15kg, Cabin 7kg`), that prefix is the check-in allowance.
        prefix=raw[:labels[0][0]].strip(" ,;|:/-")
        if prefix and _baggage_allowance_tokens(prefix):
            prefix_kind="checkin" if labels[0][2]=="cabin" else labels[0][2]
            chunks.append((prefix_kind,prefix))

        for i,(s,e,kind) in enumerate(labels):
            next_s=labels[i+1][0] if i+1<len(labels) else len(raw)
            chunk=raw[s:next_s].strip(" ,;|:/-")
            if chunk:
                chunks.append((kind,chunk))
    else:
        # No source cabin/check-in label. Do NOT guess a second category.
        chunks=[("checkin",raw)]

    return chunks


def _normalized_baggage_entries(value, person=None):
    """Return normalized unique entries: [(kind, pax_type, allowance), ...]."""
    raw=re.sub(r"\s+"," ",_text(value)).strip()
    if not raw:
        return []

    pax_type=_canonical_baggage_pax_type(person,raw)
    if not pax_type:
        # Never fall back to the word Passenger or guess a category.
        return []

    entries=[]
    seen=set()
    for kind,chunk in _baggage_kind_chunks(raw):
        tokens=_baggage_allowance_tokens(chunk)
        if not tokens:
            continue

        # Preserve multiple DISTINCT source allowances if genuinely present, but
        # never print duplicated tokens.
        allowance=" + ".join(tokens)
        key=(kind,pax_type.lower(),allowance.lower())
        if key in seen:
            continue
        seen.add(key)
        entries.append((kind,pax_type,allowance))
    return entries


def _baggage_signature(value, person=None):
    return tuple(
        (kind,pax_type.lower(),allowance.lower())
        for kind,pax_type,allowance in _normalized_baggage_entries(value,person)
    )


def _baggage_html(value, person=None):
    """Render only canonical type + normalized allowance.

    Example:
      [trolley icon] Adult 15kg
      [cabin icon]   Adult 7kg

    No `Passenger`, no `15kg 15kg`, no repeated baggage labels.
    """
    entries=_normalized_baggage_entries(value,person)
    lines=[]
    for kind,pax_type,allowance in entries:
        lines.append(
            f'<span class="bag-line">{_baggage_icon(kind)}'
            f'<span>{_esc(pax_type)} {_esc(allowance)}</span></span>'
        )
    return ''.join(lines)


def _is_payment_total_label(label):
    text=re.sub(r'\s+',' ',_text(label)).strip().lower()
    if not text:
        return False
    return bool(re.fullmatch(
        r'(?:grand\s+total|gross\s+total|total\s+fare|total\s+amount|'
        r'booking\s+total|net\s+(?:amount|payable)|amount\s+(?:paid|payable)|'
        r'final\s+(?:fare|amount|total)|total\s+price|payable\s+amount)',
        text,
        re.I,
    ))


def _clean_payment_rows(data):
    """Supplier charge rows only; summary total rows are deliberately excluded."""
    rows=[]
    for item in ((data or {}).get("payment_items") or []):
        if not isinstance(item,dict):
            continue
        label=_text(item.get("label"))
        if not label or _is_payment_total_label(label):
            continue
        try:
            amount=float(item.get("amount") or 0)
        except Exception:
            amount=0
        if amount >= 0:
            rows.append({"label":label,"amount":amount})

    if not rows:
        try: base=float((data or {}).get("base_fare") or 0)
        except Exception: base=0
        try: tax=float((data or {}).get("taxes") or 0)
        except Exception: tax=0
        if base > 0:
            rows.append({"label":"Base Fare","amount":base})
        if tax > 0:
            rows.append({"label":"Taxes & Fees","amount":tax})
    return rows


def _reconciled_supplier_total(data, rows=None):
    """Return a payable total that is arithmetically consistent with source rows."""
    rows=_clean_payment_rows(data) if rows is None else rows
    component_sum=sum(max(0.0,float(x.get("amount") or 0)) for x in rows)

    try:
        gross=float((data or {}).get("gross_total") or 0)
    except Exception:
        gross=0

    if component_sum > 0 and gross > 0:
        tolerance=max(2.0,gross*0.01)
        # Critical source-truth safeguard:
        # if all genuine charge lines total MORE than gross_total, do not scale
        # against that impossible smaller gross value.
        if component_sum > gross + tolerance:
            return component_sum
        return gross

    if gross > 0:
        return gross
    if component_sum > 0:
        return component_sum

    try:
        return max(
            0.0,
            float((data or {}).get("base_fare") or 0)
            + float((data or {}).get("taxes") or 0),
        )
    except Exception:
        return 0.0


def _payment_breakdown(data, updated_total):
    """Return payment rows using ONLY supplier-provided cost field names.

    Any difference between extracted component rows and the verified supplier total,
    and any customer selling markup/reduction, is distributed proportionally across
    the AVAILABLE source cost rows. The renderer never invents an `Other Supplier
    Charges` row. If that exact row genuinely exists in the supplier source, it is
    naturally preserved because it came through payment_items.
    """
    rows=_clean_payment_rows(data)
    source_sum=sum(max(0.0,float(x["amount"])) for x in rows)
    supplier_total=_reconciled_supplier_total(data,rows)

    if updated_total is None:
        target=max(0.0,float(supplier_total or source_sum or 0))
    else:
        try:
            target=max(0.0,float(updated_total))
        except Exception:
            target=max(0.0,float(supplier_total or source_sum or 0))

    if rows and source_sum > 0:
        # Always distribute to the source-provided fields themselves.
        factor=target/source_sum
        adjusted=[
            {"label":row["label"],"amount":max(0,round(float(row["amount"])*factor))}
            for row in rows
        ]

        # Make printed rows add to the exact target without ever going negative.
        diff=round(target)-sum(x["amount"] for x in adjusted)
        if adjusted and diff>0:
            adjusted[-1]["amount"] += diff
        elif adjusted and diff<0:
            remaining=-diff
            for idx in sorted(range(len(adjusted)),key=lambda i:adjusted[i]["amount"],reverse=True):
                reducible=min(remaining,adjusted[idx]["amount"])
                adjusted[idx]["amount"] -= reducible
                remaining -= reducible
                if remaining<=0:
                    break

        for row in adjusted:
            row["amount"]=max(0,row["amount"])

        final_diff=round(target)-sum(x["amount"] for x in adjusted)
        if adjusted and final_diff>0:
            adjusted[-1]["amount"] += final_diff

        return adjusted,target

    if target>0:
        # Only when the supplier gave no usable component rows at all.
        return [{"label":"Total Fare","amount":round(target)}],target
    return [],target

def generate_flight_ticket(data, updated_total, output_path, logo_path=None, page_size="A4", text_scale_override=None, logo_scale_override=None):
    b2b=bool((data or {}).get("b2b") or (data or {}).get("brand_neutral"))
    if b2b:
        data=_b2b_scrub_data(data)
        logo_path=None
    segs=data.get('segments') or []
    passengers=data.get('passengers') or []
    base=tax=None
    payment_rows, payment_total = _payment_breakdown(data, updated_total)
    if updated_total is not None:
        try:
            source_base=float(data.get('base_fare',0) or 0); source_tax=float(data.get('taxes',0) or 0)
            compatibility_total=_reconciled_supplier_total(data)
            if compatibility_total>0:
                base=round(float(updated_total)*source_base/compatibility_total) if source_base>0 else 0
                tax=max(0,round(float(updated_total)-base)) if source_tax>0 else 0
        except Exception:
            pass

    logo=_uri(logo_path)
    if b2b:
        logo_html='<div class="b2b-brand">our company</div>'
    else:
        logo_html=f'<a href="{MYTOURBAZAR_LOGO_URL}"><img class="brand-logo" src="{logo}"></a>' if logo else ''
    customer_mobile=_text(data.get('mobile'))  # STRICT: no fallback to airline/PNR/other numbers.
    baggage_summary=_text(data.get("baggage_summary"))
    ancillary_summary=_text(data.get("special_ancillary_summary"))
    has_real_ticket=any(_text(p.get("ticket_number")) for p in passengers)
    ticket_heading="Ticket Number" if has_real_ticket else "Airline PNR"
    has_ancillary=bool(ancillary_summary or any(_text(p.get("special_ancillary")) for p in passengers))
    pax_table_class="pax has-ancillary" if has_ancillary else "pax"

    pax_rows_list=[]
    seen_baggage_signatures=set()
    for i,p in enumerate(passengers):
        ticket_value=_text(p.get("ticket_number")) if has_real_ticket else _text(data.get("airline_pnr"))
        baggage_value=_text(p.get("baggage")) or baggage_summary
        ancillary_value=_text(p.get("special_ancillary")) or ancillary_summary

        baggage_signature=_baggage_signature(baggage_value,p)
        if baggage_signature and baggage_signature in seen_baggage_signatures:
            baggage_html=""
        else:
            baggage_html=_baggage_html(baggage_value,p)
            if baggage_signature:
                seen_baggage_signatures.add(baggage_signature)

        row=(
            f'<tr><td class="pax-index">{i+1}</td>'
            f'<td class="pax-name"><strong>{_esc(_display_person_name(p))}</strong></td>'
            f'<td class="pax-pnr">{_esc(ticket_value)}</td>'
            f'<td class="pax-type">{_esc(p.get("type") or "Adult")}</td>'
            f'<td class="pax-dob">{_esc(p.get("dob"))}</td>'
            f'<td class="baggage-cell">{baggage_html}</td>'
        )
        if has_ancillary:
            row += f'<td class="ancillary-cell">{_esc(ancillary_value)}</td>'
        row += '</tr>'
        pax_rows_list.append(row)
    pax_colspan=7 if has_ancillary else 6
    pax_rows=''.join(pax_rows_list) or f'<tr><td colspan="{pax_colspan}">No passenger details found.</td></tr>'

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
        dep_airport=_text(s.get('dep_airport_source_exact') or s.get('dep_airport'))
        arr_airport=_text(s.get('arr_airport_source_exact') or s.get('arr_airport'))
        # V189 STRICT SOURCE-TRUTH DISPLAY:
        # Airport wording is printed exactly as extracted from the supplier endpoint.
        # Never strip leading city/code words such as Delhi or Raipur.
        dep_airport_html=_airport_source_display_html(dep_airport, dep_terminal)
        arr_airport_html=_airport_source_display_html(arr_airport, arr_terminal)
        duration=_text(s.get("duration"))
        stops=_text(s.get('stops'))
        if re.fullmatch(r'0(?:\s*stops?)?', stops, re.I):
            stops=''
        duration_bits=[]
        if duration: duration_bits.append(f'<span class="duration-main">{_esc(duration)}</span>')
        if stops: duration_bits.append(f'<span class="stops">{_esc(stops)}</span>')
        duration_html='<td class="duration">'+('<span class="duration-join"> • </span>'.join(duration_bits) if duration_bits else '')+'</td>'
        airline_logo_html = _airline_logo_html(airline, number)
        cabin=_text(s.get('cabin'))
        fare_type=_text(s.get('fare_type'))
        cabin_html=f'<br><span class="flight-meta">Cabin: {_esc(cabin)}</span>' if cabin else ''
        fare_type_html=f'<br><span class="flight-meta">Fare type: {_esc(fare_type)}</span>' if fare_type else ''
        rows.append(f'''<tr class="flight-row">
          <td>{airline_logo_html}<strong>{identity}</strong>{cabin_html}{fare_type_html}</td>
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
    if isinstance(terms,str):
        terms=[terms]
    else:
        terms=list(terms)

    # Standard MyTourBazar Air Print notes are always appended, even when a
    # supplier supplies its own general instructions.
    standard_air_notes=[
        'Unless specifically mentioned otherwise on the ticket, the standard check-in baggage allowance is considered as one piece per passenger, subject to the airline\'s baggage policy.',
        'For last-minute cancellations, amendments or urgent schedule-related assistance, please contact the respective airline\'s customer-care / toll-free number directly.',
        'Cancellation, amendment, seat, baggage and other airline service charges are governed by the respective airline\'s current policy.',
        'Please refer to the original airline ticket / e-ticket for the latest flight timings, terminal information and operational updates before travel.'
    ]
    existing_norm={re.sub(r'\s+',' ',str(x or '')).strip().lower() for x in terms}
    for note in standard_air_notes:
        norm=re.sub(r'\s+',' ',note).strip().lower()
        if norm not in existing_norm:
            terms.append(note)
            existing_norm.add(norm)

    terms_html=''.join(f'<li>{_esc(x)}</li>' for x in terms)

    booking_date=_text(data.get('booking_date'))
    booked_on_html=(
        f'<div class="booked-on">Booked on: {_esc(booking_date)}</div>'
        if booking_date else ''
    )

    html=f'''<!doctype html><html><head><meta charset="utf-8"><style>
@page{{size:{page_size};margin:12mm 10mm 8mm 10mm}}*{{box-sizing:border-box}}body{{margin:0;color:#12324b;font-family:"MTBLiberationSerifBold",Georgia,serif;font-size:10.2pt;line-height:1.28}}.top{{height:33mm;position:relative;border-bottom:2.5px solid #f4a62a;margin-bottom:5mm}}.brand{{position:absolute;left:0;top:0;width:45%;height:26mm;display:flex;align-items:center}}.brand-logo{{max-width:1.55in;max-height:1.05in;object-fit:contain}}.b2b-brand{{font-size:15pt;font-weight:900;color:#073450;text-transform:lowercase;letter-spacing:.2px}}.heading{{position:absolute;right:0;top:3mm;text-align:right;color:#073450}}.heading h1{{margin:0;font-size:21pt;letter-spacing:.3px}}.heading div{{font-size:10.5pt;margin-top:2mm}}.heading .booked-on{{font-size:8.6pt;margin-top:1.1mm;color:#5d646a;font-weight:700}}.meta{{background:#f0f5fa;border:1.5px solid #9cb6cc;border-radius:6px;padding:5mm 6mm;margin-bottom:5mm}}.meta table,.pax,.flights{{width:100%;border-collapse:collapse}}.meta td{{padding:1.4mm 1mm;font-size:10.2pt}}.meta .lab{{font-weight:700;white-space:nowrap}}.meta .val{{font-weight:800;color:#063655}}.meta .pnr-val{{font-weight:900;color:#e65100;font-size:11.2pt;letter-spacing:.2px}}.section{{background:#063655;color:#fff;padding:2.6mm 4mm;font-size:12pt;letter-spacing:.1px;border-radius:3px 3px 0 0;margin-top:4mm}}.pax,.flights{{border:1.5px solid #9cb6cc}}.pax th,.flights th{{background:#dceaf5;color:#123b58;padding:2.6mm 2.4mm;text-align:center;font-size:9.7pt;border-bottom:1px solid #9cb6cc;vertical-align:middle}}.flights thead th:nth-child(1),.flights thead th:nth-child(2),.flights thead th:nth-child(4){{text-align:center!important;vertical-align:middle!important}}.duration-head{{text-align:center!important;vertical-align:middle!important;line-height:1.12}}.duration-head span{{display:block}}.duration-head .duration-head-stop{{margin-top:.55mm}}.pax td{{padding:2.6mm 2.0mm;border-bottom:1px solid #d3e0eb;font-size:9.4pt;color:#1f2c35;vertical-align:middle;text-align:center}}.pax th:nth-child(1),.pax td:nth-child(1){{width:4%}}.pax th:nth-child(2),.pax td:nth-child(2){{width:32%}}.pax th:nth-child(3),.pax td:nth-child(3){{width:16%}}.pax th:nth-child(4),.pax td:nth-child(4){{width:10%}}.pax th:nth-child(5),.pax td:nth-child(5){{width:12%}}.pax th:nth-child(6),.pax td:nth-child(6){{width:26%}}.pax.has-ancillary th:nth-child(1),.pax.has-ancillary td:nth-child(1){{width:4%}}.pax.has-ancillary th:nth-child(2),.pax.has-ancillary td:nth-child(2){{width:27%}}.pax.has-ancillary th:nth-child(3),.pax.has-ancillary td:nth-child(3){{width:14%}}.pax.has-ancillary th:nth-child(4),.pax.has-ancillary td:nth-child(4){{width:9%}}.pax.has-ancillary th:nth-child(5),.pax.has-ancillary td:nth-child(5){{width:10%}}.pax.has-ancillary th:nth-child(6),.pax.has-ancillary td:nth-child(6){{width:21%}}.pax.has-ancillary th:nth-child(7),.pax.has-ancillary td:nth-child(7){{width:15%}}.pax-name{{font-size:11.3pt!important;font-weight:800;line-height:1.18;overflow-wrap:anywhere}}.pax-pnr{{font-weight:800;color:#123b58}}.pax-index,.pax-type,.pax-dob{{white-space:nowrap}}.baggage-cell{{font-size:8.7pt;text-align:center!important}}.bag-line{{display:flex;align-items:center;justify-content:center;gap:1.2mm;margin:.7mm auto;line-height:1.2;text-align:left;width:fit-content;max-width:100%}}.bag-icon{{width:15px;height:15px;min-width:15px;color:#123b58;vertical-align:middle}}.ancillary-cell{{font-size:8.5pt;font-weight:700;color:#334b5c;text-align:center!important}}.flights td{{padding:3.6mm 3mm;border-bottom:1px solid #d3e0eb;vertical-align:middle;font-size:9.7pt;color:#26323a;white-space:normal;overflow:visible;word-break:normal;overflow-wrap:anywhere;text-align:center}}.airport-full{{display:inline-block;margin-top:.7mm;white-space:normal;overflow:visible;word-break:normal;overflow-wrap:anywhere;line-height:1.25;color:#4c5963;font-size:8.8pt;font-weight:700}}.terminal-line{{display:inline-block;margin-top:.45mm;font-size:8.7pt;font-weight:800;color:#123b58;white-space:normal}}.airline-logo-wrap{{width:96px;height:42px;display:flex;align-items:center;justify-content:center;margin:0 auto 1.8mm auto;overflow:visible}}.airline-logo{{display:block;max-width:92px;max-height:38px;width:auto;height:auto;object-fit:contain;object-position:center center}}.airline-logo.fallback{{opacity:.88;max-width:52px;max-height:34px;padding:2px}}.flight-id{{font-size:8.4pt;color:#5d646a}}.flight-meta{{display:inline-block;margin-top:.7mm;font-size:8.7pt;font-weight:600;color:#5d646a}}.flights .flight-row td:first-child{{width:27%;text-align:center}}.flights .flight-row td:nth-child(2){{width:29%;text-align:center}}.flights .flight-row td:nth-child(3){{width:15%;text-align:center}}.flights .flight-row td:nth-child(4){{width:29%;text-align:center}}.time{{font-size:12.3pt;font-weight:900;color:#123b58;line-height:1.18}}.muted{{font-family:Georgia,serif;color:#5d646a;font-size:8.7pt}}.duration{{font-weight:800;color:#123b58;text-align:center!important;vertical-align:middle!important;white-space:normal;padding-left:1.2mm!important;padding-right:1.2mm!important}}.duration-main{{display:block;font-size:10.2pt;color:#123b58;line-height:1.2;white-space:nowrap;text-align:center}}.stops{{display:block;margin-top:.8mm;font-size:8.7pt;color:#5d646a;line-height:1.15;white-space:nowrap;text-align:center}}.duration-join{{display:none}}.terminal{{display:inline-block;margin-top:1.2mm;color:#073450;font-size:8.8pt}}.dash{{color:#9cb6cc;letter-spacing:1px}}.layover td{{background:#edf4fa;text-align:center;padding:2.5mm;font-size:9.6pt;font-weight:800;color:#123b58}}.fare{{margin-top:4mm;background:#f7fafc;border:1.5px solid #9cb6cc;border-radius:6px;padding:0;overflow:hidden;font-size:9.6pt;break-inside:avoid;page-break-inside:avoid}}.fare-title{{background:#e5eff7;color:#123b58;font-size:10.2pt;font-weight:900;padding:2.4mm 3.5mm;border-bottom:1px solid #b8cad9;letter-spacing:.15px}}.payment-table{{width:100%;border-collapse:collapse}}.payment-table td{{padding:2.1mm 3.5mm;border-bottom:1px solid #d7e2eb;vertical-align:middle}}.payment-table .pay-label{{text-align:left;font-weight:700;color:#334b5c}}.payment-table .pay-amount{{text-align:right;font-weight:800;color:#123b58;white-space:nowrap}}.payment-table .payment-total td{{border-bottom:0;background:#eef5fa;font-size:10.6pt;font-weight:900;color:#073450;padding-top:2.7mm;padding-bottom:2.7mm}}.payment-table .payment-total td:last-child{{text-align:right;color:#e65100;white-space:nowrap}}.terms{{border:1.5px solid #9cb6cc;border-radius:6px;padding:4mm 5mm;margin-top:5mm}}.terms h3{{margin:0 0 2mm;color:#123b58;font-size:11.5pt;border-bottom:1px solid #c6d5e2;padding-bottom:2mm}}.terms ul{{margin:0;padding-left:5mm}}.terms li{{margin:1.8mm 0;font-size:9pt;color:#26323a}}.keep{{break-inside:avoid;page-break-inside:avoid}}tr{{break-inside:avoid;page-break-inside:avoid}}strong{{font-weight:800}}
</style></head><body>
<div class="top"><div class="brand">{logo_html}</div><div class="heading"><h1>E-TICKET ITINERARY</h1><div>Flight Booking Confirmation</div>{booked_on_html}</div></div>
<div class="meta"><table><tr><td class="lab">Trip ID:</td><td class="val">{_esc(data.get('booking_id') or data.get('trip_id'))}</td><td class="lab">Travel Date:</td><td class="val">{_esc(data.get('travel_date') or (segs[0].get('dep_date') if segs else ''))}</td></tr><tr><td class="lab">Airline PNR:</td><td class="val pnr-val">{_esc(data.get('airline_pnr'))}</td><td class="lab">GDS PNR:</td><td class="val">{_esc(data.get('gds_pnr'))}</td></tr><tr><td class="lab">Status:</td><td class="val">{_esc(data.get('status') or 'CONFIRMED')}</td><td class="lab">Customer Mobile:</td><td class="val">{_esc(customer_mobile)}</td></tr></table></div>
<div class="section">PASSENGER INFORMATION</div><table class="{pax_table_class}"><thead><tr><th>#</th><th>Passenger Name</th><th>{_esc(ticket_heading)}</th><th>Type</th><th>DOB</th><th>Baggage</th>{('<th>Special Ancillary</th>' if has_ancillary else '')}</tr></thead><tbody>{pax_rows}</tbody></table>
<div class="section">FLIGHT DETAILS &amp; SCHEDULE{(' (CONNECTING ITINERARY)' if len(segs)>1 else '')}</div><table class="flights"><thead><tr><th>Flight &amp; Aircraft</th><th>Departure</th><th class="duration-head"><span>Duration</span><span class="duration-head-stop">Stops</span></th><th>Arrival</th></tr></thead><tbody>{flight_rows}</tbody></table>
{fare_html}<div class="terms keep"><h3>General Instructions</h3><ul>{terms_html}</ul></div>
</body></html>'''
    html=apply_css_settings(html, kind="flight", text_scale_override=text_scale_override, logo_scale_override=logo_scale_override)
    HTML(string=html).write_pdf(str(output_path))
    return base,tax
