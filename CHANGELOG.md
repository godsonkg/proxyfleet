# Changelog

## Unreleased

- `publish`: upload a build to your own HTTPS server over SFTP. The server verifies the batch and switches atomically, and the client confirms each file over HTTPS. Server scripts: `server/setup.sh` and `server/proxyfleet-publish.sh`.
- `rotate-path`: replace the secret subscription path. The old URL keeps working until the new one is confirmed. Checked end to end, rolled back on failure, and tokens are never printed or put on a command line. Server helper: `server/proxyfleet-rotate.sh`.

## 0.1.0

First release. proxyfleet keeps a fleet of self-hosted proxy servers in one inventory file and builds the Surge, Loon and Mihomo node lists that go with it.

### Commands

- `validate`: checks the inventory and reports every problem in one pass.
- `plan`: shows node names, groups and counts per client, from the inventory alone. `--include-observing` gives the canary view.
- `collect`: fetches node fragments over SSH (`frm-node sub fragment`) or from a local directory. Each fragment must contain exactly the expected nodes.
- `build`: renames nodes with your templates, orders them as in the inventory, and writes `providers/` (node lists), `snippets/` (nodes plus groups) and a manifest with the node contract.
- `diff`: compares two builds. It shows nodes added and removed, order and group changes, and the names of nodes whose details changed. Details are never shown.

### Safety

- Output directories are private: `0700`/`0600`, or a current-user ACL on Windows. They are replaced in one step, only if the whole run succeeds. An unrelated non-empty directory is never replaced.
- Messages never echo fragment contents, and malformed "names" that might be credentials are counted rather than shown.
- `build` refuses collections from a different inventory, fragments changed since collection, and fragments containing a PEM private key or a GitHub token.
- Only node names are rewritten. Mihomo items are kept byte for byte apart from their `name:` line, and each rename is verified by parsing.

### Known limits

- Loon node lists have not yet been tested by importing them on a device.
- Generated groups are always `select`.
- Publishing to an HTTPS host, secret-path rotation and lifecycle commands are planned for 0.2.
