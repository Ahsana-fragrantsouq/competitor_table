r"""
Oud Elite check IN YOUR CHROME (reads only - saves nothing). Send everything it prints to Claude.

1. Start Chrome the same way as for the Ounass scraper (if it is not open already):
   "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir=C:\ff-scraper\chrome-profile
2. In that Chrome open https://ae.oudelite.com/en and click Fragrances, then copy that address; same for Gifts & Accessories
3. Run (put the two addresses in quotes):
   python oe_check_chrome.py "FRAGRANCES-ADDRESS" "GIFTS-ADDRESS"
"""
import sys
import json
from playwright.sync_api import sync_playwright

PRODUCT = "https://ae.oudelite.com/en/E0301010101-man-desert"
JS_INFO = """() => {
  const out = {};
  try { out.salla_store = window.salla && salla.config ? JSON.stringify(salla.config.get('store')).slice(0, 300) : null; } catch (e) { out.salla_store = 'err ' + e; }
  try { out.salla_api = window.salla && salla.config ? String(salla.config.get('api_url') || salla.config.get('api')) : null; } catch (e) {}
  const tags = {};
  document.querySelectorAll('*').forEach(el => { const t = el.tagName.toLowerCase(); if (t.includes('-')) tags[t] = (tags[t] || 0) + 1; });
  out.custom_tags = tags;
  const card = document.querySelector('custom-salla-product-card, salla-product-card, .product-entry, .s-product-card-entry');
  if (card) { out.card_tag = card.tagName; out.card_attrs = Array.from(card.attributes).map(a => a.name + '=' + a.value.slice(0, 200)); out.card_text = card.innerText.slice(0, 200); out.card_html = card.outerHTML.slice(0, 600); }
  out.product_links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href).filter(h => /\\/en\\/[A-Za-z0-9]+-/.test(h)).slice(0, 8);
  out.next_page = Array.from(document.querySelectorAll('a[href*="page="], a[rel=next], salla-infinite-scroll')).map(a => a.outerHTML.slice(0, 200)).slice(0, 3);
  const ld = Array.from(document.querySelectorAll('script[type="application/ld+json"]')).map(s => s.textContent.slice(0, 400));
  out.ld_json = ld.slice(0, 2);
  return out;
}"""
JS_API = """async (sid) => {
  const r = await fetch('https://api.salla.dev/store/v1/products?per_page=2', {headers: {'Store-Identifier': String(sid), 'Accept': 'application/json', 'currency': 'AED', 'Accept-Language': 'en'}});
  return r.status + ' ' + (await r.text()).slice(0, 700);
}"""

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://localhost:9222")
    page = browser.contexts[0].new_page()
    for url in [PRODUCT] + sys.argv[1:3]:
        page.goto(url, wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(6000)                       # let the shop draw its products
        info = page.evaluate(JS_INFO)
        print(f"\n[OE-CHROME] ===== {url}  (title: {page.title()[:60]})")
        for k, v in info.items():
            print(f"[OE-CHROME] {k}: {json.dumps(v, ensure_ascii=False)[:900]}")
        sid = None
        try:
            sid = json.loads(info.get("salla_store") or "{}").get("id")
        except Exception:
            pass
        if sid:
            try:
                print(f"[OE-CHROME] API test store {sid}: {page.evaluate(JS_API, sid)}")
            except Exception as ex:
                print(f"[OE-CHROME] API test error: {ex}")
    page.close()
print("\n[OE-CHROME] done - please send everything above")
