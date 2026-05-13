"""
scraper.py — Amazon Egypt scraper (curl_cffi + BeautifulSoup)
Uses curl_cffi to spoof Chrome's TLS fingerprint, bypassing Amazon's bot detection.
No Selenium / Chrome required.
"""

from __future__ import annotations  # makes type hints compatible with Python 3.9

import re
import time
import base64
import pandas as pd
import networkx as nx
from bs4 import BeautifulSoup
from curl_cffi import requests  # drop-in replacement with TLS fingerprint spoofing

IMPERSONATE = "chrome124"  # browser profile to spoof

CATEGORY_KEYWORDS = {
    "Sun Care": ["sunscreen", "spf", "sun", "uva", "uvb", "solar"],
    "Moisturizer": ["moistur", "hydrat", "hyaluronic", "cream", "lotion"],
    "Serum": ["serum", "essence", "ampoule"],
    "Cleanser": ["cleanser", "wash", "foam", "micellar"],
    "Toner": ["toner", "tonic"],
    "Mask": ["mask", "sheet"],
    "Hair Care": ["shampoo", "conditioner", "hair"],
    "Body Care": ["body", "shower", "scrub"],
    "Supplement": ["vitamin", "supplement", "capsule", "tablet"],
    "Electronics": ["phone", "laptop", "tablet", "headphone", "keyboard"],
    "Clothing": ["shirt", "dress", "pants", "shoes", "jacket"],
    "Home": ["home", "kitchen", "furniture", "lamp"],
    "Baby": ["baby", "infant", "diaper", "kids", "child"],
}


def guess_category(title: str) -> str:
    tl = title.lower()
    for cat, kws in CATEGORY_KEYWORDS.items():
        for kw in kws:
            if kw in tl:
                return cat
    return "General"


def extract_card(card) -> dict | None:
    title = "NA"
    try:
        img = card.find("img", class_="s-image")
        if img and img.get("alt"):
            title = img["alt"].strip()
    except Exception:
        pass
    if title == "NA":
        try:
            h2 = card.find("h2")
            if h2:
                title = h2.get_text(strip=True)
        except Exception:
            pass

    price = None
    try:
        price_el = card.find("span", class_="a-price")
        if price_el:
            offscreen = price_el.find("span", class_="a-offscreen")
            if offscreen:
                raw = offscreen.get_text()
                cleaned = re.sub(r"[^\d.]", "", raw.replace(",", ""))
                price = float(cleaned) if cleaned else None
    except Exception:
        pass

    rating = None
    try:
        star = card.find("span", class_=lambda c: c and "a-icon-alt" in c)
        if star:
            raw_r = star.get_text(strip=True)
            m = re.match(r"([\d.]+)", raw_r)
            if m:
                rating = float(m.group(1))
    except Exception:
        pass

    link = ""
    try:
        # try multiple selectors — sponsored items use different DOM structure
        for sel in ["h2 a[href]", "a.a-link-normal[href*='/dp/']", "a[href*='/dp/']"]:
            a = card.select_one(sel)
            if a:
                href = a.get("href", "").split("?")[0]  # strip tracking params
                if href:
                    link = href if href.startswith("http") else "https://www.amazon.eg" + href
                    break
    except Exception:
        pass

    img_url = None
    try:
        img_tag = card.find("img", class_="s-image")
        if img_tag:
            img_url = img_tag.get("src", "")
    except Exception:
        pass

    if title == "NA" and price is None and rating is None:
        return None

    return {
        "title": title,
        "price": price,
        "rating": rating,
        "link": link,
        "img_url": img_url,
    }


def fetch_image_b64(url: str) -> str | None:
    try:
        r = requests.get(url, timeout=8, impersonate=IMPERSONATE)
        if r.status_code == 200:
            return base64.b64encode(r.content).decode()
    except Exception:
        pass
    return None


def run_search(
    product: str,
    include_kw: list[str],
    exclude_kw: list[str],
    max_pages: int,
    sleep_sec: float,
    rating_weight: float,
    log_fn,
    progress_fn,
) -> dict:
    price_weight = round(1.0 - rating_weight, 4)
    base_url = "https://www.amazon.eg/s"
    query = product.replace(" ", "+")
    all_data = []

    for page in range(1, max_pages + 1):
        url = f"{base_url}?k={query}&page={page}"
        log_fn(f"[Page {page}] {url}")
        progress_fn(int(page / (max_pages + 2) * 55))

        try:
            resp = requests.get(url, timeout=25, impersonate=IMPERSONATE)
            soup = BeautifulSoup(resp.content, "lxml")
            cards = soup.find_all(
                "div", attrs={"data-component-type": "s-search-result"}
            )
            log_fn(f": {len(cards)} cards found on page {page}")
            for card in cards:
                item = extract_card(card)
                if item:
                    all_data.append(item)
        except Exception as e:
            log_fn(f"✗ Page {page} error: {e}")

        if page < max_pages:
            time.sleep(sleep_sec)

    log_fn(f"Total raw results: {len(all_data)}")
    progress_fn(60)

    # ── FILTER ──────────────────────────────────────────────────────────────
    df = pd.DataFrame(all_data)
    if df.empty:
        return {"error": "No results scraped. Amazon may be blocking — try again."}

    df = df.dropna(subset=["price", "rating"])
    df = df[df["price"] > 0]
    df = df.drop_duplicates(subset=["title"])

    before = len(df)
    base_kw = product.split()
    all_include = base_kw + include_kw
    if all_include:
        pat_inc = "|".join(re.escape(k) for k in all_include)
        df = df[df["title"].str.contains(pat_inc, case=False, na=False)]
    if exclude_kw:
        pat_exc = "|".join(re.escape(k) for k in exclude_kw)
        df = df[~df["title"].str.contains(pat_exc, case=False, na=False)]

    df = df.reset_index(drop=True)
    log_fn(f"Filtering: {before} → {len(df)} products")
    progress_fn(65)

    if df.empty:
        return {"error": "No products after filtering. Try relaxing keywords."}

    # ── SCORE ───────────────────────────────────────────────────────────────
    df2 = df.copy()
    r_min, r_max = df2["rating"].min(), df2["rating"].max()
    df2["norm_rating"] = (
        (df2["rating"] - r_min) / (r_max - r_min) if r_max != r_min else 1.0
    )
    inv = 1.0 / df2["price"]
    p_min, p_max = inv.min(), inv.max()
    df2["norm_inv_price"] = (
        (inv - p_min) / (p_max - p_min) if p_max != p_min else 1.0
    )
    df2["score"] = rating_weight * df2["norm_rating"] + price_weight * df2["norm_inv_price"]
    df2["category"] = df2["title"].apply(guess_category)
    df2 = df2.sort_values("score", ascending=False).reset_index(drop=True)

    log_fn("Scoring done ✓")
    progress_fn(72)

    # ── WINNER ──────────────────────────────────────────────────────────────
    winner_row = df2.iloc[0]
    score_max = df2["score"].max()

    img_b64 = None
    if winner_row.get("img_url"):
        log_fn("Fetching winner image…")
        img_b64 = fetch_image_b64(winner_row["img_url"])

    progress_fn(80)

    # ── NETWORK PICK ────────────────────────────────────────────────────────
    G = nx.Graph()
    for i, row in df2.iterrows():
        G.add_node(i, title=row["title"], price=row["price"], rating=row["rating"], link=row["link"])

    # connect nodes whose price difference is within 15%
    rows_list = list(df2.iterrows())
    for i in range(len(rows_list)):
        for j in range(i + 1, len(rows_list)):
            pi = rows_list[i][1]["price"]
            pj = rows_list[j][1]["price"]
            if abs(pi - pj) / max(pi, pj) < 0.15:
                G.add_edge(rows_list[i][0], rows_list[j][0])

    net_pick = None
    net_node_idx = None
    if G.nodes():
        degrees = dict(G.degree())
        top_node = max(degrees, key=lambda n: degrees[n])
        if top_node != 0:  # don't repeat winner
            nr = df2.iloc[top_node]
            net_pick = {
                "title": nr["title"],
                "price": float(nr["price"]),
                "rating": float(nr["rating"]),
                "link": nr["link"],
            }
            net_node_idx = int(top_node)

    # Spring layout for network graph
    pos = nx.spring_layout(G, seed=42, k=1.5)

    edge_x: list = []
    edge_y: list = []
    for u, v in G.edges():
        x0, y0 = pos[u]
        x1, y1 = pos[v]
        edge_x += [float(x0), float(x1), None]
        edge_y += [float(y0), float(y1), None]

    node_ids = list(G.nodes())
    node_x = [float(pos[n][0]) for n in node_ids]
    node_y = [float(pos[n][1]) for n in node_ids]
    node_text = []
    node_score = []
    node_size = []
    node_is_winner = []
    node_is_net = []
    degrees_map = dict(G.degree())
    for n in node_ids:
        row = df2.iloc[n]
        node_text.append(
            f"{row['title'][:50]}<br>EGP {row['price']:.0f} | {row['rating']}★<br>"
            f"Degree: {degrees_map[n]} | Score: {row['score']:.3f}"
        )
        node_score.append(float(row["score"]))
        node_size.append(8 + degrees_map[n] * 2)
        node_is_winner.append(int(n == 0))
        node_is_net.append(int(n == net_node_idx) if net_node_idx is not None else 0)

    network_graph = {
        "edge_x": edge_x,
        "edge_y": edge_y,
        "node_x": node_x,
        "node_y": node_y,
        "node_text": node_text,
        "node_score": node_score,
        "node_size": node_size,
        "node_is_winner": node_is_winner,
        "node_is_net": node_is_net,
    }

    log_fn("Network analysis done ✓")
    progress_fn(87)

    # ── HEATMAP ─────────────────────────────────────────────────────────────
    cats = sorted(df2["category"].unique().tolist())
    price_max = df2["price"].max()
    bins_count = min(10, int(price_max / 100) + 1)
    price_bins = [int(price_max * i / bins_count) for i in range(bins_count + 1)]
    price_labels = [f"{price_bins[i]}–{price_bins[i+1]}" for i in range(bins_count)]

    z_matrix = []
    for cat in cats:
        cat_df = df2[df2["category"] == cat]
        row_vals = []
        for i in range(bins_count):
            lo, hi = price_bins[i], price_bins[i + 1]
            count = int(((cat_df["price"] >= lo) & (cat_df["price"] < hi)).sum())
            row_vals.append(count)
        z_matrix.append(row_vals)

    progress_fn(93)

    # ── 3D SCATTER ──────────────────────────────────────────────────────────
    others_mask = df2.index != 0
    others_df = df2[others_mask]
    winner_sc = df2.iloc[0]

    scatter3d = {
        "others": {
            "x": others_df["price"].tolist(),
            "y": others_df["rating"].tolist(),
            "z": others_df["score"].tolist(),
            "text": [
                f"{r['title'][:60]}<br>EGP {r['price']:.0f} · {r['rating']}★ · score {r['score']:.3f}"
                for _, r in others_df.iterrows()
            ],
            "color": others_df["score"].tolist(),
        },
        "winner": {
            "x": float(winner_sc["price"]),
            "y": float(winner_sc["rating"]),
            "z": float(winner_sc["score"]),
            "text": f"{winner_sc['title'][:60]}<br>EGP {winner_sc['price']:.0f} · {winner_sc['rating']}★",
        },
    }

    # ── RANKED TABLE ────────────────────────────────────────────────────────
    ranked = []
    for _, r in df2.iterrows():
        ranked.append(
            {
                "title": r["title"],
                "price": float(r["price"]),
                "rating": float(r["rating"]),
                "score": float(r["score"]),
                "category": r["category"],
                "link": r["link"],
            }
        )

    log_fn("Done ✓")
    progress_fn(100)

    return {
        "stats": {
            "raw": len(all_data),
            "filtered": len(df2),
            "pages": max_pages,
            "avg_price": round(float(df2["price"].mean()), 2),
            "avg_rating": round(float(df2["rating"].median()), 2),
            "top_score": round(float(df2["score"].max()), 3),
        },
        "winner": {
            "title": str(winner_row["title"]),
            "price": float(winner_row["price"]),
            "rating": float(winner_row["rating"]),
            "category": str(winner_row["category"]),
            "score_pct": round(float(winner_row["score"] / score_max) * 100),
            "link": str(winner_row["link"]),
            "img_b64": img_b64,
        },
        "network_pick": net_pick,
        "network_graph": network_graph,
        "ranked": ranked,
        "heatmap": {
            "z": z_matrix,
            "x": price_labels,
            "y": cats,
        },
        "scatter3d": scatter3d,
    }
