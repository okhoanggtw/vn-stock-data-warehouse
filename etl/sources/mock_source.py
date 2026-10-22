"""
Nguồn dữ liệu GIẢ LẬP – chỉ để chạy thử pipeline khi chưa có mạng / chưa cài vnstock.
Mã cổ phiếu bắt đầu bằng chữ Z (ZNH1, ZCK2, ...) để không nhầm với dữ liệu thật.
TUYỆT ĐỐI không dùng kết quả từ dữ liệu này để viết nhận xét trong báo cáo.

Mô hình sinh giá: r = beta_ngành * r_thịtrường + r_ngành + nhiễu riêng, cắt ở biên độ ±6.9%.
Cố tình chèn một số lỗi dữ liệu để minh họa bước kiểm tra chất lượng (DQ):
  - 1 dòng trùng lặp, 1 dòng high < low, 1 lần chia tách cổ phiếu chưa điều chỉnh giá.
"""
import zlib

import numpy as np
import pandas as pd

SECTORS = {
    #  mã   tên L3/L2/L1, mã ICB L3/L2/L1,                beta, drift/ngày, vol ngành, vol riêng, PE, PB, ROE%
    "NH": ("Ngân hàng", "Ngân hàng", "Tài chính", "8355", "8300", "8000", 1.00, 0.0003, 0.006, 0.010, 8, 1.4, 18),
    "CK": ("Môi giới chứng khoán", "Dịch vụ tài chính", "Tài chính", "8777", "8700", "8000", 1.60, 0.0002, 0.012, 0.014, 14, 1.6, 12),
    "BD": ("Phát triển bất động sản", "Bất động sản", "Tài chính", "8633", "8600", "8000", 1.35, -0.0002, 0.011, 0.016, 20, 1.3, 7),
    "TH": ("Thép", "Tài nguyên cơ bản", "Nguyên vật liệu", "1757", "1700", "1000", 1.30, 0.0001, 0.010, 0.013, 11, 1.2, 10),
    "BL": ("Bán lẻ", "Bán lẻ", "Dịch vụ tiêu dùng", "5373", "5300", "5000", 1.05, 0.0004, 0.007, 0.012, 22, 3.5, 20),
    "DI": ("Sản xuất & Phân phối điện", "Điện, nước & xăng dầu khí đốt", "Tiện ích cộng đồng", "7535", "7500", "7000", 0.50, 0.0002, 0.005, 0.009, 12, 1.5, 13),
}
N_PER_SECTOR = 6
HNX_TICKERS = {"ZCK5", "ZBD3", "ZTH6"}


def _trading_days(start: str, end: str) -> pd.DatetimeIndex:
    days = pd.bdate_range(start, end)
    hol = set()
    for y in range(days.min().year, days.max().year + 1):
        hol |= {f"{y}-01-01", f"{y}-04-30", f"{y}-05-01", f"{y}-09-02"}
        tet = pd.Timestamp(f"{y}-02-01") - pd.Timedelta(days=10)  # Tết xấp xỉ
        hol |= {str((tet + pd.Timedelta(days=i)).date()) for i in range(7)}
    return days[~days.strftime("%Y-%m-%d").isin(hol)]


class MockSource:
    name = "mock"

    def __init__(self, change_scd: bool = False, seed: int = 42):
        self.change_scd = change_scd
        self.seed = seed
        self._cache = None

    # ------------------------------------------------------------------
    def _simulate(self):
        if self._cache is not None:
            return self._cache
        rng = np.random.default_rng(self.seed)
        days = _trading_days("2019-01-02", "2026-09-30")
        n = len(days)
        # Thị trường: tăng 2020-2021, giảm mạnh 2022, hồi phục 2023+
        drift = np.where(days.year == 2022, -0.0016, np.where(days.year == 2021, 0.0012, 0.0004))
        m = drift + rng.normal(0, 0.011, n)
        vni = 960 * np.exp(np.cumsum(m))

        prices = {}
        for sc, (*_, beta, mu, sv, iv, _pe, _pb, _roe) in SECTORS.items():
            s = rng.normal(mu, sv, n)
            for k in range(1, N_PER_SECTOR + 1):
                t = f"Z{sc}{k}"
                b = beta * rng.uniform(0.85, 1.15)
                r = np.clip(b * m + s + rng.normal(0, iv, n), -0.069, 0.069)
                close = rng.uniform(12, 80) * np.exp(np.cumsum(np.log1p(r)))
                prices[t] = (close, sc)
        self._cache = (days, vni, m, prices)
        return self._cache

    # ------------------------------------------------------------------
    def get_universe(self, group: str) -> list[str]:
        return [f"Z{sc}{k}" for sc in SECTORS for k in range(1, N_PER_SECTOR + 1)]

    def get_listing(self, tickers: list[str]) -> pd.DataFrame:
        rows = []
        for t in tickers:
            sc = t[1:3]
            n3, n2, n1, c3, c2, c1, *_ = SECTORS[sc]
            exch = "HNX" if t in HNX_TICKERS else "HOSE"
            if self.change_scd and t == "ZBD3":  # minh họa SCD2: chuyển sàn HNX -> HOSE
                exch = "HSX"
            rows.append(dict(ticker=t, company_name=f"CTCP {n3} giả lập {t[-1]}", exchange_code=exch,
                             icb_code_l1=c1, icb_name_l1=n1, icb_code_l2=c2, icb_name_l2=n2,
                             icb_code_l3=c3, icb_name_l3=n3))
        return pd.DataFrame(rows)

    def get_prices(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        days, _, _, prices = self._simulate()
        close, sc = prices[ticker]
        rng = np.random.default_rng(zlib.crc32(ticker.encode()))
        df = pd.DataFrame({"trade_date": days, "close": close})
        if ticker == "ZCK2":  # chia tách 2:1 ngày 2023-06-15 nhưng nguồn chưa điều chỉnh giá
            df.loc[df["trade_date"] >= "2023-06-15", "close"] /= 2
        spread = np.abs(rng.normal(0, 0.012, len(df)))
        df["open"] = df["close"] * (1 + rng.normal(0, 0.006, len(df)))
        df["high"] = df[["open", "close"]].max(axis=1) * (1 + spread)
        df["low"] = df[["open", "close"]].min(axis=1) * (1 - spread)
        base_vol = {"CK": 6e6, "TH": 5e6, "BD": 4e6, "NH": 3e6, "BL": 1.5e6, "DI": 8e5}[sc]
        df["volume"] = (base_vol * rng.lognormal(0, 0.5, len(df))).round(-2).astype("int64")
        for c in ["open", "high", "low", "close"]:
            df[c] = df[c].round(2)
        df["ticker"] = ticker
        df["trade_date"] = df["trade_date"].dt.date
        mask = (df["trade_date"] >= pd.Timestamp(start).date()) & (df["trade_date"] <= pd.Timestamp(end).date())
        df = df[mask]
        # Lỗi cố ý
        if ticker == "ZNH1" and len(df) > 10:
            df = pd.concat([df, df.iloc[[5]]])                       # trùng lặp
        if ticker == "ZBL2" and len(df) > 20:
            i = df.index[20]
            df.loc[i, ["high", "low"]] = df.loc[i, ["low", "high"]].values  # high < low
        return df[["ticker", "trade_date", "open", "high", "low", "close", "volume"]]

    def get_index(self, code: str, start: str, end: str) -> pd.DataFrame:
        days, vni, m, _ = self._simulate()
        level = vni if code == "VNINDEX" else vni * 1.03 * np.exp(np.cumsum(np.random.default_rng(7).normal(0, 0.002, len(vni))))
        df = pd.DataFrame({"trade_date": days.date, "close": level.round(2)})
        df["open"] = df["close"].shift(1).fillna(df["close"])
        df["high"] = df[["open", "close"]].max(axis=1) * 1.004
        df["low"] = df[["open", "close"]].min(axis=1) * 0.996
        df["volume"] = 600_000_000
        df["index_code"] = code
        mask = (df["trade_date"] >= pd.Timestamp(start).date()) & (df["trade_date"] <= pd.Timestamp(end).date())
        return df[mask][["index_code", "trade_date", "open", "high", "low", "close", "volume"]]

    def get_ratios(self, ticker: str) -> pd.DataFrame:
        sc = ticker[1:3]
        *_, pe, pb, roe = SECTORS[sc]
        rng = np.random.default_rng(zlib.crc32(ticker.encode()))
        f = rng.uniform(0.7, 1.3)
        rows = []
        for y in range(2020, 2026):
            for q in range(1, 5):
                rows += [
                    (ticker, y, q, "pe", pe * f * rng.uniform(0.85, 1.15)),
                    (ticker, y, q, "pb", pb * f * rng.uniform(0.9, 1.1)),
                    (ticker, y, q, "roe", roe / f * rng.uniform(0.85, 1.15)),   # đơn vị % (transform sẽ chuẩn hóa)
                    (ticker, y, q, "roa", roe / f / (8 if sc == "NH" else 2.5)),
                    (ticker, y, q, "eps", 1000 * rng.uniform(1, 5)),
                    (ticker, y, q, "debt_to_equity", (9 if sc == "NH" else 1.2) * rng.uniform(0.7, 1.3)),
                    (ticker, y, q, "net_margin", rng.uniform(5, 25)),
                ]
        return pd.DataFrame(rows, columns=["ticker", "year", "quarter", "metric", "value"])
