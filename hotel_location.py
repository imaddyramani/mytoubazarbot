"""Best-effort hotel address lookup without AI or a paid API key."""

from functools import lru_cache
import json
import os
import re
from urllib.parse import urlencode, quote_plus
from urllib.request import Request, urlopen


def google_maps_url(hotel_name, city="", address=""):
    query = ", ".join(x.strip() for x in (hotel_name, address or city) if str(x or "").strip())
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}" if query else ""


def _words(value):
    ignored = {"hotel", "hotels", "resort", "resorts", "the", "and", "inn", "by"}
    return {x for x in re.findall(r"[a-z0-9]+", str(value or "").lower()) if len(x) > 2 and x not in ignored}


@lru_cache(maxsize=512)
def resolve_hotel_location(hotel_name, city=""):
    """Return an OSM address and a stable Google Maps directions URL.

    Supplier-provided addresses always take priority. Nominatim is used only when
    the document contains a hotel name and city but no address. Failure is safe:
    the voucher still contains a Google Maps hotel/city search link.
    """
    hotel_name = str(hotel_name or "").strip()
    city = str(city or "").strip()
    result = {"address": "", "maps_url": google_maps_url(hotel_name, city)}
    if not hotel_name or not city or os.getenv("HOTEL_LOCATION_LOOKUP", "1") == "0":
        return result

    params = urlencode({"q": f"{hotel_name}, {city}", "format": "jsonv2", "limit": 3, "addressdetails": 1})
    request = Request(
        f"https://nominatim.openstreetmap.org/search?{params}",
        headers={"User-Agent": "MyTourBazarBot/1.0 (sales@mytourbazar.com)"},
    )
    try:
        timeout = min(3.0, max(0.5, float(os.getenv("HOTEL_LOCATION_TIMEOUT", "2.0"))))
        with urlopen(request, timeout=timeout) as response:
            rows = json.loads(response.read(200000).decode("utf-8"))
    except Exception:
        return result

    hotel_words = _words(hotel_name)
    city_words = _words(city)
    for row in rows if isinstance(rows, list) else []:
        display = re.sub(r"\s+", " ", str(row.get("display_name") or "")).strip()
        display_words = _words(display)
        if not display or (city_words and not city_words.intersection(display_words)):
            continue
        if hotel_words and not hotel_words.intersection(display_words):
            continue
        result["address"] = display
        result["maps_url"] = google_maps_url(hotel_name, city, display)
        break
    return result
