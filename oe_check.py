r"""
Oud Elite check (run ONCE on your PC:  python oe_check.py   -> send the printed lines to Claude)
Reads only - saves nothing. Finds the Salla store number, category links and tries Salla's product API.
"""
import re
import requests

H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
BASE = "https://ae.oudelite.com"


def show(title, value):
    print(f"[OE-CHECK] {title}: {value}")


# 1. one product page (Man Desert, AED 85)
r = requests.get(f"{BASE}/en/E0301010101-man-desert", headers=H, timeout=60)
html = r.text
show("product page", f"HTTP {r.status_code}, {len(html)} characters, has '85': {'85' in html}")
ids = set()
for pat in [r'store_id["\']?\s*[:=]\s*["\']?(\d{5,})', r'"store"\s*:\s*\{\s*"id"\s*:\s*"?(\d{5,})',
            r'Store-Identifier["\']?\s*[:=]\s*["\']?(\d{5,})', r'"store_id"\s*:\s*"?(\d{5,})',
            r'store[_.]id["\']?\s*[:=]\s*["\']?(\d{5,})', r'cdn\.salla\.sa/(\w+)/']:
    found = sorted(set(re.findall(pat, html)))[:5]
    if found:
        show(f"pattern {pat[:25]}", found)
        ids.update(f for f in found if f.isdigit())
show("api links in page", sorted(set(re.findall(r'https://api\.salla\.dev/[A-Za-z0-9/._-]+', html)))[:10])
for ld in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S)[:3]:
    show("ld+json", ld.strip()[:400])
for m in re.findall(r'.{80}"price".{120}', html)[:3]:
    show("price text", m)

# 2. home page -> category links
home = requests.get(f"{BASE}/en", headers=H, timeout=60).text
cats = sorted(set(re.findall(r'href="(https://ae\.oudelite\.com/en/[^"#?]+)"', home)))
show("home links", len(cats))
for c in cats[:60]:
    print("    ", c)

# 3. Salla storefront API with each store number found
for sid in sorted(ids)[:3]:
    for url in ["https://api.salla.dev/store/v1/products?per_page=2",
                "https://api.salla.dev/store/v1/products?page=1&per_page=2"]:
        try:
            a = requests.get(url, headers={**H, "Store-Identifier": sid, "Accept": "application/json",
                                           "currency": "AED", "Accept-Language": "en"}, timeout=60)
            show(f"API store {sid} {url[-20:]}", f"HTTP {a.status_code} {a.text[:500]}")
        except requests.RequestException as ex:
            show(f"API store {sid}", f"error {ex}")
print("[OE-CHECK] done - please send everything above")
