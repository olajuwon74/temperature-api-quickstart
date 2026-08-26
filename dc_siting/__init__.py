"""Data-Centre Siting & Cooling-Cost Engine — the product logic layer.

Sits on top of the untouched `fortyguard` SDK client. Nothing in here talks to
the API directly except `data.py`; `cooling_cost.py` is pure math and has no
network dependency, so it's independently testable and reusable outside the
Streamlit app.
"""
