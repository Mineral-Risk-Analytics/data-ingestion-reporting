# REMOVED: Country and Organization models have been dropped from the schema.
#
# Country was a reference table that only served as a FK target for
# the Organization model. Neither table appears in ads_database_schema_v1.
# Geography is handled throughout the codebase via ISO2 string codes
# (headquarters_country, source_geography, geography_code, etc.) with no FK
# enforcement — consistent with the schema doc's design.
#
# Organization was an undocumented leftover from an earlier architecture
# sketch. The companies table (with parent_company_id self-reference) covers
# its intended use case.
#
# This file is kept as a tombstone to prevent stale imports from breaking
# silently. Remove the file entirely once you confirm no external code
# references Country or Organization.
