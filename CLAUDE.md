# Polymarket MVP — Claude Code Instructions

## Auto-Resume Protocol

When starting a new session, **immediately** check for the file `.claude/resume_task.md`.

- If it exists and contains uncompleted work (items not marked `[x]`), **announce to the user that you found unfinished work and will resume it**, then continue executing from the first uncompleted step. Do NOT ask for permission — just inform and proceed.
- If all items are completed or the file is empty/missing, proceed normally with whatever the user asks.

### Checkpoint rules (CRITICAL — follow on every multi-step task)

Whenever you are working on a task with more than 2 steps:

1. **Before starting work**, write a checkpoint to `.claude/resume_task.md` with this format:

```markdown
# Resume Task

## Original Request
> (paste the user's original request or a faithful summary)

## Context
- Branch: (current git branch)
- Key files: (list the main files involved)
- Any important decisions or constraints

## Steps
- [ ] Step 1 description
- [ ] Step 2 description
- [ ] Step 3 description
...
```

2. **After completing each step**, update the file: change `- [ ]` to `- [x]` for that step and add a one-line note of what was done if useful.

3. **When ALL steps are done**, replace the file contents with just:
```
(empty — all work completed)
```

4. **If the plan changes** (new steps discovered, steps removed, reordering), update the file immediately to reflect the current plan.

### Why this matters
The usage limit can cut a session at any time without warning. The checkpoint file is the only way to ensure continuity. Keep it updated aggressively — better to write too often than to lose progress context.

## Alpha Lab Integration Contract

`polymarket-alpha-lab` is a separate repository at `~/Desktop/polymarket-alpha-lab`. It is the research and pricing engine for soccer markets. This repo (`polymarket-mvp`) is the execution system.

### Boundary rules
- **No Python imports** from `polymarket_alpha_lab` in this repo. Ever.
- **No Alpha Lab dependencies** in this repo's `pyproject.toml`.
- **No Alpha Lab tests** in this repo's `tests/` directory (except `test_alpha_signal_importer.py` which tests the MVP-side importer).
- **No research logic** (pricing models, feature engineering, de-vig) in this repo.

### Shared database
Both repos read/write the same SQLite file (`var/polymarket_mvp.sqlite3` or `POLYMARKET_MVP_DB_PATH`).

**Table ownership:**
- **MVP owns**: `market_snapshots`, `market_contexts`, `event_clusters`, `market_event_links`, `research_memos`, `proposals`, `approvals`, `executions`, `positions`, `order_reconciliations`, `kill_switches`, `strategy_authorizations`, `shadow_executions`, `position_events`, `exit_recommendations`, `agent_reviews`, `autopilot_heartbeats`, `market_resolutions`, `proposal_contexts`
- **Alpha Lab owns**: `sports_teams`, `sports_fixtures`, `sports_market_mappings`, `bookmaker_quotes`, `feature_snapshots`, `fair_value_snapshots`, `alpha_signals`, `clv_observations`, `model_runs`, `strategy_versions`, `provider_ingest_runs`, `provider_event_links`, `market_state_history`, `training_examples`, `evaluation_runs`, `polymarket_orderbook_snapshots`
- Each repo's `init_db()` only creates its own tables (`CREATE TABLE IF NOT EXISTS`). Never drop or alter the other repo's tables.

### Signal handoff (the only integration point)
```
alpha_signals (status='ready_for_import') -> import-alpha-signals CLI -> proposals
```
- Alpha Lab publishes signals with `status='ready_for_import'`
- MVP runs `import-alpha-signals` to convert them to proposals with `decision_engine='alpha_lab'`
- Proposals then flow through normal `risk -> authorize/approve -> execute` pipeline
- Importer marks signals as `imported` only after proposal persistence succeeds

### alpha_signals contract fields (consumed by MVP importer)
`signal_id`, `market_id`, `outcome`, `strategy_name`, `market_family`, `fair_probability`, `market_probability`, `gross_edge_bps`, `net_edge_bps`, `recommended_size_usdc`, `max_entry_price`, `model_version`, `mapping_confidence`, `feature_freshness_seconds`, `confidence_score`, `signal_expires_at`, `status`, `explanation_json`, `source_summary_json`, `quality_flags_json`

### market_snapshots contract fields (consumed by Alpha Lab)
`market_id`, `question`, `slug`, `active`, `accepting_orders`, `end_date`, `liquidity_usdc`, `volume_24h_usdc`, `outcomes_json`, `market_json`

Changes to these fields require coordination with the Alpha Lab repo.

### Key files
- `src/polymarket_mvp/alpha_signal_importer.py` — the importer
- `src/polymarket_mvp/migrations/20260408_v06_alpha_lab_import.sql` — schema migration
- `tests/test_alpha_signal_importer.py` — importer tests
