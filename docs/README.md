# Documentation index

> **Last updated: April 2026**

In-depth guides for the Battery Data Intelligence Engine backend.

| Document | What it covers |
|----------|----------------|
| [Overview](overview.md) | System layers, data flow, phases, how pieces connect, market vs. company scoring layers, Inngest scheduled jobs |
| [Database architecture](database_architecture.md) | Full table reference, relationships, design decisions, non-technical summary |
| [Data sources](data-sources.md) | Canonical map: source → tables/fields → scoring inputs → pillars → scoring layers |
| [Scoring](scoring.md) | Canonical formulas + weights across: market pair → material rollup → chemistry rollup → company scoring, plus explicit scoring gaps |
| [Operations](operations.md) | Runbooks: ingestion triggers, parsing/normalization, seed review, CLI/API parity, Inngest local dev |
| [Reports & AI](reports-and-ai.md) | Report templates, AI service protocols, embedding service (implemented), LLM narrative stubs, intelligence hub `InsightPost` |

Start with **Overview**, then **Database architecture** for data questions, then **Data sources** + **Scoring** for “what feeds what” and “how it computes”. Use **Operations** when you need to run or debug the system.


