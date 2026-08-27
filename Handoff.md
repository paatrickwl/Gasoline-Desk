# Project Handoff: Autonomous Indonesia Energy Intelligence & Swarm Prediction Desk

## 1. Project Overview
A modular, three-layer Python application (Streamlit front-end + Python computational core) built to transition from static fuel pricing to a dynamic, predictive intelligence desk for Indonesia. It pairs institutional energy pricing math with automated OSINT data collection and multi-agent swarm simulation to map out price volatility and state subsidy impacts.

---

## 2. The Three-Layer Architecture
* **Layer 1: Claude Integration (Data Gathering & Logic Engine)**
  * Parses structured and unstructured text inputs (ESDM policy drafts, market reports, currency shifts) and manages user interactions via natural language summaries.
* **Layer 2: OSINT Signal Parser (`osint_scraper.py`)**
  * Automatically pulls and monitors open-source headlines, shipping/logistics alerts (e.g., Malacca/Hormuz choke point updates), and macroeconomic indicators.
* **Layer 3: Multi-Agent Swarm Simulation Wrapper (`simulation_engine.py`)**
  * Emulates the MiroFish framework approach: takes incoming macro shocks, constructs a relationship graph, and simulates how interacting market agents (refiners, traders, consumers, policymakers) react to project forward-looking price deviations.

---

## 3. Indonesia-Specific Pricing Core (`engine.py`)
Calculates True Market-Clearing Prices using institutional trading mechanics:
* **Pricing Baseline:** Trailing 30-day moving average of Brent/ICP and MOPS benchmarks.
* **The FX Multiplier:** Bank Indonesia JISDOR middle rate conversion, factoring in IDR depreciation against the USD.
* **The Tax & Logistics Stack:** Includes 11% PPN, regional PBBKB motor vehicle fuel tax (~7.5%), and archipelagic cost-to-serve freight adjustments.
* **Subsidy Gap Output:** Computes the exact Rp/Liter delta absorbed by the state budget (APBN) versus administered retail prices (Pertalite / Pertamax).

---

## 4. Repository Structure (`requirements.txt` & modules)
- `app.py` -> Streamlit multi-page interface (Dashboard, Scenario Sandbox, OSINT Feed, Swarm Simulation).
- `engine.py` -> Core mathematical pricing formulas and tax stacks.
- `osint_scraper.py` -> Automated headline and shipping data ingestion tool.
- `simulation_engine.py` -> Multi-agent scenario testing module.
- `data/` -> Static baseline parameters (import ratios, storage capacities, tax constants).