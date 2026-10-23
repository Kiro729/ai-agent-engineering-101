"""Week 07, LAB 1 -- the market buyer: pick a shop from reviews and memory, trade, review.

The kit hands this program a shopping list file, an item and a budget, and
reads one JSON line back:

    python market_buyer.py --shops .market/shops.json --item lamp --budget 120
    ...
    {"shop": "<url from the list>", "receipt": {...} | null, "stars": 4, "review": "..."}

The negotiation itself is week-06's buyer, reused unchanged. What is new is
*which* shop to walk into and *what* to say about it afterwards. The design is
the one used across this submission: hard rules in code, judgement to the model.

    Selecting a shop (choose a shop from reviews + memory)
      code      drops shops that are clearly bad -- verified-looking bad
                reviews, or ones my own memory caught overcharging -- whenever
                a safer shop exists. This is the guarantee the review test
                checks: the bad shop is never even contacted.
      code      scores the survivors. My own past trades (first-hand, cannot be
                faked) outweigh strangers' reviews; a cheaper shop scores
                higher; a shop I have never tried gets a small nudge so the
                buyer still explores instead of locking onto one name.
      harness   when two or more survive, reads their review *text* (where the
                price and honesty hints live) and makes the final pick. One
                survivor skips the call.

    The memory (how the buyer remembers)
      Keyed by shop name, because that is all the kit guarantees. A shop can
      rename, so each entry also keeps a short fingerprint of the shop's card
      (its description + skills, not the mutable nickname) and its last url.
      A known name whose fingerprint no longer matches is treated as a stranger
      -- the old reputation is not handed to whoever is using the name now --
      except for its wrong receipts, which stay with the name for good.
      There is no public key in the list the kit gives us, so this is the best
      available identity check, not a cryptographic one.
      Round 0 ran week-06's buyer, which has no memory; the wrong receipts it
      caught are read back from the kit's log into memory (challenge 2).

    The review (leave a review either way)
      The harness writes it, so the sentence is natural, but it is fed the real
      numbers (price, budget, rounds, whether the receipt matched) and must
      answer in a fixed JSON shape. Code guarantees the shape: stars is always
      a whole 1-5 and the sentence is never empty, falling back to a rule when
      the model wanders.
"""
import argparse
import hashlib
import json
import re
from pathlib import Path

import buyer          # week-06 negotiation, reused
import harness

BAD_STARS = 2.5       # a trust-weighted average below this, with a safer shop around, is avoided
EXPLORE = 0.4         # nudge for a shop never traded with, so a new shop can win a first customer
CROWD = 0.06          # mild penalty per review: among good shops, lean to the less-crowded (hungrier) one
SHORTLIST = 3         # how many survivors the harness chooses among
RECENT = 5            # a shop that broke off 2 of my last 5 trades with it (hung, crashed) is avoided


# --------------------------------------------------------------- the memory

def mem_path(shops_file: str) -> Path:
    return Path(shops_file).parent / "memory.json"


def load_mem(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:                                   # noqa: BLE001  (missing or corrupt -> start fresh)
        return {}


def save_mem(path: Path, mem: dict) -> None:
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(mem, ensure_ascii=False, indent=1), encoding="utf-8")


def fingerprint(card: dict) -> str:
    """A short, stable-ish id for a shop from the parts of its card that the
    nickname cannot change: its description and the ids of its skills."""
    desc = (card or {}).get("description", "")
    skills = ",".join(sorted(s.get("id", "") for s in (card or {}).get("skills") or []))
    return hashlib.sha256(f"{desc}|{skills}".encode("utf-8")).hexdigest()[:10]


# ------------------------------------------- what Round 0 taught (challenge 2)
# Round 0 was run by week-06's buyer.py, which keeps no memory. It did catch
# shops whose receipt did not match the deal ("the shop closed but its receipt
# is wrong: receipt price 308.0 is above the 268 that was offered"), but that
# lesson only exists in the kit's log. Left there, a shop that cheated me stays
# a candidate as long as strangers rate it well -- elm-street did, at 3.5 stars.
# So the log is read back into memory: first-hand evidence outranks hearsay.

KIT_LOG = Path(__file__).resolve().parent / "logs" / "market-kit.log"
_ORDER = re.compile(r"\[order \S+\] (\w+), budget (\d+), round (\d+)")
_SHOP = re.compile(r"\[kit\] shop for this order: (\S+) ")
_TASK = re.compile(r"\[buyer\] -> .* taskId=(\w+)")
_WRONG = re.compile(r"the shop closed but its receipt is wrong: (.*)")
_OVER = re.compile(r"receipt price ([\d.]+) is above the (\d+) that was offered")


def import_round0(mem: dict, log_path: Path = KIT_LOG) -> int:
    """Fold every wrong receipt week-06's buyer caught in Round 0 into memory,
    once each (keyed by the A2A task id). Returns how many were new."""
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    known = {t.get("task") for e in mem.values() for t in e.get("trades", [])}
    added, cur = 0, {}
    for line in lines:                                  # the kit runs one order at a time
        if m := _ORDER.search(line):
            cur = {"item": m[1], "budget": int(m[2])}
        elif m := _SHOP.search(line):
            cur["shop"] = m[1]
        elif m := _TASK.search(line):
            cur["task"] = m[1]
        elif (m := _WRONG.search(line)) and cur.get("shop") and cur.get("task") not in known:
            over = _OVER.search(m[1])
            e = mem.setdefault(cur["shop"], {"fp": None, "trades": []})   # card never seen: fp unknown
            e["trades"].append({"item": cur["item"], "budget": cur["budget"],
                                "price": int(float(over[1])) if over else None,
                                "agreed": int(over[2]) if over else None, "rounds": None,
                                "deal": True, "honest": False, "stars": 1,
                                "task": cur["task"], "src": "round-0 kit log"})
            e["agg"] = aggregate(e["trades"])
            known.add(cur["task"])
            added += 1
            buyer.log(f"[market_buyer] memory: {cur['shop']} issued a wrong receipt in Round 0 "
                      f"(task {cur['task']}: {m[1]})")
    return added


# ------------------------------------------------------------- the selection

def weighted_stars(reviews: list) -> float | None:
    """Average stars, weighting a review by how much it can be trusted: a later
    round counts more (Round 2 reviews are the signed ones) and a newer review
    counts more. None when there are no usable reviews."""
    num = den = 0.0
    for i, r in enumerate(reviews or []):
        s = r.get("stars")
        if not isinstance(s, (int, float)):
            continue
        w = (1 + r.get("round", 0)) * (0.9 ** i)        # round weight, recency decay (newest first)
        num += s * w
        den += w
    return (num / den) if den else None


def why_bad(ws: float | None, past: dict | None) -> str | None:
    """Why a shop is to be avoided when a safer one exists, or None. My own
    memory is checked first: a wrong receipt I saw myself outweighs any number
    of good reviews from buyers I cannot check."""
    agg = (past or {}).get("agg", {})
    if agg.get("cheated"):
        return f"my memory: {agg['cheated']} wrong receipt(s)"
    if agg.get("recent_failed", 0) >= 2:
        return f"my memory: {agg['recent_failed']} of my last {RECENT} trades there broke off"
    if ws is not None and ws < BAD_STARS:
        return f"reviews: {ws:.1f} stars"
    return None


def score(name: str, ws: float | None, past: dict | None, mem: dict, n_reviews: int) -> float:
    s = ws if ws is not None else 3.0                   # no reviews -> neutral, not punished
    agg = (past or {}).get("agg") or {}
    if agg.get("deals"):
        # First-hand price I actually paid, as a fraction of budget: cheaper is
        # better. Weighted strongly because I measured it myself.
        s += (1 - agg.get("avg_price_ratio", 1.0)) * 2.0
    if name not in mem:
        s += EXPLORE                                    # never tried -> a small push to explore
    # Contrarian: when every buyer reads the same reviews and piles onto the one
    # most-reviewed shop, that shop has no reason to discount. A mild penalty for
    # the crowd leans toward a solid but hungrier shop. It never rescues a bad
    # one -- those are already filtered out.
    s -= CROWD * n_reviews
    return s


PICK_PROMPT = """You are buying one {item} with a budget of {budget}.
Pick the shop most likely to sell it cheaply and honestly. What is known:

{rows}

Reply with ONE line: the exact name of one shop above."""


def select(shops: list, mem: dict, item: str, budget: int):
    """-> chosen shop dict. Code filters and scores; the harness breaks a tie."""
    scored = []
    for sh in shops:
        ws = weighted_stars(sh.get("reviews"))
        past = mem.get(sh["name"])
        scored.append({"shop": sh, "ws": ws, "bad": why_bad(ws, past),
                       "score": score(sh["name"], ws, past, mem, len(sh.get("reviews") or []))})

    survivors = [x for x in scored if not x["bad"]] or scored      # never leave zero options
    survivors.sort(key=lambda x: x["score"], reverse=True)
    shortlist = survivors[:SHORTLIST]
    for x in scored:
        buyer.log(f"[market_buyer] {x['shop']['name']}: stars {x['ws']}, score {x['score']:.2f}"
                  + (f" [avoided: {x['bad']}]" if x["bad"] and len(survivors) < len(scored) else ""))

    if len(shortlist) == 1:
        return shortlist[0]["shop"]

    rows = []
    for x in shortlist:
        sh, past = x["shop"], mem.get(x["shop"]["name"])
        mine = ""
        if past and past.get("agg", {}).get("deals"):
            a = past["agg"]
            mine = f" | i paid ~{a['avg_price_ratio']:.0%} of budget over {a['deals']} deal(s)"
        recent = (sh.get("reviews") or [{}])[0].get("review", "")
        rows.append(f"{sh['name']} | stars {x['ws'] if x['ws'] is not None else 'none'}{mine} | recent: {recent}")
    answer = (harness.ask(PICK_PROMPT.format(item=item, budget=budget, rows="\n".join(rows))) or "").strip()
    for x in shortlist:                                 # the model must name one of the shortlist
        if x["shop"]["name"].lower() in answer.lower():
            buyer.log(f"[market_buyer] harness picked {x['shop']['name']}")
            return x["shop"]
    buyer.log(f"[market_buyer] harness gave {answer!r}, falling back to top score")
    return shortlist[0]["shop"]


# ---------------------------------------------------------------- the trade

def receipt_data(t):
    """The shop's receipt exactly as it arrived -- the whole data Part, not just
    the price. The market matches it field for field, so it is passed through
    unchanged."""
    for a in (t or {}).get("artifacts", []):
        for p in a.get("parts", []):
            d = p.get("data")
            if isinstance(d, dict) and "price" in d:
                return d
    return None


def anchor_from(past: dict | None, item: str, budget: int):
    """The lowest price this shop ever gave me for this item, capped at the
    budget -- what to open near. None when I have no such deal to remember.
    (Per item: a lamp's price is no anchor for a bookshelf. Live, the buyer
    once opened a bookshelf at a remembered lamp price of 100.)"""
    if not past:
        return None
    lows = [t["price"] for t in past.get("trades", [])
            if t.get("deal") and t.get("price") and t.get("item") == item]
    return int(min(min(lows), budget)) if lows else None


def trade(url: str, item: str, budget: int, past: dict | None = None) -> dict:
    """Walk into one shop and run week-06's negotiation. Returns the facts the
    memory and the review need -- also when the shop hangs or breaks off, which
    must cost the order its deal, never its result line."""
    r = {"deal": False, "receipt": None, "price": None, "rounds": 0, "honest": None, "fp": None, "error": None}
    try:
        c = buyer.Client(url)
        r["fp"] = fingerprint(c.card)
        anchor = anchor_from(past, item, budget)    # memory anchoring: open near a remembered low
        t = c.send(f"I want to buy a {item}")
        if t is None or buyer.state(t) != "TASK_STATE_INPUT_REQUIRED":
            buyer.log(f"[market_buyer] {url} did not open a negotiation")
            return r

        task_id, hist, prev_ask, last_offer = t["id"], [], None, None
        for rounds in range(1, buyer.MAX_ROUNDS + 1):
            r["rounds"] = rounds
            ask = buyer.last_number(t)
            stalled = (ask is not None and prev_ask is not None and ask > budget and ask >= prev_ask)
            if rounds == 1 and anchor is not None and ask is not None and ask > anchor:
                what, price = "offer", anchor   # start from what memory says it will take
                buyer.log(f"[market_buyer] opening at remembered low {anchor} (shop asked {ask})")
            else:
                what, price = buyer.choose(item, budget, ask, rounds, hist, stalled)
            if what == "walk" or price > budget:        # the budget guarantee is in week-06's code
                break
            hist.append((ask, price))
            prev_ask, last_offer = ask, price
            t = c.send(str(price), task_id)
            if t is None or buyer.state(t) != "TASK_STATE_INPUT_REQUIRED":
                break

        if buyer.state(t) == "TASK_STATE_COMPLETED":
            r["deal"], r["receipt"] = True, receipt_data(t)
            r["price"], problem = buyer.settled(t, last_offer if last_offer is not None else budget, item)
            r["honest"] = problem is None
            if problem:
                buyer.log(f"[market_buyer] receipt problem: {problem}")
        elif buyer.state(t) == "TASK_STATE_INPUT_REQUIRED":
            c.cancel(task_id)
    except Exception as e:                              # noqa: BLE001  (a hung or broken shop)
        r["deal"], r["receipt"], r["error"] = False, None, type(e).__name__
        buyer.log(f"[market_buyer] trade with {url} broke off after {r['rounds']} round(s): "
                  f"{type(e).__name__}")
    return r


# --------------------------------------------------------------- the review

REVIEW_PROMPT = """You just finished shopping for a {item} (budget {budget}). Write a one-sentence
review for other buyers, naming the real numbers.

What happened: {facts}

A no deal only because the budget was below the shop's price is not the shop's fault;
keep such a review neutral rather than negative. Paying the full budget with no
discount is not a good deal either; rate it 3 or below.

Reply with ONE line of JSON and nothing else:
{{"stars": <whole number 1-5>, "review": "<one sentence: the price vs budget, the rounds, and whether the receipt matched>"}}"""


def derive_stars(r: dict) -> int:
    """A rule for stars when the harness answer is unusable, from the outcome alone."""
    if not r["deal"]:
        return 3                                        # no deal is not the shop's fault by itself
    if r["honest"] is False:
        return 1                                        # a wrong receipt is the worst case
    ratio = (r["price"] or 0) / max(1, r["budget"])
    return 5 if ratio <= 0.75 else 4 if ratio <= 0.9 else 3


def make_review(r: dict) -> tuple[int, str]:
    if r.get("error"):                                  # the shop stopped answering: its fault, and nothing to word
        return 2, (f"No deal on the {r['item']} (budget {r['budget']}): the shop stopped answering "
                   f"after {r['rounds']} round(s).")
    facts = {"deal": r["deal"], "price": r["price"], "budget": r["budget"],
             "rounds": r["rounds"], "receipt_matched": r["honest"]}
    out = harness.ask(REVIEW_PROMPT.format(item=r["item"], budget=r["budget"], facts=json.dumps(facts)))
    stars, review = None, None
    m = re.search(r"\{.*\}", out or "", re.DOTALL)      # the model sometimes wraps the JSON in prose
    if m:
        try:
            got = json.loads(m.group())
            if isinstance(got.get("stars"), (int, float)):
                stars = int(got["stars"])
            if isinstance(got.get("review"), str) and got["review"].strip():
                review = got["review"].strip()
        except Exception:                               # noqa: BLE001
            pass
    stars = min(5, max(1, stars if stars is not None else derive_stars(r)))     # always a whole 1-5
    if not r["deal"]:
        stars = 3            # no deal because the budget was below the shop's price is not its fault
    elif r["honest"] is False:
        stars = 1            # a wrong receipt is never rewarded, whatever the model felt
    elif r["price"] is not None and r["price"] >= r["budget"]:
        stars = min(stars, 3)   # paid the full budget (the last-round max offer): a squeeze, not a good deal
    if not review:                                      # never empty
        if r["deal"]:
            review = (f"Bought the {r['item']} at {r['price']} of {r['budget']} budget in {r['rounds']} rounds; "
                      + ("receipt matched." if r["honest"] else "receipt did not match."))
        else:
            review = f"No deal on the {r['item']} within {r['budget']} after {r['rounds']} rounds."
    return stars, review


# ---------------------------------------------------------------- memory write

def aggregate(trades: list) -> dict:
    deals = [t for t in trades if t["deal"]]
    priced = [t for t in deals if t.get("price")]
    return {
        "deals": len(deals),
        "nodeal": sum(1 for t in trades if not t["deal"]),
        "avg_price_ratio": round(sum(t["price"] / max(1, t["budget"]) for t in priced) / len(priced), 3) if priced else 1.0,
        "cheated": sum(1 for t in trades if t["honest"] is False),
        "recent_failed": sum(1 for t in trades[-RECENT:] if t.get("error")),
    }


def remember(mem: dict, name: str, fp: str | None, url: str, r: dict, stars: int) -> None:
    e = mem.get(name)
    if fp is None and e:                                # the card was never read (the shop failed first): same shop
        fp = e.get("fp")
    if e and e.get("fp") is None:                       # learned from the kit log, card never seen: adopt it
        e["fp"] = fp
    if not e or e.get("fp") != fp:                      # new name, or same name different shop -> fresh entry
        # The good record is not handed to whoever holds the name now, but the
        # wrong receipts stay with the name: otherwise a shop that cheated
        # could wash its record off by rewriting one line of its card.
        kept = [t for t in (e or {}).get("trades", []) if t["honest"] is False]
        if e:
            buyer.log(f"[market_buyer] {name}'s card fingerprint changed; not carrying over old reputation"
                      + (f", except {len(kept)} wrong receipt(s)" if kept else ""))
        e = {"fp": fp, "trades": kept}
    e["last_url"] = url
    e["trades"].append({"item": r["item"], "budget": r["budget"], "price": r["price"],
                        "rounds": r["rounds"], "deal": r["deal"], "honest": r["honest"], "stars": stars,
                        "error": r.get("error")})
    # Keep memory small, but never by forgetting a wrong receipt.
    e["trades"] = [t for t in e["trades"][:-20] if t["honest"] is False] + e["trades"][-20:]
    e["agg"] = aggregate(e["trades"])
    mem[name] = e


# ----------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--shops", required=True)
    ap.add_argument("--item", required=True)
    ap.add_argument("--budget", type=int, required=True)
    a = ap.parse_args()

    shops = json.loads(Path(a.shops).read_text(encoding="utf-8"))
    mpath = mem_path(a.shops)
    mem = load_mem(mpath)
    if import_round0(mem):
        save_mem(mpath, mem)

    if not shops:
        print(json.dumps({"shop": "", "receipt": None, "stars": 3, "review": "No shops in the market."}))
        return 0

    chosen = select(shops, mem, a.item, a.budget)
    buyer.log(f"[market_buyer] chose {chosen['name']} {chosen['url']}")

    r = trade(chosen["url"], a.item, a.budget, mem.get(chosen["name"]))
    r["item"], r["budget"] = a.item, a.budget
    stars, review = make_review(r)
    remember(mem, chosen["name"], r["fp"], chosen["url"], r, stars)
    save_mem(mpath, mem)

    # The kit reads this last line. Everything above is log.
    print(json.dumps({"shop": chosen["url"], "receipt": r["receipt"], "stars": stars, "review": review},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
