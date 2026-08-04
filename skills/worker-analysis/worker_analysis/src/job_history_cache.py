#!/usr/bin/env python3
"""
Job History Cache

Saves and loads job history data to avoid redundant API calls.
Data is stored in data/job_history/{customer_name}/ directory.

Usage:
    from src.job_history_cache import (
        get_cached_job_history,
        save_job_history,
        list_cached_customers,
        get_cache_age_days
    )

    # Check for existing data first
    cached = get_cached_job_history("<Customer>")
    if cached and get_cache_age_days("<Customer>") < 7:
        # Use cached data
        result = cached
    else:
        # Fetch fresh data
        result = analyze_customer_job_history(...)
        save_job_history("<Customer>", result)
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

# Cache directory relative to project root
CACHE_DIR = Path(__file__).parent.parent / "data" / "job_history"


def _get_customer_dir(customer_name: str) -> Path:
    """Get the cache directory for a customer."""
    # Sanitize customer name for filesystem
    safe_name = "".join(c if c.isalnum() or c in "-_ " else "_" for c in customer_name)
    safe_name = safe_name.strip().replace(" ", "_")
    return CACHE_DIR / safe_name


def _get_latest_cache_file(customer_name: str) -> Optional[Path]:
    """Get the most recent cache file for a customer."""
    customer_dir = _get_customer_dir(customer_name)
    if not customer_dir.exists():
        return None

    # Find all JSON files
    json_files = list(customer_dir.glob("job_history_*.json"))
    if not json_files:
        return None

    # Sort by modification time, newest first
    json_files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    return json_files[0]


def get_cached_job_history(customer_name: str) -> Optional[Dict[str, Any]]:
    """
    Load cached job history for a customer.

    Args:
        customer_name: Customer name (case-insensitive)

    Returns:
        Cached job history dict, or None if not found
    """
    cache_file = _get_latest_cache_file(customer_name)
    if not cache_file:
        return None

    try:
        with open(cache_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def get_cache_age_days(customer_name: str) -> Optional[float]:
    """
    Get the age of cached data in days.

    Args:
        customer_name: Customer name

    Returns:
        Age in days, or None if no cache exists
    """
    cache_file = _get_latest_cache_file(customer_name)
    if not cache_file:
        return None

    mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
    age = datetime.now() - mtime
    return age.total_seconds() / 86400  # Convert to days


def get_cache_info(customer_name: str) -> Optional[Dict[str, Any]]:
    """
    Get information about cached data for a customer.

    Args:
        customer_name: Customer name

    Returns:
        Dict with cache info, or None if no cache exists
    """
    cache_file = _get_latest_cache_file(customer_name)
    if not cache_file:
        return None

    try:
        with open(cache_file, 'r') as f:
            data = json.load(f)

        mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
        age_days = (datetime.now() - mtime).total_seconds() / 86400

        return {
            "customer_name": customer_name,
            "cache_file": str(cache_file),
            "cache_date": mtime.isoformat(),
            "age_days": round(age_days, 1),
            "total_pipelines": data.get("total_pipelines"),
            "total_jobs_analyzed": data.get("job_statistics", {}).get("total_jobs_analyzed"),
            "source_breakdown": data.get("source_type_breakdown"),
            "frequency_breakdown": data.get("sync_frequency_breakdown"),
        }
    except (json.JSONDecodeError, IOError):
        return None


def save_job_history(
    customer_name: str,
    job_history: Dict[str, Any],
    organization_id: Optional[str] = None
) -> str:
    """
    Save job history data for a customer.

    Args:
        customer_name: Customer name
        job_history: Job history dict from analyze_customer_job_history()
        organization_id: Optional org ID to include in filename

    Returns:
        Path to saved file
    """
    customer_dir = _get_customer_dir(customer_name)
    customer_dir.mkdir(parents=True, exist_ok=True)

    # Create filename with timestamp
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"job_history_{timestamp}.json"
    filepath = customer_dir / filename

    # Add metadata
    job_history["_cache_metadata"] = {
        "customer_name": customer_name,
        "organization_id": organization_id or job_history.get("organization_id"),
        "cached_at": datetime.now().isoformat(),
        "cache_file": str(filepath),
    }

    with open(filepath, 'w') as f:
        json.dump(job_history, f, indent=2, default=str)

    return str(filepath)


def list_cached_customers() -> List[Dict[str, Any]]:
    """
    List all customers with cached job history.

    Returns:
        List of dicts with customer info and cache stats
    """
    if not CACHE_DIR.exists():
        return []

    customers = []
    for customer_dir in CACHE_DIR.iterdir():
        if not customer_dir.is_dir():
            continue

        customer_name = customer_dir.name.replace("_", " ")
        info = get_cache_info(customer_name)
        if info:
            customers.append(info)

    # Sort by most recently cached
    customers.sort(key=lambda x: x.get("cache_date", ""), reverse=True)
    return customers


def find_customer_by_org_id(organization_id: str) -> Optional[str]:
    """
    Find a customer name by organization ID in cached data.

    Args:
        organization_id: Airbyte organization ID

    Returns:
        Customer name if found, None otherwise
    """
    if not CACHE_DIR.exists():
        return None

    for customer_dir in CACHE_DIR.iterdir():
        if not customer_dir.is_dir():
            continue

        customer_name = customer_dir.name.replace("_", " ")
        cached = get_cached_job_history(customer_name)
        if cached and cached.get("organization_id") == organization_id:
            return customer_name

    return None


def clear_old_cache(max_age_days: int = 30) -> int:
    """
    Remove cache files older than max_age_days.

    Args:
        max_age_days: Maximum age in days to keep

    Returns:
        Number of files removed
    """
    if not CACHE_DIR.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=max_age_days)
    removed = 0

    for customer_dir in CACHE_DIR.iterdir():
        if not customer_dir.is_dir():
            continue

        for cache_file in customer_dir.glob("job_history_*.json"):
            mtime = datetime.fromtimestamp(cache_file.stat().st_mtime)
            if mtime < cutoff:
                cache_file.unlink()
                removed += 1

    return removed


# Convenience function for the main workflow
def get_or_fetch_job_history(
    customer_name: str,
    organization_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    max_cache_age_days: float = 7.0,
    force_refresh: bool = False,
    verbose: bool = False,
) -> Dict[str, Any]:
    """
    Get job history from cache or fetch fresh if needed.

    This is the main entry point - it handles caching automatically.

    Args:
        customer_name: Customer name for caching (will lookup org_id from known mappings)
        organization_id: Airbyte organization ID (optional if customer is in mappings)
        workspace_id: Airbyte workspace ID (alternative to org_id)
        max_cache_age_days: Maximum age of cache to use (default: 7 days)
        force_refresh: Force fresh API fetch even if cache exists
        verbose: Print progress messages

    Returns:
        Job history dict (from cache or fresh)
    """
    # Check cache first (unless force refresh)
    if not force_refresh:
        cache_age = get_cache_age_days(customer_name)
        if cache_age is not None and cache_age <= max_cache_age_days:
            if verbose:
                print(f"Using cached data for {customer_name} ({cache_age:.1f} days old)")
            cached = get_cached_job_history(customer_name)
            if cached:
                return cached

    # Need to fetch fresh data - try to find org_id
    if not organization_id and not workspace_id:
        # Try to get org_id from known customer mappings
        try:
            from src.airbyte_cloud_data import get_organization_info
            org_info = get_organization_info(customer_name)
            if org_info:
                organization_id = org_info[0]
                if verbose:
                    print(f"Found org_id for {customer_name}: {organization_id[:8]}...")
        except ImportError:
            pass

        # Try to get org_id from existing cache
        if not organization_id:
            cached = get_cached_job_history(customer_name)
            if cached:
                organization_id = cached.get("organization_id")

        if not organization_id:
            raise ValueError(
                f"No organization_id found for '{customer_name}'. "
                "Please provide organization_id or workspace_id, "
                "or add customer to CONFIRMED_ORG_MAPPINGS in airbyte_cloud_data.py"
            )

    if verbose:
        print(f"Fetching fresh job history for {customer_name}...")

    # Import here to avoid circular imports
    from src.job_history_analyzer import analyze_customer_job_history

    result = analyze_customer_job_history(
        organization_id=organization_id,
        workspace_id=workspace_id,
        verbose=verbose,
    )

    # Save to cache
    cache_path = save_job_history(customer_name, result, organization_id)
    if verbose:
        print(f"Saved to cache: {cache_path}")

    return result


if __name__ == "__main__":
    # CLI for listing cached customers
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "list":
        customers = list_cached_customers()
        if not customers:
            print("No cached job history data found.")
        else:
            print(f"{'Customer':<30} {'Age':<10} {'Pipelines':<10} {'Jobs':<10}")
            print("-" * 60)
            for c in customers:
                name = c.get("customer_name", "Unknown")[:28]
                age = f"{c.get('age_days', 0):.1f}d"
                pipes = c.get("total_pipelines", "?")
                jobs = c.get("total_jobs_analyzed", "?")
                print(f"{name:<30} {age:<10} {pipes:<10} {jobs:<10}")
    else:
        print("Usage: python -m src.job_history_cache list")
