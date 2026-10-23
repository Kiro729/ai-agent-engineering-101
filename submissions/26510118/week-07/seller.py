"""Week 07 -- the shop: an A2A v1.0 server that negotiates through a harness
and signs its receipts.

The A2A side is week 06's (the lab's shop): the card, the JSON-RPC routes, the
per-task state, the INPUT_REQUIRED / COMPLETED / REJECTED transitions and the
receipt artifact. Who decides what:

    code      the reserve. `_clamp` is all the rest of the shop sees, and an
              offer below the reserve is never accepted however the harness
              votes. Week 05 measured why: with the limit only in the prompt
              the model broke it 26 times in 15 episodes and one of those closed
              a deal; held by the server, it never did.
    code      facts: whether an offer clears the reserve, whether the buyer
              already pays the ask, whether the clock has run out. Each fact
              kept here is a harness call not made.
    harness   judgements: which catalogue entry a description means, the list
              price of an item nobody has repriced, and every counteroffer.
    data      the list price of the market's three items, set from the shop's
              own trades and reviews (reprice.py -> asks.json, challenge 1).

Before each counteroffer the harness is also told two things it cannot see in
the conversation, and left to weigh them itself:
    how earlier buyers who opened the same way ended up      negotiation_memory.py
    the market's average signed price for the item          market_anchor.py
The buyer is told the same market figure, so the shop's words and asks agree.

Receipts are {shop, item, price, task}, signed with shop_key.pem (LAB 2).

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
HERE = Path(__file__).resolve().parent


# --- signing the receipt (LAB 2) ---------------------------------------------
# A receipt is only a claim until it carries the shop's signature. The market
# verifies sig_seller with the public key registered at join, so the shop must
# sign with the very key the kit created -- shop_key.pem, loaded, never a fresh
# one, or a deployed shop's receipts would stop verifying. canon() is byte for
# byte the market's (sorted keys, no spaces, UTF-8), so both sides sign the same
# bytes, and the price is signed as an int because a data Part can carry 120 as
# 120.0.
_KEY_PATH = HERE / "shop_key.pem"


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


# --- the harness's three questions -------------------------------------------

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
# Challenge 1: reprice.py reads this shop's own trades and reviews from the
# market ledger and writes asks.json, whose asks replace the harness's list
# price for the items they cover. The file is re-read whenever it changes, so a
# reprice takes effect without restarting the kit (which would change the
# tunnel URL). The reserve still clamps whatever the file says.

_ASKS_PATH = HERE / "asks.json"
_asks = {"mtime": None, "asks": {}}
_LIST_PRICE: dict[str, int] = {}


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
    """From asks.json when the item has been repriced; otherwise decided by the
    harness once per item and reused, because a shop quotes the same price to
    the next customer (an early version that asked every time produced 250,
    then 160, then 250 for the same lamp)."""
    key, reserve = item["id"], item["reserve"]
    fixed = repriced(key)
    if fixed is not None:
        return _clamp(reserve, fixed)
    if key not in _LIST_PRICE:
        out = await asyncio.to_thread(harness.ask, OPEN_PROMPT.format(
            name=item["name"], description=item["description"], reserve=reserve))
        ask = _answer(out, math.ceil(reserve * OPENING))
        if not isinstance(ask, int):
            ask = math.ceil(reserve * OPENING)
        _LIST_PRICE[key] = _clamp(reserve, ask)
        print(f"[seller] list price {key}: harness {out!r} -> {_LIST_PRICE[key]}", flush=True)
    return _LIST_PRICE[key]


async def decide_turn(deal: dict, offer: int):
    """The harness's counteroffer, told the market's average and what earlier
    buyers who opened like this one went on to do."""
    item, ask, rounds = deal["item"], deal["asks"][-1], len(deal["offers"])
    earlier = list(zip(deal["asks"], deal["offers"]))[:-1][-3:]   # (my ask, their offer), this round excluded
    hist = "".join(f"You asked {a}, they offered {o}. " for a, o in earlier)
    market = await asyncio.to_thread(market_anchor.for_harness, item["id"])
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


# ------------------------------------------------------------ negotiating

class Seller(AgentExecutor):
    def __init__(self):
        # task id -> {"item", "asks", "offers", "t"}: every ask shown (the list
        # price first, the current ask last), every offer received, and when
        # the buyer last spoke. Per task, because the trade test runs seven
        # negotiations through one shop and a single "current ask" would leak
        # between them. The journal keeps the same two lists.
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

    @staticmethod
    async def _ask_for_offer(up: TaskUpdater, deal: dict, lead: str = "") -> None:
        """Quote the current ask. The market anchor goes before it and nothing
        numeric after it, because buyers (and the trade test) read the LAST
        number as the ask; metadata.ask carries it for buyers that read data."""
        item, ask = deal["item"], deal["asks"][-1]
        anchor = await asyncio.to_thread(market_anchor.sentence, item["id"], item["name"])
        await up.requires_input(up.new_agent_message(
            parts=[new_text_part(f"{lead}{anchor}The {item['name']} is {ask}")],
            metadata={"ask": int(ask)}))

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        self._sweep()
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        text = get_message_text(context.message) or ""
        deal = self.open.get(task.id)

        if deal is None:                                   # first message: what do they want?
            item = await pick_item(text)
            if item is None:
                await up.reject(say("We could not find anything like that in our catalogue."))
                return
            deal = self.open[task.id] = {"item": item, "asks": [await list_price(item)],
                                         "offers": [], "t": time.time()}
            await self._ask_for_offer(up, deal)
            return

        deal["t"] = time.time()
        item, reserve, ask = deal["item"], deal["item"]["reserve"], deal["asks"][-1]
        # A bare number costs nothing to read; a sentence with several numbers
        # in it is read by the harness, and anything with 4300 digits is not a
        # price (see prices.py).
        offer = await asyncio.to_thread(prices.read_offer, text, harness.ask)
        if offer is None:
            await self._ask_for_offer(up, deal, "Send your offer as a number. ")
            return
        print(f"[seller] read offer {offer} from {text[:70]!r}", flush=True)
        deal["offers"].append(offer)
        rounds = len(deal["offers"])
        clears = offer >= reserve
        # Reputation velocity: an offer that clears the reserve and is already
        # within reach of the ask is worth closing now rather than nibbling for
        # the last few units. Squeezing a buyer who is this close mostly earns a
        # slower, grumpier review and risks a walk; a quick, fair deal brings
        # the next customer. A little margin for reputation.
        close_enough = clears and rounds >= 2 and offer >= ask * 0.9

        # Some outcomes are already decided, and a decided outcome is not worth
        # a harness call: a buyer paying the ask has bought; on the last round
        # take the money if there is money and leave if there is not; take a
        # close-enough clearing offer; and once the ask has reached the reserve
        # there is nothing left to concede.
        if clears and offer >= ask:
            d = "ACCEPT"
        elif rounds >= MAX_ROUNDS:
            d = "ACCEPT" if clears else "REJECT"
        elif close_enough:
            print(f"[seller] {offer} clears {reserve} and is within reach of {ask}; closing fast",
                  flush=True)
            d = "ACCEPT"
        elif not clears and ask <= reserve:
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
                print(f"[seller] harness said REJECT on {offer} >= {reserve}; taking it", flush=True)
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

        deal["asks"].append(_clamp(reserve, d if isinstance(d, int) else ask))
        # An offer that clears the reserve is not "too low" -- the shop is
        # holding out for more, which is a different thing and worth saying
        # honestly.
        lead = "I can do better than" if clears else "Too low at"
        await self._ask_for_offer(up, deal, f"{lead} {offer}. ")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        self._close(task.id, "walked")
        await TaskUpdater(event_queue=event_queue, task_id=task.id,
                          context_id=task.context_id).cancel()


# ------------------------------------------------------------ the card

def build(port: int, name: str = NAME, public_url: str | None = None) -> Starlette:
    """The shop. `public_url` is the address the card advertises, which is not
    always the one the server listens on: a client reads the card and then
    talks to `supportedInterfaces[0].url`, so behind a tunnel or on a host the
    card has to name the address the other side can actually reach.

    The card says only what the code above guarantees. Buyers that compare a
    shop's words with what it did (memory, reviews) find them agree.
    """
    global NAME
    NAME = name
    url = (public_url.rstrip("/") + "/") if public_url else f"http://127.0.0.1:{port}/"
    n = catalog.configure()
    kinds = ", ".join(catalog.categories())
    card = AgentCard(
        name=name,
        description=f"{name}. Lamps, chairs and bookshelves, and {n} pieces in all ({kinds}); "
                    f"describe what you need and we will find it. Every price is negotiated: "
                    f"send a number and we answer with a counteroffer or a receipt signed with "
                    f"our registered key, always at exactly the price we accepted. Our "
                    f"counteroffers cite the market's published average for signed deals, so "
                    f"you can check them.",
        version="1.1.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url,
                                             protocol_version="1.0")],
        skills=[AgentSkill(
            id="negotiate",
            name="Find an item and negotiate a purchase",
            description=f"Name an item or describe what you need, then send a price. "
                        f"We carry {kinds}. Each reply is a counteroffer, whose ask is the "
                        f"last number in the text and metadata.ask, or a signed receipt.",
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
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on; 0.0.0.0 to accept other machines")
    ap.add_argument("--public-url", dest="public_url",
                    help="address the Agent Card advertises, when it differs from the "
                         "listening one (a LAN IP, a tunnel, a deployed host)")
    args = ap.parse_args()
    app = build(args.port, args.name, args.public_url)
    print(f"[seller] listening on {args.host}:{args.port}, "
          f"card advertises {app.state.card_url}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
