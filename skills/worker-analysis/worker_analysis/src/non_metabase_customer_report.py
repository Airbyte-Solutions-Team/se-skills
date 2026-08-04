#!/usr/bin/env python3
"""
Non-Metabase Customer Worker Analysis Report Generator

Use this template for customers who are NOT on the Pro/Enterprise plan and therefore
don't have worker usage data in Metabase. This includes customers on:
- Cloud Sales Assist
- Free tier
- Trial accounts
- Any other plan without Metabase worker tracking

This script uses the Airbyte Cloud API (via PyAirbyte MCP tools) to:
1. Fetch all connections in the workspace
2. Fetch job history for the last N days
3. Classify connections as API or DATABASE
4. Calculate worker requirements using the Pro plan formula
5. Analyze job overlaps and peak concurrency
6. Generate a branded PDF report

Usage (from Claude Code with MCP tools):
    1. Set the CUSTOMER_CONFIG below with the customer's details
    2. Run this script - it will guide you through the MCP tool calls needed
    3. After fetching data, run again to generate the report

Alternatively, use the generate_report() function directly with pre-fetched data.
"""

import os
import sys
import json
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Any, Optional, Tuple
from pathlib import Path

# =============================================================================
# CUSTOMER CONFIGURATION - Edit this section for each customer
# =============================================================================

CUSTOMER_CONFIG = {
    # Customer/Account name (used in report title)
    "customer_name": "CUSTOMER_NAME",

    # Organization ID from Airbyte Cloud
    # Find this in the Airbyte Cloud URL or via API
    "org_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx",

    # Workspace ID(s) - if known, otherwise leave empty to fetch all
    "workspace_ids": [],

    # Number of contracted workers (check Salesforce or contract)
    "contracted_workers": 1,

    # Current plan type (for reference in report)
    "account_type": "Cloud Sales Assist",

    # Number of days of job history to analyze
    "analysis_days": 10,

    # Output directory for reports
    "output_dir": "",  # Will be set automatically if empty
}

# =============================================================================
# CONNECTOR CLASSIFICATION
# =============================================================================

# Database/File connectors (2 per worker)
DATABASE_CONNECTORS = [
    "postgres", "postgresql", "mysql", "mongodb", "mongo", "mssql", "sqlserver",
    "sql-server", "oracle", "db2", "mariadb", "cockroachdb",
    "snowflake", "bigquery", "redshift", "databricks", "clickhouse",
    "s3", "gcs", "azure-blob", "azure-blob-storage", "sftp", "ftp", "file",
    "dynamodb", "firestore", "elasticsearch", "opensearch",
    "sap-hana", "sap", "hana"
]

# API connectors (5 per worker)
API_CONNECTORS = [
    "stripe", "salesforce", "hubspot", "github", "gitlab", "slack",
    "google-analytics", "facebook-marketing", "google-ads", "linkedin-ads",
    "shopify", "zendesk", "intercom", "jira", "confluence",
    "twilio", "sendgrid", "mailchimp", "asana", "notion",
    "airtable", "typeform", "surveymonkey", "square", "servicenow",
    "axiom", "amplitude", "mixpanel", "segment", "braze"
]


def classify_connector(source_name: str) -> str:
    """Classify a connector as DATABASE or API based on name patterns."""
    name_lower = source_name.lower()

    for db in DATABASE_CONNECTORS:
        if db in name_lower:
            return "DATABASE"

    for api in API_CONNECTORS:
        if api in name_lower:
            return "API"

    # Default to API for unknown (lighter weight assumption)
    return "API"


# =============================================================================
# DATA STRUCTURES
# =============================================================================

@dataclass
class Connection:
    """Represents an Airbyte connection."""
    id: str
    name: str
    source_name: str
    source_type: str  # The actual connector type
    destination_name: str
    status: str
    schedule_type: str
    schedule_data: str
    classification: str  # API or DATABASE
    is_active: bool
    jobs: List[Dict] = field(default_factory=list)
    avg_duration_minutes: float = 0
    avg_records: int = 0
    avg_bytes: int = 0


@dataclass
class AnalysisResult:
    """Results from the worker analysis."""
    customer_name: str
    org_id: str
    analysis_date: datetime
    analysis_days: int
    contracted_workers: int
    account_type: str

    # Connection counts
    total_connections: int = 0
    active_connections: int = 0
    api_connections: int = 0
    db_connections: int = 0

    # Worker calculations
    api_workers: float = 0.0
    db_workers: float = 0.0
    total_workers_needed: float = 0.0
    utilization_pct: float = 0.0

    # Job analysis
    total_jobs_analyzed: int = 0
    peak_concurrent_workers: float = 0.0
    peak_time: Optional[datetime] = None

    # Hourly patterns
    hourly_stats: Dict[int, Dict] = field(default_factory=dict)

    # Connection details
    connections: List[Connection] = field(default_factory=list)


# =============================================================================
# MCP DATA FETCHING HELPERS
# =============================================================================

def format_mcp_instructions(config: Dict) -> str:
    """
    Generate instructions for fetching data via MCP tools.

    This is displayed when running the script without pre-fetched data.
    """
    org_id = config.get("org_id", "")
    workspace_ids = config.get("workspace_ids", [])
    days = config.get("analysis_days", 10)

    instructions = f"""
================================================================================
                    MCP DATA FETCHING INSTRUCTIONS
================================================================================

To generate the worker analysis report, you need to fetch data using the
PyAirbyte MCP tools. Follow these steps:

STEP 1: List Connections
------------------------
Use: mcp__pyairbyte__list_deployed_cloud_connections

This will return all connections in the workspace. Note the connection IDs.


STEP 2: Fetch Job History (for each active connection)
------------------------------------------------------
For each connection ID, use: mcp__pyairbyte__list_cloud_sync_jobs

Parameters:
- connection_id: <the connection ID>
- limit: 50 (to get last {days}+ days of jobs)

Collect jobs for ALL active connections.


STEP 3: Run This Script with Data
---------------------------------
Once you have the connection and job data, you can either:

A) Update the CONNECTION_DATA and JOB_DATA dictionaries in this script, OR

B) Call the generate_report() function directly:

   from non_metabase_customer_report import generate_report

   result = generate_report(
       connections=connections_list,  # List of connection dicts
       jobs_by_connection=jobs_dict,  # Dict mapping conn_id to job list
       config=CUSTOMER_CONFIG
   )


CUSTOMER CONFIGURATION:
-----------------------
Customer Name: {config.get('customer_name', 'Not Set')}
Organization ID: {org_id or 'Not Set'}
Workspace IDs: {workspace_ids or 'Will fetch all'}
Contracted Workers: {config.get('contracted_workers', 1)}
Plan Type: {config.get('account_type', 'Unknown')}
Analysis Days: {days}

================================================================================
"""
    return instructions


# =============================================================================
# PRE-FETCHED DATA TEMPLATE
# =============================================================================

# Populate this with data from MCP tools, or pass directly to generate_report()
CONNECTION_DATA = [
    # Example format - replace with actual data from mcp__pyairbyte__list_deployed_cloud_connections
    # {
    #     "connectionId": "uuid-here",
    #     "name": "Connection Name",
    #     "source": {"name": "Source Name", "sourceName": "Salesforce"},
    #     "destination": {"name": "Destination Name"},
    #     "status": "active",
    #     "scheduleType": "cron",
    #     "scheduleData": {"cron": {"cronExpression": "0 0 * * * ?"}}
    # }
]

JOB_DATA = {
    # Example format - replace with actual data from mcp__pyairbyte__list_cloud_sync_jobs
    # "connection-uuid": [
    #     {
    #         "jobId": 123,
    #         "status": "succeeded",
    #         "startTime": "2026-02-01T08:00:00Z",
    #         "endTime": "2026-02-01T08:30:00Z",
    #         "rowsSynced": 10000,
    #         "bytesSynced": 5000000
    #     }
    # ]
}


# =============================================================================
# ANALYSIS FUNCTIONS
# =============================================================================

def parse_connections(raw_connections: List[Dict]) -> List[Connection]:
    """Parse raw connection data from MCP into Connection objects."""
    connections = []

    for conn in raw_connections:
        conn_id = conn.get("connectionId") or conn.get("id", "")
        name = conn.get("name", "Unknown")

        # Extract source info
        source = conn.get("source", {})
        if isinstance(source, dict):
            source_name = source.get("name", "")
            source_type = source.get("sourceName", "") or source.get("sourceDefinitionName", "")
        else:
            source_name = str(source)
            source_type = source_name

        # Extract destination info
        dest = conn.get("destination", {})
        dest_name = dest.get("name", "") if isinstance(dest, dict) else str(dest)

        # Get status
        status = conn.get("status", "unknown")
        is_active = status.lower() in ["active", "healthy", "running"]

        # Get schedule info
        schedule_type = conn.get("scheduleType", "manual")
        schedule_data = ""
        if "scheduleData" in conn:
            sd = conn["scheduleData"]
            if isinstance(sd, dict) and "cron" in sd:
                schedule_data = sd["cron"].get("cronExpression", "")
            elif isinstance(sd, str):
                schedule_data = sd

        # Classify connector
        classification = classify_connector(source_type or source_name or name)

        connections.append(Connection(
            id=conn_id,
            name=name,
            source_name=source_name,
            source_type=source_type,
            destination_name=dest_name,
            status=status,
            schedule_type=schedule_type,
            schedule_data=schedule_data,
            classification=classification,
            is_active=is_active,
        ))

    return connections


def analyze_jobs(connections: List[Connection], jobs_by_connection: Dict[str, List[Dict]], days: int = 10) -> None:
    """Analyze job history and add stats to connections."""
    cutoff = datetime.utcnow() - timedelta(days=days)

    for conn in connections:
        jobs = jobs_by_connection.get(conn.id, [])
        valid_jobs = []

        for job in jobs:
            # Parse start time
            start_str = job.get("startTime") or job.get("createdAt", "")
            if not start_str:
                continue

            try:
                start_time = datetime.fromisoformat(start_str.replace("Z", "+00:00").replace("+00:00", ""))
            except:
                continue

            if start_time < cutoff:
                continue

            valid_jobs.append({
                "job_id": job.get("jobId") or job.get("id"),
                "status": job.get("status", "unknown"),
                "start_time": start_time,
                "end_time": None,
                "duration_minutes": 0,
                "records": job.get("rowsSynced") or job.get("recordsSynced", 0),
                "bytes": job.get("bytesSynced", 0),
            })

            # Parse end time and calculate duration
            end_str = job.get("endTime") or job.get("updatedAt", "")
            if end_str:
                try:
                    end_time = datetime.fromisoformat(end_str.replace("Z", "+00:00").replace("+00:00", ""))
                    valid_jobs[-1]["end_time"] = end_time
                    valid_jobs[-1]["duration_minutes"] = (end_time - start_time).total_seconds() / 60
                except:
                    pass

        conn.jobs = valid_jobs

        # Calculate averages
        if valid_jobs:
            durations = [j["duration_minutes"] for j in valid_jobs if j["duration_minutes"] > 0]
            records = [j["records"] for j in valid_jobs if j["records"]]
            bytes_list = [j["bytes"] for j in valid_jobs if j["bytes"]]

            conn.avg_duration_minutes = sum(durations) / len(durations) if durations else 0
            conn.avg_records = int(sum(records) / len(records)) if records else 0
            conn.avg_bytes = int(sum(bytes_list) / len(bytes_list)) if bytes_list else 0


def calculate_hourly_patterns(connections: List[Connection]) -> Dict[int, Dict]:
    """
    Calculate job patterns by hour of day using ACTUAL JOB OVERLAP analysis.

    This is the CORRECT method:
        For each minute of each hour, count how many jobs are RUNNING (start <= time < end).
        Workers = (Peak Concurrent API / 5) + (Peak Concurrent DB / 2)

    NOT the wrong method of counting job starts and estimating.
    """
    # Build list of job intervals with their types
    job_intervals = []
    for conn in connections:
        if not conn.is_active:
            continue

        for job in conn.jobs:
            start_time = job.get("start_time")
            end_time = job.get("end_time")

            if not start_time:
                continue

            # If no end time, estimate from duration or default 5 minutes
            if not end_time:
                duration = job.get("duration_minutes", 5)
                end_time = start_time + timedelta(minutes=duration)

            job_intervals.append({
                "start": start_time,
                "end": end_time,
                "type": conn.classification,  # "API" or "DATABASE"
                "connection_id": conn.id
            })

    # Determine analysis date (most recent date in jobs)
    if not job_intervals:
        # Return empty hourly stats
        return {hour: {
            "api_jobs": 0, "db_jobs": 0, "total_jobs": 0,
            "api_concurrent": 0, "db_concurrent": 0,
            "unique_connections": 0, "workers_needed": 0.0
        } for hour in range(24)}

    analysis_date = max(j["start"] for j in job_intervals).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    # For each hour, find ACTUAL peak concurrency by checking every minute
    result = {}
    for hour in range(24):
        max_api_concurrent = 0
        max_db_concurrent = 0
        api_jobs_started = 0
        db_jobs_started = 0
        connections_in_hour = set()

        # Check every minute in this hour for concurrent jobs
        for minute in range(60):
            check_time = analysis_date.replace(hour=hour, minute=minute, second=0)

            api_concurrent = 0
            db_concurrent = 0

            for interval in job_intervals:
                # Job is running if: start_time <= check_time < end_time
                if interval["start"] <= check_time < interval["end"]:
                    if interval["type"] == "API":
                        api_concurrent += 1
                    else:
                        db_concurrent += 1

            max_api_concurrent = max(max_api_concurrent, api_concurrent)
            max_db_concurrent = max(max_db_concurrent, db_concurrent)

        # Count jobs that STARTED in this hour (for reference)
        for interval in job_intervals:
            if (interval["start"].date() == analysis_date.date() and
                interval["start"].hour == hour):
                connections_in_hour.add(interval["connection_id"])
                if interval["type"] == "API":
                    api_jobs_started += 1
                else:
                    db_jobs_started += 1

        # Calculate workers for this hour's PEAK concurrency
        # This is the CORRECT formula based on actual overlaps
        workers_needed = (max_api_concurrent / 5.0) + (max_db_concurrent / 2.0)

        result[hour] = {
            "api_jobs": api_jobs_started,
            "db_jobs": db_jobs_started,
            "total_jobs": api_jobs_started + db_jobs_started,
            "api_concurrent": max_api_concurrent,  # ACTUAL peak, not estimate
            "db_concurrent": max_db_concurrent,    # ACTUAL peak, not estimate
            "unique_connections": len(connections_in_hour),
            "workers_needed": workers_needed,
            # Keep old key for backwards compatibility
            "estimated_workers": workers_needed,
        }

    return result


def run_analysis(connections: List[Connection], jobs_by_connection: Dict[str, List[Dict]], config: Dict) -> AnalysisResult:
    """
    Run the full worker analysis using ACTUAL JOB OVERLAP methodology.

    IMPORTANT: Workers are calculated based on ACTUAL peak concurrent jobs,
    NOT total connection counts. Total connection counts are kept for reference only.
    """
    # Analyze jobs
    analyze_jobs(connections, jobs_by_connection, config.get("analysis_days", 10))

    # Calculate hourly patterns using ACTUAL JOB OVERLAP analysis
    hourly_stats = calculate_hourly_patterns(connections)

    # Count connections by type (for reference, NOT for worker calculation)
    active_connections = [c for c in connections if c.is_active]
    api_connections = [c for c in active_connections if c.classification == "API"]
    db_connections = [c for c in active_connections if c.classification == "DATABASE"]

    # Find peak hour based on ACTUAL concurrent workers (from job overlaps)
    peak_hour = max(hourly_stats.keys(), key=lambda h: hourly_stats[h].get("workers_needed", 0))
    peak_stats = hourly_stats[peak_hour]

    # Get ACTUAL peak concurrent jobs from job overlap analysis
    peak_api_concurrent = peak_stats.get("api_concurrent", 0)
    peak_db_concurrent = peak_stats.get("db_concurrent", 0)

    # Calculate workers using ACTUAL PEAK CONCURRENCY (correct method)
    # NOT total connection counts (wrong method)
    api_workers = peak_api_concurrent / 5.0
    db_workers = peak_db_concurrent / 2.0
    total_workers = api_workers + db_workers

    contracted = config.get("contracted_workers", 1)
    utilization = (total_workers / contracted * 100) if contracted > 0 else 0

    # Count total jobs
    total_jobs = sum(len(c.jobs) for c in connections)

    return AnalysisResult(
        customer_name=config.get("customer_name", "Customer"),
        org_id=config.get("org_id", ""),
        analysis_date=datetime.utcnow(),
        analysis_days=config.get("analysis_days", 10),
        contracted_workers=contracted,
        account_type=config.get("account_type", "Unknown"),
        total_connections=len(connections),
        active_connections=len(active_connections),
        # Connection counts for reference (NOT used for worker calc)
        api_connections=len(api_connections),
        db_connections=len(db_connections),
        # Worker calculation based on ACTUAL peak concurrent jobs
        api_workers=api_workers,
        db_workers=db_workers,
        total_workers_needed=total_workers,
        utilization_pct=utilization,
        total_jobs_analyzed=total_jobs,
        peak_concurrent_workers=total_workers,
        peak_time=datetime.utcnow().replace(hour=peak_hour, minute=0, second=0),
        hourly_stats=hourly_stats,
        connections=connections,
    )


# =============================================================================
# PDF REPORT GENERATION
# =============================================================================

def generate_pdf_report(result: AnalysisResult, output_dir: str) -> str:
    """Generate PDF report with Airbyte branding."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    from reportlab.lib.enums import TA_CENTER, TA_LEFT
    from src.report_branding import get_divider_line, get_logo_header_elements, make_branded_footer
    from src.report_tables import create_wrapped_table

    # Airbyte Brand Colors
    AIRBYTE_BLUE_100 = colors.HexColor('#F5F5FF')
    AIRBYTE_BLUE_700 = colors.HexColor('#5F5CFF')
    AIRBYTE_INDIGO_500 = colors.HexColor('#282B5C')
    AIRBYTE_INDIGO_700 = colors.HexColor('#1A194D')
    AIRBYTE_INDIGO_800 = colors.HexColor('#0D0D37')
    AIRBYTE_PEACH_100 = colors.HexColor('#FAEBEA')
    AIRBYTE_PEACH_150 = colors.HexColor('#FFE6E0')
    AIRBYTE_PEACH_700 = colors.HexColor('#FF694A')
    AIRBYTE_COOL_50 = colors.HexColor('#F9F9FB')
    AIRBYTE_COOL_200 = colors.HexColor('#D4D4E3')
    AIRBYTE_COOL_500 = colors.HexColor('#8487A4')
    AIRBYTE_NEUTRAL_800 = colors.HexColor('#222222')
    AIRBYTE_GREEN_100 = colors.HexColor('#E8F5E9')
    AIRBYTE_GREEN_600 = colors.HexColor('#43A047')

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Generate filename
    safe_name = "".join(c if c.isalnum() else "_" for c in result.customer_name)
    date_str = result.analysis_date.strftime('%Y%m%d')
    output_path = os.path.join(output_dir, f"{safe_name}_Worker_Analysis_{date_str}.pdf")

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=0.6*inch,
        leftMargin=0.6*inch,
        topMargin=0.6*inch,
        bottomMargin=0.6*inch
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'],
        fontSize=22, spaceAfter=20, alignment=TA_CENTER,
        textColor=AIRBYTE_INDIGO_800
    )

    subtitle_style = ParagraphStyle(
        'Subtitle', parent=styles['Heading2'],
        fontSize=16, alignment=TA_CENTER, spaceAfter=25,
        textColor=AIRBYTE_INDIGO_500
    )

    heading_style = ParagraphStyle(
        'SectionHeader', parent=styles['Heading2'],
        fontSize=14, spaceBefore=15, spaceAfter=10,
        textColor=AIRBYTE_INDIGO_500
    )

    normal_style = ParagraphStyle(
        'BodyText', parent=styles['Normal'],
        fontSize=10, spaceAfter=8, leading=13,
        textColor=AIRBYTE_NEUTRAL_800
    )

    elements = []

    # Airbyte logo header
    elements.extend(get_logo_header_elements())
    elements.append(Spacer(1, 4))

    # Title
    elements.append(Paragraph(result.customer_name, title_style))
    elements.append(Paragraph("Worker Utilization Analysis", subtitle_style))

    # Colored divider line
    elements.append(get_divider_line())
    elements.append(Spacer(1, 12))

    elements.append(Paragraph(f"Report Date: {result.analysis_date.strftime('%Y-%m-%d')}", normal_style))
    elements.append(Paragraph(f"Organization ID: {result.org_id}", normal_style))
    elements.append(Paragraph(f"Plan Type: {result.account_type}", normal_style))
    elements.append(Spacer(1, 15))

    # Status Box
    status_label = "OVER-UTILIZED" if result.utilization_pct > 100 else ("NEAR CAPACITY" if result.utilization_pct >= 85 else "OK")
    status_bg = AIRBYTE_PEACH_150 if result.utilization_pct > 100 else (AIRBYTE_PEACH_100 if result.utilization_pct >= 85 else AIRBYTE_BLUE_100)

    status_text = f"""
    <b>Current Status: {status_label}</b><br/>
    Workers Needed: <b>{result.total_workers_needed:.1f}</b> / Contracted: <b>{result.contracted_workers}</b> workers<br/>
    Utilization: <b>{result.utilization_pct:.0f}%</b>
    """
    elements.append(Paragraph(status_text, ParagraphStyle(
        'StatusBox', parent=normal_style,
        backColor=status_bg, borderPadding=12, alignment=TA_CENTER
    )))
    elements.append(Spacer(1, 15))

    # Executive Summary Table
    elements.append(Paragraph("Executive Summary", heading_style))

    # Get peak info from hourly stats
    peak_hour = result.peak_time.hour if result.peak_time else 0
    peak_stats = result.hourly_stats.get(peak_hour, {})
    peak_api_concurrent = peak_stats.get("api_concurrent", 0)
    peak_db_concurrent = peak_stats.get("db_concurrent", 0)

    summary_data = [
        ["Metric", "Value"],
        ["Contracted Workers", str(result.contracted_workers)],
        ["P99 Workers Needed", f"{result.total_workers_needed:.1f}"],
        ["Utilization", f"{result.utilization_pct:.0f}%"],
        ["Status", status_label],
        ["Peak Hour (UTC)", f"{peak_hour:02d}:00"],
        ["Peak Concurrent API", str(peak_api_concurrent)],
        ["Peak Concurrent DB", str(peak_db_concurrent)],
        ["Total Connections", str(result.total_connections)],
        ["Active Connections", str(result.active_connections)],
        ["Jobs Analyzed", str(result.total_jobs_analyzed)],
    ]

    summary_table = create_wrapped_table(summary_data, col_widths=[3*inch, 2.5*inch], font_size=10)
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, AIRBYTE_COOL_50]),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 20))

    # Worker Calculation - Based on ACTUAL Job Overlaps
    # Get peak concurrent jobs from hourly stats
    peak_hour = result.peak_time.hour if result.peak_time else 0
    peak_stats = result.hourly_stats.get(peak_hour, {})
    peak_api_concurrent = peak_stats.get("api_concurrent", 0)
    peak_db_concurrent = peak_stats.get("db_concurrent", 0)

    calc_section = [
        Paragraph("Worker Calculation (Based on Actual Job Overlaps)", heading_style),
        Paragraph(
            "<b>Method:</b> Analyze when jobs actually run, find peak concurrent jobs, then calculate workers.<br/>"
            "<b>Formula:</b> Workers = (Peak Concurrent API ÷ 5) + (Peak Concurrent DB ÷ 2)",
            normal_style
        ),
        Spacer(1, 10),
    ]

    calc_data = [
        ["Connection Type", "Total Count", "Peak Concurrent", "Worker Factor", "Workers Used"],
        ["API Connections", str(result.api_connections), str(peak_api_concurrent), "÷ 5", f"{result.api_workers:.1f}"],
        ["Database Connections", str(result.db_connections), str(peak_db_concurrent), "÷ 2", f"{result.db_workers:.1f}"],
        ["TOTAL", "", str(peak_api_concurrent + peak_db_concurrent), "", f"{result.total_workers_needed:.1f}"],
    ]

    calc_table = create_wrapped_table(
        calc_data,
        col_widths=[1.6*inch, 1*inch, 1.2*inch, 1*inch, 1.2*inch],
        font_size=10,
        bold_rows={-1},
    )
    calc_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_GREEN_600),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (1, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, -1), (-1, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('BACKGROUND', (0, -1), (-1, -1), AIRBYTE_GREEN_100),
    ]))
    calc_section.append(calc_table)
    elements.append(KeepTogether(calc_section))
    elements.append(Spacer(1, 20))

    # Connection Inventory
    elements.append(Paragraph("Connection Inventory", heading_style))

    conn_data = [["Connection Name", "Source", "Type", "Status", "Avg Duration"]]
    for conn in result.connections:
        conn_data.append([
            conn.name[:30] + "..." if len(conn.name) > 30 else conn.name,
            conn.source_type[:15] if conn.source_type else conn.source_name[:15],
            conn.classification,
            "Active" if conn.is_active else conn.status,
            f"{conn.avg_duration_minutes:.0f} min" if conn.avg_duration_minutes else "-"
        ])

    conn_table = create_wrapped_table(conn_data, col_widths=[2.2*inch, 1.3*inch, 0.8*inch, 0.8*inch, 1*inch], font_size=9)
    conn_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, AIRBYTE_COOL_50]),
    ]))
    elements.append(conn_table)
    elements.append(PageBreak())

    # Hourly Analysis
    elements.append(Paragraph("Hourly Job Distribution (UTC)", heading_style))
    elements.append(Paragraph(
        "Shows job activity by hour. <font color='#FF694A'><b>PEACH = High activity</b></font>, "
        "<font color='#43A047'><b>GREEN = Low activity</b></font>",
        normal_style
    ))
    elements.append(Spacer(1, 10))

    hourly_data = [["Hour", "API Peak", "DB Peak", "Total Peak", "Workers"]]
    for hour in range(24):
        stats = result.hourly_stats.get(hour, {
            "api_concurrent": 0, "db_concurrent": 0, "workers_needed": 0
        })
        api_peak = stats.get("api_concurrent", 0)
        db_peak = stats.get("db_concurrent", 0)
        workers = stats.get("workers_needed", 0)
        hourly_data.append([
            f"{hour:02d}:00",
            str(api_peak),
            str(db_peak),
            str(api_peak + db_peak),
            f"{workers:.1f}"
        ])

    hourly_table = create_wrapped_table(hourly_data, col_widths=[0.8*inch, 0.8*inch, 0.8*inch, 0.8*inch, 1.2*inch], font_size=8)

    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
    ]

    for i, row in enumerate(hourly_data[1:], start=1):
        workers = float(row[4])  # Workers column
        if workers >= result.contracted_workers:  # Over contracted
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_PEACH_100))
        elif workers >= result.contracted_workers * 0.7:  # Near capacity
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_PEACH_100))
        elif workers > 0:
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_COOL_50))
        else:
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_GREEN_100))

    hourly_table.setStyle(TableStyle(table_style))
    elements.append(hourly_table)
    elements.append(Spacer(1, 20))

    # Recommendations
    elements.append(Paragraph("Recommendations", heading_style))

    recommendations = []
    if result.utilization_pct > 100:
        additional = int(result.total_workers_needed - result.contracted_workers) + 1
        recommendations.append(f"<b>1. Purchase {additional} additional worker(s)</b> to handle current load")

    if result.db_connections >= 2:
        recommendations.append("<b>2. Stagger database syncs</b> to reduce concurrent DB load")

    if not recommendations:
        recommendations.append("<b>Current configuration appears optimal.</b> Monitor as connections are added.")

    for rec in recommendations:
        elements.append(Paragraph(rec, normal_style))
        elements.append(Spacer(1, 6))

    # Footer
    elements.append(Spacer(1, 30))
    footer_text = f"""
    <i>Report generated on {result.analysis_date.strftime('%Y-%m-%d at %H:%M:%S UTC')}.<br/>
    Analysis based on {result.analysis_days} days of job history.<br/>
    Worker calculations use Pro plan formula. Actual billing may vary by plan.</i>
    """
    elements.append(Paragraph(footer_text, ParagraphStyle(
        'Footer', parent=normal_style, fontSize=8, textColor=AIRBYTE_COOL_500, alignment=TA_CENTER
    )))

    # Build with branded footer
    footer_fn = make_branded_footer(
        result.customer_name,
        result.analysis_date.strftime('%Y-%m-%d'),
    )
    doc.build(elements, onFirstPage=footer_fn, onLaterPages=footer_fn)
    return output_path


# =============================================================================
# MAIN ENTRY POINTS
# =============================================================================

def generate_report(
    connections: List[Dict],
    jobs_by_connection: Dict[str, List[Dict]],
    config: Dict = None
) -> Dict[str, Any]:
    """
    Generate a worker analysis report from connection and job data.

    This is the main entry point for programmatic use.

    Args:
        connections: List of connection dicts from Airbyte API
        jobs_by_connection: Dict mapping connection_id to list of job dicts
        config: Customer configuration (uses CUSTOMER_CONFIG if not provided)

    Returns:
        Dict with report path and analysis results
    """
    if config is None:
        config = CUSTOMER_CONFIG

    # Parse connections
    parsed_connections = parse_connections(connections)

    # Run analysis
    result = run_analysis(parsed_connections, jobs_by_connection, config)

    # Determine output directory
    output_dir = config.get("output_dir")
    if not output_dir:
        base_dir = Path(__file__).parent.parent / "FinalReports"
        safe_name = "".join(c if c.isalnum() else "_" for c in config.get("customer_name", "Customer"))
        output_dir = str(base_dir / safe_name)

    # Generate PDF
    pdf_path = generate_pdf_report(result, output_dir)

    # Get peak info
    peak_hour = result.peak_time.hour if result.peak_time else 0
    peak_stats = result.hourly_stats.get(peak_hour, {})
    peak_api = peak_stats.get("api_concurrent", 0)
    peak_db = peak_stats.get("db_concurrent", 0)

    # Print summary
    print(f"""
================================================================================
                    WORKER ANALYSIS COMPLETE
================================================================================
Customer: {result.customer_name}
Organization ID: {result.org_id}
Plan Type: {result.account_type}

ANALYSIS METHOD: Job Overlap (Actual Concurrent Jobs)

WORKER USAGE (Based on Actual Peak Concurrency):
  P99 Workers Needed: {result.total_workers_needed:.1f}
  Contracted Workers: {result.contracted_workers}
  Utilization:        {result.utilization_pct:.0f}%
  Status:             {"OVER-UTILIZED" if result.utilization_pct > 100 else "OK"}

PEAK HOUR ({peak_hour:02d}:00 UTC):
  Peak Concurrent API: {peak_api} (÷5 = {result.api_workers:.1f} workers)
  Peak Concurrent DB:  {peak_db} (÷2 = {result.db_workers:.1f} workers)
  Total Concurrent:    {peak_api + peak_db}

CONNECTIONS (Total, not used for worker calc):
  Total: {result.total_connections}
  Active: {result.active_connections}
    - API: {result.api_connections}
    - Database: {result.db_connections}

REPORT SAVED: {pdf_path}
================================================================================
""")

    return {
        "success": True,
        "pdf_path": pdf_path,
        "customer_name": result.customer_name,
        "workers_needed": result.total_workers_needed,
        "utilization_pct": result.utilization_pct,
        "api_connections": result.api_connections,
        "db_connections": result.db_connections,
    }


def main():
    """Main entry point for CLI usage."""
    # Check if we have data
    if not CONNECTION_DATA:
        print(format_mcp_instructions(CUSTOMER_CONFIG))
        print("\nNo connection data found. Please fetch data using MCP tools first.")
        print("Update CONNECTION_DATA and JOB_DATA in this script, then run again.")
        return

    # Generate report
    result = generate_report(
        connections=CONNECTION_DATA,
        jobs_by_connection=JOB_DATA,
        config=CUSTOMER_CONFIG
    )

    if result.get("success"):
        print(f"\nReport generated successfully: {result['pdf_path']}")
    else:
        print(f"\nError generating report: {result.get('error', 'Unknown error')}")


if __name__ == "__main__":
    main()
