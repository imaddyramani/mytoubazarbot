"""Source-aware identity reconciliation for safety-critical travel documents."""
from __future__ import annotations

import copy
import re

_TITLE=re.compile(r'(?i)^(?:mr|mrs|ms|miss|master|mstr|dr|prof|child|infant)\.?\s+')
_BLOCKED=re.compile(r'(?i)\b(?:passenger|travell?er|ticket|pnr|booking|flight|baggage|fare|tax|total|seat|hotel|support)\b')


def clean_person_name(value):
    value=re.sub(r'\s+',' ',str(value or '')).strip(' ,;:/|-')
    return _TITLE.sub('',value).strip(' ,;:/|-')


def _words(value):
    return re.findall(r"[A-Za-z][A-Za-z'\-]*",clean_person_name(value).lower())


def _valid(value):
    words=_words(value)
    return bool(words and len(words)<=6 and sum(map(len,words))>=2 and not _BLOCKED.search(str(value or '')))


def _source_support(value,source_text):
    words=_words(value)
    if not words or not source_text: return False
    source_words=re.findall(r"[a-z][a-z'\-]*",str(source_text).lower())
    source_joined=''.join(source_words)
    if ''.join(words) in source_joined: return True
    if len(words)>1 and ''.join(reversed(words)) in source_joined: return True
    return all(word in source_words for word in words)


def best_source_name(primary,fallback,source_text=''):
    """Choose the most complete supplier-supported name without inventing words."""
    primary=clean_person_name(primary); fallback=clean_person_name(fallback)
    pv,fv=_valid(primary),_valid(fallback)
    if not pv: return fallback if fv else ''
    if not fv: return primary
    pt,ft=set(_words(primary)),set(_words(fallback))
    ps,fs=_source_support(primary,source_text),_source_support(fallback,source_text)
    if fs and not ps: return fallback
    if ps and not fs: return primary
    if pt < ft: return fallback
    if ft < pt: return primary
    if ps and fs and len(''.join(_words(fallback)))>len(''.join(_words(primary))): return fallback
    return primary


def _identity(row):
    row=row or {}
    for key in ('ticket_number','seat'):
        value=re.sub(r'\W+','',str(row.get(key) or '')).lower()
        if value: return key+':'+value
    return ''


def _same_name(a,b):
    aw,bw=set(_words((a or {}).get('name'))),set(_words((b or {}).get('name')))
    return bool(aw and bw and (aw<=bw or bw<=aw))


def reconcile_people(primary,fallback,source_text='',fields=()):
    """Merge by ticket/seat/name, retain full names, and restore omitted rows."""
    primary=[copy.deepcopy(x) for x in (primary or []) if isinstance(x,dict)]
    fallback=[copy.deepcopy(x) for x in (fallback or []) if isinstance(x,dict)]
    if not primary: return fallback
    used=set(); result=[]
    for index,row in enumerate(primary):
        match_index=None; rid=_identity(row)
        if rid:
            match_index=next((i for i,x in enumerate(fallback) if i not in used and _identity(x)==rid),None)
        if match_index is None:
            match_index=next((i for i,x in enumerate(fallback) if i not in used and _same_name(row,x)),None)
        if match_index is None and index<len(fallback) and index not in used: match_index=index
        source=fallback[match_index] if match_index is not None else {}
        if match_index is not None: used.add(match_index)
        chosen=best_source_name(row.get('name'),source.get('name'),source_text)
        if chosen: row['name']=chosen
        for key in fields:
            if not str(row.get(key) or '').strip() and str(source.get(key) or '').strip(): row[key]=source[key]
        result.append(row)
    for index,row in enumerate(fallback):
        if index not in used and _valid(row.get('name')): result.append(row)
    return result
