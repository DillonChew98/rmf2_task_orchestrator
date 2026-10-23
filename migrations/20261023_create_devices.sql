CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,
    role        TEXT NOT NULL,
    device_type TEXT NOT NULL,
    protocol    JSONB NOT NULL,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE
);
