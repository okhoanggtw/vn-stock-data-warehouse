"""Bước LOAD: staging -> kho dữ liệu. Toàn bộ logic nạp viết bằng SQL để dễ đưa vào báo cáo."""
from sqlalchemy import text

# 1. Chiều ngành: thêm ngành mới, cập nhật tên (SCD Type 1)
UPSERT_INDUSTRY = """
INSERT INTO dw.dim_industry (icb_code_l3, icb_name_l3, icb_code_l2, icb_name_l2, icb_code_l1, icb_name_l1)
SELECT DISTINCT ON (icb_code_l3)
       icb_code_l3, icb_name_l3, icb_code_l2, icb_name_l2, icb_code_l1, icb_name_l1
FROM staging.stg_listing
WHERE icb_code_l3 IS NOT NULL
ORDER BY icb_code_l3
ON CONFLICT (icb_code_l3) DO UPDATE
SET icb_name_l3 = EXCLUDED.icb_name_l3, icb_code_l2 = EXCLUDED.icb_code_l2, icb_name_l2 = EXCLUDED.icb_name_l2,
    icb_code_l1 = EXCLUDED.icb_code_l1, icb_name_l1 = EXCLUDED.icb_name_l1;
"""

# 2. Chiều cổ phiếu
BUILD_SRC = """
DROP TABLE IF EXISTS tmp_stock_src;
CREATE TEMP TABLE tmp_stock_src AS
SELECT l.ticker, l.company_name,
       COALESCE(i.industry_key, (SELECT industry_key FROM dw.dim_industry WHERE icb_code_l3 = 'UNKNOWN')) AS industry_key,
       COALESCE(e.exchange_key, (SELECT exchange_key FROM dw.dim_exchange WHERE exchange_code = 'UNKNOWN')) AS exchange_key
FROM staging.stg_listing l
LEFT JOIN dw.dim_industry i ON i.icb_code_l3 = l.icb_code_l3
LEFT JOIN dw.dim_exchange e ON e.exchange_code = l.exchange_code;
"""

# 2a. SCD Type 1: tên công ty đổi -> ghi đè trên bản ghi hiện hành
SCD1_NAME = """
UPDATE dw.dim_stock d SET company_name = s.company_name
FROM tmp_stock_src s
WHERE d.ticker = s.ticker AND d.is_current AND d.company_name IS DISTINCT FROM s.company_name;
"""

# 2b. SCD Type 2: đổi ngành hoặc đổi sàn -> đóng bản ghi cũ ...
SCD2_EXPIRE = """
UPDATE dw.dim_stock d
SET valid_to = CAST(:eff AS DATE) - 1, is_current = FALSE
FROM tmp_stock_src s
WHERE d.ticker = s.ticker AND d.is_current
  AND (d.industry_key <> s.industry_key OR d.exchange_key <> s.exchange_key);
"""

# ... và thêm phiên bản mới. Mã mới hoàn toàn thì hiệu lực từ 1900-01-01 để khớp mọi dữ liệu lịch sử.
SCD2_INSERT = """
INSERT INTO dw.dim_stock (ticker, company_name, industry_key, exchange_key, valid_from)
SELECT s.ticker, s.company_name, s.industry_key, s.exchange_key,
       CASE WHEN EXISTS (SELECT 1 FROM dw.dim_stock x WHERE x.ticker = s.ticker)
            THEN CAST(:eff AS DATE) ELSE DATE '1900-01-01' END
FROM tmp_stock_src s
WHERE NOT EXISTS (SELECT 1 FROM dw.dim_stock d WHERE d.ticker = s.ticker AND d.is_current);
"""

# 3. Đánh dấu ngày giao dịch
MARK_TRADING_DAYS = """
UPDATE dw.dim_date SET is_trading_day = TRUE
WHERE NOT is_trading_day
  AND full_date IN (SELECT trade_date FROM staging.stg_price UNION SELECT trade_date FROM staging.stg_index);
"""

# 4. Fact giá: tra surrogate key theo khoảng hiệu lực SCD2. Idempotent nhờ ON CONFLICT.
LOAD_FACT_PRICE = """
INSERT INTO dw.fact_daily_price (date_key, stock_key, open, high, low, close, volume, trading_value)
SELECT CAST(to_char(p.trade_date, 'YYYYMMDD') AS INT), s.stock_key,
       p.open, p.high, p.low, p.close, p.volume, ROUND(p.close * 1000 * p.volume)
FROM staging.stg_price p
JOIN dw.dim_stock s ON s.ticker = p.ticker AND p.trade_date BETWEEN s.valid_from AND s.valid_to
ON CONFLICT (date_key, stock_key) DO UPDATE
SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
    volume = EXCLUDED.volume, trading_value = EXCLUDED.trading_value,
    daily_return = NULL, log_return = NULL;
"""

# 5. Lợi nhuận ngày = close_t / close_{t-1} - 1, tính theo ticker (xuyên qua các phiên bản SCD2)
COMPUTE_RETURNS = """
WITH r AS (
    SELECT f.date_key, f.stock_key,
           f.close / NULLIF(LAG(f.close) OVER (PARTITION BY s.ticker ORDER BY f.date_key), 0) - 1 AS ret
    FROM dw.fact_daily_price f
    JOIN dw.dim_stock s ON s.stock_key = f.stock_key
    WHERE s.ticker IN (SELECT DISTINCT ticker FROM staging.stg_price)
)
UPDATE dw.fact_daily_price f
SET daily_return = r.ret, log_return = LN(1 + r.ret)
FROM r
WHERE f.date_key = r.date_key AND f.stock_key = r.stock_key
  AND f.daily_return IS NULL AND r.ret IS NOT NULL AND r.ret > -1;
"""

# 6. Kiểm tra chất lượng sau nạp: |lợi nhuận| vượt biên độ sàn -> nghi chia tách/thưởng chưa điều chỉnh giá
DQ_PRICE_LIMIT = """
INSERT INTO meta.dq_issue (run_id, ticker, trade_date, rule_name, detail, action)
SELECT :run_id, s.ticker, d.full_date, 'return_exceeds_price_limit',
       format('return=%s%%, biên độ sàn %s=%s%%', ROUND(CAST(f.daily_return * 100 AS NUMERIC), 2),
              e.exchange_code, e.price_limit_pct),
       'FLAGGED'
FROM dw.fact_daily_price f
JOIN dw.dim_stock s    ON s.stock_key = f.stock_key
JOIN dw.dim_exchange e ON e.exchange_key = s.exchange_key
JOIN dw.dim_date d     ON d.date_key = f.date_key
JOIN staging.stg_price p ON p.ticker = s.ticker AND p.trade_date = d.full_date
WHERE e.price_limit_pct IS NOT NULL
  AND ABS(f.daily_return) * 100 > e.price_limit_pct + 0.5;
"""

# 7. Fact chỉ số thị trường
LOAD_FACT_INDEX = """
INSERT INTO dw.fact_index_daily (date_key, index_code, open, high, low, close, volume)
SELECT CAST(to_char(trade_date, 'YYYYMMDD') AS INT), index_code, open, high, low, close, volume
FROM staging.stg_index
ON CONFLICT (date_key, index_code) DO UPDATE
SET open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close,
    volume = EXCLUDED.volume, daily_return = NULL;

WITH r AS (
    SELECT date_key, index_code,
           close / NULLIF(LAG(close) OVER (PARTITION BY index_code ORDER BY date_key), 0) - 1 AS ret
    FROM dw.fact_index_daily
)
UPDATE dw.fact_index_daily f SET daily_return = r.ret
FROM r
WHERE f.date_key = r.date_key AND f.index_code = r.index_code
  AND f.daily_return IS NULL AND r.ret IS NOT NULL;
"""

# 8. Fact chỉ số tài chính: xoay (pivot) từ dạng dài sang dạng rộng
LOAD_FACT_RATIO = """
INSERT INTO dw.fact_financial_ratio (date_key, stock_key, pe, pb, roe, roa, eps, debt_to_equity, net_margin)
SELECT d.date_key, s.stock_key,
       MAX(r.value) FILTER (WHERE r.metric = 'pe'),
       MAX(r.value) FILTER (WHERE r.metric = 'pb'),
       MAX(r.value) FILTER (WHERE r.metric = 'roe'),
       MAX(r.value) FILTER (WHERE r.metric = 'roa'),
       MAX(r.value) FILTER (WHERE r.metric = 'eps'),
       MAX(r.value) FILTER (WHERE r.metric = 'debt_to_equity'),
       MAX(r.value) FILTER (WHERE r.metric = 'net_margin')
FROM staging.stg_ratio r
JOIN dw.dim_date d  ON d.year = r.year AND d.quarter = r.quarter AND d.is_quarter_end
JOIN dw.dim_stock s ON s.ticker = r.ticker AND d.full_date BETWEEN s.valid_from AND s.valid_to
GROUP BY d.date_key, s.stock_key
ON CONFLICT (date_key, stock_key) DO UPDATE
SET pe = EXCLUDED.pe, pb = EXCLUDED.pb, roe = EXCLUDED.roe, roa = EXCLUDED.roa, eps = EXCLUDED.eps,
    debt_to_equity = EXCLUDED.debt_to_equity, net_margin = EXCLUDED.net_margin;
"""

STEPS = [
    ("upsert_dim_industry", UPSERT_INDUSTRY),
    ("build_stock_source", BUILD_SRC),
    ("scd1_company_name", SCD1_NAME),
    ("scd2_expire", SCD2_EXPIRE),
    ("scd2_insert", SCD2_INSERT),
    ("mark_trading_days", MARK_TRADING_DAYS),
    ("load_fact_daily_price", LOAD_FACT_PRICE),
    ("compute_returns", COMPUTE_RETURNS),
    ("dq_price_limit", DQ_PRICE_LIMIT),
    ("load_fact_index_daily", LOAD_FACT_INDEX),
    ("load_fact_financial_ratio", LOAD_FACT_RATIO),
]


def run_load(conn, run_id: str, effective_date: str, log_fn):
    params = {"run_id": run_id, "eff": effective_date}
    for name, sql in STEPS:
        total = 0
        for stmt in [s for s in sql.split(";") if s.strip()]:
            res = conn.execute(text(stmt), params)
            total += max(res.rowcount or 0, 0)
        log_fn(conn, name, total)
