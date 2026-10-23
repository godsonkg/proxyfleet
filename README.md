# proxyfleet

[![test](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/godsonkg/proxyfleet/actions/workflows/test.yml)
[![license](https://img.shields.io/github/license/godsonkg/proxyfleet)](LICENSE)

**English** | [简体中文](README.zh-CN.md)

Manage a fleet of self-hosted proxy servers from one inventory file, and plan the Surge, Loon and Mihomo (Clash Meta) subscriptions that go with it.

> **Status: early development (0.1.0.dev0).** `validate`, `plan` and `collect` work today. Building subscription files and publishing are next; see [the design](docs/design.md) (Chinese).

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
pipx install git+https://github.com/godsonkg/proxyfleet
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

## Roadmap

| Version | Scope |
|---|---|
| 0.1 | `validate`, `plan`, `collect` (done); `build` Surge/Loon/Mihomo provider files, role groups and the contract; `diff` against the last build (names and counts only) |
| 0.2 | `publish` to a static HTTPS host over SFTP with server-side verification and atomic switch; secret path rotation; lifecycle commands (`add`, `promote`, `retire`) |
| 0.3 | Release gates for retired references; append role groups into existing client configs without changing their defaults |

## Security

- The inventory holds addresses and roles only. **Never put passwords, UUIDs, keys or subscription URLs in it.**
- Generated subscription files will contain full node credentials. Keep them out of Git, cloud drives and chats.
- Inventory errors name the field and value at fault; the inventory holds no secrets. Later stages that handle generated subscriptions will report fixed error codes and never echo their contents.

## License

[MIT](LICENSE)
