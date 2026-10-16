#!/usr/bin/env python3
"""Temporary stdio MCP task tools over an existing Codex Unix WebSocket."""

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time


VERSION = "0.1.0"


class BridgeError(Exception):
    def __init__(self, classification, code=None):
        self.classification = classification
        self.code = code
        super().__init__(classification)


class AppServer:
    def __init__(self, command=None, timeout=30, socket_path=None):
        self.timeout = timeout
        self.sequence = 0
        self.responses = {}
        self.turns = {}
        self.closed = False
        self.condition = threading.Condition()
        self.process = None
        self.connection = None
        if socket_path is not None:
            import websocket
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                stream.settimeout(timeout)
                stream.connect(socket_path)
                self.connection = websocket.create_connection(
                    "ws://localhost/", socket=stream, timeout=timeout, suppress_origin=True,
                )
                self.connection.settimeout(None)
            except Exception:
                stream.close()
                raise BridgeError("app_server_socket_connection_failed") from None
        else:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, bufsize=1,
            )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self.request("initialize", {
                "clientInfo": {"name": "homelab_ssh_task_bridge", "version": VERSION},
                "capabilities": {"experimentalApi": True},
            })
            self.notify("initialized", {})
        except BaseException:
            self.close()
            raise

    def _read(self):
        try:
            for line in self._messages():
                message = json.loads(line)
                with self.condition:
                    if "id" in message and "method" not in message:
                        self.responses[message["id"]] = message
                    elif "id" in message:
                        # This bridge is not an approval bypass or an interactive UI.
                        self._write({"id": message["id"], "error": {
                            "code": -32000, "message": "Interactive request requires owning client",
                        }})
                    elif message.get("method") == "turn/completed":
                        params = message["params"]
                        self.turns[(params["threadId"], params["turn"]["id"])] = params["turn"]
                    self.condition.notify_all()
        except Exception:
            pass
        finally:
            with self.condition:
                self.closed = True
                self.condition.notify_all()

    def _messages(self):
        if self.connection is not None:
            while True:
                message = self.connection.recv()
                if not message:
                    break
                yield message
        else:
            yield from self.process.stdout

    def _write(self, message):
        try:
            if self.connection is not None:
                self.connection.send(json.dumps(message))
            else:
                self.process.stdin.write(json.dumps(message) + "\n")
                self.process.stdin.flush()
        except Exception:
            raise BridgeError("app_server_transport_closed") from None

    def notify(self, method, params):
        with self.condition:
            self._write({"method": method, "params": params})

    def request(self, method, params):
        with self.condition:
            self.sequence += 1
            request_id = self.sequence
            self._write({"id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while request_id not in self.responses:
                if self.closed:
                    raise BridgeError("app_server_transport_closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    # A timed-out write may have succeeded. Never retry automatically.
                    raise BridgeError("app_server_timeout_outcome_unknown")
                self.condition.wait(remaining)
            response = self.responses.pop(request_id)
        if "error" in response:
            error = response["error"]
            raise BridgeError("app_server_rpc_error", error.get("code"))
        return response["result"]

    def wait(self, thread_id, turn_id, seconds):
        with self.condition:
            deadline = time.monotonic() + seconds
            key = (thread_id, turn_id)
            while key not in self.turns:
                if self.closed:
                    raise BridgeError("app_server_transport_closed")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"threadId": thread_id, "turnId": turn_id, "status": "waiting"}
                self.condition.wait(remaining)
            turn = self.turns.pop(key)
        return {"threadId": thread_id, "turnId": turn_id, "status": turn["status"]}

    def close(self):
        # Close only this client's transport, never the shared app server.
        if self.connection is not None:
            self.connection.close()
            self.reader.join(timeout=3)
            return
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=3)
        self.reader.join(timeout=3)
        self.process.stdout.close()


def schema(properties, required=()):
    return {"type": "object", "properties": properties,
            "required": list(required), "additionalProperties": False}


STRING = {"type": "string", "minLength": 1}
PARAMS = {"type": "object", "additionalProperties": True}
TOOLS = [
    {"name": "bridge_status", "description":
     "Check the temporary SSH task bridge. Uses the host's existing private app-server socket; no network listener.",
     "inputSchema": schema({})},
    {"name": "create_thread", "description":
     "Create a Codex chat on this execution host, not a subagent. params uses native thread/start fields "
     "(cwd, model, modelProvider, approvalPolicy, sandbox, config). Omitted settings inherit the host config. "
     "Optional prompt starts its first turn. No Git worktree or cross-host handoff is created. "
     "Do not retry an unknown-outcome timeout; inspect threads first.",
     "inputSchema": schema({"params": PARAMS, "prompt": STRING, "title": STRING})},
    {"name": "fork_thread", "description":
     "Fork a chat on this host with native thread/fork params, including required threadId. "
     "Optional prompt starts a turn on the fork. No Git worktree or host migration. Requires user authorization.",
     "inputSchema": schema({"params": PARAMS, "prompt": STRING, "title": STRING}, ["params"])},
    {"name": "send_message_to_thread", "description":
     "Send an authorized prompt to another chat on this host. mode=start resumes an idle chat and starts "
     "a turn; busy chats are refused, never interrupted. mode=steer requires expected_turn_id and app-server "
     "ownership of that active turn. Never target your own chat. params may contain native turn/start overrides "
     "but cannot override threadId or input. Requires explicit human authorization to message the target.",
     "inputSchema": schema({"thread_id": STRING, "prompt": STRING,
                            "mode": {"type": "string", "enum": ["start", "steer"], "default": "start"},
                            "expected_turn_id": STRING, "params": PARAMS}, ["thread_id", "prompt"])},
    {"name": "list_threads", "description":
     "Read-only native thread/list on this host; params supports limit, cursor, cwd, searchTerm, archived.",
     "inputSchema": schema({"params": PARAMS})},
    {"name": "read_thread", "description":
     "Read a chat on this host without starting a turn. include_turns returns stored conversation content.",
     "inputSchema": schema({"thread_id": STRING, "include_turns": {"type": "boolean", "default": False}},
                           ["thread_id"])},
    {"name": "wait_thread", "description":
     "Wait up to 120 seconds for a turn started through this bridge. Returns waiting on timeout, not failure. "
     "Use read_thread for turns started by other clients or before this bridge process.",
     "inputSchema": schema({"thread_id": STRING, "turn_id": STRING,
                            "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 120, "default": 60}},
                           ["thread_id", "turn_id"])},
]

for tool in TOOLS:
    read_only = tool["name"] in {"bridge_status", "list_threads", "read_thread", "wait_thread"}
    tool["annotations"] = {"readOnlyHint": read_only, "destructiveHint": False,
                           "idempotentHint": read_only, "openWorldHint": not read_only}


class TaskTools:
    def __init__(self, server):
        self.server = server

    def _start_turn(self, thread_id, prompt, params=None):
        params = dict(params or {})
        if "threadId" in params or "input" in params:
            raise BridgeError("reserved_parameter")
        params.update(threadId=thread_id, input=[{"type": "text", "text": prompt}])
        result = self.server.request("turn/start", params)
        return {"threadId": thread_id, "turnId": result["turn"]["id"], "status": result["turn"]["status"]}

    def call(self, name, arguments):
        spec = next((tool for tool in TOOLS if tool["name"] == name), None)
        if spec is None:
            raise BridgeError("unknown_tool")
        allowed = spec["inputSchema"]["properties"]
        if set(arguments) - set(allowed) or any(key not in arguments for key in spec["inputSchema"]["required"]):
            raise BridgeError("invalid_arguments")
        for key, value in arguments.items():
            kind = allowed[key]["type"]
            if ((kind == "string" and (not isinstance(value, str) or not value))
                    or (kind == "object" and not isinstance(value, dict))
                    or (kind == "boolean" and not isinstance(value, bool))
                    or (kind == "number" and (isinstance(value, bool) or not isinstance(value, (int, float))))):
                raise BridgeError("invalid_arguments")
        if name == "bridge_status":
            self.server.request("thread/list", {"limit": 1})
            return {"version": VERSION, "transport": "existing-app-server-unix-websocket", "hostLocal": True,
                    "handoffSupported": False}
        if name == "list_threads":
            return self.server.request("thread/list", arguments.get("params", {"limit": 20}))
        if name == "read_thread":
            return self.server.request("thread/read", {"threadId": arguments["thread_id"],
                                      "includeTurns": arguments.get("include_turns", False)})
        if name == "wait_thread":
            seconds = arguments.get("timeout_seconds", 60)
            if not 0 <= seconds <= 120:
                raise BridgeError("invalid_timeout")
            return self.server.wait(arguments["thread_id"], arguments["turn_id"], seconds)
        if name in {"create_thread", "fork_thread"}:
            params = dict(arguments.get("params", {}))
            if name == "fork_thread" and not isinstance(params.get("threadId"), str):
                raise BridgeError("missing_thread_id")
            result = self.server.request("thread/start" if name == "create_thread" else "thread/fork", params)
            thread_id = result["thread"]["id"]
            receipt = {"threadId": thread_id, "status": "created"}
            # Return the created ID even if a secondary naming/turn operation fails.
            try:
                if "title" in arguments:
                    self.server.request("thread/name/set", {"threadId": thread_id, "name": arguments["title"]})
                if "prompt" in arguments:
                    receipt.update(self._start_turn(thread_id, arguments["prompt"]))
            except BridgeError as error:
                receipt.update(partialFailure=error.classification, rpcCode=error.code)
            return receipt
        thread_id = arguments["thread_id"]
        if thread_id == os.environ.get("CODEX_THREAD_ID"):
            raise BridgeError("self_message_refused")
        mode = arguments.get("mode", "start")
        if mode == "steer":
            if not arguments.get("expected_turn_id") or arguments.get("params"):
                raise BridgeError("steer_requires_exact_turn_and_no_overrides")
            return self.server.request("turn/steer", {
                "threadId": thread_id, "expectedTurnId": arguments["expected_turn_id"],
                "input": [{"type": "text", "text": arguments["prompt"]}],
            })
        if mode != "start":
            raise BridgeError("invalid_mode")
        thread = self.server.request("thread/read", {"threadId": thread_id})["thread"]
        if thread.get("status", {}).get("type") not in {"idle", "notLoaded"}:
            raise BridgeError("target_not_confirmed_idle")
        self.server.request("thread/resume", {"threadId": thread_id})
        return self._start_turn(thread_id, arguments["prompt"], arguments.get("params"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, help="Absolute host-local Codex executable")
    parser.add_argument("--socket", required=True, help="Existing owner-local app-server control socket")
    parser.add_argument("--call", help="Invoke a tool once instead of serving MCP")
    parser.add_argument("--arguments", default="{}", help="JSON arguments for --call; never put secrets here")
    args = parser.parse_args()
    if not Path(args.codex).is_absolute() or not Path(args.socket).is_absolute():
        parser.error("Executable and socket must be absolute paths")
    server = None

    def call(name, arguments):
        nonlocal server
        if server is None:
            server = AppServer(socket_path=args.socket)
        return TaskTools(server).call(name, arguments)

    try:
        if args.call:
            try:
                print(json.dumps(call(args.call, json.loads(args.arguments))))
            except BridgeError as error:
                print(json.dumps({"error": error.classification, "rpcCode": error.code}))
                return 1
            return 0
        for line in sys.stdin:
            request_id = None
            try:
                request = json.loads(line)
                request_id = request.get("id")
                if request_id is None:
                    continue
                method = request["method"]
                if method == "initialize":
                    result = {"protocolVersion": request.get("params", {}).get("protocolVersion", "2024-11-05"),
                              "capabilities": {"tools": {}},
                              "serverInfo": {"name": "ssh-task-bridge", "version": VERSION}}
                elif method == "tools/list":
                    result = {"tools": TOOLS}
                elif method == "tools/call":
                    params = request["params"]
                    try:
                        value = call(params["name"], params.get("arguments", {}))
                        result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": False}
                    except BridgeError as error:
                        result = {"content": [{"type": "text", "text": json.dumps({
                            "error": error.classification, "rpcCode": error.code})}], "isError": True}
                elif method == "ping":
                    result = {}
                else:
                    raise BridgeError("method_not_found", -32601)
                response = {"jsonrpc": "2.0", "id": request_id, "result": result}
            except (ValueError, KeyError, TypeError, BridgeError, OSError):
                # Never reflect transport/config errors, prompts, credentials, or raw logs.
                response = {"jsonrpc": "2.0", "id": request_id,
                            "error": {"code": -32600, "message": "Invalid bridge request"}}
            print(json.dumps(response), flush=True)
    finally:
        if server is not None:
            server.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
