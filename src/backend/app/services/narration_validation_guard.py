"""Temporary test compatibility seam for pre-pipeline narrator fixtures.

This module does not install a guard and never mutates LLMProvider. Older acceptance
fixtures patch ``_ORIGINAL_GENERATE_STREAM`` directly; the explicit narration pipeline
reads that override only when it differs from the untouched default method.
"""

from app.providers.llm_provider import LLMProvider

_DEFAULT_GENERATE_STREAM = LLMProvider.generate_stream
_ORIGINAL_GENERATE_STREAM = _DEFAULT_GENERATE_STREAM

__all__ = ["_DEFAULT_GENERATE_STREAM", "_ORIGINAL_GENERATE_STREAM"]
