import json
import re
import re
from pathlib import Path
from datetime import datetime

BASE_DIR = Path(__file__).resolve().parent
RECORDS_DIR = BASE_DIR / "data" / "records"
INDEX_PATH = RECORDS_DIR / "index.json"
RECORDS_DIR.mkdir(parents=True, exist_ok=True)


def _load_index():
    if not INDEX_PATH.exists():
        return {"next": 1, "records": []}
    try:
        return json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"next": 1, "records": []}


def _save_index(data):
    tmp = INDEX_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(INDEX_PATH)


def create_reference():
    data = _load_index()
    n = int(data.get("next", 1))
    ref = f"MTB{n:02d}"
    data["next"] = n + 1
    _save_index(data)
    return ref


def save_record(reference, record):
    data = _load_index()
    record = dict(record)
    record["reference"] = reference
    record.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
    record["updated_at"] = datetime.now().isoformat(timespec="seconds")
    path = RECORDS_DIR / f"{reference}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")

    records = [r for r in data.get("records", []) if r.get("reference") != reference]
    records.append({
        "reference": reference,
        "type": record.get("type", "document"),
        "filename": record.get("filename", ""),
        "created_at": record.get("created_at"),
        "updated_at": record.get("updated_at"),
    })
    data["records"] = records
    _save_index(data)
    return path


def load_record(reference):
    ref = str(reference or "").strip().upper()
    candidates = [ref]
    m = re.match(r"^MTB[-_ ]?(\d+)$", ref)
    if m:
        n = int(m.group(1))
        candidates.extend([f"MTB{n:02d}", f"MTB-{n:05d}", f"MTB{n}"])
    for candidate in dict.fromkeys(candidates):
        path = RECORDS_DIR / f"{candidate}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                return None
    return None


def list_records():
    data = _load_index()
    rows = data.get("records", [])
    rows.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    return rows


def update_record(reference, record):
    return save_record(reference, record)


def import_existing_pdfs(generated_dir):
    """Create lightweight legacy records for PDFs made before the reference system existed."""
    data = _load_index()
    known = {r.get("filename") for r in data.get("records", [])}
    changed = False
    for path in sorted(Path(generated_dir).glob("*.pdf")):
        if path.name in known or path.name.startswith("_"):
            continue
        lower = path.name.lower()
        if lower.startswith("tour-"):
            kind = "package"
        elif lower.startswith("air ticket"):
            kind = "flight"
        elif lower.startswith("bus"):
            kind = "bus"
        elif lower.startswith("hotel"):
            kind = "hotel"
        else:
            continue
        n = int(data.get("next", 1))
        reference = f"MTB{n:02d}"
        data["next"] = n + 1
        stamp = datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")
        rec = {
            "reference": reference, "type": kind, "filename": path.name,
            "created_at": stamp, "updated_at": stamp, "legacy": True,
            "data": None, "fare": None,
        }
        (RECORDS_DIR / f"{reference}.json").write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        data.setdefault("records", []).append({
            "reference": reference, "type": kind, "filename": path.name,
            "created_at": stamp, "updated_at": stamp, "legacy": True,
        })
        changed = True
    if changed:
        _save_index(data)
