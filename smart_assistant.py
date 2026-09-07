"""Local command assistant, document classifier and Tour planner."""
from __future__ import annotations

import re

from local_tour_planner import attractive_title, build_days, enhance_days, infer_destination, requested_days
from performance_utils import collect_local_document_text


def _source_text(parts, text):
    try:
        return collect_local_document_text(parts or [],text or '',max_chars=70000)
    except Exception:
        return str(text or '')


def classify(parts, text, api_key=None, model=None):
    combined=_source_text(parts,text); low=combined.lower()
    tour_hits=sum(x in low for x in ('day 1','day 2','inclusions','exclusions','package cost','sightseeing','cwb','cnb'))
    scores={
        'package':tour_hits+(4 if re.search(r'(?im)^\s*day\s*[12]\b',combined) else 0),
        'flight':sum(x in low for x in ('airline','flight no','flight number','e-ticket','airport','gds pnr','baggage')),
        'bus':sum(x in low for x in ('bus operator','boarding point','dropping point','seat no','coach','bus pnr')),
        'hotel':sum(x in low for x in ('check-in','check in','check-out','check out','room type','hotel confirmation','number of nights')),
    }
    kind=max(scores,key=scores.get) if max(scores.values(),default=0)>0 else 'unknown'
    confidence=min(.99,.55+.07*scores[kind]) if kind!='unknown' else 0.0
    return {'kind':kind,'confidence':confidence,'reason':f'Local document markers matched {kind}.' if kind!='unknown' else 'No reliable booking markers were found.','reference':'','instruction':str(text or '')}


def agent_plan(text, source_context='', api_key=None, model=None):
    raw=str(text or '').strip(); low=raw.lower()
    ref=re.search(r'\bMTB[A-Z0-9]{1,12}\b',raw,re.I)
    edit_words=('change','edit','replace','remove','add','update','make day','increase','decrease')
    if ref and any(x in low for x in edit_words):
        return {'action':'edit_document','kind':'unknown','reference':ref.group(0).upper(),'instruction':raw,'reason':'Saved-document edit recognized locally.','needs_user_input':''}
    if re.search(r'(?i)\b(?:tour|package|itinerary|quotation|voucher)\b',raw) and re.search(r'(?i)\b(?:\d+\s*[nd]|\d+\s*(?:nights?|days?)|kashmir|kerala|goa|rajasthan|himachal|sikkim|darjeeling|bhutan|bali|dubai|andaman|ladakh|uttarakhand)\b',raw):
        return {'action':'generate_brief','kind':'package','reference':'','instruction':raw,'reason':'New Tour brief recognized locally.','needs_user_input':''}
    guessed=classify([],raw)
    if guessed['kind']!='unknown' and len(raw.splitlines())>=4:
        return {'action':'generate_supplier','kind':guessed['kind'],'reference':'','instruction':raw,'reason':guessed['reason'],'needs_user_input':''}
    return {'action':'chat','kind':'unknown','reference':'','instruction':raw,'reason':'Local help request.','needs_user_input':''}


def _count(text, pattern):
    match=re.search(pattern,str(text or ''),re.I)
    return int(match.group(1)) if match else 0


def _money(text):
    match=re.search(r'(?:₹|INR|Rs\.?)?\s*([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]{4,8})\s*(?:pp|per\s*(?:person|pax|adult))',str(text or ''),re.I)
    return match.group(1).replace(',','') if match else ''


def generate_package_from_brief(brief, api_key=None, model=None, detail_level='basic'):
    raw=str(brief or ''); destination,key=infer_destination(raw); count=requested_days(raw)
    adults=_count(raw,r'(\d+)\s*(?:adults?|adt)\b')
    cwb=_count(raw,r'(\d+)\s*(?:cwb|child(?:ren)?\s+with\s+bed)\b')
    cnb=_count(raw,r'(\d+)\s*(?:cnb|child(?:ren)?\s+(?:without|no)\s+bed)\b')
    client=''; person=re.search(r'(?i)\b((?:Mr|Mrs|Ms|Miss|Dr)\.?\s+[A-Za-z][A-Za-z .\'-]{2,70})',raw)
    if person: client=re.split(r'[,\n]',person.group(1))[0].strip()
    hotel_cat=''; cat=re.search(r'(?i)\b([2-7])\s*(?:star|\*)\s*(premium)?\b',raw)
    if cat: hotel_cat=f"{cat.group(1)} Star"+(" Premium" if cat.group(2) else '')
    meal='Breakfast & Dinner' if re.search(r'(?i)breakfast.*dinner|dinner.*breakfast|\bMAP(?:I)?\b',raw) else ('Breakfast' if re.search(r'(?i)breakfast|\bCP\b',raw) else '')
    vehicle=''; car=re.search(r'(?i)\b(Suzuki\s+Ertiga|Ertiga|Innova(?:\s+Crysta)?|Tempo\s+Traveller|private\s+(?:cab|car|vehicle)|cab|taxi)\b',raw)
    if car: vehicle=car.group(1)
    guests=', '.join(x for x in (f'{adults} Adult(s)' if adults else '',f'{cwb} CWB' if cwb else '',f'{cnb} CNB' if cnb else '') if x)
    costs=[]; pp=_money(raw)
    if pp: costs=[{'option':'Package','per_adult':pp,'per_child':'','per_child_cwb':'','per_child_cnb':'','per_extra_bed':'','total_cost':'','currency':'INR','notes':'Customer selling rate','supplier_total':'','markup_total':'','final_total':''}]
    hotels=[]
    if hotel_cat or meal: hotels=[{'dates':'','destination':destination,'hotel_name':'','room_category':'','hotel_category':hotel_cat,'rooms':'','room_type':'','meal_plan':meal,'option':'Option 1'}]
    inclusions=[]
    if hotel_cat or meal: inclusions.append('Accommodation and meals as stated in the itinerary')
    if vehicle: inclusions.append(f'{vehicle} for confirmed transfers and sightseeing')
    inclusions.append('Sightseeing specifically mentioned in the day-wise itinerary')
    data={'client_name':client,'tour_title':attractive_title(destination,key),'destination':destination,'travel_dates':'',
          'duration':f'{max(0,count-1)} Nights and {count} Days' if count>1 else '1 Day','guests':guests,
          'adult_count':adults,'child_count':cwb+cnb,'child_cwb_count':cwb,'child_cnb_count':cnb,'extra_bed_count':0,
          'vehicle':vehicle,'pickup':'','drop':'','transit':[],'hotels':hotels,'days':build_days(raw,count,detail_level),
          'inclusions':inclusions,'exclusions':['Air/train/bus fare unless specifically included','Entry tickets and optional activities unless specifically included','Personal expenses and meals not mentioned','Anything not expressly listed under inclusions'],
          'policies':'','greeting':'','accommodation_heading':'Accommodation Schedule','package_costs':costs,
          'detail_level':str(detail_level).lower(),'show_cost':bool(costs)}
    return enhance_days(data,detail_level)


def enhance_package_itinerary(current_data, api_key=None, model=None, detail_level='detailed'):
    return enhance_days(current_data,detail_level)


def chat(text, api_key=None, model=None):
    low=str(text or '').lower()
    if 'air' in low or 'flight' in low: return 'Use ✈️ Air Print and send the supplier PDF, screenshot or text. I will extract it locally, then show Add Cost and print options.'
    if 'bus' in low: return 'Use 🚌 Bus Print and send the supplier ticket. I will extract passenger, seat, PNR, boarding, drop and fare locally.'
    if 'hotel' in low: return 'Use 🏨 Hotel Print and send the confirmation. I will extract guest, property, dates, rooms, meal plan and cost locally.'
    if 'tour' in low or 'itinerary' in low: return 'Use 🗺️ Tour Guide for supplier material, or write destination, duration, guests, hotel category, meals, vehicle and sightseeing for a new local day plan.'
    if 'edit' in low or 'change' in low: return 'Reply to the generated document or use Modify & Regenerate. Mention the exact field or Day number; unrelated data will remain unchanged.'
    return 'I can locally create Air, Bus, Hotel and Tour documents, apply supported smart changes, prepare Basic/Detailed WhatsApp or PDF, and explain each print workflow.'
