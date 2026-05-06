import asyncio
import uuid
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pantograph import Server
import tempfile

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
# proof_states = {}    # Maps state_id (str) -> Pantograph State Object
proof_ledger = {}  # Maps state_id -> {"state": PantographState, "prop": str, "tactics": list}

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
    """
    server = get_lean_server()
    state_id = str(uuid.uuid4())
    
    try:
        goal_state = await asyncio.wait_for(
            asyncio.to_thread(server.goal_start, proposition),
            timeout=TOOL_TIMEOUT
        )
        
        # Start the ledger for this specific proof path
        proof_ledger[state_id] = {
            "state": goal_state,
            "prop": proposition,
            "tactics": []
        }
        return f"Proof initialized. State ID: {state_id}\nCurrent Goal(s):\n{goal_state}"
        
    except Exception as e:
        return f"Error initializing proof: {str(e)}"
    
# async def init_proof(proposition: str) -> str:
#     """
#     Initializes a new proof state.
#     Provide ONLY the mathematical proposition you want to prove.
#     DO NOT include 'theorem name :' or the ':=' at the end.
#     Correct Example: '1 + 1 = 2'
#     Correct Example: '∀ (a b : Nat), a + b = b + a'
#     """
#     server = get_lean_server()
#     state_id = str(uuid.uuid4())
    
#     try:
#         # We pass the proposition directly to Pantograph
#         goal_state = await asyncio.wait_for(
#             asyncio.to_thread(server.goal_start, proposition),
#             timeout=TOOL_TIMEOUT
#         )
        
#         proof_states[state_id] = goal_state
#         return f"Proof initialized. State ID: {state_id}\nCurrent Goal:\n{goal_state}"
        
#     except asyncio.TimeoutError:
#         return "Error: Lean timed out while initializing the theorem. Check your syntax."
#     except Exception as e:
#         return f"Error initializing proof: {str(e)}"

@mcp.tool()
async def apply_tactic(state_id: str, tactic: str) -> str:
    """
    Applies a Lean 4 tactic to a specific proof state.
    """
    server = get_lean_server()
    
    if state_id not in proof_ledger:
        return f"Error: State ID '{state_id}' not found. You may need to re-initialise your proof"
        
    record = proof_ledger[state_id]
    current_state = record["state"]
    
    try:
        new_state = await asyncio.wait_for(
            asyncio.to_thread(server.goal_tactic, current_state, tactic),
            timeout=TOOL_TIMEOUT
        )
        
        # 1. Update the state
        record["state"] = new_state
        # 2. Add the successful tactic to the deterministic ledger
        record["tactics"].append(tactic)
        
        state_str = str(new_state).strip()
        if not state_str or state_str == "no goals":
            # 3. AUTO-GENERATE THE 100% VERIFIED SCRIPT
            verified_script = f"theorem auto_proof : {record['prop']} := by\n"
            for t in record["tactics"]:
                # Basic indentation for readability
                verified_script += f"  {t}\n" 
                
            return f"Tactic succeeded! Proof complete. No goals remaining.\n\nHere is the 100% verified Lean script:\n```lean4\n{verified_script}\n```"
        else:
            return f"Tactic succeeded. New Goals:\n{state_str}"
            
    except Exception as e:
        return f"Tactic failed: {str(e)}"
# async def apply_tactic(state_id: str, tactic: str) -> str:
#     """
#     Applies a Lean 4 tactic to a specific proof state.
#     Returns the new goal state or an error if the tactic fails.
#     """
#     # 1. Grab the active server instance!
#     server = get_lean_server()
    
#     if state_id not in proof_states:
#         return f"Error: State ID '{state_id}' not found. You may need to re-initialize."
        
#     current_state = proof_states[state_id]
    
#     try:
#         # 2. Ask the SERVER to apply the tactic to our current state
#         new_state = await asyncio.wait_for(
#             asyncio.to_thread(server.goal_tactic, current_state, tactic),
#             timeout=TOOL_TIMEOUT
#         )
        
#         # 3. Pantograph returned a new state. Update our dictionary.
#         proof_states[state_id] = new_state
        
#         # 4. Check if the proof is complete
#         # PyPantograph often returns an empty string representation when goals are cleared
#         state_str = str(new_state).strip()
#         if not state_str or state_str == "no goals":
#             return "Tactic succeeded! Proof complete. No goals remaining."
#         else:
#             return f"Tactic succeeded. New Goals:\n{state_str}"
            
#     except asyncio.TimeoutError:
#         return f"Error: Tactic '{tactic}' timed out after {TOOL_TIMEOUT} seconds. Lean may be stuck."
#     except Exception as e:
#         # 5. If the tactic is invalid, Lean rejects it and Pantograph throws an error.
#         return f"Tactic failed: {str(e)}"

@mcp.tool()
async def get_current_proof_state(state_id: str) -> str:
    """
    Returns the fully verified Lean 4 proof script constructed so far AND the current open goals.
    Use this when you need to check your progress or see what mathematical targets remain unproven.
    """
    if state_id not in proof_ledger:
        return f"Error: State ID '{state_id}' not found. The memory may have been cleared or the ID is incorrect."
        
    record = proof_ledger[state_id]
    current_state = record["state"]
    
    # 1. Build the script history
    script = f"theorem partial_proof : {record['prop']} := by\n"
    if not record["tactics"]:
        script += "  -- no tactics applied yet\n"
    else:
        for t in record["tactics"]:
            script += f"  {t}\n"
            
    # 2. Extract the active mathematical goals from Pantograph
    goals = str(current_state).strip()
    if not goals or goals == "no goals":
        goals = "No goals remaining! The proof is complete."
        
    # 3. Format it clearly for the LLM's context window
    return (
        f"=== LEAN 4 SCRIPT SO FAR ===\n"
        f"```lean4\n{script}```\n\n"
        f"=== CURRENT OPEN GOALS ===\n"
        f"{goals}"
    )

@mcp.tool()
async def verify_full_script(script: str) -> str:
    """
    Tests an entire Lean 4 script for compilation errors by running the Lean compiler.
    Use this to verify your final proof script before considering the problem completely solved.
    
    IMPORTANT: Your script MUST include necessary imports (e.g., 'import Mathlib').
    """
    # Write the agent's script to a temporary Lean file
    with tempfile.NamedTemporaryFile(suffix=".lean", delete=False) as f:
        # Prepend the Mathlib import to ensure the environment is correct
        if "import Mathlib" not in script:
            full_script = f"import Mathlib\n\n{script}"
        else:
            full_script = script
            
        f.write(full_script.encode('utf-8'))
        temp_path = f.name

    try:
        project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        
        # Run the actual Lean compiler on the file
        process = await asyncio.create_subprocess_exec(
            "lake", "env", "lean", temp_path,
            cwd=project_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        output = stdout.decode().strip() + "\n" + stderr.decode().strip()
        
        # Lean returns 0 if there are no errors (warnings are fine)
        if process.returncode == 0 and "error:" not in output.lower():
            return "✅ Compilation Successful! The proof is 100% verified and structurally sound.\n\nCompiler Output:\n" + output
        else:
            return f"❌ Compilation Failed. The REPL steps worked, but the final script has errors:\n{output}"
            
    except Exception as e:
        return f"Error running the Lean compiler: {str(e)}"
    finally:
        # Always clean up the temporary file
        if os.path.exists(temp_path):
            os.remove(temp_path)

@mcp.tool()
async def cleanup_memory() -> str:
    """
    Forces the server to clear all saved proof states to free up RAM.
    Agents should call this when switching to entirely new problems.
    """
    proof_ledger.clear()
    return "Memory cleared. All previous state IDs are now invalid."

if __name__ == "__main__":
    # 4. Start the SSE Server
    # Required for Hugging Face Spaces / Docker deployments
    print("Booting FastMCP Server on 0.0.0.0:7860...")
    
    # We use the SSE transport instead of standard stdio so agents can connect over HTTP
    mcp.run(transport="sse")
