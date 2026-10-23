# proxyfleet

[![test](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml)
[![license](https://img.shields.io/github/license/godsonkg/proxyfleet)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

Manage a fleet of self-hosted proxy servers from one inventory file, and plan the Surge, Loon and Mihomo (Clash Meta) subscriptions that go with it.

> **Status: 0.1.0, early release.** `validate`, `plan`, `collect`, `build` and `diff` are ready to use. Publishing to an HTTPS host comes in 0.2; see [the design](docs/design.md) (Chinese) and the [changelog](CHANGELOG.md).

## Why

Once you run more than a few VPS hosts, the hard part is no longer installing nodes. It is keeping every client config in sync: which machines are live, which protocols each client supports, which nodes are allowed for AI or streaming, and making sure a retired server never sneaks back in.

proxyfleet keeps that in **one file**, `fleet.yaml`, and derives everything else from it:

- **Lifecycle states**: new machines start as `observing` (canary only), then become `active`, and later `disabled` or `retired`.
- **Per-client protocol matrix**: Surge, Loon and Mihomo support different protocols; each machine declares what each client gets.
- **Usage roles with explicit opt-in**: a node joins a role such as `ai` or `streaming` only if its machine allows it, never by guessing from names or regions. Roles can also be limited to certain protocols or countries.
- **Node contract**: expected node names, order and counts per client, bound to the SHA-256 of the inventory, so a batch built from a stale or edited inventory can be rejected.
- **Retirement guard**: names of retired machines and historical nodes can never be reused.

proxyfleet runs on your own computer (Windows, macOS or Linux). It never needs to run on the servers. It works well with [frm-node](https://github.com/godsonkg/frm-node), which manages the nodes on each server, but does not require it.

## Install

Requires Python 3.9+.

```bash
pipx install git+https://github.com/godsonkg/proxyfleet@v0.1.0
```

Or for development:

```bash
git clone https://github.com/godsonkg/proxyfleet
cd proxyfleet
python -m pip install -e ".[test]"
```

## Usage

Start from the example, and **keep your real inventory outside any public repository**:

```bash
cp examples/fleet.example.yaml ~/private/fleet.yaml
proxyfleet validate ~/private/fleet.yaml
proxyfleet plan ~/private/fleet.yaml
proxyfleet plan ~/private/fleet.yaml --include-observing   # canary build
proxyfleet plan ~/private/fleet.yaml --json
```

`validate` reports every problem in one pass: unknown keys, duplicate machine names or hosts, invalid country codes, roles a machine both allows and denies, observing machines that try to join restricted roles, colliding or retired node names, and more.

`plan` shows, per client, the node names in order, the groups they form, and the contract counts:

```text
[surge] 4 nodes
  🇺🇸 US-WEST-AnyTLS
  🇺🇸 US-WEST-Snell-v6
  🇯🇵 JP-TYO-AnyTLS
  🇯🇵 JP-TYO-Hysteria2
  group 🛰️ Fleet: all 4 nodes
  group 🛰️ Fleet-AI: 2
    🇺🇸 US-WEST-AnyTLS
    🇺🇸 US-WEST-Snell-v6
  ...
```

See [`examples/fleet.example.yaml`](examples/fleet.example.yaml) for every option, with comments.

### Managing machines

Change machines without hand-editing `fleet.yaml`. Comments and layout are kept, and only the affected lines change:

```bash
proxyfleet machine list    ~/private/fleet.yaml
proxyfleet machine add     ~/private/fleet.yaml SG-NEW --host 192.0.2.50 --country SG --discover --ssh-user root --allow general
proxyfleet machine promote ~/private/fleet.yaml SG-NEW --allow telegram
proxyfleet machine disable ~/private/fleet.yaml JP-TYO     # enable brings it back
proxyfleet machine retire  ~/private/fleet.yaml HK-OLD     # permanent; asks first
proxyfleet machine set-host ~/private/fleet.yaml JP-TYO 198.51.100.21
```

- `add` always creates an **observing** machine. `--discover` asks the server which protocols each client gets (`frm-node sub fragment` over SSH); without it, list them with `--protocol surge=AnyTLS,Snell-v6`.
- Each command prints what changes in the **published** node lists and groups. `--dry-run` shows that without writing anything.
- Each edit is validated as a whole inventory before it is written (atomically, keeping file permissions). An edit that would make the inventory invalid, such as a duplicate host, writes nothing.
- If your file's layout cannot be reproduced exactly, proxyfleet refuses to edit it rather than reformat it.

### Collecting node fragments

`collect` fetches each enabled machine's node fragments and checks that every fragment contains exactly the nodes the inventory expects, named `<machine>-<protocol>`:

```bash
# Over SSH, running `frm-node sub fragment` on each server (needs root there)
proxyfleet collect ~/private/fleet.yaml --out ~/private/collected --ssh-user root

# Or from fragments you already have: <DIR>/<machine>/surge.txt, loon.txt, mihomo.yaml
proxyfleet collect ~/private/fleet.yaml --out ~/private/collected --from ~/private/fragments
```

- Every fragment must pass, or **nothing is written** and the previous output stays as it was. All failures are listed together.
- The output directory and files are readable only by you (`0700`/`0600` on Unix, a current-user ACL on Windows).
- An existing `--out` directory is replaced only if it is empty or a previous proxyfleet output, so pointing it at the wrong folder cannot delete your files.
- SSH runs in batch mode with a 10-second connect timeout and up to 3 attempts per fragment. Use `--ssh-option` for extra ssh arguments, such as a port.
- Errors name the machine, the client and the node names at fault, never the fragment contents.

### Building subscription files

`build` turns a collection into files for each client:

```bash
proxyfleet build ~/private/fleet.yaml --fragments ~/private/collected --out ~/private/built
```

```text
built/
  providers/surge.txt     node list for Surge policy-path
  providers/loon.txt      node list for a Loon remote proxy subscription (not yet tested on a device)
  providers/mihomo.yaml   proxies: list for a Mihomo proxy-provider
  snippets/surge.conf     [Proxy] + [Proxy Group] to paste into a hand-written Surge profile
  snippets/loon.conf      the same for Loon
  snippets/mihomo.yaml    proxies: + proxy-groups: for Mihomo
  manifest.json           contract (node names, order, counts), inventory SHA-256, file hashes
```

- Nodes are renamed with your naming templates and ordered as in the inventory, whatever order the servers returned them in.
- The collection must come from the **same inventory file** (by SHA-256) and no fragment may have changed since it was collected; otherwise run `collect` again.
- Fragments containing a PEM private key or a GitHub token are rejected.
- Only the name of each node is changed. Every other line is kept byte for byte, and each Mihomo rename is verified by parsing the item before and after.
- Before replacing `--out`, the result is re-read and compared with `plan`. As with `collect`, a failed build leaves the previous output untouched.

### Comparing builds

Build into a new directory and compare it with the last one before you use it:

```bash
proxyfleet diff ~/private/built ~/private/built-new
```

```text
inventory: changed
[surge] 4 -> 5 nodes
  + 🇺🇸 US-NEW-AnyTLS
  ~ 🇯🇵 JP-TYO-AnyTLS: details changed (e.g. password, port or SNI; not shown)
  group 🛰️ Fleet-Telegram
    + 🇺🇸 US-NEW-AnyTLS
```

- Shows added and removed nodes, order changes, group membership changes, and nodes whose details changed. **Details themselves are never shown**, only the node names.
- A missing old directory is treated as a first build.
- Builds edited by hand after `build` are rejected (file hashes are checked against the manifest).
- Exit status is like `diff`: `0` no changes, `1` changes, `2` error. `--json` gives machine-readable output.

### Publishing to your own HTTPS host (unreleased, coming in 0.2)

`publish` uploads a build to a small static server you control, so clients can subscribe to it.

**Once, on a Debian/Ubuntu server** (it may be one of your VPS hosts):

```bash
git clone https://github.com/godsonkg/proxyfleet && cd proxyfleet
sudo PF_DOMAIN=sub.example.com PF_UPLOAD_PUBKEY="$(cat your-upload-key.pub)" bash server/setup.sh
```

This creates an **SFTP-only, chrooted** upload account (`proxyfleet`), a systemd timer that publishes uploaded batches, and a Caddy site that serves only `surge.txt`, `loon.txt` and `mihomo.yaml` under a random 64-character path. Everything else returns 404. Your existing Caddy sites are kept: proxyfleet adds its own file and imports it, and restores your Caddyfile if validation fails.

**On your computer**, copy [`examples/publish.example.yaml`](examples/publish.example.yaml) to `~/.config/proxyfleet/publish.yaml` (Windows: `%APPDATA%\proxyfleet\publish.yaml`), fill it in and `chmod 600` it. Then:

```bash
proxyfleet publish ~/private/built --dry-run   # checks only
proxyfleet publish ~/private/built
```

- Canary builds and builds with an empty client are refused, as are builds edited after `build`.
- The batch's `.ready` marker is uploaded last. The server then checks the SHA-256 manifest, the contract (clients, counts, inventory hash) and each file's structure before switching the live files with one atomic rename. A rejected batch never touches what clients see, and the last 5 releases are kept.
- `publish` then downloads every file over HTTPS until its SHA-256 matches, and fails if that does not happen within 90 seconds (`--verify-timeout`).
- The secret path never appears in any output.

### Rotating the secret path

If the subscription URL leaks (pasted into a chat, a screenshot, a log), replace it:

```bash
proxyfleet rotate-path            # asks for confirmation; --yes to skip
proxyfleet rotate-path --admin me # a user with passwordless sudo instead of root
```

1. The server serves the current files under **both** the old and the new path, and proxyfleet checks they are identical.
2. Only the `base_url` token in `publish.yaml` is replaced, keeping comments and permissions.
3. The server drops the old path. proxyfleet checks that the new path works and the **old one returns 404**.

If anything fails before step 3, the server is rolled back and `publish.yaml` restored, so the old URL keeps working. `proxyfleet rotate-path --rollback` undoes an interrupted run. A publish that lands mid-rotation is served under both paths, so nothing goes stale. Tokens travel over SSH stdin, never on a command line, and are never printed. Afterwards, give every client the new URL from `publish.yaml`.

## Roadmap

| Version | Scope |
|---|---|
| 0.1 | `validate`, `plan`, `collect`, `build`, `diff` (done) |
| 0.2 | `publish` to a static HTTPS host over SFTP with server-side verification and atomic switch, secret path rotation and lifecycle commands (`machine add`, `promote`, `disable`, `enable`, `retire`, `set-host`) (done, unreleased) |
| 0.3 | Release gates for retired references; append role groups into existing client configs without changing their defaults |

## Security

- The inventory holds addresses and roles only. **Never put passwords, UUIDs, keys or subscription URLs in it.**
- Generated subscription files will contain full node credentials. Keep them out of Git, cloud drives and chats.
- Inventory errors name the field and value at fault; the inventory holds no secrets. `collect`, `build` and `diff` name machines, clients and nodes in their messages, never fragment or file contents.

## License

[MIT](LICENSE)
