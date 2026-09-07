"""Deterministic document edits that never rebuild unrelated booking data."""
from __future__ import annotations

import copy
import re


def _value_after(text, labels):
    label='|'.join(labels)
    patterns=(
        rf'(?is)\b(?:change|set|update|replace)\s+(?:the\s+)?(?:{label})\s+(?:to|as)\s+(.+?)(?=\s+(?:and\s+)?(?:change|set|update|replace|add|remove)\b|$)',
        rf'(?im)^\s*(?:{label})\s*[:=\-]\s*(.+?)\s*$',
    )
    for pattern in patterns:
        match=re.search(pattern,text)
        if match: return re.sub(r'\s+',' ',match.group(1)).strip(' .')
    return ''


def _amount(text, labels):
    value=_value_after(text,labels)
    if not value:
        label='|'.join(labels)
        match=re.search(rf'(?i)\b(?:{label})\b\D{{0,18}}(?:₹|INR|Rs\.?)?\s*([0-9][0-9,]*)',text)
        value=match.group(1) if match else ''
    match=re.search(r'[0-9][0-9,]*',value)
    return float(match.group(0).replace(',','')) if match else None


def _find_day(days, number):
    for index,row in enumerate(days):
        match=re.search(r'\d+',str((row or {}).get('day') or ''))
        if match and int(match.group())==number: return index
    return number-1 if 0<number<=len(days) else None


def _edit_package(data, instruction):
    changed=False; low=instruction.lower(); days=data.setdefault('days',[])
    targets=list(dict.fromkeys(int(x) for x in re.findall(r'(?i)\bday\s*(\d{1,2})\b',instruction)))
    for number in targets:
        index=_find_day(days,number)
        if index is None: continue
        match=re.search(rf'(?is)\b(?:on\s+)?day\s*{number}\b\s*(?:[:=\-]|will\s+be|guest\s+will)?\s*(.+)$',instruction)
        if match:
            request=re.sub(r'(?i)^(?:change|update|replace|make)\s+(?:it\s+)?(?:to\s+)?','',match.group(1)).strip()
            if request and request.lower() not in ('detailed','basic','more detailed'):
                row=dict(days[index]); row['description']=request; days[index]=row; changed=True
    scalar={
        'client_name':(r'guest\s*name',r'client\s*name',r'traveller\s*name'),
        'destination':(r'destination',), 'tour_title':(r'tour\s*title',r'package\s*title'),
        'travel_dates':(r'travel\s*dates?',r'tour\s*dates?'), 'duration':(r'duration',),
        'vehicle':(r'vehicle',r'cab'), 'pickup':(r'pick[ -]?up',), 'drop':(r'drop',),
    }
    for key,labels in scalar.items():
        value=_value_after(instruction,labels)
        if value: data[key]=value; changed=True
    hotels=data.get('hotels') or []
    if hotels:
        fields={'hotel_name':(r'hotel(?:\s+name)?',r'property'), 'hotel_category':(r'hotel\s+category',r'star\s+category'),
                'room_type':(r'room\s+type',r'room\s+category'), 'rooms':(r'total\s+rooms?',r'rooming'), 'meal_plan':(r'meal\s+plan',r'meals?')}
        for key,labels in fields.items():
            value=_value_after(instruction,labels)
            if value:
                hotels[0][key]=value
                if key=='room_type': hotels[0]['room_category']=value
                changed=True
        star=re.search(r'(?i)\b([2-7])\s*star\s*(premium)?\b',instruction)
        if star: hotels[0]['hotel_category']=f"{star.group(1)} Star"+(" Premium" if star.group(2) else ''); changed=True
    add_inc=re.search(r'(?is)\badd\s+(?:to\s+)?inclusions?\s*[:\-]?\s*(.+)$',instruction)
    add_exc=re.search(r'(?is)\badd\s+(?:to\s+)?exclusions?\s*[:\-]?\s*(.+)$',instruction)
    if add_inc: data.setdefault('inclusions',[]).append(add_inc.group(1).strip()); changed=True
    if add_exc: data.setdefault('exclusions',[]).append(add_exc.group(1).strip()); changed=True
    costs=data.setdefault('package_costs',[])
    if not costs: costs.append({'option':'Package','per_adult':'','per_child':'','per_child_cwb':'','per_child_cnb':'','per_extra_bed':'','total_cost':'','currency':'INR','notes':'','supplier_total':'','markup_total':'','final_total':''})
    cost_fields={'per_adult':(r'adult',), 'per_child_cwb':(r'cwb',r'child\s+with\s+bed'),
                 'per_child_cnb':(r'cnb',r'child\s+(?:without|no)\s+bed'), 'per_extra_bed':(r'extra\s+bed',r'\beb\b')}
    for key,labels in cost_fields.items():
        amount=_amount(instruction,labels)
        if amount is not None: costs[0][key]=str(int(amount)); data['show_cost']=True; changed=True
    if not any(str(x.get(k) or '') for x in costs for k in ('per_adult','per_child','per_child_cwb','per_child_cnb','per_extra_bed','total_cost','final_total')):
        data['package_costs']=[]
    return changed


def _edit_booking(doc_type,data,instruction,current_fare):
    changed=False
    mappings={
        'flight':{'airline_pnr':(r'airline\s+pnr',r'pnr'),'gds_pnr':(r'gds\s+pnr',),'booking_id':(r'trip\s+id',r'booking\s+id'),'status':(r'status',),'baggage_summary':(r'baggage',),'mobile':(r'mobile',r'phone')},
        'bus':{'pnr':(r'bus\s+pnr',r'pnr'),'booking_id':(r'booking\s+id',r'ticket\s+id'),'status':(r'status',),'operator':(r'bus\s+operator',r'operator'),'boarding_point':(r'boarding\s+point',),'drop_point':(r'drop(?:ping)?\s+point',)},
        'hotel':{'guest_name':(r'guest\s+name',r'client\s+name'),'hotel_name':(r'hotel\s+name',r'property'),'check_in':(r'check[ -]?in',),'check_out':(r'check[ -]?out',),'room_type':(r'room\s+type',r'room\s+category'),'meal_plan':(r'meal\s+plan',),'booking_id':(r'booking\s+id',r'confirmation\s+number')},
    }
    for key,labels in mappings.get(doc_type,{}).items():
        value=_value_after(instruction,labels)
        if value: data[key]=value; changed=True
    name=_value_after(instruction,(r'passenger\s+name',r'traveller\s+name'))
    if name and data.get('passengers'):
        data['passengers'][0]['name']=name; changed=True
    fare=None
    if re.search(r'(?i)\b(?:fare|cost|total|price)\b',instruction):
        fare=_amount(instruction,(r'fare',r'cost',r'total',r'price'))
        if fare is not None: changed=True
    return changed,fare if fare is not None else current_fare


def apply_edit(doc_type, current_data, instruction, api_key=None, model=None, current_fare=None):
    data=copy.deepcopy(current_data or {}); raw=str(instruction or '').strip()
    if not raw: raise ValueError('Write the field or Day number you want to change.')
    if doc_type=='package':
        changed=_edit_package(data,raw); fare=current_fare
    else:
        changed,fare=_edit_booking(doc_type,data,raw,current_fare)
    if not changed:
        raise ValueError('I could not identify a supported field. Example: “change Day 1 to …”, “set hotel name to …” or “change fare to 15000”.')
    return data,fare
