"""Direct-HTTP LLM provider subsystem (ADR-0046).

SSE frame parsing, HTTP error classification, tool-stream accumulation,
protocol engines, and the HTTPStreamProvider concrete class. New-system
modules only — the legacy SDK-based providers stay under ``providers/``
root and ``providers/shared/``.
"""
