"""Self-check for the two pieces of logic that are not the model's job:
quote grounding and candidate blocking.  Run: python test_pipeline.py"""

from pipeline import candidates, check_quote

PAGE = "Revenue from operations was Rs 7,676 crore in FY2024,\n  up 12% year on year."

# grounding tolerates the whitespace and case damage PDF extraction causes
V = "7,676"
assert check_quote("Revenue from operations was Rs 7,676 crore in FY2024", V, PAGE) == (True, "")
assert check_quote("revenue   from operations  was Rs 7,676 crore", V, PAGE) == (True, "")
assert check_quote("Revenuefromoperations was Rs7,676crore", V, PAGE) == (True, "")
# words of the page, read in an order the page does not use: kept, and marked
ok, note = check_quote("FY2024 revenue: Rs 7,676 crore", V, PAGE)
assert ok and "assembled" in note, (ok, note)
# a plausible-sounding figure that is NOT on the page is still caught
ok, note = check_quote("Revenue from operations was Rs 8,100 crore", "8,100", PAGE)
assert not ok and "not on this page" in note, (ok, note)
# a real line of the page that does not contain the figure it is offered for
ok, note = check_quote("Revenue from operations was", V, PAGE)
assert not ok and "does not contain the value" in note, (ok, note)
# text facts have no number to check, and are unaffected
assert check_quote("up 12% year on year", "up year on year", PAGE) == (True, "")
# and a scrap too small to prove anything is rejected for its own stated reason
assert check_quote("Rs", V, PAGE)[1].startswith("quote too short")

fact = lambda i, s, a: {"id": i, "subject": s, "attribute": a}
new = [fact(1, "Delhivery", "revenue from operations")]
old = [fact(2, "Delhivery", "revenue from operations"),   # same wording -> paired
       fact(3, "Delhivery", "number of employees")]       # unrelated -> not paired
pairs, unjudged = candidates(new, old)
assert [b["id"] for _, b in pairs] == [2], pairs
assert unjudged == 0

print("ok")
