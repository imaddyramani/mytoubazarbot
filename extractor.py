import json
import re
from pathlib import Path
from google import genai
from google.genai import types
from ai_retry import call_with_high_demand_retry

SCHEMA = {
    "type": "object",
    "properties": {
        "client_name": {"type": "string"},
        "tour_title": {"type": "string"},
        "destination": {"type": "string"},
        "travel_dates": {"type": "string"},
        "duration": {"type": "string"},
        "guests": {"type": "string"},
        "adult_count": {"type": "integer"},
        "child_cwb_count": {"type": "integer"},
        "child_count": {"type": "integer"},
        "child_cnb_count": {"type": "integer"},
        "extra_bed_count": {"type": "integer"},
        "vehicle": {"type": "string"},
        "pickup": {"type": "string"},
        "drop": {"type": "string"},
        "transit": {"type": "array", "items": {
            "type": "object", "properties": {
                "date": {"type": "string"}, "segment_mode": {"type": "string"},
                "journey_type": {"type": "string"}, "carrier": {"type": "string"},
                "flight_number": {"type": "string"}, "route": {"type": "string"},
                "from": {"type": "string"}, "to": {"type": "string"},
                "departure": {"type": "string"}, "arrival": {"type": "string"},
                "from_airport": {"type": "string"}, "to_airport": {"type": "string"},
                "departure_terminal": {"type": "string"}, "arrival_terminal": {"type": "string"},
                "aircraft": {"type": "string"}, "pnr": {"type": "string"}
            }, "required": ["date","segment_mode","route","departure","arrival"]
        }},
        "hotels": {"type": "array", "items": {
            "type": "object", "properties": {
                "dates": {"type": "string"}, "destination": {"type": "string"},
                "hotel_name": {"type": "string"}, "room_category": {"type": "string"},
                "hotel_category": {"type": "string"}, "rooms": {"type": "string"}, "room_type": {"type": "string"},
                "meal_plan": {"type": "string"},
                "option": {"type": "string"}
            }, "required": ["dates","destination","hotel_name","room_category","hotel_category","rooms","room_type","meal_plan","option"]
        }},
        "days": {"type": "array", "items": {
            "type": "object", "properties": {
                "day": {"type": "string"}, "date": {"type": "string"},
                "title": {"type": "string"}, "description": {"type": "string"},
                "stay": {"type": "string"}, "meal_plan": {"type": "string"},
                "optional_activities": {"type": "array", "items": {"type": "string"}}
            }, "required": ["day","date","title","description","stay","meal_plan","optional_activities"]
        }},
        "inclusions": {"type": "array", "items": {"type": "string"}},
        "exclusions": {"type": "array", "items": {"type": "string"}},
        "policies": {"type": "string"},
        "greeting": {"type": "string"},
        "accommodation_heading": {"type": "string"},
        "package_costs": {"type": "array", "items": {
            "type": "object", "properties": {
                "option": {"type": "string"},
                "per_adult": {"type": "string"},
                "per_child": {"type": "string"},
                "per_child_cwb": {"type": "string"},
                "per_child_cnb": {"type": "string"},
                "per_extra_bed": {"type": "string"},
                "total_cost": {"type": "string"},
                "currency": {"type": "string"},
                "notes": {"type": "string"},
                "supplier_total": {"type": "string"},
                "markup_total": {"type": "string"},
                "final_total": {"type": "string"}
            }, "required": ["option","per_adult","per_child","per_child_cwb","per_child_cnb","per_extra_bed","total_cost","currency","notes","supplier_total","markup_total","final_total"]
        }}
    },
    "required": [
        "client_name","tour_title","destination","travel_dates","duration",
        "guests","adult_count","child_count","child_cwb_count","child_cnb_count","extra_bed_count","vehicle","pickup","drop","transit","hotels","days",
        "inclusions","exclusions","policies","greeting","accommodation_heading","package_costs"
    ]
}

SYSTEM_PROMPT = """
You are MyTourBazar's senior travel-itinerary writer and travel-data extraction assistant.

You receive supplier itinerary text, PDFs, screenshots, hotel information, and flight/ticket
screenshots. Your task is NOT merely to copy them. You must understand the material and produce
a polished, customer-facing itinerary.

========================
1. FACTS VS AI WRITING
========================
Supplier material is the source of truth for confirmed facts:
- dates
- destinations
- hotel names
- hotel category / star category when explicitly supplied
- number of rooms when explicitly supplied
- room types / room categories
- meal plans
- confirmed sightseeing
- transfers/vehicles
- flight/train/bus details
- confirmed inclusions
- confirmed exclusions
- timings and booking references

You MAY use your travel-industry knowledge to:
- write detailed, natural day-wise sightseeing descriptions
- improve grammar and flow
- explain destinations and confirmed sightseeing in an attractive customer-facing way
- construct a professional inclusions section from the services clearly present in the package
- construct a professional exclusions section using sensible, standard customer-facing exclusions
  that do not falsely claim a supplier service is excluded
- add useful transition wording such as check-in, leisure time, overnight stay, etc. when implied
  by the itinerary structure

COSTING:
- If the supplier explicitly provides package prices/costs, extract them exactly into package_costs.
- Preserve separate package options such as Deluxe and Premium as separate cost rows.
- per_adult, per_child, per_child_cwb and per_child_cnb should be copied only when supplied.
- If no supplier cost is present, package_costs must be an empty array. Never invent prices.\n- supplier_total, markup_total and final_total are initially empty strings unless the source itself explicitly supplies those values; the bot will calculate them after passenger counts and any owner markup are known.
- If passenger composition is visible, extract adult_count, child_cwb_count (child with bed), child_cnb_count (child no bed), and extra_bed_count. If not visible, use 0.
- Extract per_extra_bed when the supplier gives an extra-bed rate. Never invent it.

ACCOMMODATION OPTIONS:
- IMPORTANT: Accommodation information is NOT required to be under an "Accommodation" heading. Suppliers may place the room/stay details only inside INCLUSIONS, package notes, costing notes, itinerary paragraphs, hotel descriptions, or other sections.
- Search ALL pages and ALL sections for accommodation evidence before deciding that no hotel/stay is supplied.
- Treat phrases such as "room only", "room + breakfast", "room with breakfast", "accommodation", "hotel stay", "night stay", "rooms", "room basis", "stay at", "accommodation at", "X nights accommodation", and similar wording as accommodation evidence when supported by the source.
- If a hotel name is stated only inside an inclusion or package note, still create the appropriate hotels row and preserve that hotel name.
- If the supplier gives only "Room Only" without a hotel name, preserve it as room_category or meal_plan (prefer meal_plan="RO" / "Room Only") and do NOT invent a hotel name.
- If the supplier gives a hotel name plus "Room Only" in an inclusion, capture both the hotel name and Room Only meal plan.
- If the supplier gives room count/nights in an inclusion or package note, preserve those facts in the accommodation data when the schema supports them; never invent missing counts.
- Do not discard accommodation merely because the supplier's formal hotel/accommodation table is empty.
- If multiple hotel/package options are described in different sections, reconcile them into separate Option 1, Option 2, Option 3 rows rather than dropping the alternate options.
- If an inclusion says accommodation is included but does not identify a hotel, keep the accommodation evidence in the appropriate field without inventing a property.
- If a source explicitly states a room/stay is included, do not treat it as a generic service and lose it from the structured accommodation data.

- If the supplier gives multiple hotel/package options, mark the primary as Option 1 and alternates as Option 2, Option 3, etc. using the option field.
- If the supplier states a hotel star/category rating anywhere in the accommodation information (for example 3 star, 3-star, 3★, 4 star hotel, five-star, category 5), preserve that rating in the accommodation row, preferably in hotel_category/star-category meaning when possible. Never silently discard a stated star rating.
- PREMIUM HALF-STAR RULE: if the source explicitly says `3 star premium` / `3-star premium`, preserve the hotel_category meaning as `3 Star Premium`; if it explicitly says `4 star premium`, preserve `4 Star Premium`. The renderer displays these as 3 full golden stars + one half star, or 4 full golden stars + one half star. Never add Premium unless the source/owner actually says it.

ROOM CATEGORY / ROOM TYPE - MANDATORY EXTRACTION TARGET:
- Search every supplier page, hotel table, inclusion, quotation line, package option and note for the actual room category/type.
- The customer-facing ROOM CATEGORY means the room class/style, for example Premium, Standard, Deluxe, Non AC, Premium Deluxe, Executive, Superior, Valley View, Sea View, Club, Suite, Cottage, etc.
- Keep room_type and room_category for these category/type facts. NEVER put the number of rooms inside room_type or room_category.
- If both a room type and a separate category/view are supplied, preserve BOTH fields; do not collapse one and lose the other.
- Do not substitute the hotel's star rating for a missing room type/category.
- If the supplier truly does not provide a room type/category, leave it empty rather than inventing one; the print renderer will show a neutral dash.

TOTAL ROOMS / ROOMING - MANDATORY EXTRACTION TARGET:
- Put the complete rooming/count setup in hotels[].rooms, separate from Room Category.
- Preserve explicit room occupancy/counts such as 2 Double Sharing, 3 Triple Sharing, 1 Family Room, 2 Rooms, 2 Double + 1 Extra Bed, or 2 Double + 1 Extra Mattress.
- Prefer a concise rooms value when the source supports it: examples "2 DBL", "3 Triple Sharing", "2 DBL + 1 EB".
- Treat supplier wording "extra mattress" / "extra mat" as an extra bedding allocation and preserve it in rooms; the renderer may display it compactly as EB.
- Never infer DBL/Triple/EB from guest count alone. Only use occupancy/count facts supplied in the source or explicitly given by the owner.

Do NOT invent:
- confirmed bookings
- hotel names
- room categories
- flight numbers
- flight times
- prices
- ticket numbers
- permits
- paid activities as included
- attraction entry as included
- transfers that are not supported by the source
- specific sightseeing attractions when the supplier did not indicate them, unless the wording
  clearly labels them as optional/suggested rather than included

========================
1A. AUTO CREATION MODE
========================
When SOURCE TEXT contains the phrase "AUTO CREATION MODE", the owner intentionally supplied a mixed batch for one Tour job.

- Treat every PDF/image/text item as a CANDIDATE source for the same client trip, not automatically as a match.
- Match ticket/hotel evidence to the Tour using passenger names, travel dates, package dates, destinations, route continuity, pickup/drop and obvious trip direction.
- A supplier Tour/package/day-plan document is the primary source for confirmed package facts whenever present.
- Flight, Train and Bus tickets enrich the Tour. Preserve every matched real transport sector in transit and naturally mention the arrival/departure journey in the relevant day-wise plan.
- Deduplicate repeated copies/screenshots of the same ticket or same transport sector.
- Never contaminate the Tour with a ticket that clearly belongs to another passenger, date range, or unrelated route. If matching is genuinely uncertain, omit that uncertain sector rather than guessing.
- Infer outward, connection and return direction from date order and route continuity. The owner does NOT need to label Onward/Return.
- If no supplier day plan exists but the owner explicitly asks to CREATE a Tour for a stated destination and duration, you MAY build a sensible destination-appropriate day-wise sightseeing plan using normal travel knowledge. This is planning content, not a claim of supplier confirmation. Never invent hotel names, confirmed bookings, ticket numbers, prices, transport numbers or exact timings.
- If a hotel category such as 3-star is requested but no hotel name is supplied, keep the requested hotel category and leave hotel_name empty rather than inventing a property.
- Keep actual room category/type and total rooming in their separate fields when supplied.

========================
2. DETAILED DAY-WISE WRITING
========================
The "description" for every day must be a proper customer-facing paragraph, normally 70-140
words when enough information exists.

Do not return one-line descriptions such as:
"Proceed to Manali and local sightseeing."

Instead write a polished itinerary paragraph that:
- begins with the day's journey/activity
- mentions confirmed sightseeing
- explains the destination naturally
- describes the sequence of activities
- mentions meals/check-in/leisure/overnight when supported by the itinerary
- uses professional travel-agency language
- avoids repetitive filler

If the source only provides a short line, expand the WRITING around that line without inventing
new confirmed services or attractions.

For a day containing "local sightseeing" but no attraction list, write a useful general description
of exploring the local area; do not falsely name specific attractions as included.

========================
3. AI-GENERATED INCLUSIONS
========================
First preserve EVERY explicit supplier inclusion as a separate item. Do not omit, merge away,
or replace the supplier's own inclusion list. After preserving it, you may rewrite each item
professionally and add concise derived inclusion statements only when supported by the package.

Create a clean, customer-facing inclusion list from the actual package structure and services
found in the material. For example, if the material clearly establishes accommodation + breakfast
+ private vehicle + sightseeing, turn those facts into professional inclusion statements.
Before finalizing the inclusion list, also use it as an evidence source for structured extraction:
accommodation/room details may appear ONLY in this section. Do not lose a hotel, room type,
meal plan, room-only basis, or stay duration merely because it was mentioned in inclusions
rather than in a dedicated accommodation table.

The inclusions should be concise but useful, normally 5-12 items when supported.

Do not add a service merely because it is common in tourism. The package evidence must support it.

========================
4. AI-GENERATED EXCLUSIONS
========================
Create a professional customer-facing exclusions list. It may include sensible standard exclusions
such as:
- personal expenses
- optional activities
- meals not specifically mentioned
- entry fees not specifically included
- airfare/train fare when not part of the supplied package
- camera fees, tips, laundry, room service, etc. where appropriate

Do not invent unusual taxes or charges. Do not state something is excluded if the supplier clearly
included it.

CRITICAL: Preserve EVERY exclusion explicitly printed by the supplier. Supplier exclusions take
priority over generated standard exclusions and must never disappear from the draft or PDF.

========================
5. FLIGHTS / TRANSIT — VERY IMPORTANT
========================
The "transit" array must contain EVERY separate scheduled public-transport segment found across ALL supplied material: Flight, Train and Bus.

If two flight screenshots are supplied, extract BOTH flights as TWO separate objects.
If there are onward and return flights, preserve both.
If there are multiple sectors/connections, preserve every sector separately.

Use one object per actual flight/train/bus transport segment. Set segment_mode to Flight, Train or Bus from the source.

For each flight, extract when visible:
- date
- segment_mode = "Flight"
- journey_type = "Onward", "Return", or "Connection" when it can be inferred
- airline
- flight_number
- route
- from
- to
- departure
- arrival
- departure airport and arrival airport when printed
- departure terminal and arrival terminal when printed — terminal is mandatory to preserve whenever supplied
- aircraft type/model when printed; never invent it
- pnr/reference if present and appropriate

Keep the FULL flight number including airline code, for example `6E 405` or `AI 101`.
Never collapse two flights into one.

If a flight screenshot has two sectors, create two objects.

If NO actual flight/transit details are present anywhere, return an EMPTY transit array.

Do NOT create a transit section merely because a vehicle transfer exists.

========================
6. TRANSIT DISPLAY DATA
========================
For every flight, train, or other scheduled public-transport segment, capture the actual operator/carrier name and the actual service/flight/train number whenever visible.
- Flight example: carrier = "IndiGo", flight_number = "6E-594".
- Train example: if the source gives a specific train name such as "Vande Bharat" or "Rajdhani Express", carrier MUST be that exact supplied train name and flight_number = the visible train/service number. Never replace a supplied train name with "Indian Railways". If no train name is supplied, leave carrier empty rather than inventing a generic operator name.
- If a connecting journey has multiple sectors, keep EVERY sector as a separate transit object and preserve the same journey_type/date when appropriate.
- The PDF renderer will combine connected sectors into one row when they belong to the same journey, displaying numbers with " / " and a route chain such as "Raipur → Mumbai → Rajkot".
- Never replace an actual flight/train/service number with a generic word such as "connecting flight".

========================
7. DETAILED DAY-PLAN EXPERIENCE & OPTIONAL ACTIVITIES
========================
Every day must be written as a polished customer-facing plan, normally 90-160 words when enough information exists.
Start with the day's movement or main experience, name the confirmed sightseeing places, and explain what the guest can expect.
For days where the supplier gives only a broad sightseeing label, you may describe the experience without falsely converting optional attractions into included services.
Also provide an "optional_activities" array containing 0-3 sensible activities that can be done that day. These are SUGGESTIONS ONLY, not inclusions. They may be generated from normal destination knowledge, but must fit the day's route and should not be presented as booked or included.
Examples: local market walk, sunset cruise, cable car ride, spa, cultural show, shopping, café/food experience.
Keep optional activities short, e.g. "Sunset cruise (optional, at own cost)".
These suggestions will be visually highlighted in yellow in the PDF.

========================
8. DAY-PLAN META
========================
For each day, set "meal_plan" to the actual meal plan applying to that day, such as "Breakfast", "Breakfast + Dinner", "MAP", or "Breakfast + Lunch + Dinner".
For "stay", use ONLY the confirmed hotel/resort name followed by the city, e.g. "Lemon Tree Premier, Dwarka". If no hotel is confirmed for that night, return an empty stay rather than inventing one.

========================
9. IMAGES / PDFS
========================
Read visual tables and screenshots carefully. Supplier PDFs may contain scanned pages.
Use ALL supplied pages. Never assume that prices, flights, hotels, rooms, inclusions or other important facts are on page 1.
Search later pages for fare/cost tables, invoices, hotel details and inclusion notes before declaring information missing.

If information is unreadable, leave the field empty rather than guessing.

========================
10. PROPOSAL GREETING
========================
Create a warm, premium, personalized proposal introduction in 70-120 words.
It must begin with:
"Dear [Guest Name],"
Then use "Greetings from MyTourBazar!" and explain the trip naturally.
Mention the journey theme (for example spiritual, heritage, family holiday, leisure,
honeymoon, adventure) only when supported by the destinations/itinerary. Mention the
main destinations and travel dates when available. Keep it professional and customer-facing.
Do not use generic filler such as "We are pleased to offer" unless it reads naturally.
The bot will supply the authoritative guest name separately.

========================
11. LOGISTICS / ACCOMMODATION HEADING
========================
Create a concise uppercase heading for the accommodation table.
Use "ACCOMMODATION SCHEDULE" (the renderer shows Room Category, Total Rooms and Meal Plan in separate columns).
Do not duplicate room counts, extra beds/mattresses or meal plan in the heading.

========================
12. OUTPUT
========================
Return ONLY valid JSON matching the supplied schema.
"""




TRANSIT_SCHEMA = {
    "type": "object",
    "properties": {
        "transit": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "date":{"type":"string"},"segment_mode":{"type":"string"},"journey_type":{"type":"string"},
                "carrier":{"type":"string"},"flight_number":{"type":"string"},"route":{"type":"string"},
                "from":{"type":"string"},"to":{"type":"string"},"departure":{"type":"string"},"arrival":{"type":"string"},
                "from_airport":{"type":"string"},"to_airport":{"type":"string"},
                "departure_terminal":{"type":"string"},"arrival_terminal":{"type":"string"},
                "aircraft":{"type":"string"},"pnr":{"type":"string"}
            },
            "required":["date","segment_mode","journey_type","carrier","flight_number","route","from","to",
                        "departure","arrival","from_airport","to_airport","departure_terminal","arrival_terminal",
                        "aircraft","pnr"]
        }}
    },
    "required":["transit"]
}

TRANSIT_PROMPT = """
You are MyTourBazar's smart transit parser. The travel-agency owner may type extremely short, mixed, code-only, or line-by-line travel sectors.
Interpret the message intelligently while preserving ONLY details actually supplied.

IMPORTANT INPUT BEHAVIOR:
- The owner does NOT need to write Onward, Return, Transit, Connection, Journey, a colon, a pipe, or any other prefix.
- Every non-empty line may represent one flight/train sector. Read the lines in the exact order supplied.
- The owner may also put multiple sectors in one line or sentence; still extract every real sector.
- IATA codes alone are valid locations. Full city/airport names are not required.

Examples you MUST understand:
RPR DEL AI1729 12:20 14:35
DEL DXB EK511 18:30 21:00
DXB DEL EK510 03:30 08:25
DEL RPR AI1730 10:20 12:05

DEL 6:30 RPR 8:15 AI1729
AI1729 DEL T2 06:30 RPR 08:15
RPR BOM 6E594 10:20 12:10
BOM RAJ 6E273 15:00 16:10
Train 12442 DEL 21:00 RPR 08:30 next day

Read ALL supplied text/files/pages together. Extract EVERY actual flight or train sector in journey order.
Recognize one-way, round-trip, connections and multi-sector journeys without requiring labels.

Journey classification when labels are absent:
- The first sector is normally Onward.
- A later sector that continues from the previous arrival point in the same outbound direction is Connection.
- If later lines form a reversed/homebound chain toward the original starting point, infer Return from the route sequence, dates and times.
- For a return chain with connections, the first homebound sector may be Return and subsequent connected homebound sectors may be Connection; preserve the actual line order.
- If the owner explicitly uses Onward/Return/Connection words, respect them, but never require them.

For each sector preserve when available: date, carrier/operator, service/flight/train number, from, to, departure time, arrival time, airport/station text, terminals, aircraft and PNR/reference.

Rules:
- segment_mode = Flight for flights, Train for trains.
- Normalize times to readable HH:MM where possible, but never invent a time.
- Keep airport codes exactly usable even if city names are absent.
- If a terminal is supplied, NEVER drop it and attach it to the correct endpoint.
- For trains, preserve the exact supplied train/service name in carrier. Never change a named train to "Indian Railways"; if no train name is supplied, carrier may be empty.
- Never invent aircraft, terminal, PNR, date, carrier or service number.
- Never merge separate sectors; return one object per sector.
- Deduplicate exact duplicates.
- Return an empty transit array only when no real sector can be understood.
Return ONLY JSON matching the schema.
"""

def _dedupe_transit(rows):
    out, seen = [], set()
    for row in rows or []:
        row = dict(row or {})
        key = tuple(str(row.get(k) or "").strip().lower() for k in
                    ("date","carrier","flight_number","from","to","departure","arrival"))
        if key in seen:
            continue
        seen.add(key); out.append(row)
    return out


def _extract_supplier_inclusion_exclusion_lists(source_text):
    """Recover explicit supplier bullet lists from locally extracted PDF text."""
    result={'inclusions':[],'exclusions':[]}
    current=None
    stop_headings=re.compile(
        r'(?i)^(?:hotel|accommodation|itinerary|day\s*\d+|package\s+cost|costing|price|'
        r'terms(?:\s*&\s*conditions)?|cancellation|payment|notes?|important)\s*:?[\s-]*$'
    )
    for raw_line in str(source_text or '').splitlines():
        line=re.sub(r'\s+',' ',raw_line).strip()
        heading=re.sub(r'[^A-Za-z ]',' ',line).strip().lower()
        if re.fullmatch(r'(?:package )?inclusions?',heading):
            current='inclusions'; continue
        if re.fullmatch(r'(?:package )?exclusions?|not included',heading):
            current='exclusions'; continue
        if current and stop_headings.fullmatch(line):
            current=None; continue
        if not current or not line:
            continue
        item=re.sub(r'^(?:[•●▪◦✓✔✘❌*\-–—]+|\d+[.)])\s*','',line).strip()
        if item==line and len(item)>180:
            continue
        if len(item)<3 or len(item)>260:
            continue
        if re.fullmatch(r'(?i)(?:yes|no|included|excluded|n/?a)',item):
            continue
        if item not in result[current]:
            result[current].append(item)
    return result


def _ensure_generated_inclusion_exclusion_lists(data):
    """Guarantee professional lists when the supplier provides none."""
    data=data or {}
    if not (data.get('inclusions') or []):
        inc=[]
        hotels=data.get('hotels') or data.get('accommodation') or []
        for h in hotels:
            place=str(h.get('destination') or '').strip()
            hotel=str(h.get('hotel_name') or '').strip()
            meal=str(h.get('meal_plan') or '').strip()
            label='Accommodation'
            if hotel and place: label=f'Accommodation at {hotel}, {place}'
            elif hotel: label=f'Accommodation at {hotel}'
            elif place: label=f'Accommodation in {place}'
            if meal: label += f' with {meal} meal plan'
            if label not in inc: inc.append(label)
        vehicle=str(data.get('vehicle') or '').strip()
        if vehicle:
            inc.append(f'Transfers and sightseeing by {vehicle} as per the itinerary')
        if data.get('days'):
            inc.append('Sightseeing and excursions specifically mentioned in the day-wise itinerary')
        if data.get('pickup') or data.get('drop'):
            inc.append('Pickup and drop arrangements as mentioned in the itinerary')
        if data.get('transit'):
            inc.append('Confirmed journey sectors specifically listed in the itinerary')
        data['inclusions']=inc or ['Services specifically mentioned in the day-wise itinerary']

    if not (data.get('exclusions') or []):
        exc=[
            'Personal expenses such as tips, laundry, telephone calls and room service',
            'Meals and refreshments not specifically mentioned in the itinerary',
            'Entry tickets, activity charges and guide fees unless specifically included',
            'Optional activities and services not expressly mentioned under inclusions',
            'Any expense arising due to weather, road conditions, delays or circumstances beyond our control',
        ]
        if not (data.get('transit') or []):
            exc.insert(0,'Airfare, train fare or bus fare unless specifically included')
        data['exclusions']=exc
    return data

def extract_transit_from_parts(file_parts, source_text, api_key, model):
    client = genai.Client(api_key=api_key)
    contents = [TRANSIT_PROMPT]
    if source_text:
        contents.append("\nUNSTRUCTURED TRANSIT TEXT:\n" + str(source_text))
    opened=[]
    try:
        for item in file_parts:
            path=Path(item["path"])
            contents.append(types.Part.from_bytes(data=path.read_bytes(), mime_type=item["mime_type"]))
            opened.append(str(path))
        response=call_with_high_demand_retry(lambda: client.models.generate_content(
            model=model, contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=TRANSIT_SCHEMA, temperature=0)))
        if not response.text:
            raise RuntimeError("Gemini returned an empty transit response.")
        result=json.loads(response.text)
        result["transit"]=_dedupe_transit(result.get("transit") or [])
        return _ensure_generated_inclusion_exclusion_lists(result)
    finally:
        for path in opened:
            try: Path(path).unlink(missing_ok=True)
            except Exception: pass


def extract_itinerary_from_parts(file_parts, source_text, api_key, model):
    client = genai.Client(api_key=api_key)

    contents = [SYSTEM_PROMPT, "\nTASK: Extract and intelligently complete the customer-facing itinerary. The guest name supplied by the bot will be applied separately and must be treated as authoritative."]
    if source_text:
        contents.append("\nSOURCE TEXT:\n" + source_text)

    opened = []
    try:
        for item in file_parts:
            path = Path(item["path"])
            data = path.read_bytes()
            part = types.Part.from_bytes(
                data=data,
                mime_type=item["mime_type"]
            )
            contents.append(part)
            opened.append(str(path))

        response = call_with_high_demand_retry(lambda: client.models.generate_content(
            model=model,
            contents=contents,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SCHEMA,
                temperature=0,
            ),
        ))

        if not response.text:
            raise RuntimeError("Gemini returned an empty response.")

        result=json.loads(response.text)
        # Deterministic safety net for supplier documents with explicit heading lists.
        # This preserves source items if the model returns either array empty.
        supplier_lists=_extract_supplier_inclusion_exclusion_lists(source_text)
        for key in ('inclusions','exclusions'):
            result.setdefault(key,[])
            existing={re.sub(r'\W+',' ',str(x)).strip().lower() for x in result[key]}
            for item in supplier_lists[key]:
                norm=re.sub(r'\W+',' ',item).strip().lower()
                if norm and norm not in existing:
                    result[key].append(item); existing.add(norm)
        return result
    finally:
        # Keep generated PDFs, but remove temporary supplier uploads after processing.
        for path in opened:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
