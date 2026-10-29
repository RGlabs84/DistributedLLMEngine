#!/usr/bin/env python3
"""
Build engine/places_data.py, the offline place list behind engine/places.py, from GeoNames.

    python3 -I scripts/build_places.py [--download-dir DIR] [--out FILE]

Standard library only. It builds from GeoNames' cities1000 list cut at MIN_POPULATION (--source
and --min-population change that; the constants below say what each costs in release size). It
downloads (once; files already in --download-dir are reused) the chosen places list
(cities1000.zip by default), admin1CodesASCII.txt and countryInfo.txt from
https://download.geonames.org/export/dump/ . The downloads are untrusted data: they are only
parsed here, never executed, and every name is cleaned before it is written. The output is
deterministic (fixed gzip header, sorted rows), so rebuilding from the same download gives the
same bytes.

The list ships as a Python module (the gzip of a TSV, as base64 text) rather than a data file
because the release installers already in the field accept only engine/*.py and
engine/static/*.png; anything else would make them refuse the release.

Data: GeoNames (https://www.geonames.org), CC BY 4.0
(https://creativecommons.org/licenses/by/4.0/). The licence needs the credit, so the file
carries it in its header and engine/places.py repeats it.
"""

import argparse
import base64
import gzip
import io
import math
import os
import re
import sys
import unicodedata
import urllib.request
import zipfile

BASE_URL = "https://download.geonames.org/export/dump/"
MIN_POPULATION = 3000   # about 91000 places; 2000 (111000) still fits the 2.5 MB release, 1000 and below do not
SOURCE = "cities1000"   # GeoNames' list of places of 1000 people or more
OTHER_FILES = ("admin1CodesASCII.txt", "countryInfo.txt")
DEFAULT_DOWNLOAD_DIR = os.path.expanduser("~/.cache/claude-work/dlne/geonames")
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine", "places_data.py")
MAX_BYTES = 2 * 1024 * 1024
MAX_NAME = 80   # a name is shown to the model as plain text; nothing longer is a town name

HEADER = (
    "# Place names for DLNE's offline location lookup (engine/places.py).\n"
    "# Source: GeoNames, https://www.geonames.org , licensed CC BY 4.0\n"
    "# (https://creativecommons.org/licenses/by/4.0/). Towns and districts of\n"
    "# 3000 people or more (GeoNames cities1000 list, cut in scripts/build_places.py).\n"
    "# Rows: latitude step, longitude (hundredths of a degree), size class, town, region number.\n"
    "# Built by scripts/build_places.py.\n"
)


def download(directory: str, source: str = SOURCE) -> None:
    os.makedirs(directory, exist_ok=True)
    for name in (source + ".zip",) + OTHER_FILES:
        target = os.path.join(directory, name)
        if os.path.isfile(target):
            continue
        with urllib.request.urlopen(BASE_URL + name, timeout=120) as response:
            data = response.read()
        with open(target + ".part", "wb") as handle:
            handle.write(data)
        os.replace(target + ".part", target)


def clean(text: str) -> str:
    """A name safe to put in a prompt and a TSV: no control characters, no tabs, trimmed. '' if nothing is left."""
    kept = "".join(" " if ch in "\t\r\n" else ch for ch in text if unicodedata.category(ch)[0] != "C" or ch in "\t\r\n")
    kept = " ".join(kept.split())
    return kept if len(kept) <= MAX_NAME else ""


def _lines(raw: bytes):
    for line in raw.decode("utf-8").splitlines():
        if line and not line.startswith("#"):
            yield line.split("\t")


def read_countries(raw: bytes) -> dict:
    return {f[0]: clean(f[4]) for f in _lines(raw) if len(f) > 4}


def read_regions(raw: bytes) -> dict:
    # "CC.code <tab> name <tab> ascii name <tab> geonameid"
    return {f[0]: clean(f[1]) for f in _lines(raw) if len(f) > 1}


_DISTRICT_RE = re.compile(r"^(.*\S) \d{1,2}\b")


def build_rows(cities: bytes, regions: dict, countries: dict, min_population: int = 0) -> list:
    rows = set()
    for f in _lines(cities):
        if len(f) < 11:
            continue
        try:
            lat, lon = float(f[4]), float(f[5])
        except ValueError:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        try:
            population = max(0, int(f[14]))
        except (ValueError, IndexError):
            population = 0
        if population < min_population:
            continue
        town = clean(f[1])
        country = countries.get(f[8], "")
        if not town or not country:
            continue
        region = regions.get(f"{f[8]}.{f[10]}", "")
        rows.add((round(lat, 2) + 0.0, round(lon, 2) + 0.0, population, town, region, country))   # + 0.0: no "-0.0"
    # "Paris 04 Hôtel-de-Ville", "Lyon 03 Part-Dieu": numbered districts of a town that is listed itself.
    towns = {(r[5], r[4], r[3]) for r in rows}
    return sorted(r for r in rows
                  if not ((m := _DISTRICT_RE.match(r[3])) and (r[5], r[4], m.group(1)) in towns))


def size_class(population: int) -> int:
    """A quarter of a power of ten, in whole numbers (1000 -> 12, 1778 -> 13, 10000 -> 16): enough to say how much bigger a place is."""
    return int(4 * math.log10(max(population, 1)))


def render(rows: list) -> bytes:
    """
    The TSV, gzipped. "@<tab>region<tab>country" lines come first (a row names its region by position
    in that list, since the same few thousand pairs repeat on every row). Each row is: latitude in
    hundredths of a degree as the step from the previous row (rows are sorted by latitude), longitude
    in hundredths, size class, town, region number.
    """
    pairs = {}
    for row in rows:
        pairs.setdefault((row[4], row[5]), len(pairs))
    lines = [HEADER] + [f"@\t{region}\t{country}\n" for region, country in pairs]
    previous = 0
    for lat, lon, people, town, region, country in rows:
        step = round(lat * 100)
        lines.append(f"{step - previous}\t{round(lon * 100)}\t{size_class(people)}\t{town}\t{pairs[(region, country)]}\n")
        previous = step
    text = "".join(lines)
    buffer = io.BytesIO()
    # mtime 0 and no stored file name: the gzip header would otherwise change every build.
    with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, compresslevel=9, mtime=0) as handle:
        handle.write(text.encode("utf-8"))
    return buffer.getvalue()


MODULE_DOC = (
    '"""\n'
    "The place list behind engine/places.py: GeoNames towns and districts of 3000 people or more,\n"
    "as the gzip of a compact tab-separated file (see render() in the builder), base64-encoded.\n"
    "GENERATED by scripts/build_places.py; do not edit by hand.\n"
    "\n"
    "Place data: GeoNames (https://www.geonames.org), licensed CC BY 4.0\n"
    "(https://creativecommons.org/licenses/by/4.0/).\n"
    '"""\n'
)


def render_module(payload: bytes) -> str:
    """The source of engine/places_data.py: DATA_B64 holds the gzip bytes, wrapped so no line is long."""
    text = base64.b64encode(payload).decode("ascii")
    lines = [text[i:i + 76] for i in range(0, len(text), 76)]
    return MODULE_DOC + '\nDATA_B64 = """\\\n' + "\n".join(lines) + '\n"""\n'


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--download-dir", default=DEFAULT_DOWNLOAD_DIR)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--min-population", type=int, default=MIN_POPULATION)
    parser.add_argument("--source", default=SOURCE, choices=("cities500", "cities1000", "cities5000", "cities15000"))
    args = parser.parse_args(argv)

    download(args.download_dir, args.source)
    path = lambda name: os.path.join(args.download_dir, name)  # noqa: E731
    with zipfile.ZipFile(path(args.source + ".zip")) as archive:
        cities = archive.read(args.source + ".txt")   # by name, never extracted to disk
    with open(path("admin1CodesASCII.txt"), "rb") as handle:
        regions = read_regions(handle.read())
    with open(path("countryInfo.txt"), "rb") as handle:
        countries = read_countries(handle.read())

    rows = build_rows(cities, regions, countries, args.min_population)
    payload = render(rows)
    if len(payload) > MAX_BYTES:
        print(f"the place list would be {len(payload)} bytes, over the {MAX_BYTES} limit.", file=sys.stderr)
        return 1
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "wb") as handle:
        handle.write(render_module(payload).encode("ascii"))
    print(f"{len(rows)} places, {len(payload)} bytes gzipped -> {os.path.abspath(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
