"""Machine-readable feeds a site publishes about itself: WordPress posts (/wp-json) and
the Shopify product catalogue (/products.json). Fetched through the crawler, so
robots.txt, delays and the page cache apply as for any page."""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from datetime import date, datetime
from urllib.parse import urljoin

from ...schema import ShopCatalogue, SitePost
from .fetch import Crawler
from .parse import Page

MAX_POSTS = 20
_TAG = re.compile(r"<[^>]+>")


def _text(value: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG.sub(" ", value or ""))).strip()


def _date(value: str | None) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date() if value else None
    except ValueError:
        return None


def _json(crawler: Crawler, url: str):
    text = crawler.fetch_text(url)
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None  # an HTML error page, a login wall...


def wordpress_posts(crawler: Crawler, site_url: str) -> list[SitePost]:
    url = urljoin(site_url, f"/wp-json/wp/v2/posts?per_page={MAX_POSTS}&_fields=date,link,title,excerpt")
    data = _json(crawler, url)
    if not isinstance(data, list):
        return []
    posts = []
    for p in data:
        if not isinstance(p, dict):
            continue
        title = _text((p.get("title") or {}).get("rendered"))
        if title:
            posts.append(SitePost(title=title, url=p.get("link"), published_on=_date(p.get("date")),
                                  excerpt=_text((p.get("excerpt") or {}).get("rendered"))[:400] or None))
    return posts


def shopify_catalogue(crawler: Crawler, site_url: str) -> ShopCatalogue | None:
    data = _json(crawler, urljoin(site_url, "/products.json?limit=250"))
    products = data.get("products") if isinstance(data, dict) else None
    if not products:
        return None
    prices = []
    for p in products:
        for v in p.get("variants") or []:
            try:
                prices.append(float(v.get("price")))
            except (TypeError, ValueError):
                pass
    newest = sorted(products, key=lambda p: p.get("published_at") or "", reverse=True)
    return ShopCatalogue(
        products_listed=len(products),
        product_types=dict(Counter(p["product_type"] for p in products if p.get("product_type")).most_common(20)),
        vendors=dict(Counter(p["vendor"] for p in products if p.get("vendor")).most_common(10)),
        price_min=min(prices) if prices else None, price_max=max(prices) if prices else None,
        newest=[p.get("title") for p in newest[:20] if p.get("title")])


def feeds_material(posts: list[SitePost], shop: ShopCatalogue | None, site_url: str) -> list[Page]:
    pages = []
    if posts:
        lines = [f"{p.published_on or 'undated'}: {p.title}" + (f". {p.excerpt}" if p.excerpt else "")
                 for p in posts]
        pages.append(Page(url=urljoin(site_url, "/wp-json/wp/v2/posts"), kind="posts", status_code=200,
                          title="Latest posts on the company's own site", text="\n".join(lines)))
    if shop:
        lines = [f"The online shop lists {shop.products_listed} products"
                 + (f", priced {shop.price_min:g} to {shop.price_max:g}." if shop.price_min is not None else ".")]
        if shop.product_types:
            lines.append("Product types: " + ", ".join(f"{t} ({n})" for t, n in shop.product_types.items()))
        if shop.vendors:
            lines.append("Brands: " + ", ".join(shop.vendors))
        lines.append("Newest products: " + "; ".join(shop.newest))
        pages.append(Page(url=urljoin(site_url, "/products.json"), kind="shop", status_code=200,
                          title="The company's online shop catalogue", text="\n".join(lines)))
    return pages
