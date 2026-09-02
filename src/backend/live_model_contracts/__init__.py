"""Local acceptance harness for real PersonalDM model stacks.

This package is deliberately outside ``tests``: pytest/CI fixtures must never patch the
Planner, Validator, Scribe or other model-facing roles while these contracts are running.
"""

__all__ = []
