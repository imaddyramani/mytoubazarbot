import json, re
from pathlib import Path
from google import genai
from google.genai import types

from ai_retry import call_with_high_demand_retry

MYTOURBAZAR_LOGO_URL = "https://share.google/UUxbVDVNxkIgplZio"
SCHEMA={"type":"object","properties":{
 "booking_id":{"type":"string"},"booking_date":{"type":"string"},"airline_pnr":{"type":"string"},"gds_pnr":{"type":"string"},
 "status":{"type":"string"},"mobile":{"type":"string"},
 "segments":{"type":"array","items":{"type":"object","properties":{
  "flight":{"type":"string"},"flight_number":{"type":"string"},"aircraft":{"type":"string"},"cabin":{"type":"string"},
  "dep_time":{"type":"string"},"dep_city":{"type":"string"},"dep_code":{"type":"string"},"dep_date":{"type":"string"},"dep_airport":{"type":"string"},"dep_terminal":{"type":"string"},
  "arr_time":{"type":"string"},"arr_city":{"type":"string"},"arr_code":{"type":"string"},"arr_date":{"type":"string"},"arr_airport":{"type":"string"},"arr_terminal":{"type":"string"},
  "duration":{"type":"string"},"stops":{"type":"string"}
 },"required":["flight","flight_number","aircraft","cabin","dep_time","dep_city","dep_code","dep_date","dep_airport","dep_terminal","arr_time","arr_city","arr_code","arr_date","arr_airport","arr_terminal","duration","stops"]}},
 "passengers":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"},"title":{"type":"string"},"ticket_number":{"type":"string"},"type":{"type":"string"},"dob":{"type":"string"},"baggage":{"type":"string"}},"required":["name","title","ticket_number","type","dob","baggage"]}},
 "base_fare":{"type":"number"},"taxes":{"type":"number"},
 "gross_total":{"type":"number"},
 "payment_items":{"type":"array","items":{"type":"object","properties":{"label":{"type":"string"},"amount":{"type":"number"}},"required":["label","amount"]}}
},"required":["booking_id","booking_date","airline_pnr","gds_pnr","status","mobile","segments","passengers","base_fare","taxes","gross_total","payment_items"]}

FARE_SCHEMA={"type":"object","properties":{
    "gross_total":{"type":"number"},
    "base_fare":{"type":"number"},
    "taxes":{"type":"number"},
    "payment_items":{"type":"array","items":{"type":"object","properties":{"label":{"type":"string"},"amount":{"type":"number"}},"required":["label","amount"]}}
},"required":["gross_total","base_fare","taxes","payment_items"]}

FARE_RECOVERY_PROMPT="""You are MyTourBazar's fare-recovery engine.
Read ALL pages of the supplied PDF/images. The fare/invoice may be on any page, including a later page.
Search every page before concluding that no fare exists. Ignore only terms/conditions, marketing and unrelated legal pages.
Find the final supplier payable amount and EVERY monetary charge line that belongs to the booking/payment breakdown. Preserve the supplier labels and their printed order. This can include Air Fare Charges, Base Fare, Fuel Surcharge/YQ/YR, Taxes, Fees, Seat Charges, Meal Charges, Baggage Charges, Ancillary Services, Convenience/Service Fees, Insurance, GST/K3 and any other explicitly printed booking charge. Do not merge or silently drop a charge.

Return payment_items as a line-by-line list of {label, amount} in supplier order. gross_total must be the supplier final payable amount. base_fare and taxes remain compatibility summary fields only.

Priority for the final amount:
1. Gross Total
2. Grand Total
3. Total Amount
4. Net Payable / Amount Payable
5. Total Fare
If charge lines are printed, capture every one. Never hide Fuel/YQ or ancillary charges inside Taxes unless the supplier itself prints them that way.
Example payment_items: [{"label":"Air Fare Charges","amount":35000},{"label":"Fuel Surcharge YQ","amount":6400},{"label":"Fees and Taxes","amount":15024}].
Never use a random amount from terms/conditions. If no final fare is actually printed, return gross_total=0 and an empty payment_items list.
Return ONLY JSON matching the schema."""

PROMPT='''You are MyTourBazar's FAST flight-ticket extraction engine.

IMPORTANT: Do NOT read or summarize the supplier's general terms, conditions, offers, marketing text, baggage policy paragraphs, refund/cancellation rules, legal notices, loyalty-program information, payment instructions, or other text that is not needed for the itinerary. Ignore those sections completely.

Extract ONLY the booking data required for our customer-facing Air Print:
- booking ID
- booking date ONLY when the supplier explicitly prints a booking/issued/booked-on date. CRITICAL: the travel/departure date is NEVER a booking date. If no explicit booking date is printed, booking_date MUST be an empty string.
- airline PNR and GDS PNR
- customer mobile ONLY when the supplier explicitly prints a customer/passenger/contact mobile or phone number. If no customer mobile is printed, mobile MUST be an empty string. NEVER use an airline phone number, support number, flight number, PNR, ticket number, Trip ID or any other number as customer mobile.
- passenger names, passenger title (Mr./Mrs./Ms./Master/Miss/etc.), ticket numbers, passenger type, date of birth and baggage
- airline/carrier name and every FULL flight number, including the airline code (example: `6E 405`, not just `405`)
- aircraft type/model when it is explicitly printed by the supplier (example: Airbus A320neo); otherwise return an empty string
- every separate flight sector, including connecting/onward/return sectors
- departure/arrival date, local time, city, and the COMPLETE airport/location wording exactly as printed by the supplier. Never shorten a long airport name. Preserve airport qualifiers such as International, Domestic, Airport, Terminal, Gate-area wording, city/airport combinations and punctuation when they are part of the supplier text. Use dep_airport / arr_airport for the full printed airport text and dep_code / arr_code for a printed 3-letter IATA code.
- terminal is CRITICAL WHEN PRESENT IN THE SOURCE: capture T1/T2/T3, Terminal 1/2/3, 2A/2B, domestic/international terminal labels, etc. in dep_terminal / arr_terminal.
- AIRPORT + TERMINAL SOURCE TRUTH: dep_airport / arr_airport must preserve the COMPLETE endpoint wording exactly as the supplier prints it. If the supplier prints `Guwahati - Lokpriya Gopinath Bordoloi Terminal 2`, keep that full wording in dep_airport AND set dep_terminal=`Terminal 2`. Never shorten the airport name and never move a terminal to the wrong endpoint.
- cabin, exact elapsed flight duration and stops WHEN PRINTED. Search carefully for Duration / Travel Time / Elapsed Time. If the supplier prints duration, it must be copied exactly; if it does not, leave it empty rather than calculating or inventing it.
- EVERY payment/fare charge line when explicitly printed. Preserve the original label and order in payment_items (examples: Air Fare Charges, Fuel Surcharge/YQ/YR, Fees and Taxes, Seat, Meal, Baggage, Ancillary, Convenience Fee, Service Fee, GST/K3). Never omit a monetary line merely because it is not called base fare or tax.
- gross_total = the supplier final payable/total amount exactly as printed. base_fare and taxes are compatibility summary fields only.

If a supplier PDF has many pages, SEARCH ALL PAGES for booking, itinerary, passenger, flight and fare information. The fare/invoice can be on a later page and must never be assumed to be on page 1. Ignore only terms-and-conditions/marketing pages when they contain no booking or fare facts.

Never merge separate sectors. Never invent missing facts.
For each segment, `flight` must be the airline name when known (example: `IndiGo`) and `flight_number` must contain the full carrier code + number (example: `6E 405`). Never split `6E` into the airline field and `405` into the flight-number field.
For each passenger:
- Preserve an explicit title from the supplier (Mr, Mrs, Ms, Master, Miss, etc.).
- IMPORTANT: keep the title ONLY in the `title` field. The `name` field must contain the passenger name without a leading title. Example: supplier `Mr. Govind Sinha` -> title=`Mr.` and name=`Govind Sinha`. Never return `title=Mr.` with `name=Mr. Govind Sinha`.
- If the supplier does not explicitly print a title, use smart contextual inference only when the source itself provides enough evidence (for example a title in nearby text). Do NOT guess a person's gender from their name alone. If there is no reliable evidence, use a neutral title appropriate to the passenger type: "Mr./Ms." for an adult, "Child" for a child, and "Infant" for an infant.
- Preserve date of birth when printed. Never invent a DOB. For children and infants, DOB is especially important and must be returned whenever it is present in the supplier source.
If fare is not printed, return base_fare=0, taxes=0, gross_total=0 and payment_items=[]. Return ONLY JSON matching the supplied schema.'''

REPAIR_PROMPT='''You are MyTourBazar's STRICT SOURCE-TRUTH flight validation pass.

Re-read ALL supplied flight source pages/images/text from scratch. The CURRENT EXTRACTION is only an UNTRUSTED candidate. Every corrected value must be supported by the visible supplier source.

TOP-LEVEL FIELDS — STRICT:
- booking_date: copy ONLY an explicitly labelled booking/issued/booked-on date such as `Booking Date`, `Booked on`, `Booked Date`, `Issued on`, or equivalent. A travel/departure/arrival date is NEVER a booking date. If no explicit booking date is visible, return booking_date="".
- mobile: copy ONLY an explicitly printed CUSTOMER/PASSENGER/CONTACT mobile or phone number. If no customer mobile is visible, return mobile="". NEVER copy airline/customer-care/support numbers, flight numbers, ticket numbers, PNRs, Trip IDs or random numeric strings into mobile.
- booking_id, airline_pnr, gds_pnr and status must also come only from the source.

SEGMENT FIELDS — STRICT:
- Verify the 3-letter departure and arrival IATA codes for EVERY sector.
- Preserve the COMPLETE printed airport/location wording exactly as shown by the supplier. Never shorten or summarize airport names.
- If terminal wording is part of the supplier endpoint line, keep it in dep_airport / arr_airport too. Example: source `Guwahati - Lokpriya Gopinath Bordoloi Terminal 2` -> dep_airport must retain that complete wording and dep_terminal=`Terminal 2`.
- VALIDATE EVERY departure and arrival terminal FROM SCRATCH. A terminal belongs ONLY to the exact endpoint beside which the supplier prints it.
- If CURRENT EXTRACTION contains a terminal not visibly attached to that endpoint, CLEAR the terminal field.
- Recognize T1/T2/T3, Terminal 1/2/3, 2A/2B, domestic/international terminal wording exactly when printed.
- Never copy a departure terminal into arrival or an arrival terminal into departure.
- Copy exact departure/arrival dates and local times, including overnight arrival dates.
- Copy exact flight duration / elapsed time / travel time only when printed.
- Never merge connecting sectors.
- Never invent or calculate missing values.

Return ONLY JSON matching the schema.'''



def _prepare_flight_source(item, work_dir):
    """Create a small, relevant-only copy before sending it to Gemini."""
    from pathlib import Path
    p=Path(item['path'])
    mime=item['mime_type']
    work_dir=Path(work_dir); work_dir.mkdir(parents=True, exist_ok=True)

    if p.suffix.lower()=='.pdf':
        try:
            import fitz
            doc=fitz.open(str(p))
            keep=[]
            keywords=(
                'flight','itinerary','booking','pnr','passenger','ticket','departure','arrival',
                'origin','destination','sector','fare','tax','airline','e-ticket','eticket','journey'
            )
            ignore=('terms and conditions','terms & conditions','conditions of carriage','privacy policy','disclaimer','offers','advertisement')
            for i,page in enumerate(doc):
                text=(page.get_text('text') or '').lower()
                score=sum(1 for k in keywords if k in text)
                if any(k in text for k in ignore):
                    score-=3
                if score>=1:
                    keep.append(i)
            # IMPORTANT: supplier fares and booking facts may appear on ANY page.
            # Never truncate to the first few pages. Keep every page so fare recovery,
            # passenger details, connected sectors and later-page invoices are visible.
            # Terms/conditions are left in the source PDF and are ignored by the extraction prompt.
            keep=sorted(set(keep))
            if not keep:
                keep=list(range(len(doc)))
            else:
                # Include all pages, even pages that look text-empty (scanned/image pages).
                keep=list(range(len(doc)))
            out=work_dir/(p.stem+'_all_pages.pdf')
            newdoc=fitz.open()
            for i in keep:
                newdoc.insert_pdf(doc,from_page=i,to_page=i)
            newdoc.save(str(out),garbage=4,deflate=True)
            newdoc.close(); doc.close()
            return {'path':str(out),'mime_type':'application/pdf'}, str(out)
        except Exception:
            return item, None

    # Screenshots: downscale oversized images so upload/extraction is quick while preserving text.
    try:
        from PIL import Image
        im=Image.open(p).convert('RGB')
        max_side=1800
        if max(im.size)>max_side:
            ratio=max_side/max(im.size)
            im=im.resize((max(1,int(im.width*ratio)),max(1,int(im.height*ratio))),Image.Resampling.LANCZOS)
        out=work_dir/(p.stem+'_optimized.jpg')
        im.save(out,'JPEG',quality=84,optimize=True)
        return {'path':str(out),'mime_type':'image/jpeg'}, str(out)
    except Exception:
        return item, None


_FLIGHT_CODE_BY_AIRLINE = {
    'indigo':'6E', 'air india':'AI', 'air india express':'IX', 'spicejet':'SG',
    'akasa air':'QP', 'akasa':'QP', 'vistara':'UK', 'alliance air':'9I',
    'star air':'S5', 'go first':'G8', 'gofirst':'G8', 'airasia india':'I5',
}
_FLIGHT_AIRLINE_BY_CODE = {'6E':'IndiGo','AI':'Air India','IX':'Air India Express','SG':'SpiceJet','QP':'Akasa Air','UK':'Vistara','9I':'Alliance Air','S5':'Star Air','G8':'Go First','I5':'AirAsia India'}



_PLACEHOLDER_VALUES = {
    '', '-', '--', '---', 'n/a', 'na', 'nil', 'none', 'null', 'unknown',
    'not specified', 'not available', 'not provided', 'not mentioned', 'not found'
}

def _clean_source_value(value):
    value = str(value or '').strip()
    return '' if value.lower() in _PLACEHOLDER_VALUES else value

def _plain_source_text(file_parts, source_text=''):
    """Collect selectable supplier text without inventing any booking facts."""
    chunks=[]
    if source_text:
        chunks.append(str(source_text))
    for item in file_parts or []:
        try:
            path=Path(item.get('path') or '')
            if path.suffix.lower()=='.pdf' and path.exists():
                import fitz
                doc=fitz.open(str(path))
                chunks.extend((page.get_text('text') or '') for page in doc)
                doc.close()
        except Exception:
            pass
    return '\n'.join(chunks)

def _segment_source_window(raw_text, seg):
    """Find a local text window around this flight number; fall back to all text."""
    if not raw_text:
        return ''
    number=_clean_source_value(seg.get('flight_number'))
    compact=re.sub(r'[^A-Z0-9]', '', number.upper())
    if compact:
        # Match AI-1729, AI 1729, AI1729 etc.
        if len(compact) >= 3:
            code=re.match(r'([A-Z0-9]{2,3})(\d+)', compact)
            if code:
                pat=re.compile(re.escape(code.group(1))+r'\s*[- ]?\s*'+re.escape(code.group(2)), re.I)
                m=pat.search(raw_text)
                if m:
                    return raw_text[max(0,m.start()-350):min(len(raw_text),m.end()+1800)]
    return raw_text

def _terminal_after_anchor(block, anchors, stop_anchors=()):
    low=block.lower()
    positions=[]
    for a in anchors:
        a=_clean_source_value(a)
        if not a: continue
        pos=low.find(a.lower())
        if pos>=0: positions.append((pos, len(a)))
    if not positions:
        return ''
    pos, ln=min(positions, key=lambda x:x[0])
    end=min(len(block), pos+650)
    for s in stop_anchors:
        s=_clean_source_value(s)
        if not s: continue
        sp=low.find(s.lower(), pos+ln)
        if sp>=0: end=min(end, sp)
    chunk=block[pos:end]
    # Explicit terminal wording only; never infer a terminal number.
    m=re.search(r'\b(?:Terminal\s*(?:No\.?\s*)?[A-Z0-9-]+|T\s*[1-9][A-Z]?|[1-9][A-Z]?\s*Terminal)\b', chunk, re.I)
    return re.sub(r'\s+',' ',m.group(0)).strip() if m else ''


def _explicit_booking_date_from_text(raw_text):
    """Return a booking date only when an explicit booking-date label exists."""
    if not raw_text:
        return ''
    patterns = (
        r'(?i)\b(?:booking\s*date|booked\s*(?:on|date)|date\s*of\s*booking|issued\s*(?:on|date)|ticketed\s*(?:on|date))\s*[:\-]?\s*'
        r'([0-3]?\d[\s./-]+(?:[A-Za-z]{3,9}|\d{1,2})[\s./-]+\d{2,4})',
        r'(?i)\b(?:booking\s*date|booked\s*(?:on|date)|date\s*of\s*booking|issued\s*(?:on|date)|ticketed\s*(?:on|date))\s*[:\-]?\s*'
        r'(\d{4}[-/]\d{1,2}[-/]\d{1,2})',
    )
    for pat in patterns:
        m = re.search(pat, raw_text)
        if m:
            return re.sub(r'\s+', ' ', m.group(1)).strip()
    return ''


def _explicit_customer_mobile_from_text(raw_text):
    """Return only a number explicitly labelled as the customer's contact number."""
    if not raw_text:
        return ''
    # Deliberately narrow labels. Generic airline/support/contact-centre numbers
    # must not be treated as the passenger's mobile.
    pat = re.compile(
        r'(?i)\b(?:customer\s*(?:mobile|phone|contact(?:\s*no\.?)?)|'
        r'passenger\s*(?:mobile|phone|contact(?:\s*no\.?)?)|'
        r'guest\s*(?:mobile|phone|contact(?:\s*no\.?)?)|'
        r'mobile\s*(?:no\.?|number)?|contact\s*mobile)\s*[:\-]?\s*'
        r'(\+?\d[\d\s().-]{7,18}\d)'
    )
    m = pat.search(raw_text)
    if not m:
        return ''
    value = re.sub(r'\s+', ' ', m.group(1)).strip()
    # Reject strings that look like short booking/flight identifiers rather than phones.
    digits = re.sub(r'\D', '', value)
    return value if 10 <= len(digits) <= 15 else ''


def _airport_contains_terminal(airport, terminal):
    airport = re.sub(r'\s+', ' ', str(airport or '')).strip().lower()
    terminal = re.sub(r'\s+', ' ', str(terminal or '')).strip().lower()
    if not airport or not terminal:
        return False
    return terminal in airport

def _recover_source_only_fields(data, raw_text):
    """Repair Gemini omissions using literal text already printed by the supplier."""
    if not raw_text:
        return data
    for seg in data.get('segments') or []:
        # Clean model placeholders first; blank means source did not supply it.
        for key in ('flight','flight_number','aircraft','cabin','dep_time','dep_city','dep_code','dep_date','dep_airport','dep_terminal',
                    'arr_time','arr_city','arr_code','arr_date','arr_airport','arr_terminal','duration','stops'):
            seg[key]=_clean_source_value(seg.get(key))

        block=_segment_source_window(raw_text, seg)

        if not seg.get('aircraft'):
            m=re.search(r'\bAircraft\s*[:\-]\s*([^\r\n|]{1,35})', block, re.I)
            if m: seg['aircraft']=_clean_source_value(m.group(1))
        if not seg.get('cabin'):
            m=re.search(r'\bCabin\s*[:\-]\s*([^\r\n|]{1,30})', block, re.I)
            if m: seg['cabin']=_clean_source_value(m.group(1))
        if not seg.get('stops'):
            m=re.search(r'\b(?:Non\s*[- ]?stop|Direct|[1-9]\s+Stops?)\b', block, re.I)
            if m: seg['stops']=re.sub(r'\s+',' ',m.group(0)).strip()
        if not seg.get('duration'):
            # Only labelled duration/travel time/elapsed time is accepted.
            m=re.search(r'\b(?:Duration|Elapsed\s*Time|Travel\s*Time|Flying\s*Time)\s*[:\-]?\s*([^\r\n|]{2,30})', block, re.I)
            if m:
                candidate=_clean_source_value(m.group(1))
                # Reject a neighbouring label accidentally swallowed by the regex.
                if candidate and not re.search(r'\b(?:Stops?|Arrival|Departure|Status|PNR)\b', candidate, re.I):
                    seg['duration']=candidate

        # Terminal fields are SOURCE-VALIDATED, not merely filled when blank.
        # This prevents a departure terminal (for example DEL T2) leaking into the
        # arrival airport when the supplier prints no arrival terminal.
        source_dep_terminal=_terminal_after_anchor(
            block,
            (seg.get('dep_airport'), seg.get('dep_code'), seg.get('dep_city')),
            (seg.get('arr_airport'), seg.get('arr_code'), seg.get('arr_city')),
        )
        source_arr_terminal=_terminal_after_anchor(
            block,
            (seg.get('arr_airport'), seg.get('arr_code'), seg.get('arr_city')),
            ('Confirmed','Airline PNR','Baggage','Status','Comp. Meal','Meal','Non stop','Non-stop'),
        )
        seg['dep_terminal']=source_dep_terminal
        seg['arr_terminal']=source_arr_terminal

    for pax in data.get('passengers') or []:
        for key in ('name','title','ticket_number','type','dob','baggage'):
            pax[key]=_clean_source_value(pax.get(key))
    for key in ('booking_id','booking_date','airline_pnr','gds_pnr','status','mobile'):
        data[key]=_clean_source_value(data.get(key))

    # STRICT TOP-LEVEL SOURCE TRUTH:
    # If selectable supplier text exists, booking_date and mobile survive only
    # when an explicit matching label is present. This prevents travel dates,
    # airline numbers or random numeric strings from leaking into those fields.
    data['booking_date'] = _explicit_booking_date_from_text(raw_text)
    data['mobile'] = _explicit_customer_mobile_from_text(raw_text)

    # Local payment-detail fallback for selectable supplier PDFs. Gemini remains the
    # primary cross-supplier reader, but obvious labelled amount rows should never be
    # lost merely because the model omitted them.
    if not (data.get('payment_items') or []):
        payment_block=raw_text
        anchor=re.search(r'(?i)\b(?:payment\s+details|fare\s+details|fare\s+summary|payment\s+summary)\b', raw_text)
        if anchor:
            payment_block=raw_text[anchor.start():anchor.start()+3000]
        rows=[]; gross=0.0
        for line in payment_block.splitlines():
            clean=re.sub(r'\s+',' ',line).strip()
            if not clean or len(clean)>180:
                continue
            m=re.search(r'(?i)^(.*?)(?:[:\-]?\s*)(?:INR|Rs\.?|₹)\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*$',clean)
            if not m:
                continue
            label=m.group(1).strip(' :-')
            try: amount=float(m.group(2).replace(',',''))
            except Exception: continue
            if not label:
                continue
            if re.search(r'(?i)\b(?:gross\s+total|grand\s+total|total\s+amount|amount\s+payable|net\s+payable|total\s+fare|amount\s+in\s+rs)\b',label):
                gross=max(gross,amount); continue
            if re.search(r'(?i)\b(?:fare|tax|fee|charge|surcharge|yq|yr|gst|k3|seat|meal|baggage|ancillary|insurance|convenience|service)\b',label):
                rows.append({'label':label,'amount':amount})
        if rows:
            data['payment_items']=rows
        if gross>0 and float(data.get('gross_total') or 0)<=0:
            data['gross_total']=gross
    return data

def _normalize_flight_segments(data):
    for seg in data.get('segments') or []:
        airline = str(seg.get('flight') or '').strip()
        number = str(seg.get('flight_number') or '').strip()
        embedded = re.search(r'\b([A-Z0-9]{2,3})[- ]?(\d{1,5})\b', airline, re.I)
        if embedded and (not number or re.fullmatch(r'\d{1,5}', number)):
            number = f"{embedded.group(1).upper()} {embedded.group(2)}"
            airline = re.sub(r'\s*\(?'+re.escape(embedded.group(0))+r'\)?\s*$', '', airline, flags=re.I).strip()
        if re.fullmatch(r'[A-Z0-9]{2,3}', airline, re.I):
            code = airline.upper()
            if re.fullmatch(r'\d{1,5}', number):
                number = f"{code} {number}"
            airline = _FLIGHT_AIRLINE_BY_CODE.get(code, airline)
        elif re.fullmatch(r'\d{1,5}', number):
            code = _FLIGHT_CODE_BY_AIRLINE.get(airline.lower())
            if code:
                number = f"{code} {number}"
        else:
            m = re.fullmatch(r'([A-Z0-9]{2,3})[- ]?(\d{1,5})', number, re.I)
            if m:
                number = f"{m.group(1).upper()} {m.group(2)}"
        seg['flight'] = airline
        seg['flight_number'] = number
        seg['aircraft'] = str(seg.get('aircraft') or '').strip()
    return data


def extract_flight_ticket(file_parts, source_text, api_key, model):
    # Keep a literal selectable-text copy for deterministic source-only recovery after AI extraction.
    raw_source_text=_plain_source_text(file_parts, source_text)
    # Reduce supplier noise before Gemini sees the material. This is the main speed optimisation.
    client=genai.Client(api_key=api_key)
    contents=[PROMPT]
    if source_text:
        # Keep pasted supplier text useful but cap very long legal/marketing dumps.
        txt=str(source_text)
        contents.append('\nSOURCE TEXT (booking-relevant text only; ignore terms/offers/policies):\n'+txt[:18000])
    paths=[]; optimized=[]; original_paths=[]
    work_dir=Path(__file__).resolve().parent/'data'/'tmp_flight_extract'
    try:
        for item in file_parts:
            original_paths.append(Path(item['path']))
            prepared, temp_path=_prepare_flight_source(item,work_dir)
            if temp_path: optimized.append(Path(temp_path))
            p=Path(prepared['path'])
            contents.append(types.Part.from_bytes(data=p.read_bytes(),mime_type=prepared['mime_type']))
            paths.append(p)

        # A short multimodal request: Gemini is explicitly told not to spend tokens on irrelevant pages.
        r=call_with_high_demand_retry(lambda: client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type='application/json', response_schema=SCHEMA, temperature=0,
            )
        ))
        if not r.text: raise RuntimeError('Gemini returned an empty flight ticket response.')
        data=_normalize_flight_segments(json.loads(r.text))

        # Source-only second reading: if useful airport/terminal/duration fields are blank,
        # re-read the ORIGINAL source once. This never blocks printing and never invents data.
        useful_fields=('dep_airport','arr_airport','dep_terminal','arr_terminal','duration','dep_code','arr_code')
        # Re-read not only when segment fields are missing, but also whenever a
        # sensitive top-level field or terminal was extracted. The second pass
        # independently verifies booking_date, customer mobile and terminal ownership.
        needs_detail_reread=(
            any(not str(seg.get(k) or '').strip() for seg in (data.get('segments') or []) for k in useful_fields)
            or any(str(seg.get('dep_terminal') or '').strip() or str(seg.get('arr_terminal') or '').strip() for seg in (data.get('segments') or []))
            or bool(str(data.get('booking_date') or '').strip())
            or bool(str(data.get('mobile') or '').strip())
        )
        if needs_detail_reread:
            try:
                repair_contents=[REPAIR_PROMPT, '\nCURRENT EXTRACTION (UNTRUSTED CANDIDATE — verify every value against source):\n'+json.dumps(data, ensure_ascii=False)]
                if source_text:
                    repair_contents.append('\nSOURCE TEXT:\n'+str(source_text)[:18000])
                for p in paths:
                    if p.exists():
                        mime='application/pdf' if p.suffix.lower()=='.pdf' else 'image/jpeg'
                        repair_contents.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime))
                rr=call_with_high_demand_retry(lambda: client.models.generate_content(
                    model=model, contents=repair_contents,
                    config=types.GenerateContentConfig(
                        response_mime_type='application/json', response_schema=SCHEMA, temperature=0,
                    )
                ))
                if rr.text:
                    repaired=_normalize_flight_segments(json.loads(rr.text))
                    # Preserve first-pass values when the repair pass returns blanks.
                    old_segments=data.get('segments') or []
                    new_segments=repaired.get('segments') or []
                    for i, nseg in enumerate(new_segments):
                        if i < len(old_segments):
                            oseg=old_segments[i]
                            for key, value in oseg.items():
                                # Terminal blanks from the validation pass are deliberate:
                                # never restore an old terminal that the source re-read cleared.
                                if key in ('dep_terminal','arr_terminal'):
                                    continue
                                if not str(nseg.get(key) or '').strip() and str(value or '').strip():
                                    nseg[key]=value
                    for key, value in data.items():
                        if key == 'segments':
                            continue
                        # A blank booking_date/mobile from the validation pass is
                        # deliberate. Never restore the first-pass hallucination.
                        if key in ('booking_date', 'mobile'):
                            continue
                        if not repaired.get(key) and value:
                            repaired[key]=value
                    data=repaired
            except Exception:
                # The second reading is best-effort only; source gaps never block printing.
                pass

        # Deterministic source-only recovery: selectable supplier text wins over AI omissions.
        # This is especially useful for Terminal / Aircraft / Cabin / Stops labels in agency PDFs.
        data=_recover_source_only_fields(data, raw_source_text)

        # Fare recovery: if the main extraction misses a clearly printed supplier
        # total, run a focused multimodal fare pass before declaring the fare absent.
        if (float(data.get('gross_total') or 0) <= 0 or not (data.get('payment_items') or [])) and paths:
            try:
                fare_contents=[FARE_RECOVERY_PROMPT]
                for p in paths:
                    if p.exists():
                        mime='application/pdf' if p.suffix.lower()=='.pdf' else 'image/jpeg'
                        fare_contents.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime))
                fr=call_with_high_demand_retry(lambda: client.models.generate_content(
                    model=model,
                    contents=fare_contents,
                    config=types.GenerateContentConfig(
                        response_mime_type='application/json',
                        response_schema=FARE_SCHEMA,
                        temperature=0,
                    )
                ))
                if fr.text:
                    fd=json.loads(fr.text)
                    gross=float(fd.get('gross_total') or 0)
                    base=float(fd.get('base_fare') or 0)
                    tax=float(fd.get('taxes') or 0)
                    items=[]
                    for row in (fd.get('payment_items') or []):
                        try:
                            amount=float(row.get('amount') or 0)
                        except Exception:
                            amount=0
                        label=str(row.get('label') or '').strip()
                        if label and amount >= 0:
                            items.append({'label':label,'amount':amount})
                    if gross > 0:
                        data['gross_total']=gross
                    if items:
                        data['payment_items']=items
                    if base > 0:
                        data['base_fare']=base
                    if tax > 0:
                        data['taxes']=tax
                    elif gross > 0 and base > 0 and not items:
                        data['taxes']=max(gross-base,0)
            except Exception:
                pass

        # Normalize payment details for downstream printing.
        clean_items=[]
        for row in (data.get('payment_items') or []):
            try: amount=float(row.get('amount') or 0)
            except Exception: amount=0
            label=str(row.get('label') or '').strip()
            if label and amount >= 0:
                clean_items.append({'label':label,'amount':amount})
        data['payment_items']=clean_items
        try: gross=float(data.get('gross_total') or 0)
        except Exception: gross=0
        if gross <= 0 and clean_items:
            gross=sum(float(x.get('amount') or 0) for x in clean_items)
        if gross <= 0:
            gross=float(data.get('base_fare') or 0)+float(data.get('taxes') or 0)
        data['gross_total']=gross
        data['_extraction_pages_optimized']=True
        return data
    finally:
        for p in paths+optimized:
            try:p.unlink(missing_ok=True)
            except:pass
        try:
            if work_dir.exists() and not any(work_dir.iterdir()): work_dir.rmdir()
        except Exception: pass
        for p in original_paths:
            # Original uploads are owned by the bot and can be removed after extraction.
            try:p.unlink(missing_ok=True)
            except:pass
