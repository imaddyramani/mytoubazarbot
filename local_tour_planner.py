"""Deterministic Tour planning and wording for the MyTourBazar workflow."""
from __future__ import annotations

import copy
import re


DESTINATIONS = {
    "kashmir": {
        "name": "Kashmir", "title": "Mesmerizing Kashmir",
        "days": [
            ("Arrival in Srinagar", "Arrive in Srinagar, meet the assigned vehicle and proceed for hotel check-in. Use the remaining time for a relaxed local orientation or leisure, followed by overnight stay in Srinagar."),
            ("Srinagar Local Sightseeing", "After breakfast, cover the confirmed Srinagar local attractions in a comfortable sequence. Return to the hotel after sightseeing and stay overnight in Srinagar."),
            ("Sonamarg Excursion", "Proceed after breakfast for the confirmed Sonamarg excursion. Enjoy the valley surroundings and complete the sightseeing mentioned in the package before returning to Srinagar for overnight stay."),
            ("Gulmarg Excursion", "Travel to Gulmarg after breakfast and cover the sightseeing included in the package. Gondola or other optional activities remain at own cost unless specifically included. Return for overnight stay as scheduled."),
            ("Pahalgam Excursion", "Proceed to Pahalgam and visit the places stated in the package. Local union-cab sightseeing will follow the supplier inclusion. Continue to the scheduled hotel and overnight stay."),
            ("Departure", "After breakfast and check-out, proceed to the confirmed departure point. The tour concludes with pleasant memories of Kashmir."),
        ],
    },
    "kerala": {
        "name": "Kerala", "title": "Mesmerizing Kerala",
        "days": [
            ("Kochi Arrival and Transfer", "Arrive at Kochi, meet the assigned vehicle and proceed towards the first scheduled destination. Complete hotel check-in and relax for the evening."),
            ("Munnar Sightseeing", "After breakfast, cover the confirmed Munnar sightseeing and enjoy the tea-country landscape. Return to the hotel after the day's visits for overnight stay."),
            ("Munnar to Thekkady", "Check out after breakfast and proceed to Thekkady. Complete the sightseeing and activities specifically mentioned in the package, followed by hotel check-in and overnight stay."),
            ("Thekkady to Alleppey", "Proceed to Alleppey after breakfast for the confirmed backwater stay or sightseeing arrangement. Enjoy the scheduled experience and overnight stay."),
            ("Kovalam and Trivandrum", "Continue towards Kovalam or Trivandrum and cover the confirmed local sightseeing in a practical sequence. Check in and stay overnight as scheduled."),
            ("Departure", "After breakfast, check out and transfer to the confirmed airport, railway station or departure point. The Kerala tour concludes here."),
        ],
    },
    "goa": {"name":"Goa","title":"Gorgeous Goa","days":[
        ("Arrival in Goa","Arrive in Goa, meet the assigned transfer and proceed to the hotel for check-in. The remaining time is free for leisure."),
        ("North Goa Sightseeing","After breakfast, cover the North Goa sightseeing specifically included in the package and return to the hotel in the evening."),
        ("South Goa Sightseeing","Proceed after breakfast for the confirmed South Goa sightseeing. Return to the hotel after completing the scheduled visits."),
        ("Leisure and Departure","Use the available time for leisure, then check out and proceed to the confirmed departure point."),
    ]},
    "rajasthan": {"name":"Rajasthan","title":"Royal Rajasthan","days":[
        ("Arrival and Local Orientation","Arrive, meet the assigned vehicle and proceed to the scheduled hotel. Cover the confirmed local sightseeing as time permits."),
        ("Heritage Sightseeing","After breakfast, visit the forts, palaces and local attractions specifically mentioned in the itinerary, then return for overnight stay."),
        ("Intercity Transfer","Check out and proceed to the next confirmed destination, covering en-route sightseeing stated in the package."),
        ("Departure","After breakfast and check-out, transfer to the confirmed departure point. The tour concludes here."),
    ]},
    "himachal": {"name":"Himachal","title":"Himachal Highlights","days":[
        ("Arrival and Transfer","Arrive at the pickup point and proceed by the assigned vehicle to the scheduled hill destination for check-in and overnight stay."),
        ("Local Sightseeing","After breakfast, cover the confirmed local sightseeing and experiences in a comfortable sequence. Return to the hotel for overnight stay."),
        ("Excursion Day","Proceed for the excursion specified in the package. Optional adventure activities remain at own cost unless explicitly included."),
        ("Departure","Check out after breakfast and transfer to the confirmed departure point."),
    ]},
    "sikkim": {"name":"Sikkim & Darjeeling","title":"Enchanting Sikkim & Darjeeling","days":[
        ("Arrival and Transfer","Arrive at the confirmed gateway and transfer to the scheduled hotel for check-in and overnight stay."),
        ("Gangtok Sightseeing","Cover the confirmed Gangtok sightseeing after breakfast and return to the hotel for overnight stay."),
        ("Excursion Day","Proceed for the permit-based or local excursion stated in the package. Any optional activity remains at own cost unless included."),
        ("Darjeeling Transfer","Check out and proceed to Darjeeling, covering confirmed en-route stops before hotel check-in."),
        ("Darjeeling Sightseeing","Complete the confirmed Darjeeling sightseeing in the scheduled sequence and return for overnight stay."),
        ("Departure","Check out and transfer to the confirmed airport or railway station."),
    ]},
}

DESTINATIONS.update({
    'bhutan':{'name':'Bhutan','title':'Beautiful Bhutan','days':[]},
    'bali':{'name':'Bali','title':'Beautiful Bali','days':[]},
    'dubai':{'name':'Dubai','title':'Dazzling Dubai','days':[]},
    'andaman':{'name':'Andaman','title':'Amazing Andaman','days':[]},
    'ladakh':{'name':'Ladakh','title':'Legendary Ladakh','days':[]},
    'uttarakhand':{'name':'Uttarakhand','title':'Enchanting Uttarakhand','days':[]},
})

ALIASES={"darjeeling":"sikkim","manali":"himachal","shimla":"himachal"}


def infer_destination(text):
    low=str(text or '').lower()
    for alias,key in ALIASES.items():
        if re.search(r'\b'+re.escape(alias)+r'\b',low):
            return DESTINATIONS[key]['name'],key
    for key,data in DESTINATIONS.items():
        if re.search(r'\b'+re.escape(key)+r'\b',low):
            return data['name'],key
    match=re.search(r'(?im)^\s*(?:destination|place)\s*[:\-]\s*([^\n]{2,60})',str(text or ''))
    clean=re.sub(r'(?i)\b\d+\s*(?:nights?|days?|n|d)\b.*','',match.group(1)).strip(' ,-') if match else ''
    return clean, ''


def requested_days(text, default=1):
    raw=str(text or '')
    pair=re.search(r'(?i)\b(\d{1,2})\s*(?:nights?|n)\s*[/&+ -]*\s*(\d{1,2})\s*(?:days?|d)\b',raw)
    if pair: return max(1,min(31,int(pair.group(2))))
    day=re.search(r'(?i)\b(\d{1,2})\s*(?:days?|d)\b',raw)
    if day: return max(1,min(31,int(day.group(1))))
    night=re.search(r'(?i)\b(\d{1,2})\s*(?:nights?|n)\b',raw)
    return max(1,min(31,int(night.group(1))+1)) if night else default


def attractive_title(destination, key=''):
    if key in DESTINATIONS: return DESTINATIONS[key]['title']
    return f"Discover {destination}" if destination else "Customized Holiday"


def _clean_description(value):
    return re.sub(r'\s+',' ',str(value or '')).strip(' •-–—')


def build_days(text, count=None, detail="basic"):
    destination,key=infer_destination(text)
    count=count or requested_days(text)
    templates=(DESTINATIONS.get(key) or {}).get('days') or [
        ("Arrival and Transfer", "Arrive at the confirmed pickup point and proceed to the scheduled accommodation for check-in and leisure."),
        ("Sightseeing", "After breakfast, cover the sightseeing and experiences specifically requested in the tour brief, then return for overnight stay."),
        ("Departure", "After breakfast and check-out, proceed to the confirmed departure point. The tour concludes here."),
    ]
    rows=[]
    for index in range(count):
        if index==count-1 and count>1: title,desc=templates[-1]
        else: title,desc=templates[min(index,len(templates)-2 if len(templates)>1 else 0)]
        rows.append({'day':str(index+1),'date':'','title':title,'description':desc,
                     'stay':'','meal_plan':'','optional_activities':[]})
    return rows


def enhance_days(data, detail="detailed"):
    result=copy.deepcopy(data or {})
    detailed=str(detail).lower()=='detailed'
    for row in result.get('days') or []:
        desc=_clean_description(row.get('description'))
        if not desc:
            desc="Follow the sightseeing, transfers and stay arrangements confirmed for this day."
        if detailed and len(desc.split())<55:
            desc=(desc.rstrip('.')+'. The day will follow a comfortable travel sequence around the confirmed services and sightseeing. '
                  'Adequate time will be kept for transfers, visits, hotel check-in or return, according to the supplied itinerary. '
                  'Only the services expressly listed in the package are treated as included; optional activities and personal expenses remain separate.')
        if not detailed:
            desc=' '.join(desc.split()[:70])
            row['optional_activities']=[]
        row['description']=desc
        row.setdefault('optional_activities',[])
    result['detail_level']='detailed' if detailed else 'basic'
    return result
