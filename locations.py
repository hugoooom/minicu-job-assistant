"""保留页面工作地址，解析明确的城市/区和网站原始地图坐标。"""

import math
import re

from search_regions import DISTRICTS

DETAIL_SOURCES = ("detail_dom", "detail_ocr")
LOCATION_COLUMNS = {"card_location": "TEXT", "location_city": "TEXT", "location_district": "TEXT",
                    "location_source": "TEXT", "longitude": "REAL", "latitude": "REAL", "coordinate_system": "TEXT"}


def has_detail_location(job):
    return job.get("location_source") in DETAIL_SOURCES and isinstance(job.get("location"), str) and bool(job["location"].strip())


def address_parts(address):
    text = re.sub(r"[\s·]+", "", address or "")
    text = re.sub(r"^[\u4e00-\u9fff]{2,8}?省", "", text)
    for city, districts in DISTRICTS.items():
        if text.startswith(city):
            rest = text[len(city):].removeprefix("市")
            district = next((name for name in districts if rest.startswith(name)), None)
            return city, district
    explicit = re.match(r"^([\u4e00-\u9fff]{2,8}?)市", text)
    return (explicit.group(1), None) if explicit else (None, None)


def coordinates(raw):
    try:
        lng, lat = (float(value) for value in raw.split(","))
        if (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90
                and (lng != 0 or lat != 0)):
            return lng, lat
    except (AttributeError, ValueError, TypeError):
        pass
    return None, None


def location_fields(address, source="detail_dom", raw_coordinates=None):
    city, district = address_parts(address)
    lng, lat = coordinates(raw_coordinates)
    return {"location": address.strip(), "location_city": city, "location_district": district,
            "location_source": source, "longitude": lng, "latitude": lat,
            "coordinate_system": "unknown" if lng is not None else None}


def import_location(job):
    address = job.get("location")
    if not isinstance(address, str) or not address.strip():
        return {key: None for key in ("location", *LOCATION_COLUMNS)}
    source = job.get("location_source") if job.get("location_source") in DETAIL_SOURCES else "card"
    raw = f"{job.get('longitude')},{job.get('latitude')}"
    fields = location_fields(address, source, raw)
    fields["card_location"] = job.get("card_location") or (address if source == "card" else None)
    return fields
