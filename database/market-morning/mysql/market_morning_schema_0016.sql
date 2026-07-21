-- Market Morning MySQL schema
-- Target revision: 0016_market_morning_model_usage
-- Generated from agent/migrations/market_morning for a NEW, EMPTY database.
-- Requirements: MySQL 8.x, InnoDB, utf8mb4, and a UTC connection session.
-- Do not run this full bootstrap file against an existing migrated database.

SET NAMES utf8mb4;
SET time_zone = '+00:00';

CREATE TABLE alembic_version (
    version_num VARCHAR(64) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

-- Running upgrade  -> 0001_market_morning_foundation

CREATE TABLE mm_users (
    user_id VARCHAR(36) NOT NULL,
    external_subject VARCHAR(255) NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'Asia/Tokyo',
    email_opt_in BOOL NOT NULL DEFAULT 0,
    account_status VARCHAR(32) NOT NULL DEFAULT 'invited',
    trial_or_subscription_status VARCHAR(32) NOT NULL DEFAULT 'private_beta',
    last_product_activity_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    deleted_at DATETIME(6),
    PRIMARY KEY (user_id),
    CONSTRAINT uq_mm_users_external_subject UNIQUE (external_subject)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE TABLE mm_user_consents (
    consent_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    consent_type VARCHAR(64) NOT NULL,
    consent_version VARCHAR(64) NOT NULL,
    accepted_at DATETIME(6) NOT NULL,
    revoked_at DATETIME(6),
    PRIMARY KEY (consent_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_user_consent_version UNIQUE (user_id, consent_type, consent_version)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE TABLE mm_issuer_snapshots (
    snapshot_id VARCHAR(36) NOT NULL,
    source_provider VARCHAR(64) NOT NULL,
    source_version VARCHAR(128) NOT NULL,
    source_url TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    checksum_sha256 VARCHAR(64) NOT NULL,
    import_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    fetched_at DATETIME(6) NOT NULL,
    imported_at DATETIME(6),
    PRIMARY KEY (snapshot_id),
    CONSTRAINT uq_mm_issuer_snapshot_source_version UNIQUE (source_provider, source_version)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE TABLE mm_issuers (
    issuer_id VARCHAR(36) NOT NULL,
    issuer_code VARCHAR(12) NOT NULL,
    legal_name_ja VARCHAR(255) NOT NULL,
    normalized_search_key VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
    market_segment VARCHAR(64) NOT NULL,
    source_snapshot_id VARCHAR(36) NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE,
    active_status VARCHAR(32) NOT NULL DEFAULT 'active',
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (issuer_id),
    FOREIGN KEY(source_snapshot_id) REFERENCES mm_issuer_snapshots (snapshot_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_issuer_code_effective_from UNIQUE (issuer_code, effective_from)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_issuers_search_key ON mm_issuers (normalized_search_key);

CREATE INDEX ix_mm_issuers_active_code ON mm_issuers (active_status, issuer_code);

CREATE TABLE mm_issuer_aliases (
    alias_id VARCHAR(36) NOT NULL,
    issuer_id VARCHAR(36) NOT NULL,
    display_alias VARCHAR(255) NOT NULL,
    normalized_alias VARCHAR(255) COLLATE utf8mb4_bin NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    source_reference TEXT,
    review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    effective_from DATE NOT NULL,
    effective_to DATE,
    approved_search_key VARCHAR(255) COLLATE utf8mb4_bin GENERATED ALWAYS AS (CASE WHEN review_status = 'approved' AND effective_to IS NULL THEN normalized_alias ELSE NULL END) STORED,
    reviewed_by VARCHAR(255),
    reviewed_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (alias_id),
    FOREIGN KEY(issuer_id) REFERENCES mm_issuers (issuer_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_issuer_alias UNIQUE (issuer_id, normalized_alias),
    CONSTRAINT uq_mm_issuer_alias_approved_search_key UNIQUE (approved_search_key)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_issuer_alias_search ON mm_issuer_aliases (review_status, normalized_alias);

CREATE TABLE mm_audit_logs (
    audit_id VARCHAR(36) NOT NULL,
    actor_user_id VARCHAR(36),
    action VARCHAR(128) NOT NULL,
    entity_type VARCHAR(64) NOT NULL,
    entity_id VARCHAR(64) NOT NULL,
    details JSON,
    occurred_at DATETIME(6) NOT NULL,
    PRIMARY KEY (audit_id),
    FOREIGN KEY(actor_user_id) REFERENCES mm_users (user_id) ON DELETE SET NULL
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_audit_entity ON mm_audit_logs (entity_type, entity_id, occurred_at);

CREATE INDEX ix_mm_audit_actor ON mm_audit_logs (actor_user_id, occurred_at);

INSERT INTO alembic_version (version_num) VALUES ('0001_market_morning_foundation');

-- Running upgrade 0001_market_morning_foundation -> 0002_market_morning_watchlist

CREATE TABLE mm_watchlist_items (
    watchlist_item_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    issuer_id VARCHAR(36) NOT NULL,
    user_label VARCHAR(40),
    sort_order INTEGER NOT NULL DEFAULT 0,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    removed_at DATETIME(6),
    active_issuer_id VARCHAR(36) GENERATED ALWAYS AS (CASE WHEN removed_at IS NULL THEN issuer_id ELSE NULL END) STORED,
    PRIMARY KEY (watchlist_item_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(issuer_id) REFERENCES mm_issuers (issuer_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_watchlist_active_user_issuer UNIQUE (user_id, active_issuer_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_watchlist_user_active_order ON mm_watchlist_items (user_id, removed_at, sort_order, created_at);

CREATE INDEX ix_mm_watchlist_issuer_active ON mm_watchlist_items (issuer_id, removed_at);

UPDATE alembic_version SET version_num='0002_market_morning_watchlist' WHERE alembic_version.version_num = '0001_market_morning_foundation';

-- Running upgrade 0002_market_morning_watchlist -> 0003_market_morning_settings

CREATE TABLE mm_account_deletion_requests (
    request_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    requested_at DATETIME(6) NOT NULL,
    processing_started_at DATETIME(6),
    completed_at DATETIME(6),
    updated_at DATETIME(6) NOT NULL,
    active_user_id VARCHAR(36) GENERATED ALWAYS AS (CASE WHEN status IN ('pending', 'processing') THEN user_id ELSE NULL END) STORED,
    PRIMARY KEY (request_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_account_deletion_active_user UNIQUE (active_user_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_account_deletion_status_requested ON mm_account_deletion_requests (status, requested_at);

CREATE TABLE mm_analytics_events (
    event_id VARCHAR(36) NOT NULL,
    event_name VARCHAR(64) NOT NULL,
    user_id VARCHAR(36),
    request_id VARCHAR(64),
    schema_version INTEGER NOT NULL DEFAULT 1,
    properties JSON NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    PRIMARY KEY (event_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE SET NULL,
    CONSTRAINT uq_mm_analytics_event_request UNIQUE (event_name, request_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_analytics_name_occurred ON mm_analytics_events (event_name, occurred_at);

CREATE INDEX ix_mm_analytics_user_occurred ON mm_analytics_events (user_id, occurred_at);

UPDATE alembic_version SET version_num='0003_market_morning_settings' WHERE alembic_version.version_num = '0002_market_morning_watchlist';

-- Running upgrade 0003_market_morning_settings -> 0004_market_morning_sources_events

CREATE TABLE mm_source_cursors (
    source_provider VARCHAR(64) NOT NULL,
    cursor_value JSON,
    last_successful_discovery_at DATETIME(6),
    last_error_at DATETIME(6),
    last_error_code VARCHAR(64),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (source_provider)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_source_cursor_health ON mm_source_cursors (last_error_at, last_successful_discovery_at);

CREATE TABLE mm_source_records (
    source_record_id VARCHAR(36) NOT NULL,
    source_provider VARCHAR(64) NOT NULL,
    provider_document_id VARCHAR(191) NOT NULL,
    provider_revision_key VARCHAR(128) NOT NULL,
    original_url TEXT NOT NULL,
    title VARCHAR(512) NOT NULL,
    document_type VARCHAR(64) NOT NULL,
    published_at DATETIME(6) NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'active',
    content_hash_sha256 VARCHAR(64) NOT NULL,
    normalized_payload JSON NOT NULL,
    supersedes_record_id VARCHAR(36),
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (source_record_id),
    FOREIGN KEY(supersedes_record_id) REFERENCES mm_source_records (source_record_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_source_record_provider_document_revision UNIQUE (source_provider, provider_document_id, provider_revision_key)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_source_record_document_history ON mm_source_records (source_provider, provider_document_id, published_at);

CREATE INDEX ix_mm_source_record_status_published ON mm_source_records (lifecycle_status, published_at);

CREATE TABLE mm_normalized_events (
    event_id VARCHAR(36) NOT NULL,
    event_family_key VARCHAR(191) COLLATE utf8mb4_bin NOT NULL,
    event_version INTEGER NOT NULL,
    issuer_id VARCHAR(36) NOT NULL,
    title VARCHAR(512) NOT NULL,
    normalized_title VARCHAR(512) COLLATE utf8mb4_bin NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    occurred_at DATETIME(6) NOT NULL,
    lifecycle_status VARCHAR(32) NOT NULL DEFAULT 'active',
    supersedes_event_id VARCHAR(36),
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (event_id),
    FOREIGN KEY(issuer_id) REFERENCES mm_issuers (issuer_id) ON DELETE RESTRICT,
    FOREIGN KEY(supersedes_event_id) REFERENCES mm_normalized_events (event_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_event_family_version UNIQUE (event_family_key, event_version)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_event_issuer_occurred ON mm_normalized_events (issuer_id, occurred_at);

CREATE INDEX ix_mm_event_status_occurred ON mm_normalized_events (lifecycle_status, occurred_at);

CREATE TABLE mm_event_sources (
    event_source_id VARCHAR(36) NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    source_record_id VARCHAR(36) NOT NULL,
    relation_type VARCHAR(32) NOT NULL DEFAULT 'primary',
    linked_at DATETIME(6) NOT NULL,
    PRIMARY KEY (event_source_id),
    FOREIGN KEY(event_id) REFERENCES mm_normalized_events (event_id) ON DELETE CASCADE,
    FOREIGN KEY(source_record_id) REFERENCES mm_source_records (source_record_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_event_source_link UNIQUE (event_id, source_record_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_event_source_record ON mm_event_sources (source_record_id, linked_at);

CREATE TABLE mm_event_merge_candidates (
    candidate_id VARCHAR(36) NOT NULL,
    left_event_id VARCHAR(36) NOT NULL,
    right_event_id VARCHAR(36) NOT NULL,
    reason VARCHAR(128) NOT NULL,
    title_similarity FLOAT NOT NULL,
    time_distance_seconds INTEGER NOT NULL,
    review_status VARCHAR(32) NOT NULL DEFAULT 'pending',
    reviewed_by VARCHAR(255),
    reviewed_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (candidate_id),
    FOREIGN KEY(left_event_id) REFERENCES mm_normalized_events (event_id) ON DELETE CASCADE,
    FOREIGN KEY(right_event_id) REFERENCES mm_normalized_events (event_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_event_merge_candidate_pair UNIQUE (left_event_id, right_event_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_event_merge_review ON mm_event_merge_candidates (review_status, created_at);

UPDATE alembic_version SET version_num='0004_market_morning_sources_events' WHERE alembic_version.version_num = '0003_market_morning_settings';

-- Running upgrade 0004_market_morning_sources_events -> 0005_market_morning_editions

CREATE TABLE mm_morning_editions (
    edition_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    edition_date DATE NOT NULL,
    edition_version INTEGER NOT NULL,
    generation_key VARCHAR(128) NOT NULL,
    schema_version INTEGER NOT NULL,
    status VARCHAR(32) NOT NULL,
    payload JSON NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    generated_at DATETIME(6) NOT NULL,
    published_at DATETIME(6) NOT NULL,
    supersedes_edition_id VARCHAR(36),
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (edition_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(supersedes_edition_id) REFERENCES mm_morning_editions (edition_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_edition_user_date_version UNIQUE (user_id, edition_date, edition_version),
    CONSTRAINT uq_mm_edition_user_generation_key UNIQUE (user_id, generation_key)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_edition_user_date_version ON mm_morning_editions (user_id, edition_date, edition_version);

UPDATE alembic_version SET version_num='0005_market_morning_editions' WHERE alembic_version.version_num = '0004_market_morning_sources_events';

-- Running upgrade 0005_market_morning_editions -> 0006_market_morning_jobs

CREATE TABLE mm_jobs (
    job_id VARCHAR(36) NOT NULL,
    job_type VARCHAR(64) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    payload JSON NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    priority INTEGER NOT NULL,
    available_at DATETIME(6) NOT NULL,
    max_attempts INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    lease_owner VARCHAR(128),
    lease_expires_at DATETIME(6),
    last_error_code VARCHAR(64),
    completed_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (job_id),
    CONSTRAINT uq_mm_job_idempotency_key UNIQUE (idempotency_key)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_job_claim ON mm_jobs (status, available_at, priority, created_at);

CREATE INDEX ix_mm_job_expired_lease ON mm_jobs (status, lease_expires_at);

CREATE TABLE mm_job_attempts (
    attempt_id VARCHAR(36) NOT NULL,
    job_id VARCHAR(36) NOT NULL,
    attempt_number INTEGER NOT NULL,
    worker_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    started_at DATETIME(6) NOT NULL,
    finished_at DATETIME(6),
    error_code VARCHAR(64),
    details JSON,
    PRIMARY KEY (attempt_id),
    FOREIGN KEY(job_id) REFERENCES mm_jobs (job_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_job_attempt_number UNIQUE (job_id, attempt_number)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_job_attempt_status ON mm_job_attempts (job_id, status, started_at);

CREATE TABLE mm_scheduler_leases (
    lease_name VARCHAR(128) NOT NULL,
    owner_id VARCHAR(128) NOT NULL,
    lease_until DATETIME(6) NOT NULL,
    heartbeat_at DATETIME(6) NOT NULL,
    acquired_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (lease_name)
)ENGINE=InnoDB CHARSET=utf8mb4;

UPDATE alembic_version SET version_num='0006_market_morning_jobs' WHERE alembic_version.version_num = '0005_market_morning_editions';

-- Running upgrade 0006_market_morning_jobs -> 0007_market_morning_manual_overrides

CREATE TABLE mm_manual_overrides (
    override_id VARCHAR(36) NOT NULL,
    override_type VARCHAR(32) NOT NULL,
    edition_date DATE NOT NULL,
    reason_code VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    active_edition_date DATE GENERATED ALWAYS AS (CASE WHEN override_type = 'publication_halt' AND status = 'active' THEN edition_date ELSE NULL END) STORED,
    created_by VARCHAR(128) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    revoked_by VARCHAR(128),
    revoked_at DATETIME(6),
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (override_id),
    CONSTRAINT uq_mm_manual_override_active_date UNIQUE (active_edition_date)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_manual_override_date_status ON mm_manual_overrides (edition_date, status, created_at);

UPDATE alembic_version SET version_num='0007_market_morning_manual_overrides' WHERE alembic_version.version_num = '0006_market_morning_jobs';

-- Running upgrade 0007_market_morning_manual_overrides -> 0008_market_morning_market_snapshots

CREATE TABLE mm_market_snapshots (
    snapshot_id VARCHAR(36) NOT NULL,
    instrument VARCHAR(32) NOT NULL,
    provider VARCHAR(64) NOT NULL,
    session_date DATE NOT NULL,
    as_of DATETIME(6) NOT NULL,
    value NUMERIC(24, 8) NOT NULL,
    previous_close NUMERIC(24, 8),
    currency VARCHAR(8) NOT NULL,
    delay_status VARCHAR(16) NOT NULL,
    fetched_at DATETIME(6) NOT NULL,
    snapshot_sha256 VARCHAR(64) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (snapshot_id),
    CONSTRAINT uq_mm_market_snapshot_identity UNIQUE (instrument, provider, session_date, as_of)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_market_snapshot_latest ON mm_market_snapshots (instrument, session_date, as_of);

UPDATE alembic_version SET version_num='0008_market_morning_market_snapshots' WHERE alembic_version.version_num = '0007_market_morning_manual_overrides';

-- Running upgrade 0008_market_morning_market_snapshots -> 0009_market_morning_global_runs

CREATE TABLE mm_global_edition_days (
    edition_date DATE NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (edition_date)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE TABLE mm_global_edition_runs (
    run_id VARCHAR(36) NOT NULL,
    edition_date DATE NOT NULL,
    run_version INTEGER NOT NULL,
    generation_key VARCHAR(191) NOT NULL,
    attempt_key VARCHAR(16) NOT NULL,
    scenario VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    is_current BOOL NOT NULL,
    current_success_date DATE GENERATED ALWAYS AS (CASE WHEN is_current = 1 AND status IN ('complete', 'partial', 'late') THEN edition_date ELSE NULL END) STORED,
    email_permitted BOOL NOT NULL,
    late BOOL NOT NULL,
    reason_code VARCHAR(64),
    spec JSON NOT NULL,
    spec_sha256 VARCHAR(64) NOT NULL,
    manifest JSON,
    manifest_sha256 VARCHAR(64),
    started_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (run_id),
    FOREIGN KEY(edition_date) REFERENCES mm_global_edition_days (edition_date) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_global_run_date_version UNIQUE (edition_date, run_version),
    CONSTRAINT uq_mm_global_run_generation_key UNIQUE (generation_key),
    CONSTRAINT uq_mm_global_run_current_success UNIQUE (current_success_date)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_global_run_date_status ON mm_global_edition_runs (edition_date, status, started_at);

CREATE TABLE mm_global_edition_items (
    item_id VARCHAR(36) NOT NULL,
    run_id VARCHAR(36) NOT NULL,
    position INTEGER NOT NULL,
    item_type VARCHAR(32) NOT NULL,
    snapshot_id VARCHAR(36) NOT NULL,
    instrument VARCHAR(32) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (item_id),
    FOREIGN KEY(run_id) REFERENCES mm_global_edition_runs (run_id) ON DELETE CASCADE,
    FOREIGN KEY(snapshot_id) REFERENCES mm_market_snapshots (snapshot_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_global_item_run_position UNIQUE (run_id, position),
    CONSTRAINT uq_mm_global_item_run_snapshot UNIQUE (run_id, snapshot_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_global_item_run_type ON mm_global_edition_items (run_id, item_type, position);

UPDATE alembic_version SET version_num='0009_market_morning_global_runs' WHERE alembic_version.version_num = '0008_market_morning_market_snapshots';

-- Running upgrade 0009_market_morning_global_runs -> 0010_market_morning_event_briefs

CREATE TABLE mm_event_briefs (
    brief_id VARCHAR(36) NOT NULL,
    event_id VARCHAR(36) NOT NULL,
    event_version INTEGER NOT NULL,
    generation_key VARCHAR(191) NOT NULL,
    schema_version INTEGER NOT NULL,
    model_version VARCHAR(64) NOT NULL,
    prompt_version VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL,
    review_status VARCHAR(32) NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    spec JSON NOT NULL,
    spec_sha256 VARCHAR(64) NOT NULL,
    payload JSON,
    payload_sha256 VARCHAR(64),
    source_ids JSON NOT NULL,
    validation_errors JSON NOT NULL,
    primary_source_record_id VARCHAR(36),
    last_failure_code VARCHAR(64),
    last_failed_at DATETIME(6),
    started_at DATETIME(6) NOT NULL,
    completed_at DATETIME(6),
    published_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (brief_id),
    CONSTRAINT ck_mm_event_brief_publishable_source CHECK (status NOT IN ('published', 'degraded') OR (primary_source_record_id IS NOT NULL AND JSON_LENGTH(source_ids) > 0)),
    FOREIGN KEY(event_id) REFERENCES mm_normalized_events (event_id) ON DELETE RESTRICT,
    FOREIGN KEY(primary_source_record_id) REFERENCES mm_source_records (source_record_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_event_brief_generation_key UNIQUE (generation_key),
    CONSTRAINT uq_mm_event_brief_generation_spec UNIQUE (event_id, event_version, schema_version, model_version, prompt_version)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_event_brief_event_status ON mm_event_briefs (event_id, status, updated_at);

CREATE INDEX ix_mm_event_brief_review ON mm_event_briefs (review_status, published_at);

CREATE TABLE mm_event_brief_sources (
    brief_source_id VARCHAR(36) NOT NULL,
    brief_id VARCHAR(36) NOT NULL,
    source_record_id VARCHAR(36) NOT NULL,
    position INTEGER NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (brief_source_id),
    FOREIGN KEY(brief_id) REFERENCES mm_event_briefs (brief_id) ON DELETE CASCADE,
    FOREIGN KEY(source_record_id) REFERENCES mm_source_records (source_record_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_event_brief_source_link UNIQUE (brief_id, source_record_id),
    CONSTRAINT uq_mm_event_brief_source_position UNIQUE (brief_id, position)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_event_brief_source_record ON mm_event_brief_sources (source_record_id, created_at);

UPDATE alembic_version SET version_num='0010_market_morning_event_briefs' WHERE alembic_version.version_num = '0009_market_morning_global_runs';

-- Running upgrade 0010_market_morning_event_briefs -> 0011_market_morning_delivery_attempts

CREATE TABLE mm_delivery_attempts (
    delivery_attempt_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    global_run_id VARCHAR(36) NOT NULL,
    edition_date DATE NOT NULL,
    channel VARCHAR(16) NOT NULL,
    idempotency_key VARCHAR(191) NOT NULL,
    status VARCHAR(32) NOT NULL,
    provider_message_id VARCHAR(191),
    deep_link_token_sha256 VARCHAR(64) NOT NULL,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    last_error_code VARCHAR(64),
    requested_at DATETIME(6) NOT NULL,
    sent_at DATETIME(6),
    delivered_at DATETIME(6),
    clicked_at DATETIME(6),
    failed_at DATETIME(6),
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (delivery_attempt_id),
    CONSTRAINT ck_mm_delivery_status CHECK (status IN ('pending', 'sending', 'sent', 'delivered', 'failed', 'clicked', 'suppressed')),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(global_run_id) REFERENCES mm_global_edition_runs (run_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_delivery_user_date_channel UNIQUE (user_id, edition_date, channel),
    CONSTRAINT uq_mm_delivery_idempotency_key UNIQUE (idempotency_key),
    CONSTRAINT uq_mm_delivery_provider_message UNIQUE (provider_message_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_delivery_status_requested ON mm_delivery_attempts (status, requested_at);

CREATE INDEX ix_mm_delivery_user_date ON mm_delivery_attempts (user_id, edition_date, requested_at);

UPDATE alembic_version SET version_num='0011_market_morning_delivery_attempts' WHERE alembic_version.version_num = '0010_market_morning_event_briefs';

-- Running upgrade 0011_market_morning_delivery_attempts -> 0012_market_morning_delivery_webhooks

CREATE TABLE mm_delivery_provider_events (
    provider_event_row_id VARCHAR(36) NOT NULL,
    delivery_attempt_id VARCHAR(36),
    provider VARCHAR(64) NOT NULL,
    provider_event_id VARCHAR(191) NOT NULL,
    provider_message_id VARCHAR(191) NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    payload_sha256 VARCHAR(64) NOT NULL,
    outcome VARCHAR(32) NOT NULL,
    error_code VARCHAR(64),
    received_at DATETIME(6) NOT NULL,
    processed_at DATETIME(6),
    PRIMARY KEY (provider_event_row_id),
    CONSTRAINT ck_mm_delivery_provider_event_type CHECK (event_type IN ('delivered', 'failed', 'clicked')),
    CONSTRAINT ck_mm_delivery_provider_event_outcome CHECK (outcome IN ('pending', 'applied', 'stale', 'unmatched', 'rejected')),
    FOREIGN KEY(delivery_attempt_id) REFERENCES mm_delivery_attempts (delivery_attempt_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_delivery_provider_event UNIQUE (provider, provider_event_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_delivery_provider_message_received ON mm_delivery_provider_events (provider_message_id, received_at);

CREATE INDEX ix_mm_delivery_provider_outcome ON mm_delivery_provider_events (outcome, received_at);

UPDATE alembic_version SET version_num='0012_market_morning_delivery_webhooks' WHERE alembic_version.version_num = '0011_market_morning_delivery_attempts';

-- Running upgrade 0012_market_morning_delivery_webhooks -> 0013_market_morning_edition_interactions

CREATE TABLE mm_edition_event_states (
    event_state_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    edition_id VARCHAR(36) NOT NULL,
    event_id VARCHAR(64) NOT NULL,
    state VARCHAR(16) NOT NULL,
    first_read_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (event_state_id),
    CONSTRAINT ck_mm_edition_event_state CHECK (state IN ('read', 'later', 'irrelevant')),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(edition_id) REFERENCES mm_morning_editions (edition_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_edition_event_state UNIQUE (user_id, edition_id, event_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_edition_event_state_user_edition ON mm_edition_event_states (user_id, edition_id, state);

CREATE TABLE mm_edition_source_opens (
    source_open_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    edition_id VARCHAR(36) NOT NULL,
    event_id VARCHAR(64) NOT NULL,
    source_record_id VARCHAR(36) NOT NULL,
    request_id VARCHAR(36) NOT NULL,
    opened_at DATETIME(6) NOT NULL,
    PRIMARY KEY (source_open_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(edition_id) REFERENCES mm_morning_editions (edition_id) ON DELETE CASCADE,
    FOREIGN KEY(source_record_id) REFERENCES mm_source_records (source_record_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_edition_source_open_request UNIQUE (user_id, request_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_edition_source_open_source ON mm_edition_source_opens (source_record_id, opened_at);

CREATE INDEX ix_mm_edition_source_open_user ON mm_edition_source_opens (user_id, opened_at);

UPDATE alembic_version SET version_num='0013_market_morning_edition_interactions' WHERE alembic_version.version_num = '0012_market_morning_delivery_webhooks';

-- Running upgrade 0013_market_morning_edition_interactions -> 0014_market_morning_issuer_research

CREATE TABLE mm_issuer_research_notes (
    note_id VARCHAR(36) NOT NULL,
    user_id VARCHAR(36) NOT NULL,
    issuer_id VARCHAR(36) NOT NULL,
    note_text TEXT NOT NULL,
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (note_id),
    FOREIGN KEY(user_id) REFERENCES mm_users (user_id) ON DELETE CASCADE,
    FOREIGN KEY(issuer_id) REFERENCES mm_issuers (issuer_id) ON DELETE RESTRICT,
    CONSTRAINT uq_mm_issuer_research_note_user_issuer UNIQUE (user_id, issuer_id)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_issuer_research_note_user_updated ON mm_issuer_research_notes (user_id, updated_at);

UPDATE alembic_version SET version_num='0014_market_morning_issuer_research' WHERE alembic_version.version_num = '0013_market_morning_edition_interactions';

-- Running upgrade 0014_market_morning_issuer_research -> 0015_market_morning_beta_privacy

CREATE TABLE mm_private_beta_invites (
    invite_id VARCHAR(36) NOT NULL,
    token_sha256 VARCHAR(64) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    expires_at DATETIME(6) NOT NULL,
    created_by_reference VARCHAR(255) NOT NULL,
    accepted_by_user_id VARCHAR(36),
    accepted_at DATETIME(6),
    revoked_at DATETIME(6),
    created_at DATETIME(6) NOT NULL,
    updated_at DATETIME(6) NOT NULL,
    PRIMARY KEY (invite_id),
    CONSTRAINT ck_mm_private_beta_invite_status CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    FOREIGN KEY(accepted_by_user_id) REFERENCES mm_users (user_id) ON DELETE SET NULL,
    CONSTRAINT uq_mm_private_beta_invite_token_sha256 UNIQUE (token_sha256)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_private_beta_invite_status_expires ON mm_private_beta_invites (status, expires_at);

UPDATE alembic_version SET version_num='0015_market_morning_beta_privacy' WHERE alembic_version.version_num = '0014_market_morning_issuer_research';

-- Running upgrade 0015_market_morning_beta_privacy -> 0016_market_morning_model_usage

CREATE TABLE mm_model_usage_events (
    usage_event_id VARCHAR(36) NOT NULL,
    usage_key VARCHAR(191) NOT NULL,
    brief_id VARCHAR(36) NOT NULL,
    attempt_number INTEGER NOT NULL,
    provider VARCHAR(64) NOT NULL,
    model VARCHAR(128) NOT NULL,
    usage_status VARCHAR(32) NOT NULL,
    cost_status VARCHAR(32) NOT NULL,
    input_tokens BIGINT,
    output_tokens BIGINT,
    total_tokens BIGINT,
    billable_cost_micros BIGINT,
    currency VARCHAR(3),
    provider_request_id_sha256 VARCHAR(64),
    occurred_at DATETIME(6) NOT NULL,
    created_at DATETIME(6) NOT NULL,
    PRIMARY KEY (usage_event_id),
    CONSTRAINT ck_mm_model_usage_attempt_positive CHECK (attempt_number >= 1),
    CONSTRAINT ck_mm_model_usage_non_negative CHECK ((input_tokens IS NULL OR input_tokens >= 0) AND (output_tokens IS NULL OR output_tokens >= 0) AND (total_tokens IS NULL OR total_tokens >= 0) AND (billable_cost_micros IS NULL OR billable_cost_micros >= 0)),
    CONSTRAINT ck_mm_model_usage_token_state CHECK ((usage_status = 'reported' AND input_tokens IS NOT NULL AND output_tokens IS NOT NULL AND total_tokens = input_tokens + output_tokens) OR (usage_status = 'missing' AND input_tokens IS NULL AND output_tokens IS NULL AND total_tokens IS NULL)),
    CONSTRAINT ck_mm_model_usage_cost_state CHECK ((cost_status = 'reported' AND billable_cost_micros IS NOT NULL AND currency IS NOT NULL) OR (cost_status = 'unpriced' AND billable_cost_micros IS NULL AND currency IS NULL)),
    FOREIGN KEY(brief_id) REFERENCES mm_event_briefs (brief_id) ON DELETE CASCADE,
    CONSTRAINT uq_mm_model_usage_key UNIQUE (usage_key),
    CONSTRAINT uq_mm_model_usage_brief_attempt UNIQUE (brief_id, attempt_number)
)ENGINE=InnoDB CHARSET=utf8mb4;

CREATE INDEX ix_mm_model_usage_occurred_status ON mm_model_usage_events (occurred_at, usage_status, cost_status);

UPDATE alembic_version SET version_num='0016_market_morning_model_usage' WHERE alembic_version.version_num = '0015_market_morning_beta_privacy';
