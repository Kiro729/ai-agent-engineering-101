"""Week 07 -- the shop on Cloudflare Workers.

The negotiation is shop_core, the same module the market test checks on the
PC (build.py copies it in). This file is what only Cloudflare has:

    harness    an HTTP model. The OpenAI API when the OPENAI_API_KEY secret
               and OPENAI_MODEL are set; Workers AI (CF_MODEL) when they are
               not or the call fails; and "" when both fail, which shop_core
               answers with its rules. The reserve is code either way.
    state      one Durable Object, "shop", holds every open negotiation and
               the journal in its SQLite storage. Consecutive messages of a
               task can arrive at different Cloudflare machines; they all meet
               the same object, so a deal cannot be half-remembered.
    key        the SHOP_KEY secret holds shop_key.pem, the key whose public
               half the kit registered with the market.
    catalogue  the market's three items only (shop_data, built from catalog.json).
    A2A        JSON-RPC by hand -- SendMessage, GetTask, CancelTask -- in the
               JSON shapes the a2a SDK produces on the PC, which were captured
               from the PC shop and compared field by field.
"""
import json
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from js import AbortSignal, Object, fetch
from pyodide.ffi import to_js
from workers import DurableObject, Response, WorkerEntrypoint

import catalog
import market_anchor
import shop_core
from negotiation_memory import Journal
from shop_data import ASKS, EPISODES, ITEMS

catalog.use(ITEMS)

STATE = {"input-required": "TASK_STATE_INPUT_REQUIRED", "completed": "TASK_STATE_COMPLETED",
         "rejected": "TASK_STATE_REJECTED"}
TERMINAL = {"TASK_STATE_COMPLETED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED", "TASK_STATE_FAILED"}
KEEP_TASKS = 3600         # finished tasks stay readable (GetTask) this long


def js(obj):
    return to_js(obj, dict_converter=Object.fromEntries)


def now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ok(rid, result) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result})


def err(rid, code: int, message: str, reason: str | None = None) -> str:
    e = {"code": code, "message": message}
    if reason:
        e["data"] = [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": reason,
                      "domain": "a2a-protocol.org", "metadata": {}}]
    return json.dumps({"jsonrpc": "2.0", "id": rid, "error": e})


def rows(cursor) -> list[dict]:
    """A SQL cursor's rows as dicts, whether the runtime hands back JS objects or Python ones."""
    out = cursor.toArray()
    out = out.to_py() if hasattr(out, "to_py") else out
    return [r.to_py() if hasattr(r, "to_py") else r for r in out]


def env_str(env, name: str) -> str:
    v = getattr(env, name, None)
    return v if isinstance(v, str) else ""


class Shop(DurableObject):
    """Every negotiation of the shop, in one place."""

    def _setup(self) -> None:
        if getattr(self, "shop", None):
            return
        from cryptography.hazmat.primitives import serialization
        self.sql = self.ctx.storage.sql
        self.sql.exec("CREATE TABLE IF NOT EXISTS journal (j TEXT)")
        self.sql.exec("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, j TEXT, t REAL)")
        episodes = [json.loads(r["j"]) for r in rows(self.sql.exec("SELECT j FROM journal"))]
        if not episodes:                                # first start: carry the PC's memory over
            for ep in EPISODES:
                self.sql.exec("INSERT INTO journal (j) VALUES (?)", json.dumps(ep))
            episodes = list(EPISODES)
        journal = Journal(episodes, lambda ep: self.sql.exec("INSERT INTO journal (j) VALUES (?)", json.dumps(ep)))
        pem = env_str(self.env, "SHOP_KEY")
        key = serialization.load_pem_private_key(pem.encode(), password=None) if pem else None
        self.shop = shop_core.Shop(self.ask, journal, key=key, asks=ASKS.get, market_mean=self.market_mean)
        self.open: dict[str, dict] = {}
        self.stats = {"t": 0.0, "items": {}}

    # ------------------------------------------------------------ the harness
    async def ask(self, prompt: str) -> str:
        t0 = time.time()
        if env_str(self.env, "OPENAI_API_KEY") and env_str(self.env, "OPENAI_MODEL"):
            try:
                body = {"model": env_str(self.env, "OPENAI_MODEL"),
                        "messages": [{"role": "user", "content": prompt}], "max_completion_tokens": 400}
                body.update(json.loads(env_str(self.env, "OPENAI_EXTRA") or "{}"))
                r = await fetch("https://api.openai.com/v1/chat/completions", js({
                    "method": "POST", "body": json.dumps(body), "signal": AbortSignal.timeout(20000),
                    "headers": {"Authorization": f"Bearer {env_str(self.env, 'OPENAI_API_KEY')}",
                                "Content-Type": "application/json"}}))
                data = (await r.json()).to_py()
                out = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
                if out:
                    print(f"[harness] openai {time.time() - t0:.1f}s -> {out.strip()[:60]!r}", flush=True)
                    return out.strip()
                print(f"[harness] openai gave nothing: {str(data.get('error') or data)[:160]}", flush=True)
            except Exception as e:                      # noqa: BLE001
                print(f"[harness] openai failed: {type(e).__name__}: {e}"[:200], flush=True)
        try:
            res = await self.env.AI.run(env_str(self.env, "CF_MODEL"), js(
                {"messages": [{"role": "user", "content": prompt}], "max_tokens": 32}))
            res = res.to_py() if hasattr(res, "to_py") else res         # the SDK may already convert
            out = res.get("response") if isinstance(res, dict) else None
            print(f"[harness] workers-ai {time.time() - t0:.1f}s -> {str(out)[:60]!r}", flush=True)
            return "" if out is None else str(out).strip()
        except Exception as e:                          # noqa: BLE001  (quota spent, outage -> rules)
            print(f"[harness] workers-ai failed: {type(e).__name__}: {e}"[:200], flush=True)
            return ""

    async def market_mean(self, item_id: str):
        if time.time() - self.stats["t"] > market_anchor.TTL:
            self.stats["t"] = time.time()               # one try per TTL, even when it fails
            try:
                r = await fetch(market_anchor.STATS, js({"signal": AbortSignal.timeout(3000)}))
                self.stats["items"] = market_anchor.items_from((await r.json()).to_py())
            except Exception:                           # noqa: BLE001  (no anchor this time)
                pass
        return market_anchor.mean_of(self.stats["items"], item_id)

    # ------------------------------------------------------------ tasks
    def _save(self, task: dict, deal: dict | None) -> None:
        d = None if deal is None else {**deal, "item": deal["item"]["id"]}   # the item is rebuilt from its id
        self.sql.exec("INSERT OR REPLACE INTO tasks (id, j, t) VALUES (?, ?, ?)",
                      task["id"], json.dumps({"task": task, "deal": d}), time.time())

    def _load(self, tid: str):
        found = rows(self.sql.exec("SELECT j FROM tasks WHERE id = ?", tid))
        if not found:
            return None, None
        row = json.loads(found[0]["j"])
        deal = row["deal"]
        if deal:
            deal["item"] = catalog.by_id(deal["item"])
        return row["task"], self.open.get(tid) or deal

    def _sweep(self) -> None:
        for tid in self.shop.stale(self.open):
            self.shop.walked(self.open.pop(tid))
        self.sql.exec("DELETE FROM tasks WHERE t < ?", time.time() - KEEP_TASKS)

    async def rpc(self, body: str) -> str:
        self._setup()
        try:
            req = json.loads(body)
        except ValueError:
            return err(None, -32700, "Parse error")
        rid, method, params = req.get("id"), req.get("method"), req.get("params") or {}
        self._sweep()
        if method == "SendMessage":
            return await self._send(rid, params)
        if method in ("GetTask", "CancelTask"):
            tid = params.get("id") or str(params.get("name", "")).split("/")[-1]
            task, deal = self._load(tid)
            if task is None:
                return err(rid, -32001, f"Task {tid} not found", "TASK_NOT_FOUND")
            if method == "CancelTask":
                if task["status"]["state"] in TERMINAL:
                    return err(rid, -32002, f"Task {tid} is in terminal state: {task['status']['state']}",
                               "TASK_NOT_CANCELABLE")
                if deal:
                    self.shop.walked(deal)
                self.open.pop(tid, None)
                if task["status"].get("message"):
                    task["history"].append(task["status"]["message"])
                task["status"] = {"state": "TASK_STATE_CANCELED", "timestamp": now()}
                self._save(task, None)
            return ok(rid, task)
        return err(rid, -32601, f"Method not found: {method}")

    async def _send(self, rid, params: dict) -> str:
        if self.shop.key is None:
            return err(rid, -32603, "The shop is not configured yet (no signing key).")
        m = params.get("message") or {}
        text = " ".join(p["text"] for p in m.get("parts") or [] if isinstance(p.get("text"), str))
        tid = m.get("taskId")
        if tid:
            task, deal = self._load(tid)
            if task is None:
                return err(rid, -32001, f"Task {tid} not found", "TASK_NOT_FOUND")
            if task["status"]["state"] in TERMINAL:
                return err(rid, -32004, f"Task {tid} is in terminal state: {task['status']['state']}",
                           "UNSUPPORTED_OPERATION")
        else:
            tid, deal = str(uuid.uuid4()), None
            task = {"id": tid, "contextId": m.get("contextId") or str(uuid.uuid4()), "status": {}, "history": []}
        if task["status"].get("message"):                # the previous answer becomes history
            task["history"].append(task["status"]["message"])
        task["history"].append({"messageId": m.get("messageId") or str(uuid.uuid4()),
                                "contextId": task["contextId"], "taskId": tid, "role": "ROLE_USER",
                                "parts": [{"text": text}]})

        reply, deal = await self.shop.handle(tid, text, deal)

        answer = {"messageId": str(uuid.uuid4()), "contextId": task["contextId"], "taskId": tid,
                  "role": "ROLE_AGENT", "parts": [{"text": reply["text"]}]}
        if reply.get("ask") is not None:
            answer["metadata"] = {"ask": int(reply["ask"])}
        task["status"] = {"state": STATE[reply["state"]], "message": answer, "timestamp": now()}
        if reply.get("receipt"):
            task["artifacts"] = [{"artifactId": str(uuid.uuid4()), "name": "receipt",
                                  "parts": [{"data": reply["receipt"]}]}]
        if deal is None:
            self.open.pop(tid, None)
        else:
            self.open[tid] = deal
        self._save(task, deal)
        return ok(rid, {"task": task})


class Default(WorkerEntrypoint):
    async def fetch(self, request):
        u = urlparse(request.url)
        if request.method == "POST":
            stub = self.env.SHOP.get(self.env.SHOP.idFromName("shop"))
            return Response(await stub.rpc(await request.text()), headers={"content-type": "application/json"})
        if u.path == "/.well-known/agent-card.json":
            description, skill = shop_core.card_text(shop_core.NAME)
            card = {
                "name": shop_core.NAME, "description": description, "version": "1.1.0",
                "supportedInterfaces": [{"url": f"{u.scheme}://{u.netloc}/", "protocolBinding": "JSONRPC",
                                         "protocolVersion": "1.0"}],
                "capabilities": {"streaming": False},
                "defaultInputModes": ["text/plain"], "defaultOutputModes": ["application/json", "text/plain"],
                "skills": [{"id": "negotiate", "name": "Find an item and negotiate a purchase",
                            "description": skill,
                            "tags": ["shopping", "negotiation", "furniture"] + catalog.categories(),
                            "examples": ["I want to buy a lamp", "90"]}],
            }
            return Response(json.dumps(card), headers={"content-type": "application/json"})
        return Response(f"{shop_core.NAME}: A2A JSON-RPC at /, card at /.well-known/agent-card.json\n")
