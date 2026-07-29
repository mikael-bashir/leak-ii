import asyncio
import os
import uuid
import json
import time
import logging
import traceback
from collections import OrderedDict
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from pantograph import Server
import uvicorn

# =============================================================================
# Leak PyPantograph service (interactive proof state) — WORKER POOL
# -----------------------------------------------------------------------------
# The CONSTRUCTION surface for hard proofs: init_proof gives a goal state, and
# apply_tactic advances it one tactic at a time, returning the resulting goals —
# the Lean Infoview as an API. Whole-script checking (verify_full_script) lives
# on the separate LSP-daemon space and remains the trusted final gate; this box
# is the scratchpad, not the judge.
#
# Parallelism model (the reason for the pool):
#   Pantograph's wire protocol is one-write-then-one-read on a single stdio
#   pipe with NO request ids, so a single subprocess can never serve two
#   concurrent calls — responses would be misattributed or the reader crashes.
#   Instead of one global lock serialising every caller (the old design), we
#   run POOL_SIZE independent Pantograph subprocesses. Each worker has its own
#   lock; every proof state is pinned at init_proof time to the worker that
#   created it, and all later calls on that state go through that worker's
#   lock. Callers on DIFFERENT states therefore run truly in parallel, while
#   any one subprocess still sees strictly serial traffic.
#
#   Memory: each worker holds its own copy of Mathlib (~2-4 GB resident).
#   LEAK2_POOL_SIZE tunes the pool (default 2); set it to 1 to restore the
#   exact old single-subprocess footprint.
#
# Hardened behaviors kept from the previous revision:
#   - proof_ledger is a bounded LRU (was unbounded -> slow OOM as states leaked).
#   - Workers are warmed in the background so the port opens immediately.
#   - A dead subprocess self-heals via in-place restart instead of wedging.
# New in this revision:
#   - cleanup_memory takes an optional state_id: agents free ONLY their own
#     state instead of nuking every parallel caller's work (the old global
#     clear is still available by passing nothing, for single-agent use).
#   - Lean-side garbage collection: dropped/evicted states now actually get
#     goal.delete'd in the owning subprocess (server.gc()); previously freed
#     Python records leaked their Lean goal states forever.
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("leak-pantograph")

mcp = FastMCP(
    "Leak-Pantograph",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
)

TOOL_TIMEOUT = 300.0          # per Pantograph call
LEDGER_MAX = 256              # max live proof states kept (LRU-evicted)
POOL_SIZE = max(1, int(os.environ.get("LEAK2_POOL_SIZE", "2")))


class PantographWorker:
    """One Pantograph subprocess + the lock that serialises access to it."""

    def __init__(self, idx: int):
        self.idx = idx
        self.server = None            # pantograph.Server, constructed lazily
        self.lock = asyncio.Lock()    # serialises THIS subprocess only


_pool: "list[PantographWorker]" = [PantographWorker(i) for i in range(POOL_SIZE)]
proof_ledger: "OrderedDict[str, dict]" = OrderedDict()
_op_count = 0


def get_lean_server(worker: PantographWorker, force_restart: bool = False):
    """Lazily construct a worker's Pantograph subprocess (loads Mathlib — slow,
    once per worker).

    Also self-heals a DEAD subprocess. PyPantograph nulls out `server.proc`
    (via its internal `_close()`) whenever a call times out, hits a decode
    error, or otherwise crashes — expected behavior, and PyPantograph ships
    `server.restart()` specifically to recover from it. A dead `.proc`
    triggers an in-place `.restart()` instead of being silently cached
    forever (which used to wedge every future call until a container restart).
    """
    if worker.server is None:
        project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        logger.info(f"🚨 [PANTO w{worker.idx}] constructing Pantograph Server (loading Mathlib)…")
        worker.server = Server(imports=["Mathlib"], project_path=project_dir, timeout=300)
        logger.info(f"✅ [PANTO w{worker.idx}] Pantograph Server ready")
    elif force_restart or worker.server.proc is None:
        logger.warning(f"♻️  [PANTO w{worker.idx}] subprocess is dead (proc=None) — restarting it")
        worker.server.restart()
        logger.info(f"✅ [PANTO w{worker.idx}] Pantograph Server restarted")
    return worker.server


async def _call_pantograph(worker: PantographWorker, fn):
    """Run fn(server) off the event loop (Pantograph's sync API drives its own
    event loop internally, so it must run off-thread). If the subprocess turns
    out to be dead — a timeout mid-call also nulls `.proc`, so the failure can
    surface on the SAME call that killed it, not just the next one — retry
    ONCE against a freshly restarted server rather than propagating the error.

    Caller MUST hold worker.lock.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(lambda: fn(get_lean_server(worker))), timeout=TOOL_TIMEOUT
        )
    except Exception as e:
        if "Server not running" in str(e):
            logger.warning(f"♻️  [w{worker.idx}] call hit a dead subprocess — restarting and retrying once")
            return await asyncio.wait_for(
                asyncio.to_thread(lambda: fn(get_lean_server(worker, force_restart=True))),
                timeout=TOOL_TIMEOUT,
            )
        raise


def _live_count(idx: int) -> int:
    return sum(1 for r in proof_ledger.values() if r.get("worker", 0) == idx)


def _pick_worker() -> PantographWorker:
    """Route a NEW proof state to the best worker: prefer warmed subprocesses,
    then idle (unlocked) ones, then the fewest live states. A cold worker is
    only chosen while nothing is warmed yet (the boot window), matching the
    old single-worker behavior of the first call paying the Mathlib load."""
    warmed = [w for w in _pool if w.server is not None]
    candidates = warmed if warmed else _pool
    return min(candidates, key=lambda w: (w.lock.locked(), _live_count(w.idx), w.idx))


async def _gc_worker(idx: int):
    """Best-effort Lean-side garbage collection on one worker. Freed Python
    GoalStates register their ids in the server's to_remove list; server.gc()
    sends the actual goal.delete. Never restarts a subprocess just to gc."""
    w = _pool[idx]
    if w.server is None or w.server.proc is None:
        return
    try:
        async with w.lock:
            if w.server is None or w.server.proc is None:
                return
            await asyncio.wait_for(asyncio.to_thread(w.server.gc), timeout=60)
    except Exception as e:
        logger.warning(f"⚠️  [w{idx}] Lean-side gc failed (non-fatal): {e}")


def _ledger_put(state_id: str, record: dict):
    proof_ledger[state_id] = record
    proof_ledger.move_to_end(state_id)
    evicted_workers = set()
    while len(proof_ledger) > LEDGER_MAX:
        old, old_rec = proof_ledger.popitem(last=False)
        evicted_workers.add(old_rec.get("worker", 0))
        logger.info(f"🧹 [LEDGER] evicted oldest state {old[:8]} (cap {LEDGER_MAX})")
    for idx in evicted_workers:
        asyncio.get_running_loop().create_task(_gc_worker(idx))


def _ledger_get(state_id: str):
    rec = proof_ledger.get(state_id)
    if rec is not None:
        proof_ledger.move_to_end(state_id)
    return rec


# =============================================================================
# MCP TOOLS
# =============================================================================
@mcp.tool()
async def init_proof(proposition: str) -> str:
    """
    Start a new interactive proof state for a proposition.
    Provide ONLY the proposition (no 'theorem name :' and no ':=' / 'by').
    Returns a State ID plus the current goal(s); advance it with apply_tactic.
    Parallel-safe: independent State IDs can be worked concurrently.
    """
    global _op_count
    _op_count += 1
    n = _op_count
    state_id = str(uuid.uuid4())
    worker = _pick_worker()
    preview = " ".join(proposition.split())[:200]
    logger.info("─" * 60)
    logger.info(f"🎯 [#{n} w{worker.idx}] init_proof: {preview}")
    t0 = time.time()
    try:
        async with worker.lock:
            # The worker's lock guarantees only one call (including a
            # self-heal retry) touches ITS subprocess at a time.
            goal_state = await _call_pantograph(worker, lambda server: server.goal_start(proposition))
        _ledger_put(state_id, {"state": goal_state, "prop": proposition,
                               "tactics": [], "worker": worker.idx})
        logger.info(f"✅ [#{n} w{worker.idx}] initialised {state_id[:8]} in "
                    f"{int((time.time()-t0)*1000)}ms  (ledger={len(proof_ledger)})")
        return f"Proof initialized. State ID: {state_id}\nCurrent Goal(s):\n{goal_state}"
    except Exception as e:
        logger.error(f"💥 [#{n} w{worker.idx}] init_proof failed: {e}")
        return f"Error initializing proof: {e}"


@mcp.tool()
async def apply_tactic(state_id: str, tactic: str) -> str:
    """
    Apply a single Lean 4 tactic to a proof state (by its State ID).
    Returns the resulting goals, or the finished script when no goals remain.
    """
    global _op_count
    _op_count += 1
    n = _op_count
    record = _ledger_get(state_id)
    if record is None:
        logger.info(f"↩️  [#{n}] apply_tactic: unknown state {state_id[:8]}")
        return f"Error: State ID '{state_id}' not found. You may need to re-initialise your proof."
    worker = _pool[record.get("worker", 0)]

    logger.info(f"🔧 [#{n} w{worker.idx}] apply_tactic {state_id[:8]}: {' '.join(tactic.split())[:160]}")
    t0 = time.time()
    try:
        async with worker.lock:
            new_state = await _call_pantograph(worker, lambda server: server.goal_tactic(record["state"], tactic))
        record["state"] = new_state
        record["tactics"].append(tactic)
        _ledger_put(state_id, record)
        state_str = str(new_state).strip()
        ms = int((time.time() - t0) * 1000)
        if not state_str or state_str == "no goals":
            script = f"theorem auto_proof : {record['prop']} := by\n"
            for tac in record["tactics"]:
                script += f"  {tac}\n"
            logger.info(f"🏁 [#{n} w{worker.idx}] proof complete for {state_id[:8]} in {ms}ms")
            return ("Tactic succeeded! Proof complete. No goals remaining.\n\n"
                    f"Verified script:\n```lean4\n{script}```")
        logger.info(f"✅ [#{n} w{worker.idx}] tactic ok in {ms}ms; goals remain")
        return f"Tactic succeeded. New Goals:\n{state_str}"
    except Exception as e:
        logger.info(f"❌ [#{n} w{worker.idx}] tactic failed: {e}")
        return f"Tactic failed: {e}"


@mcp.tool()
async def get_current_proof_state(state_id: str) -> str:
    """
    Show the tactic script built so far AND the current open goals for a State ID.
    """
    record = _ledger_get(state_id)
    if record is None:
        return f"Error: State ID '{state_id}' not found."
    script = f"theorem partial_proof : {record['prop']} := by\n"
    if not record["tactics"]:
        script += "  -- no tactics applied yet\n"
    else:
        for tac in record["tactics"]:
            script += f"  {tac}\n"
    goals = str(record["state"]).strip()
    if not goals or goals == "no goals":
        goals = "No goals remaining! The proof is complete."
    return (f"=== LEAN 4 SCRIPT SO FAR ===\n```lean4\n{script}```\n\n"
            f"=== CURRENT OPEN GOALS ===\n{goals}")


@mcp.tool()
async def cleanup_memory(state_id: str = "") -> str:
    """
    Free proof states to release RAM.
    Pass the state_id of a state YOU created to free just that one — always do
    this when you're finished with a state, and never touch ids you don't own.
    Calling with NO state_id clears EVERY state on the server; only do that
    when you know no one else is proving in parallel.
    """
    if state_id:
        record = proof_ledger.pop(state_id, None)
        if record is None:
            return f"State ID '{state_id}' not found (already freed or never existed)."
        idx = record.get("worker", 0)
        del record
        asyncio.get_running_loop().create_task(_gc_worker(idx))
        logger.info(f"🧹 [LEDGER] freed state {state_id[:8]} (ledger={len(proof_ledger)})")
        return f"State '{state_id}' freed. Other states are untouched."

    count = len(proof_ledger)
    touched = {r.get("worker", 0) for r in proof_ledger.values()}
    proof_ledger.clear()
    for idx in touched:
        asyncio.get_running_loop().create_task(_gc_worker(idx))
    logger.info(f"🧹 [LEDGER] cleared {count} states on request (global)")
    return f"Memory cleared. {count} previous state ID(s) are now invalid."


# =============================================================================
# BOOT
# =============================================================================
async def _warmup():
    """Warm every worker sequentially in the background. The port opens
    immediately (HF marks healthy); each worker's lock makes real calls wait
    behind that worker's own warmup only."""
    for w in _pool:
        logger.info(f"⏳ Warmup w{w.idx}: constructing Pantograph + loading Mathlib "
                    "(first load can take a while on a small CPU)…")
        try:
            # Building the Server loads Mathlib; a trivial goal forces it fully.
            async with w.lock:
                await asyncio.to_thread(lambda w=w: get_lean_server(w).goal_start("True"))
            logger.info(f"✅ Warmup w{w.idx} complete — Pantograph + Mathlib resident.")
        except Exception as e:
            logger.error(f"⚠️  Warmup w{w.idx} did not finish: {e}")


async def main_serve():
    logger.info("=" * 60)
    logger.info(f"Booting Leak PyPantograph service… (pool size: {POOL_SIZE})")
    logger.info("=" * 60)

    asyncio.create_task(_warmup())

    http_app = mcp.sse_app()
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*", "mcp-protocol-version", "mcp-session-id"],
        expose_headers=["mcp-session-id"],
    )
    logger.info("🌐 Serving MCP (SSE) on 0.0.0.0:7860")
    config = uvicorn.Config(
        http_app, host="0.0.0.0", port=7860,
        proxy_headers=True, forwarded_allow_ips="*",
        log_level="info", loop="asyncio",
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(main_serve())
