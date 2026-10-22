"""
KHAI PHÁ DỮ LIỆU – Phân cụm cổ phiếu theo đặc trưng rủi ro – lợi nhuận.

Đầu vào : kho dữ liệu (dw.v_price_enriched, dw.fact_index_daily, dw.fact_financial_ratio)
Đầu ra  : outputs/<run_id>/*.png, *.csv  +  bảng dw.mining_cluster_result

    python -m mining.clustering                    # tự chọn k theo Silhouette
    python -m mining.clustering --k 5 --start 2021-01-01 --end 2025-12-31
    python -m mining.clustering --no-fundamentals  # chỉ dùng đặc trưng giá
"""
import argparse
import datetime as dt
import logging

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.cluster.hierarchy import dendrogram, fcluster, linkage, set_link_color_palette  # noqa: E402
from scipy.spatial.distance import squareform  # noqa: E402
from sklearn.cluster import KMeans  # noqa: E402
from sklearn.decomposition import PCA  # noqa: E402
from sklearn.metrics import adjusted_rand_score, normalized_mutual_info_score, silhouette_score  # noqa: E402
from sklearn.preprocessing import StandardScaler  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402

from etl import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("mining")

PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e4e3df"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": INK2, "axes.labelcolor": INK,
    "xtick.color": INK2, "ytick.color": INK2, "axes.spines.top": False, "axes.spines.right": False,
    "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "figure.facecolor": "#fcfcfb",
    "axes.facecolor": "#fcfcfb", "savefig.dpi": 160, "savefig.bbox": "tight",
})

PRICE_FEATURES = ["ann_return", "ann_vol", "beta", "max_drawdown", "log_liquidity"]
FUND_FEATURES = ["pe", "pb", "roe"]
FEATURE_VI = {
    "ann_return": "Lợi nhuận năm", "ann_vol": "Biến động năm", "beta": "Beta",
    "max_drawdown": "Max drawdown", "log_liquidity": "log10(GTGD TB/ngày)",
    "pe": "P/E", "pb": "P/B", "roe": "ROE",
}


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--start", default=config.START_DATE)
    p.add_argument("--end", default=config.END_DATE)
    p.add_argument("--k", type=int, help="số cụm; bỏ trống để chọn theo Silhouette")
    p.add_argument("--k-max", type=int, default=10)
    p.add_argument("--min-coverage", type=float, default=0.8, help="tỷ lệ phiên tối thiểu có dữ liệu")
    p.add_argument("--no-fundamentals", action="store_true")
    p.add_argument("--exclude", default="", help="mã loại khỏi phân tích (vd ngoại lai), cách nhau bởi dấu phẩy")
    p.add_argument("--industry-level", default="icb_name_l2", choices=["icb_name_l1", "icb_name_l2", "icb_name_l3"])
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


# ----------------------------------------------------------------------
# 1. Lấy dữ liệu từ kho
# ----------------------------------------------------------------------
def load_data(engine, start, end, level):
    with engine.connect() as c:
        px = pd.read_sql(text(f"""
            SELECT v.ticker, v.full_date, v.daily_return, v.trading_value
            FROM dw.v_price_enriched v
            WHERE v.full_date BETWEEN :s AND :e
              AND NOT EXISTS (  -- loại lợi nhuận bị gắn cờ (chia tách chưa điều chỉnh...)
                    SELECT 1 FROM meta.dq_issue q
                    WHERE q.action = 'FLAGGED' AND q.ticker = v.ticker AND q.trade_date = v.full_date)
        """), c, params={"s": start, "e": end})
        vni = pd.read_sql(text("""
            SELECT d.full_date, f.daily_return FROM dw.fact_index_daily f JOIN dw.dim_date d USING (date_key)
            WHERE f.index_code = 'VNINDEX' AND d.full_date BETWEEN :s AND :e
        """), c, params={"s": start, "e": end})
        fin = pd.read_sql(text("""
            SELECT s.ticker, d.full_date, r.pe, r.pb, r.roe
            FROM dw.fact_financial_ratio r JOIN dw.dim_stock s USING (stock_key) JOIN dw.dim_date d USING (date_key)
            WHERE d.full_date BETWEEN :s AND :e
        """), c, params={"s": start, "e": end})
        ind = pd.read_sql(text(f"""
            SELECT s.ticker, i.{level} AS industry
            FROM dw.dim_stock s JOIN dw.dim_industry i USING (industry_key) WHERE s.is_current
        """), c)
    return px, vni, fin, ind.set_index("ticker")["industry"]


# ----------------------------------------------------------------------
# 2. Xây vector đặc trưng
# ----------------------------------------------------------------------
def build_features(px, vni, fin, min_cov, use_fund):
    R = px.pivot(index="full_date", columns="ticker", values="daily_return").sort_index()
    V = px.pivot(index="full_date", columns="ticker", values="trading_value")
    coverage = R.notna().mean()
    keep = coverage[coverage >= min_cov].index
    dropped = sorted(set(R.columns) - set(keep))
    if dropped:
        log.info("Loại %d mã thiếu dữ liệu (< %.0f%% phiên): %s", len(dropped), min_cov * 100, ", ".join(dropped))
    R, V = R[keep], V[keep]

    m = vni.set_index("full_date")["daily_return"].reindex(R.index)
    feats = pd.DataFrame(index=R.columns)
    feats["ann_return"] = R.mean() * 252
    feats["ann_vol"] = R.std() * np.sqrt(252)
    feats["beta"] = [R[t].cov(m) / m[R[t].notna()].var() for t in R.columns]
    wealth = np.exp(np.log1p(R.fillna(0)).cumsum())
    feats["max_drawdown"] = (wealth / wealth.cummax() - 1).min()
    feats["log_liquidity"] = np.log10(V.mean())

    cols = list(PRICE_FEATURES)
    if use_fund and not fin.empty:
        # Trung vị 8 quý gần nhất; P/E âm (lỗ) coi như thiếu
        last = fin.sort_values("full_date").groupby("ticker").tail(8)
        last.loc[last["pe"] <= 0, "pe"] = np.nan
        f = last.groupby("ticker")[FUND_FEATURES].median()
        feats = feats.join(f)
        for c in FUND_FEATURES:  # winsorize 5%–95% để giảm ảnh hưởng ngoại lai
            lo, hi = feats[c].quantile([0.05, 0.95])
            feats[c] = feats[c].clip(lo, hi).fillna(feats[c].median())
        cols += FUND_FEATURES
    return feats[cols].dropna(), R


# ----------------------------------------------------------------------
# 3. Chọn k
# ----------------------------------------------------------------------
def choose_k(X, k_max, seed, out):
    ks, inertia, sil = range(2, min(k_max, len(X) - 1) + 1), [], []
    for k in ks:
        km = KMeans(n_clusters=k, n_init=20, random_state=seed).fit(X)
        inertia.append(km.inertia_)
        sil.append(silhouette_score(X, km.labels_))
    table = pd.DataFrame({"k": list(ks), "inertia": inertia, "silhouette": sil})
    table.to_csv(out / "k_selection.csv", index=False)

    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    axes[0].plot(table.k, table.inertia, color=PALETTE[0], lw=2, marker="o", ms=6)
    axes[0].set(title="Elbow – tổng bình phương trong cụm", xlabel="k", ylabel="Inertia")
    axes[1].plot(table.k, table.silhouette, color=PALETTE[0], lw=2, marker="o", ms=6)
    best = int(table.loc[table.silhouette.idxmax(), "k"])
    axes[1].scatter([best], [table.silhouette.max()], s=120, facecolor="none", edgecolor=INK, lw=1.5, zorder=3)
    axes[1].annotate(f"k = {best}", (best, table.silhouette.max()), xytext=(8, -14), textcoords="offset points", color=INK)
    axes[1].set(title="Silhouette score (cao hơn = tách cụm tốt hơn)", xlabel="k", ylabel="Silhouette")
    fig.savefig(out / "01_chon_k.png")
    plt.close(fig)
    return best, table


# ----------------------------------------------------------------------
# 4. Gán nhãn mô tả cho cụm
# ----------------------------------------------------------------------
def describe_clusters(profile):
    labels = {}
    vol_rank = profile["ann_vol"].rank()
    ret_rank = profile["ann_return"].rank()
    n = len(profile)
    for cid, row in profile.iterrows():
        tags = []
        if vol_rank[cid] == n:
            tags.append("biến động cao nhất")
        elif vol_rank[cid] == 1:
            tags.append("phòng thủ")
        if ret_rank[cid] == n:
            tags.append("lợi nhuận cao nhất")
        elif ret_rank[cid] == 1:
            tags.append("lợi nhuận thấp nhất")
        if "pe" in row and row["pe"] == profile["pe"].max():
            tags.append("định giá cao")
        desc = ", ".join(tags) or "trung tính"
        labels[cid] = f"Cụm {cid} – {desc} (β≈{row['beta']:.2f})"
    return labels


# ----------------------------------------------------------------------
def main():
    args = parse_args()
    run_id = dt.datetime.now().strftime("%Y%m%d%H%M%S")
    out = config.OUTPUT_DIR / f"clustering_{run_id}"
    out.mkdir(parents=True, exist_ok=True)
    engine = create_engine(config.DB_URL, future=True)

    px, vni, fin, industry = load_data(engine, args.start, args.end, args.industry_level)
    if px.empty or vni.empty:
        raise SystemExit("Kho chưa có dữ liệu giá hoặc VNINDEX trong khoảng ngày đã chọn – hãy chạy ETL trước.")
    excluded = [t.strip().upper() for t in args.exclude.split(",") if t.strip()]
    if excluded:
        log.info("Loại theo yêu cầu: %s", ", ".join(excluded))
        px, fin = px[~px["ticker"].isin(excluded)], fin[~fin["ticker"].isin(excluded)]
    feats, R = build_features(px, vni, fin, args.min_coverage, not args.no_fundamentals)
    cols = list(feats.columns)
    log.info("Ma trận đặc trưng: %d mã × %d đặc trưng (%s)", *feats.shape, ", ".join(cols))
    feats.round(4).to_csv(out / "features.csv")

    X = StandardScaler().fit_transform(feats)
    k_best, _ = choose_k(X, args.k_max, args.seed, out)
    k = args.k or k_best
    log.info("Số cụm k = %d (Silhouette tốt nhất ở k = %d)", k, k_best)

    # ---------------- K-Means ----------------
    km = KMeans(n_clusters=k, n_init=50, random_state=args.seed).fit(X)
    feats["kmeans"] = km.labels_ + 1
    sil = silhouette_score(X, km.labels_)

    profile = feats.groupby("kmeans")[cols].mean()
    profile.insert(0, "so_ma", feats.groupby("kmeans").size())
    labels = describe_clusters(profile)
    profile["mo_ta"] = profile.index.map(labels)
    profile["cac_ma"] = feats.groupby("kmeans").apply(lambda g: ", ".join(sorted(g.index)))
    profile.round(4).to_csv(out / "kmeans_profile.csv")

    # PCA 2D
    pca = PCA(n_components=2, random_state=args.seed).fit(X)
    P = pca.transform(X)
    fig, ax = plt.subplots(figsize=(9, 6.5))
    for i, cid in enumerate(sorted(feats["kmeans"].unique())):
        msk = (feats["kmeans"] == cid).values
        ax.scatter(P[msk, 0], P[msk, 1], s=70, color=PALETTE[i % len(PALETTE)], edgecolor="#fcfcfb",
                   linewidth=1.5, label=labels[cid], zorder=3)
    for (x, y), t in zip(P, feats.index):
        ax.annotate(t, (x, y), xytext=(4, 3), textcoords="offset points", fontsize=7.5, color=INK2)
    ev = pca.explained_variance_ratio_ * 100
    ax.set(xlabel=f"PC1 ({ev[0]:.1f}% phương sai)", ylabel=f"PC2 ({ev[1]:.1f}% phương sai)",
           title=f"K-Means k={k} trên không gian PCA – Silhouette = {sil:.3f}")
    ax.legend(loc="upper left", bbox_to_anchor=(1.01, 1), frameon=False, fontsize=9)
    fig.savefig(out / "02_kmeans_pca.png")
    plt.close(fig)

    # Hồ sơ cụm (z-score) – small multiples, mỗi đặc trưng 1 ô, cùng thang
    z = pd.DataFrame(X, index=feats.index, columns=cols).groupby(feats["kmeans"]).mean()
    fig, axes = plt.subplots(1, len(cols), figsize=(2.1 * len(cols), 3.4), sharey=True)
    lim = np.abs(z.values).max() * 1.15
    for j, c in enumerate(cols):
        a = axes[j]
        a.barh([labels[i].split(" –")[0] for i in z.index], z[c],
               color=[PALETTE[(i - 1) % len(PALETTE)] for i in z.index], height=0.6)
        a.axvline(0, color=INK2, lw=0.8)
        a.set_xlim(-lim, lim)
        a.set_title(FEATURE_VI[c], fontsize=9)
        a.grid(axis="y", visible=False)
    axes[0].invert_yaxis()
    fig.suptitle("Hồ sơ cụm – trung bình z-score từng đặc trưng (0 = trung bình toàn thị trường)", x=0.02, y=1.06, ha="left")
    fig.savefig(out / "03_ho_so_cum.png")
    plt.close(fig)

    # ---------------- Phân cụm phân cấp trên khoảng cách tương quan ----------------
    C = R[feats.index].corr(min_periods=60)
    D = np.sqrt(np.clip(2 * (1 - C.values), 0, None))
    np.fill_diagonal(D, 0)
    # d = √(2(1−ρ)) chính là khoảng cách Euclid giữa 2 chuỗi lợi nhuận đã chuẩn hóa -> dùng được Ward
    Z = linkage(squareform(D, checks=False), method="ward")
    feats["hierarchical"] = fcluster(Z, t=k, criterion="maxclust")

    set_link_color_palette(PALETTE)
    fig, ax = plt.subplots(figsize=(max(9, 0.28 * len(feats)), 5))
    dendrogram(Z, labels=list(feats.index), leaf_rotation=90, leaf_font_size=8, ax=ax,
               color_threshold=Z[-(k - 1), 2], above_threshold_color=INK2)
    ax.set(title=f"Dendrogram – khoảng cách d = √(2(1−ρ)), liên kết Ward, cắt {k} cụm", ylabel="Khoảng cách")
    ax.grid(axis="x", visible=False)
    fig.savefig(out / "04_dendrogram.png")
    plt.close(fig)

    # ---------------- Đánh giá so với phân ngành ICB ----------------
    ind = industry.reindex(feats.index).fillna("Chưa phân ngành")
    evals = pd.DataFrame({
        "phuong_phap": ["K-Means (đặc trưng)", "Phân cấp (tương quan)"],
        "ARI_vs_nganh": [adjusted_rand_score(ind, feats["kmeans"]), adjusted_rand_score(ind, feats["hierarchical"])],
        "NMI_vs_nganh": [normalized_mutual_info_score(ind, feats["kmeans"]),
                         normalized_mutual_info_score(ind, feats["hierarchical"])],
        "ARI_giua_2_pp": [adjusted_rand_score(feats["kmeans"], feats["hierarchical"])] * 2,
    })
    evals.round(4).to_csv(out / "danh_gia.csv", index=False)
    for name, col in [("kmeans", "kmeans"), ("hierarchical", "hierarchical")]:
        pd.crosstab(ind, feats[col]).to_csv(out / f"crosstab_nganh_{name}.csv")

    # ---------------- Ứng dụng: đa dạng hóa danh mục ----------------
    # Danh mục A: mỗi cụm 1 mã (mã có biến động thấp nhất cụm). So với 2.000 danh mục ngẫu nhiên cùng số mã.
    picks = feats.groupby("hierarchical")["ann_vol"].idxmin().tolist()
    Rf = R[feats.index].fillna(0)
    vol_pick = Rf[picks].mean(axis=1).std() * np.sqrt(252)
    rng = np.random.default_rng(args.seed)
    rand_vols = np.array([Rf[rng.choice(feats.index, len(picks), replace=False)].mean(axis=1).std() * np.sqrt(252)
                          for _ in range(2000)])
    pct = (rand_vols < vol_pick).mean() * 100
    portfolio = pd.DataFrame({
        "danh_muc": ["Mỗi cụm 1 mã", "Ngẫu nhiên – trung vị", "Ngẫu nhiên – P10", "Ngẫu nhiên – P90"],
        "bien_dong_nam": [vol_pick, np.median(rand_vols), np.percentile(rand_vols, 10), np.percentile(rand_vols, 90)],
    })
    portfolio.round(4).to_csv(out / "da_dang_hoa.csv", index=False)

    # ---------------- Ghi kết quả vào kho ----------------
    rows = []
    for t, r in feats.iterrows():
        rows.append((run_id, t, "kmeans", int(r["kmeans"]), labels[r["kmeans"]]))
        rows.append((run_id, t, "hierarchical", int(r["hierarchical"]), None))
    pd.DataFrame(rows, columns=["run_id", "ticker", "method", "cluster_id", "cluster_label"]).to_sql(
        "mining_cluster_result", engine, schema="dw", if_exists="append", index=False)
    feats.round(4).to_csv(out / "ket_qua_phan_cum.csv")

    # ---------------- Tóm tắt ----------------
    print("\n" + "=" * 70)
    print(f"Kết quả lưu tại: {out}")
    print(f"Số mã: {len(feats)} | k = {k} | Silhouette K-Means = {sil:.3f}")
    print(f"PCA: 2 thành phần giải thích {ev.sum():.1f}% phương sai")
    print("\nHồ sơ cụm K-Means:")
    with pd.option_context("display.width", 200, "display.max_colwidth", 60):
        print(profile.drop(columns=["cac_ma"]).round(3).to_string())
    print("\nĐánh giá so với phân ngành ICB:")
    print(evals.round(3).to_string(index=False))
    print(f"\nĐa dạng hóa: danh mục 'mỗi cụm 1 mã' {picks}")
    print(f"  biến động năm = {vol_pick:.1%} | ngẫu nhiên trung vị = {np.median(rand_vols):.1%} "
          f"| thấp hơn {100 - pct:.0f}% số danh mục ngẫu nhiên")


if __name__ == "__main__":
    main()
