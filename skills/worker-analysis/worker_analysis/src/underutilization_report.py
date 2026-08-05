#!/usr/bin/env python3
"""
Underutilization Report Generator

Generates PDF reports for under-utilized customers that help AEs understand
capacity headroom and potential optimization opportunities.

Key Features:
- Shows current vs contracted worker usage
- Identifies capacity headroom
- Provides recommendations for growth or rightsizing
- Highlights unused capacity cost

Usage:
    from src.underutilization_report import generate_underutilization_report

    result = generate_underutilization_report(
        customer_name="<Customer>",
        summary_data=summary,
        hourly_data=hourly,
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

# Import Airbyte brand colors
from src.metabase_report_generator import (
    AIRBYTE_BLUE_100, AIRBYTE_BLUE_200, AIRBYTE_BLUE_300, AIRBYTE_BLUE_700,
    AIRBYTE_INDIGO_500, AIRBYTE_INDIGO_700, AIRBYTE_INDIGO_800,
    AIRBYTE_PEACH_100, AIRBYTE_PEACH_150, AIRBYTE_PEACH_700,
    AIRBYTE_COOL_50, AIRBYTE_COOL_100, AIRBYTE_COOL_200, AIRBYTE_COOL_500,
    AIRBYTE_COOL_600, AIRBYTE_COOL_700,
    AIRBYTE_NEUTRAL_800,
    AIRBYTE_SUCCESS, AIRBYTE_WARNING, AIRBYTE_DANGER,
)

# Green colors for positive indicators
AIRBYTE_GREEN_50 = colors.HexColor('#E8F5E9')
AIRBYTE_GREEN_100 = colors.HexColor('#C8E6C9')
AIRBYTE_GREEN_500 = colors.HexColor('#4CAF50')
AIRBYTE_GREEN_600 = colors.HexColor('#43A047')
AIRBYTE_GREEN_700 = colors.HexColor('#388E3C')


def generate_underutilization_report(
    customer_name: str,
    summary_data: Dict[str, Any],
    hourly_data: Dict[int, Dict[str, float]],
    output_dir: str = ".",
    connection_count: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Generate an underutilization PDF report.

    Args:
        customer_name: Customer name for report header
        summary_data: Parsed summary from parse_metabase_summary_result()
        hourly_data: Hour -> metrics mapping from parse_metabase_hourly_result()
        output_dir: Directory to save the PDF report
        connection_count: Optional number of connections

    Returns:
        Dict with file path and analysis results
    """
    if "error" in summary_data:
        return {"success": False, "error": summary_data["error"]}

    # Extract key metrics
    contracted = summary_data.get("contracted_workers", 1)
    p99 = summary_data.get("p99_workers", 0)
    avg = summary_data.get("avg_workers", 0)
    peak = summary_data.get("peak_workers", 0)
    utilization_pct = (p99 / contracted * 100) if contracted > 0 else 0

    # Calculate headroom
    headroom_workers = contracted - p99
    headroom_pct = 100 - utilization_pct

    # Analyze hourly patterns
    if hourly_data:
        hourly_p99_values = [h.get("p99_workers", 0) for h in hourly_data.values()]
        max_hourly_p99 = max(hourly_p99_values) if hourly_p99_values else 0
        min_hourly_p99 = min(hourly_p99_values) if hourly_p99_values else 0
        hourly_variance = max_hourly_p99 - min_hourly_p99
    else:
        max_hourly_p99 = p99
        min_hourly_p99 = 0
        hourly_variance = 0

    # Generate recommendations
    recommendations = _generate_underutilization_recommendations(
        contracted, p99, avg, utilization_pct, headroom_workers, headroom_pct, hourly_variance
    )

    # Create output directory
    os.makedirs(output_dir, exist_ok=True)

    # Generate filename
    safe_name = customer_name.replace(" ", "_").replace("/", "_").replace("(", "_").replace(")", "_")
    date_str = datetime.now().strftime("%Y%m%d")
    filename = f"{safe_name}_Capacity_Analysis_Report_{date_str}.pdf"
    output_path = os.path.join(output_dir, filename)

    # Build PDF
    _build_underutilization_pdf(
        output_path=output_path,
        customer_name=customer_name,
        summary_data=summary_data,
        hourly_data=hourly_data,
        contracted=contracted,
        p99=p99,
        avg=avg,
        peak=peak,
        utilization_pct=utilization_pct,
        headroom_workers=headroom_workers,
        headroom_pct=headroom_pct,
        recommendations=recommendations,
        connection_count=connection_count,
    )

    return {
        "success": True,
        "file_path": output_path,
        "customer_name": customer_name,
        "contracted_workers": contracted,
        "p99_workers": p99,
        "avg_workers": avg,
        "utilization_pct": round(utilization_pct, 1),
        "headroom_workers": round(headroom_workers, 1),
        "headroom_pct": round(headroom_pct, 1),
        "recommendations": recommendations,
        "report_type": "underutilization",
    }


def _generate_underutilization_recommendations(
    contracted: float,
    p99: float,
    avg: float,
    utilization_pct: float,
    headroom_workers: float,
    headroom_pct: float,
    hourly_variance: float,
) -> List[Dict[str, Any]]:
    """Generate recommendations based on utilization patterns."""
    recommendations = []

    # Categorize underutilization level
    if utilization_pct < 25:
        severity = "significant"
        recommendations.append({
            "type": "rightsizing",
            "priority": "high",
            "title": "Consider Rightsizing",
            "description": (
                f"Current utilization is very low at {utilization_pct:.0f}%. "
                f"Customer is using only {p99:.1f} of {contracted:.0f} contracted workers. "
                f"Consider discussing a plan adjustment to better match actual usage."
            ),
        })
    elif utilization_pct < 50:
        severity = "moderate"
        recommendations.append({
            "type": "growth_opportunity",
            "priority": "medium",
            "title": "Growth Opportunity",
            "description": (
                f"Customer has significant headroom ({headroom_pct:.0f}% unused capacity). "
                f"This represents room for {headroom_workers:.1f} more workers worth of connections. "
                f"Consider discussing expansion of data integration use cases."
            ),
        })
    elif utilization_pct < 70:
        severity = "mild"
        recommendations.append({
            "type": "healthy_headroom",
            "priority": "low",
            "title": "Healthy Capacity Buffer",
            "description": (
                f"Customer has a comfortable {headroom_pct:.0f}% headroom buffer. "
                f"This is good for handling occasional spikes and growth. "
                f"No immediate action needed."
            ),
        })
    else:
        severity = "minimal"
        recommendations.append({
            "type": "approaching_capacity",
            "priority": "info",
            "title": "Approaching Optimal Utilization",
            "description": (
                f"Customer is at {utilization_pct:.0f}% utilization, approaching optimal range. "
                f"Monitor for growth to ensure capacity remains adequate."
            ),
        })

    # Check for growth potential based on hourly variance
    if hourly_variance < 1 and p99 < contracted * 0.5:
        recommendations.append({
            "type": "consistent_low_usage",
            "priority": "info",
            "title": "Consistent Low Usage Pattern",
            "description": (
                "Worker usage is consistently low throughout the day with minimal variance. "
                "This suggests the customer may benefit from a lower tier plan, "
                "or has room to add many more connections."
            ),
        })

    # Add connection growth estimate
    if headroom_workers > 0.5:
        # Worker capacity model: 1 worker = 5 API connections OR 2 DB connections
        # These can be COMBINED: (API/5) + (DB/2) = worker capacity used
        api_only_potential = int(headroom_workers * 5)
        db_only_potential = int(headroom_workers * 2)
        # Example combination: half API, half DB
        combo_api = int(headroom_workers * 2.5)  # Uses 0.5 worker capacity
        combo_db = int(headroom_workers * 1)     # Uses 0.5 worker capacity
        recommendations.append({
            "type": "expansion_capacity",
            "priority": "info",
            "title": "Connection Expansion Capacity",
            "description": (
                f"Current headroom ({headroom_workers:.1f} workers) can support additional connections. "
                f"Options: {api_only_potential} API-only, {db_only_potential} DB-only, "
                f"or combinations like {combo_api} API + {combo_db} DB. "
                f"Formula: (API÷5) + (DB÷2) ≤ {headroom_workers:.1f} workers."
            ),
        })

    return recommendations


def _build_underutilization_pdf(
    output_path: str,
    customer_name: str,
    summary_data: Dict[str, Any],
    hourly_data: Dict[int, Dict[str, float]],
    contracted: float,
    p99: float,
    avg: float,
    peak: float,
    utilization_pct: float,
    headroom_workers: float,
    headroom_pct: float,
    recommendations: List[Dict[str, Any]],
    connection_count: Optional[int] = None,
) -> None:
    """Build the PDF report."""
    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        rightMargin=0.75*inch,
        leftMargin=0.75*inch,
        topMargin=0.75*inch,
        bottomMargin=0.75*inch
    )

    styles = getSampleStyleSheet()
    story = []

    # Custom styles
    title_style = ParagraphStyle(
        'CustomTitle', parent=styles['Heading1'],
        fontSize=24, textColor=AIRBYTE_INDIGO_800,
        spaceAfter=20, alignment=TA_CENTER
    )
    section_style = ParagraphStyle(
        'SectionHeader', parent=styles['Heading2'],
        fontSize=14, textColor=AIRBYTE_INDIGO_700,
        spaceBefore=16, spaceAfter=8
    )
    body_style = ParagraphStyle(
        'Body', parent=styles['Normal'],
        fontSize=10, textColor=AIRBYTE_NEUTRAL_800,
        spaceAfter=8, leading=14
    )
    metric_style = ParagraphStyle(
        'Metric', parent=styles['Normal'],
        fontSize=11, textColor=AIRBYTE_NEUTRAL_800
    )

    # === HEADER ===
    # Airbyte logo header
    story.extend(get_logo_header_elements())
    story.append(Spacer(1, 4))

    story.append(Paragraph("Capacity Analysis Report", title_style))
    story.append(Paragraph(
        f"<b>{customer_name}</b>",
        ParagraphStyle('CustomerName', parent=body_style, fontSize=16, alignment=TA_CENTER)
    ))

    # Colored divider line
    story.append(get_divider_line(width=6.3))
    story.append(Spacer(1, 10))

    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%B %d, %Y')}",
        ParagraphStyle('Date', parent=body_style, fontSize=10,
                       textColor=AIRBYTE_COOL_500, alignment=TA_CENTER)
    ))
    story.append(Spacer(1, 15))

    # === EXECUTIVE SUMMARY ===
    # Determine status color and message
    if utilization_pct < 50:
        status_color = AIRBYTE_GREEN_100
        status_text = "UNDER-UTILIZED"
        status_detail = f"Significant capacity headroom available ({headroom_pct:.0f}% unused)"
    elif utilization_pct < 85:
        status_color = AIRBYTE_GREEN_50
        status_text = "WELL-PROVISIONED"
        status_detail = f"Healthy capacity buffer ({headroom_pct:.0f}% headroom)"
    else:
        status_color = AIRBYTE_PEACH_100
        status_text = "NEAR OPTIMAL"
        status_detail = f"Operating near capacity ({utilization_pct:.0f}% utilized)"

    # Build executive summary content
    status_content = Paragraph(
        f"<b>Status: {status_text}</b><br/><br/>"
        f"{status_detail}<br/><br/>"
        f"<b>P99 Worker Usage:</b> {p99:.1f} of {contracted:.0f} contracted workers<br/>"
        f"<b>Average Usage:</b> {avg:.1f} workers<br/>"
        f"<b>Peak Usage:</b> {peak:.1f} workers",
        body_style
    )

    # Use Table for colored box (more reliable than backColor on Paragraph)
    status_table = Table([[status_content]], colWidths=[6.5*inch])
    status_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), status_color),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))

    story.append(Paragraph("Executive Summary", section_style))
    story.append(status_table)
    story.append(Spacer(1, 15))

    # === CAPACITY METRICS ===
    story.append(Paragraph("Capacity Metrics", section_style))

    metrics_data = [
        ["Metric", "Value", "Status"],
        ["Contracted Workers", f"{contracted:.0f}", "—"],
        ["P99 Usage (99th percentile)", f"{p99:.1f}", f"{utilization_pct:.0f}% of contracted"],
        ["Average Usage", f"{avg:.1f}", f"{(avg/contracted*100):.0f}% of contracted"],
        ["Peak Usage", f"{peak:.1f}", f"{(peak/contracted*100):.0f}% of contracted"],
        ["Available Headroom", f"{headroom_workers:.1f} workers", f"{headroom_pct:.0f}% unused"],
    ]

    metrics_table = create_wrapped_table(metrics_data, col_widths=[2.5*inch, 1.5*inch, 2.5*inch], font_size=9, header_font_size=10)
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('FONTSIZE', (0, 1), (-1, -1), 9),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
        ('TOPPADDING', (0, 0), (-1, 0), 10),
        ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 6),
        ('BACKGROUND', (0, 1), (-1, -1), AIRBYTE_COOL_50),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [AIRBYTE_COOL_50, colors.white]),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
    ]))
    story.append(metrics_table)
    story.append(Spacer(1, 20))

    # === HOURLY USAGE PATTERN ===
    if hourly_data:
        story.append(Paragraph("Hourly Usage Pattern (UTC)", section_style))

        # Calculate thresholds for color-coding
        p99_values = [hourly_data.get(h, {}).get('p99_workers', 0) for h in range(24)]
        max_p99 = max(p99_values) if p99_values else 1
        min_p99 = min(p99_values) if p99_values else 0
        range_p99 = max_p99 - min_p99 if max_p99 > min_p99 else 1

        # Identify peak and quiet hours
        sorted_hours = sorted(range(24), key=lambda h: p99_values[h], reverse=True)
        peak_hours = set(sorted_hours[:3])  # Top 3 hours
        quiet_hours = set(sorted_hours[-5:])  # Bottom 5 hours

        # Build hourly table data
        hours_row1 = ["Hour"] + [f"{h:02d}:00" for h in range(0, 12)]
        p99_row1 = ["P99"] + [f"{hourly_data.get(h, {}).get('p99_workers', 0):.1f}" for h in range(0, 12)]
        avg_row1 = ["Avg"] + [f"{hourly_data.get(h, {}).get('avg_workers', 0):.1f}" for h in range(0, 12)]

        hours_row2 = ["Hour"] + [f"{h:02d}:00" for h in range(12, 24)]
        p99_row2 = ["P99"] + [f"{hourly_data.get(h, {}).get('p99_workers', 0):.1f}" for h in range(12, 24)]
        avg_row2 = ["Avg"] + [f"{hourly_data.get(h, {}).get('avg_workers', 0):.1f}" for h in range(12, 24)]

        hourly_table1 = create_wrapped_table([hours_row1, p99_row1, avg_row1],
                                      col_widths=[0.5*inch] + [0.45*inch]*12,
                                      font_size=7,
                                      repeat_rows=1,
                                      header_text_color=AIRBYTE_NEUTRAL_800)
        hourly_table2 = create_wrapped_table([hours_row2, p99_row2, avg_row2],
                                      col_widths=[0.5*inch] + [0.45*inch]*12,
                                      font_size=7,
                                      repeat_rows=1,
                                      header_text_color=AIRBYTE_NEUTRAL_800)

        # Base table style
        base_style = [
            ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_COOL_200),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, -1), 7),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('TOPPADDING', (0, 0), (-1, -1), 4),
        ]

        # Add color-coding for hours 0-11 (columns 1-12, row 1 is P99)
        style1 = list(base_style)
        for h in range(0, 12):
            col = h + 1  # Column index (0 is label)
            if h in peak_hours:
                style1.append(('BACKGROUND', (col, 1), (col, 1), AIRBYTE_PEACH_100))  # Peak = orange/peach
            elif h in quiet_hours:
                style1.append(('BACKGROUND', (col, 1), (col, 1), AIRBYTE_GREEN_100))  # Quiet = green

        # Add color-coding for hours 12-23 (columns 1-12, row 1 is P99)
        style2 = list(base_style)
        for h in range(12, 24):
            col = h - 12 + 1  # Column index
            if h in peak_hours:
                style2.append(('BACKGROUND', (col, 1), (col, 1), AIRBYTE_PEACH_100))
            elif h in quiet_hours:
                style2.append(('BACKGROUND', (col, 1), (col, 1), AIRBYTE_GREEN_100))

        hourly_table1.setStyle(TableStyle(style1))
        hourly_table2.setStyle(TableStyle(style2))

        story.append(hourly_table1)
        story.append(Spacer(1, 5))
        story.append(hourly_table2)
        story.append(Spacer(1, 8))

        # Add legend
        legend_text = (
            "<b>Color Key:</b> "
            "<font color='#FF6B6B'>■</font> Peak hours (high usage) | "
            "<font color='#4CAF50'>■</font> Quiet hours (good for staggering syncs)"
        )
        story.append(Paragraph(legend_text, ParagraphStyle(
            'Legend', parent=body_style, fontSize=8, textColor=AIRBYTE_COOL_600
        )))
        story.append(Spacer(1, 15))

    # === RECOMMENDATIONS ===
    story.append(Paragraph("Analysis & Recommendations", section_style))

    for i, rec in enumerate(recommendations, 1):
        priority = rec.get("priority", "info")
        if priority == "high":
            rec_color = AIRBYTE_PEACH_100
        elif priority == "medium":
            rec_color = AIRBYTE_GREEN_100
        else:
            rec_color = AIRBYTE_COOL_100

        rec_content = Paragraph(
            f"<b>{i}. {rec['title']}</b><br/><br/>{rec['description']}",
            body_style
        )
        rec_table = Table([[rec_content]], colWidths=[6.5*inch])
        rec_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), rec_color),
            ('LEFTPADDING', (0, 0), (-1, -1), 10),
            ('RIGHTPADDING', (0, 0), (-1, -1), 10),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ]))
        story.append(rec_table)
        story.append(Spacer(1, 10))

    # === GROWTH POTENTIAL ===
    story.append(Spacer(1, 15))
    story.append(Paragraph("Growth Potential", section_style))

    # Calculate growth options
    api_only = int(headroom_workers * 5)
    db_only = int(headroom_workers * 2)
    combo_api = int(headroom_workers * 2.5)
    combo_db = int(headroom_workers * 1)

    growth_content = Paragraph(
        f"Based on current capacity and the Enterprise Data Worker model:"
        f"<br/><br/>"
        f"<b>Available Headroom:</b> {headroom_workers:.1f} workers ({headroom_pct:.0f}% of contracted)"
        f"<br/><br/>"
        f"<b>Growth Options (can be combined):</b>"
        f"<br/>• <b>{api_only}</b> API-only connections (Salesforce, HubSpot, etc.)"
        f"<br/>• <b>{db_only}</b> DB/File-only connections (Postgres, S3, etc.)"
        f"<br/>• <b>Mixed:</b> {combo_api} API + {combo_db} DB (example combination)"
        f"<br/><br/>"
        f"<b>Capacity Formula:</b> (API connections ÷ 5) + (DB connections ÷ 2) = workers used"
        f"<br/><br/>"
        f"<i>Example: 10 API + 4 DB = (10÷5) + (4÷2) = 2 + 2 = 4 workers</i>",
        body_style
    )
    growth_table = Table([[growth_content]], colWidths=[6.5*inch])
    growth_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_GREEN_50),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
    ]))
    story.append(growth_table)

    # === FOOTER ===
    story.append(Spacer(1, 30))
    story.append(Paragraph(
        "This report is generated based on the last 30 days of worker utilization data. "
        "Actual capacity needs may vary based on sync frequency, data volumes, and connector types.",
        ParagraphStyle('Footer', parent=styles['Normal'], fontSize=8, textColor=AIRBYTE_COOL_500)
    ))

    # Build PDF with branded footer
    footer_fn = make_branded_footer(customer_name)
    doc.build(story, onFirstPage=footer_fn, onLaterPages=footer_fn)
    print(f"Generated: {output_path}")


def should_generate_underutilization_report(summary_data: Dict[str, Any]) -> bool:
    """
    Determine if an underutilization report should be generated.

    Returns True for under-utilized customers (< 85% utilization).

    Args:
        summary_data: Parsed summary data

    Returns:
        True if report should be generated
    """
    contracted = summary_data.get("contracted_workers", 0)
    p99 = summary_data.get("p99_workers", 0)

    if contracted <= 0:
        return False

    utilization_pct = (p99 / contracted) * 100
    return utilization_pct < 85  # Under 85% = underutilized
