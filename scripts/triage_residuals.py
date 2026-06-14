"""Categorize the 67 residual flagged rows into Type A / B / C.

Heuristic:
- A code shared by >1 material in the seed → likely **Type B** (multi-material).
- A code with only 1 material in the seed AND UN text names exactly that
  material (singular) → **Type A** (the heading nails the material; "other"
  just distinguishes form).
- Everything else (single-material in seed but UN text is generic chemical
  "other") → **Type C** (chemical residual, manual judgment on share).

Heuristic gets it ~80% right; remaining ~20% need eyeballing.  Emits CSV
sorted by triage class, with proposed confidence calibration column.
"""

import csv
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.services.ingestion.seed_hs_mappings import _MAPPINGS

H6 = json.load(open("/sessions/keen-wonderful-lamport/mnt/outputs/.un_h6.json"))
h6_lookup = {r["id"]: r["text"] for r in H6["results"]}

# Count how many distinct materials each code is mapped to in the seed
materials_per_code: dict[str, set[str]] = defaultdict(set)
for hs, mat, *_ in _MAPPINGS:
    materials_per_code[hs].add(mat)

# Map seed-material → list of common terms that should appear in UN text
# if the heading is "about" that material.
MATERIAL_TERMS = {
    "Copper": ["copper", "cupric"],
    "Nickel": ["nickel"],
    "Cobalt": ["cobalt"],
    "Lithium": ["lithium"],
    "Manganese": ["manganese"],
    "Aluminum": ["aluminium", "aluminum"],
    "Iron Ore (LFP Grade)": ["iron", "ferro", "ferric", "ferrous"],
    "Iron Ore": ["iron", "ferro"],
    "Zinc": ["zinc"],
    "Tin": ["tin"],
    "Lead": ["lead"],
    "Magnesium": ["magnesium", "magnesia"],
    "Tungsten": ["tungsten"],
    "Molybdenum": ["molybdenum", "molybdate"],
    "Chromium": ["chromium", "chrome"],
    "Vanadium": ["vanadium"],
    "Niobium": ["niobium", "columbium"],
    "Tantalum": ["tantalum"],
    "Zirconium": ["zirconium"],
    "Titanium": ["titanium"],
    "Antimony": ["antimony"],
    "Silver": ["silver"],
    "Gold": ["gold"],
    "Platinum": ["platinum"],
    "Palladium": ["palladium"],
    "Tungsten": ["tungsten"],
    "Boron": ["boron", "borate", "boric"],
    "Sodium": ["sodium"],
    "Phosphate (Battery Grade)": ["phosph"],
    "Phosphorus": ["phosph"],
    "Selenium": ["selenium"],
    "Tellurium": ["tellurium"],
    "Indium": ["indium"],
    "Gallium": ["gallium"],
    "Germanium": ["germanium"],
    "Rare Earth Elements": ["rare-earth", "rare earth", "scandium", "yttrium", "lanthanum", "cerium", "neodymium"],
    "Natural Graphite": ["graphite", "carbon"],
    "Silicon (Anode Grade)": ["silicon", "silica"],
    "Silicon": ["silicon", "silica"],
    "Bismuth": ["bismuth"],
    "Beryllium": ["beryllium"],
    "Cesium": ["cesium", "caesium"],
    "Hafnium": ["hafnium"],
    "Rhenium": ["rhenium"],
    "Strontium": ["strontium"],
    "Barite": ["barium", "barite"],
    "Fluorspar": ["fluor"],
    "Yttrium": ["yttrium"],
    "Scandium": ["scandium"],
    "Cerium": ["cerium"],
    "Neodymium": ["neodymium"],
    "Praseodymium": ["praseodymium"],
    "Dysprosium": ["dysprosium"],
    "Terbium": ["terbium"],
}


def triage(hs: str, mat: str, un_text: str) -> tuple[str, float, str]:
    """Return (triage_class, proposed_confidence, reason)."""
    n_mats = len(materials_per_code[hs])
    terms = MATERIAL_TERMS.get(mat, [mat.lower()])
    text_lower = un_text.lower()
    mat_in_text = any(t in text_lower for t in terms)

    if n_mats > 1:
        # Multiple materials share this code in the seed → Type B
        return ("B", round(1.0 / max(n_mats, 2), 2), f"Code shared by {n_mats} materials")

    if mat_in_text:
        # Single material, named in UN text → Type A (residual is about form)
        return ("A", 1.0, "UN text names the material; residual = product form")

    # Single material, NOT named in UN text → Type C (generic chemical other)
    return ("C", 0.3, "Generic chemical residual; not named in UN text")


# Read the flagged-residual CSV
flagged = list(csv.DictReader(
    open("/sessions/keen-wonderful-lamport/mnt/outputs/hs_seed_audit_flagged.csv")
))
residuals = [r for r in flagged if r["flag"] == "RESIDUAL"]

# Apply triage and emit
out_rows = []
for r in residuals:
    cls, proposed_conf, reason = triage(r["hs_code"], r["material"], r["un_text"])
    delta = round(proposed_conf - float(r["seed_confidence"]), 2)
    out_rows.append({
        "triage": cls,
        "hs_code": r["hs_code"],
        "material": r["material"],
        "stage": r["stage"],
        "current_conf": r["seed_confidence"],
        "proposed_conf": proposed_conf,
        "delta": delta,
        "reason": reason,
        "un_text": r["un_text"][:100],
    })

# Sort: B first (worst overstatement), then C, then A
order = {"B": 0, "C": 1, "A": 2}
out_rows.sort(key=lambda r: (order[r["triage"]], r["material"], r["hs_code"]))

out_path = Path("/sessions/keen-wonderful-lamport/mnt/outputs/hs_residuals_triage.csv")
with out_path.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
    w.writeheader()
    w.writerows(out_rows)

# Summary
by_class = defaultdict(int)
for r in out_rows:
    by_class[r["triage"]] += 1

print(f"Total residual rows triaged: {len(out_rows)}")
for cls in ["A", "B", "C"]:
    print(f"  Type {cls}: {by_class[cls]}")
print(f"\nWritten to: {out_path}\n")

print("=== Type B (multi-material — biggest overstatement risk) ===")
for r in out_rows:
    if r["triage"] != "B":
        continue
    print(f"  {r['hs_code']:6} {r['material'][:22]:22}  {r['stage'][:14]:14}  "
          f"conf {r['current_conf']:>4} → {r['proposed_conf']:.2f}  ({r['reason']})")
