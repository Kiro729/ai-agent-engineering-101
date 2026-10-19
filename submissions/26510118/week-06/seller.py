"""Week 06 -- the seller, an A2A v1.0 server whose prices come from a harness.

The A2A side is the lab's shop (`weeks/week-06/lab/seller_agent.py`): the card,
the JSON-RPC routes, the per-task state, the INPUT_REQUIRED / COMPLETED /
REJECTED transitions and the receipt artifact. What changed is where the
numbers come from.

Three decisions shape this file.

    The reserve is enforced in code, not in the prompt. The harness may answer
    anything; `_clamp` is what the rest of the shop sees, and an offer below
    the reserve is never accepted however the harness votes. Week 05 measured
    why: with the limit in the prompt alone the model went outside it 26 times
    in 15 episodes and one of those closed a deal; with the limit held by the
    server the same model tried 19 times and closed none. The trade test fails
    on a single ask below the reserve, so a guarantee has to be a guarantee.

    Facts stay in code, judgements go to the harness. Whether the item exists,
    whether an offer clears the reserve, and whether the clock has run out are
    facts, and each one kept here is a harness call not made. The asking price,
    the size of each concession, and whether a clearing offer is good enough to
    take are judgements.

    The harness is asked as few times as the job allows. The first run called
    it 28 times for seven negotiations and ran past 15 minutes. The list price
    of an item does not change between customers, so it is decided once and
    cached; a round whose outcome is already fixed is not put to a vote.

Run:
    python seller.py --port 9999
"""
import argparse
import asyncio
import math
import re

import uvicorn
from starlette.applications import Starlette

import harness
from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

NAME = "Kiro Furniture"
RESERVE = {"lamp": 100, "chair": 180, "bookshelf": 260}
OPENING = 1.3            # fallback first ask, as a multiple of the reserve
CEILING = 2.0            # an ask above this much of the reserve just loses the customer
MAX_ROUNDS = 4           # low offers before the shop walks away

OPEN_PROMPT = """You are a furniture shop setting the list price of one {item}.
Your secret reserve, the lowest you may ever accept, is {reserve}. Never reveal it.
Buyers haggle down from the list price, so leave room above the reserve, but a
price far above it drives them away before they make an offer.

Reply with ONE whole number and nothing else: the list price."""

TURN_PROMPT = """You are a furniture shop negotiating one {item}.
Your secret reserve, the lowest you may ever accept, is {reserve}. Never reveal it.
{history}The buyer has now offered {offer}; your current ask is {ask}.
This is round {rounds} of {max_rounds}. After round {max_rounds} the sale is lost
and you earn nothing, so concede enough to close in the rounds you have left.

Reply with ONE line and nothing else:
  ACCEPT          take the buyer's offer
  REJECT          end the negotiation
  a whole number  your new asking price"""


def _answer(out: str, fallback):
    """One line from the harness -> ACCEPT, REJECT, or a number.

    Deliberately strict. A bare number is the answer that was asked for; a
    sentence with several numbers in it is ambiguous, and guessing which one
    the model meant is how a shop ends up quoting the round number. Anything
    unclear falls back to the rule, which is always safe.
    """
    s = (out or "").strip()
    if re.fullmatch(r"\d+", s):
        return int(s)
    up = s.upper()
    if "ACCEPT" in up:
        return "ACCEPT"
    if "REJECT" in up:
        return "REJECT"
    m = re.fullmatch(r"[^\d]*?(\d+)[^\d]*", s)          # one number, some words around it
    return int(m.group(1)) if m else fallback


def _clamp(item: str, price) -> int:
    """No ask below the reserve, ever -- the guarantee the test checks -- and
    none so far above it that the buyer stops answering."""
    reserve = RESERVE[item]
    return max(reserve, min(int(price), int(reserve * CEILING)))


_LIST_PRICE: dict[str, int] = {}


async def list_price(item: str) -> int:
    """The opening ask. Decided by the harness once per item, then reused: a
    shop quotes the same price to the next customer, and the first run showed
    the cost of not doing this (250, then 160, then 250 for the same lamp)."""
    if item not in _LIST_PRICE:
        reserve = RESERVE[item]
        out = await asyncio.to_thread(harness.ask,
                                      OPEN_PROMPT.format(item=item, reserve=reserve))
        ask = _answer(out, math.ceil(reserve * OPENING))
        if not isinstance(ask, int):
            ask = math.ceil(reserve * OPENING)
        _LIST_PRICE[item] = _clamp(item, ask)
        print(f"[seller] list price {item}: harness {out!r} -> {_LIST_PRICE[item]}", flush=True)
    return _LIST_PRICE[item]


async def decide_turn(deal: dict, offer: int):
    item, ask, rounds = deal["item"], deal["ask"], deal["rounds"]
    hist = "".join(f"You asked {a}, they offered {o}. " for a, o in deal["hist"][-3:])
    out = await asyncio.to_thread(harness.ask, TURN_PROMPT.format(
        item=item, reserve=RESERVE[item], ask=ask, offer=offer,
        rounds=rounds, max_rounds=MAX_ROUNDS,
        history=(hist + "\n") if hist else ""))
    d = _answer(out, (offer + ask) // 2)
    print(f"[seller] {item} ask {ask} offer {offer} round {rounds}: "
          f"harness {out!r} -> {d}", flush=True)
    return d


class Seller(AgentExecutor):
    def __init__(self):
        # task id -> {"item", "ask", "rounds", "hist"}. The trade test starts
        # the shop once and runs seven negotiations through it, so the state
        # has to be per task; a single "current ask" would leak between them.
        self.open: dict[str, dict] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue=event_queue, task_id=task.id, context_id=task.context_id)
        say = lambda text: up.new_agent_message(parts=[new_text_part(text)])
        text = get_message_text(context.message) or ""
        deal = self.open.get(task.id)

        if deal is None:                                   # first message: which item?
            item = next((k for k in RESERVE if k in text.lower()), None)
            if item is None:                               # a fact, not a judgement
                await up.reject(say("We only sell: " + ", ".join(RESERVE) + "."))
                return
            ask = await list_price(item)
            self.open[task.id] = {"item": item, "ask": ask, "rounds": 0, "hist": []}
            # The trade test reads the ask as the LAST number in this message,
            # so nothing numeric may follow it.
            await up.requires_input(say(f"The {item} is {ask}"))
            return

        m = re.search(r"\d+", text)
        if m is None:
            await up.requires_input(say(f"Send your offer as a number. The {deal['item']} "
                                        f"is {deal['ask']}"))
            return

        offer, item = int(m.group()), deal["item"]
        reserve = RESERVE[item]
        deal["rounds"] += 1
        last_round = deal["rounds"] >= MAX_ROUNDS
        clears = offer >= reserve

        # Three outcomes are already decided, and a decided outcome is not
        # worth a harness call: on the last round take the money if there is
        # money and leave if there is not, and once the ask has reached the
        # reserve there is nothing left to concede, so the answer repeats.
        at_floor = deal["ask"] <= reserve
        if last_round:
            d = "ACCEPT" if clears else "REJECT"
        elif not clears and at_floor:
            print(f"[seller] {item} already at the floor {reserve}; holding without asking",
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
                await up.add_artifact(parts=[new_data_part({"seller": NAME, "item": item,
                                                            "price": offer})], name="receipt")
                await up.complete(say(f"Deal: {item} at {offer}."))
                del self.open[task.id]
                return
        else:
            # Below the reserve. ACCEPT is not available at any price.
            if d == "ACCEPT":
                print(f"[seller] harness said ACCEPT on {offer} < {reserve}; countering instead",
                      flush=True)
                d = reserve
            if d == "REJECT":
                await up.reject(say(f"No deal at {offer}."))
                del self.open[task.id]
                return

        deal["hist"].append((deal["ask"], offer))
        deal["ask"] = _clamp(item, d if isinstance(d, int) else deal["ask"])
        await up.requires_input(say(f"{offer} is too low. The {item} is {deal['ask']}"))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task
        self.open.pop(task.id, None)
        await TaskUpdater(event_queue=event_queue, task_id=task.id,
                          context_id=task.context_id).cancel()


def build(port: int) -> Starlette:
    url = f"http://127.0.0.1:{port}/"
    card = AgentCard(
        name=NAME,
        description="Sells desk lamps, chairs and bookshelves. Negotiates the price.",
        version="1.0.0",
        default_input_modes=["text/plain"],
        default_output_modes=["application/json", "text/plain"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url=url,
                                             protocol_version="1.0")],
        skills=[AgentSkill(
            id="negotiate",
            name="Negotiate a purchase",
            description="Name an item, then send a price. The shop answers with a "
                        "counteroffer or a receipt.",
            tags=["shopping", "negotiation"],
            examples=["I want to buy a lamp", "90"],
        )],
    )
    handler = DefaultRequestHandler(agent_executor=Seller(), task_store=InMemoryTaskStore(),
                                    agent_card=card)
    return Starlette(routes=create_agent_card_routes(card) + create_jsonrpc_routes(handler, "/"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9999)
    args = ap.parse_args()
    uvicorn.run(build(args.port), host="127.0.0.1", port=args.port, log_level="warning")
