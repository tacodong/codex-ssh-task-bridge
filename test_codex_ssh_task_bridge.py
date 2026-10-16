import json
from pathlib import Path
import subprocess
import sys
import socket
import tempfile
import unittest
from unittest.mock import patch

from codex_ssh_task_bridge import AppServer, BridgeError, TaskTools
from install_codex_ssh_task_bridge import patch_config


class FakeServer:
    def __init__(self, status="idle", fail=None):
        self.calls = []
        self.status = status
        self.fail = fail

    def request(self, method, params):
        self.calls.append((method, params))
        if method == self.fail:
            raise BridgeError("app_server_rpc_error", -32602)
        if method == "thread/read":
            return {"thread": {"id": "target", "status": {"type": self.status}}}
        if method in {"thread/start", "thread/fork", "thread/resume"}:
            return {"thread": {"id": "created"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-1", "status": "inProgress"}}
        return {}

    def wait(self, thread_id, turn_id, seconds):
        return {"status": "waiting"}


class TaskTests(unittest.TestCase):
    def test_create_inherits_settings(self):
        server = FakeServer()
        result = TaskTools(server).call("create_thread", {"params": {"cwd": "/sandbox"}, "prompt": "hello"})
        self.assertEqual(result["threadId"], "created")
        self.assertEqual(server.calls[0], ("thread/start", {"cwd": "/sandbox"}))
        self.assertNotIn("approvalPolicy", server.calls[1][1])

    def test_partial_failure_retains_created_id(self):
        result = TaskTools(FakeServer(fail="turn/start")).call("create_thread", {"prompt": "hello"})
        self.assertEqual(result["threadId"], "created")
        self.assertEqual(result["partialFailure"], "app_server_rpc_error")

    def test_busy_target_not_resumed_or_interrupted(self):
        server = FakeServer(status="active")
        with self.assertRaisesRegex(BridgeError, "target_not_confirmed_idle"):
            TaskTools(server).call("send_message_to_thread", {"thread_id": "target", "prompt": "hello"})
        self.assertEqual(len(server.calls), 1)

    def test_idle_target_resumed_before_send(self):
        server = FakeServer()
        TaskTools(server).call("send_message_to_thread", {"thread_id": "target", "prompt": "hello"})
        self.assertEqual([call[0] for call in server.calls], ["thread/read", "thread/resume", "turn/start"])

    def test_steer_requires_exact_turn(self):
        with self.assertRaisesRegex(BridgeError, "steer_requires"):
            TaskTools(FakeServer()).call("send_message_to_thread", {
                "thread_id": "target", "prompt": "hello", "mode": "steer"})

    def test_steer_does_not_resume(self):
        server = FakeServer()
        TaskTools(server).call("send_message_to_thread", {
            "thread_id": "target", "prompt": "hello", "mode": "steer", "expected_turn_id": "turn-1"})
        self.assertEqual([call[0] for call in server.calls], ["turn/steer"])

    def test_self_message_refused(self):
        with patch.dict("os.environ", {"CODEX_THREAD_ID": "target"}):
            with self.assertRaisesRegex(BridgeError, "self_message_refused"):
                TaskTools(FakeServer()).call("send_message_to_thread", {"thread_id": "target", "prompt": "hello"})

    def test_reserved_turn_parameters_refused(self):
        with self.assertRaisesRegex(BridgeError, "reserved_parameter"):
            TaskTools(FakeServer()).call("send_message_to_thread", {
                "thread_id": "target", "prompt": "hello", "params": {"threadId": "wrong"}})

    def test_unknown_tool_and_invalid_arguments(self):
        for name, args in [("handoff_thread", {}), ("read_thread", {}),
                           ("bridge_status", {"surprise": True}), ("read_thread", {"thread_id": 1})]:
            with self.assertRaises(BridgeError):
                TaskTools(FakeServer()).call(name, args)

    def test_wait_is_bounded(self):
        with self.assertRaisesRegex(BridgeError, "invalid_timeout"):
            TaskTools(FakeServer()).call("wait_thread", {
                "thread_id": "target", "turn_id": "turn-1", "timeout_seconds": 121})

    def test_fork_requires_id(self):
        with self.assertRaisesRegex(BridgeError, "missing_thread_id"):
            TaskTools(FakeServer()).call("fork_thread", {"params": {}})


class ConfigTests(unittest.TestCase):
    def test_config_values_and_text_preserved(self):
        source = '# keep comment\nmodel_provider = "existing"\n[mcp_servers.existing]\ncommand = "old"\n'
        result = patch_config(source, "/python", "/bridge", "/codex", "/socket")
        self.assertTrue(result.startswith(source))
        self.assertIn("[mcp_servers.ssh_task_bridge]", result)

    def test_config_without_servers_and_no_final_newline(self):
        result = patch_config('model = "existing"', "/python", "/bridge", "/codex", "/socket")
        self.assertIn('model = "existing"\n', result)

    def test_existing_bridge_refused(self):
        with self.assertRaisesRegex(ValueError, "already_exists"):
            patch_config('[mcp_servers.ssh_task_bridge]\ncommand="existing"', "/p", "/b", "/c", "/s")

    def test_paths_are_toml_escaped(self):
        patch_config("", '/python "quoted"', "/bridge", "/codex", "/socket")

    def test_upgrade_only_owned_entry(self):
        old = patch_config('model="existing"\n', "/python", "/releases/old/bridge.py", "/codex", "/socket")
        new = patch_config(old, "/python", "/releases/new/bridge.py", "/codex", "/socket", upgrade=True)
        self.assertIn("/releases/new/bridge.py", new)
        self.assertNotIn("/releases/old/bridge.py", new)
        with self.assertRaisesRegex(ValueError, "modified_bridge_entry"):
            patch_config(old.replace('command = "/python"', 'command = "/unexpected"'),
                         "/python", "/releases/new/bridge.py", "/codex", "/socket", upgrade=True)

    def test_real_installer_preserves_before_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.toml"
            config.write_text('model="existing"\n')
            config.chmod(0o600)
            source = Path(__file__).with_name("codex_ssh_task_bridge.py")
            installer = source.with_name("install_codex_ssh_task_bridge.py")
            # Keep the socket path under the platform's Unix path-length cap.
            socket_path = root / "s"
            stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            stream.bind(str(socket_path))
            try:
                result = subprocess.run([sys.executable, str(installer), "--source", str(source),
                    "--config", str(config), "--install-root", str(root / "releases"),
                    "--receipt-directory", str(root / "receipt"), "--python", sys.executable,
                    "--codex", sys.executable, "--socket", str(socket_path), "--apply"],
                    text=True, capture_output=True, check=True)
                self.assertEqual(json.loads(result.stdout)["status"], "installed")
                self.assertEqual((root / "receipt/config.before.toml").read_text(), 'model="existing"\n')
                self.assertEqual((root / "receipt/config.before.toml").stat().st_mode & 0o777, 0o600)
            finally:
                stream.close()


class TransportTests(unittest.TestCase):
    def test_proxy_handshake_response_and_completion(self):
        program = '''import sys,json
for line in sys.stdin:
 m=json.loads(line)
 if "id" not in m: continue
 print(json.dumps({"id":m["id"],"result":{}}),flush=True)
 if m["method"]=="turn/start":
  print(json.dumps({"method":"turn/completed","params":{"threadId":"t","turn":{"id":"u","status":"completed"}}}),flush=True)
'''
        server = AppServer([sys.executable, "-u", "-c", program], timeout=2)
        try:
            server.request("turn/start", {})
            self.assertEqual(server.wait("t", "u", 2)["status"], "completed")
        finally:
            server.close()

    def test_timeout_is_unknown_outcome_not_retried(self):
        program = '''import sys,json,time
for line in sys.stdin:
 m=json.loads(line)
 if m.get("method")=="initialize": print(json.dumps({"id":m["id"],"result":{}}),flush=True)
 else: time.sleep(10)
'''
        server = AppServer([sys.executable, "-u", "-c", program], timeout=0.1)
        try:
            with self.assertRaisesRegex(BridgeError, "timeout_outcome_unknown"):
                server.request("thread/start", {})
        finally:
            server.close()

    def test_mcp_catalog_does_not_start_proxy(self):
        source = Path(__file__).with_name("codex_ssh_task_bridge.py")
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        result = subprocess.run([sys.executable, str(source), "--codex", "/missing", "--socket", "/missing"],
                                input="\n".join(json.dumps(m) for m in messages) + "\n",
                                text=True, capture_output=True, check=True)
        lines = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(len(lines[1]["result"]["tools"]), 7)
        self.assertEqual(result.stderr, "")


if __name__ == "__main__":
    unittest.main()
