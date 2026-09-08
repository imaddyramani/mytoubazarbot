import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from ai_provider import complete_json
from identity_guard import best_source_name
from local_tour_planner import attractive_title, infer_destination

LOGGER = logging.getLogger('mytourbazar.extractor')

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
    def compact(items,limit):
        out=[]; seen=set()
        for value in items or []:
            value=re.sub(r'\s+',' ',str(value or '')).strip(' •-–—')
            if not value: continue
            if len(value)>150: value=value[:147].rsplit(' ',1)[0].rstrip(' ,;:')+'…'
            key=re.sub(r'\W+',' ',value).strip().lower()
            if key and key not in seen: seen.add(key); out.append(value)
            if len(out)>=limit: break
        return out
    data['inclusions']=compact(data.get('inclusions'),8)
    data['exclusions']=compact(data.get('exclusions'),6)
    return data


def _money_value(value):
    try:
        return str(int(float(str(value).replace(',','').strip())))
    except Exception:
        return ''


def _extract_supplier_package_costs(source_text):
    """Strict local recovery for common selectable-PDF Tour costing tables."""
    text=str(source_text or '')
    fields={}
    labels={
        'per_adult':r'(?:per\s+adult|adult(?:\s+cost|\s+rate|\s+price)?|adt)',
        'per_child_cwb':r'(?:child\s+with\s+bed|child\s+w/?\s*bed|cwb)',
        'per_child_cnb':r'(?:child\s+(?:without|no)\s+bed|child\s+w/?o\s*bed|cnb)',
        'per_extra_bed':r'(?:extra\s+bed|extra\s+mattress|eb)',
        'per_child':r'(?:per\s+child|child(?:\s+cost|\s+rate|\s+price)?)',
        'total_cost':r'(?:grand\s+total|package\s+(?:total|cost|price)|total\s+(?:package\s+)?cost|net\s+payable)',
    }
    amount_pat=r'(?:INR|Rs\.?|₹)?\s*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{3,8})(?:\.\d{1,2})?'
    for key,label in labels.items():
        pattern=r'(?im)^\s*(?:'+label+r')\s*(?:[:=|\-]|INR|Rs\.?|₹)\s*'+amount_pat+r'\s*(?:/-)?\s*$'
        m=re.search(pattern,text)
        if m:
            value=_money_value(m.group(1))
            if value: fields[key]=value

    lines=[re.sub(r'\s+',' ',x).strip() for x in text.splitlines() if x.strip()]
    for i,line in enumerate(lines[:-1]):
        ordered=[]
        for key,label in labels.items():
            hit=re.search(label,line,re.I)
            if hit: ordered.append((hit.start(),key))
        if len(ordered)>=2:
            nums=re.findall(amount_pat,lines[i+1],re.I)
            ordered.sort()
            if len(nums)>=len(ordered):
                for (_,key),num in zip(ordered,nums):
                    fields.setdefault(key,_money_value(num))

    # Common quotation line: ``Total Package Cost (Deluxe) INR 71,000/-
    # PER COUPLE ...``.  The older strict line matcher rejected the optional
    # package name and the explanatory suffix.
    total_line=re.search(
        r'(?im)^\s*(?:grand\s+total|total\s+package\s+cost|package\s+(?:total|cost|price)|net\s+payable)'
        r'\s*(?:\(([^)]+)\))?\s*(?:[:=|\-])?\s*(?:INR|Rs\.?|₹)\s*'
        r'([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{3,8})(?:\.\d{1,2})?\s*(?:/-)?\s*([^\n]*)$',text)
    option='Package'; notes='Recovered from supplier costing table'
    if total_line:
        fields['total_cost']=_money_value(total_line.group(2))
        option=(total_line.group(1) or 'Package').strip()
        suffix=re.sub(r'\s+',' ',total_line.group(3) or '').strip()
        if suffix: notes=suffix

    if not fields:
        return []
    total=fields.get('total_cost','')
    return [{
        'option':option,'per_adult':fields.get('per_adult',''),
        'per_child':fields.get('per_child',''),'per_child_cwb':fields.get('per_child_cwb',''),
        'per_child_cnb':fields.get('per_child_cnb',''),'per_extra_bed':fields.get('per_extra_bed',''),
        'total_cost':total,'currency':'INR','notes':notes,
        'supplier_total':total,'markup_total':'','final_total':''
    }]

def extract_transit_from_parts(file_parts, source_text, api_key, model):
    from flight_extractor import extract_flight_ticket
    flight=extract_flight_ticket(file_parts,source_text,api_key,model); rows=[]
    for segment in flight.get('segments') or []:
        origin=segment.get('dep_code') or segment.get('dep_city') or ''
        destination=segment.get('arr_code') or segment.get('arr_city') or ''
        rows.append({'date':segment.get('dep_date') or '','segment_mode':'Flight','journey_type':'Flight',
            'carrier':segment.get('flight') or '','flight_number':segment.get('flight_number') or '',
            'route':f'{origin} → {destination}'.strip(' →'),'from':segment.get('dep_city') or origin,
            'to':segment.get('arr_city') or destination,'departure':segment.get('dep_time') or '',
            'arrival':segment.get('arr_time') or '','from_airport':segment.get('dep_airport') or '',
            'to_airport':segment.get('arr_airport') or '','departure_terminal':segment.get('dep_terminal') or '',
            'arrival_terminal':segment.get('arr_terminal') or '','aircraft':segment.get('aircraft') or '',
            'pnr':flight.get('airline_pnr') or flight.get('gds_pnr') or ''})
    return {'transit':_dedupe_transit(rows)}


def _source_days(text):
    """Keep complete explicit supplier days outside the AI token budget."""
    pattern=r'(?im)^[ \t]*Day[ \t]+(\d{1,3})[ \t]*[:.\-–]?[ \t]*([^\n]*)'
    matches=list(re.finditer(pattern,text)); days=[]; compact=[]; previous=0; seen=set()
    for i,match in enumerate(matches):
        number=str(int(match.group(1)))
        if number in seen:
            continue
        seen.add(number)
        end=matches[i+1].start() if i+1<len(matches) else len(text)
        block=text[match.end():end]
        boundary=re.search(
            r'(?im)^\s*(?:---\s*FILE\b.*|(?:package\s+)?inclusions?\b.*|(?:package\s+)?exclusions?\b.*|'
            r'flight\s+details\b.*|package\s+cost\b.*|terms\s+and\s+conditions\b.*|hotel\s+details\b.*|notes?\s*[:-].*)$',block)
        if boundary: end=match.end()+boundary.start()
        heading=re.sub(r'\s+',' ',match.group(2)).strip()
        date_match=re.search(r'\b(\d{1,2}\s+[A-Za-z]{3,9}\s+\d{4})\b',heading)
        date=date_match.group(1) if date_match else ''
        if date_match:
            title=heading[:date_match.start()].strip(' :-–—')
            opening=heading[date_match.end():].strip(' :-–—')
        else:
            title=heading; opening=''
        description=' '.join(x for x in (opening,text[match.end():end].strip()) if x)
        description=re.sub(r'\s+',' ',description).strip()
        days.append({'day':number,'date':date,'title':title,
                     'description':description})
        compact.append(text[previous:match.start()])
        compact.append(f"Day {match.group(1)}: {match.group(2)}\n[Full day description preserved locally]\n")
        # Keep accommodation, meals, transfer and cost evidence for structuring.
        for line in text[match.end():end].splitlines():
            if re.search(r'(?i)hotel|room|night|meal|breakfast|dinner|transfer|pickup|drop|INR|₹|Rs\.',line):
                compact.append(line+'\n')
        previous=end
    compact.append(text[previous:])
    return days,''.join(compact)


def _local_day_itinerary(source_days):
    """Keep an explicit supplier itinerary usable without rebuilding its days."""
    days=[]
    for item in source_days:
        day=dict(item)
        day.setdefault('date','')
        day.update({'stay':'','meal_plan':'','optional_activities':[]})
        days.append(day)
    return {
        'client_name':'','tour_title':'','destination':'','travel_dates':'',
        'duration':f'{len(days)} Days' if days else '','guests':'',
        'adult_count':0,'child_count':0,'child_cwb_count':0,
        'child_cnb_count':0,'extra_bed_count':0,'vehicle':'',
        'pickup':'','drop':'','transit':[],'hotels':[],'days':days,
        'inclusions':[],'exclusions':[],'policies':'','greeting':'',
        'accommodation_heading':'Accommodation','package_costs':[],
        '_ai_fallback_used':False,
    }


def _label_value(text, labels, max_len=120):
    joined='|'.join(labels)
    # Horizontal whitespace is intentional. ``\s`` crossed into the next line
    # when an explicit label was blank, making a PDF page marker the guest name.
    match=re.search(r'(?im)^[ \t]*(?:'+joined+r')[ \t]*[:\-][ \t]*([^\n]{1,'+str(max_len)+r'})[ \t]*$',str(text or ''))
    value=re.sub(r'\s+',' ',match.group(1)).strip() if match else ''
    if re.search(r'(?i)(?:LOCAL SELECTABLE PDF TEXT|---\s*FILE\b|\.pdf\s*/\s*PAGE)',value):
        return ''
    return value


def _dated_label_value(text,label):
    match=re.search(r'(?im)^[ \t]*'+label+r'[ \t]*:[ \t]*(\d{1,2}(?:/|-|\s)[A-Za-z]{3,9}(?:/|-|\s)\d{4})\b',str(text or ''))
    return match.group(1).strip() if match else ''


def _parse_supplier_date(value):
    value=str(value or '').strip()
    for fmt in ('%d/%b/%Y','%d %b %Y','%d-%b-%Y','%d/%m/%Y','%d-%m-%Y'):
        try: return datetime.strptime(value,fmt)
        except ValueError: pass
    return None


def _split_layout_columns(line):
    return [x.strip() for x in re.split(r'\s{2,}',str(line or '').strip()) if x.strip()]


def _local_hotels(text,days):
    lines=[re.sub(r'\s+',' ',x).strip() for x in str(text or '').splitlines() if x.strip()]
    rows=[]
    for index,line in enumerate(lines):
        match=re.match(r'(?i)^(?:hotel|resort|property)(?:\s+name)?\s*[:\-]\s*(.+)$',line)
        if not match: continue
        window='\n'.join(lines[max(0,index-4):min(len(lines),index+7)])
        room=_label_value(window,(r'room\s+(?:category|type)',r'category'))
        rows.append({'dates':_label_value(window,(r'dates?',r'check[ -]?in')),
            'destination':_label_value(window,(r'destination',r'city',r'location')),
            'hotel_name':match.group(1).strip(),'room_category':room,
            'hotel_category':_label_value(window,(r'hotel\s+category',r'star\s+category')),
            'rooms':_label_value(window,(r'total\s+rooms?',r'rooms?',r'rooming')),'room_type':room,
            'meal_plan':_label_value(window,(r'meal\s+plan',r'meals?',r'plan')),'option':'Option 1'})
    # Recover the very common supplier table layout:
    # Destination | Hotel | Room Category | Nights | Meal Plan | No of Rooms.
    # Work from the layout-preserved raw lines; a label-only parser cannot read it.
    if not rows:
        raw_lines=str(text or '').splitlines()
        header_index=next((i for i,line in enumerate(raw_lines)
                           if re.search(r'(?i)\bdestination\b',line)
                           and re.search(r'(?i)\bhotels?\b',line)
                           and re.search(r'(?i)\broom\s+category\b',line)),None)
        start=_dated_label_value(text,r'(?:tour\s+)?start(?:ing)?\s+date')
        cursor=_parse_supplier_date(start)
        if header_index is not None:
            for line in raw_lines[header_index+1:]:
                if re.search(r'(?i)^\s*(?:transportation|vehicle|total\s+package|detailed\s+tour)',line): break
                cells=_split_layout_columns(line)
                if len(cells)<6 or not re.fullmatch(r'\d{1,2}',cells[-3]): continue
                destination,hotel,room,nights,meal,room_count=cells[0],cells[1],cells[-4],cells[-3],cells[-2],cells[-1]
                if not (destination and hotel): continue
                night_count=int(nights)
                dates=''
                if cursor:
                    checkout=cursor+timedelta(days=night_count)
                    dates=f"{cursor.strftime('%d %b %Y')} – {checkout.strftime('%d %b %Y')}"
                    cursor=checkout
                rows.append({'dates':dates,'destination':destination,'hotel_name':hotel,
                    'room_category':room,'hotel_category':'','rooms':f'{room_count} Rooms',
                    'room_type':room,'meal_plan':meal.title(),'option':'Option 1','nights':str(night_count)})
    if not rows:
        for stay in dict.fromkeys(str(x.get('stay') or '').strip() for x in days):
            if stay: rows.append({'dates':'','destination':stay,'hotel_name':'','room_category':'','hotel_category':'','rooms':'','room_type':'','meal_plan':'','option':'Option 1'})
    return rows


def _numbered_items(lines):
    items=[]; current=''
    for raw in lines:
        value=re.sub(r'\s+',' ',str(raw or '')).strip()
        if not value: continue
        match=re.match(r'^\d+[.)]\s*(.*)$',value)
        if match:
            if current: items.append(current.strip())
            current=match.group(1).strip()
        elif current:
            current+=' '+value
    if current: items.append(current.strip())
    return [x for x in items if len(x)>2]


def _pdf_table_facts(file_parts):
    """Extract semantic table columns from selectable supplier PDFs.

    This supplements (not replaces) normal text parsing and remains optional when
    a PDF has no ruled/detectable table.
    """
    facts={'hotels':[],'inclusions':[],'exclusions':[]}
    try:
        import fitz
    except Exception:
        return facts
    for item in file_parts or []:
        path=Path(item.get('path') or '')
        if path.suffix.lower()!='.pdf' or not path.is_file(): continue
        try:
            with fitz.open(str(path)) as doc:
                for page in doc:
                    finder=getattr(page,'find_tables',None)
                    if not finder: continue
                    for table in finder().tables:
                        grid=table.extract() or []
                        if not grid: continue
                        normalized=[[re.sub(r'\s+',' ',str(c or '')).strip() for c in row] for row in grid]
                        header=normalized[0]
                        header_text=' | '.join(header).lower()
                        if 'inclusions' in header_text and 'exclusions' in header_text:
                            inc_index=next(i for i,c in enumerate(header) if 'inclusion' in c.lower())
                            exc_index=next(i for i,c in enumerate(header) if 'exclusion' in c.lower())
                            left=[' '.join(c for c in row[inc_index:exc_index] if c) for row in normalized[1:]]
                            right=[' '.join(c for c in row[exc_index:] if c) for row in normalized[1:]]
                            facts['inclusions'].extend(_numbered_items(left))
                            facts['exclusions'].extend(_numbered_items(right))
        except Exception as exc:
            LOGGER.warning('Optional PDF table recovery failed for %s: %s',path.name,exc)
    return facts


def _local_transit(text):
    rows=[]
    def clean_time(value):
        value=str(value or '').replace('.',':').strip()
        match=re.fullmatch(r'(\d{1,2}):(\d{2})(AM|PM)',value,re.I)
        if match and int(match.group(1))>12:
            return f'{int(match.group(1)):02d}:{match.group(2)}'
        return value
    pattern=(r'(?im)^\s*\d+[.)]\s*([A-Za-z]{3,30}|[A-Z]{3})\s+TO\s+([A-Za-z]{3,30}|[A-Z]{3})\s+'
             r'(\d{1,2}\s+[A-Za-z]{3}\s+\d{4})\s+([A-Za-z][A-Za-z ]{1,20})\s+'
             r'(\d{1,2}[.:]\d{2}\s*(?:AM|PM)?)\s*[-–]\s*(\d{1,2}[.:]\d{2}\s*(?:AM|PM)?)(?:\s+VIA\s+([^\n]+))?')
    for match in re.finditer(pattern,str(text or '')):
        origin,destination,date,carrier,departure,arrival,via=(x.strip() if x else '' for x in match.groups())
        route=f'{origin} → {destination}' + (f' via {via}' if via else '')
        rows.append({'date':date,'segment_mode':'Flight','journey_type':'Flight','carrier':carrier.title(),
            'flight_number':'','route':route,'from':origin,'to':destination,'departure':clean_time(departure),
            'arrival':clean_time(arrival),'from_airport':'','to_airport':'','departure_terminal':'',
            'arrival_terminal':'','aircraft':'','pnr':''})
    return rows


def _local_tour_result(text,source_days,file_parts=None):
    result=_local_day_itinerary(source_days)
    result['client_name']=_label_value(text,(r'authoritative\s+guest\s*/\s*client\s+name',r'guest\s+name',r'client\s+name',r'lead\s+guest'))
    destination,key=infer_destination(text); result['destination']=destination
    result['tour_title']=attractive_title(destination,key)
    result['travel_dates']=_label_value(text,(r'travel\s+dates?',r'tour\s+dates?',r'dates?'))
    if not result['travel_dates']:
        start=_dated_label_value(text,r'(?:tour\s+)?start(?:ing)?\s+date')
        end_match=re.search(r'(?i)\b(?:tour\s+)?end(?:ing)?\s+date\s*:\s*(\d{1,2}(?:/|-|\s)[A-Za-z]{3,9}(?:/|-|\s)\d{4})\b',str(text or ''))
        end=end_match.group(1).strip() if end_match else ''
        if start and end: result['travel_dates']=f'{start} – {end}'
    result['duration']=_label_value(text,(r'duration',r'tour\s+duration'))
    if not result['duration']:
        match=re.search(r'(?i)\b(\d{1,2})\s*nights?\s*(?:and|&|/)?\s*(\d{1,2})\s*days?\b',text)
        result['duration']=f'{match.group(1)} Nights and {match.group(2)} Days' if match else (f'{len(source_days)} Days' if source_days else '')
    result['vehicle']=_label_value(text,(r'vehicle(?:\s+type)?',r'transport(?:ation)?'))
    if not result['vehicle']:
        vehicle_match=re.search(r'(?im)^[ \t]*(?:transportation|vehicle(?:\s+type)?)[ \t]{2,}([^\n]{2,80})$',str(text or ''))
        if vehicle_match: result['vehicle']=re.sub(r'\s+',' ',vehicle_match.group(1)).strip()
    result['pickup']=_label_value(text,(r'pick[ -]?up(?:\s+point|\s+hub)?',)); result['drop']=_label_value(text,(r'drop(?:\s+point|\s+hub)?',))
    patterns={'adult_count':r'(\d+)\s*(?:adults?|adt)\b','child_cwb_count':r'(\d+)\s*(?:cwb|child(?:ren)?\s+with\s+bed)\b','child_cnb_count':r'(\d+)\s*(?:cnb|child(?:ren)?\s+(?:without|no)\s+bed)\b','extra_bed_count':r'(\d+)\s*(?:extra\s+bed|eb)\b'}
    for field,pattern in patterns.items():
        match=re.search(pattern,text,re.I); result[field]=int(match.group(1)) if match else 0
    result['child_count']=result['child_cwb_count']+result['child_cnb_count']
    result['guests']=', '.join(x for x in (f"{result['adult_count']} Adult(s)" if result['adult_count'] else '',f"{result['child_cwb_count']} CWB" if result['child_cwb_count'] else '',f"{result['child_cnb_count']} CNB" if result['child_cnb_count'] else '') if x)
    for day in result['days']:
        body=str(day.get('description') or '')
        stay=re.search(r'(?i)(?:overnight|stay)\s+(?:at|in)\s+([^\n.;]{2,80})',body)
        meal=re.search(r'(?i)\b(?:meal\s*plan|meals?)\s*[:\-]\s*([^\n.;]{2,50})',body)
        if stay: day['stay']=stay.group(1).strip()
        if meal: day['meal_plan']=meal.group(1).strip()
    result['hotels']=_local_hotels(text,result['days'])
    for day in result['days']:
        title=str(day.get('title') or '').lower()
        hotel=next((h for h in result['hotels'] if str(h.get('destination') or '').lower() in title),None)
        if hotel and 'airport' not in title:
            day['stay']=hotel.get('destination') or ''
            day['meal_plan']=hotel.get('meal_plan') or day.get('meal_plan') or ''
        elif not day.get('meal_plan') and re.search(r'(?i)\bafter\s+breakfast\b',str(day.get('description') or '')):
            day['meal_plan']='Breakfast'
    result['transit']=_local_transit(text)
    result['package_costs']=_extract_supplier_package_costs(text)
    result.update(_extract_supplier_inclusion_exclusion_lists(text))
    table_facts=_pdf_table_facts(file_parts)
    if table_facts.get('inclusions'): result['inclusions']=table_facts['inclusions']
    if table_facts.get('exclusions'): result['exclusions']=table_facts['exclusions']
    note=re.search(r'(?is)\bNOTE\s*[:-]\s*(.*?)(?:Hope all of above|\Z)',str(text or ''))
    if note: result['policies']=re.sub(r'\s+',' ',note.group(1)).strip()
    return _ensure_generated_inclusion_exclusion_lists(result)


def _merge_local_fallback_into_ai_package(remote, local, source_text=''):
    """Keep Qwen's structured itinerary and backfill only genuinely blank facts."""
    result=dict(remote or {}); local=local or {}
    result['client_name']=best_source_name(result.get('client_name'),local.get('client_name'),source_text)
    for key,value in local.items():
        if key in ('days','hotels','inclusions','exclusions','transit','package_costs'):
            continue
        if result.get(key) in ('',None,0,[]) and value not in ('',None,0,[]):
            result[key]=value
    for key in ('inclusions','exclusions','transit','package_costs'):
        if not result.get(key) and local.get(key): result[key]=local[key]
    for key in ('days','hotels'):
        remote_rows=list(result.get(key) or []); local_rows=list(local.get(key) or [])
        if key=='hotels' and any(str(x.get('hotel_name') or '').strip() for x in local_rows):
            # A locally recovered supplier table is authoritative and complete;
            # do not let a partial model response collapse four hotel rows to one.
            result[key]=local_rows
            continue
        if not remote_rows:
            result[key]=local_rows; continue
        local_by_day={str(x.get('day') or '').strip():x for x in local_rows if isinstance(x,dict)} if key=='days' else {}
        for index,row in enumerate(remote_rows):
            fallback=(local_by_day.get(str((row or {}).get('day') or '').strip()) if key=='days' else None) or (local_rows[index] if index<len(local_rows) else {})
            if not isinstance(row,dict): continue
            for field,value in (fallback or {}).items():
                if row.get(field) in ('',None,0,[]) and value not in ('',None,0,[]): row[field]=value
        result[key]=remote_rows
        if key=='days':
            present={str((x or {}).get('day') or '').strip() for x in remote_rows}
            result[key].extend(x for x in local_rows if str((x or {}).get('day') or '').strip() not in present)
    # A document model may repeat pages or rows.  Keep the first complete instance
    # of each numbered day and each accommodation identity.
    unique_days=[]; seen_days=set()
    for row in result.get('days') or []:
        number=str((row or {}).get('day') or '').strip()
        if number and number in seen_days: continue
        if number: seen_days.add(number)
        unique_days.append(row)
    result['days']=unique_days
    unique_hotels=[]; seen_hotels=set()
    for row in result.get('hotels') or []:
        key=(str((row or {}).get('destination') or '').strip().lower(),str((row or {}).get('hotel_name') or '').strip().lower())
        if key!=('','') and key in seen_hotels: continue
        if key!=('',''): seen_hotels.add(key)
        unique_hotels.append(row)
    result['hotels']=unique_hotels
    result['_ai_primary_used']=True
    return _ensure_generated_inclusion_exclusion_lists(result)


def extract_itinerary_from_parts(file_parts, source_text, api_key, model):
    from performance_utils import collect_complete_supplier_text
    source_text=collect_complete_supplier_text(file_parts,source_text)
    source_days,_=_source_days(source_text)
    if not source_days:
        matches=list(re.finditer(r'(?im)^\s*(\d{1,2})[.)]\s+([^\n]+)',source_text))
        source_days=[{'day':m.group(1),'title':m.group(2).strip(),'description':m.group(2).strip()} for m in matches]
    remote=complete_json(
        SYSTEM_PROMPT,
        "Extract and professionally structure this supplier Tour. Preserve every explicit supplier fact and "
        "return the complete schema. SOURCE PRESENT = COPY; SOURCE ABSENT = BLANK.\n\n"+source_text,
        SCHEMA,purpose='tour supplier extraction',max_tokens=9000,
        image_paths=[item.get('path') for item in (file_parts or []) if item.get('path')],
    )
    local=_local_tour_result(source_text,source_days,file_parts)
    if remote:
        return _merge_local_fallback_into_ai_package(remote,local,source_text)
    return local
