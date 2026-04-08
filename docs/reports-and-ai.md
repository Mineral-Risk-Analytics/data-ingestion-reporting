# Reports and AI services

Phase 1 treats **reports** and **AI** as **explicit extension points**: stable interfaces today, concrete implementations tomorrow. Nothing here blocks ingestion or the internal API.

## Reports (`app/services/reports/`)


| Module                     | Intended audience        | Phase 1 status        |
| -------------------------- | ------------------------ | --------------------- |
| `build_oem_report.py`      | OEM-facing brief         | `NotImplementedError` |
| `build_supplier_report.py` | Supplier deep-dive       | `NotImplementedError` |
| `build_investor_report.py` | Investor / macro + names | `NotImplementedError` |


### Data model hooks

Persistence is already modeled for a reports workflow:

- `**report_runs`** — `report_type`, `audience_type`, `as_of_date`, `status`, `parameters_json`, timestamps.
- `**report_insights**` — ordered blocks (`insight_type`, `title`, `body`, optional FKs to supplier/material/regulation, `sort_order`).

Planned flow:

1. Create `**report_runs**` row (`queued` → `running` → `completed` / `failed`).
2. Query `**supplier_scores**`, `**risk_events**`, `**regulations**`, `**trade_flows**` (and analyst overrides).
3. Generate `**report_insights**` rows.
4. Optional: render PDF/HTML from insights (outside Phase 1 scope).

## AI services (`app/services/ai/`)

These modules use `**typing.Protocol**` so you can drop in OpenAI, Anthropic, or an internal model server without changing call sites.

### `summarize_document.py`

- `**DocumentSummarizer**` protocol: `summarize(text, metadata=…) -> str`
- `**StubDocumentSummarizer**` — extractive first sentences (no network).

Future: `**OpenAiSummarizer**` implementing the same protocol with env-based API key.

### `classify_event.py`

- `**EventClassifier**` protocol: `classify(title, summary) -> list[str]` (category tags).
- `**StubEventClassifier**` — keyword rules mapped to `**RiskCategory**` (`app/constants.py`).

Future: constrained JSON schema from an LLM; validate against `**RiskCategory**` or an expanded taxonomy.

### `cluster_duplicates.py`

- `**DuplicateClusterer**` protocol: `cluster_key(title, url) -> str`
- `**StubDuplicateClusterer**` — URL-first; else normalized title token signature.

Future: embedding + clustering (HDBSCAN, online dedupe service).

### Integration ideas (not wired)

- After ingest, async tasks could `**summarize_document**` on `source_documents.raw_text` and store snippets in `**report_insights**` or `**metadata_json**`.
- `**classify_event**` could refine `**risk_events.risk_categories_json**` in a post-processing pass.
- `**cluster_duplicates**` could populate `**entity_links**` or collapse duplicate `**risk_events**`.

## Related reading

- [Overview](overview.md)
- [Data model & internal API](data-model-and-api.md) — `report_runs`, `report_insights`
- [Scoring](scoring.md) — inputs to narrative insight generation

