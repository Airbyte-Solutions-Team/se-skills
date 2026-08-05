#!/usr/bin/env python3
"""
Airbyte Cloud Data Integration

This module provides helper functions for working with Airbyte Cloud API data
fetched via PyAirbyte MCP tools or direct API calls. It transforms raw API
responses into formats suitable for the scheduling optimization report.

Data Sources:
1. PyAirbyte MCP tools (when running in Claude Code context)
2. Direct Airbyte Cloud API (via analyze_org_workspaces functions)
3. Metabase queries (as fallback for historical data)

Usage (in Claude Code context):
    # 1. Fetch organization
    org = mcp__pyairbyte__describe_cloud_organization(organization_name="<Customer>")

    # 2. List workspaces
    workspaces = mcp__pyairbyte__list_cloud_workspaces(organization_id=org["id"])

    # 3. List connections for each workspace
    connections = mcp__pyairbyte__list_deployed_cloud_connections(workspace_id=ws_id)

    # 4. Transform for report
    from src.airbyte_cloud_data import transform_connections_for_report
    connection_data = transform_connections_for_report(connections, jobs_by_connection)

Usage (direct API):
    from src.airbyte_cloud_data import fetch_workspace_connection_data

    connection_data = fetch_workspace_connection_data(
        workspace_id="your-workspace-id",
        client_id="your-client-id",
        client_secret="your-client-secret",
        jobs_limit=20
    )
"""

from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
import re
import os

from se_config import get_config_value

# Import credentials helper
try:
    from src.credentials import get_airbyte_credentials, ensure_credentials_loaded
except ImportError:
    try:
        from credentials import get_airbyte_credentials, ensure_credentials_loaded
    except ImportError:
        # Fallback: define inline if module not available
        def ensure_credentials_loaded():
            return bool(os.environ.get("AIRBYTE_CLOUD_CLIENT_ID"))

        def get_airbyte_credentials(client_id=None, client_secret=None):
            cid = client_id or os.environ.get("AIRBYTE_CLOUD_CLIENT_ID")
            cs = client_secret or os.environ.get("AIRBYTE_CLOUD_CLIENT_SECRET")
            if not cid or not cs:
                raise ValueError("Credentials not found")
            return cid, cs


def parse_cron_schedule(cron_expression: Optional[str]) -> Dict[str, Any]:
    """
    Parse a Quartz cron expression to extract scheduling details.

    Args:
        cron_expression: 6-value Quartz cron (seconds minutes hours day-of-month month day-of-week)

    Returns:
        Dictionary with parsed schedule info
    """
    if not cron_expression:
        return {
            "has_schedule": False,
            "schedule_type": "manual",
            "description": "Manual trigger only",
        }

    parts = cron_expression.split()
    if len(parts) != 6:
        return {
            "has_schedule": True,
            "schedule_type": "unknown",
            "raw": cron_expression,
            "error": f"Expected 6 parts, got {len(parts)}",
        }

    second, minute, hour, day_of_month, month, day_of_week = parts

    result = {
        "has_schedule": True,
        "raw": cron_expression,
        "second": second,
        "minute": minute,
        "hour": hour,
        "day_of_month": day_of_month,
        "month": month,
        "day_of_week": day_of_week,
    }

    # Determine schedule type and extract hour
    if hour == "*":
        result["schedule_type"] = "hourly"
        result["description"] = f"Every hour at :{minute.zfill(2)}"
    elif "/" in hour:
        # e.g., "*/6" = every 6 hours
        interval = hour.split("/")[1]
        result["schedule_type"] = "interval"
        result["interval_hours"] = int(interval) if interval.isdigit() else 0
        result["description"] = f"Every {interval} hours"
    elif day_of_week not in ("*", "?"):
        result["schedule_type"] = "weekly"
        result["run_hour"] = int(hour) if hour.isdigit() else 0
        result["run_minute"] = int(minute) if minute.isdigit() else 0
    else:
        result["schedule_type"] = "daily"
        result["run_hour"] = int(hour) if hour.isdigit() else 0
        result["run_minute"] = int(minute) if minute.isdigit() else 0
        result["description"] = f"Daily at {int(hour):02d}:{int(minute):02d} UTC"

    return result


def extract_source_destination(connection: Dict[str, Any]) -> Tuple[str, str]:
    """
    Extract source and destination names from a connection object.

    Args:
        connection: Connection object from PyAirbyte

    Returns:
        Tuple of (source_name, destination_name)
    """
    source = connection.get("source", {})
    destination = connection.get("destination", {})

    source_name = (
        source.get("name", "") or
        source.get("sourceName", "") or
        connection.get("sourceName", "") or
        "Unknown Source"
    )

    dest_name = (
        destination.get("name", "") or
        destination.get("destinationName", "") or
        connection.get("destinationName", "") or
        "Unknown Destination"
    )

    return source_name, dest_name


def transform_connection_for_report(
    connection: Dict[str, Any],
    jobs: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """
    Transform a single connection into the format expected by the scheduling report.

    Args:
        connection: Connection object from PyAirbyte
        jobs: Optional list of recent jobs for this connection

    Returns:
        Connection data formatted for the report
    """
    connection_id = connection.get("connectionId", connection.get("connection_id", ""))
    connection_name = connection.get("name", connection.get("connectionName", "Unknown"))

    source_name, dest_name = extract_source_destination(connection)

    # Parse schedule
    schedule_data = connection.get("scheduleData", {}) or {}
    cron = schedule_data.get("cron", {}) or {}
    cron_expression = cron.get("cronExpression")

    # Also check for schedule at top level
    if not cron_expression:
        schedule = connection.get("schedule", {}) or {}
        cron_expression = schedule.get("cronExpression")

    parsed_schedule = parse_cron_schedule(cron_expression)

    # Calculate job statistics if jobs provided
    job_stats = _calculate_job_stats(jobs) if jobs else {}

    result = {
        "connection_id": connection_id,
        "connection_name": connection_name,
        "source": source_name,
        "destination": dest_name,
        "cron_expression": cron_expression,
        "schedule_type": parsed_schedule.get("schedule_type", "unknown"),
        "schedule_description": parsed_schedule.get("description", ""),
        **job_stats,
    }

    # Add run hour if available
    if "run_hour" in parsed_schedule:
        result["typical_run_hour"] = parsed_schedule["run_hour"]
        result["start_hour_utc"] = parsed_schedule["run_hour"]

    return result


def _calculate_job_stats(jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculate statistics from a list of job records.

    Args:
        jobs: List of job objects from PyAirbyte

    Returns:
        Dictionary with job statistics
    """
    if not jobs:
        return {
            "total_jobs": 0,
            "avg_duration_minutes": 0,
        }

    durations = []
    start_hours = []

    for job in jobs:
        # Extract job timing
        created_at = job.get("createdAt") or job.get("startTime")
        ended_at = job.get("endedAt") or job.get("endTime")

        if created_at:
            # Parse timestamp to get hour
            try:
                if isinstance(created_at, str):
                    # Handle ISO format
                    dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                elif isinstance(created_at, (int, float)):
                    # Handle unix timestamp (seconds)
                    dt = datetime.utcfromtimestamp(created_at)
                else:
                    dt = None

                if dt:
                    start_hours.append(dt.hour)
            except (ValueError, TypeError):
                pass

        # Calculate duration
        if created_at and ended_at:
            try:
                if isinstance(created_at, str):
                    start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                else:
                    start = datetime.utcfromtimestamp(created_at)

                if isinstance(ended_at, str):
                    end = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                else:
                    end = datetime.utcfromtimestamp(ended_at)

                duration_minutes = (end - start).total_seconds() / 60
                if duration_minutes > 0:
                    durations.append(duration_minutes)
            except (ValueError, TypeError):
                pass

    result = {
        "total_jobs": len(jobs),
        "avg_duration_minutes": sum(durations) / len(durations) if durations else 0,
    }

    # Calculate typical run hour from job start times
    if start_hours:
        # Find most common hour
        hour_counts = {}
        for h in start_hours:
            hour_counts[h] = hour_counts.get(h, 0) + 1
        typical_hour = max(hour_counts.keys(), key=lambda h: hour_counts[h])
        result["typical_run_hour"] = typical_hour
        result["start_hour_utc"] = typical_hour

    return result


def transform_connections_for_report(
    connections: List[Dict[str, Any]],
    jobs_by_connection: Optional[Dict[str, List[Dict[str, Any]]]] = None,
) -> List[Dict[str, Any]]:
    """
    Transform a list of connections into the format expected by the scheduling report.

    Args:
        connections: List of connection objects from PyAirbyte
        jobs_by_connection: Optional mapping of connection_id -> list of jobs

    Returns:
        List of connection data formatted for the report
    """
    jobs_by_connection = jobs_by_connection or {}
    result = []

    for conn in connections:
        conn_id = conn.get("connectionId", conn.get("connection_id", ""))
        jobs = jobs_by_connection.get(conn_id, [])

        transformed = transform_connection_for_report(conn, jobs)

        # Only include connections with valid schedule info
        if transformed.get("typical_run_hour") is not None or transformed.get("cron_expression"):
            result.append(transformed)

    return result


def normalize_customer_name(name: str) -> str:
    """
    Normalize a customer name for matching purposes.

    Removes common suffixes, domains, and standardizes formatting.

    Args:
        name: Customer name to normalize

    Returns:
        Normalized name for comparison
    """
    # Convert to lowercase
    normalized = name.lower().strip()

    # Remove common domain suffixes
    domain_suffixes = ['.com', '.io', '.net', '.org', '.co', '.ai', '.app']
    for suffix in domain_suffixes:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]

    # Remove common company type suffixes
    company_suffixes = [' inc', ' inc.', ' llc', ' ltd', ' ltd.', ' gmbh', ' corp', ' corp.']
    for suffix in company_suffixes:
        if normalized.endswith(suffix):
            normalized = normalized[:-len(suffix)]

    # Remove special characters but keep spaces
    normalized = re.sub(r'[^\w\s]', '', normalized)

    return normalized.strip()


def load_customer_org_mappings(mappings_file: Optional[str] = None) -> Dict[str, Dict[str, str]]:
    """
    Load customer-to-organization mappings from a JSON file.

    The file format is:
    {
        "customer_name_from_metabase": {
            "organization_id": "uuid",
            "organization_name": "Airbyte Cloud Org Name"
        }
    }

    Args:
        mappings_file: Path to mappings file. Defaults to WorkerCustomers/org_mappings.json

    Returns:
        Dictionary of customer name -> org info
    """
    import json

    if mappings_file is None:
        # Default location
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mappings_file = os.path.join(base_dir, "WorkerCustomers", "org_mappings.json")

    if not os.path.exists(mappings_file):
        return {}

    try:
        with open(mappings_file, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_customer_org_mapping(
    customer_name: str,
    organization_id: str,
    organization_name: str,
    mappings_file: Optional[str] = None,
) -> None:
    """
    Save a discovered customer-to-organization mapping.

    Args:
        customer_name: Customer name from Metabase
        organization_id: Airbyte Cloud organization ID
        organization_name: Airbyte Cloud organization name
        mappings_file: Path to save mappings
    """
    import json

    if mappings_file is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mappings_file = os.path.join(base_dir, "WorkerCustomers", "org_mappings.json")

    # Ensure directory exists
    os.makedirs(os.path.dirname(mappings_file), exist_ok=True)

    # Load existing mappings
    mappings = load_customer_org_mappings(mappings_file)

    # Add new mapping
    mappings[customer_name] = {
        "organization_id": organization_id,
        "organization_name": organization_name,
    }

    # Also add normalized version
    normalized = normalize_customer_name(customer_name)
    if normalized != customer_name.lower():
        mappings[normalized] = {
            "organization_id": organization_id,
            "organization_name": organization_name,
        }

    # Save
    with open(mappings_file, 'w') as f:
        json.dump(mappings, f, indent=2)


def find_organization_by_name(
    org_name: str,
    known_mappings: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    """
    Map a customer name to an Airbyte Cloud organization name.

    Some customers have different names in our systems vs Airbyte Cloud.
    This function helps bridge that gap.

    Args:
        org_name: Customer name from our reports
        known_mappings: Optional dict of customer_name -> airbyte_org_name

    Returns:
        Organization name to use with PyAirbyte, or None if no mapping
    """
    # Default mappings for known discrepancies
    # Metabase account name -> Airbyte Cloud organization name
    # Populate via .se-config.yaml worker_analysis.org_name_mappings instead of hard-coding.
    default_mappings = get_config_value("org_name_mappings", {})

    # Merge with any provided mappings
    mappings = {**default_mappings, **(known_mappings or {})}

    # Also load from persistent file
    file_mappings = load_customer_org_mappings()
    for customer, org_info in file_mappings.items():
        if customer not in mappings:
            mappings[customer] = org_info.get("organization_name", customer)

    # Try exact match first
    if org_name in mappings:
        return mappings[org_name]

    # Try case-insensitive match
    org_name_lower = org_name.lower()
    for key, value in mappings.items():
        if key.lower() == org_name_lower:
            return value

    # Try normalized match
    org_name_normalized = normalize_customer_name(org_name)
    for key, value in mappings.items():
        if normalize_customer_name(key) == org_name_normalized:
            return value

    # Return original name if no mapping found (will try exact match with API)
    return org_name


def get_organization_name_variations(customer_name: str) -> List[str]:
    """
    Generate possible Airbyte Cloud organization name variations to try.

    Args:
        customer_name: Customer name from Metabase

    Returns:
        List of possible organization names to try, in order of likelihood
    """
    variations = []

    # 1. Exact name
    variations.append(customer_name)

    # 2. With mapped name if available
    mapped = find_organization_by_name(customer_name)
    if mapped and mapped != customer_name:
        variations.append(mapped)

    # 3. Without domain suffix
    for suffix in ['.com', '.io', '.net', '.org', '.co', '.ai', '.app']:
        if customer_name.lower().endswith(suffix):
            base_name = customer_name[:-len(suffix)]
            # Try various capitalizations
            variations.append(base_name)
            variations.append(base_name.title())
            variations.append(base_name.capitalize())

    # 4. Title case version
    variations.append(customer_name.title())

    # 5. Space-separated from camelCase
    spaced = re.sub(r'([a-z])([A-Z])', r'\1 \2', customer_name)
    if spaced != customer_name:
        variations.append(spaced)
        variations.append(spaced.title())

    # Remove duplicates while preserving order
    seen = set()
    unique_variations = []
    for v in variations:
        v_lower = v.lower()
        if v_lower not in seen:
            seen.add(v_lower)
            unique_variations.append(v)

    return unique_variations


# =============================================================================
# Organization Discovery - Comprehensive Mapping
# =============================================================================

# This mapping is built from the configured billing dataset's account table.
# The account table has account_name_masked (customer name) and organization_id.
# Query to get mappings (substitute bigquery_project/bigquery_dataset from .se-config.yaml):
#   SELECT a.account_name_masked, a.organization_id, o.organization_name_masked
#   FROM {bigquery_project}.{bigquery_dataset}.account a
#   LEFT JOIN {bigquery_project}.{bigquery_dataset}.organization o ON a.organization_id = o.organization_id
#   WHERE a.organization_id IS NOT NULL
#
# Format: "metabase_account_name": ("organization_id", "organization_name")

# Known workspace IDs for organizations (when API filtering doesn't work correctly)
# Format: "organization_id": ["workspace_id1", "workspace_id2", ...]
# Populate via .se-config.yaml worker_analysis.workspace_map instead of hard-coding customer IDs.
CONFIRMED_WORKSPACE_MAPPINGS: Dict[str, List[str]] = get_config_value(
    "workspace_map", {}
)


def get_workspace_ids_for_organization(organization_id: str) -> List[str]:
    """
    Get known workspace IDs for an organization from our confirmed mappings.

    Args:
        organization_id: Organization UUID

    Returns:
        List of workspace IDs, or empty list if not found
    """
    return CONFIRMED_WORKSPACE_MAPPINGS.get(organization_id, [])


CONFIRMED_ORG_MAPPINGS: Dict[str, Tuple[str, str]] = {
    k: (v["organization_id"], v.get("organization_name", k))
    for k, v in get_config_value("org_map", {}).items()
}


def get_organization_info(customer_name: str) -> Optional[Tuple[str, str]]:
    """
    Get organization ID and name for a customer.

    Args:
        customer_name: Customer name from Metabase

    Returns:
        Tuple of (organization_id, organization_name) or None if not found
    """
    # Check confirmed mappings first (case-insensitive)
    customer_lower = customer_name.lower()
    for key, value in CONFIRMED_ORG_MAPPINGS.items():
        if key.lower() == customer_lower:
            return value

    # Check normalized variations
    normalized = normalize_customer_name(customer_name)
    for key, value in CONFIRMED_ORG_MAPPINGS.items():
        if normalize_customer_name(key) == normalized:
            return value

    # Check file-based mappings
    file_mappings = load_customer_org_mappings()
    if customer_name in file_mappings:
        info = file_mappings[customer_name]
        return (info.get("organization_id", ""), info.get("organization_name", ""))

    for key, info in file_mappings.items():
        if key.lower() == customer_lower or normalize_customer_name(key) == normalized:
            return (info.get("organization_id", ""), info.get("organization_name", ""))

    return None


def get_workspace_ids_for_customer(customer_name: str) -> List[str]:
    """
    Get all workspace IDs for a customer from known mappings.

    This is useful when workspace IDs have been previously discovered
    and stored in the WorkerCustomers files.

    Args:
        customer_name: Customer name

    Returns:
        List of workspace IDs, empty if none found
    """
    import json

    # Check WorkerCustomers directory for customer-specific files
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    customer_dir = os.path.join(base_dir, "WorkerCustomers")

    workspace_ids = []

    if os.path.exists(customer_dir):
        # Check for customer-specific JSON file
        customer_file = os.path.join(customer_dir, f"{customer_name}.json")
        if os.path.exists(customer_file):
            try:
                with open(customer_file, 'r') as f:
                    data = json.load(f)
                    # Look for workspace_ids in various formats
                    if "workspace_ids" in data:
                        workspace_ids.extend(data["workspace_ids"])
                    if "workspaces" in data:
                        for ws in data["workspaces"]:
                            if isinstance(ws, str):
                                workspace_ids.append(ws)
                            elif isinstance(ws, dict) and "id" in ws:
                                workspace_ids.append(ws["id"])
            except (json.JSONDecodeError, IOError):
                pass

    return list(set(workspace_ids))  # Remove duplicates


def aggregate_workspace_connections(
    workspaces_data: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Aggregate connections from multiple workspaces into a single list.

    Args:
        workspaces_data: List of dicts with "workspace_id", "workspace_name", "connections"

    Returns:
        Flattened list of all connections with workspace info added
    """
    all_connections = []

    for ws in workspaces_data:
        workspace_id = ws.get("workspace_id", "")
        workspace_name = ws.get("workspace_name", "")
        connections = ws.get("connections", [])

        for conn in connections:
            conn_with_workspace = {
                **conn,
                "workspace_id": workspace_id,
                "workspace_name": workspace_name,
            }
            all_connections.append(conn_with_workspace)

    return all_connections


def get_connection_schedule_summary(connections: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Generate a summary of connection schedules across all connections.

    Args:
        connections: List of transformed connection data

    Returns:
        Summary statistics about schedules
    """
    total = len(connections)
    by_schedule_type = {}
    by_hour = {h: 0 for h in range(24)}

    for conn in connections:
        schedule_type = conn.get("schedule_type", "unknown")
        by_schedule_type[schedule_type] = by_schedule_type.get(schedule_type, 0) + 1

        run_hour = conn.get("typical_run_hour")
        if run_hour is not None and 0 <= run_hour <= 23:
            by_hour[run_hour] += 1

    # Find peak scheduling hours
    peak_schedule_hours = sorted(
        [(h, c) for h, c in by_hour.items() if c > 0],
        key=lambda x: x[1],
        reverse=True
    )[:5]

    return {
        "total_connections": total,
        "by_schedule_type": by_schedule_type,
        "connections_by_hour": by_hour,
        "peak_schedule_hours": [h for h, _ in peak_schedule_hours],
        "has_schedule_data": any(c > 0 for c in by_hour.values()),
    }


# Constants for schedule analysis
RECOMMENDED_QUIET_HOURS = [2, 3, 4, 5, 6]  # Default UTC hours to recommend
PEAK_BUSINESS_HOURS = [9, 10, 11, 14, 15, 16]  # Common business hours to avoid


# =============================================================================
# Direct Airbyte Cloud API Integration
# =============================================================================
# These functions use the existing analyze_org_workspaces.py infrastructure
# for efficient API calls when running outside of Claude Code MCP context.


def fetch_workspaces_for_organization(
    organization_id: str,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Fetch all workspaces for an Airbyte Cloud organization.

    This uses the organization_id (from CONFIRMED_ORG_MAPPINGS) to retrieve
    the list of workspaces associated with that organization.

    Args:
        organization_id: Airbyte Cloud organization UUID
        client_id: API client ID (auto-loads from ~/.env if not provided)
        client_secret: API client secret (auto-loads from ~/.env if not provided)

    Returns:
        List of workspace dicts with workspaceId, name, etc.
    """
    import requests
    import sys

    # Add src directory to path if not already there
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    try:
        from analyze_org_workspaces import get_access_token
    except ImportError:
        from src.analyze_org_workspaces import get_access_token

    # Get credentials (auto-loads from ~/.env if not provided)
    client_id, client_secret = get_airbyte_credentials(client_id, client_secret)

    # Get access token
    token = get_access_token(client_id, client_secret)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json"
    }

    # List workspaces for the organization
    url = "https://api.airbyte.com/v1/workspaces"
    params = {
        "organizationId": organization_id,
        "limit": 100
    }

    response = requests.get(url, headers=headers, params=params, timeout=30)
    response.raise_for_status()

    workspaces = response.json().get("data", [])
    return workspaces


def fetch_connections_for_organization(
    organization_id: str,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Fetch all connections across all workspaces for an organization.

    This is the main entry point for getting connection data when you have
    an organization ID from CONFIRMED_ORG_MAPPINGS.

    Flow: organization_id → fetch workspaces → fetch connections per workspace

    Args:
        organization_id: Airbyte Cloud organization UUID
        client_id: API client ID (auto-loads from ~/.env if not provided)
        client_secret: API client secret (auto-loads from ~/.env if not provided)
        jobs_limit: Number of recent jobs per connection

    Returns:
        Flattened list of all connections with workspace info included
    """
    # Get credentials (auto-loads from ~/.env if not provided)
    client_id, client_secret = get_airbyte_credentials(client_id, client_secret)

    # Check for confirmed workspace mappings first (faster and more reliable)
    known_workspace_ids = get_workspace_ids_for_organization(organization_id)
    if known_workspace_ids:
        print(f"  Using confirmed workspace mapping ({len(known_workspace_ids)} workspace(s))")
        return get_all_connections_for_report(
            workspace_ids=known_workspace_ids,
            client_id=client_id,
            client_secret=client_secret,
            jobs_limit=jobs_limit,
        )

    # Fall back to API lookup
    # Step 1: Get workspaces for this organization
    workspaces = fetch_workspaces_for_organization(
        organization_id=organization_id,
        client_id=client_id,
        client_secret=client_secret,
    )

    if not workspaces:
        print(f"  No workspaces found for organization {organization_id[:8]}...")
        return []

    # Step 2: Filter workspaces to only those belonging to this organization
    # (API may return workspaces from other orgs in paginated results)
    org_workspaces = [
        ws for ws in workspaces
        if ws.get("organizationId") == organization_id
    ]

    if not org_workspaces:
        # Fall back to all returned workspaces if none match (backwards compat)
        print(f"  Warning: No workspaces matched org ID, using all {len(workspaces)} returned")
        org_workspaces = workspaces
    else:
        print(f"  Found {len(org_workspaces)} workspace(s) for organization (filtered from {len(workspaces)} total)")

    # Step 3: Get workspace IDs
    workspace_ids = [ws.get("workspaceId") for ws in org_workspaces if ws.get("workspaceId")]

    # Step 3: Fetch connections from all workspaces
    all_connections = get_all_connections_for_report(
        workspace_ids=workspace_ids,
        client_id=client_id,
        client_secret=client_secret,
        jobs_limit=jobs_limit,
    )

    # Add workspace names to connections
    workspace_name_map = {ws.get("workspaceId"): ws.get("name", "Unknown") for ws in workspaces}
    for conn in all_connections:
        ws_id = conn.get("workspace_id", "")
        conn["workspace_name"] = workspace_name_map.get(ws_id, "Unknown")

    return all_connections


def fetch_workspace_connection_data(
    workspace_id: str,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Fetch connection data with job history from Airbyte Cloud API.

    Uses the efficient job fetching from analyze_org_workspaces.py.

    Args:
        workspace_id: Airbyte Cloud workspace ID
        client_id: API client ID (auto-loads from ~/.env if not provided)
        client_secret: API client secret (auto-loads from ~/.env if not provided)
        jobs_limit: Number of recent jobs to fetch per connection

    Returns:
        List of connection data formatted for the scheduling report
    """
    # Import here to avoid circular imports and allow standalone usage
    import sys
    # Add src directory to path if not already there
    src_dir = os.path.dirname(os.path.abspath(__file__))
    if src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    try:
        from analyze_org_workspaces import (
            get_access_token,
            fetch_connections,
            fetch_job_history,
            process_jobs,
        )
    except ImportError:
        # Try package import
        from src.analyze_org_workspaces import (
            get_access_token,
            fetch_connections,
            fetch_job_history,
            process_jobs,
        )

    # Get credentials (auto-loads from ~/.env if not provided)
    client_id, client_secret = get_airbyte_credentials(client_id, client_secret)

    # Authenticate
    token = get_access_token(client_id, client_secret)

    # Fetch connections
    connections = fetch_connections(token, workspace_id)

    # Fetch job history for each connection
    connection_data = []
    for conn in connections:
        connection_id = conn.get("connectionId", "")
        connection_name = conn.get("name", "Unknown")

        # Parse source/destination from connection name if available
        source_name = ""
        dest_name = ""
        if " → " in connection_name or " -> " in connection_name:
            parts = connection_name.split("→" if "→" in connection_name else "->")
            source_name = parts[0].strip() if len(parts) > 0 else ""
            dest_name = parts[1].strip() if len(parts) > 1 else ""

        # Get schedule info
        schedule_data = conn.get("scheduleData", {}) or {}
        cron = schedule_data.get("cron", {}) or {}
        cron_expression = cron.get("cronExpression")

        # Also check for schedule at top level
        if not cron_expression:
            schedule = conn.get("schedule", {}) or {}
            cron_expression = schedule.get("cronExpression")

        # Fetch recent jobs
        jobs = fetch_job_history(token, connection_id, limit=jobs_limit)
        processed_jobs = process_jobs(jobs)

        # Calculate job statistics
        job_stats = _calculate_job_stats_from_processed(processed_jobs)

        connection_data.append({
            "connection_id": connection_id,
            "connection_name": connection_name,
            "source": source_name,
            "destination": dest_name,
            "cron_expression": cron_expression,
            "workspace_id": workspace_id,
            **job_stats,
        })

    return connection_data


def _calculate_job_stats_from_processed(processed_jobs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Calculate statistics from processed job records (from analyze_org_workspaces).

    Args:
        processed_jobs: List of processed job dicts with startTime, endTime, duration

    Returns:
        Dictionary with job statistics
    """
    if not processed_jobs:
        return {
            "total_jobs": 0,
            "avg_duration_minutes": 0,
        }

    durations = []
    start_hours = []

    for job in processed_jobs:
        # Duration is already in seconds
        duration = job.get("duration", 0)
        if duration > 0:
            durations.append(duration / 60.0)  # Convert to minutes

        # Parse start time to get hour
        start_time_str = job.get("startTime")
        if start_time_str:
            try:
                dt = datetime.fromisoformat(start_time_str.replace("Z", "+00:00"))
                start_hours.append(dt.hour)
            except (ValueError, TypeError):
                pass

    result = {
        "total_jobs": len(processed_jobs),
        "avg_duration_minutes": sum(durations) / len(durations) if durations else 0,
    }

    # Calculate typical run hour from job start times
    if start_hours:
        hour_counts = {}
        for h in start_hours:
            hour_counts[h] = hour_counts.get(h, 0) + 1
        typical_hour = max(hour_counts.keys(), key=lambda h: hour_counts[h])
        result["typical_run_hour"] = typical_hour
        result["start_hour_utc"] = typical_hour

    return result


def fetch_multi_workspace_connection_data(
    workspace_ids: List[str],
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_limit: int = 20,
    rate_limit_delay: float = 0.5,
) -> Dict[str, List[Dict[str, Any]]]:
    """
    Fetch connection data from multiple workspaces.

    Args:
        workspace_ids: List of Airbyte Cloud workspace IDs
        client_id: API client ID
        client_secret: API client secret
        jobs_limit: Number of recent jobs per connection
        rate_limit_delay: Seconds to wait between workspace API calls (default 0.5s)

    Returns:
        Dictionary mapping workspace_id -> list of connection data
    """
    import time

    result = {}
    consecutive_failures = 0
    max_consecutive_failures = 5

    for i, ws_id in enumerate(workspace_ids):
        # Stop if too many consecutive failures (likely rate limited)
        if consecutive_failures >= max_consecutive_failures:
            print(f"  Stopping after {max_consecutive_failures} consecutive failures (rate limited)")
            break

        try:
            connections = fetch_workspace_connection_data(
                workspace_id=ws_id,
                client_id=client_id,
                client_secret=client_secret,
                jobs_limit=jobs_limit,
            )
            result[ws_id] = connections
            consecutive_failures = 0  # Reset on success

            # Rate limit delay between successful calls
            if i < len(workspace_ids) - 1:
                time.sleep(rate_limit_delay)

        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "Too Many Requests" in error_str:
                consecutive_failures += 1
                # Exponential backoff on rate limit
                backoff = rate_limit_delay * (2 ** consecutive_failures)
                print(f"  Rate limited, waiting {backoff:.1f}s...")
                time.sleep(backoff)
            else:
                print(f"Warning: Failed to fetch connections for workspace {ws_id}: {e}")
            result[ws_id] = []

    return result


def get_all_connections_for_report(
    workspace_ids: List[str],
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_limit: int = 20,
) -> List[Dict[str, Any]]:
    """
    Fetch and flatten all connections from multiple workspaces for report generation.

    Args:
        workspace_ids: List of workspace IDs
        client_id: API client ID
        client_secret: API client secret
        jobs_limit: Number of recent jobs per connection

    Returns:
        Flattened list of all connections with workspace info included
    """
    workspace_data = fetch_multi_workspace_connection_data(
        workspace_ids=workspace_ids,
        client_id=client_id,
        client_secret=client_secret,
        jobs_limit=jobs_limit,
    )

    all_connections = []
    for ws_id, connections in workspace_data.items():
        for conn in connections:
            conn["workspace_id"] = ws_id
            all_connections.append(conn)

    return all_connections


# =============================================================================
# PyAirbyte MCP Response Transformers
# =============================================================================
# These functions transform PyAirbyte MCP tool responses into the format
# expected by the scheduling optimization report.

def transform_pyairbyte_connections_response(
    connections_response: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Transform a PyAirbyte list_deployed_cloud_connections response.

    Args:
        connections_response: Response from mcp__pyairbyte__list_deployed_cloud_connections

    Returns:
        List of connection dicts in report format
    """
    # Handle different response formats
    if isinstance(connections_response, list):
        connections = connections_response
    elif isinstance(connections_response, dict):
        connections = connections_response.get("result", connections_response.get("data", []))
    else:
        return []

    result = []
    for conn in connections:
        transformed = transform_connection_for_report(conn)
        result.append(transformed)

    return result


def transform_pyairbyte_jobs_response(
    jobs_response: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Transform a PyAirbyte list_cloud_sync_jobs response.

    Args:
        jobs_response: Response from mcp__pyairbyte__list_cloud_sync_jobs

    Returns:
        List of job dicts
    """
    # Handle different response formats
    if isinstance(jobs_response, list):
        jobs = jobs_response
    elif isinstance(jobs_response, dict):
        jobs = jobs_response.get("result", jobs_response.get("data", []))
    else:
        return []

    return jobs


def enrich_connections_with_jobs(
    connections: List[Dict[str, Any]],
    jobs_by_connection: Dict[str, List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """
    Enrich connection data with job statistics.

    Args:
        connections: List of connection dicts (from transform_pyairbyte_connections_response)
        jobs_by_connection: Mapping of connection_id -> list of jobs

    Returns:
        Connections with job statistics added
    """
    for conn in connections:
        conn_id = conn.get("connection_id", "")
        jobs = jobs_by_connection.get(conn_id, [])

        if jobs:
            job_stats = _calculate_job_stats(jobs)
            conn.update(job_stats)

    return connections
