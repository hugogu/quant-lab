"""Factor library — versioned, polars-based.

Public API:
    from factors import register, get, all_factors, materialize_for_storage
    from factors.builtin import momentum_20d  # registers on import

The version string (factors.version.FACTOR_VERSION) is attached to every
factor row written to feature_value. Bump on algorithm change.
"""
from .version import FACTOR_VERSION
from .registry import (
    Factor,
    register,
    get,
    all_factors,
    list_names,
    materialize_for_storage,
)

# Importing builtin registers all 6 factors with the registry.
from . import builtin  # noqa: F401

__all__ = [
    "FACTOR_VERSION",
    "Factor",
    "register",
    "get",
    "all_factors",
    "list_names",
    "materialize_for_storage",
]