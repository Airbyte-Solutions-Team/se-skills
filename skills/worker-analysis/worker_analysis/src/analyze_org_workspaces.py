#!/usr/bin/env python3
"""
Analyze multiple workspaces in an organization for worker requirements.

This script:
1. Fetches connections from all workspaces
2. Analyzes job history for a specified period
3. Classifies connectors efficiently in batch
4. Calculates 99th percentile concurrency
5. Determines worker requirements
"""

import json
import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from collections import defaultdict
import sys

# Import our modules
try:
    from src.connector_classifier import ConnectorClassifier
    from src.worker_calculator import WorkerCalculator
    from src.credentials import get_airbyte_credentials, ensure_credentials_loaded
    from src import config
except ImportError:
    try:
        from connector_classifier import ConnectorClassifier
        from worker_calculator import WorkerCalculator
        from credentials import get_airbyte_credentials, ensure_credentials_loaded
        import config
    except ImportError as e:
        print(f"❌ Error importing modules: {e}")
        print("Make sure all required modules are in the same directory")
        sys.exit(1)

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
TOKEN_URL = "https://api.airbyte.com/v1/applications/token"
BASE_URL = "https://api.airbyte.com/v1/connections"
JOBS_URL = "https://api.airbyte.com/v1/jobs"
PAGE_SIZE = 200

# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------
def get_access_token(client_id: str, client_secret: str) -> str:
    """Fetch access token from Airbyte Cloud API."""
    payload = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "client_credentials"
    }
    r = requests.post(
        TOKEN_URL,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        json=payload,
        timeout=30
    )
    r.raise_for_status()
    token = r.json().get("access_token")
    if not token:
        raise ValueError("No access_token in response")
    return token

# --------------------------------------------------------------------------
# Data fetching
# --------------------------------------------------------------------------
def fetch_connections(token: str, workspace_id: str) -> List[Dict]:
    """Fetch all connections for a workspace."""
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    connections = []
    offset = 0

    while True:
        params = {
            "workspaceIds": workspace_id,
            "limit": PAGE_SIZE,
            "offset": offset
        }
        r = requests.get(BASE_URL, headers=headers, params=params, timeout=30)
        r.raise_for_status()
        page = r.json()["data"]
        connections.extend(page)

        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.1)

    return connections

def fetch_job_history(
    token: str,
    connection_id: str,
    limit: int = 20,
    max_retries: int = 2
) -> List[Dict]:
    """
    Fetch recent job history for a connection.

    Uses the same efficient approach as analyze_connections.py:
    - Just fetch last N jobs (no date filtering)
    - Simple and fast
    """
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    params = {
        "connectionId": connection_id,
        "limit": limit,
        "orderBy": "createdAt|DESC"
    }

    for attempt in range(max_retries):
        try:
            r = requests.get(JOBS_URL, headers=headers, params=params, timeout=60)
            r.raise_for_status()
            return r.json().get("data", [])
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # Exponential backoff: 1s, 2s, 4s
                print(f"      ⏱️  Timeout, retrying in {wait_time}s... (attempt {attempt + 2}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                print(f"      ❌ Timeout after {max_retries} attempts, skipping")
                return []
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 429:  # Rate limited
                wait_time = 5 * (attempt + 1)
                print(f"      ⏳ Rate limited, waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            print(f"      ⚠️  HTTP Error: {e.response.status_code}")
            return []
        except Exception as e:
            print(f"      ⚠️  Error: {str(e)[:50]}...")
            return []

    return []

# --------------------------------------------------------------------------
# Timestamp parsing
# --------------------------------------------------------------------------
def parse_timestamp(timestamp_str: str) -> Optional[datetime]:
    """Parse ISO 8601 timestamp to datetime object."""
    if not timestamp_str:
        return None
    try:
        return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
    except Exception:
        return None

def parse_duration_to_seconds(duration_str: str) -> Optional[float]:
    """Parse ISO 8601 duration (PT2M4S) to seconds."""
    if not duration_str or not duration_str.startswith('PT'):
        return None

    try:
        import re
        pattern = r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?'
        match = re.match(pattern, duration_str)
        if not match:
            return None

        hours = int(match.group(1) or 0)
        minutes = int(match.group(2) or 0)
        seconds = float(match.group(3) or 0)

        return hours * 3600 + minutes * 60 + seconds
    except Exception:
        return None

# --------------------------------------------------------------------------
# Job processing
# --------------------------------------------------------------------------
def process_jobs(jobs: List[Dict]) -> List[Dict]:
    """
    Process jobs to extract timing information.

    Returns list of dicts with:
    - startTime: ISO timestamp string
    - endTime: ISO timestamp string (if available)
    - duration: seconds
    - status: job status
    """
    processed_jobs = []

    for job in jobs:
        start_time_str = job.get("startTime") or job.get("startedAt") or job.get("createdAt")
        if not start_time_str:
            continue

        start_time = parse_timestamp(start_time_str)
        if not start_time:
            continue

        status = job.get("status", "unknown")

        # Get duration
        duration = None
        end_time = None

        if job.get("duration"):
            duration = parse_duration_to_seconds(job.get("duration"))

        # Try to calculate from end time if no duration
        if not duration and status in ["succeeded", "failed", "cancelled"]:
            end_time_str = job.get("lastUpdatedAt") or job.get("endedAt")
            if end_time_str:
                end_time = parse_timestamp(end_time_str)
                if end_time:
                    duration = (end_time - start_time).total_seconds()

        # Skip jobs with no duration (can't contribute to concurrency)
        if not duration or duration <= 0:
            continue

        # Calculate end time if we have duration
        if not end_time:
            end_time = start_time + timedelta(seconds=duration)

        # Store as ISO strings for JSON serialization
        processed_jobs.append({
            "startTime": start_time.isoformat(),
            "endTime": end_time.isoformat(),
            "duration": duration,
            "status": status,
            "jobId": job.get("jobId")
        })

    return processed_jobs

# --------------------------------------------------------------------------
# Concurrency analysis
# --------------------------------------------------------------------------
def calculate_concurrent_jobs_timeline(
    all_connection_jobs: Dict[str, List[Dict]]
) -> Tuple[List[float], List[float]]:
    """
    Calculate concurrent API and DB jobs over time.

    Returns:
        Tuple of (api_concurrent_samples, db_concurrent_samples)
    """
    # Build timeline of all job start/end events
    events = []

    for conn_id, info in all_connection_jobs.items():
        connector_type = info["connector_type"]
        jobs = info["jobs"]

        for job in jobs:
            # Parse ISO string back to datetime for timeline calculation
            start_time = parse_timestamp(job["startTime"])
            end_time = parse_timestamp(job["endTime"])

            if start_time and end_time:
                events.append({
                    "time": start_time,
                    "type": "start",
                    "connector_type": connector_type,
                    "connection_id": conn_id
                })
                events.append({
                    "time": end_time,
                    "type": "end",
                    "connector_type": connector_type,
                    "connection_id": conn_id
                })

    if not events:
        return [], []

    # Sort events by time
    events.sort(key=lambda x: x["time"])

    # Track concurrent jobs over time
    api_concurrent_samples = []
    db_concurrent_samples = []

    active_api = set()
    active_db = set()

    for event in events:
        if event["type"] == "start":
            if event["connector_type"] == "API":
                active_api.add(event["connection_id"])
            else:  # DATABASE
                active_db.add(event["connection_id"])
        else:  # end
            if event["connector_type"] == "API":
                active_api.discard(event["connection_id"])
            else:
                active_db.discard(event["connection_id"])

        # Sample current concurrency
        api_concurrent_samples.append(float(len(active_api)))
        db_concurrent_samples.append(float(len(active_db)))

    return api_concurrent_samples, db_concurrent_samples

# --------------------------------------------------------------------------
# Main analysis
# --------------------------------------------------------------------------
def analyze_workspaces(
    workspace_ids: List[str],
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    jobs_limit: int = 20,
    connection_limit: Optional[int] = None
) -> Dict:
    """
    Analyze worker requirements across multiple workspaces.

    Args:
        workspace_ids: List of workspace IDs to analyze
        client_id: Airbyte Cloud client ID (auto-loads from ~/.env if not provided)
        client_secret: Airbyte Cloud client secret (auto-loads from ~/.env if not provided)
        jobs_limit: Number of recent jobs to fetch per connection (default: 20)
        connection_limit: Optional limit on connections per workspace (for testing)

    Returns:
        Analysis results with worker requirements
    """
    print(f"\n{'='*80}")
    print(f"🚀 ANALYZING {len(workspace_ids)} WORKSPACES")
    print(f"{'='*80}\n")
    print(f"Jobs per connection: Last {jobs_limit} jobs")
    print(f"Worker Model: Universal (API/5 + DB/2)\n")

    # Get credentials (auto-loads from ~/.env if not provided)
    client_id, client_secret = get_airbyte_credentials(client_id, client_secret)

    # Authenticate
    print("🔐 Authenticating with Airbyte Cloud API...")
    token = get_access_token(client_id, client_secret)
    print("✅ Authentication successful\n")

    # Initialize classifier and calculator
    classifier = ConnectorClassifier()
    calculator = WorkerCalculator()

    # Collect all connections across workspaces
    all_workspaces_data = []
    all_connection_jobs = {}  # connection_id -> {connector_type, jobs, ...}
    unique_connectors = set()

    for idx, workspace_id in enumerate(workspace_ids, 1):
        print(f"{'='*80}")
        print(f"📊 [{idx}/{len(workspace_ids)}] WORKSPACE: {workspace_id}")
        print(f"{'='*80}\n")

        # Fetch connections
        print(f"   Fetching connections...")
        connections = fetch_connections(token, workspace_id)

        # Apply connection limit if specified (for testing)
        if connection_limit and len(connections) > connection_limit:
            print(f"   Found {len(connections)} connections, limiting to first {connection_limit} for testing\n")
            connections = connections[:connection_limit]
        else:
            print(f"   Found {len(connections)} connections\n")

        workspace_connections = []

        for conn_idx, conn in enumerate(connections, 1):
            connection_id = conn.get("connectionId")
            name = conn.get("name", "<unnamed>")
            source_id = conn.get("sourceId", "")
            dest_id = conn.get("destinationId", "")

            # Extract connector names from IDs or connection name
            # Most connections have format: "Source Name → Dest Name"
            source_name = ""
            dest_name = ""

            if " → " in name or " -> " in name:
                parts = name.split("→" if "→" in name else "->")
                source_name = parts[0].strip() if len(parts) > 0 else ""
                dest_name = parts[1].strip() if len(parts) > 1 else ""

            # Store unique connector names for batch classification
            if source_name:
                unique_connectors.add(source_name)
            if dest_name:
                unique_connectors.add(dest_name)

            print(f"   [{conn_idx}/{len(connections)}] {name[:60]}")

            # Fetch job history with timing
            fetch_start = time.time()
            jobs = fetch_job_history(token, connection_id, limit=jobs_limit)
            fetch_time = time.time() - fetch_start
            print(f"      → {len(jobs)} recent jobs ({fetch_time:.1f}s)")

            # Process jobs
            processed_jobs = process_jobs(jobs)
            if processed_jobs:
                print(f"      → {len(processed_jobs)} jobs with valid timing data")

            workspace_connections.append({
                "connection_id": connection_id,
                "name": name,
                "source_name": source_name,
                "dest_name": dest_name,
                "job_count": len(processed_jobs),
                "jobs": processed_jobs
            })

            # Store for concurrency analysis (will classify later)
            if processed_jobs:
                all_connection_jobs[connection_id] = {
                    "name": name,
                    "source_name": source_name,
                    "dest_name": dest_name,
                    "jobs": processed_jobs,
                    "connector_type": None  # Will be set after batch classification
                }

            # No sleep needed - we have good timeout/retry logic

        all_workspaces_data.append({
            "workspace_id": workspace_id,
            "total_connections": len(connections),
            "connections_with_jobs": len([c for c in workspace_connections if c["job_count"] > 0]),
            "connections": workspace_connections
        })

        print()

    # Batch classify all unique connectors
    print(f"\n{'='*80}")
    print(f"🔍 CLASSIFYING {len(unique_connectors)} UNIQUE CONNECTORS")
    print(f"{'='*80}\n")

    connector_classifications = {}
    for connector_name in unique_connectors:
        classification = classifier.classify(connector_name, use_mcp=False)
        connector_classifications[connector_name] = classification
        print(f"   {connector_name}: {classification}")

    print(f"\n✅ Classification complete\n")

    # Assign connector types to connections
    api_connections = 0
    db_connections = 0
    unknown_connections = 0

    for conn_id, info in all_connection_jobs.items():
        source_name = info["source_name"]
        dest_name = info["dest_name"]

        # Classify based on source (primary determinant)
        source_type = connector_classifications.get(source_name, "UNKNOWN")
        dest_type = connector_classifications.get(dest_name, "UNKNOWN")

        # Use source type, fallback to dest if source unknown
        if source_type != "UNKNOWN":
            info["connector_type"] = source_type
        elif dest_type != "UNKNOWN":
            info["connector_type"] = dest_type
        else:
            info["connector_type"] = "UNKNOWN"

        if info["connector_type"] == "API":
            api_connections += 1
        elif info["connector_type"] == "DATABASE":
            db_connections += 1
        else:
            unknown_connections += 1

    print(f"{'='*80}")
    print(f"📈 CONNECTOR BREAKDOWN")
    print(f"{'='*80}\n")
    print(f"   API connections: {api_connections}")
    print(f"   Database connections: {db_connections}")
    print(f"   Unknown connections: {unknown_connections}")
    print(f"   Total: {len(all_connection_jobs)}\n")

    # Calculate concurrency
    print(f"{'='*80}")
    print(f"⚡ CALCULATING CONCURRENCY")
    print(f"{'='*80}\n")

    api_samples, db_samples = calculate_concurrent_jobs_timeline(all_connection_jobs)

    print(f"   Concurrency samples collected: {len(api_samples)}")
    print(f"   API peak concurrent: {max(api_samples) if api_samples else 0:.1f}")
    print(f"   DB peak concurrent: {max(db_samples) if db_samples else 0:.1f}\n")

    # Calculate worker requirements
    print(f"{'='*80}")
    print(f"👷 CALCULATING WORKER REQUIREMENTS")
    print(f"{'='*80}\n")

    if not api_samples and not db_samples:
        print("⚠️  No concurrency data available - no jobs with valid timing found\n")
        result = {
            "workers_required": 0,
            "message": "No job data available for worker calculation"
        }
    else:
        result = calculator.calculate_from_measured_data(
            api_concurrent_samples=api_samples if api_samples else [0.0],
            db_concurrent_samples=db_samples if db_samples else [0.0]
        )

        concurrency = result.get('concurrency_analysis', {})
        capacity = result.get('capacity_calculation', {})

        print(f"   99th Percentile API Concurrent: {concurrency.get('p99_api_concurrent', 0):.2f}")
        print(f"   99th Percentile DB Concurrent: {concurrency.get('p99_db_concurrent', 0):.2f}")
        print(f"   API Capacity Required: {capacity.get('api_capacity', 0):.2f}")
        print(f"   DB Capacity Required: {capacity.get('db_capacity', 0):.2f}")
        print(f"   Total Capacity: {capacity.get('total_capacity', 0):.2f}")
        print(f"\n   🎯 WORKERS REQUIRED: {result['workers_required']} {result['worker_type']}\n")

    # Compile final results
    final_results = {
        "analysis_timestamp": datetime.utcnow().isoformat(),
        "jobs_limit": jobs_limit,
        "worker_model": "universal",
        "total_workspaces": len(workspace_ids),
        "workspace_ids": workspace_ids,
        "workspaces": all_workspaces_data,
        "summary": {
            "total_connections": sum(w["total_connections"] for w in all_workspaces_data),
            "connections_with_jobs": sum(w["connections_with_jobs"] for w in all_workspaces_data),
            "api_connections": api_connections,
            "db_connections": db_connections,
            "unknown_connections": unknown_connections
        },
        "concurrency_analysis": {
            "sample_count": len(api_samples),
            "api_concurrent_samples": api_samples[:100],  # First 100 samples
            "db_concurrent_samples": db_samples[:100],
            "api_peak": max(api_samples) if api_samples else 0,
            "db_peak": max(db_samples) if db_samples else 0
        },
        "worker_calculation": result
    }

    return final_results

# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
if __name__ == "__main__":
    # This will be called by Claude with appropriate parameters
    print("This script should be imported and called by the worker assistant")
    print("It provides the analyze_workspaces() function")
