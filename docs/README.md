# Documentation index

In-depth guides for the Battery Data Intelligence Engine backend (Phase 1 ingestion focus).

| Document | What it covers |
|----------|----------------|
| [Overview](overview.md) | System layers, data flow, phases, how pieces connect |
| [Ingestion pipeline](ingestion-pipeline.md) | Adapters, `FetchBundle`, orchestration, runs, raw storage, per–source-type flow |
| [Parsing & normalization](parsing-and-normalization.md) | Parsers, resolvers, event drafts, extending safely |
| [Scoring](scoring.md) | Rule-based scores, rationale, and how they feed future `supplier_scores` |
| [Reports & AI](reports-and-ai.md) | Report builders (stubs), AI service protocols, future LLM plug-in |
| [Data model & internal API](data-model-and-api.md) | Key tables, triggers, read paths |

Start with **Overview**, then dive into **Ingestion pipeline** and **Parsing & normalization** if you are changing ingest behavior.
