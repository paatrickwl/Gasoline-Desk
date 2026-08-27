Read the updated `handoff.md` file in the root directory. This specifies our transition to an autonomous, three-layer predictive energy desk for Indonesia.

Please act as a senior Python and data architecture specialist and execute these setup steps:

1. **Modular Repository Structure:** Set up the clean project layout:
   - `app.py` (Main Streamlit multi-page dashboard)
   - `engine.py` (Core mathematical pricing calculations, 30-day rolling averages, and tax stacks)
   - `osint_scraper.py` (Script framework to ingest/parse open-source energy news and logistics indicators)
   - `simulation_engine.py` (Modular wrapper for multi-agent scenario shock testing)
   - `data/` (Directory for baseline parameters)
   - `requirements.txt` (Dependencies: streamlit, pandas, numpy, plotly, requests)

2. **Core Implementation (`engine.py` & `app.py`):**
   - Build out the institutional pricing engine with anti-hallucination trailing averages and tax layers.
   - Design a Streamlit UI containing views for the Subsidy Gap Tracker, Macro Shock Sandbox, and the new OSINT/Simulation intelligence feed.

3. **Guardrails & Fallbacks:** Ensure robust error handling so that if external feeds or mock scrapers experience latency, the dashboard falls back cleanly to cached baseline figures.

Confirm when the project skeleton and core modules are initialized.