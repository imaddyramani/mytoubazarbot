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
generate_brief = owner wants a new itinerary from a natural-language brief.
edit_document = owner wants an existing generated document changed. Convert the request into a precise
instruction for the document editor; it may contain multiple changes.
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
    """Re-write an existing tour itinerary at basic or detailed level while preserving confirmed facts."""
    client = genai.Client(api_key=api_key)
    mode = "BASIC" if str(detail_level).lower() == "basic" else "DETAILED"
    prompt = f"""
You are MyTourBazar's senior travel itinerary editor.
Rewrite the existing tour itinerary at {mode} detail level.

FACT SAFETY:
- Preserve all confirmed dates, hotels, transport, meal plans, inclusions and exclusions exactly.
- Never invent a hotel, flight, booking, price or confirmed service.
- You may improve destination descriptions using normal travel knowledge.
- Optional activities are suggestions only and must never be presented as included/booked.

BASIC MODE:
- 35-70 words per day.
- Clearly name the principal sightseeing/experience.
- optional_activities must be [].

DETAILED MODE:
- 150-220 words per day. This is a full-length, client-ready tour-planner day plan, not a short summary.
- Structure each day naturally as a professional travel planner would: morning/start of the day, sightseeing and experiences in logical sequence, afternoon, and evening/return or leisure where applicable.
- Name EVERY included sightseeing/attraction/place explicitly.
- For every included sightseeing, give a useful client-facing description: what the place is, what the guest will see/do there, and the main experience or highlight. Avoid generic filler.
- Explain the travel flow between the included places in a natural way when the supplied itinerary supports it.
- Mention meals, hotel/stay and included vehicle/transport in the appropriate part of the day when those facts are confirmed.
- Do NOT invent exact timings, distances, travel durations, ticket inclusions, closures, or booked activities. If timing is not supplied, use natural wording such as "after breakfast", "later", "in the afternoon", or "in the evening".
- Keep the writing polished, warm and customer-facing, like a professional tour operator's final itinerary.
- Add 2-4 relevant optional activities only when genuinely useful. Label them clearly as OPTIONAL / AT OWN COST and never present them as included.

Return ONLY the complete JSON matching the supplied itinerary schema.

CURRENT ITINERARY:
{json.dumps(current_data, ensure_ascii=False, indent=2)}
"""
    response = call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ITINERARY_SCHEMA,
            temperature=0.25,
        ),
    ))
    if not response.text:
        raise RuntimeError("Gemini returned an empty itinerary enhancement response.")
    data = json.loads(response.text)
    data["detail_level"] = str(detail_level).lower()
    for day in data.get("days", []):
        day.setdefault("optional_activities", [])
        if str(detail_level).lower() == "basic":
            day["optional_activities"] = []
    return data


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
    """Fast source-aware classification: text-first for PDFs, visuals only when text is unavailable."""
    client=genai.Client(api_key=api_key)
    contents=[CLASSIFIER_PROMPT]
    combined=str(text or '').strip(); visual=[]
    for item in parts or []:
        local=_extract_local_source_text(item.get('path',''))
        if local: combined += '\n' + local
        else: visual.append(item)
    if combined: contents.append('\nCOMPLETE NORMALIZED SOURCE TEXT:\n'+combined[:50000])
    work_dir=Path(__file__).resolve().parent/'data'/'tmp_classifier'; temps=[]
    try:
        for item in visual[:3]:
            prepared,tmp=_classifier_source(item,work_dir)
            if tmp: temps.append(Path(tmp))
            p=Path(prepared['path']); contents.append(types.Part.from_bytes(data=p.read_bytes(),mime_type=prepared['mime_type']))
        response=call_with_high_demand_retry(lambda: client.models.generate_content(model=model,contents=contents,config=types.GenerateContentConfig(response_mime_type='application/json',response_schema=CLASS_SCHEMA,temperature=0)))
        if not response.text: raise RuntimeError('AI classification returned an empty response.')
        result=json.loads(response.text)
        low=combined.lower()
        tour_hits=sum(x in low for x in ('day 1','day 2','day 3','day 4','inclusions','exclusions','package cost','accommodation schedule','per adult','sightseeing'))
        if tour_hits>=3 and ('day 1' in low or 'day 2' in low) and len(low)>800:
            result['kind']='package'; result['confidence']=max(float(result.get('confidence',0) or 0),0.92)
            result['reason']='Multi-day package structure detected; embedded flights/hotels are treated as tour components.'
        return result
    finally:
        for q in temps:
            try:q.unlink(missing_ok=True)
            except:pass
        try:
            if work_dir.exists() and not any(work_dir.iterdir()): work_dir.rmdir()
        except:pass


def chat(text, api_key, model):
    client = genai.Client(api_key=api_key)
    response = call_with_high_demand_retry(lambda: client.models.generate_content(
        model=model,
        contents=[CHAT_PROMPT, "\nUSER:\n" + text],
        config=types.GenerateContentConfig(temperature=0.3),
    ))
    return (response.text or "").strip()
