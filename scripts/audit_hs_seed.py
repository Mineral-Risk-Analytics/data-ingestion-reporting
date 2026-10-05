"""Audit seed_hs_mappings._MAPPINGS against UN Comtrade HS2022 / HS2017.

For every (hs_code, material, description, confidence, stage, digit_count,
market_scope) row in the seed file:

  * Look the code up in HS2022 (H6) — the global standard.
  * Fall back to HS2017 (H5) if not found.
  * Classify the official UN description as either:
      SPECIFIC  — the heading text identifies one product / chemical
      RESIDUAL  — heading text is "other / n.e.c. / n.e.s." (catch-all bucket)
      INVALID   — the code does not exist in either revision
  * Flag rows where confidence > 0.4 but the code is RESIDUAL — those are
    overstating precision (the "283329 → Iron Ore" problem Nicole caught).

Outputs:
  outputs/hs_seed_audit_full.csv         — every row + UN text + flag
  outputs/hs_seed_audit_flagged.csv      — just the rows needing attention
"""

import csv
import json
import re
import sys
from pathlib import Path

# Allow imports when run from repo root
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.ingestion.seed_hs_mappings import _MAPPINGS

H6_PATH = Path("/sessions/keen-wonderful-lamport/mnt/outputs/.un_h6.json")
H5_PATH = Path("/sessions/keen-wonderful-lamport/mnt/outputs/.un_h5.json")
H6 = json.load(open(H6_PATH)) if H6_PATH.exists() else {"results": []}
H5 = json.load(open(H5_PATH)) if H5_PATH.exists() else {"results": []}

h6_lookup = {r["id"]: r["text"] for r in H6["results"]}
h5_lookup = {r["id"]: r["text"] for r in H5["results"]}


RESIDUAL_PATTERNS = re.compile(
    r"\b(n\.?e\.?c\.?|n\.?e\.?s\.?|other|not elsewhere)\b",
    re.IGNORECASE,
)


def classify(code: str) -> tuple[str, str, str]:
    """Return (flag, source_revision, un_text)."""
    txt = h6_lookup.get(code)
    rev = "HS2022"
    if not txt:
        txt = h5_lookup.get(code)
        rev = "HS2017"
    if not txt:
        return "INVALID", "—", "<not found in HS2022 or HS2017>"
    if RESIDUAL_PATTERNS.search(txt):
        return "RESIDUAL", rev, txt
    return "SPECIFIC", rev, txt


rows = []
for hs, mat, desc, conf, stage, digit, scope in _MAPPINGS:
    flag, rev, un_text = classify(hs)
    needs_attention = (
        flag == "INVALID"
        or (flag == "RESIDUAL" and conf > 0.4)
    )
    rows.append({
        "hs_code": hs,
        "digit_count": digit,
        "material": mat,
        "stage": stage,
        "market_scope": scope,
        "seed_confidence": conf,
        "seed_description": desc,
        "un_revision": rev,
        "un_text": un_text,
        "flag": flag,
        "needs_attention": needs_attention,
    })


def write_csv(path: Path, data: list[dict]) -> None:
    if not data:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(data[0].keys()))
        w.writeheader()
        w.writerows(data)


out_dir = Path("/sessions/keen-wonderful-lamport/mnt/outputs")
write_csv(out_dir / "hs_seed_audit_full.csv", rows)
write_csv(
    out_dir / "hs_seed_audit_flagged.csv",
    [r for r in rows if r["needs_attention"]],
)

# Summary stats
total = len(rows)
invalid = sum(1 for r in rows if r["flag"] == "INVALID")
residual_all = sum(1 for r in rows if r["flag"] == "RESIDUAL")
residual_hi = sum(
    1 for r in rows if r["flag"] == "RESIDUAL" and r["seed_confidence"] > 0.4
)
specific = sum(1 for r in rows if r["flag"] == "SPECIFIC")
hs2017_only = sum(1 for r in rows if r["un_revision"] == "HS2017")

print(f"Total seed rows audited     : {total}")
print(f"  SPECIFIC (good)           : {specific}  ({specific*100//total}%)")
print(f"  RESIDUAL ('other / nes')  : {residual_all}")
print(f"    └ with conf > 0.4 (FLAG): {residual_hi}")
print(f"  INVALID (code not in H6/H5): {invalid}")
print(f"  Codes only in HS2017      : {hs2017_only}  (verify still in use)")
print(f"")
print(f"Total NEEDS ATTENTION       : {invalid + residual_hi}")
