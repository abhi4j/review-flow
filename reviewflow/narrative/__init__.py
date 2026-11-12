"""LLM narrative over review-flow metrics, grounded in a verified fact table.

The model never writes data values. It writes references ({{fact_id}}, {{@login}},
{{#pr}}) that the server validates against the insight document and renders itself, so
every number a reader sees was computed by `metrics.py` and traces to a JSON pointer.
"""
