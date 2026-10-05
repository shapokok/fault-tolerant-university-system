CREATE TABLE students (
    id      SERIAL PRIMARY KEY,
    name    TEXT NOT NULL,
    program TEXT NOT NULL
);

CREATE TABLE courses (
    id         SERIAL PRIMARY KEY,
    code       TEXT NOT NULL,
    title      TEXT NOT NULL,
    day        TEXT NOT NULL,
    start_time TIME NOT NULL,
    room       TEXT NOT NULL
);

CREATE TABLE registrations (
    student_id INT REFERENCES students(id),
    course_id  INT REFERENCES courses(id),
    PRIMARY KEY (student_id, course_id)
);

CREATE TABLE grades (
    student_id INT REFERENCES students(id),
    course_id  INT REFERENCES courses(id),
    points     NUMERIC(2,1) NOT NULL,
    PRIMARY KEY (student_id, course_id)
);

-- idempotency_key has no UNIQUE constraint in the baseline (added in stage 3).
CREATE TABLE payments (
    id              SERIAL PRIMARY KEY,
    student_id      INT REFERENCES students(id),
    amount          NUMERIC(10,2) NOT NULL,
    status          TEXT NOT NULL,  -- pending | completed | failed
    idempotency_key TEXT,
    bank_ref        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE ledger (
    id         SERIAL PRIMARY KEY,
    payment_id INT REFERENCES payments(id),
    amount     NUMERIC(10,2) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Batch transcript generation. last_student_id is the checkpoint.
CREATE TABLE transcript_jobs (
    id              SERIAL PRIMARY KEY,
    status          TEXT NOT NULL DEFAULT 'running',  -- running | done
    last_student_id INT NOT NULL DEFAULT 0,
    heartbeat       TIMESTAMPTZ DEFAULT now(),
    started_at      TIMESTAMPTZ DEFAULT now(),
    finished_at     TIMESTAMPTZ
);

CREATE TABLE transcripts (
    job_id     INT REFERENCES transcript_jobs(id),
    student_id INT REFERENCES students(id),
    gpa        NUMERIC(3,2),
    partial    BOOLEAN NOT NULL,
    PRIMARY KEY (job_id, student_id)
);
