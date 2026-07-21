-- Market Morning MySQL incremental upgrade
-- From: 0016_market_morning_model_usage
-- To:   0017_market_morning_content_reports
-- Run only after confirming alembic_version is exactly the From revision.
-- Take a backup first. This file is not an idempotent bootstrap script.

SET NAMES utf8mb4;
SET time_zone = '+00:00';

-- Running upgrade 0016_market_morning_model_usage -> 0017_market_morning_content_reports

ALTER TABLE alembic_version MODIFY version_num VARCHAR(64) NOT NULL;

CREATE TABLE mm_content_reports (
    report_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    edition_id VARCHAR(36) NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    reason_code VARCHAR(32) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    resolution_code VARCHAR(64),
    reviewed_by VARCHAR(128),
    reviewed_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (report_id),
    CONSTRAINT ck_mm_content_report_reason CHECK (reason_code IN ('fact_inaccurate', 'source_mismatch', 'outdated_or_corrected', 'other_content_issue')),
    CONSTRAINT ck_mm_content_report_status CHECK (status IN ('pending', 'resolved', 'dismissed')),
    CONSTRAINT ck_mm_content_report_resolution_state CHECK ((status = 'pending' AND resolution_code IS NULL AND reviewed_by IS NULL AND reviewed_at IS NULL) OR (status IN ('resolved', 'dismissed') AND resolution_code IS NOT NULL AND reviewed_by IS NOT NULL AND reviewed_at IS NOT NULL)),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(edition_id) REFERENCES mm_morning_editions (edition_id) ON DELETE CASCADE,
    FOREIGN KEY(event_id) REFERENCES mm_normalized_events (event_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_content_report_user_edition_event UNIQUE (user_id, edition_id, event_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_content_report_status_created ON mm_content_reports (status, created_at);

CREATE INDEX ix_mm_content_report_event_created ON mm_content_reports (event_id, created_at);

UPDATE alembic_version SET version_num='0017_market_morning_content_reports' WHERE alembic_version.version_num = '0016_market_morning_model_usage';
