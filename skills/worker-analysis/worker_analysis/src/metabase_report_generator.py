#!/usr/bin/env python3
"""
Metabase-Based Worker Utilization Report Generator

Generates PDF reports directly from Metabase data using the P99 (99th percentile)
as the primary billing metric - matching Airbyte's actual billing methodology.

Key Features:
- Pulls data directly from Metabase (no CSV export needed)
- Uses P99 as primary billing/utilization metric
- Explains the ~7.5 hour burst allowance
- Provides actionable recommendations based on billing metric
- Uses Airbyte brand colors and styling

Usage (from MCP server or CLI):
    from src.metabase_report_generator import generate_report_from_metabase_data

    # Generate report from Metabase query results
    result = generate_report_from_metabase_data(
        summary_data=summary_result,
        hourly_data=hourly_result,
        daily_data=daily_result,
        customer_name="<Customer>",
        output_dir="/path/to/output"
    )
"""

import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from dateutil import parser as date_parser

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, KeepTogether
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from src.report_branding import get_divider_line, get_logo_header_elements, make_branded_footer
from src.report_tables import create_wrapped_table

# =============================================================================
# AIRBYTE BRAND COLORS (from Brand Style Guide 2025)
# =============================================================================

# Primary Colors - Blue (should cover ~70% of design)
AIRBYTE_BLUE_100 = colors.HexColor('#F5F5FF')
AIRBYTE_BLUE_200 = colors.HexColor('#F1F0FF')
AIRBYTE_BLUE_300 = colors.HexColor('#CAC7FF')
AIRBYTE_BLUE_400 = colors.HexColor('#C0BCFF')
AIRBYTE_BLUE_500 = colors.HexColor('#A5A3FF')
AIRBYTE_BLUE_600 = colors.HexColor('#8080FF')
AIRBYTE_BLUE_700 = colors.HexColor('#5F5CFF')
AIRBYTE_BLUE_800 = colors.HexColor('#463CFB')

# Secondary Colors - Indigo
AIRBYTE_INDIGO_50 = colors.HexColor('#5653D9')
AIRBYTE_INDIGO_100 = colors.HexColor('#4545BE')
AIRBYTE_INDIGO_200 = colors.HexColor('#4D4DA4')
AIRBYTE_INDIGO_300 = colors.HexColor('#363C7D')
AIRBYTE_INDIGO_400 = colors.HexColor('#270B8E')
AIRBYTE_INDIGO_500 = colors.HexColor('#282B5C')
AIRBYTE_INDIGO_600 = colors.HexColor('#202048')
AIRBYTE_INDIGO_700 = colors.HexColor('#1A194D')
AIRBYTE_INDIGO_800 = colors.HexColor('#0D0D37')
AIRBYTE_INDIGO_900 = colors.HexColor('#07072B')

# Secondary Colors - Peach Orange (for accents/highlights)
AIRBYTE_PEACH_100 = colors.HexColor('#FAEBEA')
AIRBYTE_PEACH_150 = colors.HexColor('#FFE6E0')
AIRBYTE_PEACH_300 = colors.HexColor('#FEC9BE')
AIRBYTE_PEACH_500 = colors.HexColor('#FF9E88')
AIRBYTE_PEACH_600 = colors.HexColor('#FE876C')
AIRBYTE_PEACH_700 = colors.HexColor('#FF694A')
AIRBYTE_PEACH_800 = colors.HexColor('#B85D4A')

# Secondary Colors - Coral Pink
AIRBYTE_CORAL_100 = colors.HexColor('#FFEDE9')
AIRBYTE_CORAL_200 = colors.HexColor('#E7BCBE')
AIRBYTE_CORAL_400 = colors.HexColor('#CE8A92')
AIRBYTE_CORAL_600 = colors.HexColor('#AB6871')
AIRBYTE_CORAL_800 = colors.HexColor('#753E45')

# Neutral Colors
AIRBYTE_WHITE = colors.HexColor('#F5F5FF')
AIRBYTE_NEUTRAL_100 = colors.HexColor('#EEEEEE')
AIRBYTE_NEUTRAL_200 = colors.HexColor('#CCCCCC')
AIRBYTE_NEUTRAL_300 = colors.HexColor('#AAAAAA')
AIRBYTE_NEUTRAL_600 = colors.HexColor('#666666')
AIRBYTE_NEUTRAL_700 = colors.HexColor('#444444')
AIRBYTE_NEUTRAL_800 = colors.HexColor('#222222')

# Neutral Cool Colors
AIRBYTE_COOL_50 = colors.HexColor('#F9F9FB')
AIRBYTE_COOL_100 = colors.HexColor('#F4F4F8')
AIRBYTE_COOL_200 = colors.HexColor('#D4D4E3')
AIRBYTE_COOL_300 = colors.HexColor('#BFC2D9')
AIRBYTE_COOL_400 = colors.HexColor('#979DBE')
AIRBYTE_COOL_500 = colors.HexColor('#8487A4')
AIRBYTE_COOL_600 = colors.HexColor('#707089')
AIRBYTE_COOL_700 = colors.HexColor('#5F5F82')
AIRBYTE_COOL_800 = colors.HexColor('#4A4A5B')
AIRBYTE_COOL_900 = colors.HexColor('#2F2F37')
AIRBYTE_COOL_950 = colors.HexColor('#222132')

# Background Colors
AIRBYTE_BG_DARK_1 = colors.HexColor('#0A0A24')
AIRBYTE_BG_DARK_2 = colors.HexColor('#14143D')
AIRBYTE_BG_PEACH = colors.HexColor('#FFEDE9')
AIRBYTE_BG_LIGHT_BLUE = colors.HexColor('#F7F7FF')

# Semantic Colors for Reports
AIRBYTE_SUCCESS = colors.HexColor('#5F5CFF')  # Blue 700 - healthy/good
AIRBYTE_WARNING = colors.HexColor('#FF9E88')  # Peach 500 - attention needed
AIRBYTE_DANGER = colors.HexColor('#FF694A')   # Peach 700 - urgent/over capacity
AIRBYTE_INFO = colors.HexColor('#8080FF')     # Blue 600 - informational


def generate_report_from_metabase_data(
    summary_data: Dict[str, Any],
    hourly_data: Optional[Dict[int, Dict[str, float]]] = None,
    daily_data: Optional[Dict[str, Dict[str, float]]] = None,
    customer_name: str = "Customer",
    output_dir: str = ".",
    workspace_ids: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Generate a worker utilization report from Metabase data.

    Uses P99 as the primary billing metric, matching Airbyte's billing methodology.

    Args:
        summary_data: Parsed summary from parse_metabase_summary_result()
        hourly_data: Optional parsed hourly patterns
        daily_data: Optional parsed daily patterns
        customer_name: Customer name for report header
        output_dir: Directory to save the PDF report
        workspace_ids: Optional list of workspace IDs for reference

    Returns:
        Dict with file path and summary statistics
    """
    if "error" in summary_data:
        return {"success": False, "error": summary_data["error"]}

    # Generate assessment
    from src.metabase_worker_data import generate_utilization_assessment
    assessment = generate_utilization_assessment(summary_data)

    # Generate safe filename
    safe_name = "".join(c if c.isalnum() else "_" for c in customer_name)
    timestamp = datetime.utcnow().strftime('%Y%m%d')
    output_path = os.path.join(output_dir, f"{safe_name}_Worker_Utilization_Report_{timestamp}.pdf")

    # Create PDF
    _create_utilization_report_pdf(
        summary_data=summary_data,
        hourly_data=hourly_data,
        daily_data=daily_data,
        assessment=assessment,
        customer_name=customer_name,
        output_path=output_path,
        workspace_ids=workspace_ids,
    )

    return {
        "success": True,
        "file_path": output_path,
        "customer_name": customer_name,
        "summary": {
            "contracted_workers": summary_data.get("contracted_workers"),
            "billing_workers_p99": summary_data.get("p99_workers"),
            "peak_workers": summary_data.get("peak_workers"),
            "avg_workers": summary_data.get("avg_workers"),
            "billing_utilization_pct": summary_data.get("billing_utilization_pct"),
            "capacity_status": summary_data.get("capacity_status"),
        },
        "assessment": {
            "recommendation": assessment.get("recommendation"),
            "message": assessment.get("message"),
            "urgency": assessment.get("urgency"),
        },
    }


def _create_utilization_report_pdf(
    summary_data: Dict[str, Any],
    hourly_data: Optional[Dict[int, Dict[str, float]]],
    daily_data: Optional[Dict[str, Dict[str, float]]],
    assessment: Dict[str, Any],
    customer_name: str,
    output_path: str,
    workspace_ids: Optional[List[str]] = None,
):
    """Create the PDF report using Airbyte brand colors."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=0.75*inch,
        bottomMargin=0.75*inch
    )

    styles = getSampleStyleSheet()

    # Custom styles using Airbyte brand colors
    title_style = ParagraphStyle(
        'CustomTitle',
        parent=styles['Heading1'],
        fontSize=24,
        spaceAfter=30,
        alignment=TA_CENTER,
        textColor=AIRBYTE_INDIGO_800  # Deep indigo for main title
    )

    section_style = ParagraphStyle(
        'SectionHeader',
        parent=styles['Heading2'],
        fontSize=16,
        spaceBefore=20,
        spaceAfter=12,
        textColor=AIRBYTE_INDIGO_500  # Medium indigo for section headers
    )

    body_style = ParagraphStyle(
        'BodyText',
        parent=styles['Normal'],
        fontSize=11,
        spaceAfter=10,
        leading=14,
        textColor=AIRBYTE_NEUTRAL_800  # Dark neutral for body text
    )

    note_style = ParagraphStyle(
        'NoteText',
        parent=styles['Normal'],
        fontSize=10,
        spaceAfter=10,
        leading=13,
        textColor=AIRBYTE_COOL_700,  # Cool gray for notes
        backColor=AIRBYTE_BLUE_100,  # Light blue background
        borderPadding=10
    )

    story = []

    # Airbyte logo header
    story.extend(get_logo_header_elements())
    story.append(Spacer(1, 4))

    # Title
    story.append(Paragraph(f"{customer_name} Data Worker Utilization Report", title_style))

    # Colored divider line
    story.append(get_divider_line(width=6.3))
    story.append(Spacer(1, 12))

    period_start = summary_data.get("period_start", "N/A")
    period_end = summary_data.get("period_end", "N/A")

    # Helper function to format dates to human-readable format
    def format_date(date_val):
        if date_val == "N/A" or not date_val:
            return "N/A"
        if hasattr(date_val, 'strftime'):
            return date_val.strftime('%B %d, %Y')
        # Parse ISO string format
        try:
            parsed = date_parser.parse(str(date_val))
            return parsed.strftime('%B %d, %Y')
        except (ValueError, TypeError):
            return str(date_val)[:10]  # Fallback to first 10 chars

    period_start_str = format_date(period_start)
    period_end_str = format_date(period_end)

    story.append(Paragraph(
        f"Analysis Period: {period_start_str} to {period_end_str}",
        ParagraphStyle('Subtitle', parent=styles['Normal'],
                      fontSize=12, alignment=TA_CENTER, spaceAfter=20)
    ))

    # Billing Methodology Note
    story.append(Paragraph("How Airbyte Measures Worker Usage", section_style))
    methodology_text = """
    Airbyte uses the <b>99th percentile (P99)</b> of worker usage over the analysis period to calculate
    your Data Worker utilization. This means approximately <b>7.5 hours of burst usage per month</b>
    are allowed without counting against your contracted worker limit.
    <br/><br/>
    This report uses the same P99 metric as your actual Airbyte bill, providing an accurate view
    of your capacity utilization.
    """
    # Use Table with single cell for reliable background color
    method_para = Paragraph(methodology_text, ParagraphStyle(
        'MethodNote', parent=body_style,
        fontSize=10,
        leading=13,
        textColor=AIRBYTE_COOL_700,
        leftIndent=5,
        rightIndent=5
    ))
    method_table = Table([[method_para]], colWidths=[6.3 * inch])
    method_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    story.append(method_table)

    # Executive Summary based on status
    story.append(Spacer(1, 10))
    story.append(Paragraph("Executive Summary", section_style))

    status = summary_data.get("capacity_status", "unknown")
    contracted = summary_data.get("contracted_workers", 0)
    p99 = summary_data.get("p99_workers", 0)
    billing_util = summary_data.get("billing_utilization_pct", 0)
    peak = summary_data.get("peak_workers", 0)

    if status == "over_capacity":
        summary_bg = AIRBYTE_PEACH_150  # Light peach for urgent issues
        summary_text = f"""
        <b>Status: Over Capacity</b><br/>
        Your P99 worker usage ({p99:.1f} workers) exceeds your contracted capacity ({contracted} workers).
        This indicates consistent overutilization that affects your billing. Consider increasing your
        worker allocation to {assessment.get('recommended_workers', contracted + 1)} workers.
        """
    elif status == "near_capacity":
        summary_bg = AIRBYTE_PEACH_100  # Very light peach for attention
        summary_text = f"""
        <b>Status: Near Capacity</b><br/>
        Operating at {billing_util:.0f}% of capacity with P99 usage of {p99:.1f} workers out of
        {contracted} contracted. Monitor usage trends closely, especially if you expect growth.
        """
    elif status == "healthy":
        summary_bg = AIRBYTE_BLUE_100  # Light blue for healthy state
        summary_text = f"""
        <b>Status: Healthy Utilization</b><br/>
        Your workspace is operating efficiently at {billing_util:.0f}% of capacity (P99: {p99:.1f} workers,
        Contracted: {contracted}). Your current allocation is well-suited to your workload.
        """
    elif status in ("under_utilized", "significantly_under_utilized"):
        summary_bg = AIRBYTE_BLUE_200  # Slightly darker blue for under-utilized
        potential = assessment.get('potential_reduction', 0)
        summary_text = f"""
        <b>Status: Under-Utilized</b><br/>
        Operating at only {billing_util:.0f}% of capacity (P99: {p99:.1f} workers, Contracted: {contracted}).
        {f'You could potentially reduce by {potential} worker(s) to optimize costs.' if potential > 0 else ''}
        """
    else:
        summary_bg = AIRBYTE_COOL_100  # Light cool gray for unknown state
        summary_text = f"""
        <b>Status: Under Review</b><br/>
        Usage data shows P99 of {p99:.1f} workers against {contracted} contracted workers
        ({billing_util:.0f}% utilization).
        """

    # Use Table with single cell for reliable background color
    summary_para = Paragraph(summary_text, ParagraphStyle(
        'Summary', parent=body_style,
        leftIndent=5,
        rightIndent=5
    ))
    summary_table = Table([[summary_para]], colWidths=[6.3 * inch])
    summary_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), summary_bg),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    story.append(summary_table)

    # Key Metrics Table
    story.append(Spacer(1, 15))
    story.append(Paragraph("Key Metrics", section_style))

    metrics_data = [
        ["Metric", "Value", "Notes"],
        ["Contracted Workers", str(contracted), "Your plan allocation"],
        ["P99 Usage (Billing Metric)", f"{p99:.1f} workers", f"{billing_util:.0f}% of capacity"],
        ["Average Usage", f"{summary_data.get('avg_workers', 0):.2f} workers",
         f"{summary_data.get('avg_utilization_pct', 0):.0f}% of capacity"],
        ["Peak Usage", f"{peak:.1f} workers",
         f"{summary_data.get('peak_utilization_pct', 0):.0f}% (bursts allowed)"],
        ["Available Headroom (P99)", f"{summary_data.get('billing_headroom', 0):.1f} workers",
         "Buffer for growth"],
    ]

    metrics_table = create_wrapped_table(metrics_data, col_widths=[2.2*inch, 1.8*inch, 2.5*inch], font_size=10, header_font_size=11)
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),  # Airbyte Indigo header
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 11),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), AIRBYTE_COOL_50),  # Light cool gray rows
        ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, -1), 10),
        ('GRID', (0, 0), (-1, -1), 1, AIRBYTE_COOL_200),  # Cool gray grid
        ('TOPPADDING', (0, 1), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 8),
        ('LEFTPADDING', (0, 0), (-1, -1), 10),
        # Highlight P99 row with Airbyte Blue
        ('BACKGROUND', (0, 2), (-1, 2), AIRBYTE_BLUE_100),
    ]))
    story.append(metrics_table)

    # Burst explanation if peak is significantly higher than P99
    # Only show this note when it's relevant (near or over capacity)
    if peak > p99 * 1.2 and status in ("near_capacity", "over_capacity"):
        burst_diff = peak - p99
        burst_text = f"""
        <b>Note on Peak vs P99:</b> Your peak usage ({peak:.1f} workers) is {burst_diff:.1f} workers
        higher than your P99 ({p99:.1f}). These short bursts fall within Airbyte's ~7.5 hour monthly
        allowance and do not affect your billing.
        """
        story.append(Spacer(1, 10))
        story.append(Paragraph(burst_text, note_style))

    # Usage Patterns (if hourly data available)
    if hourly_data:
        story.append(Spacer(1, 15))
        story.append(Paragraph("Usage Patterns by Hour (UTC)", section_style))

        # Find busiest and quietest hours
        sorted_hours = sorted(hourly_data.items(), key=lambda x: x[1].get('p99_workers', 0), reverse=True)
        busiest = sorted_hours[:3]
        quietest = sorted_hours[-3:]

        story.append(Paragraph("<b>Peak Activity Hours (UTC)</b>", body_style))

        busiest_data = [["Hour (UTC)", "P99 Workers", "Avg Workers", "% of Capacity"]]
        for hour, stats in busiest:
            p99_val = stats.get('p99_workers', 0)
            pct = (p99_val / contracted * 100) if contracted > 0 else 0
            busiest_data.append([
                f"{hour:02d}:00 - {hour+1:02d}:00",
                f"{p99_val:.2f}",
                f"{stats.get('avg_workers', 0):.2f}",
                f"{pct:.0f}%"
            ])

        busiest_table = create_wrapped_table(busiest_data, col_widths=[1.8*inch, 1.5*inch, 1.5*inch, 1.5*inch], font_size=10)
        busiest_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_BLUE_700),  # Airbyte Blue header
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, AIRBYTE_COOL_200),
            ('BACKGROUND', (0, 1), (-1, -1), AIRBYTE_BLUE_100),  # Light blue rows
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(busiest_table)

        story.append(Spacer(1, 10))
        story.append(Paragraph("<b>Low Activity Hours (UTC)</b>", body_style))

        quietest_data = [["Hour (UTC)", "P99 Workers", "Avg Workers", "% of Capacity"]]
        for hour, stats in quietest:
            p99_val = stats.get('p99_workers', 0)
            pct = (p99_val / contracted * 100) if contracted > 0 else 0
            quietest_data.append([
                f"{hour:02d}:00 - {hour+1:02d}:00",
                f"{p99_val:.2f}",
                f"{stats.get('avg_workers', 0):.2f}",
                f"{pct:.0f}%"
            ])

        quietest_table = create_wrapped_table(quietest_data, col_widths=[1.8*inch, 1.5*inch, 1.5*inch, 1.5*inch], font_size=10)
        quietest_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_COOL_600),  # Cool gray header
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, AIRBYTE_COOL_200),
            ('BACKGROUND', (0, 1), (-1, -1), AIRBYTE_COOL_50),  # Light cool gray rows
            ('TOPPADDING', (0, 0), (-1, -1), 8),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ]))
        story.append(quietest_table)

    # Daily patterns (if available)
    if daily_data:
        story.append(Spacer(1, 15))
        story.append(Paragraph("<b>Average Usage by Day of Week</b>", body_style))

        day_order = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday']
        daily_table_data = [["Day", "P99 Workers", "Avg Workers", "% of Capacity"]]

        for day in day_order:
            if day in daily_data:
                stats = daily_data[day]
                p99_val = stats.get('p99_workers', 0)
                pct = (p99_val / contracted * 100) if contracted > 0 else 0
                daily_table_data.append([
                    day,
                    f"{p99_val:.2f}",
                    f"{stats.get('avg_workers', 0):.2f}",
                    f"{pct:.0f}%"
                ])

        daily_table = create_wrapped_table(daily_table_data, col_widths=[1.8*inch, 1.5*inch, 1.5*inch, 1.5*inch], font_size=10)
        daily_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_50),  # Airbyte Indigo header
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 10),
            ('GRID', (0, 0), (-1, -1), 1, AIRBYTE_COOL_200),
            ('BACKGROUND', (0, 1), (-1, -1), AIRBYTE_BLUE_200),  # Light blue rows
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        story.append(daily_table)

    # Usage Pattern Analysis - P99 vs Average explanation
    avg_workers = summary_data.get('avg_workers', 0)
    if avg_workers > 0 and p99 > 0:
        p99_to_avg_ratio = p99 / avg_workers

        # Show this section when P99 is significantly higher than average (2x or more)
        if p99_to_avg_ratio >= 2.0:
            story.append(Spacer(1, 20))

            # Determine the severity of the spikiness
            if p99_to_avg_ratio >= 5.0:
                pattern_desc = "highly concentrated"
                pattern_severity = "significant"
            elif p99_to_avg_ratio >= 3.0:
                pattern_desc = "concentrated"
                pattern_severity = "notable"
            else:
                pattern_desc = "somewhat concentrated"
                pattern_severity = "moderate"

            # Calculate what P99 could potentially be if usage was smoother
            # A well-distributed workload might have P99 around 1.5-2x the average
            potential_p99 = avg_workers * 1.75
            potential_savings = p99 - potential_p99

            analysis_text = f"""
            <b>Why is P99 ({p99:.1f}) so much higher than Average ({avg_workers:.2f})?</b><br/><br/>
            Your P99 is <b>{p99_to_avg_ratio:.1f}x higher</b> than your average usage, indicating your
            worker consumption is <b>{pattern_desc}</b> into short bursts rather than spread evenly
            throughout the day.<br/><br/>
            <b>What this means:</b><br/>
            &bull; <b>Average Usage ({avg_workers:.2f} workers):</b> This is your typical moment-to-moment
            consumption - most of the time you're using this amount.<br/>
            &bull; <b>P99 Usage ({p99:.1f} workers):</b> This is your billing metric - the level you hit or
            exceed only 1% of the time (about 7.5 hours/month).<br/><br/>
            The large gap between these numbers means you have {pattern_severity} usage spikes that are
            driving up your P99 billing metric.
            """

            # Section header outside the box
            story.append(Paragraph("Usage Pattern Analysis", section_style))

            # Use Table with single cell for reliable background color
            content_para = Paragraph(analysis_text, ParagraphStyle(
                'AnalysisNote', parent=body_style,
                leftIndent=5,
                rightIndent=5
            ))
            content_table = Table([[content_para]], colWidths=[6.3 * inch])
            content_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
                ('TOPPADDING', (0, 0), (-1, -1), 12),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
                ('LEFTPADDING', (0, 0), (-1, -1), 15),
                ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ]))
            story.append(content_table)

            # Add optimization opportunity if there's significant potential
            if potential_savings > 0.5 and status in ("over_capacity", "near_capacity"):
                story.append(Spacer(1, 10))

                optimization_text = f"""
                <b>Optimization Opportunity:</b><br/>
                If workloads were distributed more evenly across the day, your P99 could potentially
                drop from <b>{p99:.1f}</b> to closer to <b>{potential_p99:.1f} workers</b>
                (approximately {((p99 - potential_p99) / p99 * 100):.0f}% reduction).<br/><br/>
                This could be achieved by rescheduling sync jobs that currently run during peak hours
                to lower-traffic time windows. See the scheduling recommendations below for specific
                hours to target.
                """

                # Use Table with single cell for reliable background color
                opt_para = Paragraph(optimization_text, ParagraphStyle(
                    'OptimizationNote', parent=body_style,
                    leftIndent=5,
                    rightIndent=5
                ))
                opt_table = Table([[opt_para]], colWidths=[6.3 * inch])
                opt_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_PEACH_100),
                    ('TOPPADDING', (0, 0), (-1, -1), 12),
                    ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
                    ('LEFTPADDING', (0, 0), (-1, -1), 15),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 15),
                ]))
                story.append(opt_table)

    # Scheduling Optimization Recommendations (if hourly data available)
    if hourly_data and len(hourly_data) >= 12:
        sorted_hours = sorted(hourly_data.items(), key=lambda x: x[1].get('p99_workers', 0), reverse=True)
        busiest_hours = sorted_hours[:3]
        quietest_hours = sorted_hours[-5:]  # Get 5 quietest hours for more options

        # Only show if there's significant variation (busiest is 2x+ quietest)
        busiest_p99 = busiest_hours[0][1].get('p99_workers', 0) if busiest_hours else 0
        quietest_p99 = quietest_hours[-1][1].get('p99_workers', 0) if quietest_hours else 0

        if busiest_p99 > 0 and quietest_p99 > 0 and busiest_p99 >= quietest_p99 * 1.5:
            story.append(Spacer(1, 20))

            # Format busiest hours
            busy_hours_str = ", ".join([f"{h:02d}:00 UTC" for h, _ in busiest_hours])
            quiet_hours_str = ", ".join([f"{h:02d}:00 UTC" for h, _ in sorted(quietest_hours[:4], key=lambda x: x[0])])

            # Calculate potential reduction
            avg_busy_p99 = sum(h[1].get('p99_workers', 0) for h in busiest_hours) / len(busiest_hours)
            avg_quiet_p99 = sum(h[1].get('p99_workers', 0) for h in quietest_hours) / len(quietest_hours)

            sched_text = f"""
            <b>Consider rescheduling sync jobs to reduce peak utilization:</b><br/><br/>
            <b>High-traffic hours to avoid:</b> {busy_hours_str}<br/>
            These hours show P99 usage of {avg_busy_p99:.1f} workers on average.<br/><br/>
            <b>Recommended low-traffic hours:</b> {quiet_hours_str}<br/>
            These hours show P99 usage of only {avg_quiet_p99:.1f} workers on average.<br/><br/>
            By spreading workloads more evenly across the day, you may be able to reduce your
            P99 utilization and potentially optimize your worker allocation.
            """

            # Section header outside the box
            story.append(Paragraph("Scheduling Optimization", section_style))

            # Use Table with single cell for reliable background color
            sched_para = Paragraph(sched_text, ParagraphStyle(
                'SchedulingNote', parent=body_style,
                leftIndent=5,
                rightIndent=5
            ))
            sched_table = Table([[sched_para]], colWidths=[6.3 * inch])
            sched_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
                ('TOPPADDING', (0, 0), (-1, -1), 12),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
                ('LEFTPADDING', (0, 0), (-1, -1), 15),
                ('RIGHTPADDING', (0, 0), (-1, -1), 15),
            ]))
            story.append(sched_table)

    # Recommendation
    story.append(Spacer(1, 20))

    rec_message = assessment.get("message", "No specific recommendation.")

    # Airbyte brand colors for urgency levels
    urgency = assessment.get("urgency", "none")
    if urgency == "high":
        rec_bg = AIRBYTE_PEACH_150  # Light peach for urgent
    elif urgency == "medium":
        rec_bg = AIRBYTE_PEACH_100  # Very light peach for attention
    else:
        rec_bg = AIRBYTE_BLUE_100  # Light blue for healthy/good

    # Section header outside the box
    story.append(Paragraph("Recommendation", section_style))

    # Use Table with single cell for reliable background color
    rec_para = Paragraph(rec_message, ParagraphStyle(
        'Recommendation', parent=body_style,
        leftIndent=5,
        rightIndent=5
    ))
    rec_table = Table([[rec_para]], colWidths=[6.3 * inch])
    rec_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), rec_bg),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    story.append(rec_table)

    # Workspace Information (if provided)
    if workspace_ids:
        story.append(Spacer(1, 20))
        story.append(Paragraph("Workspace Information", section_style))

        ws_text = "This analysis covers the following Airbyte workspace(s):<br/>"
        for ws_id in workspace_ids:
            ws_text += f"&bull; {ws_id}<br/>"

        story.append(Paragraph(ws_text, body_style))

    # Footer with Airbyte branding
    story.append(Spacer(1, 30))
    footer_text = f"""
    <i>Report generated on {datetime.utcnow().strftime('%Y-%m-%d at %H:%M:%S UTC')}.<br/>
    Data sourced from Airbyte Metabase. Billing calculated using P99 (99th percentile) methodology.</i>
    """
    story.append(Paragraph(footer_text, ParagraphStyle(
        'Footer', parent=styles['Normal'],
        fontSize=9, textColor=AIRBYTE_COOL_500  # Cool gray for footer
    )))

    # Build PDF with branded footer
    footer_fn = make_branded_footer(customer_name)
    doc.build(story, onFirstPage=footer_fn, onLaterPages=footer_fn)
    print(f"Generated: {output_path}")
