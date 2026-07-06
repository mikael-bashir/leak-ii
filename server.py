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
# Leak PyPantograph service (interactive proof state)
# -----------------------------------------------------------------------------
# The CONSTRUCTION surface for hard proofs: init_proof gives a goal state, and
# apply_tactic advances it one tactic at a time, returning the resulting goals —
# the Lean Infoview as an API. This is what lets an agent grind a decomposed
# lemma it can't one-shot: apply a tactic, SEE the surviving hypotheses / goal /
# elaborated signature, adjust. Whole-script checking (verify_full_script) lives
# on the separate LSP-daemon space and remains the trusted final gate; this box
# is the scratchpad, not the judge.
#
# Hardened vs the old combined server:
#   - ONE Mathlib here (no co-resident LSP daemon) -> no double-load OOM.
#   - A single lock serialises ALL access to the shared Pantograph subprocess;
#     concurrent goal_start/goal_tactic used to corrupt its stdio pipes and crash.
#   - proof_ledger is a bounded LRU (was unbounded -> slow OOM as states leaked).
#   - Mathlib is warmed in the background so the port opens immediately.
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

_lean_server = None
_lean_lock = asyncio.Lock()   # serialises the single Pantograph subprocess
proof_ledger: "OrderedDict[str, dict]" = OrderedDict()
_op_count = 0


def get_lean_server():
    """Lazily construct the Pantograph subprocess (loads Mathlib — slow, once)."""
    global _lean_server
    if _lean_server is None:
        project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        logger.info("🚨 [PANTO] constructing Pantograph Server (loading Mathlib)…")
        _lean_server = Server(imports=["Mathlib"], project_path=project_dir, timeout=300)
        logger.info("✅ [PANTO] Pantograph Server ready")
    return _lean_server


def _ledger_put(state_id: str, record: dict):
    proof_ledger[state_id] = record
    proof_ledger.move_to_end(state_id)
    while len(proof_ledger) > LEDGER_MAX:
        old, _ = proof_ledger.popitem(last=False)
        logger.info(f"🧹 [LEDGER] evicted oldest state {old[:8]} (cap {LEDGER_MAX})")


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
    """
    global _op_count
    _op_count += 1
    n = _op_count
    state_id = str(uuid.uuid4())
    preview = " ".join(proposition.split())[:200]
    logger.info("─" * 60)
    logger.info(f"🎯 [#{n}] init_proof: {preview}")
    t0 = time.time()
    try:
        async with _lean_lock:
            # Pantograph's sync API drives its own event loop internally, so it
            # must run OFF this thread (else "event loop already running"); the
            # lock guarantees only one call touches the subprocess at a time.
            goal_state = await asyncio.wait_for(
                asyncio.to_thread(lambda: get_lean_server().goal_start(proposition)),
                timeout=TOOL_TIMEOUT,
            )
        _ledger_put(state_id, {"state": goal_state, "prop": proposition, "tactics": []})
        logger.info(f"✅ [#{n}] initialised {state_id[:8]} in "
                    f"{int((time.time()-t0)*1000)}ms  (ledger={len(proof_ledger)})")
        return f"Proof initialized. State ID: {state_id}\nCurrent Goal(s):\n{goal_state}"
    except Exception as e:
        logger.error(f"💥 [#{n}] init_proof failed: {e}")
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

    logger.info(f"🔧 [#{n}] apply_tactic {state_id[:8]}: {' '.join(tactic.split())[:160]}")
    t0 = time.time()
    try:
        async with _lean_lock:
            new_state = await asyncio.wait_for(
                asyncio.to_thread(lambda: get_lean_server().goal_tactic(record["state"], tactic)),
                timeout=TOOL_TIMEOUT,
            )
        record["state"] = new_state
        record["tactics"].append(tactic)
        _ledger_put(state_id, record)
        state_str = str(new_state).strip()
        ms = int((time.time() - t0) * 1000)
        if not state_str or state_str == "no goals":
            script = f"theorem auto_proof : {record['prop']} := by\n"
            for tac in record["tactics"]:
                script += f"  {tac}\n"
            logger.info(f"🏁 [#{n}] proof complete for {state_id[:8]} in {ms}ms")
            return ("Tactic succeeded! Proof complete. No goals remaining.\n\n"
                    f"Verified script:\n```lean4\n{script}```")
        logger.info(f"✅ [#{n}] tactic ok in {ms}ms; goals remain")
        return f"Tactic succeeded. New Goals:\n{state_str}"
    except Exception as e:
        logger.info(f"❌ [#{n}] tactic failed: {e}")
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
async def cleanup_memory() -> str:
    """Clear all saved proof states to free RAM. Call when switching problems."""
    count = len(proof_ledger)
    proof_ledger.clear()
    logger.info(f"🧹 [LEDGER] cleared {count} states on request")
    return f"Memory cleared. {count} previous state ID(s) are now invalid."


# =============================================================================
# BOOT
# =============================================================================
async def _warmup():
    logger.info("⏳ Warmup: constructing Pantograph + loading Mathlib "
                "(first load can take a while on a small CPU)…")
    try:
        # Building the Server loads Mathlib; do a trivial goal to force it fully.
        async with _lean_lock:
            await asyncio.to_thread(lambda: get_lean_server().goal_start("True"))
        logger.info("✅ Warmup complete — Pantograph + Mathlib resident.")
    except Exception as e:
        logger.error(f"⚠️  Warmup did not finish: {e}")


async def main_serve():
    logger.info("=" * 60)
    logger.info("Booting Leak PyPantograph service…")
    logger.info("=" * 60)

    # Warm in the background so the port opens immediately (HF marks healthy);
    # the lock makes the first real init_proof wait behind the warmup.
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
