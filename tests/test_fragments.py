import pytest

from proxyfleet.fragments import FragmentError, node_names


def test_line_fragment_names():
    text = "# comment\n\nA-AnyTLS = anytls, 192.0.2.1, 443, password=fake\nA-Snell-v6 = snell, 192.0.2.1, 8443, psk=fake\n"
    assert node_names("surge", text) == ["A-AnyTLS", "A-Snell-v6"]


def test_mihomo_fragment_names():
    text = "proxies:\n  - name: A-AnyTLS\n    type: anytls\n    password: fake\n  - {name: \"A-Reality\", type: vless}\n"
    assert node_names("mihomo", text) == ["A-AnyTLS", "A-Reality"]


def test_empty_mihomo_fragment():
    assert node_names("mihomo", "proxies:\n  []\n") == []


@pytest.mark.parametrize("client, text, message", [
    ("surge", "[Proxy]\nA = anytls\n", "line 1: section headers"),
    ("loon", "A-AnyTLS anytls 192.0.2.1\n", "line 1: expected 'Name = ...'"),
    ("surge", " = anytls\n", "line 1: expected"),
    ("surge", "A = x\nA = y\n", "duplicate node name 'A'"),
    ("mihomo", "proxies: [\n", "invalid YAML"),
    ("mihomo", "proxies: []\nrules: []\n", "only 'proxies'"),
    ("mihomo", "proxies: {}\n", "must be a list"),
    ("mihomo", "proxies:\n  - type: anytls\n", "proxies[0]: missing name"),
])
def test_bad_fragments(client, text, message):
    with pytest.raises(FragmentError) as info:
        node_names(client, text)
    assert message in str(info.value)


def test_unsafe_duplicate_names_are_not_echoed():
    with pytest.raises(FragmentError) as info:
        node_names("surge", "pass hunter2 = x\npass hunter2 = y\n")
    assert "hunter2" not in str(info.value)


def test_errors_never_quote_secrets():
    secret = "password=hunter2-very-secret"
    for client, text in [("surge", f"broken line {secret.replace('=', ':')}\n"), ("mihomo", f"proxies: [{secret}\n")]:
        with pytest.raises(FragmentError) as info:
            node_names(client, text)
        assert "hunter2" not in str(info.value)
