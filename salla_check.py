r"""
Salla page check (READ ONLY - nothing is saved, nothing is clicked except scrolling)
===================================================================================
Why (2026-11-17): oe_scraper.py / rf_scraper.py read 0 product cards on the Fragrances / Perfumes pages
(Gifts page of Oud Elite still gives 20). This prints what IS on those pages in your Chrome, so the scrapers can be
fixed to read the cards again.

Same Chrome as the scrapers (port 9222) must be open and VISIBLE (not minimized).
  python salla_check.py
Send the whole output to Claude.
"""
from playwright.sync_api import sync_playwright

CDP_URL = "http://localhost:9222"
PAGES = [
    ("Oud Elite Fragrances (0 cards)", "https://ae.oudelite.com/en/fragrances/c435056374"),
    ("Oud Elite Gifts (20 cards - works)", "https://ae.oudelite.com/en/gift-sets/c218966268"),
    ("Reef Perfumes (0 cards)", "https://ae.reefperfumes.com/en/-/c585180698"),
]

JS_INFO = r"""() => {
  const count = s => { try { return document.querySelectorAll(s).length; } catch (e) { return 'bad selector'; } };
  const tags = {};
  document.querySelectorAll('*').forEach(e => {
    const t = e.tagName.toLowerCase();
    if (t.includes('-') && (t.includes('product') || t.includes('salla'))) tags[t] = (tags[t] || 0) + 1;
  });
  const links = Array.from(document.querySelectorAll('a[href]')).map(a => a.href)
    .filter(h => /\/p\d+|\/[A-Z]\d{6,}|product/i.test(h));
  const body = (document.body && document.body.innerText) || '';
  return {
    title: document.title, url: location.href, lang: document.documentElement.lang,
    cards_scraper_selector: count('custom-salla-product-card, salla-product-card'),
    salla_products_list: count('salla-products-list'),
    product_card_class: count('.product-card'),
    components_with_product_or_salla: tags,
    product_like_links: links.length, first_links: links.slice(0, 5),
    has_AED_text: (body.match(/AED|د\.إ/g) || []).length,
    captcha_or_block: /captcha|verify you are human|cloudflare|access denied|too many requests/i.test(body),
    empty_text: /no products|لا توجد منتجات|لايوجد منتجات/i.test(body),
    body_start: body.replace(/\s+/g, ' ').slice(0, 400),
  };
}"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.connect_over_cdp(CDP_URL)
        page = browser.contexts[0].new_page()
        page.bring_to_front()
        for label, url in PAGES:
            print(f"\n===== {label}\n{url}", flush=True)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=90000)
            except Exception as ex:
                print(f"  could not open: {str(ex).splitlines()[0][:200]}", flush=True)
                continue
            for when, wait in (("after 5 s", 5000), ("after 15 s + scrolling", 10000)):
                if "scrolling" in when:
                    for _ in range(5):
                        page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
                        page.wait_for_timeout(1500)
                page.wait_for_timeout(wait)
                info = page.evaluate(JS_INFO)
                print(f"  --- {when}", flush=True)
                for k, v in info.items():
                    print(f"  {k}: {v}", flush=True)
        page.close()
    print("\nDone - nothing was saved. Send this output to Claude.", flush=True)


if __name__ == "__main__":
    main()
