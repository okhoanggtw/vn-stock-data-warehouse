"""
Điều phối ETL: Extract (vnstock / mock) -> Transform (pandas) -> Staging -> Load (SQL) -> DW

Ví dụ:
    python -m etl.run_etl --init-db                 # tạo schema lần đầu
    python -m etl.run_etl                           # nạp tăng dần nhóm VN100 từ vnstock
    python -m etl.run_etl --tickers FPT,VCB,HPG     # chỉ vài mã
    python -m etl.run_etl --mock --init-db          # chạy thử bằng dữ liệu giả lập
"""
import argparse
import datetime as dt
import logging
import sys

import pandas as pd
from sqlalchemy import create_engine, text

from etl import config
from etl.load import run_load
from etl.transform import clean_listing, clean_prices, clean_ratios

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("etl")


def parse_args():
    p = argparse.ArgumentParser(description="ETL kho dữ liệu chứng khoán Việt Nam")
    p.add_argument("--mock", action="store_true", help="dùng dữ liệu giả lập thay cho vnstock")
    p.add_argument("--mock-change", action="store_true", help="(mock) giả lập 1 mã chuyển sàn để kiểm thử SCD2")
    p.add_argument("--init-db", action="store_true", help="chạy sql/01_create_schema.sql trước khi nạp")
    p.add_argument("--group", default=config.UNIVERSE_GROUP, help="nhóm mã, vd VN30, VN100")
    p.add_argument("--tickers", help="danh sách mã, cách nhau bởi dấu phẩy (ghi đè --group)")
    p.add_argument("--start", default=config.START_DATE)
    p.add_argument("--end", default=config.END_DATE)
    p.add_argument("--full-refresh", action="store_true", help="bỏ qua watermark, tải lại toàn bộ khoảng ngày")
    p.add_argument("--skip-ratios", action="store_true", help="không tải chỉ số tài chính")
    p.add_argument("--effective-date", default=dt.date.today().isoformat(),
                   help="ngày hiệu lực cho bản ghi SCD2 mới (mặc định hôm nay)")
    return p.parse_args()


def get_source(args):
    if args.mock:
        from etl.sources.mock_source import MockSource
        return MockSource(change_scd=args.mock_change)
    from etl.sources.vnstock_source import VnstockSource
    return VnstockSource()


def watermarks(conn) -> dict:
    """Ngày giao dịch mới nhất đã có trong kho cho từng mã / chỉ số (phục vụ incremental load)."""
    q = """
    SELECT s.ticker AS k, MAX(d.full_date) AS last_date
    FROM dw.fact_daily_price f JOIN dw.dim_stock s USING (stock_key) JOIN dw.dim_date d USING (date_key)
    GROUP BY s.ticker
    UNION ALL
    SELECT index_code, MAX(d.full_date) FROM dw.fact_index_daily f JOIN dw.dim_date d USING (date_key)
    GROUP BY index_code
    """
    return {r.k: r.last_date for r in conn.execute(text(q))}


def main():
    args = parse_args()
    run_id = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    engine = create_engine(config.DB_URL, future=True)
    source = get_source(args)
    log.info("RUN %s | nguồn=%s | %s -> %s", run_id, source.name, args.start, args.end)

    def log_step(conn, step, rows, status="OK", message=None):
        log.info("  %-28s %8s dòng  %s", step, rows, status)
        conn.execute(text("INSERT INTO meta.etl_log (run_id, step, rows_affected, status, message) "
                          "VALUES (:r, :s, :n, :st, :m)"),
                     {"r": run_id, "s": step, "n": int(rows), "st": status, "m": message})

    if args.init_db:
        sql = (config.ROOT / "sql" / "01_create_schema.sql").read_text(encoding="utf-8")
        raw = engine.raw_connection()  # chạy nguyên file, không qua bước bind tham số
        try:
            with raw.cursor() as cur:
                cur.execute(sql)
            raw.commit()
        finally:
            raw.close()
        log.info("Đã tạo schema")

    # ---------------- EXTRACT ----------------
    tickers = ([t.strip().upper() for t in args.tickers.split(",")] if args.tickers
               else source.get_universe(args.group))
    log.info("Số mã: %d", len(tickers))

    with engine.connect() as conn:
        wm = {} if args.full_refresh else watermarks(conn)

    end = pd.Timestamp(args.end).date()

    def start_for(key):
        last = wm.get(key)
        return (last + dt.timedelta(days=1)) if last else pd.Timestamp(args.start).date()

    listing = source.get_listing(tickers)

    price_frames = []
    for i, t in enumerate(tickers, 1):
        s = start_for(t)
        if s > end:
            continue
        df = source.get_prices(t, s.isoformat(), end.isoformat())
        price_frames.append(df)
        if i % 10 == 0 or i == len(tickers):
            log.info("  giá: %d/%d mã", i, len(tickers))
    prices_raw = pd.concat(price_frames, ignore_index=True) if price_frames else pd.DataFrame()

    index_frames = []
    for code in config.INDEX_CODES:
        s = start_for(code)
        if s <= end:
            index_frames.append(source.get_index(code, s.isoformat(), end.isoformat()))
    index_raw = pd.concat(index_frames, ignore_index=True) if index_frames else pd.DataFrame()

    ratios_raw = pd.DataFrame()
    if not args.skip_ratios:
        ratios_raw = pd.concat([source.get_ratios(t) for t in tickers], ignore_index=True)

    # Lưu bản thô để có thể nạp lại không cần gọi API
    raw_dir = config.RAW_DIR / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)
    for name, df in [("listing", listing), ("prices", prices_raw), ("index", index_raw), ("ratios", ratios_raw)]:
        df.to_csv(raw_dir / f"{name}.csv", index=False)

    # ---------------- TRANSFORM ----------------
    listing_c = clean_listing(listing)
    prices_c, price_issues = clean_prices(prices_raw, "ticker") if not prices_raw.empty else (prices_raw, pd.DataFrame())
    index_c, _ = clean_prices(index_raw, "index_code") if not index_raw.empty else (index_raw, None)
    ratios_c = clean_ratios(ratios_raw)

    # ---------------- STAGING + LOAD ----------------
    with engine.begin() as conn:
        conn.execute(text("TRUNCATE staging.stg_listing, staging.stg_price, staging.stg_index, staging.stg_ratio"))
        listing_c.to_sql("stg_listing", conn, schema="staging", if_exists="append", index=False)
        if not prices_c.empty:
            prices_c.to_sql("stg_price", conn, schema="staging", if_exists="append", index=False,
                            chunksize=5000, method="multi")
        if not index_c.empty:
            index_c.to_sql("stg_index", conn, schema="staging", if_exists="append", index=False, method="multi")
        if not ratios_c.empty:
            ratios_c.to_sql("stg_ratio", conn, schema="staging", if_exists="append", index=False,
                            chunksize=5000, method="multi")
        log_step(conn, "extract_listing", len(listing_c))
        log_step(conn, "extract_price", len(prices_c))
        log_step(conn, "extract_index", len(index_c))
        log_step(conn, "extract_ratio", len(ratios_c))

        if price_issues is not None and not price_issues.empty:
            price_issues.assign(run_id=run_id).to_sql("dq_issue", conn, schema="meta", if_exists="append", index=False)
            log_step(conn, "dq_rejected_rows", len(price_issues), "WARN")

        run_load(conn, run_id, args.effective_date, log_step)

    with engine.connect() as conn:
        n = conn.execute(text("SELECT COUNT(*) FROM dw.fact_daily_price")).scalar()
        dq = conn.execute(text("SELECT rule_name, COUNT(*) FROM meta.dq_issue WHERE run_id = :r GROUP BY 1"),
                          {"r": run_id}).all()
    log.info("XONG. fact_daily_price hiện có %s dòng. Lỗi DQ lần chạy này: %s", f"{n:,}", dict(dq) or "không có")


if __name__ == "__main__":
    sys.exit(main())
