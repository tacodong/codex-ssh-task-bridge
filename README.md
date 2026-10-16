# Codex SSH Task Bridge

Temporary task-management tools for Codex chats on an SSH host. The bridge exposes
an existing same-user Codex app-server as a local stdio MCP server. It does not
launch an app-server, open a network port, copy credentials, patch Codex, or change
your provider, account or model.

This is an independent workaround, not an official OpenAI product. Check whether
a fresh SSH chat already has working native create/message/fork tools before
installing it. Retire this bridge when the native path works for your use case.

## Tools

- `bridge_status`: check the existing app-server connection.
- `create_thread`: create a host-local chat and optionally start a prompt.
- `fork_thread`: fork a host-local chat and optionally start a prompt.
- `send_message_to_thread`: start a turn on an idle chat, or steer an exact active turn.
- `list_threads` / `read_thread`: inspect host-local chats.
- `wait_thread`: wait for a turn started by this bridge connection.

These are app-server thread operations. Cross-host handoff, Git-state transfer,
Desktop project/worktree creation and native subagent management are not implemented.
Chats created directly through app-server may need separate Desktop discovery.

## Requirements

- macOS or Linux, Python 3.11+, and `websocket-client==1.9.2`.
- A working Codex CLI, an existing same-user app-server Unix WebSocket, and a
  regular same-user Codex `config.toml` file.
- Working inference through your existing host configuration.

The app-server API is experimental; newer Codex versions can require changes.
See [OpenAI's app-server documentation](https://developers.openai.com/codex/app-server).
A Unix socket needs a WebSocket handshake: piping newline-delimited JSON through
`codex app-server proxy` alone does not provide it.

## Install on the SSH host

Run these commands on the machine where the SSH chat executes. Keep runtime
files outside the source checkout. The following uses conventional Codex paths;
adjust `CODEX_CONFIG`, `CONTROL_SOCKET` and `HOST_CODEX` for your installation.

```bash
git clone https://github.com/tacodong/codex-ssh-task-bridge.git
cd codex-ssh-task-bridge

BRIDGE_HOME="$HOME/.local/share/codex-ssh-task-bridge"
python3 -m venv "$BRIDGE_HOME/venv"
"$BRIDGE_HOME/venv/bin/python" -m pip install -r requirements.txt

CODEX_CONFIG="$HOME/.codex/config.toml"
CONTROL_SOCKET="$HOME/.codex/app-server-control/app-server-control.sock"
HOST_CODEX="$(command -v codex)"
BRIDGE_RECEIPT="$BRIDGE_HOME/receipts/$(date -u +%Y%m%dT%H%M%SZ)"

codex --version
codex app-server daemon version

"$BRIDGE_HOME/venv/bin/python" install_codex_ssh_task_bridge.py \
  --source "$PWD/codex_ssh_task_bridge.py" \
  --config "$CODEX_CONFIG" --socket "$CONTROL_SOCKET" \
  --python "$BRIDGE_HOME/venv/bin/python" --codex "$HOST_CODEX" \
  --install-root "$BRIDGE_HOME/releases" --receipt-directory "$BRIDGE_RECEIPT"
```

The last command prepares and validates without writing. Repeat it with `--apply`
to install. It installs immutable hash-named source, saves a protected config
before-image, preserves unrelated parsed configuration, and atomically replaces
only the configuration needed for this bridge. It refuses an existing entry by
default. Use `--upgrade` with a new receipt directory only for an unchanged
installer-owned entry; manually modified entries fail closed.

Installing explicitly trusts this bridge through its MCP entry's
`default_tools_approval_mode = "approve"`. That grants same-user chat-control
capability. Other servers, shell approval policy and sandbox settings remain
unchanged. Child chats inherit the host's native settings unless you explicitly
supply overrides. The bridge declines interactive app-server approval requests;
it does not impersonate an approval UI.

The before-image can contain private configuration. Keep it owner-only, outside
Git, and do not attach it to issues. No account login or credential transfer is
part of this installation. If the socket is missing, restore the normal supported
Codex connection first; this installer deliberately does not bootstrap a server.
A `not managed` restart response is not permission to kill an unknown server.

## Verify through Desktop SSH

Open a **fresh chat through the Desktop SSH connection** after installation.
Existing chats may have an older tool catalog. Check for `mcp__ssh_task_bridge`
tools, then use this bounded prompt:

> Test the installed ssh_task_bridge. First call bridge_status. Create one chat
> titled SSH bridge test with a prompt to reply exactly BRIDGE-CREATE-OK. Wait
> for completion and read back its stored reply. Send only that idle test chat
> a follow-up asking it to reply exactly BRIDGE-MESSAGE-OK, then wait and read it.
> Fork only that test chat with a prompt to reply exactly BRIDGE-FORK-OK, then
> wait and read the fork. You may create and message only these test chats.
> Report the exact tool names, results, child IDs, and actual execution route.
> Also list any native task-management tools exposed across all namespaces.
> Do not change config, credentials, provider, model, files, or other chats.

Confirm all three replies, persistence, and whether Desktop can discover the
created chats. A shell probe or socket health check alone does not verify this
Desktop path. Keep test transcripts and host details local.

On an unknown-outcome write timeout, use list/read to reconcile the existing
operation. Do not repeat the write: it may already have succeeded. Start mode
refuses busy targets; steer mode requires the exact active turn ID. Message
another chat only with its user's authorization. Never target the calling chat.

## Tests

```bash
python3 -m unittest discover -p test_codex_ssh_task_bridge.py -v
```

The test suite uses local fakes and temporary files; no credentials or live
inference are needed. Temporary files should be directed to your normal private
task-data directory when your workspace requires it.

## Compatibility and acceptance

The original September 30, 2026 pilot passed 20 automated tests and a model-visible
create/message/fork sequence on macOS. Linux ARM64 passed the automated suite and
socket probe; a separate provider-authentication error prevented its inference
acceptance. The original Desktop SSH frontend/discovery test remained incomplete.

On October 15, the existing installation's read-only status probe also succeeded
with CLI 0.163.0 on macOS.

On October 16, a fresh Desktop SSH chat on macOS successfully called the installed
bridge to create a test chat, send an idle follow-up, and fork it. Each completed
reply was independently read back through Desktop's native remote thread tools;
the fork retained both earlier replies. This closes the create/message/fork
acceptance gap for that SSH path. A native collaboration subagent also completed
its marker test; subagent creation is a separate native capability, not a bridge
tool. Visual sidebar appearance and cross-host handoff were not tested.

These dated results are not a guarantee for every Codex version or host.

## Remove

```bash
codex mcp remove ssh_task_bridge
```

Open a fresh SSH chat and confirm the bridge namespace is absent. This removes
the MCP entry; immutable releases, the venv and private before-images remain.
Keep them until recovery is no longer needed, then remove only your bridge-owned
files. Do not restore an entire old config over concurrent settings, delete
sessions, stop the shared server or change remote-control ownership.

## Development

Runtime code, installer and tests live in this repository. Changes should preserve
native parameter support, configuration fidelity, busy-target checks, approval
boundaries and unknown-outcome handling. Run the suite above before submitting a
change. Never include credentials, config backups, real prompts, private paths,
or session data in a commit or issue.

## License

MIT. See [LICENSE](LICENSE).
