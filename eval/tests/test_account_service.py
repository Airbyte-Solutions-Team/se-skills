"""Deterministic tests for the account/member/opportunity service boundary."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from services.account_service import AccountError, AccountService
from services.job_service import JobService
from services.output_service import OutputService
from webapp.app import _safe, _slug, _titlecase_folder


@pytest.fixture
def svc(tmp_path: Path) -> AccountService:
    customers_dir = tmp_path / "customers"
    customers_dir.mkdir()
    webapp_dir = tmp_path / "webapp"
    webapp_dir.mkdir()
    output_svc = OutputService(
        customers_dir=customers_dir,
        workspace=tmp_path,
        repo_root=tmp_path,
        se_config=lambda: {},
        safe_name=_safe,
        slug=_slug,
        run_cmd=None,
        internal_repo=None,
    )
    job_svc = JobService(
        workspace=tmp_path,
        model_for=lambda x: "claude-sonnet-4-20250514",
        persist_run=None,
    )
    return AccountService(
        customers_dir=customers_dir,
        webapp_dir=webapp_dir,
        output_service=output_svc,
        job_service=job_svc,
        safe_name=_safe,
        titlecase=_titlecase_folder,
        slug=_slug,
        team_file=webapp_dir / "team-members.yaml",
        member_prefs_dir=webapp_dir / ".member-prefs",
        se_config_file=tmp_path / ".se-config.yaml",
        sfdc_opportunities=None,
    )


def _write_team(svc: AccountService, members: list[dict]) -> None:
    svc.team_file.write_text(yaml.safe_dump({"members": members}, sort_keys=False))


def _write_output(svc: AccountService, account: str, opp: str | None, skill: str, filename: str, content: str) -> Path:
    if opp:
        out_dir = svc.customers_dir / account / "opportunities" / opp / "outputs" / skill
    else:
        out_dir = svc.customers_dir / account / "outputs" / skill
    out_dir.mkdir(parents=True, exist_ok=True)
    f = out_dir / filename
    f.write_text(content)
    return f


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------
def test_load_team_from_yaml(svc: AccountService) -> None:
    _write_team(svc, [{"id": "alice", "name": "Alice", "email": "a@example.com"}])
    team = svc.load_team()
    assert team == [{"id": "alice", "name": "Alice", "email": "a@example.com"}]


def test_load_team_fallback_se_config(svc: AccountService) -> None:
    svc.se_config_file.write_text(yaml.safe_dump({"name": "Local SE", "email": "se@example.com"}))
    team = svc.load_team()
    assert team == [{"id": "me", "name": "Local SE", "email": "se@example.com"}]


def test_load_team_default_fallback(svc: AccountService) -> None:
    team = svc.load_team()
    assert team == [{"id": "me", "name": "Me", "email": ""}]


def test_create_member_generates_slug_id(svc: AccountService) -> None:
    member = svc.create_member("Ryan Waskewich")
    assert member["id"] == "ryan-waskewich"
    assert member["name"] == "Ryan Waskewich"


def test_create_member_dedupes_existing_ids(svc: AccountService) -> None:
    svc.create_member("Alice Smith")
    svc.create_member("Alice Smith")
    team = svc.load_team()
    ids = [m["id"] for m in team]
    assert "alice-smith" in ids
    assert "alice-smith-2" in ids


def test_member_by_id_found(svc: AccountService) -> None:
    svc.create_member("Bob")
    assert svc.member_by_id("bob")["name"] == "Bob"


def test_member_by_id_missing(svc: AccountService) -> None:
    assert svc.member_by_id("missing") is None


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------
def test_list_accounts_excludes_hidden_and_internal(svc: AccountService) -> None:
    (svc.customers_dir / "Acme").mkdir()
    (svc.customers_dir / "Build").mkdir()
    (svc.customers_dir / "_trash").mkdir()
    (svc.customers_dir / ".hidden").mkdir()
    accounts = svc.list_accounts()
    assert sorted(a["name"] for a in accounts) == ["Acme", "Build"]


def test_list_accounts_respects_owner_and_archived(svc: AccountService) -> None:
    (svc.customers_dir / "Acme").mkdir()
    (svc.customers_dir / "Acme" / ".owner").write_text("alice")
    (svc.customers_dir / "Acme" / ".archived").write_text("x")
    accounts = svc.list_accounts()
    assert accounts[0]["owner"] == "alice"
    assert accounts[0]["archived"] is True


def test_create_account_creates_folders_and_meta(svc: AccountService) -> None:
    r = svc.create_account("Acme Corp", owner="alice", sfdc_name="Acme, Inc.")
    assert r["created"] is True
    assert r["name"] == "Acme-Corp"
    acc_dir = svc.customers_dir / "Acme-Corp"
    assert (acc_dir / "outputs").is_dir()
    assert (acc_dir / "raw").is_dir()
    assert (acc_dir / ".owner").read_text() == "alice"
    assert (acc_dir / ".sfdc-name").read_text() == "Acme, Inc."


def test_create_account_existing_returns_created_false(svc: AccountService) -> None:
    svc.create_account("Acme Corp")
    r = svc.create_account("Acme Corp", owner="alice")
    assert r["created"] is False
    assert (svc.customers_dir / "Acme-Corp" / ".owner").read_text() == "alice"


def test_get_account(svc: AccountService) -> None:
    svc.create_account("Acme", owner="alice")
    assert svc.get_account("Acme") == {"name": "Acme", "owner": "alice"}


def test_get_account_missing(svc: AccountService) -> None:
    with pytest.raises(AccountError) as exc:
        svc.get_account("Missing")
    assert exc.value.status_code == 404


def test_member_accounts_owned_first(svc: AccountService) -> None:
    svc.create_account("Zebras")
    svc.create_account("Ants", owner="alice")
    _write_team(svc, [{"id": "alice", "name": "Alice"}])
    result = svc.member_accounts("alice")
    names = [a["name"] for a in result["active"]]
    assert names == ["Ants", "Zebras"]


def test_member_accounts_active_archived_split(svc: AccountService) -> None:
    svc.create_account("Active", owner="alice")
    svc.create_account("Archived", owner="alice")
    svc.archive("Archived")
    _write_team(svc, [{"id": "alice", "name": "Alice"}])
    result = svc.member_accounts("alice")
    assert [a["name"] for a in result["active"]] == ["Active"]
    assert [a["name"] for a in result["archived"]] == ["Archived"]


def test_member_accounts_sort_by_last_updated(svc: AccountService) -> None:
    svc.create_account("Old", owner="alice")
    svc.create_account("New", owner="alice")
    old = _write_output(svc, "Old", None, "intro", "o.md", "# old")
    new = _write_output(svc, "New", None, "intro", "n.md", "# new")
    # Force mtime differences deterministically.
    now = datetime.now(tz=timezone.utc).timestamp()
    os.utime(old, (now - 100, now - 100))
    os.utime(new, (now, now))
    _write_team(svc, [{"id": "alice", "name": "Alice"}])
    result = svc.member_accounts("alice")
    names = [a["name"] for a in result["active"]]
    assert names == ["New", "Old"]


def test_member_accounts_stable_sort_tie_breaker(svc: AccountService) -> None:
    svc.create_account("Beta", owner="alice")
    svc.create_account("Alpha", owner="alice")
    _write_team(svc, [{"id": "alice", "name": "Alice"}])
    result = svc.member_accounts("alice")
    names = [a["name"] for a in result["active"]]
    assert names == ["Alpha", "Beta"]


def test_archive_unarchive(svc: AccountService) -> None:
    svc.create_account("Acme")
    assert svc.archive("Acme")["archived"] is True
    assert (svc.customers_dir / "Acme" / ".archived").exists()
    assert svc.unarchive("Acme")["archived"] is False
    assert not (svc.customers_dir / "Acme" / ".archived").exists()


def test_set_owner(svc: AccountService) -> None:
    svc.create_account("Acme")
    assert svc.set_owner("Acme", "alice") == {"name": "Acme", "owner": "alice"}
    assert (svc.customers_dir / "Acme" / ".owner").read_text() == "alice"


def test_delete_account_moves_to_trash(svc: AccountService) -> None:
    svc.create_account("Acme")
    r = svc.delete_account("Acme")
    assert r["deleted"] is True
    assert "Acme__" in r["trash_id"]
    assert not (svc.customers_dir / "Acme").exists()
    assert (svc.customers_dir / "_trash" / r["trash_id"]).is_dir()


def test_list_trash_and_restore(svc: AccountService) -> None:
    svc.create_account("Acme")
    r = svc.delete_account("Acme")
    trash = svc.list_trash()
    assert any(t["trash_id"] == r["trash_id"] for t in trash)
    assert svc.restore_trash(r["trash_id"])["restored"] is True
    assert (svc.customers_dir / "Acme").is_dir()
    assert not (svc.customers_dir / "_trash" / r["trash_id"]).exists()


@pytest.mark.parametrize("action,expected", [
    ("archive", True),
    ("unarchive", False),
])
def test_bulk_archive_unarchive(svc: AccountService, action: str, expected: bool) -> None:
    svc.create_account("A")
    svc.create_account("B")
    r = svc.bulk_action(action, ["A", "B"])
    assert all(item["ok"] for item in r["results"])
    if action == "archive":
        assert (svc.customers_dir / "A" / ".archived").exists()
    else:
        assert not (svc.customers_dir / "A" / ".archived").exists()


def test_bulk_delete(svc: AccountService) -> None:
    svc.create_account("A")
    svc.create_account("B")
    r = svc.bulk_action("delete", ["A", "B"])
    assert all(item["ok"] for item in r["results"])
    assert not (svc.customers_dir / "A").exists()
    assert not (svc.customers_dir / "B").exists()


def test_bulk_set_owner(svc: AccountService) -> None:
    svc.create_account("A")
    svc.create_account("B")
    r = svc.bulk_action("set-owner", ["A", "B"], owner="alice")
    assert all(item["ok"] for item in r["results"])
    assert (svc.customers_dir / "A" / ".owner").read_text() == "alice"


def test_bulk_unknown_action_rejected(svc: AccountService) -> None:
    with pytest.raises(AccountError) as exc:
        svc.bulk_action("nope", ["A"])
    assert exc.value.status_code == 400


def test_bulk_set_owner_missing_owner_rejected(svc: AccountService) -> None:
    svc.create_account("A")
    with pytest.raises(AccountError) as exc:
        svc.bulk_action("set-owner", ["A"])
    assert exc.value.status_code == 400


def test_create_account_rejects_invalid_name(svc: AccountService) -> None:
    with pytest.raises(AccountError) as exc:
        svc.create_account("../escape")
    assert exc.value.status_code == 400


# ---------------------------------------------------------------------------
# Opportunities
# ---------------------------------------------------------------------------
def test_list_opportunities_uses_sfdc_callable(svc: AccountService) -> None:
    svc.create_account("Acme")
    _write_output(svc, "Acme", "intro", "biz-qual", "bq.md", "# bq")

    async def fake_sfdc(account: str) -> list[dict]:
        return [{"name": "Intro", "slug": "intro"}]

    svc._sfdc_opportunities = fake_sfdc
    opps = asyncio.run(svc.list_opportunities("Acme"))
    assert len(opps) == 1
    assert opps[0]["name"] == "Intro"
    assert opps[0]["output_count"] == 1


def test_list_opportunities_fallback_general(svc: AccountService) -> None:
    svc.create_account("Acme")
    opps = asyncio.run(svc.list_opportunities("Acme"))
    assert opps == [{
        "name": "General", "slug": "general", "stage": None, "stage_num": None,
        "amount": None, "close_date": None, "type": None, "is_closed": None, "ae": None,
        "output_count": 0,
    }]


def test_list_opportunities_sfdc_exception_fallback(svc: AccountService) -> None:
    svc.create_account("Acme")

    async def broken(account: str) -> list[dict]:
        raise RuntimeError("SFDC down")

    svc._sfdc_opportunities = broken
    opps = asyncio.run(svc.list_opportunities("Acme"))
    assert opps[0]["name"] == "General"


def test_list_opportunities_404_for_missing_account(svc: AccountService) -> None:
    with pytest.raises(AccountError) as exc:
        asyncio.run(svc.list_opportunities("Missing"))
    assert exc.value.status_code == 404


# ---------------------------------------------------------------------------
# Account metadata
# ---------------------------------------------------------------------------
def test_account_meta_counts_account_outputs(svc: AccountService) -> None:
    svc.create_account("Acme")
    _write_output(svc, "Acme", None, "intro", "i.md", "# intro")
    meta = svc._account_meta("Acme")
    assert meta["output_count"] == 1
    assert meta["last_updated"] is not None


def test_account_meta_counts_opportunity_outputs(svc: AccountService) -> None:
    svc.create_account("Acme")
    _write_output(svc, "Acme", "intro", "biz-qual", "bq.md", "# bq")
    meta = svc._account_meta("Acme")
    assert meta["output_count"] == 1


def test_account_meta_last_updated_none_when_empty(svc: AccountService) -> None:
    svc.create_account("Acme")
    meta = svc._account_meta("Acme")
    assert meta["output_count"] == 0
    assert meta["last_updated"] is None
    assert meta["last_updated_ts"] is None


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["../etc", "/tmp/outside", "Acme/Inside"])
def test_resolve_account_dir_rejects_unsafe(svc: AccountService, name: str) -> None:
    with pytest.raises(AccountError) as exc:
        svc._resolve_account_dir(name)
    assert exc.value.status_code == 400


def test_resolve_account_dir_rejects_sibling_prefix(svc: AccountService) -> None:
    # Create a sibling directory with the same prefix as customers_dir.
    sibling = svc.customers_dir.parent / (svc.customers_dir.name + "-archive")
    sibling.mkdir()
    # The path string "../<customers_dir>-archive" resolves outside the root.
    with pytest.raises(AccountError) as exc:
        svc._resolve_account_dir("../" + sibling.name)
    assert exc.value.status_code == 400


def test_resolve_account_dir_rejects_symlink_escape(svc: AccountService) -> None:
    outside = svc.customers_dir.parent / "outside"
    outside.mkdir()
    (svc.customers_dir / "Escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AccountError) as exc:
        svc._resolve_account_dir("Escape")
    assert exc.value.status_code == 400


def test_list_accounts_skips_symlinked_escape(svc: AccountService) -> None:
    outside = svc.customers_dir.parent / "outside"
    outside.mkdir()
    link = svc.customers_dir / "Escape"
    link.symlink_to(outside, target_is_directory=True)
    accounts = svc.list_accounts()
    assert not any(a["name"] == "Escape" for a in accounts)


def test_output_count_excludes_hidden_dirs(svc: AccountService) -> None:
    svc.create_account("Acme")
    skill_dir = svc.customers_dir / "Acme" / "outputs" / "intro"
    skill_dir.mkdir(parents=True)
    (skill_dir / ".hidden").mkdir()
    (skill_dir / ".hidden" / "x.md").write_text("# hidden")
    (skill_dir / "visible.md").write_text("# visible")
    assert svc._output_service.count_outputs("Acme") == 1


# ---------------------------------------------------------------------------
# Activity (job integration)
# ---------------------------------------------------------------------------
def test_account_activity_reads_through_job_service(svc: AccountService) -> None:
    svc.create_account("Acme")
    svc._job_service.jobs["j1"] = {
        "account": "Acme", "opp_slug": None, "status": "running",
        "stdout": "", "stderr": "", "sig": "x",
    }
    activity = svc.account_activity("Acme")
    assert activity["running"] == 1


def test_opportunity_activity_reads_through_job_service(svc: AccountService) -> None:
    svc.create_account("Acme")
    svc._job_service.jobs["j1"] = {
        "account": "Acme", "opp_slug": "intro", "status": "failed",
        "finished_at": 1234567890.0, "ok": False, "stderr": "err",
        "stdout": "", "sig": "x",
    }
    activity = svc.opportunity_activity("Acme", "intro")
    assert activity["running"] == 0
    assert activity["last_run"]["status"] == "failed"


def test_account_service_does_not_mutate_job_state(svc: AccountService) -> None:
    svc.create_account("Acme")
    svc._job_service.jobs["j1"] = {
        "account": "Acme", "status": "running", "stdout": "", "stderr": "", "sig": "x",
    }
    before = dict(svc._job_service.jobs["j1"])
    svc.account_activity("Acme")
    assert svc._job_service.jobs["j1"] == before


def test_account_service_does_not_import_app_as_dependency() -> None:
    repo_root = Path(__file__).parent.parent.parent
    code = (
        "import sys\n"
        "from services.account_service import AccountService\n"
        "assert 'webapp.app' not in sys.modules, 'account_service must not depend on webapp.app'\n"
        "print('ok')\n"
    )
    env = {**os.environ, "PYTHONPATH": f"{repo_root}:{repo_root / 'webapp'}"}
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        env=env,
    )
    assert result.returncode == 0, result.stderr


# ---------------------------------------------------------------------------
# Member preferences
# ---------------------------------------------------------------------------
def test_member_prefs_round_trip(svc: AccountService) -> None:
    svc.save_member_prefs("alice", {"selected_aes": ["AE One"]})
    assert svc.read_member_prefs("alice") == {"selected_aes": ["AE One"]}
