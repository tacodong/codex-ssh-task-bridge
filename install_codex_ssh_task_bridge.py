#!/usr/bin/env python3
"""Install only the temporary owner-local SSH task MCP entry, with a before-image."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import tomllib


def patch_config(source, python, bridge, codex, socket, upgrade=False):
    before = tomllib.loads(source)
    previous = before.get("mcp_servers", {}).get("ssh_task_bridge")
    if previous and not upgrade:
        raise ValueError("bridge_entry_already_exists")
    entry = {"command": str(python), "args": [str(bridge), "--codex", str(codex), "--socket", str(socket)],
             "startup_timeout_sec": 30, "tool_timeout_sec": 150, "default_tools_approval_mode": "approve"}

    def block(values):
        return "\n[mcp_servers.ssh_task_bridge]\n" + "\n".join(
            key + " = " + json.dumps(value) for key, value in values.items()) + "\n"

    if previous:
        expected_previous = dict(entry)
        expected_previous["args"] = [previous.get("args", [None])[0], *entry["args"][1:]]
        if "default_tools_approval_mode" not in previous:
            expected_previous.pop("default_tools_approval_mode")
        old_path = Path(expected_previous["args"][0] or "/invalid")
        if (previous != expected_previous or old_path.parent.parent != Path(bridge).parent.parent
                or old_path.name != Path(bridge).name or source.count(block(previous)) != 1):
            raise ValueError("modified_bridge_entry_refused")
        updated = source.replace(block(previous), block(entry), 1)
    else:
        updated = source + ("\n" if source and not source.endswith("\n") else "") + block(entry)
    after = tomllib.loads(updated)
    if after["mcp_servers"].pop("ssh_task_bridge") != entry:
        raise ValueError("bridge_entry_readback_mismatch")
    if previous:
        before["mcp_servers"].pop("ssh_task_bridge")
    if not before.get("mcp_servers"):
        after.pop("mcp_servers")
        expected = {key: value for key, value in before.items() if key != "mcp_servers"}
    else:
        expected = before
    if after != expected:
        raise ValueError("unrelated_config_change")
    return updated


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--install-root", type=Path, required=True)
    parser.add_argument("--receipt-directory", type=Path, required=True)
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--upgrade", action="store_true", help="Replace only an unchanged installer-owned bridge entry")
    args = parser.parse_args()
    for path in (args.config, args.install_root, args.receipt_directory, args.python, args.codex, args.socket):
        if not path.is_absolute():
            parser.error("host-local paths must be absolute")
    info = args.config.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_nlink != 1:
        raise ValueError("config_not_owned_regular_file")
    socket_info = args.socket.stat()
    if not stat.S_ISSOCK(socket_info.st_mode) or socket_info.st_uid != os.geteuid():
        raise ValueError("socket_not_owned_unix_socket")
    if not os.access(args.python, os.X_OK) or not os.access(args.codex, os.X_OK):
        raise ValueError("executable_unavailable")
    content = args.source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    release = args.install_root / digest
    bridge = release / "codex_ssh_task_bridge.py"
    source = args.config.read_text()
    updated = patch_config(source, args.python, bridge, args.codex, args.socket, upgrade=args.upgrade)
    if not args.apply:
        print(json.dumps({"status": "prepared", "sourceSha256": digest, "unrelatedConfigPreserved": True}))
        return
    args.receipt_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    before = args.receipt_directory / "config.before.toml"
    fd = os.open(before, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(source)
    release.mkdir(mode=0o700, parents=True, exist_ok=True)
    if bridge.exists():
        if bridge.is_symlink() or bridge.read_bytes() != content:
            raise ValueError("installed_source_mismatch")
    else:
        shutil.copyfile(args.source, bridge)
        bridge.chmod(0o600)
    fd, temporary = tempfile.mkstemp(dir=args.config.parent, prefix=".ssh-task-bridge-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, stat.S_IMODE(info.st_mode))
        current = args.config.lstat()
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_mode)
        if args.config.read_text() != source or identity(current) != identity(info):
            raise ValueError("config_changed_concurrently")
        os.replace(temporary, args.config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if args.config.read_text() != updated or bridge.read_bytes() != content:
        raise ValueError("installation_readback_mismatch")
    print(json.dumps({"status": "installed", "sourceSha256": digest, "unrelatedConfigPreserved": True}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError):
        # Configuration and OS exceptions can contain private paths or values.
        print(json.dumps({"error": "bridge_install_failed"}))
        raise SystemExit(1)
