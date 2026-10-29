-- Demonstration schema. demo_records holds synthetic notes written through the
-- API; batch_runs holds one summary row per business date, refreshed on replay.
CREATE TABLE demo_records (
    id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    note       text NOT NULL CHECK (char_length(note) <= 200),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE batch_runs (
    business_date date PRIMARY KEY,
    record_count  integer NOT NULL,
    run_count     integer NOT NULL DEFAULT 1,
    first_run_at  timestamptz NOT NULL DEFAULT now(),
    last_run_at   timestamptz NOT NULL DEFAULT now()
);
