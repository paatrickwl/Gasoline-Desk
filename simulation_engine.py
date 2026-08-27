"""
simulation_engine.py -- Multi-Agent Swarm Simulation Wrapper (Layer 3)

Emulates a MiroFish-style approach: build a relationship graph of market
agents (refiners, traders, consumers, policymakers), inject a macro shock,
and propagate reactions through the graph over discrete steps to project
forward-looking price deviations.

This is a modular WRAPPER: the graph/agent logic is intentionally simple
and deterministic (no external LLM calls at this layer) so results are
reproducible and auditable. Swap `Agent.react()` implementations for
richer behavior models later without touching the orchestration below.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from typing import Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BASELINE_PATH = os.path.join(BASE_DIR, "data", "baseline_parameters.json")


def _load_baseline_behavior() -> dict:
    try:
        with open(BASELINE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("agent_baseline_behavior", {})
    except (FileNotFoundError, json.JSONDecodeError):
        # Hard fallback if baseline file is missing/corrupt -- keeps the
        # simulation runnable rather than crashing the dashboard.
        return {
            "refiner_margin_floor_usd_bbl": 4.5,
            "trader_risk_appetite": 0.5,
            "consumer_demand_elasticity": -0.3,
            "policymaker_subsidy_tolerance_idr_trillion": 350,
        }


@dataclass
class Agent:
    name: str
    role: str  # 'refiner' | 'trader' | 'consumer' | 'policymaker'
    state: dict = field(default_factory=dict)

    def react(self, shock_pct: float, step: int) -> float:
        """Return this agent's price-deviation contribution (in % terms)
        for the given step, given the propagated shock so far."""
        behavior = self.state.get("behavior", {})
        if self.role == "refiner":
            margin_floor = behavior.get("refiner_margin_floor_usd_bbl", 4.5)
            # Refiners pass through most of the shock but defend margin floor.
            return shock_pct * 0.8 * (1 + margin_floor / 100)
        if self.role == "trader":
            risk_appetite = behavior.get("trader_risk_appetite", 0.5)
            # Traders amplify shocks proportional to risk appetite, decaying over steps.
            return shock_pct * risk_appetite * (1.15 / (step + 1))
        if self.role == "consumer":
            elasticity = behavior.get("consumer_demand_elasticity", -0.3)
            # Consumer demand response dampens price further out.
            return shock_pct * elasticity * 0.5
        if self.role == "policymaker":
            tolerance = behavior.get("policymaker_subsidy_tolerance_idr_trillion", 350)
            # Policymakers cap upside once shocks would blow through subsidy tolerance.
            dampener = -0.25 if abs(shock_pct) * tolerance > tolerance * 0.5 else -0.05
            return shock_pct * dampener
        return 0.0


class RelationshipGraph:
    """Simple directed graph: edges carry a propagation weight (0-1)
    describing how strongly a shock at node A influences node B."""

    def __init__(self):
        self.agents: dict[str, Agent] = {}
        self.edges: dict[str, list[tuple[str, float]]] = {}

    def add_agent(self, agent: Agent) -> None:
        self.agents[agent.name] = agent
        self.edges.setdefault(agent.name, [])

    def connect(self, source: str, target: str, weight: float) -> None:
        self.edges.setdefault(source, []).append((target, weight))

    def default_market_graph(self, behavior: dict) -> "RelationshipGraph":
        for name, role in [
            ("Refiner", "refiner"),
            ("Trader", "trader"),
            ("Consumer", "consumer"),
            ("Policymaker", "policymaker"),
        ]:
            self.add_agent(Agent(name=name, role=role, state={"behavior": behavior}))
        self.connect("Refiner", "Trader", 0.7)
        self.connect("Trader", "Consumer", 0.6)
        self.connect("Consumer", "Policymaker", 0.4)
        self.connect("Policymaker", "Refiner", 0.3)
        return self


@dataclass
class ShockScenario:
    name: str
    initial_shock_pct: float  # e.g. +0.15 = +15% benchmark price shock
    steps: int = 5
    seed: Optional[int] = None


@dataclass
class SimulationResult:
    scenario_name: str
    steps: list[dict]
    final_deviation_pct: float
    summary: str


class SimulationEngine:
    """Public wrapper the dashboard calls. Wraps graph construction, shock
    injection, and step-wise propagation into one entry point."""

    def __init__(self):
        self.behavior = _load_baseline_behavior()

    def build_graph(self) -> RelationshipGraph:
        return RelationshipGraph().default_market_graph(self.behavior)

    def run(self, scenario: ShockScenario) -> SimulationResult:
        if scenario.seed is not None:
            random.seed(scenario.seed)

        graph = self.build_graph()
        current_shock = scenario.initial_shock_pct
        step_log = []

        order = ["Refiner", "Trader", "Consumer", "Policymaker"]
        for step in range(scenario.steps):
            step_contributions = {}
            aggregate = 0.0
            for agent_name in order:
                agent = graph.agents[agent_name]
                contribution = agent.react(current_shock, step)
                # small stochastic noise to emulate imperfect agent behavior
                noise = random.uniform(-0.01, 0.01)
                contribution += noise
                step_contributions[agent_name] = round(contribution, 5)
                aggregate += contribution

            current_shock = aggregate
            step_log.append({
                "step": step,
                "agent_contributions": step_contributions,
                "net_shock_pct": round(current_shock, 5),
            })

        summary = (
            f"Scenario '{scenario.name}' starting at {scenario.initial_shock_pct:+.2%} "
            f"converged to {current_shock:+.2%} projected price deviation after "
            f"{scenario.steps} propagation steps."
        )

        return SimulationResult(
            scenario_name=scenario.name,
            steps=step_log,
            final_deviation_pct=round(current_shock, 5),
            summary=summary,
        )

    def run_from_osint(self, risk_summary: dict, steps: int = 5) -> SimulationResult:
        """Bridge from `osint_scraper.summarize_risk_signals()` output into a
        shock scenario -- elevated logistics congestion becomes a positive
        price shock proxy."""
        congestion = risk_summary.get("avg_choke_point_congestion", 0.0)
        shock_pct = congestion * 0.2  # heuristic: congestion -> freight/price pressure
        scenario = ShockScenario(name="OSINT-derived logistics shock", initial_shock_pct=shock_pct, steps=steps)
        return self.run(scenario)


if __name__ == "__main__":
    engine = SimulationEngine()
    result = engine.run(ShockScenario(name="Brent +15% supply shock", initial_shock_pct=0.15, steps=5, seed=42))
    print(result.summary)
    print(json.dumps(result.steps, indent=2))
