import asyncio
import uuid
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pantograph import Server

import nest_asyncio
nest_asyncio.apply()

import logging

logging.basicConfig(level=logging.DEBUG)

# 1. Initialize FastMCP
# Binding to 0.0.0.0 exposes the server to the internet/Docker network.
# This bypasses the default localhost-only security restriction.
mcp = FastMCP(
    "Leak-II",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    ),
    dependencies=["pypantograph"],
    host="0.0.0.0", 
    port=7860
)


# 2. Global Configuration & State Management
TOOL_TIMEOUT = 300.0  # Seconds before we kill a hanging Lean tactic
lean_server = None
proof_states = {}    # Maps state_id (str) -> Pantograph State Object

def get_lean_server():
    """Lazy initialization of the Pantograph subprocess."""
    global lean_server
    if lean_server is None:
        # Boot the persistent background Lean process.
        # This assumes your Docker container has 'lake' in its PATH.
        project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        lean_server = Server(
            imports=["Mathlib"], 
            project_path=project_dir,
            timeout=300
        )
    return lean_server

# 3. Expose MCP Tools

@mcp.tool()
async def init_proof(proposition: str) -> str:
    """
    Initializes a new proof state.
    Provide ONLY the mathematical proposition you want to prove.
    DO NOT include 'theorem name :' or the ':=' at the end.
    Correct Example: '1 + 1 = 2'
    Correct Example: '∀ (a b : Nat), a + b = b + a'
    """
    server = get_lean_server()
    state_id = str(uuid.uuid4())
    
    try:
        # We pass the proposition directly to Pantograph
        goal_state = await asyncio.wait_for(
            asyncio.to_thread(server.goal_start, proposition),
            timeout=TOOL_TIMEOUT
        )
        
        proof_states[state_id] = goal_state
        return f"Proof initialized. State ID: {state_id}\nCurrent Goal:\n{goal_state}"
        
    except asyncio.TimeoutError:
        return "Error: Lean timed out while initializing the theorem. Check your syntax."
    except Exception as e:
        return f"Error initializing proof: {str(e)}"

@mcp.tool()
async def apply_tactic(state_id: str, tactic: str) -> str:
    """
    Applies a Lean 4 tactic to a specific proof state.
    Returns the new goal state or an error if the tactic fails.
    """
    # 1. Grab the active server instance!
    server = get_lean_server()
    
    if state_id not in proof_states:
        return f"Error: State ID '{state_id}' not found. You may need to re-initialize."
        
    current_state = proof_states[state_id]
    
    try:
        # 2. Ask the SERVER to apply the tactic to our current state
        new_state = await asyncio.wait_for(
            asyncio.to_thread(server.goal_tactic, current_state, tactic),
            timeout=TOOL_TIMEOUT
        )
        
        # 3. Pantograph returned a new state. Update our dictionary.
        proof_states[state_id] = new_state
        
        # 4. Check if the proof is complete
        # PyPantograph often returns an empty string representation when goals are cleared
        state_str = str(new_state).strip()
        if not state_str or state_str == "no goals":
            return "Tactic succeeded! Proof complete. No goals remaining."
        else:
            return f"Tactic succeeded. New Goals:\n{state_str}"
            
    except asyncio.TimeoutError:
        return f"Error: Tactic '{tactic}' timed out after {TOOL_TIMEOUT} seconds. Lean may be stuck."
    except Exception as e:
        # 5. If the tactic is invalid, Lean rejects it and Pantograph throws an error.
        return f"Tactic failed: {str(e)}"

@mcp.tool()
async def cleanup_memory() -> str:
    """
    Forces the server to clear all saved proof states to free up RAM.
    Agents should call this when switching to entirely new problems.
    """
    proof_states.clear()
    return "Memory cleared. All previous state IDs are now invalid."

if __name__ == "__main__":
    # 4. Start the SSE Server
    # Required for Hugging Face Spaces / Docker deployments
    print("Booting FastMCP Server on 0.0.0.0:7860...")
    
    # We use the SSE transport instead of standard stdio so agents can connect over HTTP
    mcp.run(transport="sse")