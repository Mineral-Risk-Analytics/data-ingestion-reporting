-- V1 cleanup — run AFTER a full rescore is verified, ON THE SAME DAY as
-- that rescore (deletes key off CURRENT_DATE). Destructive.
--
-- Version stamps differ per level (checked live 2026-07-20):
--   L0 hs_code_geography_risk_scores . methodology_version : '3.0' / '4.0'...
--   L1 material_geography_risk_scores . scoring_version    : '3.x' / '4.0' / '4.1'...
--   L2 material_global_risk_scores   . scoring_version     : ROLLUP_VERSION '1.0' / '1.1'
-- so stale-vintage removal is DATE-based (version-agnostic): the rescore
-- upserts by (material, geo, as_of_date), meaning same-day rows are always
-- overwritten in place and anything dated before today is a superseded
-- vintage regardless of which version string it carries.

BEGIN;
-- explicit legacy-methodology rows (any date)
DELETE FROM hs_code_geography_risk_scores  WHERE methodology_version LIKE '3.%';
DELETE FROM material_geography_risk_scores WHERE scoring_version     LIKE '3.%';
DELETE FROM material_global_risk_scores    WHERE scoring_version     LIKE '3.%';

-- stale vintages: anything not from today's verified rescore
DELETE FROM hs_code_geography_risk_scores  WHERE as_of_date < CURRENT_DATE;
DELETE FROM material_geography_risk_scores WHERE as_of_date < CURRENT_DATE;
DELETE FROM material_global_risk_scores    WHERE as_of_date < CURRENT_DATE;

-- sanity: each level should show ONE version, all rows dated today
SELECT 'L0' AS level, methodology_version AS version, count(*), min(as_of_date), max(as_of_date)
FROM hs_code_geography_risk_scores GROUP BY 2
UNION ALL
SELECT 'L1', scoring_version, count(*), min(as_of_date), max(as_of_date)
FROM material_geography_risk_scores GROUP BY 2
UNION ALL
SELECT 'L2', scoring_version, count(*), min(as_of_date), max(as_of_date)
FROM material_global_risk_scores GROUP BY 2
ORDER BY 1, 2;
COMMIT;
