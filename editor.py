"""Deterministic document edits that never rebuild unrelated booking data."""
from __future__ import annotations

import copy
import json
import re

from ai_provider import complete_json


AI_EDIT_SCHEMA = {
    "type": "object",
    "properties": {
        "operations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": {"type": "string"},
                    "path": {"type": "string"},
                    "value": {},
                },
                "required": ["action", "path", "value"],
            },
        },
        "fare_changed": {"type": "string"},
        "fare": {"type": "number"},
    },
    "required": ["operations", "fare_changed", "fare"],
}

_ALLOWED_ROOTS = {
    "package": {
        "client_name", "tour_title", "destination", "travel_dates", "duration",
        "guests", "adult_count", "child_count", "child_cwb_count", "child_cnb_count",
        "extra_bed_count", "vehicle", "pickup", "drop", "transit", "hotels", "days",
        "inclusions", "exclusions", "policies", "greeting", "accommodation_heading",
        "package_costs", "detail_level", "show_cost",
    },
    "flight": {
        "booking_id", "booking_date", "airline_pnr", "gds_pnr", "status", "mobile",
        "baggage_summary", "special_ancillary_summary", "segments", "passengers",
        "base_fare", "taxes", "gross_total", "payment_items",
    },
    "bus": {
        "booking_id", "booking_date", "pnr", "status", "mobile", "operator",
        "bus_number", "bus_type", "dep_time", "dep_city", "dep_date", "boarding_point",
        "arr_time", "arr_city", "arr_date", "drop_point", "duration", "passengers",
        "base_fare", "taxes",
    },
    "hotel": {
        "reservation_id", "guest_name", "mobile", "hotel_name", "hotel_address",
        "hotel_city", "check_in", "check_out", "nights", "room_type",
        "occupancy_summary", "room_count", "extra_bed_count", "meal_plan", "base_fare",
        "taxes", "terms", "cost_components",
    },
}


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
    cost_fields={'per_adult':(r'adult',), 'per_child_cwb':(r'cwb',r'child\s+with\s+bed'),
                 'per_child_cnb':(r'cnb',r'child\s+(?:without|no)\s+bed'), 'per_extra_bed':(r'extra\s+bed',r'\beb\b')}
    has_cost_request=bool(re.search(r'(?i)(?:₹|\bINR\b|\bRs\.?|\bfare\b|\bcost\b|\bprice\b|\brate\b|\bper\s+(?:adult|child|cwb|cnb|extra\s+bed)\b)',instruction))
    cost_changes={key:_amount(instruction,labels) for key,labels in cost_fields.items()} if has_cost_request else {}
    cost_changes={key:amount for key,amount in cost_changes.items() if amount is not None}
    if cost_changes:
        costs=data.setdefault('package_costs',[])
        if not costs: costs.append({'option':'Package','per_adult':'','per_child':'','per_child_cwb':'','per_child_cnb':'','per_extra_bed':'','total_cost':'','currency':'INR','notes':'','supplier_total':'','markup_total':'','final_total':''})
        for key,amount in cost_changes.items(): costs[0][key]=str(int(amount))
        data['show_cost']=True; changed=True
    return changed


def _edit_booking(doc_type,data,instruction,current_fare):
    changed=False
    mappings={
        'flight':{'airline_pnr':(r'airline\s+pnr',r'pnr'),'gds_pnr':(r'gds\s+pnr',),'booking_id':(r'trip\s+id',r'booking\s+id'),'status':(r'status',),'baggage_summary':(r'baggage',),'mobile':(r'mobile',r'phone')},
        'bus':{'pnr':(r'bus\s+pnr',r'pnr'),'booking_id':(r'booking\s+id',r'ticket\s+id'),'status':(r'status',),'operator':(r'bus\s+operator',r'operator'),'boarding_point':(r'boarding\s+point',),'drop_point':(r'drop(?:ping)?\s+point',)},
        'hotel':{'guest_name':(r'guest\s+name',r'client\s+name'),'hotel_name':(r'hotel\s+name',r'property'),'hotel_address':(r'hotel\s+address',r'address'),'hotel_city':(r'hotel\s+city',r'city',r'location'),'check_in':(r'check[ -]?in',),'check_out':(r'check[ -]?out',),'nights':(r'total\s+nights?',r'nights?'),'room_type':(r'room\s+type',r'room\s+category'),'extra_bed_count':(r'extra\s+(?:bed|mattress)',r'\beb\b'),'meal_plan':(r'meal\s+plan',),'reservation_id':(r'booking\s+id',r'confirmation\s+number',r'reservation\s+id')},
    }
    for key,labels in mappings.get(doc_type,{}).items():
        exact_labels=labels
        if doc_type=='flight' and key=='airline_pnr' and re.search(r'(?i)\bgds\s+pnr\b',instruction):
            exact_labels=(r'airline\s+pnr',)
        value=_value_after(instruction,exact_labels)
        if value: data[key]=value; changed=True
    name=_value_after(instruction,(r'passenger\s+name',r'traveller\s+name'))
    if name and data.get('passengers'):
        data['passengers'][0]['name']=name; changed=True
    fare=None
    if re.search(r'(?i)(?:\b(?:fare|cost|price)\b|\b(?:selling|customer|grand)\s+total\b)',instruction):
        fare=_amount(instruction,(r'fare',r'cost',r'total',r'price'))
        if fare is not None: changed=True
    return changed,fare if fare is not None else current_fare


def _path_parts(raw_path):
    path=str(raw_path or '').strip().strip('.')
    if path.startswith('document.'):
        path=path[9:]
    parts=[part for part in path.split('.') if part]
    if not parts or any(part.startswith('_') for part in parts):
        return []
    return parts


def _resolve_parent(data, parts):
    current=data
    for part in parts[:-1]:
        if isinstance(current,dict):
            if part not in current: return None,None
            current=current[part]
        elif isinstance(current,list) and part.isdigit():
            index=int(part)
            if index>=len(current): return None,None
            current=current[index]
        else:
            return None,None
    return current,parts[-1]


def _apply_operation(data, doc_type, operation):
    action=str(operation.get('action') or '').strip().lower()
    parts=_path_parts(operation.get('path'))
    if not parts or parts[0] not in _ALLOWED_ROOTS.get(doc_type,set()):
        return False
    parent,key=_resolve_parent(data,parts)
    if parent is None: return False
    value=copy.deepcopy(operation.get('value'))
    if action=='set':
        if isinstance(parent,dict):
            # Root schema fields may be restored when absent. Nested fields must
            # already exist, preventing the model from creating arbitrary data.
            if len(parts)>1 and key not in parent: return False
            old=parent.get(key)
            if isinstance(old,(dict,list)) or isinstance(value,(dict,list)): return False
            if old is not None and not isinstance(value,type(old)) and not (isinstance(old,(int,float)) and isinstance(value,(int,float))): return False
            if old==value: return False
            parent[key]=value
            return True
        if isinstance(parent,list) and key.isdigit() and int(key)<len(parent):
            index=int(key)
            old=parent[index]
            if isinstance(old,(dict,list)) or isinstance(value,(dict,list)): return False
            if old is not None and not isinstance(value,type(old)) and not (isinstance(old,(int,float)) and isinstance(value,(int,float))): return False
            if old==value: return False
            parent[index]=value
            return True
    if action=='append':
        target=parent.get(key) if isinstance(parent,dict) else None
        if not isinstance(target,list) or value in target: return False
        target.append(value)
        return True
    if action=='remove':
        target=parent.get(key) if isinstance(parent,dict) else None
        if not isinstance(target,list): return False
        if isinstance(value,int) and 0<=value<len(target):
            target.pop(value); return True
        if value in target:
            target.remove(value); return True
    return False


def _edit_with_ai(doc_type, data, instruction, current_fare):
    allowed=', '.join(sorted(_ALLOWED_ROOTS.get(doc_type,set())))
    system_prompt=f'''You are MyTourBazar's precise {doc_type} document editor.
Convert only the user's explicit requested changes into a small patch list.
Allowed actions are set, append and remove. Paths use dot notation and zero-based list indexes,
for example days.0.description or passengers.1.name. Allowed root fields: {allowed}.
Never rewrite the whole document, invent travel facts, alter unrelated values, or create a missing
nested field. For remove, value is either the exact list value or a zero-based list index.
Set fare_changed to "true" only when the user explicitly requests the separate customer fare;
otherwise return "false" and fare 0. If the request is unclear, return no operations.'''
    source=json.dumps({"instruction":instruction,"document":data},ensure_ascii=False,default=str)
    result=complete_json(system_prompt,source,AI_EDIT_SCHEMA,purpose=f'{doc_type} smart edit',max_tokens=2200)
    if not result: return False,current_fare
    changed=False
    for operation in (result.get('operations') or [])[:20]:
        if isinstance(operation,dict):
            changed=_apply_operation(data,doc_type,operation) or changed
    fare=current_fare
    if re.search(r'(?i)\b(?:fare|cost|price|total)\b',instruction) and str(result.get('fare_changed') or '').strip().lower()=='true':
        fare=float(result.get('fare') or 0)
        changed=True
    return changed,fare


def apply_edit(doc_type, current_data, instruction, api_key=None, model=None, current_fare=None):
    data=copy.deepcopy(current_data or {}); raw=str(instruction or '').strip()
    old_client_name=str(data.get('client_name') or '').strip() if doc_type=='package' else ''
    if not raw: raise ValueError('Write the field or Day number you want to change.')
    if doc_type=='package':
        changed=_edit_package(data,raw); fare=current_fare
    else:
        changed,fare=_edit_booking(doc_type,data,raw,current_fare)
    if not changed:
        changed,fare=_edit_with_ai(doc_type,data,raw,current_fare)
    if not changed:
        raise ValueError('I could not identify a supported field. Example: “change Day 1 to …”, “set hotel name to …” or “change fare to 15000”.')
    # A package guest-name edit must update both the top guest field and the
    # salutation. Preserve the existing greeting text and replace only its name.
    if doc_type=='package':
        new_client_name=str(data.get('client_name') or '').strip()
        if new_client_name and new_client_name != old_client_name:
            greeting=str(data.get('greeting') or '')
            if old_client_name and old_client_name in greeting:
                greeting=greeting.replace(old_client_name,new_client_name)
            elif re.search(r'(?i)^\s*Dear\s+[^,\n]+,',greeting):
                greeting=re.sub(r'(?i)^(\s*Dear\s+)[^,\n]+,',rf'\g<1>{new_client_name},',greeting,count=1)
            elif greeting:
                greeting=f'Dear {new_client_name},\n\n{greeting}'
            data['greeting']=greeting
    return data,fare
