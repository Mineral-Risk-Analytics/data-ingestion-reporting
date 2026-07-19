-- V1 (4.0) cleanup — run AFTER the full rescore is verified. Destructive.
-- Removes superseded 3.x rows and the pre-universe-restriction 4.0 rows
-- (the 2026-07-17 cobalt vintage) so exactly one clean 4.0 vintage remains.

BEGIN;
-- L0 legacy
DELETE FROM hs_code_geography_risk_scores WHERE methodology_version = '3.0';
-- L1 legacy + stale 4.0 (pre geo-universe restriction, dated before today)
DELETE FROM material_geography_risk_scores WHERE scoring_version LIKE '3.%';
DELETE FROM material_geography_risk_scores
  WHERE scoring_version = '4.0' AND as_of_date < CURRENT_DATE;
-- L2 legacy
DELETE FROM material_global_risk_scores WHERE scoring_version LIKE '3.%';

-- sanity: should show only 4.0 rows, all today's date
SELECT scoring_version, count(*), min(as_of_date), max(as_of_date)
FROM material_geography_risk_scores GROUP BY 1 ORDER BY 1;
COMMIT;
