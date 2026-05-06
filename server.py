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
from pathlib import Path

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
class LeanCompilerDaemon:
    def __init__(self):
        self.process: asyncio.subprocess.Process | None = None
        self.project_dir = os.environ.get("LEAN_PROJECT_PATH", ".")
        self.lock = asyncio.Lock()
        self.version = 1
        self.sandbox_path = Path(self.project_dir).resolve() / "virtual_sandbox.lean"
        self.uri = self.sandbox_path.as_uri()
        self.stderr_task: asyncio.Task | None = None
        self._is_file_open = False 
        
        logger.info(f"🔍 [INIT] URI Target: {self.uri}")
        try:
            with open(self.sandbox_path, "a") as f: pass
        except Exception as e:
            logger.error(f"❌ [INIT] File touch failed: {e}")

    async def boot(self):
        if self.process and self.process.returncode is None:
            return
            
        logger.info("🚨 [BOOT] Starting 'lake serve' subprocess...")
        self.process = await asyncio.create_subprocess_exec(
            "lake", "serve",
            cwd=self.project_dir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        self.stderr_task = asyncio.create_task(self._log_stderr())
        
        # 1. Handshake
        await self._send_msg("initialize", {
            "processId": os.getpid(),
            "rootUri": f"file://{os.path.abspath(self.project_dir)}",
            "capabilities": {"textDocument": {"synchronization": {"change": 1}}} # Full Sync
        }, msg_id=0)

        while True:
            msg = await asyncio.wait_for(self._read_msg(), timeout=30.0)
            if msg.get("id") == 0:
                logger.info("✅ [BOOT] Handshake Response Received.")
                break
                
        await self._send_msg("initialized", {})
        logger.info("✅ [BOOT] Daemon Online.")

    async def _log_stderr(self):
        if not self.process or not self.process.stderr: return
        while True:
            try:
                line = await self.process.stderr.readline()
                if not line: break
                logger.info(f"🛰️ [LSP-STDERR] {line.decode('utf-8').strip()}")
            except Exception: break

    async def _send_msg(self, method: str, params: dict, msg_id: int | None = None):
        if not self.process or not self.process.stdin: raise BrokenPipeError("LSP Dead")
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if msg_id is not None: msg["id"] = msg_id
        
        payload = json.dumps(msg)
        logger.info(f"📤 [LSP-OUT] {payload}") # REVEAL EVERYTHING
        
        body = payload.encode('utf-8')
        header = f"Content-Length: {len(body)}\r\n\r\n".encode('utf-8')
        self.process.stdin.write(header + body)
        await self.process.stdin.drain()

    async def _read_msg(self) -> dict:
        if not self.process or not self.process.stdout: raise EOFError("LSP Dead")
        content_length = 0
        try:
            while True:
                line_bytes = await self.process.stdout.readline()
                if not line_bytes: raise EOFError("LSP EOF")
                line = line_bytes.decode('utf-8').strip()
                if not line and content_length > 0: break
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":")[1].strip())
            
            body_bytes = await self.process.stdout.readexactly(content_length)
            raw_json = body_bytes.decode('utf-8')
            logger.info(f"📥 [LSP-IN] {raw_json}") # REVEAL EVERYTHING
            return json.loads(raw_json)
        except Exception as e:
            logger.error(f"❌ [READ-ERR] {e}")
            return {}

    async def verify_script(self, script: str) -> str:
        async with self.lock:
            if not self.process or self.process.returncode is not None:
                await self.boot()
                self._is_file_open = False

            self.version += 1
            full_text = (script if "import Mathlib" in script else f"import Mathlib\n\n{script}").strip() + "\n\n"
            
            try:
                if not self._is_file_open:
                    await self._send_msg("textDocument/didOpen", {
                        "textDocument": {"uri": self.uri, "languageId": "lean", "version": self.version, "text": full_text}
                    })
                    self._is_file_open = True
                else:
                    await self._send_msg("textDocument/didChange", {
                        "textDocument": {"uri": self.uri, "version": self.version},
                        "contentChanges": [{"text": full_text}]
                    })
                
                latest_diagnostics = []
                received_correct_version = False
                finished_progress = False
                
                logger.info(f"⏳ [WAITING] Loop start for v{self.version}")
                
                # THE ULTIMATE LOOP: Wait for BOTH diagnostics and empty progress
                start_time = asyncio.get_event_loop().time()
                while not (received_correct_version and finished_progress):
                    # Check for tool-level timeout
                    if asyncio.get_event_loop().time() - start_time > 180.0:
                        raise TimeoutError("Lean timed out (180s)")

                    msg = await self._read_msg()
                    if not msg: continue
                    
                    method = msg.get("method")
                    if method == "textDocument/publishDiagnostics":
                        p = msg.get("params", {})
                        if p.get("uri") == self.uri:
                            v = p.get("version")
                            latest_diagnostics = p.get("diagnostics", [])
                            # Lean 4.x sometimes doesn't send the version in publishDiagnostics
                            # If so, we treat it as potentially the right one
                            if v is None or v == self.version:
                                received_correct_version = True
                                logger.info(f"🎯 [MATCH] Diagnostics for v{v} captured.")
                                
                    elif method == "$/lean/fileProgress":
                        p = msg.get("params", {})
                        if p.get("textDocument", {}).get("uri") == self.uri:
                            processing = p.get("processing", [])
                            if len(processing) == 0:
                                finished_progress = True
                                logger.info("🏁 [MATCH] Lean reported processing finished.")
                            else:
                                logger.info(f"🏗️ [COMPILING] {len(processing)} ranges remaining...")

                # Construct Result
                errors = [d for d in latest_diagnostics if d.get("severity", 1) in (1, 2)]
                if not errors:
                    res = "✅ Compilation Successful! The proof is 100% verified."
                else:
                    res = "❌ Compilation Failed:\n" + "\n".join([f"Line {e['range']['start']['line']+1}: {e['message']}" for e in errors])
                
                logger.info(f"🏁 [VERIFY-COMPLETE] Result length: {len(res)}")
                return res
                
            except Exception as e:
                logger.error(f"💥 [CRITICAL] {traceback.format_exc()}")
                if isinstance(e, (EOFError, BrokenPipeError)):
                    self.process = None # Force reboot on next call
                return f"❌ Verification Error: {e}"

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
    dummy_script = "import Mathlib\ntheorem warmup : 1 + 1 = 2 := by rfl"
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