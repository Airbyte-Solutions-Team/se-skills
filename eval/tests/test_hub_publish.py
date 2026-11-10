"""Tests for `OutputService.publish_to_hub` — publishing outputs into one member's
folder of the internal.airbyte.ai repo via an auto-merging PR.

Uses real git against a scratch bare "origin" so the folder fence, staging, and
branch handling are exercised for real; only `gh` is stubbed.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from services.output_service import OutputError, OutputService

MEMBER = "gary-yang"
BASE = f"src/solutions-team/members/{MEMBER}"

HANDOVER = """<!DOCTYPE html>
<html lang="en">
<head>
<title>Handover Documents &mdash; Gary Yang</title>
<style>
  .nav-card { display: block; }
</style>
</head>
<body>
<div class="header">
  <div class="header-inner">
    <a href="./" style="x">&larr; Gary Yang Hub</a>
    <h1>Handover Documents</h1>
    <div class="subtitle">Pre-Sales to Post-Sales Transition Documents</div>
  </div>
</div>
<div class="main">
  <div class="nav-grid">
    <a href="accounts/existing-co/" class="nav-card">
      <div class="accent-bar"></div>
      <span class="arrow">&rarr;</span>
      <h2>Existing-Co</h2>
      <p>Coverage handoff for Existing-Co.</p>
      <div class="meta"><span class="badge">Active</span></div>
    </a>
  </div>
</div>
</body>
</html>
"""


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd, check=True, capture_output=True, text=True,
    ).stdout


@pytest.fixture
def env(tmp_path: Path):
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "--bare", "-b", "main", str(origin))
    _git(tmp_path, "init", "-b", "main", str(seed))
    member = seed / BASE
    (member / "accounts" / "existing-co").mkdir(parents=True)
    (member / "handover.html").write_text(HANDOVER, encoding="utf-8")
    (member / "index.html").write_text("<title>Gary Yang</title>", encoding="utf-8")
    (member / "accounts" / "existing-co" / "index.html").write_text(
        "<title>Existing-Co handoff</title>", encoding="utf-8")
    # A neighbouring member that must never be touched.
    other = seed / "src/solutions-team/members/someone-else"
    other.mkdir(parents=True)
    (other / "index.html").write_text("theirs", encoding="utf-8")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")
    _git(tmp_path, "clone", str(origin), str(clone))

    customers = tmp_path / "customers"
    gh_calls: list[list[str]] = []

    async def run_cmd(args, cwd, timeout=120):
        if args[0] == "gh":
            gh_calls.append(list(args))
            if args[1:3] == ["pr", "create"]:
                return 0, "https://github.com/x/y/pull/1\n", ""
            return 0, "", ""
        full = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args[1:]]
        p = subprocess.run(full, cwd=cwd, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or p.stdout.strip())
        return p.returncode, p.stdout, p.stderr

    svc = OutputService(
        customers_dir=customers, workspace=customers, repo_root=customers,
        se_config=lambda: {}, safe_name=lambda n: n,
        slug=lambda n: n.replace(" ", "-").lower(),
        run_cmd=run_cmd, internal_repo=lambda: clone,
    )
    return SimpleNamespace(svc=svc, clone=clone, origin=origin, customers=customers, gh=gh_calls)


def _out(env, rel: str, text: str) -> str:
    f = env.customers / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    return rel


def _body(path: str, account: str, member: str = "", **kw):
    return SimpleNamespace(path=path, account=account, member=member,
                           description=kw.get("description", ""), meta=kw.get("meta", ""))


def _origin_files(env, branch: str) -> list[str]:
    return _git(env.origin, "ls-tree", "-r", "--name-only", branch).splitlines()


def test_markdown_output_published_as_page_with_index(env) -> None:
    rel = _out(env, "Acme/outputs/deal-assessment/deal-assessment-2026-11-01.md",
               "# Acme — verdict\n\n## Driver\n\nThey need data.\n")
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme", member="Gary Yang")))

    assert res["status"] == "published" and res["auto_merge"] is True
    files = _origin_files(env, res["branch"])
    assert f"{BASE}/accounts/acme/deal-assessment.html" in files
    index = _git(env.origin, "show", f"{res['branch']}:{BASE}/accounts/index.html")
    # no Overview yet -> a typical card that opens the account's outputs page
    assert 'href="acme/outputs.html"' in index and 'badge">Outputs<' in index
    assert "Existing-Co" in index and 'href="../"' in index
    assert "Handover Documents" not in index
    assert 'href="deal-assessment.html"' in _show(env, "accounts/acme/outputs.html", res["branch"])
    # gh: PR created then auto-merge requested with squash
    assert env.gh[0][1:3] == ["pr", "create"]
    assert env.gh[1][1:3] == ["pr", "merge"] and "--auto" in env.gh[1] and "--squash" in env.gh[1]


def test_only_member_folder_changes(env) -> None:
    rel = _out(env, "Acme/outputs/next-move/next-move.md", "# Acme\n\n## Moves\n\n- one\n")
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    changed = _git(env.origin, "diff", "--name-only", "main", res["branch"]).splitlines()
    assert changed and all(p.startswith(BASE + "/") for p in changed)


def test_coverage_handoff_gets_index_page_and_handover_card(env) -> None:
    html = ('<html><title>Acme handoff</title><body><div class="stat"><div class="num">$55K</div>'
            '<div class="label">Deal Size</div></div></body></html>')
    rel = _out(env, "Acme/outputs/coverage-handoff/coverage-handoff-2026-11-01-Acme.html", html)
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))

    assert f"{BASE}/accounts/acme/index.html" in _origin_files(env, res["branch"])
    handover = _git(env.origin, "show", f"{res['branch']}:{BASE}/handover.html")
    assert 'href="accounts/acme/"' in handover and "$55K" in handover
    assert 'href="accounts/existing-co/"' in handover  # existing card kept


def test_other_member_rejected(env) -> None:
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    with pytest.raises(OutputError) as e:
        asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme", member="Someone Else")))
    assert e.value.status_code == 403
    assert env.gh == []


def test_republish_identical_content_is_noop(env) -> None:
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n\n## S\n\nx\n")
    first = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    # Pretend the first PR merged so origin/main has the files.
    _git(env.origin, "branch", "-f", "main", first["branch"])
    second = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert second["status"] == "unchanged"
    assert len([c for c in env.gh if c[1:3] == ["pr", "create"]]) == 1


def test_dirty_clone_refused_and_untouched(env) -> None:
    (env.clone / BASE / "scratch.txt").write_text("wip", encoding="utf-8")
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    with pytest.raises(OutputError) as e:
        asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert e.value.status_code == 409
    assert (env.clone / BASE / "scratch.txt").read_text() == "wip"


def test_clone_restored_to_clean_main_after_publish(env) -> None:
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert _git(env.clone, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert _git(env.clone, "status", "--porcelain").strip() == ""


def test_auto_merge_failure_still_returns_pr(env, monkeypatch) -> None:
    orig = env.svc._run_cmd

    async def flaky(args, cwd, timeout=120):
        if args[0] == "gh" and args[1:3] == ["pr", "merge"]:
            raise RuntimeError("auto-merge not allowed")
        return await orig(args, cwd, timeout)

    env.svc._run_cmd = flaky
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert res["pr_url"] and res["auto_merge"] is False and "auto-merge" in res["auto_merge_error"]


def test_hub_plan_lists_targets(env) -> None:
    rel = _out(env, "Acme/outputs/prep-call/p.md", "# A\n")
    plan = asyncio.run(env.svc.hub_plan(rel, "Acme", "Gary Yang"))
    assert plan["targets"] == [f"{BASE}/accounts/acme/prep-call.html",
                               f"{BASE}/accounts/acme/outputs.html", f"{BASE}/accounts/index.html"]


# --- strict fence: nothing outside members/gary-yang/ is ever written or pushed ----------------

def test_config_cannot_redirect_member(env) -> None:
    env.svc._se_config = lambda: {"internal_repo_member": "someone-else"}
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    changed = _git(env.origin, "diff", "--name-only", "main", res["branch"]).splitlines()
    assert changed and all(p.startswith(BASE + "/") for p in changed)
    assert not any("someone-else" in p for p in changed)


def test_stray_write_outside_member_folder_is_not_published(env) -> None:
    orig = env.svc._render_accounts_index

    def sneaky(member_dir, overrides=None):
        (member_dir.parent / "someone-else" / "index.html").write_text("TAMPERED", encoding="utf-8")
        (member_dir.parent / "someone-else" / "new.html").write_text("NEW", encoding="utf-8")
        return orig(member_dir, overrides)

    env.svc._render_accounts_index = sneaky
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    changed = _git(env.origin, "diff", "--name-only", "main", res["branch"]).splitlines()
    assert all(p.startswith(BASE + "/") for p in changed)
    assert _git(env.origin, "show", f"{res['branch']}:src/solutions-team/members/someone-else/index.html") == "theirs"
    # Tracked tampering is reset; we never `git clean` outside the member folder, so only the
    # injected untracked file remains — which makes the next publish refuse (409) rather than
    # silently sweep it into a commit.
    assert _git(env.clone, "status", "--porcelain").split() == ["??", "src/solutions-team/members/someone-else/new.html"]
    with pytest.raises(OutputError) as e:
        asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert e.value.status_code == 409


def test_final_fence_refuses_to_push_if_commit_leaves_member_folder(env) -> None:
    orig = env.svc._hub_git

    async def lying_git(args, repo):
        out = await orig(args, repo)
        if args[:4] == ["git", "diff", "--name-only", "origin/main"]:
            return out + "src/solutions-team/members/someone-else/index.html\n"
        return out

    env.svc._hub_git = lying_git
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    with pytest.raises(OutputError) as e:
        asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert "outside" in e.value.detail
    assert env.gh == []
    assert _git(env.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == ["main"]  # nothing pushed


def test_symlink_escape_is_refused(env, tmp_path) -> None:
    import os
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = env.clone / BASE / "accounts" / "acme"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable on this platform")
    _git(env.clone, "add", "-A")
    _git(env.clone, "commit", "-m", "symlink")
    _git(env.clone, "push", "origin", "main")
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    with pytest.raises(OutputError) as e:
        asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert "outside" in e.value.detail
    assert list(outside.iterdir()) == []


def test_old_unfenced_push_methods_are_gone(env) -> None:
    assert not hasattr(env.svc, "push_to_repo") and not hasattr(env.svc, "push_status")


def test_card_meta_is_plain_text_and_renders_without_literal_entities(env) -> None:
    svc = env.svc
    stats = {"deal size": "$55K", "stage": "4 &mdash; Proposal",
             "cv:coverage window": "July 17 &ndash; July 28, 2026"}
    meta = svc._build_card_meta(stats)
    assert meta == "$55K · Stage 4 — Proposal · Coverage July 17 – July 28, 2026"
    card = svc._upsert_handover_card(HANDOVER, "Acme", "acme", "", meta)
    assert "&amp;mdash;" not in card and "&amp;middot;" not in card and "&amp;ndash;" not in card


def test_card_meta_omits_unspecified_coverage_window(env) -> None:
    stats = {"deal size": "$55K", "stage": "2", "cv:coverage window": "? &ndash; ?"}
    assert env.svc._build_card_meta(stats) == "$55K · Stage 2"


# --- Overview = the account page; outputs live behind its "Opportunity outputs" tile ----------

OVERVIEW_DOC = ('<!doctype html><html><head><title>Acme Pro — Opportunity Overview</title></head>'
                '<body><div class="opp-workspace"><section class="workspace-hero"><h1>Acme Pro</h1>'
                '<div class="workspace-facts"><div class="workspace-fact"><span class="workspace-fact-value">2</span>'
                '<span class="workspace-fact-label">Stage</span></div></div></section>'
                '<section class="panel">Brief</section></div></body></html>')
HANDOFF_DOC = ('<html><title>Coverage Handoff — Acme</title><body><div class="stat"><div class="num">$55K</div>'
               '<div class="label">Deal Size</div></div></body></html>')


def _pub_overview(env, account="Acme", member="Gary Yang", doc=None):
    return asyncio.run(env.svc.publish_overview_to_hub(account, doc or OVERVIEW_DOC, member))


def _merge(env, res) -> None:
    """Pretend the PR merged so the next publish sees it on origin/main."""
    _git(env.origin, "branch", "-f", "main", res["branch"])


def _show(env, path: str, ref: str = "main") -> str:
    return _git(env.origin, "show", f"{ref}:{BASE}/{path}")


def _changed(env, res) -> list[str]:
    return _git(env.origin, "diff", "--name-only", "main", res["branch"]).splitlines()


def test_overview_becomes_account_index_with_outputs_tile(env) -> None:
    res = _pub_overview(env)
    assert res["status"] == "published" and res["auto_merge"] is True
    index = _show(env, "accounts/acme/index.html", res["branch"])
    assert env.svc.OVERVIEW_MARKER in index and "Acme Pro" in index
    # outputs are reached through the hero tile, not an inline list
    assert 'class="workspace-fact hub-tile" href="outputs.html"' in index and "0 &rarr;" in index
    assert "Published outputs" not in index
    assert 'href="../"' in index  # back to Accounts
    assert sorted(_changed(env, res)) == sorted(
        [f"{BASE}/accounts/acme/index.html", f"{BASE}/accounts/acme/outputs.html", f"{BASE}/accounts/index.html"])


def test_overview_lands_in_accounts_not_handover(env) -> None:
    res = _pub_overview(env)
    assert f"{BASE}/handover.html" not in _changed(env, res)
    assert "acme" not in _show(env, "handover.html", res["branch"])
    cards = _show(env, "accounts/index.html", res["branch"])
    assert 'href="acme/" class="nav-card"' in cards and 'badge">Overview<' in cards


def test_tile_opens_outputs_page_that_lists_published_outputs(env) -> None:
    _merge(env, _pub_overview(env))
    rel = _out(env, "Acme/outputs/prep-call/p.md", "# Prep\n\n## Q\n\nx\n")
    r2 = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    outputs = _show(env, "accounts/acme/outputs.html", r2["branch"])
    assert 'href="prep-call.html" class="nav-card"' in outputs
    assert '<a href="index.html"' in outputs and "&larr; Overview" in outputs   # back to the Overview
    index = _show(env, "accounts/acme/index.html", r2["branch"])
    assert "1 &rarr;" in index and index.count(env.svc._OUT_START) == 1          # live count, one region
    assert f"{BASE}/handover.html" not in _changed(env, r2)


def test_handoff_after_overview_goes_beside_it_and_card_links_to_it(env) -> None:
    _merge(env, _pub_overview(env))
    rel = _out(env, "Acme/outputs/coverage-handoff/h.html", HANDOFF_DOC)
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    assert f"{BASE}/accounts/acme/coverage-handoff.html" in _origin_files(env, res["branch"])
    assert env.svc.OVERVIEW_MARKER in _show(env, "accounts/acme/index.html", res["branch"])  # overview intact
    assert 'href="accounts/acme/coverage-handoff.html"' in _show(env, "handover.html", res["branch"])
    assert 'href="coverage-handoff.html" class="nav-card"' in _show(env, "accounts/acme/outputs.html", res["branch"])


def test_overview_moves_existing_handoff_aside_and_relinks_its_card(env) -> None:
    rel = _out(env, "Acme/outputs/coverage-handoff/h.html", HANDOFF_DOC)
    _merge(env, asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme"))))
    assert 'href="accounts/acme/"' in _show(env, "handover.html")  # legacy: handoff at index.html

    res = _pub_overview(env)
    assert "Deal Size" in _show(env, "accounts/acme/coverage-handoff.html", res["branch"])  # preserved
    assert env.svc.OVERVIEW_MARKER in _show(env, "accounts/acme/index.html", res["branch"])
    handover = _show(env, "handover.html", res["branch"])
    assert 'href="accounts/acme/coverage-handoff.html"' in handover and 'href="accounts/acme/"' not in handover
    assert handover.count("<h2>Acme</h2>") == 1  # card re-pointed, not duplicated
    assert 'href="coverage-handoff.html" class="nav-card"' in _show(env, "accounts/acme/outputs.html", res["branch"])
    assert "1 &rarr;" in _show(env, "accounts/acme/index.html", res["branch"])


def test_handoff_without_overview_still_uses_index_html_and_gets_an_outputs_card(env) -> None:
    rel = _out(env, "Acme/outputs/coverage-handoff/h.html", HANDOFF_DOC)
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    files = _origin_files(env, res["branch"])
    assert f"{BASE}/accounts/acme/index.html" in files
    assert f"{BASE}/accounts/acme/coverage-handoff.html" not in files
    assert 'href="index.html" class="nav-card"' in _show(env, "accounts/acme/outputs.html", res["branch"])
    assert 'href="acme/outputs.html"' in _show(env, "accounts/index.html", res["branch"])


def test_unrepublished_legacy_account_card_keeps_working(env) -> None:
    """existing-co has a page but no outputs.html yet: its card must still open its page."""
    rel = _out(env, "Acme/outputs/next-move/n.md", "# A\n")
    res = asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme")))
    cards = _show(env, "accounts/index.html", res["branch"])
    assert 'href="existing-co/" class="nav-card"' in cards and 'href="acme/outputs.html" class="nav-card"' in cards


def test_overview_publish_is_fenced_to_member_folder(env) -> None:
    with pytest.raises(OutputError) as e:
        _pub_overview(env, member="Someone Else")
    assert e.value.status_code == 403 and env.gh == []
    res = _pub_overview(env)
    assert _changed(env, res) and all(p.startswith(BASE + "/") for p in _changed(env, res))


def test_overview_republish_updates_in_place_keeping_tile_count(env) -> None:
    _merge(env, _pub_overview(env))
    rel = _out(env, "Acme/outputs/prep-call/p.md", "# Prep\n\n## Q\n\nx\n")
    _merge(env, asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme"))))
    res = _pub_overview(env, doc=OVERVIEW_DOC.replace("Brief", "Brief v2"))
    index = _show(env, "accounts/acme/index.html", res["branch"])
    assert "Brief v2" in index and "1 &rarr;" in index
    assert index.count(env.svc.OVERVIEW_MARKER) == 1 and index.count(env.svc._OUT_START) == 1


def test_malformed_overview_is_rejected_before_any_git_work(env) -> None:
    with pytest.raises(OutputError):
        _pub_overview(env, doc="<html><body><section>no facts grid</section></body></html>")
    assert env.gh == [] and _git(env.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads").split() == ["main"]


def test_overview_hub_plan_lists_move_when_handoff_holds_index(env) -> None:
    rel = _out(env, "Acme/outputs/coverage-handoff/h.html", HANDOFF_DOC)
    _merge(env, asyncio.run(env.svc.publish_to_hub(_body(rel, "Acme"))))
    plan = asyncio.run(env.svc.overview_hub_plan("Acme", "Gary Yang"))  # must sync the stale clone itself
    assert f"{BASE}/accounts/acme/coverage-handoff.html" in plan["targets"]
    assert f"{BASE}/handover.html" in plan["targets"] and f"{BASE}/accounts/acme/outputs.html" in plan["targets"]
    assert all(t.startswith(BASE + "/") for t in plan["targets"])


def test_plan_reflects_overview_that_only_exists_on_origin(env) -> None:
    """The clone's local main is stale after a PR merges; the plan must still see the Overview."""
    _merge(env, _pub_overview(env))
    rel = _out(env, "Acme/outputs/coverage-handoff/h.html", HANDOFF_DOC)
    plan = asyncio.run(env.svc.hub_plan(rel, "Acme", "Gary Yang"))
    assert plan["targets"][0].endswith("/accounts/acme/coverage-handoff.html")
