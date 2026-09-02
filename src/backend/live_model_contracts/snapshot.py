"""Compatibility import for the schema-aware local truth snapshot oracle."""

from live_model_contracts.oracle_snapshot import TruthSnapshot, capture, semantic_diff

__all__ = ["TruthSnapshot", "capture", "semantic_diff"]
