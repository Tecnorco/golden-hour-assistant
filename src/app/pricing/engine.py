"""Deterministic pricing, distance and availability. No LLM anywhere in here.

All money is computed with Decimal and rounded half-up to cents, then exposed
as floats for JSON. Every rule comes from data/pricing.yaml.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel

from app.models import QuoteLine, QuoteResult

CENT = Decimal("0.01")


class PricingError(ValueError):
    """Invalid quote request. The message is safe to show to the agent."""


class Package(BaseModel):
    name: str
    includes: str
    price: Decimal | None = None
    hourly_rate: Decimal | None = None
    min_hours: float | None = None


class AddOnSpec(BaseModel):
    name: str
    price: Decimal
    allowed_for: list[str] | None = None


class Rules(BaseModel):
    weekend_surcharge_pct: Decimal
    free_travel_miles: Decimal
    travel_rate_per_mile: Decimal
    deposit_pct: Decimal


class PriceList(BaseModel):
    currency: str
    studio_town: str
    packages: dict[str, Package]
    add_ons: dict[str, AddOnSpec]
    rules: Rules
    towns: dict[str, float]


@lru_cache(maxsize=4)
def load_price_list(path: Path) -> PriceList:
    return PriceList.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _money(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def calculate_quote(
    pl: PriceList,
    package: str,
    event_date: date,
    distance_miles: float,
    hours: float | None = None,
    add_ons: list[str] | None = None,
    extra_photo_sets: int = 0,
) -> QuoteResult:
    add_ons = list(add_ons or [])
    if package not in pl.packages:
        raise PricingError(f"unknown package '{package}'. Valid: {', '.join(pl.packages)}")
    pkg = pl.packages[package]
    if distance_miles < 0:
        raise PricingError("distance_miles cannot be negative")
    if len(set(add_ons)) != len(add_ons):
        raise PricingError("duplicate add-ons; use extra_photo_sets for more than one set of extra photos")
    for a in add_ons:
        if a not in pl.add_ons:
            raise PricingError(f"unknown add-on '{a}'. Valid: {', '.join(pl.add_ons)}")
        allowed = pl.add_ons[a].allowed_for
        if allowed and package not in allowed:
            raise PricingError(f"add-on '{a}' is only available for: {', '.join(allowed)}")
    if extra_photo_sets < 0:
        raise PricingError("extra_photo_sets cannot be negative")
    if extra_photo_sets and "extra_photos" not in add_ons:
        add_ons.append("extra_photos")
    if "extra_photos" in add_ons and extra_photo_sets == 0:
        extra_photo_sets = 1

    lines: list[QuoteLine] = []
    notes: list[str] = []

    # Package price
    if pkg.hourly_rate is not None:
        if hours is None:
            raise PricingError(f"'{package}' is priced per hour; hours is required")
        if pkg.min_hours and hours < pkg.min_hours:
            raise PricingError(f"'{package}' has a {pkg.min_hours:g}-hour minimum; got {hours:g}")
        if (hours * 2) != int(hours * 2):
            raise PricingError("hours must be in half-hour steps")
        base = _money(pkg.hourly_rate * Decimal(str(hours)))
        lines.append(QuoteLine(description=f"{pkg.name} ({hours:g} hours x ${pkg.hourly_rate:,.2f})", amount=float(base)))
    else:
        if hours not in (None, 0):
            notes.append(f"hours ignored: {pkg.name} is a fixed package ({pkg.includes})")
        assert pkg.price is not None
        base = _money(pkg.price)
        lines.append(QuoteLine(description=f"{pkg.name} ({pkg.includes})", amount=float(base)))

    # Weekend surcharge applies to the package price only.
    weekend = event_date.weekday() >= 5
    surcharge = Decimal(0)
    if weekend:
        surcharge = _money(base * pl.rules.weekend_surcharge_pct / 100)
        lines.append(
            QuoteLine(description=f"Weekend surcharge ({pl.rules.weekend_surcharge_pct:g}% of package, {event_date:%A})",
                      amount=float(surcharge))
        )

    # Add-ons
    addon_total = Decimal(0)
    for a in add_ons:
        spec = pl.add_ons[a]
        qty = extra_photo_sets if a == "extra_photos" else 1
        amount = _money(spec.price * qty)
        addon_total += amount
        label = f"{spec.name} x {qty}" if qty > 1 else spec.name
        lines.append(QuoteLine(description=label, amount=float(amount)))

    # Travel beyond the free radius
    extra_miles = max(Decimal(0), Decimal(str(distance_miles)) - pl.rules.free_travel_miles)
    travel = _money(extra_miles * pl.rules.travel_rate_per_mile)
    if travel > 0:
        lines.append(
            QuoteLine(description=f"Travel ({extra_miles.normalize():f} miles beyond {pl.rules.free_travel_miles:g} x ${pl.rules.travel_rate_per_mile:.2f})",
                      amount=float(travel))
        )
    else:
        notes.append(f"travel free within {pl.rules.free_travel_miles:g} miles")

    total = _money(base + surcharge + addon_total + travel)
    deposit = _money(total * pl.rules.deposit_pct / 100)
    return QuoteResult(
        package=package,
        package_name=pkg.name,
        event_date=event_date,
        is_weekend=weekend,
        distance_miles=distance_miles,
        lines=lines,
        total=float(total),
        deposit=float(deposit),
        deposit_pct=int(pl.rules.deposit_pct),
        notes=notes,
    )


def _norm(town: str) -> str:
    return " ".join(town.lower().replace(",", " ").split())


def lookup_distance(pl: PriceList, town: str) -> float | None:
    """Exact (case/space-insensitive) match on the town table. No fuzzy guessing:
    an unknown town must turn into a question to the customer."""
    wanted = _norm(town)
    for name, miles in pl.towns.items():
        if _norm(name) == wanted:
            return float(miles)
    return None


AvailabilityStatus = Literal["available", "booked", "blocked", "past"]


class Calendar(BaseModel):
    booked: set[date]
    blocked: list[tuple[date, date]]


@lru_cache(maxsize=4)
def load_calendar(path: Path) -> Calendar:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Calendar(
        booked={date.fromisoformat(b["date"]) for b in raw.get("booked", [])},
        blocked=[(date.fromisoformat(b["start"]), date.fromisoformat(b["end"])) for b in raw.get("blocked", [])],
    )


def check_availability(cal: Calendar, day: date, today: date | None = None) -> AvailabilityStatus:
    """Only the status is returned; who booked a date is private and never shown."""
    if day < (today or date.today()):
        return "past"
    if any(start <= day <= end for start, end in cal.blocked):
        return "blocked"
    if day in cal.booked:
        return "booked"
    return "available"
