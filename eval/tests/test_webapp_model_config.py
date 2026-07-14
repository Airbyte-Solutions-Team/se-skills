"""Deterministic tests for ARCH-005 per-skill/per-use model configuration."""

import pytest

import webapp.app as app
from webapp.app import DEFAULT_CLAUDE_MODEL, _model_for, _se_config_clear


def test_model_for_defaults_when_no_config(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(app, "SE_CONFIG", tmp_path / "missing.yaml")
    _se_config_clear()
    assert _model_for("quick-ask") == DEFAULT_CLAUDE_MODEL
    assert _model_for("live-ask") == DEFAULT_CLAUDE_MODEL
    assert _model_for("deal-assessment") == DEFAULT_CLAUDE_MODEL
    assert _model_for("unknown-skill") == DEFAULT_CLAUDE_MODEL


def test_model_for_uses_use_specific_config(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / ".se-config.yaml"
    cfg.write_text("""
models:
  default: claude-sonnet-4-6
  quick-ask: claude-sonnet-4-5
  live-ask: claude-opus-4-6
  deal-assessment: claude-sonnet-4-5
""")
    monkeypatch.setattr(app, "SE_CONFIG", cfg)
    _se_config_clear()
    assert _model_for("quick-ask") == "claude-sonnet-4-5"
    assert _model_for("live-ask") == "claude-opus-4-6"
    assert _model_for("deal-assessment") == "claude-sonnet-4-5"


def test_model_for_falls_back_to_models_default(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / ".se-config.yaml"
    cfg.write_text("""
models:
  default: claude-haiku-4-5
""")
    monkeypatch.setattr(app, "SE_CONFIG", cfg)
    _se_config_clear()
    assert _model_for("quick-ask") == "claude-haiku-4-5"
    assert _model_for("roi-business-case") == "claude-haiku-4-5"


def test_model_for_falls_back_to_constant_if_models_missing(monkeypatch, tmp_path) -> None:
    cfg = tmp_path / ".se-config.yaml"
    cfg.write_text("name: Test\n")
    monkeypatch.setattr(app, "SE_CONFIG", cfg)
    _se_config_clear()
    assert _model_for("quick-ask") == DEFAULT_CLAUDE_MODEL
