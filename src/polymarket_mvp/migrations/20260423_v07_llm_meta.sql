-- v0.7 LLM observability: per-proposal model/tokens/cost/latency meta

ALTER TABLE proposals ADD COLUMN llm_meta_json TEXT;
