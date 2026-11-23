"""Trajectory-aware USGS vs WMD crosscheck — classifier + HTML render + DB driver.

Used by the `wmd-crosscheck` CLI. WMD's multi-year series (wmd_production)
is the VINTAGE CONTROL for the ~1-year-newer USGS estimate: a level gap that
sits on WMD's trajectory is timing (ignore); a gap far off the whole series
is basis/definition (INVESTIGATE). Co-product and artisanal metals are
auto-tagged (attribution inherently differs). RESOLVED holds documented
per-material source decisions. WMD never feeds scoring — audit layer only.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional

YEARS = (2020, 2021, 2022, 2023, 2024)

# Deterministic basis notes — differences we already KNOW aren't vintage.
KNOWN_BASIS: dict[str, str] = {
    "Copper": "Our USGS table mixes refined output into mine production (JP/DE/KR appear as 'producers'); WMD is mine-stage. Stage mismatch — reload or relabel our table.",
    "Titanium": "USGS = titanium sponge metal; WMD = mineral-sands TiO2 content. Different product & stage entirely.",
    "Chromium": "USGS = gross marketable ore; WMD = Cr2O3 content. Grade differences shift shares (esp. Türkiye).",
    "Lithium": "USGS = Li content; WMD = Li2O content. Bases differ (×2.153 LCE); both count China lepidolite.",
    "Iron Ore": "USGS = gross ore tonnes; WMD = Fe content. Different basis by design.",
    "Aluminum": "Both sources are smelter (refined-stage) production. Differences here are tail-coverage/vintage, not basis.",
}
# Byproduct metals — recovered at smelters/refineries from a primary metal's
# ore, so country attribution is inherently compiler-dependent. A USGS/WMD gap
# here is expected and unresolvable, NOT a bug to chase.
CO_PRODUCT_METALS = {
    "Rhenium", "Selenium", "Tellurium", "Indium", "Germanium", "Gallium",
    "Cadmium", "Bismuth", "Arsenic", "Zirconium",
}
# Metals with a large informal/artisanal share that official statistics
# capture unevenly — divergence expected; both are estimates.
ARTISANAL_METALS = {"Tantalum", "Tin"}

CAUSE_NOTE = {
    "coproduct": "Byproduct metal — attribution is compiler-dependent (credited at the smelter/refinery, not the mine). Neither source authoritative; a gap is expected, not a bug.",
    "artisanal": "Large informal/artisanal share captured unevenly by official statistics. Both figures are estimates; divergence is expected.",
}

# HHI from stored USGS shares is over the truncated top-N universe (inflated).
SUPPRESS_MATERIALS = {"Platinum-Group Metals"}  # 3 WMD sheets → manual only

WMD_SUPPRESS_MIN_PP = 1.0  # don't list WMD-only countries below this share

# Documented source decisions (rendered as a green banner). A resolved
# INVESTIGATE case: we read both compilers' methodology and recorded which
# source wins per the single-source-per-stage rule.
RESOLVED: dict[str, str] = {
    "Natural Graphite": (
        "RESOLVED 2026-07-18 — KEEP USGS (India 17 kt, ~1%). WMD lists India at "
        "~116 kt (6.9%), sourced from IBM national statistics = gross run-of-mine "
        "amorphous graphite (low fixed-carbon, largely non-marketable). USGS "
        "explicitly REVISED India down to 17 kt in MCS 2026 'based on company and "
        "government reports' — a deliberate marketable-production basis, consistent "
        "across countries. Definitional difference; USGS is correct for our "
        "concentration pillar (China 80% marketable, not WMD's 76%). No change to "
        "stored value; decision logged."
    ),
}


@dataclass
class Row:
    country: str
    usgs_pct: Optional[float]
    wmd_series: dict[int, float]           # year -> share pct over full universe
    verdict: str = ""                       # agree|timing|basis|unexplained|usgs_only|wmd_only
    note: str = ""


@dataclass
class MatReport:
    material: str
    wmd_sheet: str
    basis: Optional[str]
    usgs_ref_year: int
    wmd_hhi: float
    usgs_hhi_trunc: float
    usgs_countries: int
    wmd_countries: int
    basis_banner: Optional[str]
    rows: list[Row] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for r in self.rows:
            c[r.verdict] = c.get(r.verdict, 0) + 1
        return c


def classify(usgs_pct: Optional[float], series: dict[int, float], *,
             threshold_pp: float, known_basis: bool,
             cause: Optional[str] = None) -> tuple[str, str]:
    w24 = series.get(2024)
    # WMD-only (USGS omits) — the tail-truncation manifestation
    if usgs_pct is None or usgs_pct == 0:
        if w24 and w24 >= WMD_SUPPRESS_MIN_PP:
            return "wmd_only", "USGS omits this producer (consistent with our top-N tail truncation)."
        return "agree", ""
    # USGS-only (WMD gap)
    if not w24:
        if usgs_pct >= threshold_pp:
            return "usgs_only", "WMD does not list this producer — keep USGS here."
        return "agree", ""
    if abs(usgs_pct - w24) < threshold_pp:
        return "agree", ""
    # Flagged. Vintage vs basis.
    recent = [series[y] for y in (2022, 2023, 2024) if y in series]
    step = (series.get(2024, 0) - series.get(2023, series.get(2024, 0)))
    lo, hi = min(recent), max(recent)
    proj25 = w24 + step
    if step > 0:
        hi = max(hi, proj25)
    elif step < 0:
        lo = min(lo, proj25)
    tol = max(1.5, 0.5 * abs(step))
    if lo - tol <= usgs_pct <= hi + tol:
        return "timing", f"USGS ~{usgs_pct:.1f}% continues WMD's {series.get(2023,0):.1f}→{w24:.1f}% trajectory (proj. {proj25:.1f}%). Timing, not error."
    ratio = usgs_pct / w24 if w24 else 999
    pp = abs(usgs_pct - w24)
    if known_basis:
        return "basis", f"Off WMD's series ({ratio:.1f}× WMD 2024) — expected from the known basis/stage difference above."
    severe = ratio >= 1.8 or ratio <= 0.55 or pp >= 6.0
    band = f"WMD held {min(recent):.1f}–{max(recent):.1f}% across 2022–24"
    if severe:
        if cause in CAUSE_NOTE:
            return cause, f"{CAUSE_NOTE[cause]} (USGS {usgs_pct:.1f}% vs WMD 2024 {w24:.1f}%, {ratio:.1f}×)"
        return "unexplained", f"USGS {usgs_pct:.1f}% vs WMD 2024 {w24:.1f}% ({ratio:.1f}×, {pp:.1f}pp); {band}. Off the whole series → definitional, not vintage. Read both sources' methodology."
    return "mild", f"USGS {usgs_pct:.1f}% vs WMD 2024 {w24:.1f}% ({pp:.1f}pp, just outside WMD's recent band) — likely vintage/rounding."


def hhi_from_shares(shares_pct: list[float]) -> float:
    return sum((s / 100.0) ** 2 for s in shares_pct) * 10000


_BADGE = {
    "unexplained": ("#b3261e", "#fff", "INVESTIGATE"),
    "basis":       ("#8a5a00", "#fff", "basis"),
    "coproduct":   ("#1c6e6e", "#dff", "co-product"),
    "artisanal":   ("#8a4a1e", "#fee", "artisanal"),
    "mild":        ("#4a4326", "#e8dca8", "review"),
    "usgs_only":   ("#7a2f9e", "#fff", "USGS-only"),
    "wmd_only":    ("#2b5c8a", "#fff", "WMD-only"),
    "timing":      ("#3a3f4a", "#cfd4dc", "timing"),
    "agree":       ("#1f6d3a", "#fff", "agree"),
}
_ORDER = ["unexplained", "basis", "coproduct", "artisanal", "mild",
          "usgs_only", "wmd_only", "timing", "agree"]


def _spark(series: dict[int, float]) -> str:
    return " ".join(f"{series[y]:.1f}" if y in series else "·" for y in YEARS)


def render_html(reports: list[MatReport], *, year: int, threshold_pp: float,
                generated: str, skipped: list[str]) -> str:
    def badge(v):
        bg, fg, lbl = _BADGE.get(v, ("#555", "#fff", v))
        return f'<span style="background:{bg};color:{fg};padding:1px 7px;border-radius:10px;font-size:11px;white-space:nowrap">{lbl}</span>'

    def open_unexpl(m):  # unexplained cells NOT covered by a resolved decision
        return 0 if m.material in RESOLVED else m.counts().get("unexplained", 0)

    reports = sorted(reports, key=lambda m: (-open_unexpl(m),
                                             -(1 if m.basis_banner and m.material not in ("Aluminum",) else 0),
                                             m.material))
    total_unexpl = sum(open_unexpl(m) for m in reports)
    total_resolved = sum(1 for m in reports if m.material in RESOLVED)

    parts = [f'''<!doctype html><html><head><meta charset="utf-8">
<title>USGS × WMD crosscheck {year}</title>
<style>
 body{{font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#0f1216;color:#e6e9ee}}
 .wrap{{max-width:1100px;margin:0 auto;padding:28px 22px 80px}}
 h1{{font-size:22px;margin:0 0 4px}} .sub{{color:#9aa2ad;font-size:13px;margin-bottom:20px}}
 table{{border-collapse:collapse;width:100%;margin:6px 0 4px}}
 th,td{{text-align:left;padding:5px 9px;border-bottom:1px solid #222831;font-variant-numeric:tabular-nums}}
 th{{color:#9aa2ad;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.03em}}
 details{{background:#161b22;border:1px solid #222831;border-radius:9px;margin:10px 0;padding:4px 14px}}
 summary{{cursor:pointer;padding:9px 2px;font-weight:600;font-size:15px;list-style:none;display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
 summary::-webkit-details-marker{{display:none}}
 .cnt{{font-size:12px;color:#9aa2ad;font-weight:400}}
 .banner{{background:#3a2a00;border-left:3px solid #8a5a00;padding:8px 12px;border-radius:4px;margin:8px 0;font-size:13px;color:#ffd98a}}
 .resolved{{background:#0f2e1a;border-left:3px solid #1f6d3a;padding:8px 12px;border-radius:4px;margin:8px 0;font-size:13px;color:#a8e6bf}}
 .num{{text-align:right}} .mut{{color:#8a929c}} .note{{color:#aab2bd;font-size:12px}}
 .sumtab tr:hover{{background:#161b22}} a{{color:#6cb6ff}}
 .kbig{{font-size:34px;font-weight:700}} .krow{{display:flex;gap:34px;margin:14px 0 26px;flex-wrap:wrap}}
 .kcell small{{display:block;color:#9aa2ad;font-size:12px;font-weight:400}}
</style></head><body><div class="wrap">
<h1>USGS × World Mining Data — production-share crosscheck</h1>
<div class="sub">Generated {generated} · USGS ref latest vs WMD {year} · flag threshold {threshold_pp:.0f}pp · WMD 2020–2024 series used as vintage control</div>
<div class="krow">
  <div class="kcell"><span class="kbig" style="color:#ff6b60">{total_unexpl}</span><small>open INVESTIGATE cells (real work)</small></div>
  <div class="kcell"><span class="kbig" style="color:#7fd39b">{total_resolved}</span><small>resolved decisions logged</small></div>
  <div class="kcell"><span class="kbig">{len(reports)}</span><small>mapped materials compared</small></div>
</div>
<p class="note">Read order: <b>INVESTIGATE</b> = off WMD's whole series with no known cause — read both compilers' methodology. <b>basis</b> = documented stage/content difference. <b>co-product</b> = byproduct metal, attribution inherently compiler-dependent (expected, unresolvable). <b>artisanal</b> = large informal share captured unevenly (expected). <b>timing</b> = USGS estimate sits on WMD's trajectory — ignore. <b>WMD-only</b> = producer USGS drops via top-N truncation. <b>USGS-only</b> = WMD gap. Green banner = resolved decision.</p>
<table class="sumtab"><thead><tr><th>Material</th><th class="num">WMD&nbsp;HHI</th><th class="num">USGS&nbsp;HHI*</th><th class="num">WMD&nbsp;ctry</th><th class="num">USGS&nbsp;ctry</th><th>flags</th></tr></thead><tbody>''']

    for m in reports:
        c = dict(m.counts())
        if m.material in RESOLVED and c.get("unexplained"):
            c.pop("unexplained")  # covered by the green decision banner
            c["resolved✓"] = 1
        chips = "".join((f'<span style="background:#1f6d3a;color:#fff;padding:1px 7px;border-radius:10px;font-size:11px">resolved ✓</span>&nbsp;'
                         if v == "resolved✓" else f'{badge(v)}&nbsp;{c[v]} ')
                        for v in (_ORDER + ["resolved✓"]) if c.get(v))
        star = " ⚠" if m.material in KNOWN_BASIS and m.material != "Aluminum" else ""
        parts.append(f'<tr><td><a href="#{m.material.replace(" ","_")}">{m.material}{star}</a></td>'
                     f'<td class="num">{m.wmd_hhi:,.0f}</td><td class="num mut">{m.usgs_hhi_trunc:,.0f}</td>'
                     f'<td class="num">{m.wmd_countries}</td><td class="num">{m.usgs_countries}</td><td>{chips or "—"}</td></tr>')
    parts.append("</tbody></table>")
    parts.append('<p class="note">*USGS HHI is computed over our stored top-N universe (shares renormalized to 100%), so it is systematically inflated vs the full-universe WMD HHI — itself a finding (loader fix pending).</p>')

    for m in reports:
        c = m.counts()
        head = "".join(f'{badge(v)}&nbsp;<span class="cnt">{c[v]}</span>&nbsp;&nbsp;' for v in _ORDER if c.get(v))
        parts.append(f'<details id="{m.material.replace(" ","_")}"><summary>{m.material} '
                     f'<span class="cnt">· {m.wmd_sheet} · basis {m.basis or "—"}</span> {head}</summary>')
        if m.material in RESOLVED:
            parts.append(f'<div class="resolved">✓ {RESOLVED[m.material]}</div>')
        if m.basis_banner:
            parts.append(f'<div class="banner">{m.basis_banner}</div>')
        parts.append('<table><thead><tr><th>Country</th><th class="num">USGS %</th>'
                     '<th class="num">WMD 20 21 22 23 24 (%)</th><th></th><th>note</th></tr></thead><tbody>')
        rows = sorted(m.rows, key=lambda r: (_ORDER.index(r.verdict) if r.verdict in _ORDER else 9,
                                             -(r.wmd_series.get(2024) or r.usgs_pct or 0)))
        for r in rows:
            if r.verdict == "agree":
                continue
            u = f"{r.usgs_pct:.2f}" if r.usgs_pct else '<span class="mut">—</span>'
            parts.append(f'<tr><td>{r.country}</td><td class="num">{u}</td>'
                         f'<td class="num mut">{_spark(r.wmd_series)}</td><td>{badge(r.verdict)}</td>'
                         f'<td class="note">{r.note}</td></tr>')
        agree_n = c.get("agree", 0)
        parts.append(f'</tbody></table><p class="note">+ {agree_n} countries agree within {threshold_pp:.0f}pp (hidden).</p></details>')

    if skipped:
        parts.append(f'<p class="note">Skipped (manual): {", ".join(skipped)} — multiple WMD sheets per material.</p>')
    parts.append("</div></body></html>")
    return "".join(parts)


# -- DB driver ----------------------------------------------------------
def run_crosscheck(session, *, year: int = 2024, threshold_pp: float = 2.0,
                   generated: str) -> tuple[list["MatReport"], list[str]]:
    """Build reports from wmd_production (full series) + material_production_shares (USGS)."""
    from sqlalchemy import select as _sel
    from app.models.supply import Material, MaterialProductionShare
    from app.models.wmd import WmdCommodity, WmdProduction

    comms = session.scalars(
        _sel(WmdCommodity).where(WmdCommodity.material_id.is_not(None))
    ).all()
    reports: list[MatReport] = []
    skipped: list[str] = []
    for comm in comms:
        mat = session.get(Material, comm.material_id)
        if mat is None or mat.canonical_name in SUPPRESS_MATERIALS:
            if mat:
                skipped.append(mat.canonical_name)
            continue
        prod = session.scalars(
            _sel(WmdProduction).where(WmdProduction.commodity_id == comm.id)
        ).all()
        by_year: dict[int, dict[str, float]] = {}
        for p in prod:
            by_year.setdefault(p.year, {})[p.country_code.upper()] = p.volume
        sbc: dict[str, dict[int, float]] = {}
        for yr, vols in by_year.items():
            tot = sum(vols.values())
            if tot <= 0:
                continue
            for cc, v in vols.items():
                if v > 0:
                    sbc.setdefault(cc, {})[yr] = v / tot * 100
        if not sbc:
            continue
        urows = session.scalars(
            _sel(MaterialProductionShare).where(
                MaterialProductionShare.material_id == comm.material_id,
                MaterialProductionShare.production_share > 0,
            )
        ).all()
        if not urows:
            continue
        ref = max(r.reference_year for r in urows)
        u = {r.country_code.upper(): float(r.production_share) * 100
             for r in urows if r.reference_year == ref}
        kb = mat.canonical_name in KNOWN_BASIS
        cause = ("coproduct" if mat.canonical_name in CO_PRODUCT_METALS
                 else "artisanal" if mat.canonical_name in ARTISANAL_METALS else None)
        rows: list[Row] = []
        for cc in sorted(set(sbc) | set(u)):
            verdict, note = classify(u.get(cc), sbc.get(cc, {}),
                                     threshold_pp=threshold_pp, known_basis=kb, cause=cause)
            rows.append(Row(cc, u.get(cc), sbc.get(cc, {}), verdict, note))
        wmd_hhi = hhi_from_shares([sbc[c][2024] for c in sbc if 2024 in sbc[c]])
        usgs_hhi = hhi_from_shares(list(u.values()))
        reports.append(MatReport(
            mat.canonical_name, comm.wmd_name, comm.content_basis, ref,
            wmd_hhi, usgs_hhi, len(u), len(sbc),
            KNOWN_BASIS.get(mat.canonical_name), rows,
        ))
    return reports, skipped
