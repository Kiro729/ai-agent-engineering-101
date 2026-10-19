"""Week 06 -- the buyer, an A2A v1.0 client whose offers come from a harness.

This is what the `a2a` CLI did by hand in the lab, written out:

    a2a card get <url>              ->  GET /.well-known/agent-card.json
    a2a send -a <url> "I want ..."  ->  POST SendMessage
    a2a send --task-id <t> 80       ->  POST SendMessage with taskId

and the two things the CLI was quietly doing for us: the `A2A-Version: 1.0`
header on every POST, and carrying the task id from one message to the next.
Without the header the server reads the request as 0.3 and answers -32009.

The budget is enforced here, not in the prompt. `decide_offer` may return
anything; nothing above `budget` is ever sent. The trade test fails the run if
a single deal lands above it, and a model cannot promise that.

Run:
    python buyer.py http://127.0.0.1:9999 lamp 120
"""
import re
import sys
import uuid

import httpx

MAX_ROUNDS = 6           # offers before walking away
TIMEOUT = 60             # seconds per request

# Fractions of the budget to walk up, stage by stage. Rule-based for now; the
# harness replaces decide_offer. Starting low leaves room to climb, and the
# last rung is the budget itself.
LADDER = (0.65, 0.80, 0.90, 1.00)


def decide_offer(item: str, budget: int, ask: int, rounds: int) -> int:
    """The next offer. Clamped to the budget by the caller."""
    frac = LADDER[min(rounds, len(LADDER) - 1)]
    return int(budget * frac)


def log(*parts):
    print(" ".join(str(p) for p in parts), flush=True)


class Client:
    def __init__(self, base: str):
        self.http = httpx.Client(timeout=TIMEOUT)
        card = self.http.get(base.rstrip("/") + "/.well-known/agent-card.json").json()
        iface = (card.get("supportedInterfaces") or [{}])[0]
        self.url = iface.get("url") or base
        log(f"[buyer] card: {card.get('name')!r} via {iface.get('protocolBinding')} {self.url}")
        log(f"[buyer] skills: {[s.get('id') for s in card.get('skills') or []]}")

    def rpc(self, method: str, params: dict):
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4())[:8], "method": method, "params": params}
        # The header is the whole reason this client talks to a 1.0 server at
        # all. Every POST carries it, including CancelTask.
        r = self.http.post(self.url, json=body, headers={"A2A-Version": "1.0"}).json()
        if "error" in r:
            log(f"[buyer] <- error {r['error'].get('code')} "
                f"{str(r['error'].get('message'))[:120]}")
            return None
        return r["result"].get("task", r["result"])

    def send(self, text: str, task_id: str | None = None):
        msg = {"role": "ROLE_USER", "messageId": str(uuid.uuid4()), "parts": [{"text": text}]}
        if task_id:
            msg["taskId"] = task_id
        log(f"[buyer] -> {text!r}" + (f" taskId={task_id[:8]}" if task_id else ""))
        t = self.rpc("SendMessage", {"message": msg})
        if t:
            log(f"[buyer] <- {state(t)} | {message_text(t)[:120]}")
        return t

    def cancel(self, task_id: str):
        try:
            self.rpc("CancelTask", {"name": f"tasks/{task_id}", "id": task_id})
        except Exception as e:                                   # noqa: BLE001
            log(f"[buyer] cancel failed, stopping anyway: {e}")


def state(t) -> str:
    return (t or {}).get("status", {}).get("state", "")


def message_text(t) -> str:
    parts = (t or {}).get("status", {}).get("message", {}).get("parts", [])
    return " ".join(p.get("text", "") for p in parts)


def last_number(t):
    """The seller's ask: the last number in its message, as the protocol note says."""
    nums = re.findall(r"\d+(?:\.\d+)?", message_text(t))
    return int(float(nums[-1])) if nums else None


def receipt_price(t):
    for a in (t or {}).get("artifacts", []):
        for p in a.get("parts", []):
            d = p.get("data")
            if isinstance(d, dict) and isinstance(d.get("price"), (int, float)):
                return d["price"]
    return None


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    base, item, budget = sys.argv[1], sys.argv[2], int(sys.argv[3])
    log(f"[buyer] shopping for {item}, budget {budget}, at {base}")

    c = Client(base)
    t = c.send(f"I want to buy a {item}")
    if t is None or state(t) != "TASK_STATE_INPUT_REQUIRED":
        log(f"[buyer] the shop did not open a negotiation ({state(t) or 'error'}); stopping")
        return 0
    task_id = t["id"]

    for rounds in range(MAX_ROUNDS):
        ask = last_number(t)
        if ask is not None and ask <= budget:
            offer = ask                       # within budget: take it
            log(f"[buyer] ask {ask} is within {budget}, accepting")
        else:
            offer = decide_offer(item, budget, ask, rounds)
            log(f"[buyer] ask {ask} is over {budget}, offering {offer}")

        if offer > budget:                    # the guarantee, in code
            log(f"[buyer] refusing to offer {offer} over budget {budget}; walking away")
            break

        t = c.send(str(offer), task_id)
        if t is None:
            break
        if state(t) != "TASK_STATE_INPUT_REQUIRED":
            break
    else:
        log(f"[buyer] {MAX_ROUNDS} rounds without a deal; walking away")

    if state(t) == "TASK_STATE_COMPLETED":
        log(f"[buyer] bought {item} at {receipt_price(t)}")
    else:
        log(f"[buyer] no deal ({state(t) or 'error'})")
        if state(t) == "TASK_STATE_INPUT_REQUIRED":
            c.cancel(task_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
