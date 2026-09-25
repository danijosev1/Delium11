"""Local Streamlit front end for Delium (localhost, single user).

The UI is a thin presentation layer: it calls the SAME internal functions the
CLI calls (`delium.ingestion.*`, `delium.validation.run_validation`,
`delium.discovery.run_discovery`, `delium.ingestion.discover_cross_market`) and
never re-implements any engine, scoring, or verdict logic. This package holds
the non-UI helpers (credentials, cost estimates, pure formatters, and thin
service wrappers); `app.py` is the Streamlit script that renders them.
"""
