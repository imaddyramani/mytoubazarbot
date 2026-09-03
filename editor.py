import json
import re
from google import genai
from google.genai import types
from ai_retry import call_with_high_demand_retry



def _restore_named_train_carriers(updated_data, instruction):
    # Local safety guard: never replace an owner-supplied train name with a generic label.
    if not isinstance(updated_data, dict):
        return updated_data
    raw = str(instruction or "")
    names = []
    patterns = [
        r"(?i)\b([A-Za-z0-9.&' -]{2,60}?(?:Express|Mail|Superfast|Intercity|Passenger|Special))\b",
        r"(?i)\b(Vande Bharat|Rajdhani(?: Express)?|Shatabdi(?: Express)?|Duronto(?: Express)?|Humsafar(?: Express)?|Tejas(?: Express)?|Jan Shatabdi|Garib Rath|Sampark Kranti|Gatimaan Express)\b",
    ]
    for pattern in patterns:
        for m in re.finditer(pattern, raw):
            name = " ".join(m.group(1).split()).strip(" ,.-")
            if name and name.lower() not in [x.lower() for x in names]:
                names.append(name)
    rows = updated_data.get("transit")
    if not isinstance(rows, list) or not names:
        return updated_data
    generic = {"", "indian railways", "railways", "railway", "train"}
    name_index = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("segment_mode") or "").strip().lower() != "train":
            continue
        carrier = str(row.get("carrier") or "").strip().lower()
        if carrier in generic and name_index < len(names):
            row["carrier"] = names[name_index]
            name_index += 1
    return updated_data


def _preserve_untargeted_tour_days(current_data, updated_data, instruction):
    """Apply a day-specific AI edit as a patch without losing other tour days."""
    old_days=list((current_data or {}).get('days') or [])
    if not old_days or not isinstance(updated_data,dict):
        return updated_data
    targets={int(x) for x in re.findall(r'(?i)\bday\s*(\d{1,2})\b',str(instruction or ''))}
    targets={x for x in targets if 1 <= x <= len(old_days)}
    if not targets:
        return updated_data
    ai_days=list(updated_data.get('days') or [])
    merged=[dict(x or {}) for x in old_days]
    ordered=sorted(targets)
    for target in ordered:
        chosen=None
        for row in ai_days:
            m=re.search(r'\d+',str((row or {}).get('day') or ''))
            if m and int(m.group())==target:
                chosen=row; break
        if chosen is None and len(ai_days)==len(targets):
            chosen=ai_days[ordered.index(target)]
        if isinstance(chosen,dict):
            base=dict(merged[target-1]); base.update(chosen); merged[target-1]=base
    updated_data['days']=merged
    return updated_data

def apply_edit(doc_type, current_data, instruction, api_key, model, current_fare=None):
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not configured in .env")

    type_names = {
        "package": "Tour Itinerary",
        "flight": "Air Ticket Itinerary",
        "bus": "Bus Ticket Itinerary",
        "hotel": "Hotel Confirmation Voucher",
    }
    name = type_names.get(doc_type, doc_type)

    prompt = f"""
You are the document editor for MyTourBazar.
You are editing an existing {name} using a user's natural-language change request.

IMPORTANT RULES:
1. Return ONLY valid JSON in this exact wrapper format:
{{"updated_data": <complete updated data object>, "updated_fare": <number or null>}}
2. Return the COMPLETE data object, not a partial patch.
3. Make ONLY the requested changes. Preserve every other existing fact exactly.
4. Do not invent missing bookings, dates, flight numbers, hotels, prices, times, passengers,
   sightseeing, inclusions, or exclusions.
5. For a Tour Itinerary, the user may change any day plan, day date/title/description,
   hotel/stay, meal plan, logistics, greeting, title, inclusions, exclusions, customer costing,
   or any number of transit sectors. TOUR COSTING IS DIRECT CUSTOMER SELLING COST - there is no
   markup workflow. Understand natural wording such as "adult 43700", "make adult forty three
   thousand seven hundred", "CWB should be 32000", "child without bed 26000", or mixed cost +
   hotel/transit instructions. Update only the intended per_adult/per_child/per_child_cwb/
   per_child_cnb/per_extra_bed fields, set show_cost=true when a customer cost is added/changed,
   and preserve unrelated rates. Never calculate a markup from hidden supplier pricing. If the user
   says to increase/decrease an already-visible customer rate, apply that relative change to the
   existing customer rate only. Transit may mix flights, trains and buses in one natural message
   or voice transcription. Infer the journey sequence from the described route order; no Onward/Return
   prefix is required. Preserve every sector as a separate transit object and do not drop connections.
   TRAIN NAME RULE: when the owner supplies a specific train/service name, put that exact supplied name
   in the transit carrier field. Never replace a supplied train name with the generic text 'Indian Railways'.
   If only a train number is supplied and no train name is supplied, keep carrier empty rather than inventing one.
6. For Flight or Bus documents, the user may change any passenger, route, timing, service,
   PNR, baggage, or fare information. If the user asks to change the fare, put the new total
   in updated_fare and leave the supplier base_fare/taxes as historical source values unless
   the request explicitly asks to change those source values.
7. For Hotel documents, the user may change guest, hotel, room, dates, meal plan, address,
   reservation details, terms, or CUSTOMER HOTEL COSTING. When the owner gives a customer hotel
   selling cost naturally by text/voice, store it in updated_data.customer_hotel_cost using
   {{"per_room": number-or-null, "eb": number-or-null, "total": number, "currency": "INR"}}.
   Understand normal wording such as "per room 8500", "room cost 8500, extra bed 1200, total 18200",
   or mixed hotel+cost changes. Do not overwrite supplier cost_components/base_fare/taxes. The Hotel
   print uses its structured room-cost/GRAND TOTAL element and does not use a generic Total Fare box.
7A. ACCOMMODATION ROOM CATEGORY: for Tour hotel rows, room_type and room_category are
   important customer-facing facts and mean the actual room class/style, such as Premium, Standard,
   Deluxe, Non AC, Executive, Superior, Suite or a supplied view/category. Preserve both when both
   are present. Never put room counts into these fields, never replace them with a star rating, and
   never drop these fields while editing another part.
7B. TOTAL ROOMS / ROOMING: keep the room count/occupancy separately in hotels[].rooms. Understand
   natural edits such as "2 double rooms", "3 triple sharing", "2 dbl plus 1 extra mattress"
   and save a concise value such as "2 DBL", "3 Triple Sharing", or "2 DBL + 1 EB". Extra
   mattress/extra mat may be represented as EB in the rooming display. Never infer DBL/Triple/EB
   from guest count unless the owner explicitly says it.
7C. PREMIUM HOTEL STAR RULE: understand normal owner wording without any command prefix. If the
   owner says the hotels are `3 star premium`, preserve/apply hotel_category as `3 Star Premium`;
   `4 star premium` means `4 Star Premium`. These are rendered as 3 or 4 full golden stars plus
   one visual half star. Do not invent Premium and do not confuse a separate Premium room type
   with a premium hotel-star rating unless the owner actually describes the hotel rating that way.
8. If the user asks for a wording improvement to a day plan, rewrite only that day in polished
   travel-agency language while retaining the confirmed facts already present.
9. If the requested change is ambiguous, make the safest interpretation and do not alter unrelated data.

CURRENT DOCUMENT DATA:
{json.dumps(current_data, ensure_ascii=False, indent=2)}

CURRENT PRINTED FARE (if applicable): {current_fare if current_fare is not None else 'N/A'}

USER CHANGE REQUEST:
{instruction}
"""

    client = genai.Client(api_key=api_key)
    response = call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0,
        ),
    ))
    if not response.text:
        raise RuntimeError("Gemini returned an empty edit response.")
    result = json.loads(response.text)
    if not isinstance(result, dict) or not isinstance(result.get("updated_data"), dict):
        raise RuntimeError("Gemini returned an invalid edit response.")
    updated_data = result.get("updated_data")
    if doc_type == "package":
        updated_data = _preserve_untargeted_tour_days(current_data, updated_data, instruction)
        updated_data = _restore_named_train_carriers(updated_data, instruction)
    return updated_data, result.get("updated_fare")
