"""Factor version — bump on algorithm changes.

When you change the math of any factor, bump FACTOR_VERSION (e.g. v1.0 → v1.1).
Old feature_value rows stay queryable by their factor_version; new rows get the
new version. This is the reproducibility contract from the Kimi design.
"""
FACTOR_VERSION = "v1.0"