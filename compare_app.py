# -*- coding: utf-8 -*-
"""
PChome × MOMO 商品價格比較
執行方式：streamlit run compare_app.py
"""
import re
import time
from datetime import datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import requests
import streamlit as st
from curl_cffi import requests as cffi_requests  # MOMO 需要模擬瀏覽器 TLS 指紋
from plotly.subplots import make_subplots

st.set_page_config(page_title="PChome × MOMO 價格比較", page_icon="🛒", layout="wide")

# ───────────────────────── 常數 ─────────────────────────
PCHOME_API = "https://ecshweb.pchome.com.tw/search/v3.3/all/results"
PCHOME_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

MOMO_API = "https://apisearch.momoshop.com.tw/momoSearchCloud/moec/textSearch"
MOMO_HOME = "https://www.momoshop.com.tw/"
MOMO_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://www.momoshop.com.tw",
    "Referer": "https://www.momoshop.com.tw/",
}

PLATFORM_ORDER = ["PChome", "MOMO"]
PLATFORM_COLORS = {"PChome": "#1C7ED6", "MOMO": "#E64980"}

FIXED_EDGES = [0, 200, 500, 1000, 2000, 5000, np.inf]
FIXED_LABELS = ["0–199", "200–499", "500–999", "1,000–1,999", "2,000–4,999", "5,000 以上"]


# ───────────────────────── 爬蟲：PChome ─────────────────────────
def scrape_pchome(keyword: str, pages: int, delay: float, on_page) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    warns: list[str] = []
    for page in range(1, pages + 1):
        on_page("PChome", page)
        try:
            resp = requests.get(
                PCHOME_API,
                params={"q": keyword, "page": page, "sort": "sale/dc"},
                headers=PCHOME_HEADERS,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as e:
            warns.append(f"PChome 第 {page} 頁爬取失敗：{e}")
            break

        prods = data.get("prods") or []
        if not prods:
            warns.append(f"PChome 第 {page} 頁沒有資料，已停止。")
            break
        for p in prods:
            rows.append({"品名": p.get("name", ""), "價格": p.get("price"), "頁數": page})

        total_page = data.get("totalPage")
        if total_page and page >= int(total_page):
            if page < pages:
                warns.append(f"PChome 搜尋結果只有 {total_page} 頁，已全部爬完。")
            break
        if page < pages:
            time.sleep(delay)

    df = pd.DataFrame(rows, columns=["品名", "價格", "頁數"])
    df.insert(0, "平台", "PChome")
    return df, warns


# ───────────────────────── 爬蟲：MOMO ─────────────────────────
def momo_payload(keyword: str, page: int) -> dict:
    return {
        "host": "momoshop",
        "flag": "searchEngine",
        "data": {
            "searchValue": keyword,
            "searchType": "1",
            "cateLevel": "-1",
            "curPage": str(page),
            "priceS": "0",
            "priceE": "9999999",
            "showType": "chessboardType",
            "isFuzzy": "0",
            "rtnCateDatainfo": {
                "cateCode": "",
                "cateLv": "-1",
                "keyword": keyword,
                "curPage": str(page),
                "historyDoPush": "false",
                "timestamp": int(time.time() * 1000),
            },
            "flag": 2018,
            "serviceCode": "MT01",
            "addressSearchData": {},
        },
    }


def scrape_momo(keyword: str, pages: int, delay: float, on_page) -> tuple[pd.DataFrame, list[str]]:
    rows: list[dict] = []
    warns: list[str] = []

    session = cffi_requests.Session(impersonate="chrome")
    try:
        session.get(MOMO_HOME, headers=MOMO_HEADERS)  # 先取得 cookie
    except Exception as e:
        warns.append(f"MOMO 首頁連線失敗（仍嘗試搜尋）：{e}")

    for page in range(1, pages + 1):
        on_page("MOMO", page)
        try:
            resp = session.post(MOMO_API, headers=MOMO_HEADERS, json=momo_payload(keyword, page))
        except Exception as e:
            warns.append(f"MOMO 第 {page} 頁連線失敗：{e}")
            break

        if resp.status_code != 200:
            warns.append(f"MOMO 第 {page} 頁回應 {resp.status_code}（可能被擋）：{resp.text[:200]}")
            break

        try:
            products = resp.json().get("rtnSearchData", {}).get("goodsInfoList", [])
        except ValueError as e:
            warns.append(f"MOMO 第 {page} 頁回傳格式錯誤：{e}")
            break
        if not products:
            warns.append(f"MOMO 第 {page} 頁沒有資料，已停止。")
            break

        for p in products:
            rows.append({"品名": p.get("goodsName", ""), "價格": p.get("goodsPrice", ""), "頁數": page})
        if page < pages:
            time.sleep(delay)

    df = pd.DataFrame(rows, columns=["品名", "價格", "頁數"])
    df.insert(0, "平台", "MOMO")
    return df, warns


SCRAPERS = {"PChome": scrape_pchome, "MOMO": scrape_momo}


def run_scrape(keyword: str, platforms: list[str], pages: int, delay: float):
    """依序爬取所選平台，回傳 (合併後的原始資料, 警告列表)。"""
    total = pages * len(platforms)
    state = {"done": 0}
    bar = st.progress(0.0, text="準備爬取...")

    def on_page(name: str, page: int):
        bar.progress(min(state["done"] / total, 1.0), text=f"{name}：爬取第 {page} / {pages} 頁...")
        state["done"] += 1

    frames, warns = [], []
    for name in platforms:
        df, w = SCRAPERS[name](keyword, pages, delay, on_page)
        frames.append(df)
        warns.extend(w)
    bar.empty()
    return pd.concat(frames, ignore_index=True), warns


# ───────────────────────── 資料清理 ─────────────────────────
def parse_price(x):
    """取出第一組數字（處理 $、逗號、「起」等）。"""
    if isinstance(x, (int, float, np.integer, np.floating)):
        return x if pd.notna(x) else np.nan
    m = re.search(r"\d[\d,]*", str(x))
    return int(m.group().replace(",", "")) if m else np.nan


def extract_brand(name: str) -> str:
    """品名常以【品牌】開頭；抓不到就歸為「未標示品牌」。"""
    m = re.match(r"\s*[【\[](.+?)[】\]]", str(name))
    return m.group(1).strip() if m else "未標示品牌"


def clean(raw: pd.DataFrame) -> pd.DataFrame:
    df = raw.copy()
    df["價格"] = df["價格"].map(parse_price)
    df = df.dropna(subset=["價格"])
    df = df[(df["價格"] > 0) & (df["品名"].astype(str).str.strip() != "")]
    df = df.drop_duplicates(subset=["平台", "品名"]).reset_index(drop=True)
    df["價格"] = df["價格"].astype(int)
    df["品牌"] = df["品名"].map(extract_brand)
    df["序號"] = df.groupby("平台").cumcount() + 1
    return df


def assign_bins(prices: pd.Series, mode: str, n_bins: int) -> tuple[pd.Series, list[str]]:
    """兩個平台共用同一組價格區間，才能互相比較。"""
    if mode == "固定區間":
        cat = pd.cut(prices, bins=FIXED_EDGES, labels=FIXED_LABELS, right=False)
        return cat.astype(str), FIXED_LABELS

    lo, hi = int(prices.min()), int(prices.max())
    if lo == hi:
        label = f"{lo:,}"
        return pd.Series(label, index=prices.index), [label]
    edges = np.unique(np.round(np.linspace(lo, hi, n_bins + 1)).astype(int))
    labels = [f"{edges[i]:,}–{edges[i + 1]:,}" for i in range(len(edges) - 1)]
    cat = pd.cut(prices, bins=edges, labels=labels, include_lowest=True)
    return cat.astype(str), labels


# ───────────────────────── 側邊欄：搜尋條件 ─────────────────────────
st.title("🛒 PChome × MOMO 商品價格比較")
st.caption("輸入產品名稱與爬取頁數，同時爬取兩個平台並比較價格分布。")

with st.sidebar:
    st.header("搜尋條件")
    with st.form("search_form"):
        keyword_input = st.text_input("產品名稱", value="行動電源")
        platforms_input = st.multiselect("比較平台", PLATFORM_ORDER, default=PLATFORM_ORDER)
        pages_input = st.number_input("每個平台爬取頁數", min_value=1, max_value=20, value=2, step=1)
        delay_input = st.slider("每頁間隔（秒）", 0.5, 10.0, 2.0, 0.5)
        submitted = st.form_submit_button("開始爬取", type="primary")
    st.caption("請合理設定頁數與間隔，避免對網站造成負擔。")

if submitted:
    kw = keyword_input.strip()
    if not kw:
        st.sidebar.warning("請輸入產品名稱")
    elif not platforms_input:
        st.sidebar.warning("請至少選一個平台")
    else:
        raw_new, warns_new = run_scrape(kw, platforms_input, int(pages_input), float(delay_input))
        st.session_state["raw"] = raw_new
        st.session_state["warns"] = warns_new
        st.session_state["keyword"] = kw

raw = st.session_state.get("raw")
keyword = st.session_state.get("keyword", "")

if raw is None:
    st.info("請在左側輸入產品名稱與頁數，然後按「開始爬取」。")
    st.stop()

for w in st.session_state.get("warns", []):
    st.warning(w)

if raw.empty:
    st.error("沒有取得任何資料，可能是被擋或關鍵字無結果。")
    st.stop()

df = clean(raw)
if df.empty:
    st.error("資料清理後沒有有效價格。")
    st.stop()

platforms = [p for p in PLATFORM_ORDER if p in set(df["平台"])]
n_plat = len(platforms)

# ───────────────────────── 側邊欄：圖表設定 ─────────────────────────
with st.sidebar:
    st.divider()
    st.subheader("圖表設定")
    bin_mode = st.radio("價格區間方式", ["固定區間", "等寬區間"], horizontal=True)
    n_bins = st.slider("等寬區間數", 2, 10, 5, disabled=(bin_mode == "固定區間"))
    top_n = st.slider("旭日圖／柱狀圖顯示前幾大品牌", 3, 20, 8)

df["價格區間"], bin_labels = assign_bins(df["價格"], bin_mode, n_bins)
present = set(df["價格區間"])
bin_labels = [b for b in bin_labels if b in present]

# ───────────────────────── 總覽 ─────────────────────────
summary = (
    df.groupby("平台")["價格"]
    .agg(商品數="count", 平均價格="mean", 中位數="median", 最低價="min", 最高價="max")
    .reindex(platforms)
)
summary.index.name = "平台"

st.subheader(f"「{keyword}」搜尋結果總覽")
for p in platforms:
    s = summary.loc[p]
    st.markdown(f"**{p}**")
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("商品數", f"{int(s['商品數']):,}")
    c2.metric("平均價格", f"NT$ {s['平均價格']:,.0f}")
    c3.metric("中位數", f"NT$ {s['中位數']:,.0f}")
    c4.metric("最低價", f"NT$ {s['最低價']:,.0f}")
    c5.metric("最高價", f"NT$ {s['最高價']:,.0f}")

if n_plat == 2:
    a, b = platforms
    ma, mb = summary.loc[a, "平均價格"], summary.loc[b, "平均價格"]
    if ma != mb:
        cheap, exp = (a, b) if ma < mb else (b, a)
        pct = abs(ma - mb) / max(ma, mb) * 100
        st.info(
            f"平均價格：**{cheap}** 較低，約比 {exp} 低 {pct:.1f}%"
            f"（{a} NT$ {ma:,.0f}　vs　{b} NT$ {mb:,.0f}）。"
        )
    st.caption("兩平台搜尋結果的商品組合與排序方式不同，平均價格僅供參考。")

tab_line, tab_pie, tab_sun, tab_bar, tab_data = st.tabs(
    ["📈 折線圖", "🥧 圓餅圖", "☀️ 旭日圖", "📊 柱狀圖", "📄 資料"]
)

# ---- 折線圖 ----
with tab_line:
    max_cnt = int(df.groupby("平台").size().max())
    n_show = st.slider("每個平台顯示前幾筆", 1, max_cnt, min(70, max_cnt)) if max_cnt > 1 else 1
    plot_df = df[df["序號"] <= n_show]

    fig = px.line(
        plot_df,
        x="序號",
        y="價格",
        color="平台",
        markers=True,
        hover_name="品名",
        color_discrete_map=PLATFORM_COLORS,
        category_orders={"平台": platforms},
        labels={"序號": "商品序號（依搜尋結果順序）", "價格": "價格 (NT$)"},
        title=f"「{keyword}」售價走勢：PChome vs MOMO",
    )
    for i, p in enumerate(platforms):
        m = plot_df.loc[plot_df["平台"] == p, "價格"].mean()
        fig.add_hline(
            y=m,
            line_dash="dash",
            line_color=PLATFORM_COLORS[p],
            annotation_text=f"{p} 平均 {m:,.0f}",
            annotation_font_color=PLATFORM_COLORS[p],
            annotation_position="top left" if i == 0 else "bottom right",
        )
    fig.update_layout(height=550)
    st.plotly_chart(fig)

# ---- 圓餅圖 ----
with tab_pie:
    palette = px.colors.qualitative.Set3
    color_map = {lab: palette[i % len(palette)] for i, lab in enumerate(bin_labels)}

    fig = make_subplots(
        rows=1,
        cols=n_plat,
        specs=[[{"type": "domain"}] * n_plat],
        subplot_titles=platforms,
    )
    for i, p in enumerate(platforms, start=1):
        c = df[df["平台"] == p]["價格區間"].value_counts().reindex(bin_labels).fillna(0).astype(int)
        c = c[c > 0]
        fig.add_trace(
            go.Pie(
                labels=list(c.index),
                values=list(c.values),
                name=p,
                hole=0.3,
                sort=False,
                textinfo="label+percent",
                marker=dict(colors=[color_map[lab] for lab in c.index]),
            ),
            row=1,
            col=i,
        )
    fig.update_layout(height=550, title_text=f"「{keyword}」價格區間佔比（商品數）")
    st.plotly_chart(fig)
    st.caption("兩個平台使用相同的價格區間與顏色，方便對照。")

# ---- 旭日圖 ----
with tab_sun:
    sb = df.copy()
    top_brands = sb["品牌"].value_counts().head(top_n).index
    sb["品牌"] = sb["品牌"].where(sb["品牌"].isin(top_brands), "其他品牌")
    sb["商品數"] = 1
    fig = px.sunburst(
        sb,
        path=["平台", "價格區間", "品牌"],
        values="商品數",
        color="平台",
        color_discrete_map=PLATFORM_COLORS,
        title=f"「{keyword}」平台 → 價格區間 → 品牌",
    )
    fig.update_traces(textinfo="label+percent parent", insidetextorientation="radial")
    fig.update_layout(height=750)
    st.plotly_chart(fig)
    st.caption(f"由內到外：平台 → 價格區間 → 品牌（兩平台合計前 {top_n} 大品牌，其餘為「其他品牌」）。")

# ---- 柱狀圖 ----
with tab_bar:
    # (1) 價格區間商品數
    st.subheader("各價格區間的商品數")
    as_pct = st.radio("顯示方式", ["商品數", "平台內佔比 (%)"], horizontal=True, key="bar_mode") != "商品數"
    idx = pd.MultiIndex.from_product([platforms, bin_labels], names=["平台", "價格區間"])
    cnt = df.groupby(["平台", "價格區間"]).size().reindex(idx, fill_value=0).rename("商品數").reset_index()
    cnt["佔比"] = cnt["商品數"] / cnt.groupby("平台")["商品數"].transform("sum") * 100
    y_col = "佔比" if as_pct else "商品數"
    fig = px.bar(
        cnt,
        x="價格區間",
        y=y_col,
        color="平台",
        barmode="group",
        text_auto=".1f" if as_pct else True,
        color_discrete_map=PLATFORM_COLORS,
        category_orders={"價格區間": bin_labels, "平台": platforms},
        labels={"佔比": "佔比 (%)"},
    )
    fig.update_layout(height=450)
    st.plotly_chart(fig)

    # (2) 價格統計值
    st.subheader("價格統計值比較")
    stats = summary.reset_index().melt(
        id_vars="平台",
        value_vars=["平均價格", "中位數", "最低價", "最高價"],
        var_name="統計",
        value_name="價格",
    )
    fig = px.bar(
        stats,
        x="統計",
        y="價格",
        color="平台",
        barmode="group",
        text_auto=",.0f",
        color_discrete_map=PLATFORM_COLORS,
        category_orders={"統計": ["平均價格", "中位數", "最低價", "最高價"], "平台": platforms},
        labels={"價格": "價格 (NT$)"},
    )
    fig.update_layout(height=450)
    st.plotly_chart(fig)

    # (3) 品牌比較
    st.subheader("品牌比較")
    c_a, c_b = st.columns(2)
    brand_metric = c_a.radio("比較指標", ["商品數", "平均價格"], horizontal=True, key="brand_metric")
    excl_unknown = c_b.checkbox("排除「未標示品牌」", value=True)
    bdf = df[df["品牌"] != "未標示品牌"] if excl_unknown else df
    if bdf.empty:
        st.info("沒有可比較的品牌（品名中沒有【品牌】標示）。")
    else:
        brands = bdf["品牌"].value_counts().head(top_n).index.tolist()
        bd = (
            bdf[bdf["品牌"].isin(brands)]
            .groupby(["品牌", "平台"])["價格"]
            .agg(商品數="count", 平均價格="mean")
            .reset_index()
        )
        fig = px.bar(
            bd,
            x="品牌",
            y=brand_metric,
            color="平台",
            barmode="group",
            text_auto=",.0f",
            color_discrete_map=PLATFORM_COLORS,
            category_orders={"品牌": brands, "平台": platforms},
            labels={"平均價格": "平均價格 (NT$)"},
        )
        fig.update_layout(height=450)
        st.plotly_chart(fig)
        st.caption("品牌取自品名開頭的【品牌】標示；PChome 的品名較少使用此格式，品牌數可能偏少。")

# ---- 資料 ----
with tab_data:
    show_cols = ["平台", "序號", "品名", "品牌", "價格", "價格區間", "頁數"]
    sel = st.multiselect("篩選平台", platforms, default=platforms, key="data_filter")
    st.dataframe(df[df["平台"].isin(sel)][show_cols], width="stretch")
    safe_kw = re.sub(r'[\\/:*?"<>|\s]+', "_", keyword)
    st.download_button(
        "下載 CSV",
        data=df[show_cols].to_csv(index=False).encode("utf-8-sig"),  # utf-8-sig：Excel 開啟中文不亂碼
        file_name=f"{safe_kw}_PChome_MOMO_{datetime.now():%Y%m%d}.csv",
        mime="text/csv",
    )
