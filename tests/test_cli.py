import json
from pathlib import Path

from proxyfleet.cli import main

EXAMPLE = str(Path(__file__).resolve().parents[1] / "examples" / "fleet.example.yaml")


def test_validate_ok(capsys):
    assert main(["validate", EXAMPLE]) == 0
    assert "ok: 4 machines (2 active, 1 observing, 1 retired)" in capsys.readouterr().out


def test_validate_reports_every_error(tmp_path, capsys):
    bad = tmp_path / "fleet.yaml"
    bad.write_text("version: 2\nmachines: []\n", encoding="utf-8")
    assert main(["validate", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "version: must be 1" in err and "machines: must be a non-empty list" in err


def test_validate_missing_file(tmp_path, capsys):
    assert main(["validate", str(tmp_path / "nope.yaml")]) == 1
    assert "inventory not found" in capsys.readouterr().err


def test_plan_json(capsys):
    assert main(["plan", EXAMPLE, "--json"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["contract"]["counts"] == {"surge": 4, "loon": 4, "mihomo": 4}
    assert plan["clients"]["surge"]["groups"]["🛰️ Fleet-AI"] == ["🇺🇸 US-WEST-AnyTLS", "🇺🇸 US-WEST-Snell-v6"]


def test_plan_canary_includes_observing(capsys):
    assert main(["plan", EXAMPLE, "--include-observing"]) == 0
    out = capsys.readouterr().out
    assert "active + observing (canary)" in out and "US-NEW-AnyTLS" in out


def test_usage_errors_exit_2(capsys):
    assert main([]) == 2
    assert main(["plan"]) == 2
