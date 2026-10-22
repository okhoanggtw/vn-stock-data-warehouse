-- =====================================================================
--  KHO DỮ LIỆU CHỨNG KHOÁN VIỆT NAM – Galaxy Schema (Fact Constellation)
--  PostgreSQL >= 13
--  Schema:
--    staging : dữ liệu thô vừa trích xuất (truncate mỗi lần chạy ETL)
--    dw      : kho dữ liệu (dimension + fact)
--    meta    : nhật ký ETL, lỗi chất lượng dữ liệu
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS staging;
CREATE SCHEMA IF NOT EXISTS dw;
CREATE SCHEMA IF NOT EXISTS meta;

-- ---------------------------------------------------------------------
-- STAGING
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS staging.stg_listing (
    ticker          VARCHAR(10)  NOT NULL,
    company_name    TEXT,
    exchange_code   VARCHAR(10),
    icb_code_l1     VARCHAR(10), icb_name_l1 TEXT,
    icb_code_l2     VARCHAR(10), icb_name_l2 TEXT,
    icb_code_l3     VARCHAR(10), icb_name_l3 TEXT,
    extracted_at    TIMESTAMP    DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.stg_price (
    ticker          VARCHAR(10)  NOT NULL,
    trade_date      DATE         NOT NULL,
    open            NUMERIC(14,3),
    high            NUMERIC(14,3),
    low             NUMERIC(14,3),
    close           NUMERIC(14,3),
    volume          BIGINT,
    extracted_at    TIMESTAMP    DEFAULT now()
);

CREATE TABLE IF NOT EXISTS staging.stg_index (
    index_code      VARCHAR(20)  NOT NULL,
    trade_date      DATE         NOT NULL,
    open            NUMERIC(14,3),
    high            NUMERIC(14,3),
    low             NUMERIC(14,3),
    close           NUMERIC(14,3),
    volume          BIGINT,
    extracted_at    TIMESTAMP    DEFAULT now()
);

-- Chỉ số tài chính ở dạng "dài": 1 dòng = 1 mã × 1 quý × 1 chỉ tiêu
CREATE TABLE IF NOT EXISTS staging.stg_ratio (
    ticker          VARCHAR(10)  NOT NULL,
    year            INT          NOT NULL,
    quarter         INT          NOT NULL,
    metric          VARCHAR(30)  NOT NULL,
    value           DOUBLE PRECISION,
    extracted_at    TIMESTAMP    DEFAULT now()
);

-- ---------------------------------------------------------------------
-- DIMENSIONS
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS dw.dim_date (
    date_key        INT          PRIMARY KEY,          -- YYYYMMDD
    full_date       DATE         NOT NULL UNIQUE,
    day_of_month    SMALLINT     NOT NULL,
    day_of_week     SMALLINT     NOT NULL,             -- 1 = Thứ Hai ... 7 = Chủ Nhật (ISO)
    day_name_vi     VARCHAR(10)  NOT NULL,
    week_of_year    SMALLINT     NOT NULL,
    month           SMALLINT     NOT NULL,
    quarter         SMALLINT     NOT NULL,
    year            SMALLINT     NOT NULL,
    year_month      CHAR(7)      NOT NULL,             -- 2024-03
    year_quarter    CHAR(7)      NOT NULL,             -- 2024-Q1
    is_weekend      BOOLEAN      NOT NULL,
    is_quarter_end  BOOLEAN      NOT NULL,
    is_trading_day  BOOLEAN      NOT NULL DEFAULT FALSE -- ETL cập nhật khi có phiên giao dịch
);

-- Sinh sẵn lịch 2015 – 2030
INSERT INTO dw.dim_date
SELECT  to_char(d, 'YYYYMMDD')::INT,
        d::DATE,
        EXTRACT(DAY FROM d),
        EXTRACT(ISODOW FROM d),
        (ARRAY['Thứ Hai','Thứ Ba','Thứ Tư','Thứ Năm','Thứ Sáu','Thứ Bảy','Chủ Nhật'])[EXTRACT(ISODOW FROM d)],
        EXTRACT(WEEK FROM d),
        EXTRACT(MONTH FROM d),
        EXTRACT(QUARTER FROM d),
        EXTRACT(YEAR FROM d),
        to_char(d, 'YYYY-MM'),
        to_char(d, 'YYYY') || '-Q' || EXTRACT(QUARTER FROM d),
        EXTRACT(ISODOW FROM d) IN (6, 7),
        d::DATE = (date_trunc('quarter', d) + INTERVAL '3 months - 1 day')::DATE,
        FALSE
FROM generate_series('2015-01-01'::DATE, '2030-12-31'::DATE, INTERVAL '1 day') AS d
ON CONFLICT (date_key) DO NOTHING;

CREATE TABLE IF NOT EXISTS dw.dim_exchange (
    exchange_key    SERIAL       PRIMARY KEY,
    exchange_code   VARCHAR(10)  NOT NULL UNIQUE,
    exchange_name   TEXT         NOT NULL,
    price_limit_pct NUMERIC(5,2)                       -- biên độ dao động giá
);

INSERT INTO dw.dim_exchange (exchange_code, exchange_name, price_limit_pct) VALUES
    ('HOSE',    'Sở Giao dịch Chứng khoán TP.HCM', 7.00),
    ('HNX',     'Sở Giao dịch Chứng khoán Hà Nội', 10.00),
    ('UPCOM',   'Thị trường UPCoM',                15.00),
    ('UNKNOWN', 'Chưa xác định',                   NULL)
ON CONFLICT (exchange_code) DO NOTHING;

-- Phân cấp ngành ICB: Cấp 1 (Ngành) -> Cấp 2 (Siêu ngành) -> Cấp 3 (Ngành con)
CREATE TABLE IF NOT EXISTS dw.dim_industry (
    industry_key    SERIAL       PRIMARY KEY,
    icb_code_l3     VARCHAR(10)  NOT NULL UNIQUE,      -- khóa tự nhiên (cấp chi tiết nhất)
    icb_name_l3     TEXT,
    icb_code_l2     VARCHAR(10),
    icb_name_l2     TEXT,
    icb_code_l1     VARCHAR(10),
    icb_name_l1     TEXT
);

INSERT INTO dw.dim_industry (icb_code_l3, icb_name_l3, icb_code_l2, icb_name_l2, icb_code_l1, icb_name_l1)
VALUES ('UNKNOWN', 'Chưa phân ngành', 'UNKNOWN', 'Chưa phân ngành', 'UNKNOWN', 'Chưa phân ngành')
ON CONFLICT (icb_code_l3) DO NOTHING;

-- Dim_Stock: Slowly Changing Dimension Type 2
CREATE TABLE IF NOT EXISTS dw.dim_stock (
    stock_key       SERIAL       PRIMARY KEY,          -- surrogate key
    ticker          VARCHAR(10)  NOT NULL,             -- natural / business key
    company_name    TEXT,
    industry_key    INT          NOT NULL REFERENCES dw.dim_industry(industry_key),
    exchange_key    INT          NOT NULL REFERENCES dw.dim_exchange(exchange_key),
    valid_from      DATE         NOT NULL,
    valid_to        DATE         NOT NULL DEFAULT '9999-12-31',
    is_current      BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT ck_valid_range CHECK (valid_from <= valid_to)
);
-- Mỗi mã chỉ có đúng 1 phiên bản "hiện hành"
CREATE UNIQUE INDEX IF NOT EXISTS ux_dim_stock_current ON dw.dim_stock (ticker) WHERE is_current;
CREATE INDEX IF NOT EXISTS ix_dim_stock_ticker_range ON dw.dim_stock (ticker, valid_from, valid_to);

-- ---------------------------------------------------------------------
-- FACTS
-- ---------------------------------------------------------------------
-- Grain: 1 mã cổ phiếu × 1 phiên giao dịch. Phân vùng (partition) theo năm.
CREATE TABLE IF NOT EXISTS dw.fact_daily_price (
    date_key        INT          NOT NULL REFERENCES dw.dim_date(date_key),
    stock_key       INT          NOT NULL REFERENCES dw.dim_stock(stock_key),
    open            NUMERIC(14,3),
    high            NUMERIC(14,3),
    low             NUMERIC(14,3),
    close           NUMERIC(14,3) NOT NULL,            -- đơn vị: nghìn VND
    volume          BIGINT,
    trading_value   NUMERIC(20,0),                     -- VND = close * 1000 * volume
    daily_return    DOUBLE PRECISION,                  -- close_t / close_{t-1} - 1
    log_return      DOUBLE PRECISION,
    PRIMARY KEY (date_key, stock_key)
) PARTITION BY RANGE (date_key);

DO $$
DECLARE y INT;
BEGIN
    FOR y IN 2015..2030 LOOP
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS dw.fact_daily_price_%s PARTITION OF dw.fact_daily_price
             FOR VALUES FROM (%s) TO (%s)', y, y * 10000 + 101, (y + 1) * 10000 + 101);
    END LOOP;
END $$;

CREATE INDEX IF NOT EXISTS ix_fact_price_stock ON dw.fact_daily_price (stock_key, date_key);

-- Grain: 1 chỉ số thị trường × 1 phiên (VNINDEX, VN30) – dùng để tính beta
CREATE TABLE IF NOT EXISTS dw.fact_index_daily (
    date_key        INT          NOT NULL REFERENCES dw.dim_date(date_key),
    index_code      VARCHAR(20)  NOT NULL,             -- degenerate dimension
    open            NUMERIC(14,3),
    high            NUMERIC(14,3),
    low             NUMERIC(14,3),
    close           NUMERIC(14,3) NOT NULL,
    volume          BIGINT,
    daily_return    DOUBLE PRECISION,
    PRIMARY KEY (date_key, index_code)
);

-- Grain: 1 mã × 1 quý. date_key = ngày cuối quý.
CREATE TABLE IF NOT EXISTS dw.fact_financial_ratio (
    date_key        INT          NOT NULL REFERENCES dw.dim_date(date_key),
    stock_key       INT          NOT NULL REFERENCES dw.dim_stock(stock_key),
    pe              DOUBLE PRECISION,
    pb              DOUBLE PRECISION,
    roe             DOUBLE PRECISION,                  -- tỷ lệ, 0.18 = 18%
    roa             DOUBLE PRECISION,
    eps             DOUBLE PRECISION,                  -- VND
    debt_to_equity  DOUBLE PRECISION,
    net_margin      DOUBLE PRECISION,
    PRIMARY KEY (date_key, stock_key)
);

-- Kết quả khai phá dữ liệu ghi ngược vào kho để làm dashboard
CREATE TABLE IF NOT EXISTS dw.mining_cluster_result (
    run_id          VARCHAR(20)  NOT NULL,
    ticker          VARCHAR(10)  NOT NULL,
    method          VARCHAR(20)  NOT NULL,             -- kmeans / hierarchical
    cluster_id      INT          NOT NULL,
    cluster_label   TEXT,
    PRIMARY KEY (run_id, ticker, method)
);

-- ---------------------------------------------------------------------
-- META
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS meta.etl_log (
    log_id          SERIAL       PRIMARY KEY,
    run_id          VARCHAR(20)  NOT NULL,
    step            TEXT         NOT NULL,
    rows_affected   INT,
    status          VARCHAR(10)  NOT NULL,             -- OK / WARN / FAIL
    message         TEXT,
    logged_at       TIMESTAMP    DEFAULT now()
);

CREATE TABLE IF NOT EXISTS meta.dq_issue (
    issue_id        SERIAL       PRIMARY KEY,
    run_id          VARCHAR(20)  NOT NULL,
    ticker          VARCHAR(10),
    trade_date      DATE,
    rule_name       TEXT         NOT NULL,
    detail          TEXT,
    action          VARCHAR(20)  NOT NULL,             -- REJECTED / FLAGGED
    logged_at       TIMESTAMP    DEFAULT now()
);

-- ---------------------------------------------------------------------
-- VIEW tiện dụng cho Power BI / truy vấn OLAP
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW dw.v_price_enriched AS
SELECT  d.full_date, d.year, d.quarter, d.year_quarter, d.month, d.year_month,
        s.ticker, s.company_name,
        i.icb_name_l1, i.icb_name_l2, i.icb_name_l3,
        e.exchange_code,
        f.open, f.high, f.low, f.close, f.volume, f.trading_value,
        f.daily_return, f.log_return
FROM dw.fact_daily_price f
JOIN dw.dim_date     d ON d.date_key     = f.date_key
JOIN dw.dim_stock    s ON s.stock_key    = f.stock_key
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
JOIN dw.dim_exchange e ON e.exchange_key = s.exchange_key;
