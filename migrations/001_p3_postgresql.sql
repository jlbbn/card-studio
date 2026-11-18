BEGIN;

CREATE TABLE IF NOT EXISTS p3_operations (
    terminal_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    operation_type TEXT NOT NULL CHECK(operation_type IN ('purchase', 'refund', 'reversal')),
    amount_minor BIGINT NOT NULL CHECK(amount_minor > 0),
    currency CHAR(3) NOT NULL,
    transaction_state TEXT NOT NULL,
    original_transaction_id TEXT,
    request_digest TEXT NOT NULL,
    payload_json JSONB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(terminal_id, transaction_id),
    UNIQUE(terminal_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS p3_original ON p3_operations(terminal_id, original_transaction_id);

CREATE TABLE IF NOT EXISTS p3_nonces (
    terminal_id TEXT NOT NULL,
    nonce TEXT NOT NULL,
    seen_at TEXT NOT NULL,
    PRIMARY KEY(terminal_id, nonce)
);

CREATE TABLE IF NOT EXISTS p3_effects (
    effect_sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    effect_id TEXT NOT NULL UNIQUE,
    terminal_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    effect_type TEXT NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(terminal_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS p3_audit (
    sequence BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    terminal_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p3_treasury_cursor (
    consumer_id TEXT PRIMARY KEY,
    effect_sequence BIGINT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS p3_treasury_consumed (
    consumer_id TEXT NOT NULL,
    effect_id TEXT NOT NULL,
    effect_sequence BIGINT NOT NULL,
    terminal_id TEXT NOT NULL,
    transaction_id TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    amount_minor BIGINT NOT NULL,
    currency CHAR(3) NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(consumer_id, effect_id),
    UNIQUE(consumer_id, terminal_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS p3_reconciliation (
    run_id TEXT NOT NULL,
    item_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('MATCHED', 'MISSING', 'DUPLICATE', 'LATE', 'MISMATCH', 'REVIEW_REQUIRED')),
    detail_json JSONB NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(run_id, item_key, status)
);

CREATE TABLE IF NOT EXISTS p3_schema_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO p3_schema_metadata(key, value) VALUES ('schema_version', '1')
ON CONFLICT(key) DO NOTHING;

COMMIT;
