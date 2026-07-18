# features/ — Phase 2 placeholder

This directory is reserved for **Phase 2** of quant-lab (see top-level README for the full phased roadmap).

**Status: scaffolded only.** DB tables exist (see `sql/001_init.sql`) but no code yet.

When implemented, this module will:
- Provide factor definitions (YAML/Python registration)
- Daily batch computation (polars, factor_version tagged)
- IC-weighted rolling scoring
- 5-8 starter factors: momentum, volatility, RSI, valuation, ROE, ...
