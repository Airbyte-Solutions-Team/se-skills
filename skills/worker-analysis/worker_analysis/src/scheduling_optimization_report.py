#!/usr/bin/env python3
"""
Scheduling Optimization Report Generator

Generates PDF reports for over-utilized customers that help AEs recommend
specific connection rescheduling to reduce peak worker usage.

Key Features:
- Shows connections running during peak hours
- Identifies low-traffic time windows
- Provides ready-to-use Quartz cron expressions (6-value format)
- Estimates potential P99 reduction from rescheduling

Usage:
    from src.scheduling_optimization_report import generate_scheduling_optimization_report

    result = generate_scheduling_optimization_report(
        customer_name="<Customer>",
        summary_data=summary,
        hourly_data=hourly,
        connection_data=connections,
        output_dir="<output-dir>"
    )
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    PageBreak, KeepTogether
)
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from src.report_branding import get_divider_line, get_logo_header_elements, make_branded_footer
from src.report_tables import create_wrapped_table

# Import Airbyte brand colors from existing report generator
from src.metabase_report_generator import (
    AIRBYTE_BLUE_100, AIRBYTE_BLUE_200, AIRBYTE_BLUE_300, AIRBYTE_BLUE_700,
    AIRBYTE_INDIGO_500, AIRBYTE_INDIGO_700, AIRBYTE_INDIGO_800,
    AIRBYTE_PEACH_100, AIRBYTE_PEACH_150, AIRBYTE_PEACH_700,
    AIRBYTE_COOL_50, AIRBYTE_COOL_100, AIRBYTE_COOL_200, AIRBYTE_COOL_500,
    AIRBYTE_COOL_600, AIRBYTE_COOL_700,
    AIRBYTE_NEUTRAL_800,
    AIRBYTE_SUCCESS, AIRBYTE_WARNING, AIRBYTE_DANGER,
)

# Import cron utilities
from src.cron_generator import (
    generate_daily_cron,
    generate_cron_alternatives,
    format_cron_with_explanation,
    format_timezone_table,
)

# Import scheduling analysis helpers
from src.scheduling_queries import (
    identify_peak_hours,
    identify_quiet_hours,
    categorize_hours,
)

# Green color for low-traffic indicators
AIRBYTE_GREEN_100 = colors.HexColor('#E8F5E9')
AIRBYTE_GREEN_600 = colors.HexColor('#43A047')


def _shorten_text(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return value[:max_length - 3] + "..."


def generate_scheduling_optimization_report(
    customer_name: str,
    summary_data: Dict[str, Any],
    hourly_data: Dict[int, Dict[str, float]],
    connection_data: List[Dict[str, Any]],
    output_dir: str = ".",
    peak_connection_data: Optional[List[Dict[str, Any]]] = None,
    job_history_analysis: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate a scheduling optimization PDF report.

    Args:
        customer_name: Customer name for report header
        summary_data: Parsed summary from parse_metabase_summary_result()
        hourly_data: Hour -> metrics mapping from parse_metabase_hourly_result()
        connection_data: List of connection records with timing data
        output_dir: Directory to save the PDF report
        peak_connection_data: Optional pre-filtered peak-hour connections
        job_history_analysis: Optional analysis from job_history_analyzer for enhanced confidence

    Returns:
        Dict with file path, analysis results, and recommendations
    """
    if "error" in summary_data:
        return {"success": False, "error": summary_data["error"]}

    # Analyze patterns
    peak_hours = identify_peak_hours(hourly_data, top_n=3)
    quiet_hours = identify_quiet_hours(hourly_data, bottom_n=5)
    hour_categories = categorize_hours(hourly_data)

    # Identify connections running at peak hours
    peak_connections = peak_connection_data or _identify_peak_hour_connections(
        connection_data, peak_hours
    )

    # Generate recommendations
    recommendations = _generate_recommendations(
        peak_connections, quiet_hours, hourly_data
    )

    # Calculate potential impact - use job history analysis if available
    if job_history_analysis and job_history_analysis.get("success"):
        # Use enhanced confidence from actual job history
        impact_analysis = _calculate_potential_impact_with_job_history(
            summary_data, hourly_data, peak_connections, recommendations, job_history_analysis
        )
    else:
        # Fall back to basic estimation
        impact_analysis = _calculate_potential_impact(
            summary_data, hourly_data, peak_connections, recommendations
        )

    # Generate safe filename
    safe_name = "".join(c if c.isalnum() else "_" for c in customer_name)
    timestamp = datetime.utcnow().strftime('%Y%m%d')
    output_path = os.path.join(
        output_dir, f"{safe_name}_Scheduling_Optimization_Report_{timestamp}.pdf"
    )

    # Create PDF
    _create_scheduling_report_pdf(
        customer_name=customer_name,
        summary_data=summary_data,
        hourly_data=hourly_data,
        peak_hours=peak_hours,
        quiet_hours=quiet_hours,
        peak_connections=peak_connections,
        recommendations=recommendations,
        impact_analysis=impact_analysis,
        output_path=output_path,
    )

    return {
        "success": True,
        "file_path": output_path,
        "customer_name": customer_name,
        "analysis": {
            "peak_hours": peak_hours,
            "quiet_hours": quiet_hours,
            "peak_connections_count": len(peak_connections),
            "recommendations_count": len(recommendations),
        },
        "impact": impact_analysis,
    }


def _identify_peak_hour_connections(
    connection_data: List[Dict[str, Any]],
    peak_hours: List[int],
) -> List[Dict[str, Any]]:
    """
    Filter connections that typically run during peak hours.

    Args:
        connection_data: List of connection records
        peak_hours: List of peak hour integers

    Returns:
        List of connections running at peak hours, sorted by impact
    """
    peak_connections = []

    for conn in connection_data:
        run_hour = conn.get("typical_run_hour", conn.get("start_hour_utc"))
        if run_hour in peak_hours:
            peak_connections.append({
                **conn,
                "is_peak_hour": True,
                "impact": "HIGH" if conn.get("total_jobs", 0) > 20 else "MEDIUM",
            })

    # Sort by total jobs (most impactful first)
    peak_connections.sort(
        key=lambda x: x.get("total_jobs", 0), reverse=True
    )

    return peak_connections


def _generate_recommendations(
    peak_connections: List[Dict[str, Any]],
    quiet_hours: List[int],
    hourly_data: Dict[int, Dict[str, float]],
) -> List[Dict[str, Any]]:
    """
    Generate rescheduling recommendations for peak-hour connections.

    Args:
        peak_connections: Connections running at peak hours
        quiet_hours: Low-traffic hours to recommend
        hourly_data: Hourly usage data

    Returns:
        List of recommendation dictionaries
    """
    recommendations = []
    used_hours = set()

    for conn in peak_connections[:10]:  # Top 10 peak connections
        current_hour = conn.get("typical_run_hour", conn.get("start_hour_utc", 0))

        # Find best available quiet hour
        recommended_hour = None
        for qh in quiet_hours:
            if qh not in used_hours:
                recommended_hour = qh
                used_hours.add(qh)
                break

        if recommended_hour is None:
            # All quiet hours used, pick the lowest-usage one
            recommended_hour = quiet_hours[0] if quiet_hours else 3

        # Generate cron expression
        cron = generate_daily_cron(recommended_hour, 0)
        alternatives = generate_cron_alternatives(recommended_hour, count=3)

        # Get hourly metrics
        current_metrics = hourly_data.get(current_hour, {})
        recommended_metrics = hourly_data.get(recommended_hour, {})

        recommendations.append({
            "connection_name": conn.get("connection_name", "Unknown"),
            "connection_id": conn.get("connection_id"),
            "source": conn.get("source", ""),
            "destination": conn.get("destination", ""),
            "current_hour": current_hour,
            "current_cron": conn.get("cron_expression"),
            "current_p99": current_metrics.get("p99_workers", 0),
            "recommended_hour": recommended_hour,
            "recommended_cron": cron,
            "recommended_p99": recommended_metrics.get("p99_workers", 0),
            "alternative_crons": alternatives,
            "avg_duration_minutes": conn.get("avg_duration_minutes", 0),
            "impact": conn.get("impact", "MEDIUM"),
        })

    return recommendations


def _calculate_potential_impact(
    summary_data: Dict[str, Any],
    hourly_data: Dict[int, Dict[str, float]],
    peak_connections: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Estimate the potential P99 reduction from rescheduling.

    Args:
        summary_data: Account summary data
        hourly_data: Hourly usage patterns
        peak_connections: Connections at peak hours
        recommendations: Rescheduling recommendations

    Returns:
        Impact analysis dictionary
    """
    current_p99 = summary_data.get("p99_workers", 0)
    contracted = summary_data.get("contracted_workers", 0)

    # Estimate impact based on how many connections are being rescheduled
    # This is a rough estimate - actual impact depends on many factors
    num_recommendations = len(recommendations)

    if num_recommendations == 0:
        return {
            "current_p99": current_p99,
            "estimated_p99": current_p99,
            "potential_reduction": 0,
            "reduction_percentage": 0,
            "confidence": "low",
            "message": "No connections identified for rescheduling.",
        }

    # Calculate average P99 difference between peak and quiet hours
    peak_hours = identify_peak_hours(hourly_data, top_n=3)
    quiet_hours = identify_quiet_hours(hourly_data, bottom_n=5)

    avg_peak_p99 = sum(
        hourly_data.get(h, {}).get("p99_workers", 0) for h in peak_hours
    ) / max(len(peak_hours), 1)

    avg_quiet_p99 = sum(
        hourly_data.get(h, {}).get("p99_workers", 0) for h in quiet_hours
    ) / max(len(quiet_hours), 1)

    # Rough estimate: each rescheduled connection could reduce P99 by
    # a fraction of the peak-quiet difference
    per_connection_impact = (avg_peak_p99 - avg_quiet_p99) / max(len(peak_connections), 1)
    estimated_reduction = per_connection_impact * num_recommendations * 0.5  # Conservative

    estimated_p99 = max(current_p99 - estimated_reduction, avg_quiet_p99)
    reduction_percentage = ((current_p99 - estimated_p99) / current_p99 * 100) if current_p99 > 0 else 0

    # Determine confidence level
    if num_recommendations >= 5 and reduction_percentage > 10:
        confidence = "medium"
    elif num_recommendations >= 3:
        confidence = "low-medium"
    else:
        confidence = "low"

    return {
        "current_p99": round(current_p99, 1),
        "estimated_p99": round(estimated_p99, 1),
        "potential_reduction": round(current_p99 - estimated_p99, 1),
        "reduction_percentage": round(reduction_percentage, 0),
        "confidence": confidence,
        "connections_to_reschedule": num_recommendations,
        "peak_hour_p99_avg": round(avg_peak_p99, 1),
        "quiet_hour_p99_avg": round(avg_quiet_p99, 1),
        "message": (
            f"Rescheduling {num_recommendations} connection(s) could potentially "
            f"reduce P99 from {current_p99:.1f} to ~{estimated_p99:.1f} workers "
            f"({reduction_percentage:.0f}% reduction)."
        ),
    }


def _calculate_potential_impact_with_job_history(
    summary_data: Dict[str, Any],
    hourly_data: Dict[int, Dict[str, float]],
    peak_connections: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    job_history_analysis: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Calculate potential impact using actual job history data for higher confidence.

    Args:
        summary_data: Account summary data
        hourly_data: Hourly usage patterns
        peak_connections: Connections at peak hours
        recommendations: Rescheduling recommendations
        job_history_analysis: Analysis from job_history_analyzer module

    Returns:
        Impact analysis dictionary with enhanced confidence
    """
    current_p99 = summary_data.get("p99_workers", 0)
    contracted = summary_data.get("contracted_workers", 0)
    num_recommendations = len(recommendations)

    if num_recommendations == 0:
        return {
            "current_p99": current_p99,
            "estimated_p99": current_p99,
            "potential_reduction": 0,
            "reduction_percentage": 0,
            "confidence": "low",
            "confidence_score": 0,
            "data_quality": job_history_analysis.get("data_quality", "none"),
            "message": "No connections identified for rescheduling.",
        }

    # Use job history analysis for confidence and estimates
    confidence = job_history_analysis.get("confidence", "low")
    confidence_score = job_history_analysis.get("confidence_score", 0)
    data_quality = job_history_analysis.get("data_quality", "unknown")
    estimated_p99 = job_history_analysis.get("estimated_new_p99", current_p99)
    estimated_reduction = job_history_analysis.get("estimated_worker_reduction", 0)
    reduction_percentage = job_history_analysis.get("reduction_percentage", 0)

    # Get peak/quiet hour averages for context
    peak_hours = identify_peak_hours(hourly_data, top_n=3)
    quiet_hours = identify_quiet_hours(hourly_data, bottom_n=5)

    avg_peak_p99 = sum(
        hourly_data.get(h, {}).get("p99_workers", 0) for h in peak_hours
    ) / max(len(peak_hours), 1)

    avg_quiet_p99 = sum(
        hourly_data.get(h, {}).get("p99_workers", 0) for h in quiet_hours
    ) / max(len(quiet_hours), 1)

    # Build confidence explanation
    confidence_reason = job_history_analysis.get("reason", "")
    jobs_analyzed = job_history_analysis.get("total_jobs_analyzed", 0)
    overlaps_found = job_history_analysis.get("peak_hour_overlaps", 0)
    verified_count = job_history_analysis.get("recommended_connections_verified", 0)

    # Build detailed message
    if confidence in ["high", "medium"]:
        message = (
            f"Rescheduling {num_recommendations} connection(s) could reduce P99 from "
            f"{current_p99:.1f} to ~{estimated_p99:.1f} workers ({reduction_percentage:.0f}% reduction). "
            f"[{confidence.upper()} confidence based on {jobs_analyzed} jobs analyzed]"
        )
    else:
        message = (
            f"Rescheduling {num_recommendations} connection(s) could potentially reduce P99 from "
            f"{current_p99:.1f} to ~{estimated_p99:.1f} workers ({reduction_percentage:.0f}% reduction). "
            f"[{confidence.upper()} confidence - {confidence_reason}]"
        )

    return {
        "current_p99": round(current_p99, 1),
        "estimated_p99": round(estimated_p99, 1),
        "potential_reduction": round(estimated_reduction, 1),
        "reduction_percentage": round(reduction_percentage, 0),
        "confidence": confidence,
        "confidence_score": confidence_score,
        "data_quality": data_quality,
        "jobs_analyzed": jobs_analyzed,
        "overlaps_found": overlaps_found,
        "verified_connections": verified_count,
        "connections_to_reschedule": num_recommendations,
        "peak_hour_p99_avg": round(avg_peak_p99, 1),
        "quiet_hour_p99_avg": round(avg_quiet_p99, 1),
        "message": message,
    }


def _create_scheduling_report_pdf(
    customer_name: str,
    summary_data: Dict[str, Any],
    hourly_data: Dict[int, Dict[str, float]],
    peak_hours: List[int],
    quiet_hours: List[int],
    peak_connections: List[Dict[str, Any]],
    recommendations: List[Dict[str, Any]],
    impact_analysis: Dict[str, Any],
    output_path: str,
):
    """Create the scheduling optimization PDF report."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=0.6*inch,
        leftMargin=0.6*inch,
        topMargin=0.6*inch,
        bottomMargin=0.6*inch
    )

    styles = getSampleStyleSheet()

    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=22,
        spaceAfter=20,
        alignment=TA_CENTER,
        textColor=AIRBYTE_INDIGO_800
    )

    section_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=14,
        spaceBefore=15,
        spaceAfter=10,
        textColor=AIRBYTE_INDIGO_500
    )

    body_style = ParagraphStyle(
        'BodyText',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=8,
        leading=13,
        textColor=AIRBYTE_NEUTRAL_800
    )

    code_style = ParagraphStyle(
        'CodeText',
        parent=styles['Normal'],
        fontName='Courier',
        fontSize=9,
        spaceAfter=6,
        leading=12,
        textColor=AIRBYTE_INDIGO_700,
        backColor=AIRBYTE_COOL_50,
        borderPadding=8
    )

    story = []

    # =========================================================================
    # PAGE 1: Executive Summary
    # =========================================================================

    # Airbyte logo header
    story.extend(get_logo_header_elements())
    story.append(Spacer(1, 4))

    story.append(Paragraph(f"{customer_name}", title_style))
    story.append(Paragraph("Scheduling Optimization Report", ParagraphStyle(
        'Subtitle', parent=styles['Heading2'],
        fontSize=16, alignment=TA_CENTER, spaceAfter=10,
        textColor=AIRBYTE_INDIGO_500
    )))

    # Colored divider line
    story.append(get_divider_line())
    story.append(Spacer(1, 12))

    # Status box
    contracted = summary_data.get("contracted_workers", 0)
    p99 = summary_data.get("p99_workers", 0)
    status = summary_data.get("capacity_status", "unknown")

    if status == "over_capacity":
        status_label = "OVER-UTILIZED"
        status_bg = AIRBYTE_PEACH_150
    elif status == "near_capacity":
        status_label = "NEAR CAPACITY"
        status_bg = AIRBYTE_PEACH_100
    else:
        status_label = status.upper().replace("_", " ")
        status_bg = AIRBYTE_BLUE_100

    status_text = f"""
    <b>Current Status: {status_label}</b><br/>
    P99 Worker Usage: <b>{p99:.1f}</b> / Contracted: <b>{contracted}</b> workers<br/>
    Billing Utilization: <b>{summary_data.get('billing_utilization_pct', 0):.0f}%</b>
    """
    story.append(Paragraph(status_text, ParagraphStyle(
        'StatusBox', parent=body_style,
        backColor=status_bg,
        borderPadding=12,
        alignment=TA_CENTER
    )))

    story.append(Spacer(1, 15))

    # Key Finding - use KeepTogether to prevent title/content separation
    if peak_connections:
        finding_text = f"""
        <b>{len(peak_connections)} connection(s)</b> are running during peak hours,
        contributing to elevated P99 worker usage. By rescheduling these connections to
        low-traffic time windows, you may be able to significantly reduce utilization.
        """
    else:
        finding_text = """
        No connections were identified running during peak hours. Your sync schedules
        appear to be well-distributed across the day.
        """
    key_finding_section = [
        Paragraph("Key Finding", section_style),
        Paragraph(finding_text, body_style),
    ]
    story.append(KeepTogether(key_finding_section))

    # Estimated Impact - use KeepTogether and combine content into single box
    story.append(Spacer(1, 10))

    impact_text = impact_analysis.get("message", "Unable to estimate impact.")
    impact_confidence = impact_analysis.get("confidence", "low")
    impact_bg = AIRBYTE_GREEN_100 if impact_analysis.get("potential_reduction", 0) > 0 else AIRBYTE_COOL_100

    # Combine impact message and confidence into one box
    combined_impact_text = f"""
    {impact_text}<br/><br/>
    <i><font color="#707089">Confidence: {impact_confidence.upper()} - Actual results may vary based on workload characteristics.</font></i>
    """

    estimated_impact_section = [
        Paragraph("Estimated Impact", section_style),
        Paragraph(combined_impact_text, ParagraphStyle(
            'ImpactBox', parent=body_style,
            backColor=impact_bg,
            borderPadding=12
        )),
    ]
    story.append(KeepTogether(estimated_impact_section))

    # =========================================================================
    # PAGE 2: Peak Hour Analysis
    # =========================================================================
    story.append(PageBreak())
    story.append(Paragraph("Peak Hour Analysis", title_style))

    # Hourly Worker Usage header and legend (keep together, but table is too large)
    hourly_header_section = [
        Paragraph("Hourly Worker Usage (UTC)", section_style),
        Paragraph(
            "Hours are color-coded: <font color='#FF694A'><b>RED = Peak (avoid)</b></font>, "
            "<font color='#43A047'><b>GREEN = Low-traffic (recommended)</b></font>",
            body_style
        ),
    ]
    story.append(KeepTogether(hourly_header_section))

    # Build hourly table
    hourly_table_data = [["Hour (UTC)", "P99 Workers", "Avg Workers", "Status"]]

    for hour in range(24):
        metrics = hourly_data.get(hour, {"p99_workers": 0, "avg_workers": 0})
        p99_val = metrics.get("p99_workers", 0)
        avg_val = metrics.get("avg_workers", 0)

        if hour in peak_hours:
            status_str = "PEAK"
        elif hour in quiet_hours:
            status_str = "LOW"
        else:
            status_str = ""

        hourly_table_data.append([
            f"{hour:02d}:00",
            f"{p99_val:.1f}",
            f"{avg_val:.1f}",
            status_str
        ])

    # Build row styles
    table_style = [
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('TOPPADDING', (0, 0), (-1, -1), 4),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
    ]

    # Color-code rows by status
    status_text_colors = {}
    for i, row in enumerate(hourly_table_data[1:], start=1):
        hour = int(row[0].split(":")[0])
        if hour in peak_hours:
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_PEACH_100))
            table_style.append(('TEXTCOLOR', (3, i), (3, i), AIRBYTE_PEACH_700))
            status_text_colors[(i, 3)] = AIRBYTE_PEACH_700
        elif hour in quiet_hours:
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_GREEN_100))
            table_style.append(('TEXTCOLOR', (3, i), (3, i), AIRBYTE_GREEN_600))
            status_text_colors[(i, 3)] = AIRBYTE_GREEN_600
        else:
            table_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_COOL_50))

    hourly_table = create_wrapped_table(
        hourly_table_data,
        col_widths=[1.2*inch, 1.3*inch, 1.3*inch, 1.0*inch],
        font_size=9,
        text_color_overrides=status_text_colors,
    )
    hourly_table.setStyle(TableStyle(table_style))
    story.append(hourly_table)

    # Summary of peak and quiet hours
    story.append(Spacer(1, 15))
    peak_hours_str = ", ".join(f"{h:02d}:00" for h in sorted(peak_hours))
    quiet_hours_str = ", ".join(f"{h:02d}:00" for h in sorted(quiet_hours))

    summary_box = f"""
    <b>Peak Hours (Avoid Scheduling):</b> {peak_hours_str} UTC<br/>
    <b>Low-Traffic Hours (Recommended):</b> {quiet_hours_str} UTC
    """
    story.append(Paragraph(summary_box, ParagraphStyle(
        'SummaryBox', parent=body_style,
        backColor=AIRBYTE_BLUE_100,
        borderPadding=10
    )))

    # =========================================================================
    # PAGE 3: Connections Running at Peak Times
    # =========================================================================
    if peak_connections:
        story.append(PageBreak())
        story.append(Paragraph("Connections Running at Peak Times", title_style))

        story.append(Paragraph(
            f"The following {len(peak_connections)} connection(s) run during peak hours "
            "and are candidates for rescheduling:",
            body_style
        ))

        # Connections table
        conn_table_data = [["Connection", "Source → Dest", "Run Hour", "Duration", "Impact"]]

        for conn in peak_connections[:15]:  # Show top 15
            name = conn.get("connection_name", "Unknown")
            name = _shorten_text(name, 35)

            source = conn.get("source", "")
            dest = conn.get("destination", "")
            route = f"{source} → {dest}"
            route = _shorten_text(route, 25)

            hour = conn.get("typical_run_hour", conn.get("start_hour_utc", 0))
            duration = conn.get("avg_duration_minutes", 0)
            impact = conn.get("impact", "MEDIUM")

            conn_table_data.append([
                name,
                route,
                f"{hour:02d}:00 UTC",
                f"{duration:.0f} min",
                impact
            ])

        conn_table = create_wrapped_table(
            conn_table_data,
            col_widths=[2.2*inch, 1.8*inch, 1.0*inch, 0.8*inch, 0.7*inch],
            font_size=9,
        )

        conn_style = [
            ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_PEACH_700),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
            ('ALIGN', (2, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
            ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ]

        # Alternate row colors
        for i in range(1, len(conn_table_data)):
            if i % 2 == 0:
                conn_style.append(('BACKGROUND', (0, i), (-1, i), AIRBYTE_PEACH_100))
            else:
                conn_style.append(('BACKGROUND', (0, i), (-1, i), colors.white))

        conn_table.setStyle(TableStyle(conn_style))
        story.append(conn_table)

    # =========================================================================
    # PAGE 4: Recommended Schedule Changes
    # =========================================================================
    if recommendations:
        story.append(PageBreak())
        story.append(Paragraph("Recommended Schedule Changes", title_style))

        story.append(Paragraph(
            "For each connection, we provide a recommended Quartz cron expression "
            "(6-value format) for the Airbyte Cloud UI:",
            body_style
        ))

        for i, rec in enumerate(recommendations[:8], start=1):  # Show top 8
            rec_block = []

            # Connection header
            rec_block.append(Paragraph(
                f"<b>{i}. {rec['connection_name']}</b>",
                ParagraphStyle('RecHeader', parent=section_style, fontSize=12, spaceBefore=15)
            ))

            # Route info
            rec_block.append(Paragraph(
                f"{rec['source']} → {rec['destination']}",
                ParagraphStyle('Route', parent=body_style, textColor=AIRBYTE_COOL_600, fontSize=9)
            ))

            # Current vs Recommended
            current_hour = rec['current_hour']
            recommended_hour = rec['recommended_hour']
            recommended_cron = rec['recommended_cron']

            change_text = f"""
            <b>Current Schedule:</b> Runs at ~{current_hour:02d}:00 UTC (peak hour)<br/>
            <b>Recommended:</b> Move to {recommended_hour:02d}:00 UTC (low-traffic)
            """
            rec_block.append(Paragraph(change_text, body_style))

            # Cron expression box
            cron_info = format_cron_with_explanation(recommended_cron)
            cron_box = f"""
            <b>Quartz Cron Expression:</b> <font face="Courier">{recommended_cron}</font><br/>
            {cron_info.get('description', '')}
            """
            rec_block.append(Paragraph(cron_box, code_style))

            # Alternative schedules
            alternatives = rec.get('alternative_crons', [])
            if alternatives:
                alt_text = "<b>Alternative schedules:</b> " + ", ".join(
                    f"<font face='Courier'>{alt}</font>" for alt in alternatives[:3]
                )
                rec_block.append(Paragraph(alt_text, ParagraphStyle(
                    'Alternatives', parent=body_style, fontSize=9
                )))

            # Keep the recommendation block together
            story.append(KeepTogether(rec_block))

    # =========================================================================
    # PAGE 5: Implementation Guide
    # =========================================================================
    story.append(PageBreak())
    story.append(Paragraph("Implementation Guide", title_style))

    # How to Update Connection Schedules - use KeepTogether
    guide_text = """
    <b>In Airbyte Cloud UI:</b><br/>
    1. Navigate to the connection you want to reschedule<br/>
    2. Click on "Settings" or "Configuration"<br/>
    3. Find the "Schedule" section<br/>
    4. Select "Cron" as the schedule type<br/>
    5. Enter the 6-value Quartz cron expression from this report<br/>
    6. Ensure timezone is set to UTC<br/>
    7. Save changes<br/><br/>
    <b>Important:</b> Quartz cron uses 6 values (seconds, minutes, hours, day-of-month, month, day-of-week).
    Standard Unix cron uses 5 values - make sure to include the leading "0" for seconds.
    """
    update_guide_section = [
        Paragraph("How to Update Connection Schedules", section_style),
        Paragraph(guide_text, ParagraphStyle(
            'Guide', parent=body_style,
            backColor=AIRBYTE_BLUE_100,
            borderPadding=12
        )),
    ]
    story.append(KeepTogether(update_guide_section))

    # Timezone Reference - use KeepTogether
    story.append(Spacer(1, 15))

    # Use 3:00 UTC as example (common quiet hour)
    tz_data = [
        ["Timezone", "Local Time", "Notes"],
        ["UTC", "03:00", "Server time"],
        ["US/Pacific (PST)", "19:00 (prev day)", "7 PM previous day"],
        ["US/Eastern (EST)", "22:00 (prev day)", "10 PM previous day"],
        ["Europe/London (GMT)", "03:00", "Same as UTC"],
        ["Europe/Berlin (CET)", "04:00", "1 hour ahead"],
        ["Asia/Tokyo (JST)", "12:00", "Noon same day"],
    ]

    tz_table = create_wrapped_table(tz_data, col_widths=[2.0*inch, 1.5*inch, 2.5*inch], font_size=9)
    tz_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_500),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 9),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('BACKGROUND', (0, 1), (-1, -1), AIRBYTE_COOL_50),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
    ]))

    timezone_section = [
        Paragraph("Timezone Conversion Reference", section_style),
        Paragraph(
            "Example: 03:00 UTC in common timezones",
            ParagraphStyle('TableCaption', parent=body_style, fontSize=9, textColor=AIRBYTE_COOL_600)
        ),
        tz_table,
    ]
    story.append(KeepTogether(timezone_section))

    # Best Practices - use KeepTogether
    story.append(Spacer(1, 15))

    practices_text = """
    <b>1. Stagger start times:</b> Don't schedule all syncs at the same minute.
    Use :00, :15, :30, :45 to spread load.<br/><br/>
    <b>2. Consider data freshness needs:</b> Business-critical syncs may need to run
    during peak hours for timely data.<br/><br/>
    <b>3. Account for sync duration:</b> If a sync takes 2 hours, starting at 03:00
    means it runs until 05:00.<br/><br/>
    <b>4. Monitor after changes:</b> Review P99 metrics after rescheduling to verify
    the expected reduction.
    """
    best_practices_section = [
        Paragraph("Best Practices for Sync Scheduling", section_style),
        Paragraph(practices_text, body_style),
    ]
    story.append(KeepTogether(best_practices_section))

    # Footer
    story.append(Spacer(1, 30))
    footer_text = f"""
    <i>Report generated on {datetime.utcnow().strftime('%Y-%m-%d at %H:%M:%S UTC')}.<br/>
    This report provides recommendations based on historical usage patterns.
    Actual results may vary.</i>
    """
    story.append(Paragraph(footer_text, ParagraphStyle(
        'Footer', parent=styles['Normal'],
        fontSize=8, textColor=AIRBYTE_COOL_500
    )))

    # Build PDF with branded footer
    footer_fn = make_branded_footer(customer_name)
    doc.build(story, onFirstPage=footer_fn, onLaterPages=footer_fn)
    print(f"Generated: {output_path}")


# Convenience function for batch generation
def should_generate_scheduling_report(summary_data: Dict[str, Any]) -> bool:
    """
    Determine if a scheduling optimization report should be generated.

    Returns True for over-utilized or near-capacity customers.

    Args:
        summary_data: Parsed summary data

    Returns:
        True if report should be generated
    """
    status = summary_data.get("capacity_status", "unknown")
    billing_util = summary_data.get("billing_utilization_pct", 0)

    # Generate for over-utilized or near-capacity (85%+)
    if status in ("over_capacity", "near_capacity"):
        return True

    # Also generate if utilization is 85% or higher
    if billing_util >= 85:
        return True

    return False
