-- =====================================================================
--  TRUY VẤN OLAP MẪU – mỗi truy vấn minh họa 1 thao tác OLAP để đưa vào Chương VI
--  Quy ước: lợi nhuận năm hóa = trung bình lợi nhuận ngày × 252
-- =====================================================================

-- Q1. ROLL-UP: Lợi nhuận năm hóa theo Ngành (ICB cấp 2) × Năm, kèm dòng tổng theo ngành và tổng chung
SELECT  COALESCE(i.icb_name_l2, '== TẤT CẢ NGÀNH ==')        AS nganh,
        COALESCE(CAST(d.year AS TEXT), '== CẢ GIAI ĐOẠN ==') AS nam,
        ROUND(CAST(AVG(f.daily_return) * 252 * 100 AS NUMERIC), 2) AS loi_nhuan_nam_pct,
        ROUND(SUM(f.trading_value) / 1e12, 2)                AS gia_tri_gd_nghin_ty
FROM dw.fact_daily_price f
JOIN dw.dim_stock s    ON s.stock_key = f.stock_key
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
JOIN dw.dim_date d     ON d.date_key = f.date_key
GROUP BY ROLLUP (i.icb_name_l2, d.year)
ORDER BY i.icb_name_l2 NULLS LAST, d.year NULLS LAST;

-- Q2. CUBE: Giá trị giao dịch theo mọi tổ hợp Ngành (ICB cấp 1) × Quý (năm 2024)
--     (VN100 chỉ gồm mã sàn HOSE nên CUBE theo Sàn sẽ không có ý nghĩa)
SELECT  COALESCE(i.icb_name_l1, 'ALL') AS nganh_l1,
        COALESCE(d.year_quarter, 'ALL') AS quy,
        ROUND(SUM(f.trading_value) / 1e9, 1) AS gia_tri_gd_ty
FROM dw.fact_daily_price f
JOIN dw.dim_stock s    ON s.stock_key = f.stock_key
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
JOIN dw.dim_date d     ON d.date_key = f.date_key
WHERE d.year = 2024
GROUP BY CUBE (i.icb_name_l1, d.year_quarter)
ORDER BY 1, 2;

-- Q3. SLICE: Năm 2022 – ngành nào giảm sâu nhất?
SELECT  i.icb_name_l2 AS nganh,
        ROUND(CAST((EXP(SUM(f.log_return) / COUNT(DISTINCT s.ticker)) - 1) * 100 AS NUMERIC), 2) AS loi_nhuan_tb_1_ma_pct,
        COUNT(DISTINCT s.ticker) AS so_ma
FROM dw.fact_daily_price f
JOIN dw.dim_stock s    ON s.stock_key = f.stock_key
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
JOIN dw.dim_date d     ON d.date_key = f.date_key
WHERE d.year = 2022
GROUP BY i.icb_name_l2
ORDER BY 2;

-- Q4. DRILL-DOWN: từ ngành Ngân hàng xuống từng mã, lợi nhuận và biến động theo năm
SELECT  s.ticker, d.year,
        ROUND(CAST((EXP(SUM(f.log_return)) - 1) * 100 AS NUMERIC), 2)            AS loi_nhuan_nam_pct,
        ROUND(CAST(STDDEV_SAMP(f.daily_return) * SQRT(252) * 100 AS NUMERIC), 2) AS bien_dong_nam_pct
FROM dw.fact_daily_price f
JOIN dw.dim_stock s    ON s.stock_key = f.stock_key
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
JOIN dw.dim_date d     ON d.date_key = f.date_key
WHERE i.icb_name_l2 = 'Ngân hàng'
GROUP BY s.ticker, d.year
ORDER BY s.ticker, d.year;

-- Q5. DICE + RANK: Top 5 mã thanh khoản cao nhất mỗi quý (2024–2025)
WITH q AS (
    SELECT d.year_quarter, s.ticker, SUM(f.trading_value) AS gtgd,
           RANK() OVER (PARTITION BY d.year_quarter ORDER BY SUM(f.trading_value) DESC) AS hang
    FROM dw.fact_daily_price f
    JOIN dw.dim_stock s ON s.stock_key = f.stock_key
    JOIN dw.dim_date d  ON d.date_key = f.date_key
    WHERE d.year BETWEEN 2024 AND 2025
    GROUP BY d.year_quarter, s.ticker
)
SELECT year_quarter, hang, ticker, ROUND(gtgd / 1e9, 1) AS gia_tri_gd_ty
FROM q WHERE hang <= 5
ORDER BY year_quarter, hang;

-- Q6. Beta của từng mã so với VN-Index: beta = cov(R_ma, R_VNI) / var(R_VNI)
SELECT  s.ticker,
        ROUND(CAST(COVAR_SAMP(f.daily_return, x.daily_return) / VAR_SAMP(x.daily_return) AS NUMERIC), 2) AS beta,
        ROUND(CAST(CORR(f.daily_return, x.daily_return) AS NUMERIC), 2) AS tuong_quan_vni
FROM dw.fact_daily_price f
JOIN dw.dim_stock s ON s.stock_key = f.stock_key
JOIN dw.fact_index_daily x ON x.date_key = f.date_key AND x.index_code = 'VNINDEX'
GROUP BY s.ticker
ORDER BY beta DESC;

-- Q7. Kết hợp 2 bảng fact (drill-across): ROE trung bình vs biến động giá theo ngành
--     P/E dùng trung vị và bỏ P/E <= 0 (quý thua lỗ), vì trung bình P/E bị méo bởi giá trị âm / cực lớn
WITH vol AS (
    SELECT s.ticker, STDDEV_SAMP(f.daily_return) * SQRT(252) AS bien_dong
    FROM dw.fact_daily_price f JOIN dw.dim_stock s USING (stock_key)
    GROUP BY s.ticker
), fin AS (
    SELECT s.ticker, AVG(r.roe) AS roe_tb,
           PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY r.pe) FILTER (WHERE r.pe > 0) AS pe_tv
    FROM dw.fact_financial_ratio r JOIN dw.dim_stock s USING (stock_key)
    GROUP BY s.ticker
)
SELECT  i.icb_name_l2 AS nganh,
        COUNT(*)                                          AS so_ma,
        ROUND(CAST(AVG(fin.roe_tb) * 100 AS NUMERIC), 1)  AS roe_tb_pct,
        ROUND(CAST(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY fin.pe_tv) AS NUMERIC), 1) AS pe_trung_vi,
        ROUND(CAST(AVG(vol.bien_dong) * 100 AS NUMERIC), 1) AS bien_dong_nam_pct
FROM vol
JOIN fin USING (ticker)
JOIN dw.dim_stock s    ON s.ticker = vol.ticker AND s.is_current
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
GROUP BY i.icb_name_l2
ORDER BY roe_tb_pct DESC;

-- Q8. Lịch sử SCD2: các mã từng đổi sàn / đổi ngành
SELECT s.ticker, e.exchange_code, i.icb_name_l3, s.valid_from, s.valid_to, s.is_current
FROM dw.dim_stock s
JOIN dw.dim_exchange e ON e.exchange_key = s.exchange_key
JOIN dw.dim_industry i ON i.industry_key = s.industry_key
WHERE s.ticker IN (SELECT ticker FROM dw.dim_stock GROUP BY ticker HAVING COUNT(*) > 1)
ORDER BY s.ticker, s.valid_from;

-- Q9. Chất lượng dữ liệu: thống kê lỗi theo quy tắc
SELECT rule_name, action, COUNT(*) AS so_dong
FROM meta.dq_issue
GROUP BY rule_name, action
ORDER BY so_dong DESC;
