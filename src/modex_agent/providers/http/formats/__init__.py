"""Protocol engines — one module per wire format (ADR-0046).

Each engine (openai_compat / openai_responses / anthropic) lowers a canonical
``LLMRequest`` onto its wire format and translates its SSE stream into
``LLMStreamEvent`` values. Engines are imported from their modules directly;
this package marker carries no re-exports.
"""
