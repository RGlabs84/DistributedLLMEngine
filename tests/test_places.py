"""The offline place-name lookup (engine/places.py) and the list it reads (engine/places_data.py)."""

import base64
import gzip
import math
import os
import subprocess
import sys
import time

import pytest

from engine import places, places_data


@pytest.fixture
def fresh_places(monkeypatch):
    """Let a test swap the list; the real one is loaded again for whoever runs next."""
    monkeypatch.setattr(places, "_loaded", False)
    yield
    monkeypatch.undo()
    places._loaded = False


@pytest.mark.parametrize("lat, lon, expected", [
    (51.5074, -0.1278, "London, England, United Kingdom"),
    (48.8566, 2.3522, "Paris, Île-de-France, France"),   # not "Paris 04 Hôtel-de-Ville": districts are left out
    (-33.8688, 151.2093, "Sydney, New South Wales, Australia"),
    (40.7128, -74.0060, "New York City, New York, United States"),
    (64.15, -21.80, "Reykjavík, Capital Region, Iceland"),
    (35.68, 139.69, "Tokyo, Japan"),   # region and town share a name: said once
])
def test_known_coordinates_name_the_nearest_town(lat, lon, expected):
    assert places.describe(lat, lon) == expected


@pytest.mark.parametrize("lat, lon, expected", [
    (30.0131, 31.2089, "Giza, Egypt"),                                     # the middle of Giza
    (51.539, -0.143, "Camden Town, England, United Kingdom"),             # a London neighbourhood, not the borough next door
    (40.7644, -73.923, "Astoria, New York, United States"),               # a district of 150 000 keeps its name beside Manhattan
    (40.672, -73.977, "Brooklyn, New York, United States"),
    (25.0805, 55.1403, "Dubai Marina, Dubai, United Arab Emirates"),
    (51.80, -1.90, "Cirencester, England, United Kingdom"),               # countryside: the nearest town
    (41.5, -100.5, "North Platte, Nebraska, United States"),
])
def test_a_precise_fix_names_the_neighbourhood_or_town(lat, lon, expected):
    assert places.describe(lat, lon) == expected


@pytest.mark.parametrize("lat, lon, expected", [
    (22.252, 114.162, "Hong Kong, Hong Kong"),                  # Yue Fai Court (nearest) is a housing estate
    (22.292, 114.182, "Hong Kong, Hong Kong"),                  # so is Fortress Garden
    (35.67, 139.712, "Tokyo, Japan"),                           # Sendagaya (nearest) is a district of Tokyo
    (60.143, 24.961, "Helsinki, Uusimaa, Finland"),             # Ullanlinna (nearest) is a quarter of Helsinki
    (41.373, 2.2, "Barcelona, Catalonia, Spain"),
])
def test_a_small_place_beside_a_big_city_names_the_city(lat, lon, expected):
    assert places.describe(lat, lon) == expected


def test_the_ocean_is_none():
    assert places.describe(0.0, -30.0) is None      # mid-Atlantic
    assert places.describe(-48.9, -123.4) is None   # Point Nemo
    assert places.describe(-89.9, 0.0) is None      # the pole


def test_the_fifty_km_limit():
    # Due north of Reykjavik there is open sea: only the distance to the town decides.
    assert places.describe(64.15 + 0.35, -21.80) is not None   # ~39 km
    assert places.describe(64.15 + 0.80, -21.80 + 0.80) is None  # well beyond 50 km of any listed town


@pytest.mark.parametrize("lat, lon", [
    (math.nan, 0), (0, math.nan), (math.inf, 0), (0, -math.inf),
    (91, 0), (-90.5, 0), (0, 181), (0, -180.1),
    ("51.5", "-0.12"), ("abc", 1), (None, None), (None, 5), ([1], [2]), ({}, {}),
    (True, False), (b"1", b"2"),
    (10**400, 0), (0, -10**400), (10**4000, 10**4000), (1e400, 0),      # an int too large for a float is no number
])
def test_bad_input_is_none_and_never_raises(lat, lon):
    assert places.describe(lat, lon) is None


def test_whole_numbers_and_the_edges_of_the_range_are_accepted():
    assert places.describe(52, 0) == "Royston, England, United Kingdom"
    assert places.describe(90, 180) is None
    assert places.describe(-90, -180) is None


def test_a_lookup_is_fast():
    places.describe(51.5, -0.12)   # the one-time load is not what a phone message waits on
    start = time.perf_counter()
    for _ in range(200):
        places.describe(40.71, -74.0)
    assert (time.perf_counter() - start) / 200 < 0.005


def _rows_of(module_text):
    return [line.split("\t") for line in module_text.splitlines() if not line.startswith("#")]


def test_the_attribution_is_in_the_modules_and_in_the_data():
    assert "GeoNames" in places.__doc__ and "CC BY 4.0" in places.__doc__
    assert "GeoNames" in places_data.__doc__ and "CC BY 4.0" in places_data.__doc__ and "geonames.org" in places_data.__doc__
    text = gzip.decompress(base64.b64decode(places_data.DATA_B64)).decode("utf-8")
    header = "\n".join(line for line in text.splitlines() if line.startswith("#"))
    assert "GeoNames" in header and "CC BY 4.0" in header and "geonames.org" in header


def test_the_data_module_is_small_and_well_formed():
    path = places_data.__file__
    assert os.path.getsize(path) <= 1536 * 1024   # the release archive carries it: see test_release.py's cap
    with open(path, "rb") as handle:
        source = handle.read()
    assert source.isascii() and max(len(line) for line in source.splitlines()) <= 100
    rows = _rows_of(gzip.decompress(base64.b64decode(places_data.DATA_B64)).decode("utf-8"))
    regions = [r for r in rows if r[0] == "@"]
    rows = [r for r in rows if r[0] != "@"]
    assert len(rows) > 50000
    assert all(len(r) == 3 and r[2] for r in regions)   # a region may be empty, a country may not
    assert all(len(r) == 5 and r[3] and 0 <= int(r[4]) < len(regions) for r in rows)
    assert all(int(r[0]) >= 0 for r in rows[1:])   # latitude only ever steps up: that is what the bisect relies on
    lat = 0
    for r in rows:
        lat += int(r[0])
        assert -9000 <= lat <= 9000 and -18000 <= int(r[1]) <= 18000


def _list_text(*places_):
    """The file text for (lat, lon, size class, town, region, country) rows given in latitude order."""
    regions = {}
    for _lat, _lon, _size, _town, region, country in places_:
        regions.setdefault((region, country), len(regions))
    lines = [f"@\t{region}\t{country}\n" for region, country in regions]
    previous = 0
    for lat, lon, size, town, region, country in places_:
        step = round(lat * 100)
        lines.append(f"{step - previous}\t{round(lon * 100)}\t{size}\t{town}\t{regions[(region, country)]}\n")
        previous = step
    return "".join(lines)


def _use_list(monkeypatch, text):
    monkeypatch.setattr(places_data, "DATA_B64", base64.b64encode(gzip.compress(text.encode("utf-8"))).decode("ascii"))


def test_places_is_not_imported_by_the_worker():
    """The half megabyte of place data is read only when a location arrives."""
    code = "import sys, engine.places; assert 'engine.places_data' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True, cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_a_missing_list_gives_none_instead_of_an_error(fresh_places, monkeypatch):
    monkeypatch.delattr(places_data, "DATA_B64")
    assert places.describe(51.5, -0.12) is None


@pytest.mark.parametrize("damaged", ["", "not base64 !!!", "AAAA", base64.b64encode(b"plain, not gzip").decode(),
                                     base64.b64encode(gzip.compress(b"\xff\xfe not utf-8")).decode(),
                                     base64.b64encode(gzip.compress(b"x" * 100)[:-9]).decode()])
def test_a_damaged_list_gives_none(fresh_places, monkeypatch, damaged):
    monkeypatch.setattr(places_data, "DATA_B64", damaged)
    assert places.describe(10.0, 10.0) is None


def test_a_damaged_list_warns_once(fresh_places, monkeypatch, caplog):
    monkeypatch.setattr(places_data, "DATA_B64", "AAAA")
    with caplog.at_level("WARNING", logger="engine.places"):
        places.describe(10.0, 10.0)
        places.describe(11.0, 11.0)
    assert len([r for r in caplog.records if "Place list unavailable" in r.getMessage()]) == 1


def test_bad_rows_are_skipped_and_good_ones_kept(fresh_places, monkeypatch):
    _use_list(monkeypatch, "# header\nnot a row\nx\ty\tBad\t\tNowhere\n"
              "@\tGoodRegion\tGoodLand\n1000\t1000\t12\tNoRegion\t7\n0\t1000\t12\t\t0\n0\t1000\t12\tGoodTown\t0\n")
    assert places.describe(10.0, 10.0) == "GoodTown, GoodRegion, GoodLand"


def test_the_date_line_is_not_a_boundary(fresh_places, monkeypatch):
    _use_list(monkeypatch, _list_text((10.0, 179.99, 12, "WestSide", "R", "Land")))
    assert places.describe(10.0, -179.99) == "WestSide, R, Land"


def test_the_rule_for_a_bigger_place_nearby(fresh_places, monkeypatch):
    # 0.01 degree of latitude is about 1.1 km. The point is at 10.00 N.
    _use_list(monkeypatch, _list_text(
        (10.00, 20.00, 14, "Suburb", "R", "Land"),    # on the spot, 1 000 people
        (10.02, 20.00, 20, "City", "R", "Land"),      # 2.2 km farther, a million: far bigger, close enough
        (10.02, 20.02, 16, "Town", "R", "Land"),      # bigger, but not by thirty times
    ))
    assert places.describe(10.00, 20.00) == "City, R, Land"
    assert places.describe(9.995, 20.00) == "City, R, Land"
    _use_list(monkeypatch, _list_text((10.00, 20.00, 14, "Suburb", "R", "Land"), (10.05, 20.00, 20, "FarCity", "R", "Land")))
    places._loaded = False
    assert places.describe(10.00, 20.00) == "Suburb, R, Land"   # the city is 5.5 km farther: too far to count


def test_a_bigger_place_must_be_much_bigger(fresh_places, monkeypatch):
    _use_list(monkeypatch, _list_text((10.00, 20.00, 16, "Near", "R", "Land"), (10.02, 20.00, 21, "Bigger", "R", "Land")))
    assert places.describe(10.0, 20.0) == "Near, R, Land"     # 5 classes: not enough
    places._loaded = False
    _use_list(monkeypatch, _list_text((10.00, 20.00, 16, "Near", "R", "Land"), (10.02, 20.00, 22, "Bigger", "R", "Land")))
    assert places.describe(10.0, 20.0) == "Bigger, R, Land"   # 6 classes: enough


def test_an_equal_tie_goes_to_the_nearer_place(fresh_places, monkeypatch):
    _use_list(monkeypatch, _list_text((10.00, 20.00, 14, "A", "R", "Land"), (10.01, 20.00, 14, "B", "R", "Land")))
    assert places.describe(10.004, 20.0) == "A, R, Land"
    assert places.describe(10.006, 20.0) == "B, R, Land"


# --- scripts/build_places.py ---------------------------------------------------------------

@pytest.fixture(scope="module")
def build_places():
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "build_places.py")
    spec = importlib.util.spec_from_file_location("build_places_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _city(name, lat, lon, cc="XX", admin1="01", code="PPL", people=0):
    fields = ["1", name, name, "", str(lat), str(lon), "P", code, cc, "", admin1, "", "", "", str(people)] + [""] * 4
    return "\t".join(fields)


def test_the_builder_keeps_towns_and_sections_drops_numbered_districts_and_cleans_names(build_places):
    cities = "\n".join([
        _city("Bigtown", 10.004, 20.006),
        _city("Bigtown 04 Centre", 10.01, 20.01),     # numbered district of a listed town
        _city("Oldquarter", 10.02, 20.02, code="PPLX", people=5000),   # a neighbourhood: kept, it is what a precise fix needs
        _city("Tab\x07 \u202eTown", 11, 21),       # control and bidi-override characters never reach the file
        _city("Nowhere", 12, 22, cc="ZZ"),            # unknown country
        _city("Badlat", "north", 22),
    ]).encode()
    rows = build_places.build_rows(cities, {"XX.01": "Region One"}, {"XX": "Xland"})
    assert rows == [(10.0, 20.01, 0, "Bigtown", "Region One", "Xland"), (10.02, 20.02, 5000, "Oldquarter", "Region One", "Xland"),
                    (11.0, 21.0, 0, "Tab Town", "Region One", "Xland")]


def test_the_builder_cuts_by_population_and_sizes_places(build_places):
    cities = "\n".join([_city("Big", 1, 1, people=50000), _city("Small", 2, 2, people=2999), _city("Unknown", 3, 3)]).encode()
    rows = build_places.build_rows(cities, {}, {"XX": "Xland"}, min_population=3000)
    assert [r[3] for r in rows] == ["Big"]
    classes = [build_places.size_class(p) for p in (0, 1, 999, 1000, 3000, 20000, 1000000, 9000000)]
    assert classes == sorted(classes) and classes[:2] == [0, 0] and classes[3] == 12 and classes[6] == 24
    assert build_places.size_class(300000) - build_places.size_class(10000) >= 5 and build_places.size_class(300000) - build_places.size_class(10000) <= 7


def test_the_builder_output_is_the_same_bytes_every_time(build_places):
    rows = [(1.0, 2.0, 5000, "A", "B", "C"), (3.0, 4.0, 0, "D", "", "E"), (3.0, -4.5, 90000, "F", "B", "C")]
    assert build_places.render(rows) == build_places.render(rows)
    text = gzip.decompress(build_places.render(rows)).decode("utf-8")
    assert "CC BY 4.0" in text and "geonames.org" in text
    # regions are numbered in order of first use; latitude is a step from the previous row, longitude is absolute
    assert text.endswith("@\tB\tC\n@\t\tE\n100\t200\t14\tA\t0\n200\t400\t0\tD\t1\n0\t-450\t19\tF\t0\n") 
    assert "@\tB\tC\n@\t\tE\n" in text


def test_the_module_text_decodes_to_the_same_bytes_and_has_short_lines(build_places):
    payload = build_places.render([(1.0, 2.0, 5000, "A", "B", "C")])
    namespace = {}
    exec(compile(build_places.render_module(payload), "places_data_under_test", "exec"), namespace)   # our own generator's output
    assert base64.b64decode(namespace["DATA_B64"]) == payload
    assert max(len(line) for line in build_places.render_module(payload).splitlines()) <= 100
    assert build_places.render_module(payload) == build_places.render_module(payload)
