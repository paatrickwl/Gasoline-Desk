"""
forecast_engine.py -- Oil Price Driver & Forecast Module

Two honest, separate things live here, and they must not be confused:

1. `OIL_PRICE_DRIVERS` -- a documented, qualitative catalog of what actually
   moves Brent/ICP/MOPS benchmark prices (OPEC+ supply policy, US shale/EIA
   inventories, global demand, USD strength, geopolitical/choke-point risk,
   seasonal demand, Indonesia-specific FX/policy factors). This is reference
   knowledge, not a fitted model -- it explains WHY, it doesn't predict a
   number. Where we have a REAL live signal for one of these factors (choke
   point mentions from `osint_scraper.py`'s actual fetched headlines), the
   driver read is grounded in that live data, not invented.

2. `ForecastEngine.forecast()` -- an actual numeric projection, gated hard on
   whether real historical time-series data has been loaded. No historical
   data -> the method says so explicitly and returns a flat carry-forward of
   the current baseline (method="flat_carry_forward_no_history"), not a
   confident-looking guess. Historical data present (`data/historical_prices.csv`)
   -> a simple linear trend extrapolation (numpy polyfit), labeled for what
   it is: a naive trend line, not a calibrated econometric model.

Anti-hallucination stance: this module will never present a driver-based
narrative as if it were a statistical forecast, and never present a
statistical forecast with a confidence it hasn't earned.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORICAL_CSV_PATH = os.path.join(BASE_DIR, "data", "historical_prices.csv")
MIN_POINTS_FOR_TREND = 10

# -- Qualitative driver catalog -----------------------------------------------
# Reference knowledge, not fitted coefficients. Each entry: what it is, why it
# moves benchmark crude prices, and (where we can) which of our own real
# signals lets us give a CURRENT read on it rather than a static description.
OIL_PRICE_DRIVERS = [
    {
        "factor": "OPEC+ supply policy",
        "category": "supply",
        "description": "Quota decisions (production cuts/increases) from OPEC+ directly move global crude supply and are historically the single biggest lever on Brent/ICP.",
        "typical_effect": "Cuts -> price up. Unwinding cuts / quota increases -> price down.",
        "live_signal_available": False,
    },
    {
        "factor": "US shale output & EIA inventory data",
        "category": "supply",
        "description": "Weekly EIA crude inventory draws/builds and US shale production levels signal near-term supply tightness or glut.",
        "typical_effect": "Inventory draw -> price up. Inventory build -> price down.",
        "live_signal_available": False,
    },
    {
        "factor": "Global demand outlook (IEA/OPEC reports, China/India growth)",
        "category": "demand",
        "description": "Monthly IEA/OPEC demand-growth revisions and Chinese/Indian industrial activity data set the demand side of the balance.",
        "typical_effect": "Upward demand revision -> price up. Downward revision (e.g. recession fears) -> price down.",
        "live_signal_available": False,
    },
    {
        "factor": "USD strength (DXY)",
        "category": "macro",
        "description": "Crude is dollar-denominated; a stronger dollar makes oil more expensive for non-USD buyers, which typically softens demand and price.",
        "typical_effect": "DXY up -> Brent/ICP down (inverse correlation). DXY down -> Brent/ICP up.",
        "live_signal_available": False,
    },
    {
        "factor": "Geopolitical & choke-point risk",
        "category": "logistics",
        "description": "Conflict or shipping disruption near Hormuz, Malacca, Suez, or Red Sea raises freight risk premiums and can constrain physical supply routes.",
        "typical_effect": "Escalation/choke-point disruption -> price up (risk premium). De-escalation -> premium fades.",
        "live_signal_available": True,  # sourced from osint_scraper's real headline mentions
    },
    {
        "factor": "Seasonal demand cycles",
        "category": "demand",
        "description": "Northern hemisphere winter heating demand and the US summer driving season create predictable seasonal demand swings.",
        "typical_effect": "Winter/summer peak seasons -> price support. Shoulder seasons -> softer demand.",
        "live_signal_available": False,
    },
    {
        "factor": "Rupiah / JISDOR FX move",
        "category": "indonesia_specific",
        "description": "Even if the USD/bbl benchmark is flat, IDR depreciation directly raises the domestic Rp/liter cost -- this is an Indonesia-specific pass-through driver, not a global crude driver.",
        "typical_effect": "IDR depreciation -> higher domestic market-clearing price even at flat benchmark. IDR appreciation -> lower.",
        "live_signal_available": False,
    },
    {
        "factor": "ESDM/Pertamina policy & subsidy allocation timing",
        "category": "indonesia_specific",
        "description": "Government decisions on subsidy quota, administered price resets, and fuel mix policy directly set the retail side of the gap independent of the cost side.",
        "typical_effect": "Policy tightening/quota cuts -> administered price pressure. Subsidy budget increases -> administered price held down regardless of cost.",
        "live_signal_available": True,  # sourced from osint_scraper's real "policy" category headlines
    },
]


@dataclass
class HistoricalSeries:
    dates: list
    values: list

    @property
    def has_enough_points(self) -> bool:
        return len(self.values) >= MIN_POINTS_FOR_TREND


def load_historical_series(benchmark_key: str, path: str = HISTORICAL_CSV_PATH) -> Optional[HistoricalSeries]:
    """Loads data/historical_prices.csv if present. Expected columns:
    date (YYYY-MM-DD), benchmark_key, value. Returns None if the file
    doesn't exist or has no rows for this benchmark -- callers must treat
    None as "no real history available", not silently fall back to a guess."""
    if not os.path.exists(path):
        return None

    dates, values = [], []
    with open(path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("benchmark_key") != benchmark_key:
                continue
            try:
                d = datetime.strptime(row["date"], "%Y-%m-%d")
                v = float(row["value"])
            except (KeyError, ValueError):
                continue
            dates.append(d)
            values.append(v)

    if not values:
        return None

    paired = sorted(zip(dates, values), key=lambda p: p[0])
    dates, values = [p[0] for p in paired], [p[1] for p in paired]
    return HistoricalSeries(dates=dates, values=values)


@dataclass
class ForecastResult:
    benchmark_key: str
    method: str  # "linear_trend" | "flat_carry_forward_no_history"
    current_value: float
    projected_value: float
    horizon_days: int
    confidence_note: str
    history_points_used: int = 0


class ForecastEngine:
    def __init__(self, baseline: dict):
        self.baseline = baseline

    def explain_drivers(self, osint_risk_summary: Optional[dict] = None) -> list[dict]:
        """Returns the driver catalog, annotated with a live read wherever
        `live_signal_available` is True and a real OSINT risk summary was
        passed in (from osint_scraper.summarize_risk_signals())."""
        drivers = [dict(d) for d in OIL_PRICE_DRIVERS]  # shallow copy, don't mutate the module constant
        if not osint_risk_summary:
            return drivers

        for d in drivers:
            if d["factor"] == "Geopolitical & choke-point risk":
                congestion = osint_risk_summary.get("avg_choke_point_congestion", 0)
                risk_level = osint_risk_summary.get("risk_level", "normal")
                d["current_read"] = (
                    f"Live read from real headlines just fetched: choke-point congestion index "
                    f"{congestion:.2f} ({risk_level}). Source: {osint_risk_summary.get('source', 'unknown')}."
                )
            elif d["factor"] == "ESDM/Pertamina policy & subsidy allocation timing":
                policy_count = osint_risk_summary.get("policy_signal_count", 0)
                d["current_read"] = (
                    f"Live read: {policy_count} policy-tagged headline(s) in the current OSINT pull. "
                    f"Source: {osint_risk_summary.get('source', 'unknown')}."
                )
        return drivers

    def forecast(self, benchmark_key: str, horizon_days: int = 30) -> ForecastResult:
        current_value = self.baseline["benchmarks"].get(benchmark_key)
        if current_value is None:
            raise KeyError(f"Unknown benchmark_key: {benchmark_key}")

        series = load_historical_series(benchmark_key)

        if series is None or not series.has_enough_points:
            points_available = len(series.values) if series else 0
            return ForecastResult(
                benchmark_key=benchmark_key,
                method="flat_carry_forward_no_history",
                current_value=current_value,
                projected_value=current_value,
                horizon_days=horizon_days,
                confidence_note=(
                    f"No usable historical time series for {benchmark_key} "
                    f"({points_available}/{MIN_POINTS_FOR_TREND} points in data/historical_prices.csv). "
                    f"This is NOT a forecast -- it is the current baseline held flat. Add real daily/weekly "
                    f"{benchmark_key} history to data/historical_prices.csv to enable an actual trend projection."
                ),
                history_points_used=points_available,
            )

        # Simple linear trend extrapolation on day-index vs value. This is a
        # naive method by design -- no seasonality, no mean reversion, no
        # macro model -- and is labeled as such rather than dressed up.
        day_indices = np.array([(d - series.dates[0]).days for d in series.dates], dtype=float)
        values = np.array(series.values, dtype=float)
        slope, intercept = np.polyfit(day_indices, values, 1)

        last_day_index = day_indices[-1]
        projected_day_index = last_day_index + horizon_days
        projected_value = float(slope * projected_day_index + intercept)

        return ForecastResult(
            benchmark_key=benchmark_key,
            method="linear_trend",
            current_value=float(values[-1]),
            projected_value=round(projected_value, 3),
            horizon_days=horizon_days,
            confidence_note=(
                f"Simple linear trend fit on {len(values)} historical points "
                f"({series.dates[0].date()} to {series.dates[-1].date()}). No seasonality, mean-reversion, "
                f"or macro drivers modeled -- treat as directional only, not a calibrated price target."
            ),
            history_points_used=len(values),
        )


if __name__ == "__main__":
    import json

    from engine import load_baseline

    baseline = load_baseline()
    engine = ForecastEngine(baseline)

    print("=== Driver catalog ===")
    for d in engine.explain_drivers():
        print(f"- [{d['category']}] {d['factor']}: {d['typical_effect']}")

    print("\n=== Forecast (ICP, 30 days) ===")
    result = engine.forecast("icp_usd_bbl", horizon_days=30)
    print(json.dumps(result.__dict__, indent=2, default=str))
