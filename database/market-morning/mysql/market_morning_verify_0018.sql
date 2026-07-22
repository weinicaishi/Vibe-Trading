-- Read-only checks after applying the 0018 bootstrap or 0017 -> 0018 upgrade.
-- Run this with the Market Morning database selected.

SELECT
    DATABASE() AS selected_database,
    VERSION() AS mysql_version,
    @@SESSION.time_zone AS session_time_zone,
    @@character_set_database AS database_charset,
    @@collation_database AS database_collation;

SELECT version_num AS schema_revision
FROM alembic_version;

SELECT
    version_num = '0018_market_morning_auth_sessions' AS schema_revision_is_current
FROM alembic_version;

SELECT COUNT(*) AS market_morning_table_count
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND LEFT(table_name, 3) = 'mm_';

SELECT
    COLUMN_TYPE AS alembic_version_column_type
FROM information_schema.columns
WHERE table_schema = DATABASE()
  AND table_name = 'alembic_version'
  AND column_name = 'version_num';

SELECT table_name
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND LEFT(table_name, 3) = 'mm_'
ORDER BY table_name;
