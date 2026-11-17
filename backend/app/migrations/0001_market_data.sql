-- Market data: instrument list, candles, quotes.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE markets (
    coin          text PRIMARY KEY,
    asset_index   integer NOT NULL,
    sz_decimals   integer NOT NULL,
    max_leverage  integer NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE candles (
    coin       text             NOT NULL,
    "interval" text             NOT NULL,
    open_time  timestamptz      NOT NULL,
    o          double precision NOT NULL,
    h          double precision NOT NULL,
    l          double precision NOT NULL,
    c          double precision NOT NULL,
    v          double precision NOT NULL DEFAULT 0,
    trades     integer          NOT NULL DEFAULT 0,
    PRIMARY KEY (coin, "interval", open_time)
);
SELECT create_hypertable('candles', 'open_time', chunk_time_interval => INTERVAL '30 days');

CREATE TABLE quotes (
    coin  text             NOT NULL,
    ts    timestamptz      NOT NULL,
    bid   double precision NOT NULL,
    ask   double precision NOT NULL
);
CREATE INDEX quotes_coin_ts_idx ON quotes (coin, ts DESC);
SELECT create_hypertable('quotes', 'ts', chunk_time_interval => INTERVAL '1 day');
-- The ingestion service re-applies this with QUOTE_RETENTION_DAYS on start.
SELECT add_retention_policy('quotes', INTERVAL '7 days');
