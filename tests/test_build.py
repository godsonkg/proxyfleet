import hashlib
import json
import sys

import pytest
import yaml

from proxyfleet.build import BuildError, build
from proxyfleet.cli import main
from proxyfleet.collect import DirSource, collect
from proxyfleet.fragments import node_names
from proxyfleet.inventory import load

from helpers import EXAMPLE, write_tree

POSIX = sys.platform != "win32"


@pytest.fixture
def inventory():
    return load(EXAMPLE)


@pytest.fixture
def collected(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory, reverse=True)
    collect(inventory, DirSource(tmp_path / "src"), tmp_path / "collected")
    return tmp_path / "collected"


def read(path):
    return path.read_text(encoding="utf-8")


def test_build_renames_and_orders_nodes(tmp_path, inventory, collected):
    out = tmp_path / "built"
    build(inventory, collected, out)
    # Fragments were written in reverse protocol order; output follows the inventory.
    assert node_names("surge", read(out / "providers" / "surge.txt")) == [
        "🇺🇸 US-WEST-AnyTLS", "🇺🇸 US-WEST-Snell-v6", "🇯🇵 JP-TYO-AnyTLS", "🇯🇵 JP-TYO-Hysteria2"]
    assert node_names("loon", read(out / "providers" / "loon.txt"))[1] == "🛠️ US-WEST-Reality"
    mihomo = yaml.safe_load(read(out / "providers" / "mihomo.yaml"))
    assert [p["name"] for p in mihomo["proxies"]][:2] == ["🇺🇸 US-WEST-AnyTLS", "🇺🇸 US-WEST-Reality"]
    assert mihomo["proxies"][0]["password"] == "fake"


def test_snippets_contain_groups(tmp_path, inventory, collected):
    out = tmp_path / "built"
    build(inventory, collected, out)
    surge = read(out / "snippets" / "surge.conf")
    assert surge.startswith("[Proxy]\n")
    assert "🛰️ Fleet-AI = select, 🇺🇸 US-WEST-AnyTLS, 🇺🇸 US-WEST-Snell-v6\n" in surge
    assert "🛰️ Fleet-AI = select,🛠️ US-WEST-AnyTLS,🛠️ US-WEST-Reality\n" in read(out / "snippets" / "loon.conf")
    groups = yaml.safe_load(read(out / "snippets" / "mihomo.yaml"))["proxy-groups"]
    assert {g["name"]: g["proxies"] for g in groups}["🛰️ Fleet-Telegram"] == [
        "🇯🇵 JP-TYO-AnyTLS", "🇯🇵 JP-TYO-Hysteria2"]


def test_manifest_records_contract_and_hashes(tmp_path, inventory, collected):
    out = tmp_path / "built"
    manifest = build(inventory, collected, out)
    assert manifest["kind"] == "build"
    assert manifest["contract"]["counts"] == {"surge": 4, "loon": 4, "mihomo": 4}
    assert manifest["contract"]["inventory_sha256"] == inventory.sha256
    for relative, digest in manifest["files"].items():
        assert hashlib.sha256((out / relative).read_bytes()).hexdigest() == digest
    assert json.loads(read(out / "manifest.json")) == manifest


@pytest.mark.skipif(not POSIX, reason="POSIX permission bits")
def test_build_output_is_private(tmp_path, inventory, collected):
    out = tmp_path / "built"
    build(inventory, collected, out)
    for path in (out, out / "providers", out / "snippets"):
        assert (path.stat().st_mode & 0o777) == 0o700
    assert ((out / "providers" / "surge.txt").stat().st_mode & 0o777) == 0o600


def test_rejects_collection_from_another_inventory(tmp_path, collected):
    edited = tmp_path / "fleet.yaml"
    edited.write_text(read(EXAMPLE) + "\n# edited\n", encoding="utf-8")
    with pytest.raises(BuildError) as info:
        build(load(edited), collected, tmp_path / "built")
    assert "inventory changed since this collection" in str(info.value)


def test_rejects_observing_mismatch(tmp_path, inventory, collected):
    with pytest.raises(BuildError) as info:
        build(inventory, collected, tmp_path / "built", include_observing=True)
    assert "not made with --include-observing" in str(info.value)


def test_rejects_tampered_fragment_and_keeps_previous_output(tmp_path, inventory, collected):
    out = tmp_path / "built"
    first = build(inventory, collected, out)
    path = collected / "JP-TYO" / "surge.txt"
    path.write_text(read(path).replace("443", "444"), encoding="utf-8")
    with pytest.raises(BuildError) as info:
        build(inventory, collected, out)
    assert "JP-TYO surge: fragment changed since collection" in str(info.value)
    assert json.loads(read(out / "manifest.json")) == first


def test_rejects_private_key_material(tmp_path, inventory):
    write_tree(tmp_path / "src", inventory)
    path = tmp_path / "src" / "US-WEST" / "loon.txt"
    path.write_text(read(path) + "# -----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="utf-8")
    collect(inventory, DirSource(tmp_path / "src"), tmp_path / "collected")
    with pytest.raises(BuildError) as info:
        build(inventory, tmp_path / "collected", tmp_path / "built")
    assert "US-WEST loon: contains a PEM private key" in str(info.value)
    assert "OPENSSH" not in str(info.value)


def test_not_a_collection(tmp_path, inventory):
    (tmp_path / "empty").mkdir()
    with pytest.raises(BuildError) as info:
        build(inventory, tmp_path / "empty", tmp_path / "built")
    assert "is not a collect output" in str(info.value)


MIHOMO_TRICKY = """proxies:
  - name: "US-WEST-AnyTLS"
    type: anytls
    server: 203.0.113.10
    port: 443
    password: '01234567'
    short-id: 0123
    ws-opts:
      headers:
        name: keep-me
  - name: US-WEST-Reality
    type: vless
    server: 203.0.113.10
    port: 2443
    uuid: 11111111-1111-4111-8111-111111111111
"""


def tricky_collection(tmp_path, inventory, mihomo_text):
    write_tree(tmp_path / "src", inventory)
    (tmp_path / "src" / "US-WEST" / "mihomo.yaml").write_text(mihomo_text, encoding="utf-8")
    collect(inventory, DirSource(tmp_path / "src"), tmp_path / "collected")
    return tmp_path / "collected"


def test_mihomo_values_are_kept_byte_for_byte(tmp_path, inventory):
    collected = tricky_collection(tmp_path, inventory, MIHOMO_TRICKY)
    build(inventory, collected, tmp_path / "built")
    text = read(tmp_path / "built" / "providers" / "mihomo.yaml")
    assert "    short-id: 0123\n" in text
    assert "    password: '01234567'\n" in text
    assert "        name: keep-me\n" in text
    assert '  - name: "🇺🇸 US-WEST-AnyTLS"\n' in text


def test_mihomo_flow_style_is_rejected(tmp_path, inventory):
    flow = "proxies:\n  - {name: US-WEST-AnyTLS, type: anytls}\n  - {name: US-WEST-Reality, type: vless}\n"
    collected = tricky_collection(tmp_path, inventory, flow)
    with pytest.raises(BuildError) as info:
        build(inventory, collected, tmp_path / "built")
    assert "US-WEST mihomo: unsupported Mihomo layout" in str(info.value)


def test_cli_build(tmp_path, capsys, collected):
    out = tmp_path / "built"
    assert main(["build", str(EXAMPLE), "--fragments", str(collected), "--out", str(out)]) == 0
    captured = capsys.readouterr()
    assert "surge 4, loon 4, mihomo 4 nodes" in captured.out
    assert "providers/surge.txt" in captured.out
    assert "contain node credentials" in captured.err


def test_cli_build_failure(tmp_path, capsys):
    (tmp_path / "empty").mkdir()
    assert main(["build", str(EXAMPLE), "--fragments", str(tmp_path / "empty"), "--out", str(tmp_path / "b")]) == 1
    assert "was not changed" in capsys.readouterr().err
