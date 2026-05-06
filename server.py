import asyncio
import uuid
import os
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pantograph import Server
from starlette.middleware.cors import CORSMiddleware
import json
import logging
import uvicorn
import traceback

import nest_asyncio
nest_asyncio.apply()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# 1. Initialize FastMCP
mcp = FastMCP(
    "Leak-II",
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False,
    )
)

# 2. Global Configuration & State Management
TOOL_TIMEOUT = 300.0  
lean_server = None
proof_ledger = {}  

def get_lean_server():
    """Lazy initialization of the Pantograph subprocess."""
    global lean_server
    if lean_server is None:
        project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        lean_server = Server(
            imports=["Mathlib"], 
            project_path=project_dir,
            timeout=300
        )
    return lean_server

# ==========================================
# THE FAST COMPILER DAEMON (LSP PROTOCOL)
# ==========================================
# ==========================================
# THE FAST COMPILER DAEMON (LSP PROTOCOL)
# ==========================================
class LeanCompilerDaemon:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        self.lock = asyncio.Lock()
        self.version = 0
        self.uri = f"file://{os.path.abspath(self.project_dir)}/virtual_sandbox.lean"
        self.stderr_task: asyncio.Task | None = None

    async def boot(self):
        """Boots the persistent Lean LSP server and completes the JSON-RPC handshake."""
        if self.process and self.process.returncode is None:
            return
            
        logger.info("🚨 Booting Persistent Lean Compiler (LSP)...")
        self.process = await asyncio.create_subprocess_exec(
            "lake", "serve",
            cwd=self.project_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        # Drain stderr continuously so the OS buffer doesn't fill and freeze the process
        self.stderr_task = asyncio.create_task(self._log_stderr())
        
        # 1. LSP requires an 'initialize' request
        await self._send_msg("initialize", {
            "processId": None,
            "rootUri": f"file://{os.path.abspath(self.project_dir)}",
            "capabilities": {}
        }, msg_id=1)
        
        # 2. Wait for the server to acknowledge initialization
        while True:
            msg = await asyncio.wait_for(self._read_msg(), timeout=30.0)
            if msg.get("id") == 1:
                break
                
        # 3. Send the 'initialized' notification
        await self._send_msg("initialized", {})
        logger.info("✅ Warm Compiler Online and Ready.")

    async def _log_stderr(self):
        """Constantly reads the Lean compiler's standard error to prevent freezing."""
        if not self.process or not self.process.stderr:
            return
        while True:
            try:
                line = await self.process.stderr.readline()
                if not line:
                    break
                # Only log warnings/errors to avoid spamming stdout
                err_text = line.decode('utf-8').strip()
                if "error" in err_text.lower() or "warning" in err_text.lower():
                    logger.warning(f"[LSP STDERR]: {err_text}")
            except Exception:
                break

    async def _send_msg(self, method: str, params: dict, msg_id: int | None = None):
        """Formats and sends a JSON-RPC message to the Lean server."""
        if not self.process or not self.process.stdin:
            raise BrokenPipeError("LSP Process is dead or missing stdin.")
            
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if msg_id is not None:
            msg["id"] = msg_id
            
        body = json.dumps(msg).encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('utf-8')
        
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()

    async def _read_msg(self) -> dict:
        """Reads HTTP-style headers and decodes the JSON-RPC response."""
        if not self.process or not self.process.stdout:
            raise EOFError("Process is dead or stdout missing.")
            
        content_length = 0
        while True:
            line_bytes = await self.process.stdout.readline()
            if not line_bytes:
                raise EOFError("Lean server closed connection unexpectedly.")
                
            line = line_bytes.decode('utf-8').strip()
            if not line:
                break
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
                
        if content_length == 0:
            raise ValueError("Empty Content-Length header received.")
            
        body = await self.process.stdout.readexactly(content_length)
        return json.loads(body.decode('utf-8'))

    async def verify_script(self, script: str) -> str:
        """Sends the script to the warm daemon and waits for diagnostics."""
        async with self.lock:
            if not self.process or self.process.returncode is not None:
                logger.warning("Daemon dead. Rebooting...")
                await self.boot()
                
            self.version += 1
            full_text = script if "import Mathlib" in script else f"import Mathlib\n\n{script}"
            
            try:
                await self._send_msg("textDocument/didOpen", {
                    "textDocument": {
                        "uri": self.uri,
                        "languageId": "lean",
                        "version": self.version,
                        "text": full_text
                    }
                })
                
                latest_diagnostics = []
                is_processing = True
                
                logger.info("Waiting for LSP compilation to complete...")
                
                # CRITICAL FIX: We must listen to the $/lean/fileProgress stream.
                # We update the diagnostics array as they come in, but we ONLY break
                # the loop when the 'processing' array is empty (meaning Lean is done).
                while is_processing:
                    # Massive 180s timeout. Mathlib is huge, give it time if it needs it.
                    msg = await asyncio.wait_for(self._read_msg(), timeout=180.0)
                    method = msg.get("method")
                    
                    if method == "textDocument/publishDiagnostics":
                        params = msg.get("params", {})
                        if params.get("uri") == self.uri:
                            latest_diagnostics = params.get("diagnostics", [])
                            
                    elif method == "$/lean/fileProgress":
                        params = msg.get("params", {})
                        doc = params.get("textDocument", {})
                        if doc.get("uri") == self.uri:
                            processing = params.get("processing")
                            # An empty list means Lean has finished all compilation for this file
                            if isinstance(processing, list) and len(processing) == 0:
                                is_processing = False
                                
                await self._send_msg("textDocument/didClose", {
                    "textDocument": {"uri": self.uri}
                })
                
                errors = [d for d in latest_diagnostics if d.get("severity", 1) == 1]
                
                if not errors:
                    return "✅ Compilation Successful! The proof is 100% verified and structurally sound."
                    
                error_msgs = []
                for e in errors:
                    line_num = e.get("range", {}).get("start", {}).get("line", 0) + 1
                    message = e.get("message", "Unknown error")
                    error_msgs.append(f"Line {line_num}: {message}")
                    
                return "❌ Compilation Failed:\n" + "\n".join(error_msgs)
                
            except asyncio.TimeoutError:
                return "❌ Verification Error: The Lean LSP server timed out (took >180s)."
            except Exception:
                err_str = traceback.format_exc()
                logger.error(f"Internal LSP Error:\n{err_str}")
                if self.process:
                    try:
                        self.process.kill()
                    except Exception:
                        pass
                    self.process = None
                return f"❌ Verification Error (LSP Crash):\n{err_str}"

fast_compiler = LeanCompilerDaemon()

# ==========================================
# MCP TOOLS
# ==========================================

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
        
        proof_ledger[state_id] = {
            "state": goal_state,
            "prop": proposition,
            "tactics": []
        }
        return f"Proof initialized. State ID: {state_id}\nCurrent Goal(s):\n{goal_state}"
        
    except Exception as e:
        return f"Error initializing proof: {str(e)}"

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
        
        record["state"] = new_state
        record["tactics"].append(tactic)
        
        state_str = str(new_state).strip()
        if not state_str or state_str == "no goals":
            verified_script = f"theorem auto_proof : {record['prop']} := by\n"
            for t in record["tactics"]:
                verified_script += f"  {t}\n" 
                
            return f"Tactic succeeded! Proof complete. No goals remaining.\n\nHere is the 100% verified Lean script:\n```lean4\n{verified_script}\n```"
        else:
            return f"Tactic succeeded. New Goals:\n{state_str}"
            
    except Exception as e:
        return f"Tactic failed: {str(e)}"

@mcp.tool()
async def get_current_proof_state(state_id: str) -> str:
    """
    Returns the fully verified Lean 4 proof script constructed so far AND the current open goals.
    Use this when you need to check your progress or see what mathematical targets remain unproven.
    """
    if state_id not in proof_ledger:
        return f"Error: State ID '{state_id}' not found."
        
    record = proof_ledger[state_id]
    current_state = record["state"]
    
    script = f"theorem partial_proof : {record['prop']} := by\n"
    if not record["tactics"]:
        script += "  -- no tactics applied yet\n"
    else:
        for t in record["tactics"]:
            script += f"  {t}\n"
            
    goals = str(current_state).strip()
    if not goals or goals == "no goals":
        goals = "No goals remaining! The proof is complete."
        
    return (
        f"=== LEAN 4 SCRIPT SO FAR ===\n"
        f"```lean4\n{script}```\n\n"
        f"=== CURRENT OPEN GOALS ===\n"
        f"{goals}"
    )

@mcp.tool()
async def verify_full_script(script: str) -> str:
    """
    Tests an entire Lean 4 script for compilation errors instantly using the LSP daemon.
    Use this to verify your final proof script before considering the problem completely solved.
    
    IMPORTANT: Your script MUST include necessary imports (e.g., 'import Mathlib').
    """
    try:
        return await fast_compiler.verify_script(script)
    except Exception:
        err_trace = traceback.format_exc()
        logger.error(f"MCP Tool Exception:\n{err_trace}")
        return f"❌ Unexpected Python error during verification:\n{err_trace}"

@mcp.tool()
async def cleanup_memory() -> str:
    """
    Forces the server to clear all saved proof states to free up RAM.
    Agents should call this when switching to entirely new problems.
    """
    proof_ledger.clear()
    return "Memory cleared. All previous state IDs are now invalid."

async def main_serve():
    logger.info("Booting Lean LSP Daemon...")
    await fast_compiler.boot()
    
    logger.info("⏳ Pushing dummy request to load Mathlib into memory. This may take 15-30 seconds...")
    dummy_script = "import Mathlib\ntheorem warmup : 1 + 1 = 2 := by rdfascdfgasgfl"
    warmup_result = await fast_compiler.verify_script(dummy_script)
    logger.info(f"✅ Mathlib Warmup Complete. Daemon is locked in RAM! Result: {warmup_result}")

    # 1. Grab the standard Starlette ASGI application
    http_app = mcp.sse_app()
    
    # 2. Add the CORS middleware exactly like Leak-I
    http_app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*", "mcp-protocol-version", "mcp-session-id"], 
        expose_headers=["mcp-session-id"]
    )
    
    # 3. Start Uvicorn programmatically so it shares the CURRENT event loop
    logger.info("Booting up Leak-II environment...")
    config = uvicorn.Config(
        http_app, 
        host="0.0.0.0", 
        port=7860,
        proxy_headers=True,               
        forwarded_allow_ips="*",
        log_level="info"
    )
    server = uvicorn.Server(config)
    await server.serve()

if __name__ == "__main__":
    asyncio.run(main_serve())