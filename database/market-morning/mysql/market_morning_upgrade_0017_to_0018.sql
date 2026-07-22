-- Market Morning MySQL incremental upgrade
-- From: 0017_market_morning_content_reports
-- To:   0018_market_morning_auth_sessions
-- Run only after confirming alembic_version is exactly the From revision.
-- Take a backup first. This file is not an idempotent bootstrap script.

SET NAMES utf8mb4;
SET time_zone = '+00:00';

-- Running upgrade 0017_market_morning_content_reports -> 0018_market_morning_auth_sessions

CREATE TABLE mm_auth_sessions (
    auth_session_id VARCHAR(36) NOT NULL,
    issuer_sha256 VARCHAR(64) NOT NULL,
    session_reference_sha256 VARCHAR(64) NOT NULL,
    external_subject VARCHAR(255) NOT NULL,
    status VARCHAR(16) NOT NULL DEFAULT 'active',
    token_issued_at DATETIME(6) NOT NULL,
    token_expires_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6),
    revocation_reason VARCHAR(64),
    PRIMARY KEY (auth_session_id),
    CONSTRAINT ck_mm_auth_session_status CHECK (status IN ('active', 'revoked')),
    CONSTRAINT ck_mm_auth_session_revocation_state CHECK ((status = 'active' AND revoked_at IS NULL AND revocation_reason IS NULL) OR (status = 'revoked' AND revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)),
    CONSTRAINT uq_mm_auth_session_issuer_reference UNIQUE (issuer_sha256, session_reference_sha256)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_auth_session_subject_status ON mm_auth_sessions (external_subject, status);

UPDATE alembic_version SET version_num='0018_market_morning_auth_sessions' WHERE alembic_version.version_num = '0017_market_morning_content_reports';
