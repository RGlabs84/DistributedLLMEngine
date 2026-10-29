"""
Offline place names for the opt-in approximate location a phone can send.

``describe(lat, lon)`` turns the coordinates into "Town, Region, Country" using a list that ships
with the release as a Python module (``engine/places_data.py``, built by scripts/build_places.py;
Python because the release installers in the field accept only ``engine/*.py`` and icons). Nothing here
touches the network, so the coordinates never leave this machine for a map service; the caller
uses only the returned text and does not keep or log the coordinates.

Place data: GeoNames (https://www.geonames.org), licensed CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/).
"""

import base64
import bisect
import gzip
import logging
import math
import threading
import zlib
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

MAX_KM = 50.0
# A place at least BIGGER_BY size classes (a class is a quarter of a power of ten, so 6 is about 30
# times the people) above the nearest one beats it if it is not more than BIGGER_WITHIN_KM farther away.
BIGGER_WITHIN_KM = 3.0
BIGGER_BY = 6
_EARTH_KM = 6371.0
# One degree of latitude is about 111 km everywhere, so rows more than this far in latitude cannot
# be within MAX_KM and need no distance at all (the list is sorted by latitude).
_BAND_DEG = MAX_KM / 111.0 + 0.01

_lock = threading.Lock()
_loaded = False
# (lat, lon, cos of lat, size class, town, number of the region); the sorted latitudes alone feed the bisect
_lats: List[float] = []
_rows: List[Tuple[float, float, float, int, str, int]] = []
_regions: List[Tuple[str, str]] = []   # per region number: (region, country)


def _load() -> None:
    """Read the list once. A missing or damaged list leaves it empty, so describe() answers None and the chat goes on."""
    global _loaded
    with _lock:
        if _loaded:
            return
        rows: List[Tuple[float, float, float, int, str, int]] = []
        regions: List[Tuple[str, str]] = []
        try:
            # Imported here, not at the top: only a coordinator that gets a location pays for the
            # megabyte of text, and workers never import it.
            from engine import places_data
            text = gzip.decompress(base64.b64decode(places_data.DATA_B64)).decode("utf-8")
            lat_hundredths = 0
            for line in text.splitlines():
                if line.startswith("#"):
                    continue
                parts = line.split("\t")
                try:
                    if parts[0] == "@":   # a region: "@", region, country
                        regions.append((parts[1], parts[2]))
                        continue
                    town, number = parts[3], int(parts[4])
                    lat_hundredths += int(parts[0])
                    lat, lon, size = lat_hundredths / 100.0, int(parts[1]) / 100.0, int(parts[2])
                except (ValueError, IndexError):
                    continue
                if town and 0 <= number < len(regions) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0:
                    rows.append((lat, lon, math.cos(math.radians(lat)), size, town, number))
        except (ImportError, AttributeError, ValueError, OSError, EOFError, zlib.error) as exc:
            log.warning("Place list unavailable (%s); approximate locations will not be named.", type(exc).__name__)
            rows, regions = [], []
        rows.sort(key=lambda r: r[0])   # the file is sorted already; bisect needs it, so make sure
        _rows[:] = rows
        _lats[:] = [r[0] for r in rows]
        _regions[:] = regions
        _loaded = True


def _number(value) -> Optional[float]:
    """A finite float, or None for anything else (strings and booleans included: a client must send numbers)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        value = float(value)
    except OverflowError:       # an int too large for a float
        return None
    return value if math.isfinite(value) else None


def describe(lat, lon) -> Optional[str]:
    """
    "Town, Region, Country" for a listed place within ~50 km, else None (ocean, wilderness, bad input).

    The nearest place is usually right, but next to a big city the nearest one can be a small
    suburb or district, which makes a poor web search. So the biggest place (by size class; the
    nearer of equals) that is not more than BIGGER_WITHIN_KM farther than the nearest wins, but only
    if it is at least BIGGER_BY classes above the nearest. Thirty times is the bar so that a district
    of 150 000 beside a city of 1.5 million keeps its own name, while a suburb of 20 000 a couple of
    kilometres from the middle of a city of a million does not.
    """
    lat, lon = _number(lat), _number(lon)
    if lat is None or lon is None or not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        return None
    _load()
    lo = bisect.bisect_left(_lats, lat - _BAND_DEG)
    hi = bisect.bisect_right(_lats, lat + _BAND_DEG)
    rad = math.radians(lat)
    cos_lat = math.cos(rad)
    # Rows farther than this in longitude cannot be within MAX_KM; it saves most of the haversines.
    lon_band = 180.0 if cos_lat < 0.01 else min(180.0, MAX_KM / (111.0 * cos_lat) + 0.05)
    nearest = MAX_KM
    kept = []   # (km, size, row index) of every row that can still win
    for index in range(lo, hi):
        row_lat, row_lon, row_cos, size, _town, _region = _rows[index]
        d_lon = abs(row_lon - lon)
        if min(d_lon, 360.0 - d_lon) > lon_band:   # the date line is not a boundary
            continue
        # Haversine; the cosine of the row's latitude was worked out once at load.
        half_lat = math.sin(math.radians(row_lat - lat) / 2.0)
        half_lon = math.sin(math.radians(row_lon - lon) / 2.0)
        a = half_lat * half_lat + cos_lat * row_cos * half_lon * half_lon
        km = 2.0 * _EARTH_KM * math.asin(min(1.0, math.sqrt(a)))
        if km <= min(MAX_KM, nearest + BIGGER_WITHIN_KM):
            kept.append((km, size, index))
            nearest = min(nearest, km)
    kept = [c for c in kept if c[0] <= nearest + BIGGER_WITHIN_KM]
    if not kept:
        return None
    index = min(kept, key=lambda c: (c[0], c[2]))[2]   # the nearest
    biggest = min(kept, key=lambda c: (-c[1], c[0], c[2]))
    if biggest[1] >= _rows[index][3] + BIGGER_BY:
        index = biggest[2]
    town, number = _rows[index][4], _rows[index][5]
    region, country = _regions[number]
    # "Dubai, Dubai, United Arab Emirates" reads badly; an empty region is just left out.
    return ", ".join(p for p in (town, "" if region == town else region, country) if p)
