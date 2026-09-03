import json, re
from pathlib import Path
from google import genai
from google.genai import types

from ai_retry import call_with_high_demand_retry

MYTOURBAZAR_LOGO_URL = "https://share.google/UUxbVDVNxkIgplZio"
SCHEMA={"type":"object","properties":{
 "booking_id":{"type":"string"},"booking_date":{"type":"string"},"airline_pnr":{"type":"string"},"gds_pnr":{"type":"string"},
 "status":{"type":"string"},"mobile":{"type":"string"},"baggage_summary":{"type":"string"},"special_ancillary_summary":{"type":"string"},
 "segments":{"type":"array","items":{"type":"object","properties":{
  "flight":{"type":"string"},"flight_number":{"type":"string"},"aircraft":{"type":"string"},"cabin":{"type":"string"},"fare_type":{"type":"string"},
  "dep_time":{"type":"string"},"dep_city":{"type":"string"},"dep_code":{"type":"string"},"dep_date":{"type":"string"},"dep_airport":{"type":"string"},"dep_terminal":{"type":"string"},
  "arr_time":{"type":"string"},"arr_city":{"type":"string"},"arr_code":{"type":"string"},"arr_date":{"type":"string"},"arr_airport":{"type":"string"},"arr_terminal":{"type":"string"},
  "duration":{"type":"string"},"stops":{"type":"string"},"layover":{"type":"string"}
 },"required":["flight","flight_number","aircraft","cabin","fare_type","dep_time","dep_city","dep_code","dep_date","dep_airport","dep_terminal","arr_time","arr_city","arr_code","arr_date","arr_airport","arr_terminal","duration","stops","layover"]}},
 "passengers":{"type":"array","items":{"type":"object","properties":{"name":{"type":"string"},"title":{"type":"string"},"ticket_number":{"type":"string"},"type":{"type":"string"},"dob":{"type":"string"},"baggage":{"type":"string"},"special_ancillary":{"type":"string"}},"required":["name","title","ticket_number","type","dob","baggage","special_ancillary"]}},
 "base_fare":{"type":"number"},"taxes":{"type":"number"},
 "gross_total":{"type":"number"},
 "payment_items":{"type":"array","items":{"type":"object","properties":{"label":{"type":"string"},"amount":{"type":"number"}},"required":["label","amount"]}}
},"required":["booking_id","booking_date","airline_pnr","gds_pnr","status","mobile","baggage_summary","special_ancillary_summary","segments","passengers","base_fare","taxes","gross_total","payment_items"]}


ENDPOINT_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "flight_number": {"type": "string"},
                    "dep_code": {"type": "string"},
                    "dep_time": {"type": "string"},
                    "dep_endpoint_text": {"type": "string"},
                    "dep_terminal": {"type": "string"},
                    "dep_terminal_evidence": {"type": "string"},
                    "arr_code": {"type": "string"},
                    "arr_time": {"type": "string"},
                    "arr_endpoint_text": {"type": "string"},
                    "arr_terminal": {"type": "string"},
                    "arr_terminal_evidence": {"type": "string"}
                },
                "required": [
                    "flight_number",
                    "dep_code",
                    "dep_time",
                    "dep_endpoint_text",
                    "dep_terminal",
                    "dep_terminal_evidence",
                    "arr_code",
                    "arr_time",
                    "arr_endpoint_text",
                    "arr_terminal",
                    "arr_terminal_evidence"
                ]
            }
        }
    },
    "required": ["segments"]
}

ENDPOINT_ROW_PROMPT = """You are MyTourBazar's AIRPORT ENDPOINT ROW VERIFIER.

Your ONLY job is to read the DEPARTURE and ARRIVAL cells/rows for EVERY flight sector
from the supplier ticket/PDF/screenshot/text.

DO NOT extract fare, passenger, booking or other data.
DO NOT use airport knowledge.
DO NOT infer terminals.

MOST IMPORTANT METHOD:
For EACH flight sector:
1. Identify the exact sector using its printed flight number.
2. Read the DEPARTURE side/cell independently.
3. Read the ARRIVAL side/cell independently.
4. Never carry any word, terminal or airport text from one side to the other.
5. Never carry a terminal from one flight row into the next flight row.

dep_endpoint_text / arr_endpoint_text:
- Copy the COMPLETE airport/location wording printed inside that endpoint's own cell.
- AIRPORT NAME IS IMPORTANT: if a full airport name is visibly printed, you MUST copy it.
  Never collapse `Delhi - Indira Gandhi International Airport Terminal 1` into only
  `DEL Terminal 1`, `Delhi Terminal 1`, or another shortened form.
- Include ALL airport-name continuation lines belonging to that same cell.
- If the terminal is written inside the airport name, include it.
- If the terminal is printed on the NEXT LINE but still inside the same Departure/Arrival
  cell, include it at the end of endpoint_text too.
- Do not include departure/arrival time, date, flight duration, fare type or baggage.
- Keep supplier wording. Do not shorten or rewrite airport names.
- IMPORTANT: a leading city/place word inside the printed airport line is part of
  the source and MUST be preserved. Never remove it because the city/code is also
  printed above the line.
  Example source `Delhi indira gandhi international, delhi` must stay exactly that.
  Example source `Raipur airport, raipur` must stay exactly that.
- Never change either example to `indira gandhi international, delhi` or
  `airport, raipur`.

TERMINAL:
- dep_terminal / arr_terminal = exact terminal only when visibly printed in THAT SAME
  endpoint cell. Examples: `Terminal 2`, `T1`, `Terminal 2A`, `2B`, `Domestic Terminal`.
- If no terminal is visible in that exact endpoint cell, return an empty string.
- dep_terminal_evidence / arr_terminal_evidence = a SHORT VERBATIM text snippet from
  that SAME endpoint cell that proves the terminal. It must contain the terminal wording.
  If terminal is blank, evidence must also be blank.

CRITICAL CONNECTION RULE:
Suppose one flight row shows:
Departure: `Guwahati - Lokpriya Gopinath Bordoloi Terminal 2`
Arrival: `Kolkata - Netaji Subhas Chandra Bose Airport`

and the next flight row shows:
Departure: `Kolkata - Netaji Subhas Chandra Bose Airport`
Arrival: `Raipur - Raipur`

Then:
- GAU departure terminal = Terminal 2
- CCU arrival terminal = blank
- CCU departure terminal = blank
- RPR arrival terminal = blank

The presence of Terminal 2 anywhere else on the page is NOT evidence for those endpoints.

WHEN TERMINAL IS EMBEDDED:
`Delhi - Indira Gandhi International Airport T3`
must be copied as the full endpoint text and terminal=`T3`.

WHEN TERMINAL IS ON A SEPARATE LINE IN SAME CELL:
Airport line: `Mumbai - Chhatrapati Shivaji Maharaj International Airport`
next line in SAME ARRIVAL CELL: `Terminal 2`
then arr_endpoint_text must become:
`Mumbai - Chhatrapati Shivaji Maharaj International Airport Terminal 2`
and arr_terminal=`Terminal 2`.

If layout is ambiguous, leave terminal blank rather than assigning it to the wrong endpoint.

FINAL SELF-CHECK FOR EVERY ENDPOINT:
- If the supplier visibly prints a proper airport name, endpoint_text must contain it.
- A result containing only an IATA code + terminal is incomplete when the airport name
  is visible in that same source cell.
- Do not use world knowledge to fill an airport name that the supplier does not print.

Return ONLY JSON matching the schema.
"""


FOCUSED_ENDPOINT_SCHEMA = {
    "type": "object",
    "properties": {
        "flight_number": {"type": "string"},
        "dep_code": {"type": "string"},
        "dep_time": {"type": "string"},
        "dep_endpoint_text": {"type": "string"},
        "dep_terminal": {"type": "string"},
        "dep_terminal_evidence": {"type": "string"},
        "arr_code": {"type": "string"},
        "arr_time": {"type": "string"},
        "arr_endpoint_text": {"type": "string"},
        "arr_terminal": {"type": "string"},
        "arr_terminal_evidence": {"type": "string"}
    },
    "required": [
        "flight_number","dep_code","dep_time","dep_endpoint_text",
        "dep_terminal","dep_terminal_evidence","arr_code","arr_time",
        "arr_endpoint_text","arr_terminal","arr_terminal_evidence"
    ]
}

FOCUSED_ENDPOINT_PROMPT = """You are MyTourBazar's SINGLE-SECTOR AIR ENDPOINT TRANSCRIBER.

You will be given ONE target flight sector identity plus the supplier ticket/PDF/image.
Your only task is to transcribe the Departure and Arrival endpoint cells belonging to
THAT exact sector. Ignore every other flight row and every other column.

ABSOLUTE RULES:
- SOURCE PRESENT = COPY EXACTLY. SOURCE ABSENT/UNCLEAR = BLANK.
- Do not use airport knowledge. Do not infer airport names or terminals.
- Never copy text from Flight/Aircraft, Duration/Stops, Fare, Cabin, Baggage,
  Passenger, Operated-by, or another sector.
- Never include strings such as `Operated by 6E`, `by 6E`, `2h 10m`, `Non stop`,
  `Family fare`, `Cabin: Economy`, or dates/times inside endpoint_text.
- Preserve all wrapped airport/location lines inside the endpoint cell in source order.
- Preserve leading city/place words exactly. `Raipur airport, raipur` must remain
  `Raipur airport, raipur`; `Delhi indira gandhi international, delhi` must remain whole.
- If the source prints `DEL(INDIRA GANDHI AIRPORT, DELHI)`, copy that complete visible
  endpoint wording; do not truncate it.
- Terminal belongs only to the same endpoint cell. If not visibly printed there, blank.
- If the target sector cannot be isolated confidently, return blank endpoint text rather
  than borrowing from another row.

Return ONLY JSON matching the schema.
"""

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
- ticket_number is NOT a PNR. Copy it only when an actual passenger ticket/e-ticket number is explicitly printed. Never put Airline PNR, GDS PNR, Trip ID or booking reference into ticket_number. If absent, leave blank.
- airline/carrier name and every FULL flight number, including the airline code (example: `6E 405`, not just `405`)
- aircraft type/model when it is explicitly printed by the supplier (example: Airbus A320neo); otherwise return an empty string
- fare_type: copy the exact printed fare family/type such as SAVER/FLEX/SPECIAL. If absent, leave blank.
- baggage_summary: copy the COMPLETE supplier baggage allowance when printed, including BOTH check-in and cabin baggage. Example: `Check-in: 15KG (1 piece), Cabin: 7KG (1 piece)`. Never drop cabin baggage.
- BAGGAGE TYPE RULE: passenger `type` must be canonical `Adult`, `Child`, or `Infant` whenever the source/title/passenger context supports it. Never use the generic word `Passenger` as a passenger type.
- Preserve the supplier baggage source once only. Do not duplicate the same allowance token (for example never turn `15KG` into `15KG 15KG`).
- special_ancillary_summary: copy ONLY explicitly BOOKED/CONFIRMED special ancillaries such as paid seat/seat number, meal, extra baggage beyond normal allowance, priority boarding, wheelchair, lounge, sports equipment or another SSR/service. Normal included check-in/cabin baggage is NOT a special ancillary. If nothing special is booked, leave blank.
- every separate flight sector, including connecting/onward/return sectors
- departure/arrival date, local time, city, and the COMPLETE airport/location wording exactly as printed by the supplier. Never shorten a long airport name. Preserve airport qualifiers such as International, Domestic, Airport, Terminal, Gate-area wording, city/airport combinations and punctuation when they are part of the supplier text. Use dep_airport / arr_airport for the full printed airport text and dep_code / arr_code for a printed 3-letter IATA code.
- terminal is CRITICAL WHEN PRESENT IN THE SOURCE: capture T1/T2/T3, Terminal 1/2/3, 2A/2B, domestic/international terminal labels, etc. in dep_terminal / arr_terminal.
- AIRPORT + TERMINAL SOURCE TRUTH: dep_airport / arr_airport must preserve the COMPLETE endpoint wording exactly as the supplier prints it. If the supplier prints `Guwahati - Lokpriya Gopinath Bordoloi Terminal 2`, keep that full wording in dep_airport AND set dep_terminal=`Terminal 2`. Never shorten the airport name and never move a terminal to the wrong endpoint.
- TERMINAL ENDPOINT LOCK: a terminal belongs ONLY to the exact endpoint where the supplier visibly prints it.
- ROW/CELL OWNERSHIP: always visually inspect the complete Departure and Arrival columns for EACH individual flight row. A terminal can be embedded inside the airport name OR printed on the next line inside that same cell. In either case it belongs only to that endpoint.
- CONNECTION SAFETY EXAMPLE: if GAU departure says `... Terminal 2` but CCU arrival says only `Kolkata - Netaji Subhas Chandra Bose Airport`, then CCU arrival terminal MUST be blank. If the next CCU departure also has no terminal, it stays blank. If RPR arrival has no terminal, it stays blank. Never repeat a previous endpoint terminal into an arrival, connection, next sector or destination.
- cabin and exact elapsed duration ONLY when printed.
- stops are SOURCE-ONLY. Never infer `0`, `0 stops`, `Direct` or `Non-stop` from the route. If supplier does not print it, leave blank.
- layover: copy the exact printed layover/connection duration after the correct preceding sector. Never calculate it from times.
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
- `special_ancillary` must contain only that passenger’s explicitly booked/confirmed ancillary/SSR when the supplier maps it to the passenger (examples: Seat 12A, Veg Meal, Extra Baggage 10KG, Wheelchair). Otherwise leave it blank.
If fare is not printed, return base_fare=0, taxes=0, gross_total=0 and payment_items=[]. Return ONLY JSON matching the supplied schema.'''


SOURCE_TRUTH_PROMPT = """You are MyTourBazar's FINAL AIR SOURCE TRANSCRIBER.

Read ONLY the supplied ticket/PDF/image/text. You are not given any earlier extraction.
SOURCE PRESENT = COPY EXACTLY. SOURCE ABSENT = BLANK.
Never guess, infer, calculate, repeat nearby values, or use airport knowledge.

TOP LEVEL:
- booking_id only if printed.
- booking_date only if explicitly labelled Booking Date / Booked on / Issued on / Ticketed on. Travel date is never booking date.
- airline_pnr / gds_pnr only if explicitly printed with that meaning.
- mobile only if explicitly a customer/passenger contact. Never airline/support number.
- baggage_summary must preserve the complete printed baggage wording, including check-in AND cabin baggage.
- special_ancillary_summary contains only explicitly booked/confirmed ancillary/SSR services. Do not treat normal included baggage as an ancillary.

PASSENGERS:
- Preserve explicit name/title/type/DOB.
- Normalize passenger type to exactly `Adult`, `Child`, or `Infant` when source/title context supports it. Never output generic `Passenger` as the type.
- ticket_number only when an actual passenger ticket/e-ticket number is printed.
- PNR, Trip ID and booking ID are never ticket numbers.
- Preserve complete baggage wording when printed, including both check-in and cabin allowance when present.
- Never duplicate an allowance while transcribing. Source `15KG` stays one `15KG`, not `15KG 15KG`.
- Preserve passenger-specific special_ancillary only when explicitly booked/confirmed and visibly associated with that passenger.

VISUAL ROW RULE:
For each flight number, inspect its Departure cell and Arrival cell separately from top to bottom. Airport names may wrap across multiple lines, and terminal can be embedded in the airport name or appear as a final continuation line. Capture all lines belonging to that one cell before moving to the other endpoint.

EACH FLIGHT SECTOR IS INDEPENDENT:
- exact airline and full flight number
- aircraft only if printed
- cabin only if printed
- fare_type only if printed
- exact departure time/date/city/IATA
- dep_airport = exact complete printed departure endpoint wording
- dep_terminal = terminal only if visibly attached to that departure endpoint
- exact arrival time/date/city/IATA
- arr_airport = exact complete printed arrival endpoint wording
- arr_terminal = terminal only if visibly attached to that arrival endpoint
- duration only if printed
- stops only if printed; never infer 0/Non-stop
- layover only if explicitly printed; bind it to the preceding segment

TERMINAL EXAMPLE:
If the source shows:
GAU departure: `Guwahati - Lokpriya Gopinath Bordoloi Terminal 2`
CCU arrival: `Kolkata - Netaji Subhas Chandra Bose Airport`
CCU departure: `Kolkata - Netaji Subhas Chandra Bose Airport`
RPR arrival: `Raipur - Raipur`
then ONLY GAU gets Terminal 2. CCU arrival, CCU departure and RPR terminal fields are blank,
and their airport strings must not contain Terminal 2.

Never repeat terminal values across sectors.
Never append a terminal just because it appeared elsewhere on the page.
Return ONLY JSON matching the schema.
"""

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
- If a terminal is genuinely visible for an endpoint, put the terminal in BOTH that endpoint's airport text and its terminal field. Example: dep_airport=`Guwahati - Lokpriya Gopinath Bordoloi Terminal 2`, dep_terminal=`Terminal 2`.
- If CURRENT EXTRACTION contains a terminal not visibly attached to that exact endpoint, CLEAR the terminal field. Do not copy it to arrival, connection airport, next-sector departure, or destination.
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
    """Search only the endpoint line and at most two continuation lines."""
    if not block:
        return ''
    lines=[re.sub(r'\s+',' ',x).strip() for x in str(block).splitlines()]
    anchors=[_clean_source_value(a) for a in anchors if _clean_source_value(a)]
    if not anchors:
        return ''
    boundary=re.compile(
        r'(?i)^(?:baggage|fare\s*type|duration|economy|business|premium|'
        r'airline\s*pnr|gds\s*pnr|status|passenger|trip\s*id|booking|'
        r'\d{1,2}:\d{2}|[A-Z0-9]{2,3}\s*[- ]?\s*\d{2,5})\b'
    )
    for i,line in enumerate(lines):
        if not any(a.lower() in line.lower() for a in anchors):
            continue
        pieces=[line]
        for j in range(i+1, min(len(lines), i+3)):
            nxt=lines[j]
            if not nxt:
                continue
            if boundary.search(nxt):
                break
            if any(_clean_source_value(s).lower() in nxt.lower()
                   for s in stop_anchors if _clean_source_value(s)):
                break
            pieces.append(nxt)
        chunk=' '.join(pieces)
        m=re.search(
            r'\b(?:Terminal\s*(?:No\.?\s*)?[A-Z0-9-]+|T\s*[1-9][A-Z]?|[1-9][A-Z]?\s*Terminal)\b',
            chunk,re.I
        )
        if m:
            return re.sub(r'\s+',' ',m.group(0)).strip()
    return ''


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


def _terminal_key(value):
    """Normalize Terminal 2 / T2 / 2A into a comparable source key."""
    s = re.sub(r'\s+', ' ', str(value or '')).strip().lower()
    if not s:
        return ''
    m = re.search(r'\bterminal\s*([a-z]?\d+[a-z]?|domestic|international)\b', s, re.I)
    if m:
        return re.sub(r'\s+', '', m.group(1)).lower()
    m = re.search(r'\bt\s*([1-9]\d*[a-z]?)\b', s, re.I)
    if m:
        return m.group(1).lower()
    m = re.fullmatch(r'\s*([1-9]\d*[a-z])\s*', s, re.I)
    if m:
        return m.group(1).lower()
    if 'domestic terminal' in s or s == 'domestic':
        return 'domestic'
    if 'international terminal' in s or s == 'international':
        return 'international'
    return ''


def _airport_terminal_key(airport):
    s = re.sub(r'\s+', ' ', str(airport or '')).strip().lower()
    if not s:
        return ''
    m = re.search(r'\bterminal\s*([a-z]?\d+[a-z]?|domestic|international)\b', s, re.I)
    if m:
        return re.sub(r'\s+', '', m.group(1)).lower()
    m = re.search(r'\bt\s*([1-9]\d*[a-z]?)\b', s, re.I)
    if m:
        return m.group(1).lower()
    if 'domestic terminal' in s:
        return 'domestic'
    if 'international terminal' in s:
        return 'international'
    return ''


def _airport_contains_terminal(airport, terminal):
    """True only when THIS endpoint's airport text proves THIS terminal."""
    tk = _terminal_key(terminal)
    ak = _airport_terminal_key(airport)
    return bool(tk and ak and tk == ak)


def _append_source_terminal_to_airport(airport, terminal):
    airport = re.sub(r'\s+', ' ', str(airport or '')).strip()
    terminal = re.sub(r'\s+', ' ', str(terminal or '')).strip()
    if not terminal:
        return airport
    if _airport_contains_terminal(airport, terminal):
        return airport
    return (airport + ' ' + terminal).strip() if airport else terminal



def _norm_source_phrase(value):
    return re.sub(r'\s+',' ',str(value or '')).strip().lower()


def _strip_unproven_terminal_from_airport(airport, raw_text):
    """Selectable source text must literally support a terminal-bearing airport phrase."""
    airport=re.sub(r'\s+',' ',str(airport or '')).strip()
    if not airport or not _airport_terminal_key(airport) or not raw_text:
        return airport
    if _norm_source_phrase(airport) in _norm_source_phrase(raw_text):
        return airport
    return re.sub(
        r'(?i)\s*(?:,|-)?\s*(?:Terminal\s*(?:No\.?\s*)?[A-Z0-9-]+|T\s*[1-9][A-Z]?|[1-9][A-Z]?\s*Terminal)\s*$',
        '',airport
    ).strip(' ,-')


def _sanitize_ticket_numbers(data):
    forbidden={
        re.sub(r'\W+','',str(data.get(k) or '')).upper()
        for k in ('booking_id','airline_pnr','gds_pnr')
        if str(data.get(k) or '').strip()
    }
    for pax in data.get('passengers') or []:
        ticket=str(pax.get('ticket_number') or '').strip()
        if ticket and re.sub(r'\W+','',ticket).upper() in forbidden:
            pax['ticket_number']=''
    return data


def _sanitize_inferred_stops(data):
    for seg in data.get('segments') or []:
        stops=str(seg.get('stops') or '').strip()
        if re.fullmatch(r'0(?:\s*stops?)?', stops, re.I):
            seg['stops']=''
    return data



def _canonical_passenger_type(pax):
    """Return only Adult / Child / Infant when supported by passenger context.

    This is deliberately conservative. Generic `Passenger` is never retained.
    """
    pax=pax or {}
    raw=" ".join(str(pax.get(k) or "") for k in ("type","title","name","baggage")).strip().lower()

    if re.search(r"\b(?:infant|inf|baby)\b", raw):
        return "Infant"
    if re.search(r"\b(?:child|chd|cnn)\b", raw):
        return "Child"
    if re.search(r"\b(?:adult|adt)\b", raw):
        return "Adult"

    title=str(pax.get("title") or "").strip().lower().replace(".","")
    if title in ("master","mstr"):
        return "Child"
    if title in ("mr","mrs","ms","dr","prof"):
        return "Adult"
    if title=="infant":
        return "Infant"
    if title=="child":
        return "Child"

    # Unknown is safer than a wrong category.
    return ""


def _normalize_passenger_types(data):
    for pax in (data or {}).get("passengers") or []:
        canonical=_canonical_passenger_type(pax)
        pax["type"]=canonical
    return data


def _apply_baggage_summary(data):
    summary=re.sub(r'\s+',' ',str(data.get('baggage_summary') or '')).strip()
    if not summary:
        return data
    for pax in data.get('passengers') or []:
        current=re.sub(r'\s+',' ',str(pax.get('baggage') or '')).strip()
        if (not current) or (re.search(r'(?i)\bcabin\b',summary) and not re.search(r'(?i)\bcabin\b',current)):
            pax['baggage']=summary
    return data


def _norm_flight_key(value):
    return re.sub(r'[^A-Z0-9]', '', str(value or '').upper())


def _norm_time_key(value):
    raw = str(value or '').strip().lower()
    raw = re.sub(r'\bhrs?\b', '', raw)
    raw = re.sub(r'\s+', '', raw)
    return raw


def _norm_code_key(value):
    return re.sub(r'[^A-Z]', '', str(value or '').upper())[:3]



def _endpoint_airport_core(value, city='', code=''):
    """Remove endpoint city/code/terminal wrappers only for richness comparison."""
    text=re.sub(r'\s+',' ',str(value or '')).strip()
    if not text:
        return ''
    # Remove leading city and IATA wrappers, but never invent anything.
    for prefix in (city, code):
        prefix=re.sub(r'\s+',' ',str(prefix or '')).strip()
        if prefix:
            text=re.sub(r'(?i)^\s*'+re.escape(prefix)+r'\s*(?:\([A-Z]{3}\))?\s*[-,:|]*\s*','',text,count=1)
    if code:
        text=re.sub(r'(?i)^\s*\(?'+re.escape(str(code).strip())+r'\)?\s*[-,:|]*\s*','',text,count=1)
    # Remove a terminal token for the purpose of deciding whether a real airport name remains.
    text=re.sub(
        r'(?i)\b(?:Terminal\s*(?:No\.?\s*)?[A-Za-z0-9-]+|'
        r'T\s*[1-9]\d*[A-Za-z]?|[1-9][A-Za-z]?\s*Terminal|'
        r'(?:Domestic|International)\s+Terminal)\b',
        ' ',
        text,
    )
    text=re.sub(r'\s+',' ',text).strip(' -,:|()')
    return text


def _endpoint_has_substantive_airport_name(value, city='', code=''):
    core=_endpoint_airport_core(value,city,code)
    if not core:
        return False
    if city and core.lower()==str(city).strip().lower():
        return False
    if code and core.upper()==str(code).strip().upper():
        return False
    words=re.findall(r"[A-Za-z][A-Za-z.'-]*",core)
    # Airport/Aerodrome wording is strong evidence. For suppliers that omit the word
    # "Airport", accept a proper multi-word printed name such as Lokpriya Gopinath Bordoloi.
    if re.search(r'(?i)\b(?:airport|aerodrome|airfield)\b',core):
        return True
    return len(words) >= 3


def _airport_name_richness(value, city='', code=''):
    core=_endpoint_airport_core(value,city,code)
    if not core:
        return 0
    words=re.findall(r"[A-Za-z][A-Za-z.'-]*",core)
    score=len(words)
    if re.search(r'(?i)\b(?:airport|aerodrome|airfield)\b',core):
        score += 8
    if re.search(r'(?i)\b(?:international|domestic)\b',core):
        score += 3
    return score


def _pdf_layout_lines(pdf_path):
    """Extract selectable PDF text spans with exact source coordinates."""
    spans=[]
    try:
        import fitz
        doc=fitz.open(str(pdf_path))
        for page_no,page in enumerate(doc):
            payload=page.get_text('dict') or {}
            page_rect=page.rect
            for block in payload.get('blocks') or []:
                if block.get('type',0) != 0:
                    continue
                for line in block.get('lines') or []:
                    for span in line.get('spans') or []:
                        raw=str(span.get('text') or '')
                        value=re.sub(r'\s+',' ',raw).strip()
                        if not value:
                            continue
                        bbox=span.get('bbox') or line.get('bbox') or (0,0,0,0)
                        spans.append({
                            'page':page_no,
                            'page_width':float(page_rect.width),
                            'page_height':float(page_rect.height),
                            'x0':float(bbox[0]),'y0':float(bbox[1]),
                            'x1':float(bbox[2]),'y1':float(bbox[3]),
                            'text':value,
                        })
        doc.close()
    except Exception:
        return []
    return spans


def _line_center(line):
    return ((float(line['x0'])+float(line['x1']))/2.0,
            (float(line['y0'])+float(line['y1']))/2.0)


def _line_contains_iata(line, code):
    code=re.sub(r'[^A-Z]','',str(code or '').upper())[:3]
    if not code:
        return False
    return bool(re.search(r'(?<![A-Z])'+re.escape(code)+r'(?![A-Z])',line.get('text',''),re.I))


def _compact_alnum(value):
    return re.sub(r'[^A-Z0-9]','',str(value or '').upper())


def _normalized_clock(value):
    raw=str(value or '').upper()
    raw=re.sub(r'\bHRS?\b','',raw)
    raw=re.sub(r'\s+','',raw)
    return raw


def _span_matches_time(span, value):
    target=_normalized_clock(value)
    if not target:
        return False
    return target in _normalized_clock(span.get('text','')) or _normalized_clock(span.get('text','')) in target


def _span_matches_flight(span, value):
    target=_compact_alnum(value)
    if not target:
        return False
    current=_compact_alnum(span.get('text',''))
    if not current:
        return False
    # Require the numeric flight-number portion; carrier code alone (e.g. `6E`)
    # is too common elsewhere on supplier tickets and must not anchor a sector row.
    nums=re.findall(r'\d{2,5}',target)
    flight_digits=nums[-1] if nums else ''
    if flight_digits and flight_digits not in current:
        return False
    return target in current or current in target or bool(flight_digits and flight_digits in current)


def _bad_airport_candidate_text(text):
    text=re.sub(r'\s+',' ',str(text or '')).strip()
    if not text:
        return True
    if re.fullmatch(r'(?i)(?:terminal\s*\w+|t\s*\d+\w?|[A-Z]{3}|[A-Z]{3}\s+terminal\s*\w+)',text):
        return True
    if re.search(r'(?i)\b(?:operated\s+by|flight\s*&?\s*aircraft|aircraft|fare\s*type|family\s*fare|'
                 r'cabin|economy|business|duration|stops?|non\s*[- ]?stop|direct|baggage|passenger|'
                 r'pnr|booking|trip\s*id|status|meal|flight\s*no)\b',text):
        return True
    # Airline/operator contamination like "by 6E" or "operated by UK".
    if re.search(r'(?i)(?:^|\s)by\s+[A-Z0-9]{2,3}(?:\s|$)',text):
        return True
    # Duration fragments anywhere inside candidate text.
    if re.search(r'(?i)(?:^|\s)\d{1,2}\s*(?:h|hr|hrs|hour|hours)\b'
                 r'(?:\s*\d{0,2}\s*(?:m|min|mins|minute|minutes)\b)?',text):
        return True
    if re.fullmatch(r'(?i)\s*\d{1,3}\s*(?:m|min|mins|minute|minutes)\s*',text):
        return True
    if re.search(r'\b\d{1,2}:\d{2}\b',text):
        return True
    if re.search(r'(?i)\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b',text) and re.search(r'\b20\d{2}\b',text):
        return True
    return False


def _endpoint_is_suspicious(text):
    """Final reject gate for contaminated endpoint text."""
    value=re.sub(r'\s+',' ',str(text or '')).strip()
    if not value:
        return True
    if _bad_airport_candidate_text(value):
        return True
    # Reject obvious orphan punctuation/truncation endings.
    if value.endswith(('(', '[', '{', ':')):
        return True
    # A bare code/city/terminal is not a usable airport/location description.
    if re.fullmatch(r'(?i)[A-Z]{3}(?:\s*[-,:()]\s*)?',value):
        return True
    return False


def _separate_endpoint_code_prefix(value, code=''):
    """Separate a printed IATA wrapper from the airport/location field.

    This is structural field separation, not semantic rewriting. The raw source can be
    retained separately, while the airport field avoids duplicating the IATA already
    printed in the city/code line.

    Examples:
      DEL(INDIRA GANDHI AIRPORT, DELHI) -> INDIRA GANDHI AIRPORT, DELHI
      DEL - Indira Gandhi International Airport -> Indira Gandhi International Airport
      Delhi indira gandhi international, delhi -> unchanged
    """
    raw=re.sub(r'\s+',' ',str(value or '')).strip()
    code=re.sub(r'[^A-Z]','',str(code or '').upper())[:3]
    if not raw or not code:
        return raw
    # Complete CODE(...) wrapper: keep everything inside exactly, plus any terminal tail.
    m=re.match(r'^\s*'+re.escape(code)+r'\s*\((.*)\)\s*(.*)$',raw,re.I)
    if m:
        inside=m.group(1).strip(); tail=m.group(2).strip()
        return re.sub(r'\s+',' ',(inside+' '+tail).strip())
    # Simple leading CODE delimiter. Remove only the exact IATA token + punctuation.
    m=re.match(r'^\s*'+re.escape(code)+r'\s*[-–—:|]\s*(.+)$',raw,re.I)
    if m:
        return re.sub(r'\s+',' ',m.group(1)).strip()
    return raw


def _airport_candidate_score(text, city='', code=''):
    if _bad_airport_candidate_text(text):
        return -999
    core=_endpoint_airport_core(text,city,code)
    if not core:
        return -999
    words=re.findall(r"[A-Za-z][A-Za-z.'-]*",core)
    score=len(words)
    if re.search(r'(?i)\b(?:airport|aerodrome|airfield)\b',core):
        score += 16
    if re.search(r'(?i)\b(?:international|domestic)\b',core):
        score += 5
    if re.search(r'(?i)\bterminal\b|\bT\s*\d+\b',text):
        score += 2
    if len(words) < 2 and not re.search(r'(?i)\bairport\b',core):
        score -= 8
    return score


def _group_spans_in_zone(spans, page, x0, x1, y0, y1, y_tol=3.4):
    """Group only spans inside one endpoint column; cross-column text can never join."""
    selected=[]
    for sp in spans:
        if sp['page'] != page:
            continue
        cx,cy=_line_center(sp)
        if x0 <= cx <= x1 and y0 <= cy <= y1:
            selected.append(sp)
    selected=sorted(selected,key=lambda s:(s['y0'],s['x0']))
    groups=[]
    for sp in selected:
        cy=_line_center(sp)[1]
        target=None
        for g in reversed(groups[-3:]):
            if abs(g['cy']-cy) <= y_tol:
                target=g; break
        if target is None:
            target={'cy':cy,'spans':[]}
            groups.append(target)
        target['spans'].append(sp)
        target['cy']=sum(_line_center(x)[1] for x in target['spans'])/len(target['spans'])
    lines=[]
    for g in groups:
        ss=sorted(g['spans'],key=lambda s:s['x0'])
        value=' '.join(s['text'] for s in ss)
        value=re.sub(r'\s+',' ',value).strip()
        if not value: continue
        lines.append({
            'page':page,'x0':min(s['x0'] for s in ss),'x1':max(s['x1'] for s in ss),
            'y0':min(s['y0'] for s in ss),'y1':max(s['y1'] for s in ss),
            'text':value,'cy':g['cy'],
        })
    return sorted(lines,key=lambda x:(x['cy'],x['x0']))


def _nearby_time_score(spans, anchor, expected_time, x0, x1):
    if not expected_time:
        return 0
    ay=_line_center(anchor)[1]
    for sp in spans:
        if sp['page'] != anchor['page']:
            continue
        cx,cy=_line_center(sp)
        if x0 <= cx <= x1 and abs(cy-ay) <= 58 and _span_matches_time(sp,expected_time):
            return 10
    return 0


def _nearby_flight_score(spans, page, row_y, flight_number, dep_x):
    if not flight_number:
        return 0
    for sp in spans:
        if sp['page'] != page:
            continue
        cx,cy=_line_center(sp)
        if cx >= dep_x or abs(cy-row_y) > 85:
            continue
        if _span_matches_flight(sp,flight_number):
            return 14
    return 0


def _best_segment_geometry_pair(spans, seg):
    dep_code=str(seg.get('dep_code') or '').strip().upper()
    arr_code=str(seg.get('arr_code') or '').strip().upper()
    if not dep_code or not arr_code:
        return None
    dep_anchors=[sp for sp in spans if _line_contains_iata(sp,dep_code)]
    arr_anchors=[sp for sp in spans if _line_contains_iata(sp,arr_code)]
    candidates=[]
    for dl in dep_anchors:
        dx,dy=_line_center(dl)
        for al in arr_anchors:
            if al['page'] != dl['page']:
                continue
            ax,ay=_line_center(al)
            if dx >= ax or abs(dy-ay) > 72:
                continue
            sep=ax-dx
            if sep < 120:
                continue
            # Endpoint column bounds for validation scoring.
            dep_left=dx-sep*0.32; dep_right=dx+sep*0.40
            arr_left=ax-sep*0.40; arr_right=ax+sep*0.32
            score=100-abs(dy-ay)
            score += _nearby_time_score(spans,dl,seg.get('dep_time'),dep_left,dep_right)
            score += _nearby_time_score(spans,al,seg.get('arr_time'),arr_left,arr_right)
            score += _nearby_flight_score(spans,dl['page'],(dy+ay)/2.0,seg.get('flight_number'),dx)
            candidates.append((score,dl,al))
    if not candidates:
        return None
    candidates.sort(key=lambda x:x[0],reverse=True)
    score,dl,al=candidates[0]
    return {'score':score,'dep':dl,'arr':al,'row_y':(_line_center(dl)[1]+_line_center(al)[1])/2.0}


def _airport_block_from_zone(spans, page, x0, x1, top, bottom, city='', code='', anchor_y=0):
    lines=_group_spans_in_zone(spans,page,x0,x1,top,bottom)
    if not lines:
        return ''

    eligible=[]
    for ln in lines:
        txt=ln['text']
        # Pure endpoint header line such as "Delhi (DEL)" or "RPR" is not airport body.
        stripped=re.sub(r'(?i)\b'+re.escape(str(code or ''))+r'\b',' ',txt) if code else txt
        if city:
            stripped=re.sub(r'(?i)^\s*'+re.escape(str(city).strip())+r'\s*[-,:()]*\s*$','',stripped)
        stripped=re.sub(r'[()\-,:|\s]+',' ',stripped).strip()
        if not stripped:
            continue
        terminal_only=bool(_extract_terminal_from_endpoint_text(txt))
        if _bad_airport_candidate_text(txt) and not terminal_only:
            continue
        score=_airport_candidate_score(txt,city,code)
        terminal_only=terminal_only and score < 2
        # Airport body is normally below the code/time/date block. Allow a small tolerance.
        if ln['cy'] < anchor_y-5:
            continue
        if score >= 2 or terminal_only:
            eligible.append((ln,score,terminal_only))

    if not eligible:
        return ''

    # Build contiguous source blocks; wrapped airport lines must remain together.
    blocks=[]
    current=[]
    last=None
    for item in eligible:
        ln=item[0]
        if last is None or (ln['y0']-last['y1']) <= 13.5:
            current.append(item)
        else:
            if current: blocks.append(current)
            current=[item]
        last=ln
    if current: blocks.append(current)

    scored=[]
    for block in blocks:
        value=' '.join(x[0]['text'] for x in block)
        value=re.sub(r'\s+',' ',value).strip()
        if _endpoint_is_suspicious(value):
            continue
        total=sum(max(0,x[1]) for x in block)
        if re.search(r'(?i)\b(?:airport|aerodrome|airfield)\b',value): total += 10
        if re.search(r'(?i)\b(?:international|domestic)\b',value): total += 4
        # Prefer blocks close to row anchor but not the code/date line itself.
        first_y=block[0][0]['cy']
        total += max(0,8-int(abs(first_y-anchor_y)/15))
        scored.append((total,value))
    if not scored:
        return ''
    scored.sort(key=lambda x:x[0],reverse=True)
    best=scored[0][1]
    return best if _endpoint_has_substantive_airport_name(best,city,code) else ''


def _recover_airport_names_from_pdf_geometry(data, original_paths):
    """V190 deterministic sector/column endpoint engine for selectable PDFs.

    Key difference from V184-V189: first lock ONE visual sector row, then split it into
    independent Departure and Arrival columns. Cross-column text is never eligible.
    """
    data=data or {}
    all_spans=[]
    for path in original_paths or []:
        try:
            path=Path(path)
            if path.suffix.lower()=='.pdf' and path.exists():
                all_spans.extend(_pdf_layout_lines(path))
        except Exception:
            continue
    if not all_spans:
        return data

    matches=[]
    for idx,seg in enumerate(data.get('segments') or []):
        match=_best_segment_geometry_pair(all_spans,seg)
        if match:
            match['idx']=idx
            matches.append(match)

    # Determine a safe vertical band for each matched row. Midpoint to next matched row
    # prevents one connecting sector from borrowing the next sector's airport text.
    by_page={}
    for m in matches:
        by_page.setdefault(m['dep']['page'],[]).append(m)
    for page,items in by_page.items():
        items.sort(key=lambda m:m['row_y'])
        for i,m in enumerate(items):
            prev_y=items[i-1]['row_y'] if i>0 else None
            next_y=items[i+1]['row_y'] if i+1<len(items) else None
            top=max(m['row_y']-12, ((prev_y+m['row_y'])/2.0) if prev_y is not None else m['row_y']-12)
            default_bottom=m['row_y']+125
            bottom=min(default_bottom, ((m['row_y']+next_y)/2.0)-2 if next_y is not None else default_bottom)
            if bottom <= top+28:
                bottom=top+90
            m['top']=top; m['bottom']=bottom

    segments=data.get('segments') or []
    for m in matches:
        seg=segments[m['idx']]
        dl,al=m['dep'],m['arr']
        dx,dy=_line_center(dl); ax,ay=_line_center(al)
        sep=max(120.0,ax-dx)
        page_width=max(float(dl.get('page_width') or 0),float(al.get('page_width') or 0),ax+sep*0.35)

        # If a flight-number span is visible, use its right edge to hard-stop the left
        # boundary of Departure. This permanently blocks "Operated by 6E" leakage.
        flight_right=None
        for sp in all_spans:
            if sp['page'] != dl['page']:
                continue
            cx,cy=_line_center(sp)
            if cx < dx and abs(cy-m['row_y']) <= 85 and _span_matches_flight(sp,seg.get('flight_number')):
                flight_right=max(flight_right or sp['x1'],sp['x1'])
        dep_left=((flight_right+dx)/2.0) if flight_right is not None else dx-sep*0.30
        dep_right=dx+sep*0.38
        arr_left=ax-sep*0.38
        arr_right=min(page_width,ax+sep*0.34)

        geo_dep=_airport_block_from_zone(
            all_spans,dl['page'],dep_left,dep_right,m['top'],m['bottom'],
            str(seg.get('dep_city') or ''),str(seg.get('dep_code') or ''),dy,
        )
        geo_arr=_airport_block_from_zone(
            all_spans,al['page'],arr_left,arr_right,m['top'],m['bottom'],
            str(seg.get('arr_city') or ''),str(seg.get('arr_code') or ''),ay,
        )

        if geo_dep and not _endpoint_is_suspicious(geo_dep):
            dep_field=_separate_endpoint_code_prefix(geo_dep,seg.get('dep_code'))
            seg['dep_endpoint_source_raw']=geo_dep
            seg['dep_airport']=dep_field
            seg['dep_airport_source_exact']=dep_field
            seg['dep_airport_source_locked']=True
            embedded=_extract_terminal_from_endpoint_text(dep_field)
            if embedded: seg['dep_terminal']=embedded
        if geo_arr and not _endpoint_is_suspicious(geo_arr):
            arr_field=_separate_endpoint_code_prefix(geo_arr,seg.get('arr_code'))
            seg['arr_endpoint_source_raw']=geo_arr
            seg['arr_airport']=arr_field
            seg['arr_airport_source_exact']=arr_field
            seg['arr_airport_source_locked']=True
            embedded=_extract_terminal_from_endpoint_text(arr_field)
            if embedded: seg['arr_terminal']=embedded

    return data

def _extract_terminal_from_endpoint_text(value):
    """Derive terminal only from this endpoint's own copied text."""
    text = re.sub(r'\s+', ' ', str(value or '')).strip()
    if not text:
        return ''
    patterns = (
        r'\bTerminal\s*(?:No\.?\s*)?([A-Za-z]?\d+[A-Za-z]?|Domestic|International)\b',
        r'\b(T\s*[1-9]\d*[A-Za-z]?)\b',
        r'\b([1-9][A-Za-z]?)\s*Terminal\b',
        r'\b(Domestic|International)\s+Terminal\b',
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        found = m.group(0)
        found = re.sub(r'\s+', ' ', found).strip()
        if re.match(r'(?i)^t\s*\d', found):
            found = re.sub(r'\s+', '', found).upper()
        return found
    return ''


def _terminal_evidence_is_valid(terminal, evidence):
    """Dedicated row pass must provide same-cell evidence for a loose terminal."""
    terminal = re.sub(r'\s+', ' ', str(terminal or '')).strip()
    evidence = re.sub(r'\s+', ' ', str(evidence or '')).strip()
    if not terminal or not evidence:
        return False

    tk = _terminal_key(terminal)
    ek = _airport_terminal_key(evidence) or _terminal_key(evidence)
    return bool(tk and ek and tk == ek)


def _compose_verified_endpoint(endpoint_text, terminal, evidence):
    """Build display text using ONLY one verified endpoint row/cell.

    Prefer terminal already embedded in endpoint_text. If the terminal is printed on a
    separate line in the same cell, the dedicated verifier supplies same-cell evidence;
    only then is it appended.
    """
    endpoint = re.sub(r'\s+', ' ', str(endpoint_text or '')).strip()
    terminal = re.sub(r'\s+', ' ', str(terminal or '')).strip()

    embedded = _extract_terminal_from_endpoint_text(endpoint)
    if embedded:
        return endpoint, embedded

    if terminal and _terminal_evidence_is_valid(terminal, evidence):
        # Append only after evidence from the SAME departure/arrival cell.
        return (endpoint + ' ' + terminal).strip(), terminal

    return endpoint, ''


def _endpoint_row_match_score(seg, row):
    """Match verifier row to extracted segment without relying on array order."""
    score = 0
    sf = _norm_flight_key(seg.get('flight_number'))
    rf = _norm_flight_key(row.get('flight_number'))
    if sf and rf:
        if sf == rf:
            score += 12
        else:
            return -999

    pairs = (
        ('dep_code', _norm_code_key, 4),
        ('arr_code', _norm_code_key, 4),
        ('dep_time', _norm_time_key, 3),
        ('arr_time', _norm_time_key, 3),
    )
    for key, fn, weight in pairs:
        a = fn(seg.get(key))
        b = fn(row.get(key))
        if a and b:
            if a == b:
                score += weight
            else:
                score -= weight
    return score


def _apply_verified_endpoint_rows(data, verified):
    """Final endpoint authority: row-by-row departure/arrival verification wins."""
    rows = (verified or {}).get('segments') or []
    used = set()

    for seg in data.get('segments') or []:
        best_i = None
        best_score = -999
        for i, row in enumerate(rows):
            if i in used:
                continue
            score = _endpoint_row_match_score(seg, row)
            if score > best_score:
                best_i, best_score = i, score

        # Require strong identity: usually flight number + at least one time/code.
        if best_i is None or best_score < 12:
            continue

        row = rows[best_i]
        used.add(best_i)

        dep_text, dep_terminal = _compose_verified_endpoint(
            row.get('dep_endpoint_text'),
            row.get('dep_terminal'),
            row.get('dep_terminal_evidence'),
        )
        arr_text, arr_terminal = _compose_verified_endpoint(
            row.get('arr_endpoint_text'),
            row.get('arr_terminal'),
            row.get('arr_terminal_evidence'),
        )

        # A non-empty endpoint copied from its exact row is authoritative.
        # Keep an explicit source-exact copy so the renderer cannot later shorten it.
        if dep_text:
            dep_field=_separate_endpoint_code_prefix(dep_text,seg.get('dep_code'))
            seg['dep_endpoint_source_raw'] = dep_text
            seg['dep_airport'] = dep_field
            seg['dep_airport_source_exact'] = dep_field
            seg['dep_terminal'] = dep_terminal
        if arr_text:
            arr_field=_separate_endpoint_code_prefix(arr_text,seg.get('arr_code'))
            seg['arr_endpoint_source_raw'] = arr_text
            seg['arr_airport'] = arr_field
            seg['arr_airport_source_exact'] = arr_field
            seg['arr_terminal'] = arr_terminal

    return data


def _verify_endpoint_rows(client, model, original_paths, source_text=''):
    """One focused multimodal pass that reads each Departure/Arrival row independently."""
    contents = [ENDPOINT_ROW_PROMPT]
    if source_text:
        contents.append(
            '\nSOURCE TEXT (use only as supporting evidence; preserve visual row ownership):\n'
            + str(source_text)[:18000]
        )

    added = 0
    for p in original_paths or []:
        try:
            p = Path(p)
            if not p.exists():
                continue
            suffix = p.suffix.lower()
            if suffix == '.pdf':
                mime = 'application/pdf'
            elif suffix in ('.png',):
                mime = 'image/png'
            elif suffix in ('.webp',):
                mime = 'image/webp'
            else:
                mime = 'image/jpeg'
            contents.append(types.Part.from_bytes(data=p.read_bytes(), mime_type=mime))
            added += 1
        except Exception:
            continue

    if not added and not source_text:
        return {'segments': []}

    rr = call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_mime_type='application/json',
            response_schema=ENDPOINT_ROW_SCHEMA,
            temperature=0,
        )
    ))
    if not rr.text:
        return {'segments': []}
    return json.loads(rr.text)


def _focused_endpoint_verify(client, model, original_paths, seg, source_text=''):
    """One sector per model call: prevents connection-row cross-contamination."""
    identity=(
        f"TARGET SECTOR ONLY:\n"
        f"Flight: {seg.get('flight_number','')}\n"
        f"Departure: {seg.get('dep_code','')} {seg.get('dep_time','')}\n"
        f"Arrival: {seg.get('arr_code','')} {seg.get('arr_time','')}\n"
    )
    contents=[FOCUSED_ENDPOINT_PROMPT,identity]
    if source_text:
        contents.append('\nSOURCE TEXT (supporting only; visual row ownership wins):\n'+str(source_text)[:18000])
    added=0
    for p in original_paths or []:
        try:
            p=Path(p)
            if not p.exists(): continue
            suffix=p.suffix.lower()
            mime='application/pdf' if suffix=='.pdf' else ('image/png' if suffix=='.png' else ('image/webp' if suffix=='.webp' else 'image/jpeg'))
            contents.append(types.Part.from_bytes(data=p.read_bytes(),mime_type=mime)); added+=1
        except Exception:
            continue
    if not added and not source_text:
        return {}
    rr=call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,contents=contents,
        config=types.GenerateContentConfig(response_mime_type='application/json',response_schema=FOCUSED_ENDPOINT_SCHEMA,temperature=0)
    ))
    return json.loads(rr.text) if rr.text else {}


def _apply_focused_endpoint(seg,row):
    if not row: return seg
    dep_text,dep_terminal=_compose_verified_endpoint(row.get('dep_endpoint_text'),row.get('dep_terminal'),row.get('dep_terminal_evidence'))
    arr_text,arr_terminal=_compose_verified_endpoint(row.get('arr_endpoint_text'),row.get('arr_terminal'),row.get('arr_terminal_evidence'))
    # Never replace a deterministic selectable-PDF lock with AI text.
    if not seg.get('dep_airport_source_locked') and dep_text and not _endpoint_is_suspicious(dep_text):
        dep_field=_separate_endpoint_code_prefix(dep_text,seg.get('dep_code'))
        seg['dep_endpoint_source_raw']=dep_text; seg['dep_airport']=dep_field; seg['dep_airport_source_exact']=dep_field; seg['dep_terminal']=dep_terminal
    if not seg.get('arr_airport_source_locked') and arr_text and not _endpoint_is_suspicious(arr_text):
        arr_field=_separate_endpoint_code_prefix(arr_text,seg.get('arr_code'))
        seg['arr_endpoint_source_raw']=arr_text; seg['arr_airport']=arr_field; seg['arr_airport_source_exact']=arr_field; seg['arr_terminal']=arr_terminal
    return seg


def _endpoint_needs_focused_verify(seg,key):
    exact=str(seg.get(key+'_airport_source_exact') or '').strip()
    current=str(seg.get(key+'_airport') or '').strip()
    return (not exact) or _endpoint_is_suspicious(exact) or _endpoint_is_suspicious(current)


def _final_endpoint_safety_gate(data):
    """Never print contaminated source-critical airport text."""
    for seg in data.get('segments') or []:
        for prefix in ('dep','arr'):
            exact_key=prefix+'_airport_source_exact'; airport_key=prefix+'_airport'
            exact=re.sub(r'\s+',' ',str(seg.get(exact_key) or '')).strip()
            airport=re.sub(r'\s+',' ',str(seg.get(airport_key) or '')).strip()
            if exact and _endpoint_is_suspicious(exact):
                seg.pop(exact_key,None); exact=''
            chosen=exact or airport
            if chosen and _endpoint_is_suspicious(chosen):
                # Blank is safer than wrong on an air ticket.
                seg[airport_key]=''
            elif chosen:
                seg[airport_key]=chosen
    return data


def _apply_special_ancillary_summary(data):
    """Recover only explicit ancillary charge/service labels; standard baggage is excluded."""
    current = re.sub(r'\s+', ' ', str(data.get('special_ancillary_summary') or '')).strip()
    labels = []
    for item in data.get('payment_items') or []:
        label = re.sub(r'\s+', ' ', str((item or {}).get('label') or '')).strip()
        if not label:
            continue
        if re.search(
            r'(?i)\b(?:seat(?:\s+selection)?|meal|priority(?:\s+boarding)?|wheelchair|'
            r'lounge|sports?\s+equipment|special\s+service|SSR|extra\s+baggage|'
            r'excess\s+baggage|additional\s+baggage)\b',
            label,
        ):
            # Generic normal "Baggage" is not automatically special; only extra/excess/additional.
            if label.lower() not in {x.lower() for x in labels}:
                labels.append(label)
    if not current and labels:
        data['special_ancillary_summary'] = ', '.join(labels)
    return data


def _enforce_terminal_endpoint_truth(data):
    """Final terminal safety gate.

    A terminal survives ONLY if the same departure/arrival endpoint's own
    verified row/cell supports it. The dedicated row verifier is applied after
    this gate and is the final authority.
    """
    for seg in data.get('segments') or []:
        for airport_key, terminal_key in (
            ('dep_airport', 'dep_terminal'),
            ('arr_airport', 'arr_terminal'),
        ):
            airport = re.sub(r'\s+', ' ', str(seg.get(airport_key) or '')).strip()
            terminal = re.sub(r'\s+', ' ', str(seg.get(terminal_key) or '')).strip()
            seg[airport_key] = airport
            seg[terminal_key] = terminal

            ak = _airport_terminal_key(airport)
            tk = _terminal_key(terminal)

            # Airport wording itself is the authoritative endpoint evidence.
            if ak and not tk:
                m = re.search(r'\bTerminal\s*([A-Za-z]?\d+[A-Za-z]?|Domestic|International)\b', airport, re.I)
                if m:
                    seg[terminal_key] = 'Terminal ' + m.group(1)
                else:
                    m = re.search(r'\bT\s*([1-9]\d*[A-Za-z]?)\b', airport, re.I)
                    if m:
                        seg[terminal_key] = 'T' + m.group(1)
                continue

            # Terminal field without matching endpoint evidence is leakage.
            if tk and ak != tk:
                seg[terminal_key] = ''

    return data


def _recover_source_only_fields(data, raw_text):
    """Repair Gemini omissions using literal text already printed by the supplier."""
    if not raw_text:
        return data
    for seg in data.get('segments') or []:
        # Clean model placeholders first; blank means source did not supply it.
        for key in ('flight','flight_number','aircraft','cabin','fare_type','dep_time','dep_city','dep_code','dep_date','dep_airport','dep_terminal',
                    'arr_time','arr_city','arr_code','arr_date','arr_airport','arr_terminal','duration','stops','layover'):
            seg[key]=_clean_source_value(seg.get(key))

        block=_segment_source_window(raw_text, seg)

        if not seg.get('aircraft'):
            m=re.search(r'\bAircraft\s*[:\-]\s*([^\r\n|]{1,35})', block, re.I)
            if m: seg['aircraft']=_clean_source_value(m.group(1))
        if not seg.get('cabin'):
            m=re.search(r'\bCabin\s*[:\-]\s*([^\r\n|]{1,30})', block, re.I)
            if m: seg['cabin']=_clean_source_value(m.group(1))
        if not seg.get('fare_type'):
            m=re.search(r'(?i)\bFare\s*type\s*[:\-]?\s*([^\r\n|]{1,30})', block)
            if m: seg['fare_type']=_clean_source_value(m.group(1))
        if not seg.get('layover'):
            m=re.search(r'(?i)\b(?:Long\s+)?Layover\s*[:\-]?\s*([^\r\n|]{2,30})', block)
            if m: seg['layover']=_clean_source_value(m.group(1))
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

        seg['dep_airport']=_strip_unproven_terminal_from_airport(seg.get('dep_airport'), raw_text)
        seg['arr_airport']=_strip_unproven_terminal_from_airport(seg.get('arr_airport'), raw_text)

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
        # Keep airport wording unchanged; never reconstruct it by appending a terminal.

    for pax in data.get('passengers') or []:
        for key in ('name','title','ticket_number','type','dob','baggage','special_ancillary'):
            pax[key]=_clean_source_value(pax.get(key))
    for key in ('booking_id','booking_date','airline_pnr','gds_pnr','status','mobile','baggage_summary','special_ancillary_summary'):
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
            if re.search(r'(?i)\b(?:fare|tax(?:es)?|fee|charge|surcharge|yq|yr|gst|k3|seat|meal|baggage|ancillary|insurance|convenience|service)\b',label):
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


# ===========================================================================
# V194 LOCAL-FIRST AIR ENGINE
# ===========================================================================
_LOCAL_IATA_EXCLUDE={
    'PNR','GDS','DOB','GST','INR','TAX','FARE','AIR','ADT','CHD','INF','SSR','PDF',
    'THE','AND','FOR','FROM','TO','NON','STOP','CAB','BAG','KGS','KG'
}


def _local_blank_air_data():
    return {
        'booking_id':'','booking_date':'','airline_pnr':'','gds_pnr':'','status':'','mobile':'',
        'baggage_summary':'','special_ancillary_summary':'','segments':[],'passengers':[],
        'base_fare':0.0,'taxes':0.0,'gross_total':0.0,'payment_items':[],
    }


def _local_label_value(raw, labels, max_len=70):
    if not raw:
        return ''
    label_pat='|'.join(labels)
    pat=re.compile(r'(?im)^\s*(?:'+label_pat+r')\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9./_\- ]{1,'+str(max_len)+r'})\s*$')
    m=pat.search(raw)
    if not m:
        pat=re.compile(r'(?i)\b(?:'+label_pat+r')\s*[:#\-]?\s*([A-Za-z0-9][A-Za-z0-9./_\-]{1,30})')
        m=pat.search(raw)
    return re.sub(r'\s+',' ',m.group(1)).strip(' :-|') if m else ''


def _local_iata_from_text(text):
    for m in re.finditer(r'(?<![A-Z])([A-Z]{3})(?![A-Z])',str(text or '').upper()):
        code=m.group(1)
        if code not in _LOCAL_IATA_EXCLUDE:
            return code
    return ''


def _local_flight_from_text(text):
    raw=str(text or '').upper()
    # Flight numbers are normally 2-character IATA carrier codes + 1-4 digits.
    # Restrict the local detector deliberately; unusual formats can use the one AI
    # fallback rather than turning Booking IDs, `Check-in 15KG`, or `Oct 2026` into flights.
    reject={'IN','ON','ID','NO','KG','RS','MR','MS','DR','AM','PM','JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'}
    known3={k for k in _FLIGHT_AIRLINE_BY_CODE if len(k)==3}
    for m in re.finditer(r'(?<![A-Z0-9])([A-Z0-9]{2,3})\s*[- ]?\s*(\d{1,4})(?!\d)',raw):
        code,num=m.group(1),m.group(2)
        if code in reject:
            continue
        if len(code)==3 and code not in known3:
            continue
        if re.search(r'(?i)\b(?:booking|trip|invoice|amount|total|baggage|check[- ]?in|date)\b',raw):
            # Still allow an explicitly-labelled Flight line.
            if not re.search(r'(?i)\bflight\b',raw):
                continue
        return f'{code} {num}'
    return ''


def _local_time_from_zone(spans,page,x0,x1,y0,y1,anchor_y):
    candidates=[]
    for sp in spans:
        if sp['page']!=page:
            continue
        cx,cy=_line_center(sp)
        if not (x0<=cx<=x1 and y0<=cy<=y1):
            continue
        m=re.search(r'(?i)\b([01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM)?\b',str(sp.get('text') or ''))
        if m:
            candidates.append((abs(cy-anchor_y),re.sub(r'\s+',' ',m.group(0)).strip()))
    candidates.sort(key=lambda x:x[0])
    return candidates[0][1] if candidates else ''


def _local_date_from_zone(spans,page,x0,x1,y0,y1,anchor_y):
    candidates=[]
    pats=(
        r'(?i)\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s+\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}\b',
        r'(?i)\b\d{1,2}\s+[A-Za-z]{3,9}\s+20\d{2}\b',
        r'\b\d{1,2}[/-]\d{1,2}[/-]20\d{2}\b',
    )
    for sp in spans:
        if sp['page']!=page:
            continue
        cx,cy=_line_center(sp)
        if not (x0<=cx<=x1 and y0<=cy<=y1):
            continue
        for pat in pats:
            m=re.search(pat,str(sp.get('text') or ''))
            if m:
                candidates.append((abs(cy-anchor_y),re.sub(r'\s+',' ',m.group(0)).strip()))
                break
    candidates.sort(key=lambda x:x[0])
    return candidates[0][1] if candidates else ''


def _local_city_from_anchor(text,code):
    value=re.sub(r'(?i)(?<![A-Z])'+re.escape(code)+r'(?![A-Z])',' ',str(text or ''))
    value=re.sub(r'[()\[\],|:;]+',' ',value)
    value=re.sub(r'\b\d{1,2}:\d{2}\b',' ',value)
    value=re.sub(r'\s+',' ',value).strip(' -')
    if value and len(value)<=45 and re.search(r'[A-Za-z]',value) and not re.search(r'(?i)\b(?:airport|international|terminal|aerodrome|airfield)\b',value):
        return value
    return ''


def _local_segments_from_pdf_geometry(original_paths):
    spans=[]
    for path in original_paths or []:
        try:
            p=Path(path)
            if p.exists() and p.suffix.lower()=='.pdf':
                spans.extend(_pdf_layout_lines(p))
        except Exception:
            pass
    if not spans:
        return []

    iatas=[]; flights=[]
    for sp in spans:
        code=_local_iata_from_text(sp.get('text'))
        if code:
            cp=dict(sp); cp['iata']=code; iatas.append(cp)
        fn=_local_flight_from_text(sp.get('text'))
        if fn:
            cp=dict(sp); cp['flight_number']=fn; flights.append(cp)

    segments=[]; used=set()
    for fl in flights:
        fx,fy=_line_center(fl); candidates=[]
        same=[x for x in iatas if x['page']==fl['page']]
        for dep in same:
            dx,dy=_line_center(dep)
            if dx<fx-35 or abs(dy-fy)>145:
                continue
            for arr in same:
                if arr is dep or arr['iata']==dep['iata']:
                    continue
                ax,ay=_line_center(arr)
                if ax<=dx or abs(ay-dy)>75:
                    continue
                sep=ax-dx
                if sep<105:
                    continue
                dep_time=_local_time_from_zone(spans,dep['page'],dx-sep*.32,dx+sep*.40,dy-30,dy+105,dy)
                arr_time=_local_time_from_zone(spans,arr['page'],ax-sep*.40,ax+sep*.32,ay-30,ay+105,ay)
                score=100-abs(ay-dy)-min(35,abs(((dy+ay)/2)-fy)/4)+(22 if fx<dx else 0)+(12 if dep_time else 0)+(12 if arr_time else 0)
                candidates.append((score,dep,arr,dep_time,arr_time))
        if not candidates:
            continue
        candidates.sort(key=lambda x:x[0],reverse=True)
        _,dep,arr,dep_time,arr_time=candidates[0]
        key=(fl['page'],fl['flight_number'],round(_line_center(dep)[1],0),dep['iata'],arr['iata'])
        if key in used:
            continue
        used.add(key)

        dx,dy=_line_center(dep); ax,ay=_line_center(arr); sep=max(105.0,ax-dx)
        page_width=max(float(dep.get('page_width') or 0),float(arr.get('page_width') or 0),ax+sep*.35)
        top=min(dy,ay)-12; bottom=max(dy,ay)+125
        dep_left=dx-sep*.30; dep_right=dx+sep*.38
        arr_left=ax-sep*.38; arr_right=min(page_width,ax+sep*.34)
        dep_city=_local_city_from_anchor(dep.get('text'),dep['iata'])
        arr_city=_local_city_from_anchor(arr.get('text'),arr['iata'])
        dep_airport=_airport_block_from_zone(spans,dep['page'],dep_left,dep_right,top,bottom,dep_city,dep['iata'],dy)
        arr_airport=_airport_block_from_zone(spans,arr['page'],arr_left,arr_right,top,bottom,arr_city,arr['iata'],ay)
        dep_terminal=_extract_terminal_from_endpoint_text(dep_airport)
        arr_terminal=_extract_terminal_from_endpoint_text(arr_airport)
        dep_date=_local_date_from_zone(spans,dep['page'],dep_left,dep_right,top,bottom,dy)
        arr_date=_local_date_from_zone(spans,arr['page'],arr_left,arr_right,top,bottom,ay)
        center_lines=_group_spans_in_zone(spans,dep['page'],dep_right,arr_left,top,bottom)
        center_text=' '.join(x.get('text','') for x in center_lines)
        dm=re.search(r'(?i)\b(\d{1,2}\s*(?:h|hr|hrs|hour|hours)\s*(?:\d{1,2}\s*(?:m|min|mins|minutes))?)\b',center_text)
        sm=re.search(r'(?i)\b(?:non\s*[- ]?stop|direct|[1-9]\s+stops?)\b',center_text)
        fn=fl['flight_number']; carrier=fn.split()[0]
        seg={
            'flight':_FLIGHT_AIRLINE_BY_CODE.get(carrier,carrier),'flight_number':fn,
            'aircraft':'','cabin':'','fare_type':'','dep_time':dep_time,'dep_city':dep_city,
            'dep_code':dep['iata'],'dep_date':dep_date,
            'dep_airport':_separate_endpoint_code_prefix(dep_airport,dep['iata']),'dep_terminal':dep_terminal,
            'arr_time':arr_time,'arr_city':arr_city,'arr_code':arr['iata'],'arr_date':arr_date,
            'arr_airport':_separate_endpoint_code_prefix(arr_airport,arr['iata']),'arr_terminal':arr_terminal,
            'duration':re.sub(r'\s+',' ',dm.group(1)).strip() if dm else '',
            'stops':re.sub(r'\s+',' ',sm.group(0)).strip() if sm else '','layover':''
        }
        if seg['dep_airport']:
            seg['dep_airport_source_exact']=seg['dep_airport']
        if seg['arr_airport']:
            seg['arr_airport_source_exact']=seg['arr_airport']
        segments.append(seg)

    clean=[]; seen=set()
    for seg in segments:
        k=(seg.get('flight_number'),seg.get('dep_code'),seg.get('arr_code'),seg.get('dep_time'),seg.get('arr_time'))
        if k not in seen:
            seen.add(k); clean.append(seg)
    return clean


def _local_segments_from_text(raw):
    out=[]; seen=set(); raw=str(raw or '')
    reject={'IN','ON','ID','NO','KG','RS','MR','MS','DR','AM','PM','JAN','FEB','MAR','APR','MAY','JUN','JUL','AUG','SEP','OCT','NOV','DEC'}
    known3={k for k in _FLIGHT_AIRLINE_BY_CODE if len(k)==3}
    for fm in re.finditer(r'(?<![A-Z0-9])([A-Z0-9]{2,3})\s*[- ]?\s*(\d{1,4})(?!\d)',raw.upper()):
        if fm.group(1) in reject or (len(fm.group(1))==3 and fm.group(1) not in known3):
            continue
        fn=f'{fm.group(1)} {fm.group(2)}'
        window=raw[max(0,fm.start()-150):min(len(raw),fm.end()+1200)]
        codes=[]
        for cm in re.finditer(r'(?<![A-Z])([A-Z]{3})(?![A-Z])',window.upper()):
            c=cm.group(1)
            if c not in _LOCAL_IATA_EXCLUDE and c not in codes:
                codes.append(c)
        if len(codes)<2:
            continue
        times=[re.sub(r'\s+',' ',x).strip() for x in re.findall(r'(?i)\b(?:[01]?\d|2[0-3]):[0-5]\d\s*(?:AM|PM)?\b',window)]
        k=(fn,codes[0],codes[1],times[0] if times else '')
        if k in seen:
            continue
        seen.add(k); carrier=fm.group(1)
        out.append({
            'flight':_FLIGHT_AIRLINE_BY_CODE.get(carrier,carrier),'flight_number':fn,
            'aircraft':'','cabin':'','fare_type':'','dep_time':times[0] if times else '',
            'dep_city':'','dep_code':codes[0],'dep_date':'','dep_airport':'','dep_terminal':'',
            'arr_time':times[1] if len(times)>1 else '','arr_city':'','arr_code':codes[1],
            'arr_date':'','arr_airport':'','arr_terminal':'','duration':'','stops':'','layover':''
        })
    return out


def _local_baggage_summary(raw):
    lines=[re.sub(r'\s+',' ',x).strip() for x in str(raw or '').splitlines()]
    hits=[]
    for i,line in enumerate(lines):
        if re.search(r'(?i)\b(?:baggage|check[- ]?in|checked\s+bag|cabin\s+bag|hand\s+bag|carry[- ]?on)\b',line) and re.search(r'(?i)\b\d+(?:\.\d+)?\s*(?:kg|kgs|piece|pieces|pc|pcs)\b',line):
            hits.append(line)
            if i+1<len(lines) and re.search(r'(?i)\b(?:cabin|hand|carry[- ]?on)\b',lines[i+1]) and re.search(r'\d',lines[i+1]):
                hits.append(lines[i+1])
            break
    return ' '.join(dict.fromkeys(x for x in hits if x))


def _local_passengers_from_text(raw,baggage_summary=''):
    lines=[re.sub(r'\s+',' ',x).strip() for x in str(raw or '').splitlines() if str(x).strip()]
    passengers=[]; seen=set()
    title_pat=re.compile(r"(?i)\b(Mr|Mrs|Ms|Miss|Master|Mstr|Dr|Prof)\.?\s+([A-Za-z][A-Za-z .'/-]{2,70})")
    row_pat=re.compile(r"(?i)^\s*\d{1,2}[.)]?\s+([A-Za-z][A-Za-z .'/-]{2,60}?)\s+(Adult|Child|Infant|ADT|CHD|INF)\b")
    for line in lines:
        title=''; name=''; ptype=''
        m=title_pat.search(line)
        if m:
            title=m.group(1)+('.' if not m.group(1).endswith('.') else '')
            name=m.group(2)
            name=re.split(r'(?i)\s+(?:Adult|Child|Infant|ADT|CHD|INF|DOB|Ticket|PNR|Baggage|\d{10,})\b',name)[0].strip(' ,-')
        else:
            m=row_pat.search(line)
            if m:
                name=m.group(1).strip(' ,-'); ptype=m.group(2)
        if not name or len(name)<3 or re.search(r'(?i)\b(?:flight|airport|booking|customer|support|fare|payment)\b',name):
            continue
        low=(ptype+' '+title+' '+line).lower()
        if re.search(r'\b(?:infant|inf)\b',low):
            canon='Infant'
        elif re.search(r'\b(?:child|chd)\b',low) or title.lower().startswith(('master','mstr')):
            canon='Child'
        else:
            canon='Adult'
        key=re.sub(r'\W+','',name).lower()
        if key in seen:
            continue
        seen.add(key); dob=''; ticket=''
        dm=re.search(r'(?i)\b(?:DOB\s*[:\-]?\s*)?([0-3]?\d[/-](?:0?\d|[A-Za-z]{3})[/-]\d{2,4})\b',line)
        if dm and ('dob' in line.lower() or canon in ('Child','Infant')):
            dob=dm.group(1)
        tm=re.search(r'(?i)\b(?:ticket(?:\s*(?:no|number))?|e-ticket)\s*[:#\-]?\s*([0-9][0-9\-]{8,18})\b',line)
        if tm:
            ticket=tm.group(1)
        bag=line if re.search(r'(?i)\b(?:kg|kgs|cabin|check[- ]?in|baggage)\b',line) else baggage_summary
        passengers.append({'name':name,'title':title,'ticket_number':ticket,'type':canon,'dob':dob,'baggage':bag,'special_ancillary':''})
    return passengers


def _local_first_air_extract(raw_text,original_paths):
    data=_local_blank_air_data(); raw=str(raw_text or '')
    data['booking_date']=_explicit_booking_date_from_text(raw)
    data['mobile']=_explicit_customer_mobile_from_text(raw)
    data['gds_pnr']=_local_label_value(raw,[r'GDS\s*PNR'])
    data['airline_pnr']=_local_label_value(raw,[r'Airline\s*PNR',r'Carrier\s*PNR'])
    if not data['airline_pnr']:
        for m in re.finditer(r'(?i)\bPNR\s*[:#\-]?\s*([A-Z0-9]{5,9})\b',raw):
            if 'gds' not in raw[max(0,m.start()-12):m.start()].lower():
                data['airline_pnr']=m.group(1); break
    data['booking_id']=_local_label_value(raw,[r'Booking\s*(?:ID|Id|Reference|Ref)',r'Trip\s*ID',r'Reservation\s*(?:ID|Ref)'])
    data['status']=_local_label_value(raw,[r'Status'],24)
    data['baggage_summary']=_local_baggage_summary(raw)
    data['passengers']=_local_passengers_from_text(raw,data['baggage_summary'])
    data['segments']=_local_segments_from_pdf_geometry(original_paths) or _local_segments_from_text(raw)
    data=_recover_source_only_fields(data,raw)
    data=_normalize_flight_segments(data)
    data=_sanitize_ticket_numbers(data)
    data=_sanitize_inferred_stops(data)
    data=_apply_baggage_summary(data)
    data=_normalize_passenger_types(data)
    data=_apply_special_ancillary_summary(data)
    try:
        data=_recover_airport_names_from_pdf_geometry(data,original_paths)
    except Exception:
        pass
    return _final_endpoint_safety_gate(data)


def _local_air_core_complete(data):
    segs=(data or {}).get('segments') or []
    pax=(data or {}).get('passengers') or []
    if not segs or not pax:
        return False
    return all(all(str(seg.get(k) or '').strip() for k in ('flight_number','dep_code','arr_code','dep_time','arr_time')) for seg in segs)


def _merge_ai_into_local(local,ai):
    local=local or _local_blank_air_data(); ai=ai or {}
    for key in ('booking_id','booking_date','airline_pnr','gds_pnr','status','mobile','baggage_summary','special_ancillary_summary'):
        if not str(local.get(key) or '').strip() and str(ai.get(key) or '').strip():
            local[key]=ai.get(key)
    if not local.get('passengers') and ai.get('passengers'):
        local['passengers']=ai.get('passengers') or []
    elif local.get('passengers') and ai.get('passengers'):
        for i,p in enumerate(local['passengers']):
            if i<len(ai['passengers']):
                for k in ('title','ticket_number','type','dob','baggage','special_ancillary'):
                    if not str(p.get(k) or '').strip() and str(ai['passengers'][i].get(k) or '').strip():
                        p[k]=ai['passengers'][i].get(k)
    if not local.get('segments') and ai.get('segments'):
        local['segments']=ai.get('segments') or []
    elif local.get('segments') and ai.get('segments'):
        for i,seg in enumerate(local['segments']):
            if i<len(ai['segments']):
                for k in ('flight','flight_number','aircraft','cabin','fare_type','dep_time','dep_city','dep_code','dep_date','dep_airport','dep_terminal','arr_time','arr_city','arr_code','arr_date','arr_airport','arr_terminal','duration','stops','layover'):
                    if not str(seg.get(k) or '').strip() and str(ai['segments'][i].get(k) or '').strip():
                        seg[k]=ai['segments'][i].get(k)
    if not local.get('payment_items') and ai.get('payment_items'):
        local['payment_items']=ai.get('payment_items') or []
    for k in ('base_fare','taxes','gross_total'):
        try: lv=float(local.get(k) or 0)
        except Exception: lv=0
        try: av=float(ai.get(k) or 0)
        except Exception: av=0
        if lv<=0 and av>0:
            local[k]=av
    return local


AIR_LIGHT_PROMPT="Extract only booking facts needed for an airline itinerary. SOURCE PRESENT = COPY, SOURCE ABSENT = BLANK. Do not infer airports, terminals, dates, baggage or fares. Return every flight sector separately; never merge connections. Preserve passenger names/types, PNR/ticket numbers, baggage, flight number, departure/arrival date/time/IATA/airport/terminal, duration/stops only when printed, and supplier payment rows/total. Ignore terms/marketing. Return JSON only."

def extract_flight_ticket(file_parts, source_text, api_key, model):
    """V194 local-first extraction: zero AI for readable tickets, max one fallback."""
    raw_source_text=_plain_source_text(file_parts,source_text)
    original_paths=[Path(item.get('path') or '') for item in (file_parts or []) if item.get('path')]
    data=_local_first_air_extract(raw_source_text,original_paths)
    used_ai=False

    if not _local_air_core_complete(data):
        client=genai.Client(api_key=api_key)
        contents=[AIR_LIGHT_PROMPT]
        compact=re.sub(r'\n{3,}','\n\n',str(raw_source_text or '')).strip()
        if compact:
            contents.append('\nSUPPLIER TEXT:\n'+compact[:6500])
        # Only scanned/image sources need a visual attachment. Selectable PDFs stay text-only.
        if len(compact)<500:
            for item in (file_parts or [])[:1]:
                try:
                    p=Path(item.get('path') or '')
                    if p.exists():
                        contents.append(types.Part.from_bytes(data=p.read_bytes(),mime_type=item.get('mime_type') or ('application/pdf' if p.suffix.lower()=='.pdf' else 'image/jpeg')))
                except Exception:
                    pass
        try:
            r=call_with_high_demand_retry(lambda: client.models.generate_content(
                model=model,contents=contents,
                config=types.GenerateContentConfig(
                    response_mime_type='application/json',response_schema=SCHEMA,
                    temperature=0,max_output_tokens=2200,
                )
            ))
            if r.text:
                data=_merge_ai_into_local(data,_normalize_flight_segments(json.loads(r.text)))
                used_ai=True
        except Exception:
            pass

    # All remaining verification is deterministic/local. No second source-truth,
    # repair, endpoint-verifier or fare-recovery AI calls.
    data=_recover_source_only_fields(data,raw_source_text)
    data=_normalize_flight_segments(data)
    data=_sanitize_ticket_numbers(data)
    data=_sanitize_inferred_stops(data)
    data=_apply_baggage_summary(data)
    data=_normalize_passenger_types(data)
    data=_apply_special_ancillary_summary(data)
    data=_enforce_terminal_endpoint_truth(data)
    try:
        data=_recover_airport_names_from_pdf_geometry(data,original_paths)
    except Exception:
        pass
    data=_final_endpoint_safety_gate(data)

    clean=[]
    for row in (data.get('payment_items') or []):
        try: amount=float(row.get('amount') or 0)
        except Exception: amount=0
        label=str(row.get('label') or '').strip()
        if label and amount>=0:
            clean.append({'label':label,'amount':amount})
    data['payment_items']=clean
    try: gross=float(data.get('gross_total') or 0)
    except Exception: gross=0
    if gross<=0 and clean:
        gross=sum(float(x.get('amount') or 0) for x in clean)
    if gross<=0:
        try: gross=float(data.get('base_fare') or 0)+float(data.get('taxes') or 0)
        except Exception: gross=0
    data['gross_total']=gross
    data['_local_first']=True
    data['_ai_fallback_used']=used_ai
    return data
