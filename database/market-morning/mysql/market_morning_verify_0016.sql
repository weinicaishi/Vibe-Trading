-- Read-only checks after applying market_morning_schema_0016.sql.
-- Run this with the Market Morning database selected.

SELECT
    DATABASE() AS selected_database,
    VERSION() AS mysql_version,
    @@SESSION.time_zone AS session_time_zone,
    @@character_set_database AS database_charset,
    @@collation_database AS database_collation;

SELECT version_num AS schema_revision
FROM alembic_version;

SELECT COUNT(*) AS market_morning_table_count
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND LEFT(table_name, 3) = 'mm_';

SELECT table_name
FROM information_schema.tables
WHERE table_schema = DATABASE()
  AND LEFT(table_name, 3) = 'mm_'
ORDER BY table_name;
