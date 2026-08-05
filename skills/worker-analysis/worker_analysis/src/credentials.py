#!/usr/bin/env python3
"""
Credentials Helper for Airbyte Cloud API

This module provides robust credential loading from multiple sources:
1. Environment variables (AIRBYTE_CLOUD_CLIENT_ID, AIRBYTE_CLOUD_CLIENT_SECRET)
2. ~/.env file
3. Project .env file
4. Direct parameters

Usage:
    from src.credentials import get_airbyte_credentials, ensure_credentials_loaded

    # Auto-load from all sources
    client_id, client_secret = get_airbyte_credentials()

    # Or ensure env vars are loaded first
    ensure_credentials_loaded()
    # Then use os.environ directly
"""

import os
from pathlib import Path
from typing import Optional, Tuple


# Common .env file locations to check
ENV_FILE_LOCATIONS = [
    Path.home() / ".env",                    # User home directory
    Path.cwd() / ".env",                     # Current working directory
    Path(__file__).parent.parent / ".env",   # Project root
]


def load_dotenv_file(filepath: Path) -> dict:
    """
    Load environment variables from a .env file.

    Args:
        filepath: Path to the .env file

    Returns:
        Dictionary of key-value pairs from the file
    """
    env_vars = {}

    if not filepath.exists():
        return env_vars

    try:
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()

                # Skip empty lines and comments
                if not line or line.startswith('#'):
                    continue

                # Handle export prefix
                if line.startswith('export '):
                    line = line[7:]

                # Split on first = only
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()

                    # Remove quotes if present
                    if (value.startswith('"') and value.endswith('"')) or \
                       (value.startswith("'") and value.endswith("'")):
                        value = value[1:-1]

                    env_vars[key] = value
    except Exception:
        pass

    return env_vars


def ensure_credentials_loaded() -> bool:
    """
    Ensure Airbyte credentials are loaded into os.environ.

    Checks multiple .env file locations and loads the first one found
    that contains the required credentials.

    Returns:
        True if credentials are available, False otherwise
    """
    # Check if already in environment
    if os.environ.get("AIRBYTE_CLOUD_CLIENT_ID") and os.environ.get("AIRBYTE_CLOUD_CLIENT_SECRET"):
        return True

    # Try loading from .env files
    for env_path in ENV_FILE_LOCATIONS:
        env_vars = load_dotenv_file(env_path)

        client_id = env_vars.get("AIRBYTE_CLOUD_CLIENT_ID")
        client_secret = env_vars.get("AIRBYTE_CLOUD_CLIENT_SECRET")

        if client_id and client_secret:
            # Load into environment
            os.environ["AIRBYTE_CLOUD_CLIENT_ID"] = client_id
            os.environ["AIRBYTE_CLOUD_CLIENT_SECRET"] = client_secret

            # Also load workspace ID if available
            if "AIRBYTE_CLOUD_WORKSPACE_ID" in env_vars:
                os.environ["AIRBYTE_CLOUD_WORKSPACE_ID"] = env_vars["AIRBYTE_CLOUD_WORKSPACE_ID"]

            return True

    return False


def get_airbyte_credentials(
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
) -> Tuple[str, str]:
    """
    Get Airbyte Cloud API credentials from multiple sources.

    Priority:
    1. Directly passed parameters
    2. Environment variables
    3. ~/.env file
    4. Project .env file

    Args:
        client_id: Optional client ID override
        client_secret: Optional client secret override

    Returns:
        Tuple of (client_id, client_secret)

    Raises:
        ValueError: If credentials cannot be found
    """
    # Use passed values if provided
    if client_id and client_secret:
        return client_id, client_secret

    # Try loading from .env files first
    ensure_credentials_loaded()

    # Get from environment
    client_id = client_id or os.environ.get("AIRBYTE_CLOUD_CLIENT_ID")
    client_secret = client_secret or os.environ.get("AIRBYTE_CLOUD_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise ValueError(
            "Airbyte Cloud credentials not found. Please either:\n"
            "1. Set AIRBYTE_CLOUD_CLIENT_ID and AIRBYTE_CLOUD_CLIENT_SECRET environment variables\n"
            "2. Create a ~/.env file with these variables\n"
            "3. Pass credentials directly to the function"
        )

    return client_id, client_secret


def get_workspace_id(workspace_id: Optional[str] = None) -> Optional[str]:
    """
    Get Airbyte Cloud workspace ID if available.

    Args:
        workspace_id: Optional workspace ID override

    Returns:
        Workspace ID or None if not configured
    """
    if workspace_id:
        return workspace_id

    ensure_credentials_loaded()
    return os.environ.get("AIRBYTE_CLOUD_WORKSPACE_ID")


def list_available_credentials() -> dict:
    """
    List all credential sources and their status (for debugging).

    Returns:
        Dictionary with credential source information
    """
    result = {
        "environment_variables": {
            "AIRBYTE_CLOUD_CLIENT_ID": bool(os.environ.get("AIRBYTE_CLOUD_CLIENT_ID")),
            "AIRBYTE_CLOUD_CLIENT_SECRET": bool(os.environ.get("AIRBYTE_CLOUD_CLIENT_SECRET")),
            "AIRBYTE_CLOUD_WORKSPACE_ID": bool(os.environ.get("AIRBYTE_CLOUD_WORKSPACE_ID")),
        },
        "env_files": {}
    }

    for env_path in ENV_FILE_LOCATIONS:
        env_vars = load_dotenv_file(env_path)
        result["env_files"][str(env_path)] = {
            "exists": env_path.exists(),
            "has_client_id": bool(env_vars.get("AIRBYTE_CLOUD_CLIENT_ID")),
            "has_client_secret": bool(env_vars.get("AIRBYTE_CLOUD_CLIENT_SECRET")),
            "has_workspace_id": bool(env_vars.get("AIRBYTE_CLOUD_WORKSPACE_ID")),
        }

    return result


if __name__ == "__main__":
    # Debug: Show credential sources
    import json
    print("Credential Sources:")
    print(json.dumps(list_available_credentials(), indent=2))

    print("\nAttempting to load credentials...")
    try:
        client_id, client_secret = get_airbyte_credentials()
        print(f"Success! Client ID: {client_id[:8]}...")
    except ValueError as e:
        print(f"Failed: {e}")
