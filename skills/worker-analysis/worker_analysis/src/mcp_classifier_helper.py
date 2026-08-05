"""
MCP-based Connector Classification Helper

This module provides utilities for Claude Code to use PyAirbyte MCP tools
to classify connectors during the /workers command execution.

NOTE: This module is designed to be used BY Claude Code during execution,
not by standalone Python scripts. The MCP tool calls are made by Claude Code's
MCP integration.
"""

from typing import Dict, List, Optional
try:
    from src import config
except ImportError:
    import config


def classify_connector_via_mcp(connector_name: str) -> str:
    """
    Classify a connector using PyAirbyte MCP tools.

    This function is meant to be called BY Claude Code, which will use:
    - mcp__pyairbyte__get_connector_info(connector_name)

    Classification logic:
    1. Try name-based classification first (fast)
    2. If UNKNOWN, use MCP to get connector metadata
    3. Infer type from:
       - Install type (yaml=API, java=DATABASE typically)
       - Documentation keywords
       - Connector name patterns

    Args:
        connector_name: Full connector name (e.g., 'source-postgres')

    Returns:
        'API', 'DATABASE', or 'UNKNOWN'
    """
    # First try fast name-based classification
    name_classification = config.classify_connector_by_name(connector_name)
    if name_classification != "UNKNOWN":
        return name_classification

    # If UNKNOWN, Claude Code should use MCP to get more info
    # This is a placeholder - Claude Code will implement the actual logic
    return "UNKNOWN"


def get_classification_hints(connector_info: Dict) -> str:
    """
    Analyze connector metadata to infer type.

    Claude Code will call mcp__pyairbyte__get_connector_info() and pass
    the result to this function for analysis.

    Args:
        connector_info: Connector metadata from MCP tool

    Returns:
        'API', 'DATABASE', or 'UNKNOWN'
    """
    # Database indicators (includes file/object storage)
    # NOTE: File connectors count as DATABASE for worker capacity calculation
    database_keywords = [
        'database', 'db', 'warehouse', 'storage', 'file', 'csv', 'json', 'parquet',
        'sql', 'nosql', 'bigquery', 'snowflake', 'redshift',
        'mongo', 'postgres', 'mysql', 'oracle', 's3', 'gcs', 'azure-blob',
        'sftp', 'ftp', 'object-storage'
    ]

    # API indicators
    api_keywords = [
        'api', 'rest', 'marketing', 'crm', 'saas', 'analytics',
        'ads', 'social', 'payment', 'commerce', 'ticketing',
        'messaging', 'collaboration', 'project-management'
    ]

    connector_name = connector_info.get('name', '').lower()
    docs_url = connector_info.get('documentationUrl', '').lower()

    # Check documentation URL and connector name
    database_score = sum(1 for kw in database_keywords if kw in connector_name or kw in docs_url)
    api_score = sum(1 for kw in api_keywords if kw in connector_name or kw in docs_url)

    if database_score > api_score:
        return 'DATABASE'
    elif api_score > database_score:
        return 'API'
    else:
        return 'UNKNOWN'


def build_classification_prompt(unknown_connectors: List[str]) -> str:
    """
    Build a prompt for Claude Code to classify unknown connectors using MCP.

    Args:
        unknown_connectors: List of connector names that couldn't be classified

    Returns:
        Prompt text for Claude Code to use MCP tools
    """
    if not unknown_connectors:
        return ""

    prompt = f"""
The following {len(unknown_connectors)} connector(s) could not be classified using name patterns:

{chr(10).join(f"- {conn}" for conn in unknown_connectors)}

Please use PyAirbyte MCP tools to classify these connectors:

1. For each connector, call: mcp__pyairbyte__get_connector_info(connector_name)
2. Analyze the metadata (documentationUrl, connector type, install type)
3. Classify as:
   - DATABASE: If it's a database, warehouse, file storage, or data lake connector
   - API: If it's a REST API, SaaS application, or marketing/analytics platform

Then update the classification results accordingly.
"""

    return prompt


def suggest_config_updates(classified_connectors: Dict[str, str]) -> str:
    """
    Generate suggestions for updating config.py with newly classified connectors.

    Args:
        classified_connectors: Dict mapping connector names to classifications

    Returns:
        Suggested additions to config.py
    """
    database_additions = []
    api_additions = []

    for connector_name, classification in classified_connectors.items():
        # Extract the key part of the connector name
        # e.g., 'source-square' -> 'square'
        key = connector_name.replace('source-', '').replace('destination-', '')

        if classification == 'DATABASE':
            database_additions.append(key)
        elif classification == 'API':
            api_additions.append(key)

    suggestions = []

    if database_additions:
        suggestions.append("\n# Suggested DATABASE_CONNECTORS additions:")
        suggestions.append("# " + ", ".join(f'"{conn}"' for conn in database_additions))

    if api_additions:
        suggestions.append("\n# Suggested API_CONNECTORS additions:")
        suggestions.append("# " + ", ".join(f'"{conn}"' for conn in api_additions))

    return "\n".join(suggestions) if suggestions else ""


# --------------------------------------------------------------------------
# Claude Code Integration Notes
# --------------------------------------------------------------------------
"""
When running the /workers command, Claude Code should:

1. After analyzing connections, collect all UNKNOWN connectors
2. For each UNKNOWN connector:
   a. Call mcp__pyairbyte__get_connector_info(connector_name)
   b. Pass the result to get_classification_hints()
   c. Update the classification in the results
3. Generate a report with accurate classifications
4. Optionally suggest config.py updates for future runs

Example workflow in /workers command:

```python
from mcp_classifier_helper import get_classification_hints, suggest_config_updates

# During analysis
unknown_connectors = [conn for conn, type in classifications.items() if type == 'UNKNOWN']

# Claude Code uses MCP tools
for connector_name in unknown_connectors:
    # Claude Code: mcp__pyairbyte__get_connector_info(connector_name)
    connector_info = {...}  # Result from MCP

    # Analyze metadata
    classification = get_classification_hints(connector_info)

    # Update results
    classifications[connector_name] = classification

# Suggest config updates
suggestions = suggest_config_updates(classifications)
```
"""
