"""Decide what two claims about the same thing say about each other.

No model runs here. Two claims that share a group_key are already known to be
about the same subject and metric; what remains is arithmetic on their values
and a comparison of their qualifiers, and both are deterministic. That makes
the system's headline capability - cross-document contradiction detection -
reproducible and explainable rather than a prompt that might behave differently
on Tuesday.

The rules, given two claims whose values are comparable:

    values equal                          -> corroborates
    values differ, exactly one qualifier
      differs and none is unstated        -> reconcilable, naming that qualifier
    values differ, no qualifier differs,
      at least one agrees, and the two
      did not come off one printed row    -> contradicts
    anything else                          -> not_comparable

`not_comparable` carries most of the weight, and three of its cases exist
because the first honest run produced 37 contradictions from a single document
and every one of them was ours, not the filing's:

- Two figures differing on several qualifiers at once are not in tension. They
  are different measurements that happen to share a topic.
- Two figures where a qualifier is unstated on one side prove nothing. The
  Economic Survey, the RBI and the IMF disagree about Indian GDP growth mostly
  by data vintage, and calling that a contradiction because one of them did not
  spell its vintage out would bury every real finding.
- Two figures printed on the same row are two columns of one table. The column
  IS the qualifier - FY24 beside FY23, consolidated beside standalone - so a
  disagreement there is a gap in extraction, not a claim about the document.

And a contradiction is an assertion that two figures are like-for-like, so it
requires at least one qualifier that actually agrees. An absence of stated
qualifiers on both sides is an absence of evidence, not evidence of sameness.
"""

import re

QUALIFIERS = ("period", "basis", "vintage", "scope")

# Relative tolerance when comparing values. 8,141.7 crore and 81,417 million are
# the same figure rounded differently, and a demo that calls those a
# contradiction is worse than useless.
TOLERANCE = 0.005

# Magnitude words, and the enum the labeller may return instead.
SCALE = {
    "": 1, "unit": 1, "count": 1,
    "thousand": 1e3, "thousands": 1e3,
    "lakh": 1e5, "lakhs": 1e5, "inr_lakh": 1e5,
    "million": 1e6, "millions": 1e6, "mn": 1e6, "inr_million": 1e6,
    "crore": 1e7, "crores": 1e7, "inr_crore": 1e7,
    "billion": 1e9, "billions": 1e9, "bn": 1e9, "inr_billion": 1e9,
}

# Units that are not amounts of money and may only be compared with themselves.
DIMENSIONLESS = {"%": "percent", "percent": "percent", "ratio": "ratio"}

CURRENCY = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR",
    "$": "USD", "us$": "USD", "usd": "USD",
}

NUMERIC = re.compile(r"-?[\d,]+(?:\.\d+)?")


def to_base(value, currency="", unit=""):
    """(magnitude, dimension) in base units, or (None, None) if not comparable.

    dimension is a currency code, "percent", "ratio", or "count" - two claims
    can only be compared within one dimension.
    """
    if value is None:
        return None, None
    text = str(value).strip()
    if not (m := NUMERIC.search(text)):
        return None, None
    try:
        number = float(m.group(0).replace(",", ""))
    except ValueError:
        return None, None

    unit_key = (unit or "").strip().lower()
    if unit_key in DIMENSIONLESS:
        return number, DIMENSIONLESS[unit_key]

    scale = SCALE.get(unit_key)
    if scale is None:
        return None, None

    dimension = CURRENCY.get((currency or "").strip().lower(), "count" if scale == 1 else "INR")
    return number * scale, dimension


def values_equal(a, b, tolerance=TOLERANCE):
    if a is None or b is None:
        return False
    if a == b:
        return True
    scale = max(abs(a), abs(b))
    return scale > 0 and abs(a - b) / scale <= tolerance


def compare_qualifiers(a, b):
    """(differing, unstated) qualifier names.

    A qualifier only *differs* when both claims state it and the statements
    disagree. One side saying nothing is not a disagreement; it is a gap, and
    the difference between those two is the difference between a real finding
    and a false one.
    """
    differing, unstated = [], []
    for q in QUALIFIERS:
        left = (a.get(q) or "").strip().lower()
        right = (b.get(q) or "").strip().lower()
        if left and right:
            if left != right:
                differing.append(q)
        elif left or right:
            unstated.append(q)
    return differing, unstated


REASONS = {
    "period": "{a} covers {av}, {b} covers {bv}",
    "basis": "{a} is {av}, {b} is {bv}",
    "vintage": "{a} is {av}, {b} is {bv}",
    "scope": "{a} is scoped to {av}, {b} to {bv}",
}


def same_row(a, b):
    """Whether two claims were mined from the same printed line of the same page."""
    return (
        a.get("doc_id") is not None
        and a.get("doc_id") == b.get("doc_id")
        and a.get("pdf_page") == b.get("pdf_page")
        and (a.get("quote") or "\x00") == (b.get("quote") or "\x01")
    )


def _reason(dimension, a, b):
    """The explanation, as an f-string. No model writes these.

    Templated rather than generated: instant, consistent, and better phrased
    than a 4B model manages.
    """
    return REASONS[dimension].format(
        a="one", b="the other", av=a.get(dimension), bv=b.get(dimension)
    )


def verdict(a, b):
    """How claim `a` relates to claim `b`. Both must already share a group_key."""
    left, left_dim = to_base(a.get("value"), a.get("currency"), a.get("unit"))
    right, right_dim = to_base(b.get("value"), b.get("currency"), b.get("unit"))

    if left is None or right is None:
        return {"type": "not_comparable", "dimension": None,
                "reason": "a value could not be read as a quantity", "confidence": 0.0}
    if left_dim != right_dim:
        return {"type": "not_comparable", "dimension": "unit",
                "reason": f"measured in {left_dim} and {right_dim}", "confidence": 0.3}

    differing, unstated = compare_qualifiers(a, b)

    if values_equal(left, right):
        note = "same value"
        if a.get("unit") and b.get("unit") and a["unit"] != b["unit"]:
            note = f"same value stated in {a['unit']} and {b['unit']}"
        return {"type": "corroborates", "dimension": None, "reason": note,
                "confidence": 0.9 if not differing else 0.7}

    if unstated:
        names = ", ".join(unstated)
        return {"type": "not_comparable", "dimension": unstated[0],
                "reason": f"values differ but {names} is not stated on both sides",
                "confidence": 0.2}

    if len(differing) == 1:
        dimension = differing[0]
        return {"type": "reconcilable", "dimension": dimension,
                "reason": _reason(dimension, a, b), "confidence": 0.85}

    if not differing:
        # Two figures printed on the same row are different columns of one
        # table - FY24 beside FY23, consolidated beside standalone - and the
        # column that separates them is exactly the qualifier the extractor
        # failed to capture. Calling that a contradiction is reporting our own
        # blind spot as a finding about the document.
        if same_row(a, b):
            return {"type": "not_comparable", "dimension": None,
                    "reason": "different columns of the same printed row",
                    "confidence": 0.1}

        # A contradiction is a claim that two figures are like-for-like. That
        # needs some qualifier actually agreeing, not merely an absence of
        # disagreement: with nothing stated on either side we know nothing.
        shared = [q for q in QUALIFIERS if (a.get(q) or "").strip()
                  and (b.get(q) or "").strip()]
        if not shared:
            return {"type": "not_comparable", "dimension": None,
                    "reason": "values differ but no qualifier is stated on either side",
                    "confidence": 0.1}

        agreed = ", ".join(f"{q} {a.get(q)}" for q in shared)
        return {"type": "contradicts", "dimension": None,
                "reason": f"same metric and same {agreed}, different values",
                "confidence": 0.8}

    return {"type": "not_comparable", "dimension": None,
            "reason": "differ on " + ", ".join(differing) + " - different measurements",
            "confidence": 0.1}


def adjudicate(members, max_pairs=200):
    """Every informative pair in one group.

    Groups are small because the group key is tight, but a pathological metric
    could still explode quadratically, so the pair count is capped.
    """
    out = []
    for i, a in enumerate(members):
        for b in members[i + 1:]:
            if len(out) >= max_pairs:
                return out
            v = verdict(a, b)
            v.update({
                "a": a.get("_id") or a.get("id"),
                "b": b.get("_id") or b.get("id"),
                "cross_document": a.get("doc_id") != b.get("doc_id"),
            })
            out.append(v)
    return out


if __name__ == "__main__":
    def claim(value, unit="crore", currency="₹", **q):
        return dict({"value": value, "unit": unit, "currency": currency}, **q)

    # Case 1: the same figure written two ways.
    v = verdict(claim("8,141.7", "crore"), claim("81,417", "million"))
    assert v["type"] == "corroborates", v

    # Case 2: a genuine contradiction - qualifiers agree, values do not.
    v = verdict(claim("8,141.7", period="FY2024", basis="consolidated", doc_id="d1"),
                claim("9,000.0", period="FY2024", basis="consolidated", doc_id="d2"))
    assert v["type"] == "contradicts", v
    assert "FY2024" in v["reason"], v

    # ... but only when something actually agrees. Nothing stated on either side
    # is an absence of evidence, not evidence of like-for-like.
    v = verdict(claim("4,552", doc_id="d1"), claim("740", doc_id="d1"))
    assert v["type"] == "not_comparable", v

    # Two figures on one printed row are two columns of a table - the column is
    # the qualifier the extractor missed, not a disagreement in the document.
    v = verdict(claim("77,908.26", period="FY2023", doc_id="d1", pdf_page=21,
                      quote="Less: Total expenses 80,235.00 77,908.26 88,249.67 85,968.83"),
                claim("85,968.83", period="FY2023", doc_id="d1", pdf_page=21,
                      quote="Less: Total expenses 80,235.00 77,908.26 88,249.67 85,968.83"))
    assert v["type"] == "not_comparable" and "same printed row" in v["reason"], v

    # Case 3: an apparent contradiction, explained by exactly one dimension.
    v = verdict(claim("8,141.7", period="FY2024", basis="consolidated"),
                claim("7,225.3", period="FY2023", basis="consolidated"))
    assert v["type"] == "reconcilable" and v["dimension"] == "period", v
    assert "FY2024" in v["reason"] and "FY2023" in v["reason"], v

    v = verdict(claim("8,141.7", period="FY2024", basis="consolidated"),
                claim("7,225.3", period="FY2024", basis="standalone"))
    assert v["type"] == "reconcilable" and v["dimension"] == "basis", v

    # Two dimensions at once is not a finding.
    v = verdict(claim("8,141.7", period="FY2024", basis="consolidated"),
                claim("7,225.3", period="FY2023", basis="standalone"))
    assert v["type"] == "not_comparable", v

    # An unstated qualifier is a gap, not a contradiction. This is the rule that
    # keeps the macroeconomy documents from generating hundreds of false ones.
    v = verdict(claim("8,141.7", period="FY2024"), claim("7,225.3"))
    assert v["type"] == "not_comparable" and v["dimension"] == "period", v

    # Different dimensions never compare.
    assert verdict(claim("12.5", "%", ""), claim("12.5", "crore"))["type"] == "not_comparable"

    # Rounding must not read as disagreement: these are one figure written two
    # ways, 0.002% apart, and calling them a contradiction would be the single
    # most embarrassing thing this system could do.
    assert values_equal(to_base("8,141.7", "₹", "crore")[0],
                        to_base("81,415.38", "₹", "million")[0])
    # A real difference still has to register.
    assert not values_equal(to_base("8,141.7", "₹", "crore")[0],
                            to_base("7,225.3", "₹", "crore")[0])

    assert to_base("1,23,456", "₹", "crore")[1] == "INR"
    assert to_base("not a number")[0] is None
    assert to_base("5", "", "furlongs")[0] is None

    pairs = adjudicate([
        {"_id": "1", "doc_id": "d1", "value": "8,141.7", "unit": "crore", "period": "FY2024"},
        {"_id": "2", "doc_id": "d2", "value": "8,141.7", "unit": "crore", "period": "FY2024"},
        {"_id": "3", "doc_id": "d2", "value": "7,225.3", "unit": "crore", "period": "FY2023"},
    ])
    assert len(pairs) == 3, pairs
    assert [p["type"] for p in pairs] == ["corroborates", "reconcilable", "reconcilable"], pairs
    assert pairs[0]["cross_document"] is True

    print("verdict self-check ok")
