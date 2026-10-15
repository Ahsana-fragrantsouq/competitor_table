"""
Suggested price - THE rule, in one place (plain functions, no database, no website).
=====================================================================================
Import it wherever a suggested price is needed:

    from suggested_price import calculate_suggested_price
    calculate_suggested_price(cheapest=1620, rate=3, lop=1500, mop=1700)     # -> 1571

Step 4: Calculate the suggested price
    Suggested Price = Cheapest in-stock competitor price - X%
        X = discount rate from Settings -> Auto discount rate (discount_rate.py), 3% now
        in maths terms: Suggested Price = Cheapest price x (100 - X) / 100
        rounding: to the nearest whole AED (0.5 goes up: 1571.50 -> 1572)

Step 5: LOP / MOP (price_limits.py, typed on each product card)
    Suggested price less than or equal to LOP     -> LOP
    Suggested price greater than or equal to MOP  -> MOP
    otherwise                                     -> unchanged
    Final rule: LOP <= Suggested Price <= MOP        (empty LOP / MOP = no limit)

    No competitor in stock -> NO suggested price (None). LOP / MOP never invent a price.

Functions (SECTION numbers below):
  SECTION 1  settings                 -> DECIMALS (rounding)
  SECTION 2  raw_suggested_price()    -> step 4 only: cheapest - X%, rounded
  SECTION 3  apply_lop_mop()          -> step 5 only: keep a price between LOP and MOP
  SECTION 4  calculate_suggested_price() -> step 4 + step 5 together (use this one normally)
  SECTION 5  explain_suggested_price()   -> same, but also says WHY (for logs / the page)
  SECTION 6  self-test                -> python suggested_price.py   (checks the examples)

Used by:
  app.py least_for()        -> one product (after OK / Not OK, after LOP / MOP Save)
  price_limits.py           -> apply_lop_mop() for one product
  app.py SUGGEST_SQL        -> all products at once in Postgres, SAME rule written in SQL
                               (price_limits.clamp_sql); if you change the rule here, change it there too.
"""

from decimal import Decimal, ROUND_HALF_UP


# ============================================================ SECTION 1: settings
DECIMALS = 0          # 0 = whole AED (1571.40 -> 1571). 2 = keep fils (1571.40). Same as app.py SUGGEST_DECIMALS.


def log(*a):
    """Print to the Render log (lines start with [SUGGESTED])."""
    print(*a, flush=True)


def _number(v, name):
    """Turn v into a float, or None when empty. Wrong text -> ValueError with a clear message."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {v!r}")


# ============================================================ SECTION 2: step 4 - cheapest competitor - X%
def raw_suggested_price(cheapest, rate):
    """Cheapest in-stock competitor price - X%, rounded to DECIMALS.
        cheapest : cheapest IN-STOCK competitor price (None = no competitor in stock)
        rate     : X, the Auto discount rate in % (e.g. 3). None = no rate saved yet.
    Returns the price, or None when there is no competitor in stock or no rate (nothing is guessed).
    Example: raw_suggested_price(1620, 3) -> 1620 x 97 / 100 = 1571.40 -> 1571"""
    cheapest, rate = _number(cheapest, "cheapest"), _number(rate, "rate")
    if cheapest is None or cheapest <= 0:
        return None                                        # no competitor in stock -> no suggested price
    if rate is None:
        return None                                        # no discount rate saved -> no suggested price
    exact = Decimal(str(cheapest)) * (Decimal(100) - Decimal(str(rate))) / Decimal(100)
    # half up, like Postgres ROUND: 1571.50 -> 1572
    rounded = exact.quantize(Decimal(1).scaleb(-DECIMALS), rounding=ROUND_HALF_UP)
    return float(rounded)


# ============================================================ SECTION 3: step 5 - LOP / MOP
def apply_lop_mop(price, lop=None, mop=None):
    """Keep a suggested price between LOP and MOP.
        price <= LOP -> LOP
        price >= MOP -> MOP
        otherwise    -> price unchanged
    Empty LOP / MOP (None or "") = no limit. price None stays None (LOP / MOP never invent a price).
    LOP higher than MOP is not allowed -> ValueError (the Save on the card refuses it too)."""
    price, lop, mop = _number(price, "price"), _number(lop, "LOP"), _number(mop, "MOP")
    if lop is not None and mop is not None and lop > mop:
        raise ValueError(f"LOP ({lop}) cannot be higher than MOP ({mop})")
    if price is None:
        return None                                        # no suggested price -> nothing to limit
    if lop is not None and price <= lop:
        return lop                                         # less than or equal to LOP -> LOP
    if mop is not None and price >= mop:
        return mop                                         # greater than or equal to MOP -> MOP
    return price                                           # between LOP and MOP -> unchanged


# ============================================================ SECTION 4: step 4 + step 5 together
def calculate_suggested_price(cheapest, rate, lop=None, mop=None):
    """The final suggested price for ONE product. Use this one normally.
        cheapest : cheapest IN-STOCK competitor price (None = none in stock)
        rate     : Auto discount rate X in %  (Settings -> Auto discount rate)
        lop, mop : the product's LOP / MOP (None = no limit)
    Returns the final price (LOP <= price <= MOP), or None when there is no competitor in stock / no rate.
    Example: calculate_suggested_price(1620, 3, lop=1600) -> raw 1571 <= 1600 -> 1600"""
    return apply_lop_mop(raw_suggested_price(cheapest, rate), lop, mop)


# ============================================================ SECTION 5: same, with the reason (for logs / page)
def explain_suggested_price(cheapest, rate, lop=None, mop=None):
    """Like calculate_suggested_price, but returns a dict that also says what happened:
        {"raw": 1571.0, "final": 1600.0, "rule": "raised to LOP", "text": "1620 - 3% = 1571 -> LOP 1600"}
    rule = "no competitor in stock" | "no discount rate" | "normal" | "raised to LOP" | "lowered to MOP" """
    raw = raw_suggested_price(cheapest, rate)
    final = apply_lop_mop(raw, lop, mop)
    if _number(cheapest, "cheapest") in (None, 0):
        rule, text = "no competitor in stock", "no competitor in stock -> no suggested price"
    elif raw is None:
        rule, text = "no discount rate", "no Auto discount rate saved -> no suggested price"
    elif final != raw and lop is not None and final == _number(lop, "LOP"):
        rule, text = "raised to LOP", f"{cheapest} - {rate}% = {raw:g} -> LOP {final:g}"
    elif final != raw:
        rule, text = "lowered to MOP", f"{cheapest} - {rate}% = {raw:g} -> MOP {final:g}"
    else:
        rule, text = "normal", f"{cheapest} - {rate}% = {raw:g}"
    log(f"[SUGGESTED] {text} (LOP {lop}, MOP {mop})")
    return {"raw": raw, "final": final, "rule": rule, "text": text}


# ============================================================ SECTION 6: self-test (python suggested_price.py)
if __name__ == "__main__":
    tests = [   # (cheapest, rate, lop, mop, expected final)
        (1620, 3, None, None, 1571),     # 1620 x 97 / 100 = 1571.40 -> 1571
        (1620, 3, 1500, 1700, 1571),     # between LOP and MOP -> unchanged
        (1500, 3, 1500, 1700, 1500),     # 1455 <= LOP 1500 -> 1500
        (1850, 3, 1500, 1700, 1700),     # 1794.50 -> 1795 >= MOP 1700 -> 1700
        (1546.39, 3, 1500, None, 1500),  # 1500 <= LOP 1500 (equal) -> 1500
        (100, 3, None, None, 97),        # the example on the Settings page
        (None, 3, 1500, 1700, None),     # no competitor in stock -> no suggested price
        (1620, None, None, None, None),  # no discount rate saved -> no suggested price
    ]
    ok = True
    for cheapest, rate, lop, mop, want in tests:
        got = calculate_suggested_price(cheapest, rate, lop, mop)
        good = got == want
        ok &= good
        print(f"{'OK  ' if good else 'FAIL'} cheapest {cheapest}, X {rate}%, LOP {lop}, MOP {mop} -> {got} (expected {want})")
    try:
        apply_lop_mop(1600, lop=1700, mop=1600)
        print("FAIL LOP > MOP was accepted"); ok = False
    except ValueError as ex:
        print(f"OK   LOP > MOP refused: {ex}")
    print("ALL OK" if ok else "SOME TESTS FAILED")