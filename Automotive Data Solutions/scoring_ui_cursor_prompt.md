# Cursor Prompt: Admin Scoring UI Views
## Market × Geography Scores + Battery Chemistry Detail

### Context

The backend already exposes two scoring layers:

- **Market layer** — material × geography risk surface; company-agnostic.  
  Routes: `GET /api/v1/market/scores`, `GET /api/v1/materials/{id}/market-scores`, `POST /api/v1/market/rescore`
- **Chemistry layer** — per-chemistry composite risk score built from material
  composition weights.  
  Routes: `GET /api/v1/chemistries/{id}`, `GET /api/v1/chemistries/{id}/risk/history`,
  `POST /api/v1/chemistries/{id}/rescore`

Neither scoring layer has a dedicated admin view today. This prompt builds both.

---

### Important: frontend ↔ backend type alignment

The hand-authored `BatteryChemistryRead` in
`lib/types/reference-data.ts` uses `display_name`, but the backend
`BatteryChemistryRead` schema actually returns `name`. Fix the mismatch in
**this PR** — the detail page you're creating must use the real field names.

Backend `GET /api/v1/chemistries` returns per item:
```
id, slug, name (not display_name), description, status, current_market_share_pct,
market_share_as_of_date, is_active, verified, created_at, updated_at,
latest_risk_score: { id, as_of_date, methodology_version,
  material_concentration_score, geopolitical_score, composite_risk_score,
  score_confidence, computed_at, metadata_json } | null
```

The frontend type `BatteryChemistryRead` has `display_name`, `category`,
`latest_risk_score: number | null`, and `latest_risk_band`. These do **not** match
the backend. Fix `lib/types/reference-data.ts` `BatteryChemistryRead` to mirror
the backend exactly before building the detail page. Update the chemistries list
page (`app/(dashboard)/data/chemistries/page.tsx`) to use `name` instead of
`display_name` and to render `latest_risk_score?.composite_risk_score` and
`latest_risk_score?.score_confidence` for the score column.

---

### Task 1 — Market × Geography Scores view

#### 1a. Add types to `lib/types/reference-data.ts`

Add after the existing `BatteryChemistryRead` block:

```typescript
// ---------------------------------------------------------------------------
// Market scores (material × geography)
// ---------------------------------------------------------------------------

export interface MaterialGeographyScoreRead {
  id: number;
  material_id: number;
  geography_code: string;
  as_of_date: string;           // ISO date
  material_concentration_score: number | null;
  geopolitical_trade_score: number | null;
  regulatory_compliance_score: number | null;
  operational_score: number | null;
  financial_pressure_score: number | null;
  overall_risk_score: number | null;
  event_count: number;
  scoring_version: string;
  created_at: string;           // ISO datetime
}

export interface RescoredResult {
  scored: number;
  as_of_date: string;
  run_id: string;
}

export type MarketScoresResponse = PaginatedResponse<MaterialGeographyScoreRead>;
```

Also add these to the exports in `lib/types/index.ts`:
```typescript
export type {
  // ... existing exports ...
  MaterialGeographyScoreRead,
  RescoredResult,
  MarketScoresResponse,
} from "./reference-data";
```

#### 1b. Create `lib/api/market-scores.ts`

```typescript
import type { ApiClient } from "./client";
import type { MarketScoresResponse, RescoredResult } from "@/lib/types";

export interface MarketScoresParams {
  page?: number;
  limit?: number;
  material_id?: number;
  geography_code?: string;
  min_overall?: number;
}

export async function getMarketScores(
  client: ApiClient,
  params: MarketScoresParams = {},
): Promise<MarketScoresResponse> {
  const { data } = await client.get<MarketScoresResponse>(
    "/api/v1/market/scores",
    { params },
  );
  return data;
}

export async function rescoreMarket(
  client: ApiClient,
): Promise<RescoredResult> {
  const { data } = await client.post<RescoredResult>("/api/v1/market/rescore");
  return data;
}
```

#### 1c. Create `lib/hooks/use-market-scores.ts`

Follow the exact same pattern as `lib/hooks/use-chemistries.ts`:

```typescript
"use client";

import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from "@tanstack/react-query";
import { useApiClient } from "./use-api-client";
import {
  getMarketScores,
  rescoreMarket,
  type MarketScoresParams,
} from "@/lib/api/market-scores";
import type { MarketScoresResponse } from "@/lib/types";

export const marketScoreQueryKeys = {
  all: ["market-scores"] as const,
  lists: () => [...marketScoreQueryKeys.all, "list"] as const,
  list: (params: MarketScoresParams) =>
    [...marketScoreQueryKeys.lists(), params] as const,
};

export function useMarketScores(
  params: MarketScoresParams,
  options?: Omit<UseQueryOptions<MarketScoresResponse>, "queryKey" | "queryFn">,
) {
  const client = useApiClient();
  return useQuery<MarketScoresResponse>({
    queryKey: marketScoreQueryKeys.list(params),
    queryFn: () => getMarketScores(client, params),
    placeholderData: (prev) => prev,
    ...options,
  });
}

export function useRescoreMarket() {
  const client = useApiClient();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => rescoreMarket(client),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: marketScoreQueryKeys.all });
    },
  });
}
```

#### 1d. Create `app/(dashboard)/data/market-scores/page.tsx`

Page requirements:
- Title: "Market Risk Scores" / subtitle: "Latest material × geography risk surface. One row per active (material, geography) pair."
- Filter bar (no full-text search; use dropdowns/inputs):
  - **Material** — number input or a select populated from `GET /materials` (use a simple text input for `material_id` as integer; a full select can be a follow-up)
  - **Geography** — text input for ISO-2 code (uppercase transform on blur/submit)
  - **Min Overall Score** — number input (0–100)
  - **Clear** button that resets all three
- DataTable columns:
  - `Material ID` — `material_id` (right-aligned, monospace)
  - `Geography` — `geography_code` (uppercase Badge, outline variant)
  - `Overall` — `overall_risk_score`; render with `<ConfidenceBadge value={...} />` (same component used in the chemistries list)
  - `Concentration` — `material_concentration_score`; right-aligned number formatted to 1 decimal, `—` if null
  - `Geopolitical` — `geopolitical_trade_score`; same format
  - `Regulatory` — `regulatory_compliance_score`; same format
  - `Operational` — `operational_score`; same format
  - `Financial` — `financial_pressure_score`; same format
  - `Events` — `event_count`; right-aligned integer badge (use `<Badge variant="secondary">`)
  - `As of` — `as_of_date`; formatted as `MMM d, yyyy`
- Pagination using `<DataTablePagination>` (default limit 50)
- **Rescore button** top-right of toolbar: "Rescore all" — calls `useRescoreMarket()`, shows a spinner while mutating, shows a toast/brief inline message on success (e.g. "Scored N pairs"). Use `useRescoreMarket` from the hook above.
- State: `page`, `limit`, `materialId` (number | undefined), `geographyCode` (string), `minOverall` (number | undefined). Debounce geography and minOverall inputs 300ms before triggering the query.

Use the same structural pattern as the chemistries list page (same import set, same `mx-auto flex max-w-7xl flex-col gap-4` layout wrapper).

#### 1e. Add the route to the sidebar nav

Find the nav config (likely in `components/layout/sidebar.tsx` or a `nav.ts` constant) and add:

```
{ label: "Market Scores", href: "/data/market-scores", icon: <BarChart3 /> }
```

Place it after "Chemistries" in the Data section.

---

### Task 2 — Battery Chemistry detail page

#### 2a. Update `lib/types/reference-data.ts` — fix `BatteryChemistryRead` and add detail types

Replace the existing `BatteryChemistryRead` interface with the backend-accurate version:

```typescript
export interface ChemistryRiskScoreRead {
  id: number;
  as_of_date: string;           // ISO date
  methodology_version: string;
  material_concentration_score: number | null;
  geopolitical_score: number | null;
  composite_risk_score: number | null;
  score_confidence: number | null;
  computed_at: string;          // ISO datetime
  metadata_json: Record<string, unknown> | null;
}

export interface BatteryChemistryRead {
  id: number;
  slug: string;
  name: string;                 // NOTE: backend uses `name`, not `display_name`
  description: string | null;
  status: string;
  current_market_share_pct: number | null;
  market_share_as_of_date: string | null;
  is_active: boolean;
  verified: boolean;
  created_at: string;
  updated_at: string;
  latest_risk_score: ChemistryRiskScoreRead | null;
}

export interface ChemistryMaterialRead {
  id: number;
  material_id: number;
  material_canonical_name: string;
  role: string;
  intensity: number;            // 0..1 (fraction of chemistry mass)
  is_substitutable: boolean;
  valid_from: string;           // ISO date
  valid_to: string | null;
  notes: string | null;
}

export interface ChemistryDetailRead extends BatteryChemistryRead {
  active_materials: ChemistryMaterialRead[];
}
```

Also add `ChemistryRiskScoreRead`, `ChemistryMaterialRead`, `ChemistryDetailRead` to the exports in `lib/types/index.ts`.

#### 2b. Update `lib/api/chemistries.ts`

Add three new functions after `getChemistries`:

```typescript
import type { ChemistryDetailRead, ChemistryRiskScoreRead, RescoredResult } from "@/lib/types";

export async function getChemistryDetail(
  client: ApiClient,
  id: number,
): Promise<ChemistryDetailRead> {
  const { data } = await client.get<ChemistryDetailRead>(`/api/v1/chemistries/${id}`);
  return data;
}

export async function getChemistryRiskHistory(
  client: ApiClient,
  id: number,
  limit = 24,
): Promise<ChemistryRiskScoreRead[]> {
  const { data } = await client.get<ChemistryRiskScoreRead[]>(
    `/api/v1/chemistries/${id}/risk/history`,
    { params: { limit } },
  );
  return data ?? [];
}

export async function rescoreChemistry(
  client: ApiClient,
  id: number,
): Promise<ChemistryRiskScoreRead> {
  const { data } = await client.post<ChemistryRiskScoreRead>(
    `/api/v1/chemistries/${id}/rescore`,
  );
  return data;
}
```

#### 2c. Create `lib/hooks/use-chemistry-detail.ts`

```typescript
"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useApiClient } from "./use-api-client";
import {
  getChemistryDetail,
  getChemistryRiskHistory,
  rescoreChemistry,
} from "@/lib/api/chemistries";
import { chemistryQueryKeys } from "./use-chemistries";

export const chemistryDetailQueryKeys = {
  detail: (id: number) => [...chemistryQueryKeys.all, "detail", id] as const,
  history: (id: number) => [...chemistryQueryKeys.all, "history", id] as const,
};

export function useChemistryDetail(id: number) {
  const client = useApiClient();
  return useQuery({
    queryKey: chemistryDetailQueryKeys.detail(id),
    queryFn: () => getChemistryDetail(client, id),
  });
}

export function useChemistryRiskHistory(id: number) {
  const client = useApiClient();
  return useQuery({
    queryKey: chemistryDetailQueryKeys.history(id),
    queryFn: () => getChemistryRiskHistory(client, id, 24),
  });
}

export function useRescoreChemistry(id: number) {
  const client = useApiClient();
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => rescoreChemistry(client, id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: chemistryDetailQueryKeys.detail(id) });
      queryClient.invalidateQueries({ queryKey: chemistryDetailQueryKeys.history(id) });
      queryClient.invalidateQueries({ queryKey: chemistryQueryKeys.lists() });
    },
  });
}
```

#### 2d. Create `app/(dashboard)/data/chemistries/[id]/page.tsx`

This is a `"use client"` page. Route params: `{ id: string }` — parse to int with `Number(params.id)`.

**Layout structure:**

```
┌──────────────────────────────────────────────────────────┐
│  ← Back to Chemistries   [Rescore button]                │
│  Chemistry name (slug badge)                             │
│  status chip | verified chip | market share %            │
├─────────────────────┬────────────────────────────────────┤
│  Pillar Scores      │  Risk Score History                │
│  (score card grid)  │  (table, last 24 runs)             │
├─────────────────────┴────────────────────────────────────┤
│  Active Composition (DataTable)                          │
└──────────────────────────────────────────────────────────┘
```

**Back link:**
```tsx
import Link from "next/link";
<Link href="/data/chemistries" className="text-sm text-muted-foreground hover:text-foreground flex items-center gap-1">
  <ChevronLeft className="h-4 w-4" /> Chemistries
</Link>
```

**Header section:**
- Large `name` (h1 font-semibold) with `slug` displayed as `<Badge variant="outline" className="font-mono text-xs">` inline
- Row of chips: `status` (Badge outline), `is_active` ("Active" green / "Inactive" muted), `verified` (show only if true — a blue "Verified" badge)
- `current_market_share_pct` — if not null, show "Market share: {value}%" in small muted text

**Pillar score cards (left column):**
Display each of the two pillar scores from `latest_risk_score` as a small card:
- `material_concentration_score` — label "Material Concentration"
- `geopolitical_score` — label "Geopolitical"
- `composite_risk_score` — label "Composite (overall)", rendered larger / bolder
- `score_confidence` — label "Score Confidence", rendered as 0–100% (multiply by 100 if it's 0..1)

For each score card:
```tsx
<div className="rounded-lg border bg-card p-4">
  <p className="text-xs text-muted-foreground">{label}</p>
  <p className="mt-1 text-2xl font-semibold tabular-nums">
    {value != null ? value.toFixed(1) : "—"}
  </p>
</div>
```

Arrange as a 2×2 grid (`grid grid-cols-2 gap-3`).

**Risk score history (right column):**
Use `useChemistryRiskHistory(id)`. Render as a simple table (not a full DataTable — just an HTML table with Tailwind classes):
- Columns: `As of`, `Composite`, `Concentration`, `Geopolitical`, `Confidence`
- Cap at 24 rows (already server-limited)
- `as_of_date` formatted as `MMM d, yyyy`
- Numbers to 1 decimal place, `—` for null

**Active composition table (full width):**
Use `data.active_materials` from `useChemistryDetail`. DataTable with columns:
- `Material` — `material_canonical_name`
- `Role` — `role` (Badge outline, humanize: replace `_` with space, title-case)
- `Intensity` — `intensity` rendered as percentage (e.g. `0.42` → `42%`)
- `Substitutable` — `is_substitutable` → "Yes" (green) / "No" (muted) badge
- `Valid from` — `valid_from`
- `Valid to` — `valid_to ?? "—"`
- `Notes` — `notes`, `line-clamp-2 text-xs text-muted-foreground`

Empty state: "No active materials linked to this chemistry."

**Rescore button (top-right):**
```tsx
const rescore = useRescoreChemistry(id);
<Button
  variant="outline"
  size="sm"
  onClick={() => rescore.mutate()}
  disabled={rescore.isPending}
>
  {rescore.isPending ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <RefreshCw className="mr-2 h-4 w-4" />}
  Rescore
</Button>
```

On `rescore.isSuccess`, show a small inline success note:  
`<span className="text-xs text-emerald-600">Rescored {format(new Date(rescore.data.as_of_date), 'MMM d')}</span>`

#### 2e. Update the chemistries list page to link to detail

In `app/(dashboard)/data/chemistries/page.tsx`:

1. Fix `display_name` → `name` throughout
2. Fix score column: `row.original.latest_risk_score?.composite_risk_score` (not a direct number) and `row.original.latest_risk_score?.score_confidence`
3. Wrap the name cell in a `<Link>`:
```tsx
import Link from "next/link";
// in the display_name column cell:
cell: ({ row }) => (
  <Link href={`/data/chemistries/${row.original.id}`} className="hover:underline">
    <div className="flex flex-col">
      <span className="font-medium">{row.original.name}</span>
      <span className="font-mono text-xs text-muted-foreground">{row.original.slug}</span>
    </div>
  </Link>
),
```
4. The `latest_risk_band` column — this field no longer exists in the corrected types. Replace it with a computed band derived from `composite_risk_score`:
```typescript
// At module level:
function scoreToBand(score: number | null | undefined): string | null {
  if (score == null) return null;
  if (score >= 75) return "CRIT";
  if (score >= 55) return "HIGH";
  if (score >= 35) return "MOD";
  return "LOW";
}
```
Use `scoreToBand(row.original.latest_risk_score?.composite_risk_score)` in the risk band column cell.

5. For the score column, show `composite_risk_score` via `<ConfidenceBadge value={row.original.latest_risk_score?.composite_risk_score ?? null} />`.

---

### File checklist

```
lib/types/reference-data.ts          — add/fix types
lib/types/index.ts                   — add new type exports
lib/api/market-scores.ts             — NEW
lib/api/chemistries.ts               — add 3 functions
lib/hooks/use-market-scores.ts       — NEW
lib/hooks/use-chemistry-detail.ts    — NEW
app/(dashboard)/data/market-scores/page.tsx        — NEW
app/(dashboard)/data/chemistries/[id]/page.tsx     — NEW
app/(dashboard)/data/chemistries/page.tsx          — fix display_name→name, add links, fix score column
components/layout/sidebar.tsx (or nav config)      — add Market Scores nav entry
```

---

### Patterns to follow

- All pages are `"use client"` components; no server-side data fetching.
- Hooks import from `@/lib/api/...` and use `useApiClient()` for the client.
- Query keys always have a top-level namespace string (e.g. `"market-scores"`).
- Use `<DataTable>`, `<DataTablePagination>`, `<DataTableToolbar>` from `@/components/data-table/`.
- Use `<ConfidenceBadge>` from `@/components/shared/confidence-badge` for 0–100 score rendering.
- Use `<Badge variant="outline">` for text classification chips.
- Humanize enum strings with the existing `humanize()` util from `@/lib/utils/format`.
- Date formatting: use `date-fns` `format(new Date(isoString), 'MMM d, yyyy')`.

---

### What NOT to do

- Do not add a backend route — everything you need is already exposed.
- Do not use `any` types — use the interfaces defined above.
- Do not duplicate query key strings — reuse `chemistryQueryKeys` from `use-chemistries.ts` as the base namespace for detail keys.
- Do not skip the `display_name` → `name` fix — leaving it broken means the list page renders blank names.
