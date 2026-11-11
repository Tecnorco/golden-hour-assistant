from datetime import date
from pathlib import Path

import pytest
import yaml

from app.pricing.engine import (
    PricingError, calculate_quote, check_availability, load_calendar, load_price_list, lookup_distance,
)

ROOT = Path(__file__).resolve().parents[1]
PL = load_price_list(ROOT / "data" / "pricing.yaml")
CAL = load_calendar(ROOT / "data" / "calendar.json")
TODAY = date(2026, 11, 10)
WED, SAT, SUN = date(2026, 12, 9), date(2026, 12, 12), date(2027, 3, 7)

GOLDEN = [c for c in yaml.safe_load((ROOT / "evals" / "golden.yaml").read_text())["cases"] if "expected_total" in c]


@pytest.mark.parametrize("case", GOLDEN, ids=[c["file"] for c in GOLDEN])
def test_engine_matches_hand_computed_golden_totals(case):
    f = case["expected_fields"]
    miles = lookup_distance(PL, f["location"])
    assert miles is not None
    q = calculate_quote(
        PL, f["service_type"], date.fromisoformat(str(f["event_date"])), miles,
        hours=f.get("hours"), add_ons=f.get("add_ons", []), extra_photo_sets=f.get("extra_photo_sets", 0),
    )
    assert q.total == case["expected_total"]
    assert q.deposit == case["expected_deposit"]
    assert round(sum(line.amount for line in q.lines), 2) == q.total  # itemization adds up


def test_all_golden_quotes_are_covered():
    assert len(GOLDEN) == 8


def test_package_prices():
    assert calculate_quote(PL, "mini_portrait", WED, 0).total == 175
    assert calculate_quote(PL, "standard_portrait", WED, 0).total == 350
    assert calculate_quote(PL, "wedding_half_day", WED, 0).total == 1800
    assert calculate_quote(PL, "wedding_full_day", WED, 0).total == 2900
    assert calculate_quote(PL, "event", WED, 0, hours=2).total == 450


def test_weekend_surcharge_only_on_package_price():
    q = calculate_quote(PL, "standard_portrait", SAT, 0, add_ons=["printed_album"])
    assert q.is_weekend
    assert q.total == 350 + 35 + 300  # album not surcharged
    assert calculate_quote(PL, "mini_portrait", SUN, 0).total == 192.50
    assert not calculate_quote(PL, "mini_portrait", WED, 0).is_weekend


def test_weekend_surcharge_on_hourly_event():
    assert calculate_quote(PL, "event", SAT, 0, hours=2.5).total == 562.5 + 56.25


@pytest.mark.parametrize("miles,travel", [(0, 0), (25, 0), (26, 1.5), (40, 22.5), (70, 67.5), (25.5, 0.75)])
def test_travel_free_radius_then_per_mile(miles, travel):
    assert calculate_quote(PL, "mini_portrait", WED, miles).total == pytest.approx(175 + travel)


def test_deposit_is_30_percent_of_total_rounded_to_cents():
    q = calculate_quote(PL, "standard_portrait", date(2027, 4, 8), 32, add_ons=["extra_photos"], extra_photo_sets=2)
    assert (q.total, q.deposit, q.deposit_pct) == (600.50, 180.15, 30)
    q = calculate_quote(PL, "mini_portrait", WED, 25.1)  # 175.15 -> 52.545 -> 52.55 (half-up)
    assert (q.total, q.deposit) == (175.15, 52.55)


def test_all_add_on_prices():
    q = calculate_quote(PL, "wedding_full_day", WED, 0, add_ons=[
        "second_photographer", "extra_photos", "rush_delivery", "printed_album", "engagement_session"])
    assert q.total == 2900 + 400 + 120 + 150 + 300 + 250


def test_extra_photo_sets_imply_add_on_and_multiply():
    q = calculate_quote(PL, "mini_portrait", WED, 0, extra_photo_sets=3)
    assert q.total == 175 + 360
    assert any("x 3" in line.description for line in q.lines)


@pytest.mark.parametrize("kwargs,msg", [
    (dict(package="mini_portrait", add_ons=["second_photographer"]), "only available"),
    (dict(package="event", hours=1.5), "2-hour minimum"),
    (dict(package="event"), "hours is required"),
    (dict(package="event", hours=2.25), "half-hour"),
    (dict(package="boudoir"), "unknown package"),
    (dict(package="mini_portrait", add_ons=["drone"]), "unknown add-on"),
    (dict(package="mini_portrait", add_ons=["printed_album", "printed_album"]), "duplicate"),
    (dict(package="mini_portrait", distance_miles=-1), "negative"),
])
def test_invalid_requests_raise_clear_errors(kwargs, msg):
    args = dict(event_date=WED, distance_miles=0) | kwargs
    with pytest.raises(PricingError, match=msg):
        calculate_quote(PL, **args)


def test_hours_on_fixed_package_are_ignored_with_note():
    q = calculate_quote(PL, "wedding_half_day", WED, 0, hours=6)
    assert q.total == 1800 and any("ignored" in n for n in q.notes)


@pytest.mark.parametrize("town,miles", [
    ("Willowbrook", 0), ("lakeview  springs", 48), ("Copper Valley", 70), ("ASHFORD", 25), ("Pine Harbor,", 32),
])
def test_lookup_distance_known_towns(town, miles):
    assert lookup_distance(PL, town) == miles


@pytest.mark.parametrize("town", ["Glenmoor", "Lakeview", "Springfield", "", "Pine Harbour"])
def test_lookup_distance_never_guesses(town):
    assert lookup_distance(PL, town) is None


@pytest.mark.parametrize("day,status", [
    (date(2027, 2, 14), "booked"),
    (date(2026, 12, 25), "blocked"),
    (date(2027, 1, 1), "blocked"),
    (date(2027, 3, 2), "blocked"),
    (date(2026, 12, 9), "available"),
    (date(2027, 6, 12), "available"),
    (date(2026, 11, 1), "past"),
])
def test_availability(day, status):
    assert check_availability(CAL, day, today=TODAY) == status


def test_travel_line_reads_naturally():
    q = calculate_quote(PL, "mini_portrait", WED, 48.0)
    assert any(line.description.startswith("Travel (23 miles beyond 25 x $1.50)") for line in q.lines)
