"""Week 06 -- the seller, an A2A v1.0 server whose prices come from a harness.

The A2A side is the lab's shop (`weeks/week-06/lab/seller_agent.py`): the card,
the JSON-RPC routes, the per-task state, the INPUT_REQUIRED / COMPLETED /
REJECTED transitions and the receipt artifact. What changed is where the
numbers come from.

Two decisions shape this file.

    The reserve is enforced in code, not in the prompt. `decide_ask` may return
    anything; `_clamp` is what the rest of the shop sees. Week 05 measured why:
    with the limit in the prompt alone the model went outside it 26 times in 15
    episodes and one of those closed a deal; with the limit held by the server
    the same model tried 19 times and closed none. The trade test fails on a
    single ask below the reserve, so a guarantee has to be a guarantee.

    The harness decides only the asking price and the counteroffers. Whether
    the item exists, whether an offer clears the reserve, and whether the task
    is still open are decided here, because they are facts, not judgements --
    and every question skipped is a harness call saved.

Run:
    python seller.py --port 9999
"""
import argparse
import json
import math
import re

import uvicorn
from starlette.applications import Starlette

from a2a.helpers import get_message_text, new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill

NAME = "Kiro Furniture"
RESERVE = {"lamp": 100, "chair": 180, "bookshelf": 260}
OPENING = 1.3            # first ask, as a multiple of the reserve
MAX_ROUNDS = 4           # low offers before the shop walks away


def decide_ask(item: str, reserve: int, ask: int, offer: int, rounds: int) -> int:
    """The next asking price. Rule-based for now; the harness replaces this.

    Whatever it returns is clamped to the reserve by the caller, so it cannot
    break the trade test on its own.
    """
    return (offer + ask) // 2


def _clamp(item: str, price: int) -> int:
    """No ask below the reserve, ever. This is the guarantee the test checks."""
    return max(RESERVE[item], int(price))


class Seller(AgentExecutor):
    def __init__(self):
        # task id -> {"item", "ask", "rounds"}. The trade test starts the shop
        # once and runs seven negotiations through it, so the state has to be
        # per task; a single "current ask" would leak between them.
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
            if item is None:
                await up.reject(say("We only sell: " + ", ".join(RESERVE) + "."))
                return
            ask = _clamp(item, math.ceil(RESERVE[item] * OPENING))
            self.open[task.id] = {"item": item, "ask": ask, "rounds": 0}
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
        if offer >= reserve:                               # a fact, not a judgement
            await up.add_artifact(parts=[new_data_part({"seller": NAME, "item": item,
                                                        "price": offer})], name="receipt")
            await up.complete(say(f"Deal: {item} at {offer}."))
            del self.open[task.id]
            return

        deal["rounds"] += 1
        if deal["rounds"] >= MAX_ROUNDS:
            await up.reject(say(f"No deal at {offer}."))
            del self.open[task.id]
            return

        deal["ask"] = _clamp(item, decide_ask(item, reserve, deal["ask"], offer, deal["rounds"]))
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
