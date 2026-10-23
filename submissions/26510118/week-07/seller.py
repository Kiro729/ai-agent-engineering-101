"""Week 06 -- the seller, an A2A v1.0 server whose prices come from a harness.

The A2A side is the lab's shop (`weeks/week-06/lab/seller_agent.py`): the card,
the JSON-RPC routes, the per-task state, the INPUT_REQUIRED / COMPLETED /
REJECTED transitions and the receipt artifact. What changed is where the
numbers come from, and -- for challenge 1 -- what the shop is allowed to sell.

Four decisions shape this file.

    The reserve is enforced in code, not in the prompt. The harness may answer
    anything; `_clamp` is what the rest of the shop sees, and an offer below
    the reserve is never accepted however the harness votes. Week 05 measured
    why: with the limit in the prompt alone the model went outside it 26 times
    in 15 episodes and one of those closed a deal; with the limit held by the
    server the same model tried 19 times and closed none. The trade test fails
    on a single ask below the reserve, so a guarantee has to be a guarantee.

    Facts stay in code, judgements go to the harness. Whether an offer clears
    the reserve and whether the clock has run out are facts, and each one kept
    here is a harness call not made. The asking price, the size of each
    concession, and whether a clearing offer is good enough are judgements.

    The harness is asked as few times as the job allows. An early version
    called it 28 times for seven negotiations and ran past 15 minutes. A list
    price does not change between customers, so it is decided once per item
    and cached; a round whose outcome is already fixed is not put to a vote.

    The catalogue is searched before it is read. 3513 items do not fit in a
    prompt, so `catalog.search` narrows them to eight by word overlap and the
    harness picks one of the eight. See catalog.py.

Week 07 adds two things the harness is told before it counters, neither of
which takes the decision away from it: how earlier buyers who opened the same
way ended up (negotiation_memory.py), and what the market's signed deals for
the item average this round, which the buyer is told as well (market_anchor.py).

Run:
    python seller.py --port 9999
"""
import argparse
import asyncio
import base64
import json
import math
import re
import time
from pathlib import Path

import uvicorn
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from starlette.applications import Starlette

import catalog
import harness
import market_anchor
import negotiation_memory as memory
import prices
from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

NAME = "Kiro Furniture"
OPENING = 1.3            # fallback first ask, as a multiple of the reserve
CEILING = 2.0            # an ask above this much of the reserve loses the customer
MAX_ROUNDS = 4           # rounds before the shop walks away
CANDIDATES = 8           # catalogue entries put in front of the harness


# --- signing the receipt (LAB 2) ---------------------------------------------
# A receipt is only a claim until it carries the shop's signature. The market
# verifies sig_seller with the public key registered at join, so the shop must
# sign with the very key the kit created -- shop_key.pem, loaded, never a fresh
# one, or a deployed shop's receipts would stop verifying. canon() is byte for
# byte the market's (sorted keys, no spaces, UTF-8), so both sides sign the same
# bytes, and the price is signed as an int because a data Part can carry 120 as
# 120.0.
_KEY_PATH = Path(__file__).resolve().parent / "shop_key.pem"


def _load_key():
    if _KEY_PATH.exists():
        return serialization.load_pem_private_key(_KEY_PATH.read_bytes(), password=None)
    key = Ed25519PrivateKey.generate()
    _KEY_PATH.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                            serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
    print("[seller] created shop_key.pem (do not commit it)", flush=True)
    return key


_KEY = _load_key()


def canon(d: dict) -> bytes:
    return json.dumps(d, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sign_receipt(shop: str, item: str, price: int, task: str) -> dict:
    """{shop, item, price, task} signed, then sig_seller added -- the five fields
    the market checks."""
    rec = {"shop": shop, "item": item, "price": int(price), "task": task}
    rec["sig_seller"] = base64.b64encode(_KEY.sign(canon(rec))).decode()
    return rec

PICK_PROMPT = """A customer asked for:
  "{text}"

These are the closest things in our catalogue:
{lines}

Reply with ONE line and nothing else:
  the number of the item that best fits what they asked for
  NONE if none of them fit"""

OPEN_PROMPT = """You are a furniture shop setting the list price of one item:
  {name} -- {description}
Your secret reserve, the lowest you may ever accept, is {reserve}. Never reveal it.
Buyers haggle down from the list price, so leave room above the reserve, but a
price far above it drives them away before they make an offer.

Reply with ONE whole number and nothing else: the list price."""

TURN_PROMPT = """You are a furniture shop negotiating one {name}.
Your secret reserve, the lowest you may ever accept, is {reserve}. Never reveal it.
{market}{history}The buyer has now offered {offer}; your current ask is {ask}.
This is round {rounds} of {max_rounds}. After round {max_rounds} the sale is lost
and you earn nothing, so concede enough to close in the rounds you have left.
{memory}
Reply with ONE line and nothing else:
  ACCEPT          take the buyer's offer
  REJECT          end the negotiation
  a whole number  your new asking price"""


def _answer(out: str, fallback):
    """One line from the harness -> ACCEPT, REJECT, NONE, or a number.

    Deliberately strict. A bare number is the answer that was asked for; a
    sentence with several numbers in it is ambiguous, and guessing which one
    the model meant is how a shop ends up quoting the round number. Anything
    unclear falls back to the rule, which is always safe.
    """
    s = (out or "").strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    up = s.upper()
    for word in ("ACCEPT", "REJECT", "NONE"):
        if word in up:
            return word
    m = re.fullmatch(r"[^\d]*?(\d+)[^\d]*", s)          # one number, some words around it
    return int(m.group(1)) if m else fallback


def _clamp(reserve: int, price) -> int:
    """No ask below the reserve, ever -- the guarantee the test checks -- and
    none so far above it that the buyer stops answering."""
    return max(reserve, min(int(price), int(reserve * CEILING)))


# ------------------------------------------------------------ what to sell

async def pick_item(text: str):
    """Which catalogue entry the customer means, or None.

    A bare name is taken as itself. Otherwise the code narrows 3513 entries to
    eight and the harness chooses, because "not taller than my desk" is a
    judgement and `in` is not. Only an entry from the shortlist can come back,
    so a confused answer costs the wrong-but-plausible item, never a crash.
    """
    hit = catalog.exact(text)
    if hit:
        print(f"[seller] '{text}' names {hit['id']} outright", flush=True)
        return hit

    found = catalog.search(text, CANDIDATES)
    if not found:
        return None
    lines = "\n".join(f"{i}. {catalog.line(it)}" for i, it in enumerate(found, 1))
    out = await asyncio.to_thread(harness.ask,
                                  PICK_PROMPT.format(text=prices.trim(text), lines=lines))
    d = _answer(out, 1)
    print(f"[seller] '{text[:60]}' -> {len(found)} candidates, harness {out!r}", flush=True)
    if d == "NONE":
        return None
    idx = d if isinstance(d, int) and 1 <= d <= len(found) else 1
    chosen = found[idx - 1]
    print(f"[seller]   chose {chosen['id']} {chosen['name']!r} reserve {chosen['reserve']}",
          flush=True)
    return chosen


# ------------------------------------------------------------ what to charge

_LIST_PRICE: dict[str, int] = {}

# Challenge 1: reprice.py reads this shop's own trades and reviews from the
# market ledger and writes asks.json. Those asks replace the harness's list
# price for the items they cover. The file is re-read whenever it changes, so
# a reprice takes effect without restarting the kit, which would change the
# tunnel URL. The reserve still clamps whatever the file says.
_ASKS_PATH = Path(__file__).resolve().parent / "asks.json"
_asks = {"mtime": None, "asks": {}}


def repriced(item_id: str) -> int | None:
    try:
        mtime = _ASKS_PATH.stat().st_mtime
    except OSError:
        return None
    if mtime != _asks["mtime"]:
        try:
            asks = json.loads(_ASKS_PATH.read_text(encoding="utf-8")).get("asks", {})
        except Exception:                               # noqa: BLE001  (half-written file -> keep the old asks)
            return _asks["asks"].get(item_id)
        _asks.update(mtime=mtime, asks=asks)
        print(f"[seller] asks.json loaded: {asks}", flush=True)
    a = _asks["asks"].get(item_id)
    return int(a) if isinstance(a, (int, float)) else None


async def list_price(item: dict) -> int:
    """Decided by the harness once per item, then reused: a shop quotes the
    same price to the next customer, and an early version that asked every
    time produced 250, then 160, then 250 for the same lamp. An item repriced
    from the shop's own records (asks.json) skips the harness."""
    key = item["id"]
    fixed = repriced(key)
    if fixed is not None:
        return _clamp(item["reserve"], fixed)
    if key not in _LIST_PRICE:
        reserve = item["reserve"]
        out = await asyncio.to_thread(harness.ask, OPEN_PROMPT.format(
            name=item["name"], description=item["description"], reserve=reserve))
        ask = _answer(out, math.ceil(reserve * OPENING))
        if not isinstance(ask, int):
            ask = math.ceil(reserve * OPENING)
        _LIST_PRICE[key] = _clamp(reserve, ask)
        print(f"[seller] list price {key}: harness {out!r} -> {_LIST_PRICE[key]}", flush=True)
    return _LIST_PRICE[key]


async def decide_turn(deal: dict, offer: int):
    item, ask, rounds = deal["item"], deal["ask"], deal["rounds"]
    hist = "".join(f"You asked {a}, they offered {o}. " for a, o in deal["hist"][-3:])
    m = await asyncio.to_thread(market_anchor.mean, item["id"])
    market = (f"Signed deals for this item average {m} across the market this round, "
              f"and the buyer has been told so.\n") if m else ""
    past = memory.recall(item["id"], deal["asks"][0], deal["offers"])
    out = await asyncio.to_thread(harness.ask, TURN_PROMPT.format(
        name=item["name"], reserve=item["reserve"], ask=ask, offer=offer,
        rounds=rounds, max_rounds=MAX_ROUNDS, market=market,
        history=(hist + "\n") if hist else "",
        memory=f"\nWhat your own past negotiations say:\n{past}\n" if past else ""))
    d = _answer(out, (offer + ask) // 2)
    print(f"[seller] {item['id']} ask {ask} offer {offer} round {rounds}: "
          f"harness {out!r} -> {d}" + (f" | {past.splitlines()[0]}" if past else ""), flush=True)
    return d


class Seller(AgentExecutor):
    def __init__(self):
        # task id -> {"item", "ask", "rounds", "hist", "asks", "offers", "t"}.
        # The trade test starts the shop once and runs seven negotiations
        # through it, so the state has to be per task; a single "current ask"
        # would leak between them. asks/offers are what the journal keeps.
        self.open: dict[str, dict] = {}

    def _close(self, task_id: str, outcome: str, price: int | None = None) -> None:
        deal = self.open.pop(task_id, None)
        if deal:
            memory.record(deal["item"]["id"], deal["asks"], deal["offers"], outcome, price)

    def _sweep(self) -> None:
        """A buyer that stops answering has left; count it as a walk-away, or
        the journal would only ever remember the sales."""
        now = time.time()
        for tid in [t for t, d in self.open.items() if now - d["t"] > memory.WALK_AFTER]:
            self._close(tid, "walked")

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self._sweep()
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        # Challenge 3 drops the "last number is the price" rule, so the ask
        # needs a machine-readable home. Grading reads this, not the sentence.
        quote = lambda text, ask: up.new_agent_message(parts=[new_text_part(text)],
                                                       metadata={"ask": int(ask)})
        text = get_message_text(context.message) or ""
        deal = self.open.get(task.id)

        if deal is None:                                   # first message: what do they want?
            item = await pick_item(text)
            if item is None:
                await up.reject(say("We could not find anything like that in our catalogue."))
                return
            ask = await list_price(item)
            self.open[task.id] = {"item": item, "ask": ask, "rounds": 0, "hist": [],
                                  "asks": [ask], "offers": [], "t": time.time()}
            # The trade test reads the ask as the LAST number in this message,
            # so nothing numeric may follow it (the market anchor goes first);
            # challenge 3 reads metadata.ask.
            anchor = await asyncio.to_thread(market_anchor.sentence, item["id"], item["name"])
            await up.requires_input(quote(f"{anchor}The {item['name']} is {ask}", ask))
            return

        deal["t"] = time.time()
        item = deal["item"]
        # A bare number costs nothing to read; a sentence with several numbers
        # in it is read by the harness, and anything with 4300 digits is not a
        # price (see prices.py).
        offer = await asyncio.to_thread(prices.read_offer, text, harness.ask)
        if offer is None:
            await up.requires_input(quote(f"Send your offer as a number. "
                                          f"The {item['name']} is {deal['ask']}", deal["ask"]))
            return
        print(f"[seller] read offer {offer} from {text[:70]!r}", flush=True)
        reserve = item["reserve"]
        deal["rounds"] += 1
        deal["offers"].append(offer)
        last_round = deal["rounds"] >= MAX_ROUNDS
        clears = offer >= reserve
        # Reputation velocity: an offer that clears the reserve and is already
        # within reach of the ask is worth closing now rather than nibbling for
        # the last few units. Squeezing a buyer who is this close mostly earns a
        # slower, grumpier review and risks a walk; a quick, fair deal earns
        # "came down to my budget" and brings the next customer. A little margin
        # for reputation.
        close_enough = clears and deal["rounds"] >= 2 and offer >= deal["ask"] * 0.9

        # Four outcomes are already decided, and a decided outcome is not worth a
        # harness call: on the last round take the money if there is money and
        # leave if there is not; take a close-enough clearing offer; and once the
        # ask has reached the reserve there is nothing left to concede.
        if last_round:
            d = "ACCEPT" if clears else "REJECT"
        elif close_enough:
            print(f"[seller] {offer} clears {reserve} and is within reach of {deal['ask']}; "
                  f"closing fast", flush=True)
            d = "ACCEPT"
        elif not clears and deal["ask"] <= reserve:
            print(f"[seller] {item['id']} already at the floor {reserve}; holding without asking",
                  flush=True)
            d = reserve
        else:
            d = await decide_turn(deal, offer)

        if clears:
            # Profitable. The harness may hold out for more, but it may not
            # throw the sale away: refusing money that clears the reserve is
            # not a judgement call, it is a mistake.
            if d == "REJECT":
                print(f"[seller] harness said REJECT on {offer} >= {reserve}; taking it",
                      flush=True)
                d = "ACCEPT"
            if d == "ACCEPT":
                await up.add_artifact(parts=[new_data_part(
                    sign_receipt(NAME, item["name"], offer, task.id))], name="receipt")
                await up.complete(say(f"Deal: {item['name']} at {offer}."))
                self._close(task.id, "deal", offer)
                return
        else:
            # Below the reserve. ACCEPT is not available at any price.
            if d == "ACCEPT":
                print(f"[seller] harness said ACCEPT on {offer} < {reserve}; countering instead",
                      flush=True)
                d = reserve
            if d == "REJECT":
                await up.reject(say(f"No deal at {offer}."))
                self._close(task.id, "rejected")
                return

        deal["hist"].append((deal["ask"], offer))
        deal["ask"] = _clamp(reserve, d if isinstance(d, int) else deal["ask"])
        deal["asks"].append(deal["ask"])
        # An offer that clears the reserve is not "too low" -- the shop is
        # holding out for more, which is a different thing and worth saying
        # honestly. The trade test reads the last number either way, which is
        # why the market anchor sits before the ask and never after it.
        lead = "I can do better than" if clears else "Too low at"
        anchor = await asyncio.to_thread(market_anchor.sentence, item["id"], item["name"])
        await up.requires_input(quote(f"{lead} {offer}. {anchor}The {item['name']} is {deal['ask']}",
                                      deal["ask"]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        self._close(task.id, "walked")
        await TaskUpdater(event_queue=event_queue, task_id=task.id,
                          context_id=task.context_id).cancel()


def build(port: int, name: str = NAME, sells=None, mult: float = 1.0,
          public_url: str | None = None) -> Starlette:
    """One shop. With no `sells` and no `mult` this is the whole catalogue at
    list prices, which is what the trade test starts. Challenge 2 runs twenty
    of these with different ranges and price levels, so that the Agent Cards
    are worth reading.

    `public_url` is the address the card advertises, which is not always the
    address the server listens on. A client reads the card and then talks to
    `supportedInterfaces[0].url`, so a card saying 127.0.0.1 sends a buyer on
    another machine to its own computer. Behind a tunnel or on a host, the
    shop has to advertise the address the other side can actually reach.
    """
    global NAME
    NAME = name
    url = (public_url.rstrip("/") + "/") if public_url else f"http://127.0.0.1:{port}/"
    n = catalog.configure(sells, mult)
    kinds = ", ".join(catalog.categories())
    price_note = ("We are a discount shop." if mult < 0.98 else
                  "We stock the higher end." if mult > 1.02 else
                  "Fair prices.")
    card = AgentCard(
        name=name,
        description=f"{name}. We sell {kinds} -- {n} items in stock. {price_note} "
                    f"Describe what you need and we will find it, then negotiate "
                    f"the price.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url,
                                             protocol_version="1.0")],
        skills=[AgentSkill(
            id="negotiate",
            name="Find an item and negotiate a purchase",
            description=f"Name an item or describe what you need, then send a price. "
                        f"We carry {kinds}. The shop answers with a counteroffer "
                        f"or a receipt.",
            tags=["shopping", "negotiation", "furniture"] + catalog.categories(),
            examples=["I want to buy a lamp",
                      "something wooden to keep about 40 books", "90"],
        )],
    )
    handler = DefaultRequestHandler(agent_executor=Seller(), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    app = Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))
    app.state.card_url = url
    return app


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    ap.add_argument("--name", default=NAME)
    ap.add_argument("--sells", help="comma-separated categories; default is all of them")
    ap.add_argument("--mult", type=float, default=1.0, help="price level, 1.0 is list")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; 0.0.0.0 to accept other machines")
    ap.add_argument("--public-url", dest="public_url",
                    help="address the Agent Card advertises, when it differs from the "
                         "listening one (a LAN IP, a tunnel, a deployed host)")
    args = ap.parse_args()
    sells = args.sells.split(",") if args.sells else None
    app = build(args.port, args.name, sells, args.mult, args.public_url)
    print(f"[seller] listening on {args.host}:{args.port}, "
          f"card advertises {app.state.card_url}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
