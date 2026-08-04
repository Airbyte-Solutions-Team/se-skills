"""
Connector classification utilities for determining if a connector is API or Database type.

Uses a combination of:
1. Name pattern matching (fast, local)
2. PyAirbyte MCP tools (accurate, requires MCP)
"""

from typing import Dict, Optional, Tuple
try:
    from src import config
except ImportError:
    import config


class ConnectorClassifier:
    """Classifies Airbyte connectors as API or DATABASE type."""

    def __init__(self):
        """Initialize the classifier with a cache for performance."""
        self._classification_cache: Dict[str, str] = {}

    def classify(self, connector_name: str, use_mcp: bool = True) -> str:
        """
        Classify a connector as 'API' or 'DATABASE'.

        Args:
            connector_name: Full connector name (e.g., 'source-postgres')
            use_mcp: Whether to attempt MCP-based classification

        Returns:
            'API', 'DATABASE', or 'UNKNOWN'
        """
        # Check cache first
        if connector_name in self._classification_cache:
            return self._classification_cache[connector_name]

        # Try name-based classification first (fast)
        classification = self._classify_by_name(connector_name)

        if classification != "UNKNOWN":
            self._classification_cache[connector_name] = classification
            return classification

        # If name-based fails and MCP is available, try MCP
        if use_mcp:
            mcp_classification = self._classify_by_mcp(connector_name)
            if mcp_classification != "UNKNOWN":
                self._classification_cache[connector_name] = mcp_classification
                return mcp_classification

        # Default to UNKNOWN
        self._classification_cache[connector_name] = "UNKNOWN"
        return "UNKNOWN"

    def _classify_by_name(self, connector_name: str) -> str:
        """
        Classify connector using name pattern matching.

        Args:
            connector_name: Connector name (e.g., 'source-postgres')

        Returns:
            'API', 'DATABASE', or 'UNKNOWN'
        """
        return config.classify_connector_by_name(connector_name)

    def _classify_by_mcp(self, connector_name: str) -> str:
        """
        Classify connector using PyAirbyte MCP tools.

        Uses the Airbyte registry to fetch connector metadata and infer type.

        Args:
            connector_name: Connector name (e.g., 'source-postgres')

        Returns:
            'API', 'DATABASE', or 'UNKNOWN'
        """
        try:
            # This will be called by Claude Code's MCP integration
            # Note: In standalone Python, this won't work - it requires Claude Code's MCP
            # For now, this is a placeholder that Claude Code can call

            # The logic would be:
            # 1. Get connector info from registry
            # 2. Analyze install type, documentation, and connector name
            # 3. Infer if it's API or DATABASE based on patterns

            # Database indicators:
            # - Install type: java (typically databases)
            # - Documentation contains: database, warehouse, storage, file
            # - Connector name contains: db, sql, nosql

            # API indicators:
            # - Install type: yaml (typically declarative API connectors)
            # - Documentation contains: api, rest, marketing, crm, saas
            # - Connector name patterns: marketing, ads, analytics

            # Since this needs to be called via Claude Code's MCP integration,
            # we return UNKNOWN here and let the orchestration layer handle it
            return "UNKNOWN"

        except Exception:
            # If MCP call fails, return UNKNOWN
            return "UNKNOWN"

    def classify_connection(
        self,
        source_connector: str,
        destination_connector: Optional[str] = None
    ) -> Tuple[str, Optional[str]]:
        """
        Classify both source and destination connectors.

        Args:
            source_connector: Source connector name
            destination_connector: Optional destination connector name

        Returns:
            Tuple of (source_type, destination_type)
            destination_type is None if destination_connector is None
        """
        source_type = self.classify(source_connector)

        if destination_connector:
            dest_type = self.classify(destination_connector)
            return source_type, dest_type

        return source_type, None

    def get_classification_summary(self, connectors: list) -> Dict[str, int]:
        """
        Get a summary of connector classifications.

        Args:
            connectors: List of connector names

        Returns:
            Dictionary with counts: {'API': 5, 'DATABASE': 3, 'UNKNOWN': 1}
        """
        summary = {"API": 0, "DATABASE": 0, "UNKNOWN": 0}

        for connector in connectors:
            classification = self.classify(connector)
            summary[classification] += 1

        return summary

    def clear_cache(self):
        """Clear the classification cache."""
        self._classification_cache.clear()


# Singleton instance for convenience
_classifier_instance = None


def get_classifier() -> ConnectorClassifier:
    """Get the singleton classifier instance."""
    global _classifier_instance
    if _classifier_instance is None:
        _classifier_instance = ConnectorClassifier()
    return _classifier_instance


# Convenience functions
def classify_connector(connector_name: str) -> str:
    """
    Classify a single connector.

    Args:
        connector_name: Connector name (e.g., 'source-postgres')

    Returns:
        'API', 'DATABASE', or 'UNKNOWN'
    """
    return get_classifier().classify(connector_name)


def classify_connection(
    source_connector: str,
    destination_connector: Optional[str] = None
) -> Tuple[str, Optional[str]]:
    """
    Classify a connection's connectors.

    Args:
        source_connector: Source connector name
        destination_connector: Optional destination connector name

    Returns:
        Tuple of (source_type, destination_type)
    """
    return get_classifier().classify_connection(source_connector, destination_connector)


# --------------------------------------------------------------------------
# Helper for extracting connector name from connection metadata
# --------------------------------------------------------------------------
def extract_source_connector_name(connection: Dict) -> Optional[str]:
    """
    Extract source connector name from connection metadata.

    Args:
        connection: Connection dictionary from Airbyte API

    Returns:
        Source connector name or None if not found
    """
    # Try different possible field names
    if "sourceDefinitionId" in connection:
        # This is an ID, we'd need to look it up
        # For now, try to extract from name
        pass

    if "sourceName" in connection:
        return connection["sourceName"]

    # Try to parse from connection name (e.g., "Postgres > Snowflake")
    if "name" in connection:
        name = connection["name"]
        if ">" in name or "→" in name or "to" in name.lower():
            # Try to extract source name
            parts = name.split(">") if ">" in name else name.split("→") if "→" in name else name.split("to")
            if len(parts) >= 1:
                source_name = parts[0].strip().lower()
                # Convert to connector format
                return f"source-{source_name.replace(' ', '-')}"

    return None


def extract_destination_connector_name(connection: Dict) -> Optional[str]:
    """
    Extract destination connector name from connection metadata.

    Args:
        connection: Connection dictionary from Airbyte API

    Returns:
        Destination connector name or None if not found
    """
    if "destinationName" in connection:
        return connection["destinationName"]

    # Try to parse from connection name (e.g., "Postgres > Snowflake")
    if "name" in connection:
        name = connection["name"]
        if ">" in name or "→" in name or "to" in name.lower():
            parts = name.split(">") if ">" in name else name.split("→") if "→" in name else name.split("to")
            if len(parts) >= 2:
                dest_name = parts[1].strip().lower()
                return f"destination-{dest_name.replace(' ', '-')}"

    return None
