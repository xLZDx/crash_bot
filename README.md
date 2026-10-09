# Crash Bot — Analysis and Simulation Workspace

Experimental data-collection, statistical-analysis, and machine-learning code for studying crash-game outcomes. The repository includes historical bot prototypes, a virtual simulator, test scripts, and review materials.

**Status:** Research and development. The documented system state includes unvalidated predictive performance; it does not establish any reliable advantage or profitable strategy.

## Repository guide

- [SPEC_AND_CODEBASE.md](SPEC_AND_CODEBASE.md) — detailed specification, architecture, system constraints, and historical assessment.
- [ANALYSIS_FINDINGS.md](ANALYSIS_FINDINGS.md) — findings from prior analysis.
- [FIX_PLAN_V2.md](FIX_PLAN_V2.md) — proposed corrections and validation tasks.
- `bot.py`, `bot_strategy.py`, `backtest.py`, and related scripts — experimental collection, strategy, or simulation code.
- `tests/` — available test suites.
- `core/` — review and engineering evidence.
- `requirements.txt` — Python dependencies.

## Verification

Use a disposable, offline environment and review all file access and network destinations before running scripts. Compare simulated outcomes with out-of-sample statistical baselines. A cryptographically committed crash outcome must not be assumed predictable from previous outcomes.

## Safety and scope

Do not connect real accounts, spend funds, or activate any real-money functions without explicit approval. No measured positive expected value or general production readiness is claimed here. Preserve independent evaluation evidence, and report negative or inconclusive results.

## Language policy

All new GitHub documentation, code comments, commit messages, issues, and PRs must be in English.
