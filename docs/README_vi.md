# Kho dữ liệu chứng khoán Việt Nam và phân cụm cổ phiếu

BTL môn Kho dữ liệu và Khai phá dữ liệu. Dự án gồm 4 bước:

- **Extract:** lấy dữ liệu bằng `vnstock`.
- **Transform:** làm sạch dữ liệu và kiểm tra chất lượng (DQ).
- **Load:** nạp vào kho PostgreSQL theo **Galaxy Schema**, có SCD2 và nạp tăng dần (incremental).
- **Phân tích:** viết truy vấn OLAP, sau đó phân cụm cổ phiếu bằng **K-Means** và **phân cụm phân cấp**.

```
stock-dw/
├── sql/
│   ├── 01_create_schema.sql   # staging + dw + meta, dim/fact, partition theo năm, view cho Power BI
│   └── 02_olap_queries.sql    # ROLLUP, CUBE, slice, drill-down, dice, beta, drill-across, SCD2, DQ
├── etl/
│   ├── run_etl.py             # điều phối ETL
│   ├── transform.py           # 6 quy tắc làm sạch dữ liệu giá
│   ├── load.py                # SQL nạp staging -> dw (SCD1, SCD2, fact, tính lợi nhuận, cờ DQ)
│   └── sources/
│       ├── vnstock_source.py  # nguồn thật
│       └── mock_source.py     # nguồn giả lập để chạy thử (mã bắt đầu bằng Z)
├── mining/clustering.py       # đặc trưng -> chọn k -> K-Means + Hierarchical -> ARI -> đa dạng hóa
├── docker-compose.yml         # PostgreSQL 16
└── requirements.txt
```

## 1. Cài đặt

```bash
# PostgreSQL: dùng Docker, hoặc dùng bản đã cài sẵn và tạo database tên stock_dw
docker compose up -d

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# vnstock không còn phát hành trên PyPI, phải cài từ index riêng của tác giả
pip install -U --extra-index-url https://vnstocks.com/api/simple vnstock vnai

cp .env.example .env               # sửa DB_URL nếu cần
```

## 2. Chạy thử bằng dữ liệu giả lập (không cần mạng)

Bước này dùng để kiểm tra kho và code chạy đúng trước khi lấy dữ liệu thật.

```bash
python -m etl.run_etl --mock --init-db --end 2025-06-30 --effective-date 2025-06-30
# Lần 2: chỉ nạp phần mới, đồng thời giả lập mã ZBD3 chuyển sàn HNX -> HOSE để kiểm tra SCD2
python -m etl.run_etl --mock --mock-change --end 2025-12-31 --effective-date 2025-07-01
python -m mining.clustering
```

> ⚠️ Kết quả trên dữ liệu giả lập **không được dùng** để viết nhận xét trong báo cáo.

## 3. Chạy với dữ liệu thật

```bash
# Tạo database mới, hoặc xóa dữ liệu mock trước:
#   DROP SCHEMA dw, staging, meta CASCADE;
python -m etl.run_etl --init-db --tickers FPT,VCB,HPG,VHM,SSI --skip-ratios   # thử vài mã trước
python -m etl.run_etl                                   # toàn bộ VN100, 2020-01-01 -> 2025-12-31
python -m etl.run_etl --end 2026-10-31                  # sau này chỉ tải thêm các phiên mới
python -m mining.clustering --start 2021-01-01 --end 2025-12-31 --k 4 --exclude KOS   # cấu hình dùng trong báo cáo
```

- KOS bị loại vì là ngoại lai (β ≈ 0,15, biến động 16%/năm, P/E ≈ 80), làm méo cả cụm lẫn kết quả đa dạng hóa.
- Silhouette tốt nhất ở k = 2, nhưng đường Silhouette gần như phẳng (0,18–0,23) nên chọn k = 4 để cụm có ý nghĩa kinh tế hơn.

- Mỗi mã cần khoảng 3 request (giá, chỉ số tài chính, cộng phần danh mục dùng chung). Với 100 mã, thời gian chạy khoảng 5–10 phút.
- Nếu bị lỗi giới hạn số request (rate limit), tăng `REQUEST_SLEEP_SEC` trong `.env`.
- Dữ liệu thô của mỗi lần chạy được lưu ở `data/raw/<run_id>/`.

## 4. Thiết kế kho

| Bảng | Grain / vai trò |
|---|---|
| `dim_date` | 1 ngày, sinh sẵn 2015–2030, có cờ `is_trading_day` và `is_quarter_end` |
| `dim_stock` | **SCD Type 2**: đổi ngành hoặc đổi sàn thì sinh bản ghi mới (`valid_from`, `valid_to`, `is_current`). Tên công ty theo **Type 1** (ghi đè) |
| `dim_industry` | Phân cấp ICB L1 → L2 → L3, dùng cho drill-down |
| `dim_exchange` | HOSE, HNX, UPCOM, kèm biên độ giá (7/10/15%) phục vụ kiểm tra DQ |
| `fact_daily_price` | 1 mã × 1 phiên. **Partition theo năm**. Có `daily_return`, `log_return` tính sẵn |
| `fact_index_daily` | 1 chỉ số × 1 phiên (VNINDEX, VN30), dùng để tính beta |
| `fact_financial_ratio` | 1 mã × 1 quý: P/E, P/B, ROE, ROA, EPS, D/E, biên lợi nhuận ròng |
| `mining_cluster_result` | Kết quả phân cụm ghi ngược vào kho để làm dashboard |
| `meta.etl_log`, `meta.dq_issue` | Nhật ký ETL và lỗi chất lượng dữ liệu |

**Các quy tắc kiểm tra chất lượng dữ liệu (DQ):**
- **Loại bỏ:**
  - dòng trùng lặp;
  - giá đóng cửa ≤ 0;
  - high < low;
  - close nằm ngoài khoảng [low, high];
  - giao dịch rơi vào cuối tuần;
  - khối lượng âm.
- **Gắn cờ:** lợi nhuận vượt biên độ của sàn. Trường hợp này thường do chia tách hoặc thưởng cổ phiếu mà giá chưa được điều chỉnh. Bước phân cụm sẽ tự loại các ngày bị gắn cờ.

## 5. Khai phá dữ liệu

1. **Đặc trưng cho mỗi mã:**
   - lợi nhuận năm hóa;
   - biến động năm hóa;
   - beta so với VN-Index;
   - max drawdown;
   - log thanh khoản;
   - trung vị P/E, P/B, ROE của 8 quý gần nhất (đã winsorize 5–95%).
2. **Chuẩn hóa** bằng StandardScaler, rồi **chọn k** theo Elbow và Silhouette.
3. **K-Means** chạy trên vector đặc trưng.
4. **Phân cụm phân cấp Ward** chạy trên khoảng cách tương quan d = √(2(1−ρ)).
   - Đây là khoảng cách Euclid giữa hai chuỗi lợi nhuận đã chuẩn hóa, nên dùng được phương pháp Ward.
5. **Đánh giá:** tính ARI và NMI giữa cụm tìm được và phân ngành ICB cấp 2, kèm bảng crosstab.
6. **Ứng dụng:** chọn mỗi cụm 1 mã để lập danh mục, rồi so biến động của danh mục đó với 2.000 danh mục ngẫu nhiên cùng số mã.

Kết quả nằm ở `outputs/clustering_<run_id>/`:
- 4 biểu đồ PNG: chọn k, PCA, hồ sơ cụm, dendrogram;
- các file CSV: đặc trưng, hồ sơ cụm, đánh giá, crosstab, đa dạng hóa.
