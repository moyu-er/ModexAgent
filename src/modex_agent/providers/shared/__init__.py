"""Provider shared types and utilities.

StreamDelta, ParsedResponse — intermediate response carriers.
classify_openai_error — structured error extraction from openai SDK exceptions.
constants — shared provider parameter keys and injection helpers
            (e.g. REASONING_EFFORT_PARAM, inject_reasoning_effort).

These are NOT exported via framework/providers/__init__.py;
import directly from framework.providers.shared when needed.
"""
