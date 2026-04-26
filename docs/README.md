# Documentation index

> **Last updated: April 2026**

In-depth guides for the Battery Data Intelligence Engine backend.

| Document | What it covers |
|----------|----------------|
| [Overview](overview.md) | System layers, data flow, phases, how pieces connect, market vs. company scoring layers, Inngest scheduled jobs |
| [Database architecture](database_architecture.md) | Full table reference, relationships, design decisions, non-technical summary |
| [Ingestion pipeline](ingestion-pipeline.md) | Adapters, `FetchBundle`, entity resolution, `LINK_EVENTS_TO_COMPANIES` feature flag, run tracking, raw storage |
| [Parsing & normalization](parsing-and-normalization.md) | Parsers, resolvers, event drafts, RiskCategory taxonomy, extending safely |
| [Scoring](scoring.md) | Six-pillar company scoring, chemistry risk scoring, market-layer (material × geography) scoring, decay schedules, evidence aggregation |
| [Reports & AI](reports-and-ai.md) | Report templates, AI service protocols, embedding service (implemented), LLM narrative stubs, intelligence hub `InsightPost` |
| [Data model & internal API](data-model-and-api.md) | Key table groups, HTTP surface, market-score routes, Inngest endpoint, CLI parity |

Start with **Overview**, then **Database architecture** for data questions and **Ingestion pipeline** for pipeline behaviour.
