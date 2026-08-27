"""
margin_optimizer.py -- Commercial Fuel Margin/Profit Optimizer

Consulting-lens module: for COMMERCIAL (non-subsidized) fuels only, finds
the retail price that maximizes margin x volume, using:

  * COGS      -> engine.py's real market-clearing price (benchmark + FX + tax stack)
  * Demand    -> a constant-elasticity demand curve (ILLUSTRATIVE assumption,
                 not measured from real consumer data -- see baseline JSON)
  * Ceiling   -> two real-world caps on how high price can realistically go:
                   1. competitor price in the same octane class (from our own
                      fuel catalog -- real comparison logic, though the
                      catalog prices themselves are still placeholders)
                   2. a willingness-to-pay ceiling multiplier (illustrative)

SCOPE GUARDRAIL: this module refuses to run on administered/subsidized
fuels (Pertalite, Solar Subsidi). "Maximizing the gap" on a state-subsidized
fuel means recommending a retail price increase / subsidy cut -- that's a
public policy question, not a business margin question, and conflating the
two would be a real mischaracterization. Call `is_administered` first and
route those fuels elsewhere.

Anti-hallucination stance: every number this module returns is either (a)
computed live from engine.py's real cost stack, or (b) explicitly tagged as
an illustrative assumption from the baseline catalog. The sensitivity table
is shown in full so the "recommended price" is auditable, not a black box.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import numpy as np


class AdministeredFuelError(Exception):
    """Raised when the optimizer is asked to run on a subsidized/administered
    fuel -- refuse rather than silently producing a 'raise the subsidized
    price' recommendation dressed up as a business number."""


@dataclass
class ElasticityEstimate:
    elasticity: float
    r_squared: float
    n_points: int
    method: str = "log_log_ols"


def estimate_elasticity_from_data(price_volume_pairs: list[tuple[float, float]]) -> ElasticityEstimate:
    """Empirical price elasticity of demand from real (price, volume) history,
    via log-log OLS: ln(volume) = a + e*ln(price). The slope `e` is the
    elasticity. This REPLACES the illustrative assumption in the baseline
    catalog once real data is available -- prefer this over the assumed
    constant whenever the caller has it.

    Raises ValueError on fewer than 4 points or non-positive price/volume
    (can't take a log of those) -- refuses to fabricate a fit from garbage
    input rather than silently returning an assumption-derived elasticity.
    """
    if len(price_volume_pairs) < 4:
        raise ValueError(f"Need at least 4 (price, volume) points to fit elasticity, got {len(price_volume_pairs)}.")

    prices = np.array([p for p, v in price_volume_pairs], dtype=float)
    volumes = np.array([v for p, v in price_volume_pairs], dtype=float)
    if np.any(prices <= 0) or np.any(volumes <= 0):
        raise ValueError("All price and volume values must be positive to fit a log-log elasticity.")

    log_p = np.log(prices)
    log_v = np.log(volumes)
    slope, intercept = np.polyfit(log_p, log_v, 1)

    predicted = slope * log_p + intercept
    ss_res = np.sum((log_v - predicted) ** 2)
    ss_tot = np.sum((log_v - np.mean(log_v)) ** 2)
    r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

    return ElasticityEstimate(elasticity=round(float(slope), 3), r_squared=round(float(r_squared), 3), n_points=len(price_volume_pairs))


@dataclass
class PricePoint:
    price_idr_liter: float
    price_delta_pct: float
    volume_index: float
    margin_idr_liter: float
    profit_index: float
    within_ceiling: bool


@dataclass
class OptimizationResult:
    fuel_key: str
    fuel_label: str
    cogs_idr_liter: float
    current_price_idr_liter: float
    recommended_price_idr_liter: float
    recommended_margin_idr_liter: float
    current_margin_idr_liter: float
    binding_constraint: str  # "elasticity_optimum" | "competitor_ceiling" | "wtp_ceiling" | "current_price"
    competitor_reference_price: Optional[float]
    competitor_ceiling: Optional[float]
    wtp_ceiling: float
    elasticity: float
    sensitivity_table: list = field(default_factory=list)
    caveats: list = field(default_factory=list)


def get_competitor_prices(fuel_key: str, baseline: dict) -> list[dict]:
    """Other brands' fuels in the same octane_class -- real comparison logic
    against whatever prices the catalog currently holds (placeholder or
    real, depending on what's been loaded into baseline_parameters.json)."""
    fuels = baseline.get("fuels", {})
    this_fuel = fuels.get(fuel_key)
    if not this_fuel:
        return []
    octane_class = this_fuel.get("octane_class")
    this_brand = this_fuel.get("brand")

    return [
        {"fuel_key": k, "label": v["label"], "brand": v["brand"], "price_idr_liter": v["price_idr_liter"]}
        for k, v in fuels.items()
        if k != fuel_key and v.get("octane_class") == octane_class and v.get("brand") != this_brand
    ]


def _volume_index(price: float, base_price: float, elasticity: float, base_index: float = 100.0) -> float:
    """Constant-elasticity demand curve: Volume(P) = Volume0 * (P/P0)^elasticity.
    elasticity is negative (higher price -> lower volume)."""
    if base_price <= 0:
        return base_index
    return base_index * (price / base_price) ** elasticity


def optimize_price(
    fuel_key: str,
    engine,  # engine.PricingEngine, avoiding an import cycle by duck-typing
    benchmark_key: str,
    price_range_pct: tuple = (-10, 15),
    step_pct: float = 2.5,
    elasticity_override: Optional[float] = None,
    competitor_prices_override: Optional[list[float]] = None,
    current_price_override: Optional[float] = None,
) -> OptimizationResult:
    """
    elasticity_override: pass a real elasticity (e.g. from
        estimate_elasticity_from_data()) to replace the illustrative catalog
        assumption for this call -- does not mutate the baseline.
    competitor_prices_override: pass real, currently-observed competitor
        prices to replace the placeholder catalog prices for this call.
    current_price_override: pass this fuel's real live retail price (e.g.
        from fuel_price_scraper.py) to use as the "current price" instead
        of the static baseline catalog price -- the sensitivity table's
        0% point and the current-margin comparison both anchor on this.
    """
    baseline = engine.baseline
    fuel = baseline.get("fuels", {}).get(fuel_key)
    if not fuel:
        raise KeyError(f"Unknown fuel_key: {fuel_key}")
    if fuel.get("is_administered", False):
        raise AdministeredFuelError(
            f"{fuel['label']} is a government-administered/subsidized fuel. Margin optimization does not "
            f"apply -- 'maximizing the gap' here would mean recommending a subsidy cut / consumer price "
            f"increase, which is a public policy question, not a business pricing question."
        )

    elasticity = elasticity_override if elasticity_override is not None else fuel.get("demand_elasticity")
    wtp_multiplier = fuel.get("wtp_ceiling_multiplier")
    if elasticity is None or wtp_multiplier is None:
        raise ValueError(
            f"{fuel['label']} has no demand_elasticity / wtp_ceiling_multiplier configured in "
            f"data/baseline_parameters.json -- cannot run the optimizer without these (illustrative) inputs."
        )

    pricing_result = engine.price_fuel(benchmark_key, fuel_key)
    cogs = pricing_result["pricing"]["final_price_idr_liter"]
    current_price = current_price_override if current_price_override and current_price_override > 0 else fuel["price_idr_liter"]

    if competitor_prices_override:
        competitor_reference_price = sum(competitor_prices_override) / len(competitor_prices_override)
    else:
        competitors = get_competitor_prices(fuel_key, baseline)
        competitor_reference_price = (
            sum(c["price_idr_liter"] for c in competitors) / len(competitors) if competitors else None
        )
    # Real-world ceilings: can't realistically price far above the
    # competitive set, and can't exceed the assumed willingness-to-pay
    # threshold either.
    competitor_ceiling = competitor_reference_price * 1.03 if competitor_reference_price else None
    wtp_ceiling = current_price * wtp_multiplier
    effective_ceiling = min(c for c in [competitor_ceiling, wtp_ceiling] if c is not None)

    # -- Build the auditable sensitivity table ---------------------------
    low_pct, high_pct = price_range_pct
    steps = int(round((high_pct - low_pct) / step_pct)) + 1
    table: list[PricePoint] = []
    for i in range(steps):
        pct = low_pct + i * step_pct
        price = current_price * (1 + pct / 100)
        volume_idx = _volume_index(price, current_price, elasticity)
        margin = price - cogs
        profit_idx = margin * volume_idx / 100.0
        table.append(PricePoint(
            price_idr_liter=round(price, 2),
            price_delta_pct=round(pct, 2),
            volume_index=round(volume_idx, 2),
            margin_idr_liter=round(margin, 2),
            profit_index=round(profit_idx, 2),
            within_ceiling=price <= effective_ceiling,
        ))

    # -- Pick the recommendation: max profit_index among points that respect
    # the ceiling and keep a positive margin. -----------------------------
    feasible = [p for p in table if p.within_ceiling and p.margin_idr_liter > 0]
    if not feasible:
        # Nothing feasible (e.g. COGS already above ceiling) -- fall back to
        # holding current price rather than recommending something absurd.
        recommended = next((p for p in table if abs(p.price_delta_pct) < 1e-9), table[0])
        binding_constraint = "current_price"
    else:
        recommended = max(feasible, key=lambda p: p.profit_index)
        at_ceiling = abs(recommended.price_idr_liter - effective_ceiling) / effective_ceiling < 0.01
        if at_ceiling:
            binding_constraint = (
                "competitor_ceiling" if competitor_ceiling is not None and abs(effective_ceiling - competitor_ceiling) < 1
                else "wtp_ceiling"
            )
        else:
            binding_constraint = "elasticity_optimum"

    caveats = []
    if elasticity_override is not None:
        caveats.append(f"demand_elasticity ({elasticity}) was estimated from your uploaded price/volume data, "
                        f"not the catalog assumption.")
    else:
        caveats.append(f"demand_elasticity ({elasticity}) is an ILLUSTRATIVE assumption from "
                        f"data/baseline_parameters.json, not measured from real consumer/volume data.")
    caveats.append(f"wtp_ceiling_multiplier ({wtp_multiplier}) is an illustrative assumption -- no real "
                    f"willingness-to-pay data has been loaded.")
    caveats.append("COGS-side numbers (benchmark, FX, tax stack) are real/live.")
    caveats.append(
        "Current price is a live scraped retail price." if current_price_override
        else "Current price is the static baseline catalog placeholder -- no live price was available for this fuel/region."
    )
    if competitor_prices_override:
        caveats.append("Competitor prices used were the values you provided, not the catalog placeholders.")
    elif not competitor_reference_price:
        caveats.append(f"No competitor fuels found in the same octane class ({fuel.get('octane_class')}) -- "
                        f"ceiling is set by willingness-to-pay assumption only.")
    else:
        caveats.append("Competitor prices used for the ceiling are still placeholder catalog values pending "
                        "a real price feed.")

    return OptimizationResult(
        fuel_key=fuel_key,
        fuel_label=fuel["label"],
        cogs_idr_liter=round(cogs, 2),
        current_price_idr_liter=current_price,
        recommended_price_idr_liter=recommended.price_idr_liter,
        recommended_margin_idr_liter=recommended.margin_idr_liter,
        current_margin_idr_liter=round(current_price - cogs, 2),
        binding_constraint=binding_constraint,
        competitor_reference_price=round(competitor_reference_price, 2) if competitor_reference_price else None,
        competitor_ceiling=round(competitor_ceiling, 2) if competitor_ceiling else None,
        wtp_ceiling=round(wtp_ceiling, 2),
        elasticity=elasticity,
        sensitivity_table=[p.__dict__ for p in table],
        caveats=caveats,
    )


if __name__ == "__main__":
    import json

    from engine import PricingEngine

    engine = PricingEngine()
    result = optimize_price("pertamax_ron92", engine, "mops92_usd_bbl")
    print(json.dumps(result.__dict__, indent=2, default=str))
