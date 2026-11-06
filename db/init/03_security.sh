#!/bin/bash
# Stage E3, only when SECURITY_MODE=on: user accounts with bcrypt hashes, the audit log,
# the least-privilege role the services use, and the idempotency UNIQUE index (that role
# is not allowed to create it). Passwords come from .env.
set -e
if [ "$SECURITY_MODE" != "on" ]; then exit 0; fi
psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
     -v app_pw="$APP_DB_PASSWORD" -v student_pw="$STUDENT_PASSWORD" -v admin_pw="$ADMIN_PASSWORD" <<'SQL'
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Synthetic accounts: student1..student50 (one per seeded student) and one admin.
CREATE TABLE users (
    id            SERIAL PRIMARY KEY,
    username      TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,  -- bcrypt, cost 10
    role          TEXT NOT NULL CHECK (role IN ('student', 'admin')),
    student_id    INT REFERENCES students(id),
    failed_logins INT NOT NULL DEFAULT 0,
    locked_until  TIMESTAMPTZ
);
INSERT INTO users (username, password_hash, role, student_id)
SELECT 'student' || id, crypt(:'student_pw', gen_salt('bf', 10)), 'student', id FROM students;
INSERT INTO users (username, password_hash, role)
VALUES ('admin', crypt(:'admin_pw', gen_salt('bf', 10)), 'admin');

CREATE TABLE audit_log (
    id         BIGSERIAL PRIMARY KEY,
    ts         TIMESTAMPTZ NOT NULL DEFAULT now(),
    request_id TEXT,
    user_id    TEXT,
    action     TEXT NOT NULL,
    resource   TEXT,
    result     TEXT NOT NULL,
    source_ip  TEXT
);

CREATE UNIQUE INDEX payments_idempotency_key ON payments (idempotency_key);

-- Least privilege: only what the code needs. A role that does not own a table cannot DROP
-- or ALTER it; TRUNCATE and DELETE are never granted; audit_log is INSERT only (append-only).
CREATE ROLE uni_app LOGIN PASSWORD :'app_pw';
GRANT USAGE ON SCHEMA public TO uni_app;
GRANT SELECT ON students, courses, grades TO uni_app;
GRANT SELECT, INSERT ON registrations, transcripts TO uni_app;
GRANT SELECT, INSERT, UPDATE ON payments, transcript_jobs TO uni_app;
GRANT INSERT ON ledger, audit_log TO uni_app;
GRANT SELECT, UPDATE ON users TO uni_app;
GRANT USAGE ON SEQUENCE payments_id_seq, ledger_id_seq, transcript_jobs_id_seq, audit_log_id_seq TO uni_app;
SQL
