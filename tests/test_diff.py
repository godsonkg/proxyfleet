import json

import pytest
import yaml

from proxyfleet.build import build
from proxyfleet.cli import main
from proxyfleet.collect import DirSource, collect
from proxyfleet.diff import DiffError, compare, format_text, load_build
from proxyfleet.inventory import load

from helpers import EXAMPLE, write_tree


def make_build(tmp_path, name, inventory_path=EXAMPLE, edit=None):
    inventory = load(inventory_path)
    src = tmp_path / f"{name}-src"
    write_tree(src, inventory)
    if edit:
        edit(src)
    collect(inventory, DirSource(src), tmp_path / f"{name}-collected")
    build(inventory, tmp_path / f"{name}-collected", tmp_path / name)
    return tmp_path / name


def promoted_inventory(tmp_path):
    text = EXAMPLE.read_text(encoding="utf-8").replace("    state: observing\n", "    state: active\n")
    data = yaml.safe_load(text)
    next(m for m in data["machines"] if m["name"] == "US-NEW")["roles"]["allow"] = ["general", "telegram"]
    path = tmp_path / "fleet2.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def rotate_jp_password(src):
    path = src / "JP-TYO" / "mihomo.yaml"
    text = path.read_text(encoding="utf-8")
    path.write_text(text.replace("password: fake", "password: rotated-secret", 1), encoding="utf-8")


def test_identical_builds(tmp_path, capsys):
    built = make_build(tmp_path, "a")
    result = compare(load_build(built), load_build(built))
    assert result["changed"] is False
    assert format_text(result)[-1] == "no changes"
    assert main(["diff", str(built), str(built)]) == 0


def test_first_build_has_no_baseline(tmp_path, capsys):
    built = make_build(tmp_path, "a")
    assert main(["diff", str(tmp_path / "missing"), str(built)]) == 1
    out = capsys.readouterr().out
    assert out.startswith("no previous build")
    assert "[surge] 0 -> 4 nodes" in out and "  + 🇺🇸 US-WEST-AnyTLS" in out


def test_added_node_and_group_membership(tmp_path):
    old = load_build(make_build(tmp_path, "a"))
    new = load_build(make_build(tmp_path, "b", promoted_inventory(tmp_path)))
    result = compare(old, new)
    surge = result["clients"]["surge"]
    assert result["inventory_changed"] is True
    assert surge["count"] == [4, 5]
    assert surge["added"] == ["🇺🇸 US-NEW-AnyTLS"] and surge["removed"] == []
    assert surge["groups"]["🛰️ Fleet-Telegram"]["added"] == ["🇺🇸 US-NEW-AnyTLS"]
    assert "🛰️ Fleet-AI" not in surge["groups"]


def test_removed_group(tmp_path):
    def drop_jp_roles(path):
        data = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
        next(m for m in data["machines"] if m["name"] == "JP-TYO")["roles"]["allow"] = ["general"]
        path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        return path
    old = load_build(make_build(tmp_path, "a"))
    new = load_build(make_build(tmp_path, "b", drop_jp_roles(tmp_path / "fleet3.yaml")))
    groups = compare(old, new)["clients"]["mihomo"]["groups"]
    assert groups["🛰️ Fleet-Telegram"]["status"] == "removed"
    assert "  group removed 🛰️ Fleet-Telegram" in format_text(compare(old, new))


def test_details_change_is_named_but_not_shown(tmp_path, capsys):
    old = make_build(tmp_path, "a")
    new = make_build(tmp_path, "b", edit=rotate_jp_password)
    result = compare(load_build(old), load_build(new))
    assert result["clients"]["mihomo"]["details_changed"] == ["🇯🇵 JP-TYO-AnyTLS"]
    assert result["clients"]["surge"]["details_changed"] == []
    assert main(["diff", str(old), str(new)]) == 1
    assert main(["diff", str(old), str(new), "--json"]) == 1
    out = capsys.readouterr().out
    assert "~ 🇯🇵 JP-TYO-AnyTLS: details changed" in out
    assert "rotated-secret" not in out and "fake" not in out


def test_details_change_is_reported_alongside_new_nodes(tmp_path):
    old = load_build(make_build(tmp_path, "a"))
    new = load_build(make_build(tmp_path, "b", promoted_inventory(tmp_path), edit=rotate_jp_password))
    mihomo = compare(old, new)["clients"]["mihomo"]
    assert mihomo["added"] == ["🇺🇸 US-NEW-AnyTLS"]
    assert mihomo["details_changed"] == ["🇯🇵 JP-TYO-AnyTLS"]


def test_hand_edited_build_is_rejected(tmp_path):
    built = make_build(tmp_path, "a")
    path = built / "providers" / "surge.txt"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(DiffError) as info:
        load_build(built)
    assert "does not match its manifest" in str(info.value)


def test_not_a_build(tmp_path, capsys):
    built = make_build(tmp_path, "a")
    assert main(["diff", str(tmp_path / "a-collected"), str(built)]) == 2
    assert "is not a build output" in capsys.readouterr().err


def test_json_output(tmp_path, capsys):
    old = make_build(tmp_path, "a")
    new = make_build(tmp_path, "b", promoted_inventory(tmp_path))
    main(["diff", str(old), str(new), "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["changed"] is True
    assert data["clients"]["loon"]["added"] == ["🛠️ US-NEW-AnyTLS"]
