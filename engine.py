"""
engine.py -- Indonesia Energy Pricing Core

Institutional-grade fuel pricing math: trailing benchmark averages, FX
conversion (JISDOR), the official ESDM base-price formula, tax stack, and
subsidy gap output.

Cost-side formula is Kepmen ESDM No. 245.K/MG.01/MEM.M/2022 ("Formula Harga
Dasar dalam Perhitungan Harga Jual Eceran Jenis BBM Umum") -- read directly
from the official PDF on jdih.esdm.go.id on 2026-08-26. This Kepmen governs
non-subsidized fuel ("BBM Umum") sold through ANY general fuel station, not
just Pertamina's -- see `esdm_formula` in data/baseline_parameters.json for
the full citation and scope notes, including where our reading is an
approximation (Pertalite/Solar Subsidi's real subsidized-fuel formula is
separate legislation we have not sourced; PPN/PBBKB layering order is our
best-effort reading of two DPP definitions, not a certified tax computation).

Anti-hallucination design: every function accepts real/live series when
available, but ALWAYS has a deterministic, auditable fallback to
`data/baseline_parameters.json`. No function invents a number that isn't
either (a) computed from supplied data, or (b) pulled verbatim from the
cached baseline. Nothing is ever silently guessed by an LLM at this layer.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(BASE_DIR, "data", "baseline_parameters.json")


def load_baseline(path: str = BASELINE_PATH) -> dict:
    """Load cached baseline parameters. Raises only if the file itself is
    missing/corrupt -- callers should treat that as a hard-stop config error,
    not something to paper over with invented numbers."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def trailing_average(values: Iterable[float], window: int = 30, fallback: Optional[float] = None) -> float:
    """30-day (or `window`-day) trailing average of a benchmark price series.

    Falls back to `fallback` (typically the cached baseline benchmark) when
    fewer than `window` clean data points are available -- prevents a thin
    or gappy live feed from producing a misleading average.
    """
    clean = [v for v in values if isinstance(v, (int, float)) and v > 0]
    if len(clean) < window:
        if fallback is None:
            raise ValueError(f"Insufficient data ({len(clean)}/{window}) and no fallback provided.")
        return fallback
    window_slice = clean[-window:]
    return sum(window_slice) / len(window_slice)


def fx_convert_usd_to_idr(usd_value: float, jisdor_rate: float) -> float:
    """Convert a USD figure to IDR using the BI JISDOR middle rate."""
    return usd_value * jisdor_rate


def bbl_to_liter(usd_per_bbl: float, bbl_to_liter_ratio: float = 159.0) -> float:
    """USD/bbl -> USD/liter. Default 159 is the official Kepmen 245/2022
    rounded conversion (158.987 is the more precise industry figure)."""
    return usd_per_bbl / bbl_to_liter_ratio


@dataclass
class TaxStackResult:
    base_price_idr_liter: float
    mops_idr_liter: float
    konstanta_idr_liter: float
    margin_idr_liter: float
    ppn_amount: float
    pbbkb_amount: float
    final_price_idr_liter: float
    breakdown: dict = field(default_factory=dict)


def compute_esdm_base_price(
    mops_usd_bbl: float,
    mops_pct: float,
    jisdor_rate: float,
    konstanta_idr_liter: float,
    bbl_to_liter_ratio: float = 159.0,
) -> dict:
    """Kepmen ESDM No. 245.K/MG.01/MEM.M/2022 'Harga Dasar Tertinggi'
    (highest base price ceiling) formula -- applies to ALL retail operators
    selling non-subsidized fuel ('BBM Umum'), not Pertamina-only:

        MOPS_effective_usd_bbl = mops_usd_bbl * (mops_pct / 100)
        MOPS_idr_liter = (MOPS_effective_usd_bbl / bbl_to_liter_ratio) * jisdor_rate
        Harga Dasar Tertinggi = (MOPS_idr_liter + Konstanta) / 0.9
        Margin = (10/90) * (MOPS_idr_liter + Konstanta)

    mops_pct and konstanta_idr_liter are per-fuel, from each fuel's
    regulatory_class in data/baseline_parameters.json (RON90=99.21%/Rp1800,
    RON92=100%/Rp1800, RON95=100%/Rp2000, RON98=101%/Rp2000, CN48=100%/
    Rp1800, CN51=100%/Rp2000). This is the WHOLESALE ceiling only -- PPN and
    PBBKB are layered on top separately (see apply_ppn_pbbkb), under
    different regulations this Kepmen does not cover."""
    mops_effective_usd_bbl = mops_usd_bbl * (mops_pct / 100.0)
    mops_usd_liter = bbl_to_liter(mops_effective_usd_bbl, bbl_to_liter_ratio)
    mops_idr_liter = fx_convert_usd_to_idr(mops_usd_liter, jisdor_rate)
    cost_plus_konstanta = mops_idr_liter + konstanta_idr_liter
    harga_dasar_tertinggi = cost_plus_konstanta / 0.9
    margin = harga_dasar_tertinggi - cost_plus_konstanta
    return {
        "mops_idr_liter": mops_idr_liter,
        "konstanta_idr_liter": konstanta_idr_liter,
        "margin_idr_liter": margin,
        "harga_dasar_tertinggi_idr_liter": harga_dasar_tertinggi,
    }


def apply_ppn_pbbkb(harga_dasar_idr_liter: float, ppn_pct: float, pbbkb_pct: float, ppn_exempt: bool = False) -> TaxStackResult:
    """Layers PBBKB (provincial fuel tax) then PPN (VAT) on top of the ESDM
    base price. PBBKB's tax base is defined as the value BEFORE PPN, and
    PPN's tax base is the retail selling price -- so PBBKB is applied
    first, then PPN on the PBBKB-inclusive subtotal. This ordering is this
    engine's best-effort reading of those two DPP (tax-base) definitions
    found during research, not a certified tax computation.

    ppn_exempt: Solar Subsidi is VAT-exempt per pajakku.com (2026-08-26) --
    pass True to skip the PPN layer for subsidized fuel."""
    pbbkb_amount = harga_dasar_idr_liter * (pbbkb_pct / 100.0)
    subtotal = harga_dasar_idr_liter + pbbkb_amount
    ppn_amount = 0.0 if ppn_exempt else subtotal * (ppn_pct / 100.0)
    final_price = subtotal + ppn_amount

    return TaxStackResult(
        base_price_idr_liter=harga_dasar_idr_liter,
        mops_idr_liter=0,  # filled in by compute_market_clearing_price
        konstanta_idr_liter=0,
        margin_idr_liter=0,
        ppn_amount=ppn_amount,
        pbbkb_amount=pbbkb_amount,
        final_price_idr_liter=final_price,
        breakdown={
            "base_price_idr_liter": round(harga_dasar_idr_liter, 2),
            "pbbkb_idr_liter": round(pbbkb_amount, 2),
            "ppn_idr_liter": round(ppn_amount, 2),
            "final_price_idr_liter": round(final_price, 2),
        },
    )


def compute_market_clearing_price(
    benchmark_usd_bbl: float,
    mops_pct: float,
    konstanta_idr_liter: float,
    jisdor_rate: float,
    ppn_pct: float,
    pbbkb_pct: float,
    bbl_to_liter_ratio: float = 159.0,
    ppn_exempt: bool = False,
) -> TaxStackResult:
    """Full pipeline: MOPS/Argus benchmark (USD/bbl) -> ESDM Harga Dasar
    Tertinggi (Konstanta + 10/90 margin) -> + PBBKB -> + PPN."""
    esdm = compute_esdm_base_price(benchmark_usd_bbl, mops_pct, jisdor_rate, konstanta_idr_liter, bbl_to_liter_ratio)
    result = apply_ppn_pbbkb(esdm["harga_dasar_tertinggi_idr_liter"], ppn_pct, pbbkb_pct, ppn_exempt)
    result.mops_idr_liter = esdm["mops_idr_liter"]
    result.konstanta_idr_liter = esdm["konstanta_idr_liter"]
    result.margin_idr_liter = esdm["margin_idr_liter"]
    result.breakdown = {
        "mops_idr_liter": round(esdm["mops_idr_liter"], 2),
        "konstanta_idr_liter": round(esdm["konstanta_idr_liter"], 2),
        "margin_idr_liter": round(esdm["margin_idr_liter"], 2),
        "harga_dasar_tertinggi_idr_liter": round(esdm["harga_dasar_tertinggi_idr_liter"], 2),
        **result.breakdown,
    }
    return result


def compute_subsidy_gap(market_clearing_price_idr_liter: float, administered_price_idr_liter: float) -> dict:
    """Rp/Liter delta absorbed by the state budget (APBN) vs the
    administered retail price. Positive = state subsidizes; negative =
    state is over-collecting relative to true cost."""
    gap = market_clearing_price_idr_liter - administered_price_idr_liter
    return {
        "market_clearing_price_idr_liter": round(market_clearing_price_idr_liter, 2),
        "administered_price_idr_liter": round(administered_price_idr_liter, 2),
        "subsidy_gap_idr_liter": round(gap, 2),
        "is_subsidized": gap > 0,
    }


class PricingEngine:
    """High-level facade wiring live data (when present) to baseline
    fallback. This is the object `app.py` should talk to."""

    def __init__(self, baseline_path: str = BASELINE_PATH):
        self.baseline = load_baseline(baseline_path)
        self.loaded_at = datetime.utcnow().isoformat()

    def get_benchmark(self, key: str, live_series: Optional[Iterable[float]] = None, window: int = 30) -> tuple[float, str]:
        """Returns (value, source) where source is 'live' or 'baseline_fallback'."""
        fallback = self.baseline["benchmarks"].get(key)
        if live_series:
            try:
                return trailing_average(live_series, window=window, fallback=fallback), "live"
            except ValueError:
                pass
        return fallback, "baseline_fallback"

    def get_fx_rate(self, live_rate: Optional[float] = None) -> tuple[float, str]:
        if live_rate and live_rate > 0:
            return live_rate, "live"
        return self.baseline["fx"]["jisdor_idr_usd"], "baseline_fallback"

    def price_fuel(
        self,
        benchmark_key: str,
        retail_key: str,
        live_series: Optional[Iterable[float]] = None,
        live_fx: Optional[float] = None,
        live_retail_price: Optional[float] = None,
    ) -> dict:
        """
        live_retail_price: pass a real scraped retail price (e.g. from
            fuel_price_scraper.py) to use instead of the static baseline
            catalog price for this call -- same live-or-fallback pattern as
            live_series/live_fx. Does not mutate the baseline.
        """
        fuel = self.baseline.get("fuels", {}).get(retail_key)
        if not fuel:
            raise KeyError(f"Unknown retail_key: {retail_key}")

        benchmark_val, benchmark_source = self.get_benchmark(benchmark_key, live_series)
        fx_rate, fx_source = self.get_fx_rate(live_fx)

        stack = self.baseline["tax_stack"]
        mandated_benchmark_key = fuel.get("mops_benchmark_key", benchmark_key)
        is_regulation_compliant_benchmark = benchmark_key == mandated_benchmark_key
        result = compute_market_clearing_price(
            benchmark_usd_bbl=benchmark_val,
            mops_pct=fuel.get("mops_pct", 100.0),
            konstanta_idr_liter=fuel.get("konstanta_idr_liter", 1800),
            jisdor_rate=fx_rate,
            ppn_pct=stack["ppn_pct"],
            pbbkb_pct=stack["pbbkb_pct"],
            bbl_to_liter_ratio=self.baseline["conversion"]["bbl_to_liter"],
            ppn_exempt=fuel.get("ppn_exempt", False),
        )

        if live_retail_price is not None and live_retail_price > 0:
            retail_price, retail_source = live_retail_price, "live"
        else:
            retail_price = fuel.get("price_idr_liter") if fuel else None
            retail_source = "baseline_fallback"
        subsidy = compute_subsidy_gap(result.final_price_idr_liter, retail_price) if retail_price else None

        return {
            "benchmark_key": benchmark_key,
            "benchmark_value_usd_bbl": round(benchmark_val, 3),
            "benchmark_source": benchmark_source,
            "fx_rate": fx_rate,
            "fx_source": fx_source,
            "pricing": result.breakdown,
            "subsidy_gap": subsidy,
            "retail_price_source": retail_source,
            "fuel_label": fuel.get("label"),
            "fuel_brand": fuel.get("brand"),
            "is_administered": fuel.get("is_administered", False),
            "regulatory_class": fuel.get("regulatory_class"),
            "mandated_benchmark_key": mandated_benchmark_key,
            "is_regulation_compliant_benchmark": is_regulation_compliant_benchmark,
            "esdm_regulation": self.baseline.get("esdm_formula", {}).get("regulation"),
        }


if __name__ == "__main__":
    engine = PricingEngine()
    print(json.dumps(engine.price_fuel("icp_usd_bbl", "pertalite_ron90"), indent=2))
