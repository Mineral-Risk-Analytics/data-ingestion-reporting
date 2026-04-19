# Documentation index

> **Last updated: April 2026**

In-depth guides for the Battery Data Intelligence Engine backend.

| Document | What it covers |
|----------|----------------|
| [Overview](overview.md) | System layers, data flow, phases, how pieces connect |
| [Database architecture](database_architecture.md) | Full table reference, relationships, design decisions, non-technical summary |
| [Ingestion pipeline](ingestion-pipeline.md) | Adapters, `FetchBundle`, entity resolution, scoring orchestration, run tracking, raw storage |
| [Parsing & normalization](parsing-and-normalization.md) | Parsers, resolvers, event drafts, RiskCategory taxonomy, extending safely |
| [Scoring](scoring.md) | Five-pillar scoring formulas, decay schedules, evidence aggregation, orchestration layer |
| [Reports & AI](reports-and-ai.md) | Report templates, AI service protocols, embedding service (implemented), LLM narrative stubs |
| [Data model & internal API](data-model-and-api.md) | Key table groups, HTTP surface, CLI parity |

Start with **Overview**, then **Database architecture** for data questions and **Ingestion pipeline** for pipeline behaviour.
