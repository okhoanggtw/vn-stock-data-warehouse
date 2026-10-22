"""
Nguồn dữ liệu thật: thư viện vnstock (>= 4.0).

Cài đặt (vnstock không còn phát hành trên PyPI, phải dùng index riêng):
    pip install -U --extra-index-url https://vnstocks.com/api/simple vnstock vnai

Các API dùng ở đây (lớp "adapter" cổ điển, vẫn còn ở vnstock 4.x):
    Quote(symbol, source).history(start, end, interval='1D')   -> time, open, high, low, close, volume
    Listing(source).symbols_by_group('VN100')                  -> Series mã
    Listing(source).symbols_by_industries(lang='vi')           -> symbol, organ_name, icb_level, icb_code, icb_name
    Listing(source).symbols_by_exchange()                      -> symbol, exchange, type, ...
    Finance(symbol, source).ratio(period='quarter', lang='en') -> chỉ tiêu × kỳ (2024-Q1, ...)

Đã test với API thật (vnstock 4.0.9, nguồn VCI):
  - history() trả thêm vài phiên TRƯỚC start -> phải cắt lại theo [start, end].
  - Hạn mức theo cấp tài khoản vnai: Khách 20 lượt/phút & 4 kỳ BCTC; Cộng đồng (API key miễn phí)
    60 lượt/phút & 8 kỳ; bản trả phí không giới hạn kỳ. Vượt hạn mức lượt gọi thì vnai gọi sys.exit()
    -> _call() bắt riêng trường hợp này và chờ hết phút rồi gọi lại.
  - ratio() có lỗi: _get_report cắt N dòng ĐẦU của bảng xếp từ cũ đến mới, nên ra 2018-Q1..Q4 thay vì
    các quý gần nhất. get_ratios() lấy bảng raw rồi tự giữ N quý MỚI NHẤT, với N đúng bằng hạn mức kỳ
    của cấp tài khoản hiện tại (vnai get_max_periods) – không lấy quá phần được phép.
  - Bảng raw: mỗi dòng 1 kỳ (year, quarter, ratioType), quarter=5 + RATIO_YEAR là số liệu cả năm.
    ROE/ROA/biên LN ở dạng tỷ lệ (0.23). Không có EPS -> suy ra EPS = giá / P/E.

Giá trả về tính theo NGHÌN VND (vd 85.5 = 85.500đ).
"""
import logging
import re
import time

import pandas as pd

from etl import config

log = logging.getLogger(__name__)

EXCHANGE_MAP = {"HSX": "HOSE", "HOSE": "HOSE", "HNX": "HNX", "UPCOM": "UPCOM"}

# Cột của bảng ratio raw (VCI) -> cột trong fact_financial_ratio
RAW_RATIO_COLS = {
    "pe": "pe",
    "pb": "pb",
    "roe": "roe",
    "roa": "roa",
    "debtToEquity": "debt_to_equity",
    "afterTaxProfitMargin": "net_margin",
}
# item_id của ratio() công khai -> cột trong fact_financial_ratio
ITEM_ID_MAP = {
    "pe_ratio": "pe",
    "pb_ratio": "pb",
    "roe": "roe",
    "roa": "roa",
    "debt_to_equity": "debt_to_equity",
    "net_margin": "net_margin",
}
RATIO_LIMIT = 1000  # chỉ để sửa lỗi thứ tự của _get_report; số kỳ giữ lại do _max_periods() quyết định
RATE_LIMIT_WAIT_SEC = 61   # vnai đếm lượt gọi theo cửa sổ 60 giây
RATE_LIMIT_MAX_WAITS = 5


def _max_periods() -> int | None:
    """Số kỳ BCTC tối đa được phép theo cấp tài khoản vnai (None = không giới hạn)."""
    try:
        from vnai.beam.patching import get_max_periods
        return get_max_periods()
    except Exception:  # noqa: BLE001 – không xác định được thì dùng mức của tài khoản khách
        return 4

# Dự phòng: ánh xạ theo tên chỉ tiêu (tiếng Anh) khi không có item_id
RATIO_PATTERNS = {
    "pe": r"^p/?e\b|price.?to.?earn",
    "pb": r"^p/?b\b|price.?to.?book",
    "roe": r"\broe\b|return on equity",
    "roa": r"\broa\b|return on (total )?assets",
    "eps": r"\beps\b|earnings? per share",
    "debt_to_equity": r"debt.{0,3}equity",
    "net_margin": r"net (profit )?margin",
}


class VnstockSource:
    name = "vnstock"

    def __init__(self, source: str = config.VNSTOCK_SOURCE):
        try:
            from vnstock import Finance, Listing, Quote  # noqa: F401
        except ImportError as e:
            raise SystemExit(
                "Chưa cài vnstock. Chạy:\n"
                "  pip install -U --extra-index-url https://vnstocks.com/api/simple vnstock vnai\n"
                "hoặc chạy thử với dữ liệu giả lập: python -m etl.run_etl --mock"
            ) from e
        self.source = source
        self._Quote, self._Listing, self._Finance = Quote, Listing, Finance

    # ------------------------------------------------------------------
    def _call(self, fn, what: str):
        """Gọi API có retry + nghỉ giữa các lần gọi (API miễn phí giới hạn số request/phút)."""
        last_err = None
        rate_waits = 0
        attempt = 0
        while attempt < config.MAX_RETRIES:
            attempt += 1
            try:
                result = fn()
                time.sleep(config.REQUEST_SLEEP_SEC)
                return result
            except SystemExit as e:  # vnai thoát chương trình khi vượt hạn mức lượt gọi
                if "rate limit" not in str(e).lower() or rate_waits >= RATE_LIMIT_MAX_WAITS:
                    raise
                rate_waits += 1
                attempt -= 1  # chờ hạn mức không tính là một lần lỗi
                log.warning("%s: vượt hạn mức lượt gọi – chờ %ds (lần %d/%d)",
                            what, RATE_LIMIT_WAIT_SEC, rate_waits, RATE_LIMIT_MAX_WAITS)
                time.sleep(RATE_LIMIT_WAIT_SEC)
            except Exception as e:  # noqa: BLE001
                last_err = e
                wait = config.REQUEST_SLEEP_SEC * 2 ** attempt
                log.warning("%s lỗi lần %d/%d: %s – thử lại sau %.1fs", what, attempt, config.MAX_RETRIES, e, wait)
                time.sleep(wait)
        log.error("%s thất bại: %s", what, last_err)
        return None

    # ------------------------------------------------------------------
    def get_universe(self, group: str) -> list[str]:
        s = self._call(lambda: self._Listing(source=self.source).symbols_by_group(group), f"symbols_by_group({group})")
        if s is None or len(s) == 0:
            raise RuntimeError(f"Không lấy được danh sách mã nhóm {group}")
        return sorted(pd.Series(s).astype(str).str.upper().unique().tolist())

    def get_listing(self, tickers: list[str]) -> pd.DataFrame:
        lst = self._Listing(source=self.source)
        ind = self._call(lambda: lst.symbols_by_industries(lang="vi"), "symbols_by_industries")
        exc = self._call(lambda: lst.symbols_by_exchange(), "symbols_by_exchange")

        out = pd.DataFrame({"ticker": tickers})
        if ind is not None and not ind.empty:
            ind = ind[ind["symbol"].isin(tickers)]
            names = ind.drop_duplicates("symbol").set_index("symbol")["organ_name"]
            wide = ind[ind["icb_level"] <= 3].pivot_table(
                index="symbol", columns="icb_level", values=["icb_code", "icb_name"], aggfunc="first"
            )
            wide.columns = [f"{c}_l{lvl}" for c, lvl in wide.columns]
            out = out.merge(wide, left_on="ticker", right_index=True, how="left")
            out["company_name"] = out["ticker"].map(names)
        if exc is not None and not exc.empty:
            ex = exc.drop_duplicates("symbol").set_index("symbol")["exchange"].astype(str).str.upper()
            out["exchange_code"] = out["ticker"].map(ex).map(lambda v: EXCHANGE_MAP.get(v, v))
        return out

    def get_prices(self, ticker: str, start: str, end: str) -> pd.DataFrame:
        df = self._call(
            lambda: self._Quote(symbol=ticker, source=self.source).history(start=start, end=end, interval="1D"),
            f"history({ticker})",
        )
        return _normalize_ohlcv(df, "ticker", ticker, start, end)

    def get_index(self, code: str, start: str, end: str) -> pd.DataFrame:
        df = self._call(
            lambda: self._Quote(symbol=code, source=self.source).history(start=start, end=end, interval="1D"),
            f"history({code})",
        )
        return _normalize_ohlcv(df, "index_code", code, start, end)

    def get_ratios(self, ticker: str) -> pd.DataFrame:
        def fetch():
            fin = self._Finance(symbol=ticker, source=self.source)
            try:  # bảng raw, sau đó chỉ giữ số kỳ được phép (xem _max_periods)
                return fin.provider._get_report("ratio", period="quarter", mode="raw", limit=RATIO_LIMIT)
            except (AttributeError, TypeError) as e:  # hàm nội bộ đổi chữ ký -> dùng API công khai
                log.warning("Không gọi được _get_report raw (%s) – dùng ratio() công khai", e)
                return fin.ratio(period="quarter", lang="en")

        df = self._call(fetch, f"ratio({ticker})")
        if df is None or df.empty:
            return pd.DataFrame(columns=["ticker", "year", "quarter", "metric", "value"])
        try:
            out = _ratio_to_long(df, ticker)
        except Exception as e:  # noqa: BLE001 – định dạng API có thể đổi
            log.warning("Không parse được ratio của %s (%s) – bỏ qua", ticker, e)
            return pd.DataFrame(columns=["ticker", "year", "quarter", "metric", "value"])
        n = _max_periods()
        if n is not None and not out.empty:  # chỉ giữ n quý mới nhất theo hạn mức tài khoản
            keep = out[["year", "quarter"]].drop_duplicates().sort_values(["year", "quarter"]).tail(n)
            out = out.merge(keep, on=["year", "quarter"])
        return out


# ----------------------------------------------------------------------
def _normalize_ohlcv(df, key_col: str, key: str, start: str, end: str) -> pd.DataFrame:
    cols = [key_col, "trade_date", "open", "high", "low", "close", "volume"]
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=cols)
    df = df.rename(columns={"time": "trade_date"}).copy()
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    # VCI trả thêm vài phiên TRƯỚC start (đã test: start=2024-01-01 -> từ 2023-12-27), cắt lại cho đúng khoảng
    df = df[(df["trade_date"] >= pd.Timestamp(start).date()) & (df["trade_date"] <= pd.Timestamp(end).date())]
    df[key_col] = key
    return df[cols]


def _match_metric(label: str) -> str | None:
    label = str(label).strip().lower()
    for metric, pat in RATIO_PATTERNS.items():
        if re.search(pat, label):
            return metric
    return None


def _ratio_to_long(df: pd.DataFrame, ticker: str) -> pd.DataFrame:
    """Chuyển bảng ratio của vnstock về dạng dài: ticker, year, quarter, metric, value.

    Hỗ trợ 3 định dạng:
      (r) vnstock 4.x raw (VCI _get_report mode='raw'): mỗi dòng 1 kỳ, cột year, quarter, ratioType, pe, pb, ...
      (a) vnstock 4.x ratio(): mỗi dòng 1 chỉ tiêu (cột item / item_en / item_id), mỗi cột 1 kỳ '2024-Q1'
      (b) vnstock 3.x: mỗi dòng 1 kỳ, cột MultiIndex (nhóm, chỉ tiêu) + yearReport, lengthReport
    """
    rows = []
    if {"year", "quarter", "pe"} <= set(df.columns):  # (r)
        q = df.copy()
        q["year"] = pd.to_numeric(q["year"], errors="coerce")
        q["quarter"] = pd.to_numeric(q["quarter"], errors="coerce")
        q = q[q["quarter"].between(1, 4)]  # bỏ dòng cả năm (quarter=5)
        if "ratioType" in q.columns:
            q = q[q["ratioType"] == "RATIO_TTM"]
        num = {c: pd.to_numeric(q[c], errors="coerce") for c in [*RAW_RATIO_COLS, "marketCap", "numberOfSharesMktCap"]
               if c in q.columns}
        wide = pd.DataFrame({RAW_RATIO_COLS[c]: num[c] for c in RAW_RATIO_COLS if c in num})
        if {"marketCap", "numberOfSharesMktCap", "pe"} <= set(num):
            # EPS (VND, TTM) = giá cuối quý / P/E = (vốn hóa / số CP) / P/E
            price = num["marketCap"] / num["numberOfSharesMktCap"].where(num["numberOfSharesMktCap"] > 0)
            wide["eps"] = price / num["pe"].where(num["pe"] > 0)
        wide["year"], wide["quarter"] = q["year"].astype(int), q["quarter"].astype(int)
        out = wide.melt(id_vars=["year", "quarter"], var_name="metric", value_name="value")
        out.insert(0, "ticker", ticker)
        return out.dropna(subset=["value"]).drop_duplicates(["ticker", "year", "quarter", "metric"])
    if "item_en" in df.columns or "item_id" in df.columns:  # (a)
        label_col = "item_en" if "item_en" in df.columns else "item_id"
        period_cols = [c for c in df.columns if re.match(r"^\d{4}-Q[1-4]$", str(c))]
        for _, r in df.iterrows():
            # Có item_id thì chỉ khớp chính xác (regex dễ nhầm, vd 'Debt/Equity' = debtPerEquity)
            metric = (ITEM_ID_MAP.get(str(r["item_id"]).strip()) if "item_id" in df.columns
                      else _match_metric(r[label_col]))
            if not metric:
                continue
            for p in period_cols:
                y, q = str(p).split("-Q")
                rows.append((ticker, int(y), int(q), metric, pd.to_numeric(r[p], errors="coerce")))
    else:  # (b)
        flat = df.copy()
        if isinstance(flat.columns, pd.MultiIndex):
            flat.columns = [c[-1] for c in flat.columns]
        for _, r in flat.iterrows():
            y, q = int(r["yearReport"]), int(r["lengthReport"])
            for c in flat.columns:
                metric = _match_metric(c)
                if metric:
                    rows.append((ticker, y, q, metric, pd.to_numeric(r[c], errors="coerce")))
    out = pd.DataFrame(rows, columns=["ticker", "year", "quarter", "metric", "value"])
    # Giữ chỉ tiêu khớp đầu tiên nếu một metric khớp nhiều dòng
    return out.dropna(subset=["value"]).drop_duplicates(["ticker", "year", "quarter", "metric"])
