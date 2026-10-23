import copy
from pathlib import Path

import pytest
import yaml

from proxyfleet.inventory import InventoryError, country_flag, load, parse
from proxyfleet.plan import build_plan, node_names, retired_node_names, role_groups

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "fleet.example.yaml"


@pytest.fixture
def data():
    return yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))


def errors_of(data):
    with pytest.raises(InventoryError) as info:
        parse(data)
    return "\n".join(info.value.errors)


def machine(data, name):
    return next(m for m in data["machines"] if m["name"] == name)


def test_example_is_valid():
    inventory = load(EXAMPLE)
    assert len(inventory.machines) == 4
    assert len(inventory.sha256) == 64


def test_country_flag():
    assert country_flag("US") == "\U0001F1FA\U0001F1F8"


def test_node_names_follow_inventory_and_protocol_order(data):
    inventory = parse(data)
    assert node_names(inventory, "surge") == [
        "🇺🇸 US-WEST-AnyTLS", "🇺🇸 US-WEST-Snell-v6", "🇯🇵 JP-TYO-AnyTLS", "🇯🇵 JP-TYO-Hysteria2"]
    assert node_names(inventory, "loon")[0] == "🛠️ US-WEST-AnyTLS"


def test_observing_only_in_canary(data):
    inventory = parse(data)
    assert "🇺🇸 US-NEW-AnyTLS" not in node_names(inventory, "surge")
    assert node_names(inventory, "surge", include_observing=True)[-1] == "🇺🇸 US-NEW-AnyTLS"


def test_observing_never_enters_role_groups(data):
    machine(data, "US-NEW")["roles"]["allow"] = ["general", "telegram"]
    inventory = parse(data)
    for members in role_groups(inventory, "surge").values():
        assert "🇺🇸 US-NEW-AnyTLS" not in members


def test_role_protocol_and_country_filters(data):
    inventory = parse(data)
    groups = role_groups(inventory, "mihomo")
    assert groups["🛰️ Fleet-AI"] == ["🇺🇸 US-WEST-AnyTLS", "🇺🇸 US-WEST-Reality"]
    # JP-TYO allows telegram with every protocol, including Hysteria2.
    assert groups["🛰️ Fleet-Telegram"] == ["🇯🇵 JP-TYO-AnyTLS", "🇯🇵 JP-TYO-Hysteria2"]
    # A country filter keeps a non-US machine out of AI even if it allows AI.
    jp = machine(data, "JP-TYO")
    jp["roles"] = {"allow": ["general", "ai"]}
    groups = role_groups(parse(data), "mihomo")
    assert all("JP-TYO" not in m for m in groups["🛰️ Fleet-AI"])


def test_deny_wins_over_allow_is_rejected(data):
    machine(data, "US-WEST")["roles"]["deny"] = ["ai"]
    assert "ai in both allow and deny" in errors_of(data)


def test_empty_role_groups_are_omitted(data):
    machine(data, "JP-TYO")["roles"]["allow"] = ["general"]
    groups = role_groups(parse(data), "surge")
    assert "🛰️ Fleet-Telegram" not in groups


def test_contract_counts(data):
    plan = build_plan(parse(data, "a" * 64))
    assert plan["contract"]["counts"] == {"surge": 4, "loon": 4, "mihomo": 4}
    assert plan["contract"]["inventory_sha256"] == "a" * 64
    canary = build_plan(parse(data), include_observing=True)
    assert canary["contract"]["counts"]["surge"] == 5


def test_retired_names_include_retired_machines(data):
    names = retired_node_names(parse(data))
    assert "HK-OLD" in names and "🇭🇰 HK-OLD-AnyTLS" in names


@pytest.mark.parametrize("mutate, message", [
    (lambda d: d.update(version=2), "version: must be 1"),
    (lambda d: d.update(colour="blue"), "unknown key 'colour'"),
    (lambda d: d.update(clients=["surge", "quantumult"]), "unsupported client 'quantumult'"),
    (lambda d: d["naming"].update(default="{flag} {name}"), "must contain {protocol}"),
    (lambda d: d["naming"].update(default="{name}-{protocol}-{ip}"), "unknown placeholder(s) ip"),
    (lambda d: d["groups"].update(role="Fleet"), "groups.role: must be a string containing {role}"),
    (lambda d: d["roles"].update(Bad={}), "role keys must match"),
    (lambda d: d["roles"]["google"].update(label="AI"), "duplicate group name(s)"),
    (lambda d: d["groups"].update(role="Fleet, {role}"), "contains a forbidden character"),
    (lambda d: d["roles"]["ai"].update(label="A=I"), "group name '🛰️ Fleet-A=I' contains a forbidden character"),
    (lambda d: d["lifecycle"]["observing"].update(deny_roles=["nope"]), "unknown role 'nope'"),
    (lambda d: d.update(machines=[]), "machines: must be a non-empty list"),
])
def test_top_level_errors(data, mutate, message):
    mutate(data)
    assert message in errors_of(data)


@pytest.mark.parametrize("mutate, message", [
    (lambda m: m.update(name="bad name"), "name: must match"),
    (lambda m: m.update(host="bad host!"), "host: must be an IP address or host name"),
    (lambda m: m.update(country="USA"), "ISO 3166-1 alpha-2"),
    (lambda m: m.update(state="paused"), "state: must be one of"),
    (lambda m: m["protocols"].pop("loon"), "protocols.loon: missing"),
    (lambda m: m["protocols"].update(surge=["AnyTLS", "anytls"]), "duplicate value 'anytls'"),
    (lambda m: m["protocols"].update(surge=["Any TLS"]), "invalid value 'Any TLS'"),
    (lambda m: m["protocols"].update(surge=[], loon=[], mihomo=[]), "enabled machine has no client protocols"),
    (lambda m: m["roles"].update(allow=["general", "gaming"]), "unknown role 'gaming'"),
    (lambda m: m.update(owner="me"), "unknown key 'owner'"),
])
def test_machine_errors(data, mutate, message):
    mutate(machine(data, "US-WEST"))
    assert message in errors_of(data)


def test_all_errors_are_reported_together(data):
    machine(data, "US-WEST")["country"] = "USA"
    machine(data, "JP-TYO")["state"] = "paused"
    errors = errors_of(data)
    assert "ISO 3166-1" in errors and "state: must be one of" in errors


def test_duplicate_machine_name_and_host(data):
    clone = copy.deepcopy(machine(data, "US-WEST"))
    data["machines"].append(clone)
    errors = errors_of(data)
    assert "duplicate machine name" in errors
    assert "duplicate host 203.0.113.10" in errors


def test_hosts_are_normalized_before_comparison(data):
    machine(data, "JP-TYO")["host"] = "2001:db8::1"
    machine(data, "US-NEW")["host"] = "2001:DB8:0::1"
    assert "duplicate host 2001:db8::1" in errors_of(data)


def test_observing_machine_cannot_allow_denied_roles(data):
    machine(data, "US-NEW")["roles"]["allow"] = ["general", "ai"]
    assert "observing machine cannot allow ai" in errors_of(data)


def test_generated_names_must_be_unique(data):
    # "A-B" + "C" and "A" + "B-C" both render as "A-B-C".
    data["naming"] = {"default": "{name}-{protocol}"}
    data["machines"] = [
        {"name": "A-B", "host": "192.0.2.1", "country": "US", "state": "active",
         "protocols": {"surge": ["C"], "loon": [], "mihomo": []}},
        {"name": "A", "host": "192.0.2.2", "country": "US", "state": "active",
         "protocols": {"surge": ["B-C"], "loon": [], "mihomo": []}},
    ]
    data["lifecycle"] = {}
    assert "surge: duplicate node name 'A-B-C'" in errors_of(data)


def test_forbidden_characters_in_generated_names(data):
    data["naming"]["default"] = "{name}={protocol}"
    assert "forbidden character" in errors_of(data)


def test_retired_names_cannot_be_reused(data):
    data["retired_names"] = ["🇺🇸 US-WEST-AnyTLS"]
    assert "is retired and must not be reused" in errors_of(data)


def test_duplicate_yaml_keys_are_rejected(tmp_path):
    path = tmp_path / "fleet.yaml"
    path.write_text("version: 1\nversion: 1\nmachines: []\n", encoding="utf-8")
    with pytest.raises(InventoryError) as info:
        load(path)
    assert "duplicate key: version" in info.value.errors[0]


def test_sha256_changes_with_file_content(tmp_path):
    text = EXAMPLE.read_text(encoding="utf-8")
    first, second = tmp_path / "a.yaml", tmp_path / "b.yaml"
    first.write_text(text, encoding="utf-8")
    second.write_text(text + "\n# edited\n", encoding="utf-8")
    assert load(first).sha256 != load(second).sha256
