from __future__ import annotations

from webapp.hosted.smoke import run_live_smoke, run_offline_smoke


class _Environment:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def getenv(self, name: str) -> str:
        return self.values.get(name, "")


def test_offline_smoke_passes() -> None:
    report = run_offline_smoke()

    assert report.ok
    assert all(check.status == "ok" for check in report.checks)


def test_live_smoke_requires_explicit_gate() -> None:
    report = run_live_smoke(_Environment({}))

    assert not report.ok
    assert report.checks[0].check_id == "live_gate"


def test_live_smoke_refuses_ci_even_when_enabled() -> None:
    report = run_live_smoke(
        _Environment({"ALLOW_LIVE_HOSTED_SMOKE": "1", "CI": "1"})
    )

    assert not report.ok
    assert report.checks[0].check_id == "live_ci_gate"


def test_smoke_output_is_derived_only() -> None:
    report = run_offline_smoke()
    rendered = report.model_dump_json()

    assert "OfflineSmokeSecret" not in rendered
    assert "customer" not in rendered.lower()
