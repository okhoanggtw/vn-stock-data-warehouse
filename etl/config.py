"""Cấu hình chung. Đọc từ biến môi trường hoặc file .env ở thư mục gốc dự án."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

try:  # python-dotenv là tùy chọn
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:
    pass

DB_URL = os.getenv("DB_URL", "postgresql+psycopg2://postgres:postgres@localhost:5432/stock_dw")

# Phạm vi dữ liệu
START_DATE = os.getenv("START_DATE", "2020-01-01")
END_DATE = os.getenv("END_DATE", "2025-12-31")
UNIVERSE_GROUP = os.getenv("UNIVERSE_GROUP", "VN100")   # VN30 / VN100 / HOSE ...
INDEX_CODES = [c.strip() for c in os.getenv("INDEX_CODES", "VNINDEX,VN30").split(",") if c.strip()]

# Nguồn vnstock: VCI hoặc KBS
VNSTOCK_SOURCE = os.getenv("VNSTOCK_SOURCE", "VCI")
REQUEST_SLEEP_SEC = float(os.getenv("REQUEST_SLEEP_SEC", "3.2"))   # nghỉ giữa các request: khách 20 lượt/phút -> 3.2s; có API key Cộng đồng 60 lượt/phút -> 1.1s
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))

# Thư mục lưu dữ liệu thô (để chạy lại ETL không cần gọi API)
RAW_DIR = ROOT / "data" / "raw"
OUTPUT_DIR = ROOT / "outputs"
