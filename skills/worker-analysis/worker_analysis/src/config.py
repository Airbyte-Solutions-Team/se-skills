"""
Configuration and constants for the Airbyte Data Worker Assistant.
"""

import os
from pathlib import Path
from typing import Dict, Any

# --------------------------------------------------------------------------
# Auto-load .env file from project root
# --------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    # Load .env from the Worker project root directory
    _project_root = Path(__file__).parent.parent
    _env_file = _project_root / ".env"
    if _env_file.exists():
        load_dotenv(_env_file, override=False)  # Don't override existing env vars
except ImportError:
    pass  # python-dotenv not installed, skip auto-loading

# --------------------------------------------------------------------------
# Worker Capacity Rules (Universal Data Worker Model)
# --------------------------------------------------------------------------
# There is ONE worker model used by all plans (Pro, Enterprise, Flex, SME).
# Data Worker capacity is COMBINABLE:
# Formula: (API connections ÷ 5) + (DB connections ÷ 2) = workers used
# Example: 10 API + 4 DB = (10÷5) + (4÷2) = 2 + 2 = 4 workers
DB_CONNECTIONS_PER_WORKER = 2
API_CONNECTIONS_PER_WORKER = 5

PEAK_PERCENTILE = 99  # Use 99th percentile as peak (not max)

# --------------------------------------------------------------------------
# Worker Model Configuration
# --------------------------------------------------------------------------
WORKER_MODEL: Dict[str, Any] = {
    "name": "Data Worker",
    "worker_type": "Data Worker",
    "multiplier": 1,
    "db_per_worker": DB_CONNECTIONS_PER_WORKER,
    "api_per_worker": API_CONNECTIONS_PER_WORKER,
    "description": "Universal model - (API÷5) + (DB÷2) = workers"
}

DEFAULT_PLAN_TYPE = "pro"

# --------------------------------------------------------------------------
# Connector Classification Patterns
# --------------------------------------------------------------------------
# Database/File connectors (slower, more resource intensive)
# NOTE: File connectors count as database connectors for worker capacity (2 per worker)
DATABASE_CONNECTORS = [
    # Relational databases
    "postgres", "postgresql", "mysql", "mongodb", "mongo", "mssql", "sqlserver",
    "oracle", "db2", "mariadb", "cockroachdb",
    # Data warehouses
    "snowflake", "bigquery", "redshift", "databricks", "clickhouse",
    # File/Object storage
    "s3", "gcs", "azure-blob", "azure-blob-storage", "sftp", "ftp", "file", "local-file",
    # NoSQL/Document stores
    "dynamodb", "firestore", "elasticsearch", "opensearch",
    # Enterprise systems
    "sap-hana", "sap", "hana"
]

# API connectors (typically faster, less resource intensive)
API_CONNECTORS = [
    "stripe", "salesforce", "hubspot", "github", "gitlab", "slack",
    "google-analytics", "google-sheets", "facebook-marketing", "google-ads", "linkedin-ads",
    "shopify", "zendesk", "intercom", "jira", "confluence",
    "twilio", "sendgrid", "mailchimp", "asana", "notion",
    "airtable", "typeform", "surveymonkey", "square", "servicenow"
]

# --------------------------------------------------------------------------
# Analysis Defaults
# --------------------------------------------------------------------------
DEFAULT_ANALYSIS_DAYS = 30
DEFAULT_JOBS_LIMIT = 5  # Last 5 jobs per connection for fast analysis
DEFAULT_SYNC_DURATION_MINUTES = 30  # For estimation mode

# Time intervals for sync frequencies
SYNC_INTERVALS = {
    "sub_hourly": 15,    # minutes (15 is the common Airbyte sub-hourly cadence)
    "hourly": 60,        # minutes
    "daily": 1440        # minutes (24 hours)
}

# --------------------------------------------------------------------------
# Directory Paths
# --------------------------------------------------------------------------
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
ANALYSES_DIR = os.path.join(DATA_DIR, "analyses")
CUSTOMERS_DIR = os.path.join(DATA_DIR, "customers")
BENCHMARKS_DIR = os.path.join(DATA_DIR, "benchmarks")
TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")

# Ensure directories exist
for dir_path in [DATA_DIR, ANALYSES_DIR, CUSTOMERS_DIR, BENCHMARKS_DIR, TEMPLATES_DIR]:
    os.makedirs(dir_path, exist_ok=True)

# --------------------------------------------------------------------------
# Environment Variables
# --------------------------------------------------------------------------
def get_env_var(key: str, default: str = None) -> str:
    """Get environment variable with optional default."""
    value = os.getenv(key, default)
    if value is None:
        raise ValueError(f"Environment variable {key} is required but not set")
    return value

# Airbyte API credentials (optional - can be passed directly)
AIRBYTE_CLIENT_ID = os.getenv("AIRBYTE_CLOUD_CLIENT_ID", "")
AIRBYTE_CLIENT_SECRET = os.getenv("AIRBYTE_CLOUD_CLIENT_SECRET", "")
AIRBYTE_WORKSPACE_ID = os.getenv("AIRBYTE_CLOUD_WORKSPACE_ID", "")

# --------------------------------------------------------------------------
# Utility Functions
# --------------------------------------------------------------------------
def get_worker_model() -> Dict[str, Any]:
    """
    Get worker model configuration.

    All plans use the same universal worker model with combinable capacity:
    (API / 5) + (DB / 2) = workers used.

    Returns:
        Worker model configuration dictionary
    """
    return WORKER_MODEL


def is_database_connector(connector_name: str) -> bool:
    """
    Check if a connector is a database/file connector.

    Args:
        connector_name: Connector name (e.g., 'source-postgres')

    Returns:
        True if connector is database/file type, False otherwise
    """
    connector_lower = connector_name.lower()
    return any(db in connector_lower for db in DATABASE_CONNECTORS)


def is_api_connector(connector_name: str) -> bool:
    """
    Check if a connector is an API connector.

    Args:
        connector_name: Connector name (e.g., 'source-stripe')

    Returns:
        True if connector is API type, False otherwise
    """
    connector_lower = connector_name.lower()
    return any(api in connector_lower for api in API_CONNECTORS)


def classify_connector_by_name(connector_name: str) -> str:
    """
    Classify a connector as 'DATABASE' or 'API' based on name patterns.

    Args:
        connector_name: Connector name (e.g., 'source-postgres')

    Returns:
        'DATABASE', 'API', or 'UNKNOWN'
    """
    if is_database_connector(connector_name):
        return "DATABASE"
    elif is_api_connector(connector_name):
        return "API"
    else:
        return "UNKNOWN"


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------
def validate_workspace_id(workspace_id: str) -> bool:
    """Validate workspace ID format."""
    return bool(workspace_id and len(workspace_id) > 0)


def validate_percentage(value: float, name: str = "value") -> None:
    """
    Validate that a value is a valid percentage (0-100 or 0.0-1.0).

    Args:
        value: Value to validate
        name: Name of the parameter for error messages

    Raises:
        ValueError: If value is not a valid percentage
    """
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    if value > 100:
        raise ValueError(f"{name} cannot exceed 100")
