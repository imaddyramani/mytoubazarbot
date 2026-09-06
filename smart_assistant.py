import json
from pathlib import Path
from google import genai
from google.genai import types
from ai_retry import call_with_high_demand_retry
from extractor import SCHEMA as ITINERARY_SCHEMA

CLASS_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["package", "flight", "bus", "hotel", "edit", "chat", "unknown"]},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
        "reference": {"type": "string"},
        "instruction": {"type": "string"},
    },
    "required": ["kind", "confidence", "reason", "reference", "instruction"],
}

CLASSIFIER_PROMPT = """
You are the FIRST-RESPONSE intelligence layer for MyTourBazar. Understand the complete supplied
source and decide the dominant output type before extraction.

Return exactly one: package, flight, bus, hotel, edit, chat, unknown.

SOURCE TYPES: pasted supplier text, PDF, scanned PDF, screenshot/photo, or mixed sources.

RULES:
1. Supplier material is source data, not a new itinerary request.
2. PACKAGE DOMINANCE: a multi-day destination plan containing sightseeing/experiences plus
   accommodation, transfers/logistics, inclusions/exclusions or package costing is PACKAGE even
   when it contains flights or hotel confirmations. Those are tour components.
3. FLIGHT is a standalone airline/e-ticket booking with passenger, PNR, sectors, airline,
   times, terminal, baggage, fare/ticket information.
4. BUS is a standalone bus booking with operator, service/bus number, passenger, seat, boarding/drop,
   departure/arrival, PNR/ticket information.
5. HOTEL is a standalone hotel confirmation/voucher with property, reservation number, check-in/out,
   nights, rooms, guest, room type, meal plan/address/contact.
6. TOUR indicators include Day 1/Day 2, destination routing, sightseeing, accommodation schedule,
   transfers, inclusions/exclusions, package rates, per adult/CWB/CNB/EB, multiple package options.
7. A natural-language request to create/plan a tour is PACKAGE.
8. If an existing MyTourBazar reference or clear change request is present, classify EDIT and preserve
   the requested instruction.
9. Do not invent references. Return only JSON matching the schema.
"""

AGENT_PLAN_SCHEMA = {
    "type":"object",
    "properties":{
        "action":{"type":"string","enum":["generate_supplier","generate_brief","edit_document","chat","ask_user"]},
        "kind":{"type":"string","enum":["package","flight","bus","hotel","unknown"]},
        "reference":{"type":"string"},
        "instruction":{"type":"string"},
        "reason":{"type":"string"},
        "needs_user_input":{"type":"string"}
    },
    "required":["action","kind","reference","instruction","reason","needs_user_input"]
}

AGENT_PROMPT = """
You are the autonomous MyTourBazar AI Assistant. Understand the owner's intent and choose the best
existing bot operation even when no button or shortcut exists. You may combine several modifications
into one instruction. Never invent confirmed travel facts.

Actions:
generate_supplier = supplier source must be extracted as Air/Bus/Hotel/Tour.
generate_brief = owner wants a NEW tour/package/itinerary/quotation/voucher from natural language.
This includes short unstructured travel briefs with destination + duration/pax/services even when the
owner does NOT write "make", "create", "tour", "package" or any command prefix.
Examples:
- "Goa 4N 5D, Mr Amit, 2 adults, 3 star, breakfast, private cab, North and South Goa"
- "Kashmir quotation 5N/6D, 4 adults, 3 star hotels, breakfast, private vehicle"
- "Prepare detailed itinerary for Bali, 6 days, couple, breakfast, private transfers"
edit_document = owner wants an existing generated document changed. A real MTB reference such as MTB12
plus a change request must route here. Convert the request into a precise instruction for the document
editor; it may contain multiple changes.
generate_supplier = ONLY when actual supplier/source material must be extracted.
chat = normal question.
ask_user = only when an essential missing fact cannot reasonably be inferred.

Examples:
"make this B2B" -> remove MyTourBazar branding/logo/footer and apply configured B2B last page.
"use footer 2 and legal" -> change footer and page size.
"change hotel to option 2 and make it 4 star" -> update accommodation option/category.
"add 10000 markup and print" -> apply total markup and regenerate with final selling amount.
"add start date 20 Sep 2026" -> set tour start date and regenerate.
"make day 3 more detailed" -> edit Day 3 only while preserving all other facts.
Return only JSON matching the schema.
"""

def agent_plan(text, source_context, api_key, model):
    client=genai.Client(api_key=api_key)
    contents=[AGENT_PROMPT, "\nOWNER MESSAGE:\n"+str(text or "")[:20000]]
    if source_context:
        contents.append("\nCURRENT DOCUMENT/SOURCE CONTEXT:\n"+str(source_context)[:30000])
    response=call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model, contents=contents,
        config=types.GenerateContentConfig(response_mime_type='application/json',response_schema=AGENT_PLAN_SCHEMA,temperature=0.05)
    ))
    if not response.text: raise RuntimeError('AI assistant returned an empty decision.')
    return json.loads(response.text)


CHAT_PROMPT = """
You are MyTourBazar's AI travel-agency assistant inside Telegram.
Speak naturally and professionally, like a helpful Gemini-style assistant, but concise.
You can help the owner with itinerary creation, flight/bus/hotel prints, document editing,
travel wording, inclusions/exclusions, supplier-data interpretation, and bot workflow questions.
If the user is asking for a document operation, explain the next action briefly; do not pretend a PDF was created unless the bot actually performs it.
"""




def enhance_package_itinerary(current_data, api_key, model, detail_level="detailed"):
    """Rewrite day plans only; every non-day field remains local and immutable."""
    source=json.loads(json.dumps(current_data or {}))
    days=list(source.get('days') or [])
    requested=str(detail_level).lower()
    if requested == 'basic' and all(str(x.get('description') or '').strip() for x in days):
        for day in days:
            words=str(day.get('description') or '').split()
            day['description']=' '.join(words[:70]).strip()
            day['optional_activities']=[]
        source['days']=days
        source['detail_level']='basic'
        return source

    # A complete supplier narrative needs no AI rewriting. Preserve it exactly.
    if requested == 'detailed' and days and all(len(str(x.get('description') or '').split()) >= 90 for x in days):
        source['detail_level']='detailed'
        return source
    if not api_key:
        source['detail_level']=requested
        return source

    client = genai.Client(api_key=api_key)
    mode = "BASIC" if requested == "basic" else "DETAILED"
    day_schema={
        'type':'object','properties':{'days':{'type':'array','items':{
            'type':'object','properties':{
                'day':{'type':'string'},'title':{'type':'string'},
                'description':{'type':'string'},
                'optional_activities':{'type':'array','items':{'type':'string'}},
            },'required':['day','title','description','optional_activities']
        }}},'required':['days']
    }
    prompt = f"""
You are MyTourBazar's senior travel itinerary editor.
Write ONLY the day-wise tour plan at {mode} detail level.

FACT SAFETY:
- Use only the supplied day titles/descriptions and destination as factual evidence.
- Never invent a hotel, flight, booking, price or confirmed service.
- You may improve destination descriptions using normal travel knowledge.
- Optional activities are suggestions only and must never be presented as included/booked.

BASIC MODE:
- 35-70 words per day.
- Clearly name the principal sightseeing/experience.
- optional_activities must be [].

DETAILED MODE:
- 90-140 words per day. Keep it useful, polished and free of repetitive filler.
- Give every day a clear professional flow: arrival/start, morning, sightseeing sequence, afternoon, evening, hotel return/check-in and overnight stay wherever supported.
- Explain each confirmed attraction and experience meaningfully, including what the guest will see or do, without inventing booked services.
- Structure each day naturally as a professional travel planner would: morning/start of the day, sightseeing and experiences in logical sequence, afternoon, and evening/return or leisure where applicable.
- Name EVERY included sightseeing/attraction/place explicitly.
- For every included sightseeing, give a useful client-facing description: what the place is, what the guest will see/do there, and the main experience or highlight. Avoid generic filler.
- Explain the travel flow between the included places in a natural way when the supplied itinerary supports it.
- Mention meals, hotel/stay and included vehicle/transport in the appropriate part of the day when those facts are confirmed.
- Do NOT invent exact timings, distances, travel durations, ticket inclusions, closures, or booked activities. If timing is not supplied, use natural wording such as "after breakfast", "later", "in the afternoon", or "in the evening".
- Keep the writing polished, warm and customer-facing, like a professional tour operator's final itinerary.
- Add 2-4 destination-appropriate optional activities for EVERY day where leisure or sightseeing makes them possible. Travel-only days should still receive 1-2 sensible optional suggestions when practical. Label them clearly as OPTIONAL / AT OWN COST and never present them as included.

Return ONLY JSON containing the days array. Do not return title, dates, hotels,
cost, guests, transport, inclusions, exclusions or any other package field.

DESTINATION: {source.get('destination') or ''}
SOURCE DAYS:
{json.dumps([{'day':x.get('day',''),'title':x.get('title',''),'description':x.get('description','')} for x in days], ensure_ascii=False, indent=2)}
"""
    response = call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=day_schema,
            temperature=0.25,
        ),
    ))
    if not response.text:
        raise RuntimeError("Gemini returned an empty itinerary enhancement response.")
    generated = json.loads(response.text).get('days') or []
    by_day={str(x.get('day') or '').strip().lower():x for x in generated}
    merged=[]
    for original in days:
        day=dict(original)
        candidate=by_day.get(str(original.get('day') or '').strip().lower()) or {}
        for key in ('title','description','optional_activities'):
            if candidate.get(key) not in (None,''):
                day[key]=candidate[key]
        day.setdefault("optional_activities", [])
        if requested == "basic":
            day["optional_activities"] = []
        elif not day.get("optional_activities"):
            # Last-resort client-safe suggestions. These are deliberately generic,
            # relevant across destinations and explicitly remain at own cost.
            day["optional_activities"] = [
                "Explore the nearby local market and shop for regional products at own cost",
                "Try a local café or regional dining experience during available leisure time at own cost",
            ]
        merged.append(day)
    source['days']=merged
    source["detail_level"] = requested
    return source


def _classifier_source(item, work_dir):
    """Shrink supplier material for the document-type classifier only."""
    from pathlib import Path
    p=Path(item["path"]); work_dir=Path(work_dir); work_dir.mkdir(parents=True,exist_ok=True)
    if p.suffix.lower()=='.pdf':
        try:
            import fitz
            doc=fitz.open(str(p)); keep=[]
            keywords=('flight','e-ticket','eticket','pnr','passenger','booking','airline','departure','arrival','bus','boarding','drop','hotel','check-in','check out','itinerary','sightseeing','tour')
            for i,page in enumerate(doc):
                txt=(page.get_text('text') or '').lower()
                if any(k in txt for k in keywords): keep.append(i)
            if not keep: keep=[0]
            keep=sorted(set(keep))[:3]
            out=work_dir/(p.stem+'_classify.pdf'); nd=fitz.open()
            for i in keep: nd.insert_pdf(doc,from_page=i,to_page=i)
            nd.save(str(out),garbage=4,deflate=True); nd.close(); doc.close()
            return {'path':str(out),'mime_type':'application/pdf'}, out
        except Exception: return item,None
    try:
        from PIL import Image
        im=Image.open(p).convert('RGB'); max_side=1400
        if max(im.size)>max_side:
            r=max_side/max(im.size); im=im.resize((max(1,int(im.width*r)),max(1,int(im.height*r))),Image.Resampling.LANCZOS)
        out=work_dir/(p.stem+'_classify.jpg'); im.save(out,'JPEG',quality=78,optimize=True)
        return {'path':str(out),'mime_type':'image/jpeg'}, out
    except Exception: return item,None


def _extract_local_source_text(path, max_chars=50000):
    p=Path(path)
    if p.suffix.lower()=='.pdf':
        try:
            import fitz
            doc=fitz.open(str(p)); chunks=[]; total=0
            for i,page in enumerate(doc):
                t=(page.get_text('text') or '').strip()
                if t:
                    chunk=f"\n--- PAGE {i+1} ---\n{t}"
                    chunks.append(chunk); total += len(chunk)
                if total>=max_chars: break
            doc.close(); return ''.join(chunks)[:max_chars]
        except Exception: return ''
    return ''

def classify(parts, text, api_key, model):
    """Classify supplier material locally without consuming a Groq request."""
    combined=str(text or '').strip()
    for item in parts or []:
        local=_extract_local_source_text(item.get('path',''))
        if local: combined += '\n' + local
    low=combined.lower()
    tour_hits=sum(x in low for x in ('day 1','day 2','day 3','inclusions','exclusions','package cost','accommodation','sightseeing','cwb','cnb'))
    scores={
        'package':tour_hits + (4 if re.search(r'(?im)^\s*day\s*[12]\b',combined) else 0),
        'flight':sum(x in low for x in ('airline','flight no','flight number','e-ticket','sector','airport','gds pnr')),
        'bus':sum(x in low for x in ('bus operator','boarding point','dropping point','seat no','coach','bus pnr')),
        'hotel':sum(x in low for x in ('check-in','check in','check-out','check out','room type','hotel confirmation','number of nights')),
    }
    kind=max(scores,key=scores.get) if max(scores.values(),default=0)>0 else 'unknown'
    confidence=min(0.99,0.55+(scores.get(kind,0)*0.07)) if kind!='unknown' else 0.0
    return {'kind':kind,'confidence':confidence,
            'reason':f'Local document markers matched {kind}.' if kind!='unknown' else 'No reliable local document markers found.',
            'reference':'','instruction':str(text or '')}


NEW_TOUR_BRIEF_PROMPT = """
You are MyTourBazar's senior tour planner.

The OWNER is asking you to CREATE a brand-new CLIENT-READY customer itinerary from a natural-language brief.
This is a QUICK ITINERARY request, NOT supplier extraction. Build the itinerary yourself from the destination, duration and services supplied by the owner, using normal travel-planning knowledge while never inventing confirmed booking facts.

Build a polished, practical day-wise tour plan using the owner's stated requirements and normal
destination knowledge. Return ONLY JSON matching the itinerary schema.

STRICT FACT RULES:
- Preserve every explicit owner fact exactly: client name, destination, duration, dates, passenger
  counts, hotel/star category, meals, vehicle, pickup/drop, sightseeing, room type, and any price.
- If travel dates are not supplied, leave travel_dates and each day.date empty.
- If a hotel NAME is not supplied, NEVER invent one. hotel_name must stay empty.
- If a hotel CATEGORY is requested (3 star, 3 star premium, 4 star, etc.), preserve it.
- You may create an accommodation row with an empty hotel_name so the requested category/meal plan
  still appears in the itinerary.
- If number of rooms or room type is not supplied, leave it empty. Do not invent it.
- Do NOT create flight/train/bus transit unless the owner explicitly asks for or supplies it.
  If no public transport is mentioned, transit must be [].
- A private cab/vehicle requested for local transfers/sightseeing belongs in vehicle/inclusions,
  not in public transit.
- Never invent PNRs, airline numbers, ticket details, confirmed hotels, booking references or prices.
- package_costs must be [] unless the owner explicitly gives customer rates/costing.
- When the owner explicitly gives a selling price/rate, preserve it as customer-facing costing.

DAY PLAN:
- Create exactly the number of days requested. For example, 4N/5D must produce exactly 5 day-plan entries.
- Use a sensible route and sightseeing sequence for the destination.
- Explicitly include every sightseeing/place the owner requested.
- When the owner gives broad sightseeing such as "North & South Goa sightseeing", create a sensible,
  client-ready day plan around that request using normal travel knowledge.
- Basic day plans should normally be about 60-110 words per day: useful but not bloated.
- Do not state optional/paid attractions as included unless the owner explicitly included them.
- optional_activities may contain 0-2 clearly optional, at-own-cost suggestions.

INCLUSIONS:
- Build inclusions from the services requested by the owner: requested hotel category/accommodation,
  stated meals, private vehicle/transfers, and specified sightseeing.
- Do not add airfare, rail or bus tickets unless explicitly requested.

EXCLUSIONS:
- Add sensible standard exclusions such as personal expenses, optional activities/entry fees not
  specifically included, and anything not expressly included. Keep them professional and concise.

GUEST COUNTS:
- Populate adult_count, child_count, child_cwb_count, child_cnb_count and extra_bed_count from the
  brief when stated; otherwise use 0 for unspecified categories.
- guests should be a readable summary.

HOTELS:
- When only a general category is given (e.g. "3-star hotels"), create appropriate accommodation
  row(s) for overnight destination(s) with hotel_name="" and hotel_category set to the requested
  category. Preserve meal plan in meal_plan.
- Do not invent property names.

OWNER BRIEF:
"""

def generate_package_from_brief(brief, api_key, model, detail_level="basic"):
    """Create local package facts, using Groq only to write missing day plans."""
    from extractor import extract_itinerary_from_parts
    data=extract_itinerary_from_parts([],str(brief or ''),api_key,model)
    if not data.get('days'):
        import re
        m=re.search(r'(?i)\b(\d{1,2})\s*(?:days?|d)\b',str(brief or ''))
        if not m:
            nights=re.search(r'(?i)\b(\d{1,2})\s*(?:nights?|n)\b',str(brief or ''))
            count=int(nights.group(1))+1 if nights else 1
        else:
            count=int(m.group(1))
        destination=data.get('destination') or 'Destination'
        data['days']=[{'day':str(i),'date':'','title':f'{destination} – Day {i}',
                      'description':'','stay':'','meal_plan':'','optional_activities':[]}
                     for i in range(1,min(count,31)+1)]
        data['duration']=data.get('duration') or f'{count} Days'
    return enhance_package_itinerary(data,api_key,model,detail_level)


def chat(text, api_key, model):
    client = genai.Client(api_key=api_key)
    response = call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=[CHAT_PROMPT, "\nUSER:\n" + text],
        config=types.GenerateContentConfig(temperature=0.3),
    ))
    return (response.text or "").strip()
