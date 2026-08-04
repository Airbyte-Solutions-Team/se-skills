"""
SE Suite configuration loader for the worker-analysis toolkit.

Reads the optional `.se-config.yaml` in the current working directory (or the
standard SE workspace fallback) and returns the `worker_analysis` block. Values
can be overridden with `WORKER_ANALYSIS_<KEY>` environment variables.
"""

import os
from pathlib import Path
from typing import Any, Dict


def _candidate_paths() -> list[Path]:
    cwd = Path(os.getcwd())
    return [
        cwd / ".se-config.yaml",
        Path.home() / ".se-skills" / ".se-config.yaml",
        Path.home() / "airbyte-work" / ".se-config.yaml",
    ]


def _load_raw() -> Dict[str, Any]:
    for candidate in _candidate_paths():
        if candidate.exists():
            try:
                import yaml

                cfg = yaml.safe_load(candidate.read_text()) or {}
                return cfg.get("worker_analysis", {}) if isinstance(cfg, dict) else {}
            except Exception:
                pass
    return {}


def worker_analysis_config() -> Dict[str, Any]:
    """Return the worker_analysis block from .se-config.yaml (cached per process)."""
    cache = getattr(worker_analysis_config, "_cache", None)
    if cache is not None:
        return cache
    cfg = _load_raw()
    worker_analysis_config._cache = cfg
    return cfg


def clear_cache() -> None:
    worker_analysis_config._cache = None


def get_config_value(key: str, default: Any = None) -> Any:
    """Return a config value, preferring .se-config.yaml then an env override."""
    env_key = f"WORKER_ANALYSIS_{key.upper()}"
    return worker_analysis_config().get(key, os.environ.get(env_key, default))


def bigquery_project() -> str:
    return get_config_value("bigquery_project", "<bigquery-project>")


def bigquery_dataset() -> str:
    return get_config_value("bigquery_dataset", "airbyte_warehouse")


def bigquery_reporting_dataset() -> str:
    return get_config_value("bigquery_reporting_dataset", "airbyte_warehouse_reporting")
