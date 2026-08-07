"""Agent evaluation harness — dataset curation, experiment runner, evaluators.

Layer 2 of the eval architecture (ADR-0024). Runs as a separate process
(opt-in via ``[eval]`` extra) to avoid OTel tracer-provider conflicts with
the bot's JSON-OTLP trace path.

Usage::

    python -m bot.eval.cli curate --dataset react-baseline --max 50
    python -m bot.eval.cli run --dataset react-baseline --experiment v1
"""
