"""Bước TRANSFORM phía Python: làm sạch dữ liệu trước khi đưa vào staging.
Các phép biến đổi cần lịch sử (lợi nhuận ngày, cờ vượt biên độ) làm bằng SQL trong load.py."""
import pandas as pd

EXCHANGE_MAP = {"HSX": "HOSE", "HOSE": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"}


def clean_listing(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["ticker"] = df["ticker"].str.strip().str.upper()
    df["exchange_code"] = (
        df.get("exchange_code", pd.Series(index=df.index, dtype=object))
        .astype("string").str.upper().map(lambda v: EXCHANGE_MAP.get(v, "UNKNOWN") if pd.notna(v) else "UNKNOWN")
    )
    for lvl in (1, 2, 3):
        for kind in ("code", "name"):
            col = f"icb_{kind}_l{lvl}"
            if col not in df.columns:
                df[col] = None
    # Mã thiếu ngành cấp 3 -> dùng cấp thấp hơn có sẵn, nếu không có thì UNKNOWN
    df["icb_code_l3"] = df["icb_code_l3"].fillna(df["icb_code_l2"]).fillna("UNKNOWN").astype(str)
    df["icb_name_l3"] = df["icb_name_l3"].fillna(df["icb_name_l2"])
    if "company_name" not in df.columns:
        df["company_name"] = None
    cols = ["ticker", "company_name", "exchange_code",
            "icb_code_l1", "icb_name_l1", "icb_code_l2", "icb_name_l2", "icb_code_l3", "icb_name_l3"]
    return df[cols].drop_duplicates("ticker")


def clean_prices(df: pd.DataFrame, key_col: str = "ticker") -> tuple[pd.DataFrame, pd.DataFrame]:
    """Trả về (dữ liệu sạch, danh sách lỗi). Lỗi được ghi vào meta.dq_issue."""
    issues = []
    if df.empty:
        return df, pd.DataFrame(columns=["ticker", "trade_date", "rule_name", "detail", "action"])
    df = df.copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for c in ["open", "high", "low", "close"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")

    def reject(mask, rule, detail_fn):
        for _, r in df[mask].iterrows():
            issues.append((r[key_col], r["trade_date"], rule, detail_fn(r), "REJECTED"))
        return df[~mask]

    # R1: trùng (mã, ngày) -> giữ bản ghi cuối
    dup = df.duplicated([key_col, "trade_date"], keep="last")
    df = reject(dup, "duplicate_row", lambda r: "Trùng mã + ngày, giữ bản ghi cuối")
    # R2: thiếu giá đóng cửa hoặc giá <= 0
    bad = df["close"].isna() | (df["close"] <= 0)
    df = reject(bad, "invalid_close", lambda r: f"close={r['close']}")
    # R3: high < low
    bad = df["high"] < df["low"]
    df = reject(bad, "high_lt_low", lambda r: f"high={r['high']} < low={r['low']}")
    # R4: close nằm ngoài [low, high] (dung sai 0.5%)
    bad = (df["close"] > df["high"] * 1.005) | (df["close"] < df["low"] * 0.995)
    df = reject(bad, "close_out_of_range", lambda r: f"close={r['close']} ngoài [{r['low']}, {r['high']}]")
    # R5: ngày cuối tuần
    bad = pd.to_datetime(df["trade_date"]).dt.dayofweek >= 5
    df = reject(bad, "weekend_date", lambda r: "Giao dịch rơi vào cuối tuần")
    # R6: khối lượng âm
    bad = df["volume"] < 0
    df = reject(bad, "negative_volume", lambda r: f"volume={r['volume']}")

    issues_df = pd.DataFrame(issues, columns=["ticker", "trade_date", "rule_name", "detail", "action"])
    return df.reset_index(drop=True), issues_df


def clean_ratios(df: pd.DataFrame) -> pd.DataFrame:
    """Chuẩn hóa đơn vị: ROE/ROA/net_margin về dạng tỷ lệ (0.18 thay vì 18)."""
    if df.empty:
        return df
    df = df.copy()
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["value"])
    for metric in ("roe", "roa", "net_margin"):
        m = df["metric"] == metric
        # Nếu trung vị |giá trị| > 1.5 thì nguồn đang trả về %, chia 100
        per_ticker_median = df[m].groupby("ticker")["value"].transform(lambda s: s.abs().median())
        scale = (per_ticker_median > 1.5).map({True: 100.0, False: 1.0})
        df.loc[m, "value"] = df.loc[m, "value"] / scale
    return df.drop_duplicates(["ticker", "year", "quarter", "metric"], keep="last")
