"""
Prospect Worker Estimation Report Generator

Generates a polished PDF report for prospects that sizes data workers using
time-window-aware queuing logic rather than peak-concurrency estimation.

The key insight: a scheduled batch with a completion deadline is a queuing
problem, not a concurrency problem. You don't need a worker for every sync —
you need enough workers that the queue drains before the deadline.

Report structure:
- Title page with customer name and report metadata
- Executive summary with status box and key metrics table
- Section: The requirement (workload and deadline)
- Section: How Airbyte sizes this (queuing mental model)
- Section: The math (sync-minutes, window, concurrent slots)
- Section: The recommendation (minimum vs headroom)
- Section: Headroom & growth
- Bottom line and next steps

Usage:
    from src.prospect_estimation_report import generate_prospect_report

    report = generate_prospect_report(
        customer_name="<Customer>",
        prepared_by="<SE Name>",
        prepared_by_title="Solutions Engineering, Airbyte",
        total_databases=38,
        critical_syncs=19,
        completion_window_minutes=30,
        avg_sync_duration_minutes=8,
        p90_sync_duration_minutes=10,
        window_start_label="12:00 AM",
        window_end_label="12:30 AM",
        downstream_deadline_label="2:00 AM",
        connector_type="database",
        output_dir=".",
        growth_notes="Loan volume scaling from ~600K toward 1M+",
        cdc_available=True,
    )
"""

import os
from dataclasses import dataclass
from datetime import datetime
from math import ceil
from pathlib import Path
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from src.report_branding import (
    get_divider_line,
    get_logo_header_elements,
    make_branded_footer,
)

from src.queuing_calculator import (
    QueuingEstimateInput,
    QueuingEstimateResult,
    estimate_workers_for_window,
    workers_to_concurrent_slots,
)
from src.report_tables import create_wrapped_table

# =============================================================================
# AIRBYTE BRAND COLORS (matching existing reports)
# =============================================================================

AIRBYTE_BLUE_100 = colors.HexColor('#F5F5FF')
AIRBYTE_BLUE_200 = colors.HexColor('#F1F0FF')
AIRBYTE_BLUE_700 = colors.HexColor('#5F5CFF')
AIRBYTE_INDIGO_500 = colors.HexColor('#282B5C')
AIRBYTE_INDIGO_700 = colors.HexColor('#1A194D')
AIRBYTE_INDIGO_800 = colors.HexColor('#0D0D37')
AIRBYTE_PEACH_100 = colors.HexColor('#FAEBEA')
AIRBYTE_PEACH_150 = colors.HexColor('#FFE6E0')
AIRBYTE_COOL_50 = colors.HexColor('#F9F9FB')
AIRBYTE_COOL_200 = colors.HexColor('#D4D4E3')
AIRBYTE_COOL_500 = colors.HexColor('#8487A4')
AIRBYTE_COOL_700 = colors.HexColor('#5F5F82')
AIRBYTE_NEUTRAL_800 = colors.HexColor('#222222')


# =============================================================================
# STYLES (matching existing report generators)
# =============================================================================

def _get_styles():
    """Build paragraph styles matching existing report templates."""
    base = getSampleStyleSheet()

    title_style = ParagraphStyle(
        'CustomTitle', parent=base['Heading1'],
        fontSize=22, spaceAfter=20, alignment=TA_CENTER,
        textColor=AIRBYTE_INDIGO_800
    )

    subtitle_style = ParagraphStyle(
        'Subtitle', parent=base['Heading2'],
        fontSize=16, alignment=TA_CENTER, spaceAfter=25,
        textColor=AIRBYTE_INDIGO_500
    )

    heading_style = ParagraphStyle(
        'SectionHeader', parent=base['Heading2'],
        fontSize=14, spaceBefore=15, spaceAfter=10,
        textColor=AIRBYTE_INDIGO_500
    )

    body_style = ParagraphStyle(
        'BodyText', parent=base['Normal'],
        fontSize=10, spaceAfter=8, leading=13,
        textColor=AIRBYTE_NEUTRAL_800
    )

    note_style = ParagraphStyle(
        'NoteText', parent=base['Normal'],
        fontSize=10, spaceAfter=10, leading=13,
        textColor=AIRBYTE_COOL_700,
        leftIndent=5, rightIndent=5,
    )

    callout_style = ParagraphStyle(
        'CalloutText', parent=base['Normal'],
        fontSize=10, spaceAfter=6, leading=13,
        textColor=AIRBYTE_INDIGO_500,
        fontName='Helvetica-Bold',
    )

    meta_style = ParagraphStyle(
        'MetaText', parent=base['Normal'],
        fontSize=10, spaceAfter=6, leading=13,
        textColor=AIRBYTE_COOL_500,
    )

    footer_style = ParagraphStyle(
        'FooterText', parent=base['Normal'],
        fontSize=8, leading=10,
        textColor=AIRBYTE_COOL_500,
        alignment=TA_CENTER,
    )

    return {
        'title': title_style,
        'subtitle': subtitle_style,
        'heading': heading_style,
        'body': body_style,
        'note': note_style,
        'callout': callout_style,
        'meta': meta_style,
        'footer': footer_style,
    }


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

def generate_prospect_report(
    customer_name: str,
    prepared_by: str,
    prepared_by_title: str = "Solutions Engineering, Airbyte",
    prepared_for_team: str = "Data Engineering",
    total_databases: int = 38,
    critical_syncs: int = 19,
    completion_window_minutes: float = 30.0,
    avg_sync_duration_minutes: float = 8.0,
    p90_sync_duration_minutes: float = 10.0,
    window_start_label: str = "12:00 AM",
    window_end_label: str = "12:30 AM",
    downstream_deadline_label: str = "2:00 AM",
    connector_type: str = "database",
    output_dir: str = ".",
    growth_notes: str = "",
    cdc_available: bool = False,
    additional_context: str = "",
    date_str: str = "",
) -> Dict[str, Any]:
    """Generate a prospect worker estimation PDF report with queuing logic.

    This is the main entry point. It runs the queuing calculator, then builds
    a narrative PDF report explaining the sizing recommendation.

    Returns:
        Dict with `file_path`, estimation results, and key metrics.
    """
    if not date_str:
        date_str = datetime.utcnow().strftime("%Y-%m-%d")

    # Run queuing estimation
    estimate_input = QueuingEstimateInput(
        total_syncs=total_databases,
        critical_syncs=critical_syncs,
        avg_sync_duration_minutes=avg_sync_duration_minutes,
        p90_sync_duration_minutes=p90_sync_duration_minutes,
        completion_window_minutes=completion_window_minutes,
        connector_type=connector_type,
    )
    result = estimate_workers_for_window(estimate_input)

    # Generate PDF
    os.makedirs(output_dir, exist_ok=True)
    safe_name = "".join(c if c.isalnum() else "_" for c in customer_name)
    timestamp = datetime.utcnow().strftime("%Y%m%d")
    filename = f"{safe_name}_Worker_Estimation_{timestamp}.pdf"
    output_path = os.path.join(output_dir, filename)

    _build_pdf(
        customer_name=customer_name,
        prepared_by=prepared_by,
        prepared_by_title=prepared_by_title,
        prepared_for_team=prepared_for_team,
        total_databases=total_databases,
        critical_syncs=critical_syncs,
        completion_window_minutes=completion_window_minutes,
        avg_sync_duration_minutes=avg_sync_duration_minutes,
        p90_sync_duration_minutes=p90_sync_duration_minutes,
        window_start_label=window_start_label,
        window_end_label=window_end_label,
        downstream_deadline_label=downstream_deadline_label,
        connector_type=connector_type,
        growth_notes=growth_notes,
        cdc_available=cdc_available,
        date_str=date_str,
        result=result,
        output_path=output_path,
    )

    return {
        "success": True,
        "file_path": output_path,
        "customer_name": customer_name,
        "estimation": {
            "recommended_minimum_workers": result.recommended_minimum_workers,
            "recommended_with_headroom": result.recommended_with_headroom,
            "concurrent_slots_minimum": result.concurrent_slots_minimum,
            "drain_time_minimum_minutes": result.estimated_drain_time_minimum,
            "drain_time_headroom_minutes": result.estimated_drain_time_headroom,
            "margin_minimum_minutes": result.margin_minutes_minimum,
            "margin_headroom_minutes": result.margin_minutes_headroom,
        },
        "input_params": result.input_params,
    }


# =============================================================================
# PDF BUILDER
# =============================================================================

def _build_pdf(
    customer_name: str,
    prepared_by: str,
    prepared_by_title: str,
    prepared_for_team: str,
    total_databases: int,
    critical_syncs: int,
    completion_window_minutes: float,
    avg_sync_duration_minutes: float,
    p90_sync_duration_minutes: float,
    window_start_label: str,
    window_end_label: str,
    downstream_deadline_label: str,
    connector_type: str,
    growth_notes: str,
    cdc_available: bool,
    date_str: str,
    result: QueuingEstimateResult,
    output_path: str,
) -> None:
    """Assemble the PDF document matching existing report styling."""
    styles = _get_styles()

    doc = SimpleDocTemplate(
        output_path,
        pagesize=letter,
        leftMargin=0.6 * inch,
        rightMargin=0.6 * inch,
        topMargin=0.6 * inch,
        bottomMargin=0.6 * inch,
    )

    elements = []

    # =========================================================================
    # TITLE PAGE
    # =========================================================================

    # Airbyte logo (full wordmark) at top of page 1
    elements.extend(get_logo_header_elements())
    elements.append(Spacer(1, 4))

    elements.append(Paragraph(customer_name, styles['title']))
    elements.append(Paragraph("Data Worker Estimation Report", styles['subtitle']))

    # Colored divider line
    elements.append(get_divider_line())
    elements.append(Spacer(1, 12))

    elements.append(Paragraph(f"Report Date: {date_str}", styles['meta']))
    elements.append(Paragraph(f"Prepared by: {prepared_by}, {prepared_by_title}", styles['meta']))
    elements.append(Paragraph(f"Prepared for: {customer_name} \u2014 {prepared_for_team}", styles['meta']))
    elements.append(Spacer(1, 15))

    # Status box (matching existing report pattern)
    status_text = (
        f"<b>Recommendation: {result.recommended_minimum_workers} Data Workers "
        f"(minimum) / {result.recommended_with_headroom} Data Workers (with headroom)</b><br/>"
        f"Critical syncs: {critical_syncs} \u00b7 "
        f"Completion window: {int(completion_window_minutes)} min \u00b7 "
        f"Avg sync duration: ~{int(avg_sync_duration_minutes)} min<br/>"
        f"Drain time at minimum: ~{result.estimated_drain_time_minimum} min \u00b7 "
        f"Margin: ~{result.margin_minutes_minimum} min"
    )
    status_para = Paragraph(status_text, ParagraphStyle(
        'StatusBox', parent=styles['body'],
        leftIndent=5, rightIndent=5, alignment=TA_CENTER,
    ))
    status_table = Table([[status_para]], colWidths=[6.5 * inch])
    status_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    elements.append(status_table)
    elements.append(Spacer(1, 15))

    # Data Worker Methodology Note (matching metabase_report_generator pattern)
    slots_per_worker = 2 if connector_type == "database" else 5
    method_text = (
        "<b>How Data Workers Are Sized</b><br/>"
        f"Each Data Worker provides <b>{slots_per_worker} concurrent "
        f"{connector_type} sync slots</b>. "
        "This estimation uses a throughput model: rather than sizing for all syncs "
        "running simultaneously (peak concurrency), it calculates the minimum workers "
        "needed to drain all critical syncs within the allotted time window. "
        "This approach yields a lower, more cost-efficient recommendation for "
        "batch workloads with a defined completion deadline."
    )
    method_para = Paragraph(method_text, ParagraphStyle(
        'MethodNote', parent=styles['body'],
        fontSize=9.5, leading=12.5,
        textColor=AIRBYTE_COOL_700,
        leftIndent=5, rightIndent=5,
    ))
    method_table = Table([[method_para]], colWidths=[6.5 * inch])
    method_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_COOL_50),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 12),
        ('RIGHTPADDING', (0, 0), (-1, -1), 12),
        ('BOX', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
    ]))
    elements.append(method_table)
    elements.append(Spacer(1, 15))

    # Key Metrics Table
    elements.append(Paragraph("Key Metrics", styles['heading']))

    metrics_data = [
        ["Metric", "Value", "Notes"],
        ["Total Databases", str(total_databases), "All connections at production"],
        ["Critical Syncs (in-window)", str(critical_syncs), "Must land within the completion window"],
        ["Completion Window", f"{int(completion_window_minutes)} min",
         f"{window_start_label} to {window_end_label}"],
        ["Avg Sync Duration", f"~{int(avg_sync_duration_minutes)} min", "Based on trial job history"],
        ["P90 Sync Duration", f"~{int(p90_sync_duration_minutes)} min", "Worst-case planning"],
        ["Connector Type", connector_type.capitalize(), "Determines slots per worker"],
        ["Recommended Minimum", f"{result.recommended_minimum_workers} workers",
         f"~{result.concurrent_slots_minimum} sustained concurrent syncs"],
        ["Recommended w/ Headroom", f"{result.recommended_with_headroom} workers",
         f"~{result.concurrent_slots_headroom} sustained concurrent syncs"],
    ]

    metrics_table = create_wrapped_table(
        metrics_data, col_widths=[2.2 * inch, 1.5 * inch, 3.0 * inch], font_size=10,
    )
    metrics_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'LEFT'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, AIRBYTE_COOL_50]),
        # Highlight recommendation rows
        ('BACKGROUND', (0, 7), (-1, 8), AIRBYTE_BLUE_100),
    ]))
    elements.append(metrics_table)

    # =========================================================================
    # SECTION: The Nightly Requirement
    # =========================================================================
    elements.append(PageBreak())
    elements.append(Paragraph("The Nightly Requirement", styles['heading']))

    elements.append(Paragraph(
        f"{customer_name} replicates roughly {total_databases} databases into Snowflake "
        f"\u2014 one sync per database. About {critical_syncs} are critical and must land "
        f"inside a {int(completion_window_minutes)}-minute window: the run starts at "
        f"{window_start_label}, and the critical data needs to be in Snowflake by roughly "
        f"{window_end_label}. Downstream Snowflake transforms then finish by "
        f"{downstream_deadline_label}. The other ~{total_databases - critical_syncs} "
        f"databases are non-critical and run off-peak.",
        styles['body'],
    ))
    elements.append(Spacer(1, 8))

    # Requirement summary table
    req_data = [
        ["Parameter", "Value"],
        ["Total databases at production", str(total_databases)],
        ["Critical databases (in-window)", str(critical_syncs)],
        [f"Landing window ({window_start_label} to ~{window_end_label})",
         f"{int(completion_window_minutes)} min"],
        ["Downstream deadline", downstream_deadline_label],
        ["Non-critical (off-peak)", f"~{total_databases - critical_syncs}"],
    ]
    req_table = create_wrapped_table(
        req_data, col_widths=[3.5 * inch, 2.5 * inch], font_size=10,
    )
    req_table.setStyle(TableStyle([
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
    elements.append(req_table)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(
        f"<b>The deadline that drives sizing is the {int(completion_window_minutes)}-minute "
        f"critical window \u2014 not the full run to {downstream_deadline_label}.</b>",
        styles['body'],
    ))

    # =========================================================================
    # SECTION: How Airbyte Sizes This
    # =========================================================================
    elements.append(Spacer(1, 10))
    elements.append(Paragraph("How Airbyte Sizes This", styles['heading']))

    # When queuing helps callout
    queuing_applies_text = (
        "<b>Why Queuing Applies Here</b><br/>"
        "Queue-based sizing reduces worker needs when two conditions are met: "
        "(1) many syncs must run in a defined time window, and "
        "(2) the syncs are short enough that they can take turns sharing a smaller pool "
        "and still all finish before the deadline. "
        f"This workload has both \u2014 {critical_syncs} syncs in a "
        f"{int(completion_window_minutes)}-minute window, each taking "
        f"~{int(avg_sync_duration_minutes)} minutes \u2014 so queuing materially "
        "lowers the required worker count. Without a tight window (e.g., syncs spread "
        "across the day with no shared deadline), standard peak-concurrency sizing applies "
        "and queuing adds no benefit."
    )
    qa_para = Paragraph(queuing_applies_text, ParagraphStyle(
        'QueuingApplies', parent=styles['body'],
        leftIndent=5, rightIndent=5,
    ))
    qa_table = Table([[qa_para]], colWidths=[6.5 * inch])
    qa_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_PEACH_100),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    elements.append(qa_table)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(
        "A data worker is a unit of concurrent capacity. You pay for how many syncs run "
        "at the same instant \u2014 not for how many databases you have. That distinction "
        "is the entire sizing lever.",
        styles['body'],
    ))
    elements.append(Paragraph(
        f"Each sync runs in a few minutes (about {int(avg_sync_duration_minutes)} minutes "
        f"at today\u2019s volumes), so they don\u2019t all need to run at once. Airbyte queues "
        "the batch and runs a steady handful in parallel; as each sync finishes, the next "
        "one starts. Peak concurrency is capped by what you buy, and the queue absorbs "
        "the rest \u2014 draining the batch steadily across the window.",
        styles['body'],
    ))
    elements.append(Spacer(1, 8))

    # Mental model callout box
    mental_model_text = (
        "<b>The Mental Model</b><br/>"
        "It\u2019s a checkout line, not a stadium turnstile. You don\u2019t open a lane for "
        "every shopper who will arrive tonight \u2014 you run enough lanes that the line "
        "clears before the doors close. Workers are the lanes; the "
        f"{int(completion_window_minutes)}-minute window is closing time."
    )
    mm_para = Paragraph(mental_model_text, ParagraphStyle(
        'MentalModel', parent=styles['body'],
        leftIndent=5, rightIndent=5,
    ))
    mm_table = Table([[mm_para]], colWidths=[6.5 * inch])
    mm_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    elements.append(mm_table)
    elements.append(Spacer(1, 10))

    # Queue flow diagram as table
    queue_data = [
        ["QUEUED", "\u2192", "RUNNING", "\u2192", "LANDED"],
        [f"{critical_syncs} syncs", "",
         f"~{result.concurrent_slots_minimum} at a time", "",
         f"by {window_end_label}"],
    ]
    queue_table = Table(
        queue_data,
        colWidths=[1.4 * inch, 0.4 * inch, 1.8 * inch, 0.4 * inch, 1.4 * inch],
    )
    queue_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('FONTNAME', (0, 1), (-1, 1), 'Helvetica'),
        ('FONTSIZE', (0, 1), (-1, 1), 9),
        ('TEXTCOLOR', (0, 0), (-1, -1), AIRBYTE_INDIGO_500),
        ('TEXTCOLOR', (1, 0), (1, -1), AIRBYTE_BLUE_700),
        ('TEXTCOLOR', (3, 0), (3, -1), AIRBYTE_BLUE_700),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_COOL_50),
        ('BOX', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
    ]))
    elements.append(queue_table)

    # =========================================================================
    # SECTION: What the Batch Actually Needs (The Math)
    # =========================================================================
    elements.append(PageBreak())
    elements.append(Paragraph("What the Batch Actually Needs", styles['heading']))

    sync_minutes = critical_syncs * avg_sync_duration_minutes
    min_concurrent = ceil(sync_minutes / completion_window_minutes)

    elements.append(Paragraph(
        f"{critical_syncs} critical syncs at about {int(avg_sync_duration_minutes)} minutes "
        f"each \u2014 trial job history informs this figure \u2014 is roughly "
        f"{int(sync_minutes)} sync-minutes of work to clear inside a "
        f"{int(completion_window_minutes)}-minute window.",
        styles['body'],
    ))
    elements.append(Spacer(1, 8))

    # Step-by-step math box
    math_text = (
        f"<b>01</b>&nbsp;&nbsp;&nbsp;{critical_syncs} syncs \u00d7 "
        f"~{int(avg_sync_duration_minutes)} min = "
        f"{int(sync_minutes)} sync-minutes<br/>"
        f"<b>02</b>&nbsp;&nbsp;&nbsp;{int(sync_minutes)} sync-minutes \u00f7 "
        f"{int(completion_window_minutes)}-min window \u2248 "
        f"~{min_concurrent} syncs running continuously<br/>"
        f"<b>03</b>&nbsp;&nbsp;&nbsp;~{result.concurrent_slots_minimum} at a time "
        f"({result.recommended_minimum_workers} workers) clears all "
        f"{critical_syncs} in ~{int(result.estimated_drain_time_minimum)} min "
        f"\u2014 inside the window"
    )
    math_para = Paragraph(math_text, ParagraphStyle(
        'MathBox', parent=styles['body'],
        leftIndent=5, rightIndent=5,
    ))
    math_table = Table([[math_para]], colWidths=[6.5 * inch])
    math_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    elements.append(math_table)
    elements.append(Spacer(1, 15))

    # Scenario table
    elements.append(Paragraph("Scenario Analysis \u2014 Workers vs. Drain Time", styles['heading']))

    scenario_header = ["Workers", "Concurrent Slots", "Waves",
                       "Drain Time (avg)", "Drain Time (P90)", "Fits in Window?"]
    scenario_data = [scenario_header]

    for s in result.scenarios:
        fits_label = "No"
        if s["fits_in_window_avg"] and s["fits_in_window_p90"]:
            fits_label = "Yes (both)"
        elif s["fits_in_window_avg"]:
            fits_label = "Avg only"

        scenario_data.append([
            str(s["workers"]),
            str(s["concurrent_slots"]),
            str(s["waves"]),
            f"{s['drain_time_avg_minutes']} min",
            f"{s['drain_time_p90_minutes']} min",
            fits_label,
        ])

    scenario_table = create_wrapped_table(
        scenario_data,
        col_widths=[0.8 * inch, 1.1 * inch, 0.7 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch],
        font_size=9,
    )
    scenario_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 9),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, AIRBYTE_COOL_50]),
        # Highlight the minimum recommendation row
        ('BACKGROUND', (0, result.recommended_minimum_workers),
         (-1, result.recommended_minimum_workers), AIRBYTE_BLUE_100),
        ('BACKGROUND', (0, result.recommended_with_headroom),
         (-1, result.recommended_with_headroom), AIRBYTE_BLUE_200),
    ]))
    elements.append(scenario_table)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(
        f"{result.concurrent_slots_minimum} concurrent syncs "
        f"({result.recommended_minimum_workers} workers) clear all "
        f"{critical_syncs} inside the window, landing with "
        f"~{int(result.margin_minutes_minimum)} minutes to spare at average sync durations. "
        f"A {_ordinal(result.recommended_with_headroom)} worker adds more lanes and pulls "
        f"the finish back to ~{int(result.estimated_drain_time_headroom)} minutes.",
        styles['body'],
    ))

    # =========================================================================
    # SECTION: The Recommendation
    # =========================================================================
    elements.append(Spacer(1, 10))
    elements.append(Paragraph(
        f"The Recommendation: {result.recommended_minimum_workers} vs "
        f"{result.recommended_with_headroom} Workers",
        styles['heading'],
    ))

    elements.append(Paragraph(
        f"Both options clear the batch inside the window at "
        f"~{int(avg_sync_duration_minutes)}-minute syncs \u2014 they differ in how much "
        "room they leave for variance and growth.",
        styles['body'],
    ))
    elements.append(Spacer(1, 8))

    # Recommendation comparison table
    rec_data = [
        ["", "Minimum", "With Headroom"],
        ["Workers", str(result.recommended_minimum_workers),
         str(result.recommended_with_headroom)],
        ["Concurrent Slots", str(result.concurrent_slots_minimum),
         str(result.concurrent_slots_headroom)],
        ["Drain Time (avg)", f"~{int(result.estimated_drain_time_minimum)} min",
         f"~{int(result.estimated_drain_time_headroom)} min"],
        ["Margin in Window", f"~{int(result.margin_minutes_minimum)} min",
         f"~{int(result.margin_minutes_headroom)} min"],
        ["Fits at P90?",
         "No" if not result.scenarios[result.recommended_minimum_workers - 1]["fits_in_window_p90"] else "Yes",
         "Yes"],
    ]
    rec_table = create_wrapped_table(
        rec_data, col_widths=[2.0 * inch, 2.0 * inch, 2.0 * inch], font_size=10,
    )
    rec_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('BACKGROUND', (1, 1), (1, -1), AIRBYTE_COOL_50),
        ('BACKGROUND', (2, 1), (2, -1), AIRBYTE_BLUE_100),
    ]))
    elements.append(rec_table)
    elements.append(Spacer(1, 10))

    elements.append(Paragraph(
        f"<b>{result.recommended_minimum_workers} is the floor</b> that clears the window; "
        f"<b>{result.recommended_with_headroom} is the cushion</b> \u2014 "
        "the extra worker buys margin for retries, slow nights, and growth.",
        styles['body'],
    ))

    # =========================================================================
    # SECTION: Headroom & Growth
    # =========================================================================
    elements.append(PageBreak())
    elements.append(Paragraph("Headroom & Growth", styles['heading']))

    elements.append(Paragraph(
        f"{result.recommended_minimum_workers} workers holds the line at today\u2019s "
        f"volumes; the move to {result.recommended_with_headroom} is the lever for growth.",
        styles['body'],
    ))
    elements.append(Spacer(1, 8))

    if growth_notes:
        growth_text = (
            f"<b>Growth:</b> {growth_notes}. That is the trigger to step from "
            f"{result.recommended_minimum_workers} workers to "
            f"{result.recommended_with_headroom} \u2014 not a resize of the window itself."
        )
        growth_para = Paragraph(growth_text, ParagraphStyle(
            'GrowthBox', parent=styles['body'], leftIndent=5, rightIndent=5,
        ))
        growth_table = Table([[growth_para]], colWidths=[6.5 * inch])
        growth_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_PEACH_100),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ]))
        elements.append(growth_table)
        elements.append(Spacer(1, 10))

    if cdc_available:
        cdc_text = (
            "<b>CDC Keeps Syncs Bounded:</b> With change-data-capture, each sync moves "
            "only what changed \u2014 so duration tracks change volume, not table size. "
            f"That keeps sync times from ballooning as data grows, and keeps the "
            f"{result.recommended_minimum_workers}-worker floor viable longer."
        )
        cdc_para = Paragraph(cdc_text, ParagraphStyle(
            'CdcBox', parent=styles['body'], leftIndent=5, rightIndent=5,
        ))
        cdc_table = Table([[cdc_para]], colWidths=[6.5 * inch])
        cdc_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('LEFTPADDING', (0, 0), (-1, -1), 15),
            ('RIGHTPADDING', (0, 0), (-1, -1), 15),
        ]))
        elements.append(cdc_table)
        elements.append(Spacer(1, 10))

    # Worth stating plainly
    elements.append(Paragraph(
        "These are estimates for a pre-production workload, grounded in trial job history. "
        "Once in production, actual usage is the source of truth and we will re-check against "
        f"it. Sync duration is the whole ballgame: at ~{int(avg_sync_duration_minutes)} "
        f"minutes, {result.recommended_minimum_workers} workers is the floor; if durations "
        f"climb past ~{int(p90_sync_duration_minutes)} minutes, "
        f"{result.recommended_with_headroom} becomes necessary.",
        styles['body'],
    ))

    # =========================================================================
    # SECTION: Bottom Line & Next Steps
    # =========================================================================
    elements.append(Spacer(1, 15))
    elements.append(Paragraph("Bottom Line & Next Steps", styles['heading']))

    bottom_text = (
        f"<b>{result.recommended_minimum_workers} workers is the floor</b> that clears "
        f"tonight\u2019s batch at realistic ~{int(avg_sync_duration_minutes)}-minute syncs; "
        f"<b>{result.recommended_with_headroom} is the headroom</b> for growth and "
        "worst-case nights. It\u2019s a queuing problem, not a database count \u2014 which "
        f"is why the number is {result.recommended_minimum_workers} to "
        f"{result.recommended_with_headroom}, not one per database."
    )
    bottom_para = Paragraph(bottom_text, ParagraphStyle(
        'BottomBox', parent=styles['body'], leftIndent=5, rightIndent=5,
    ))
    bottom_table = Table([[bottom_para]], colWidths=[6.5 * inch])
    bottom_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), AIRBYTE_BLUE_100),
        ('TOPPADDING', (0, 0), (-1, -1), 12),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
        ('LEFTPADDING', (0, 0), (-1, -1), 15),
        ('RIGHTPADDING', (0, 0), (-1, -1), 15),
    ]))
    elements.append(bottom_table)
    elements.append(Spacer(1, 15))

    # Next steps
    elements.append(Paragraph("<b>Next Steps:</b>", styles['body']))

    next_steps = [
        f"Start the POV at {result.recommended_minimum_workers} workers \u2014 the floor "
        f"that clears the batch; step to {result.recommended_with_headroom} if trial "
        "durations run hot.",
        f"Lock the critical list \u2014 confirm which ~{critical_syncs} databases must "
        "land in-window so the rest schedule off-peak.",
        f"Validate sync duration in the trial \u2014 confirm the "
        f"~{int(avg_sync_duration_minutes)}-minute figure end to end.",
        "Re-check against production usage once live, and adjust if the data says so.",
    ]

    steps_data = [["#", "Action"]]
    for i, step in enumerate(next_steps, 1):
        steps_data.append([str(i), step])

    steps_table = create_wrapped_table(
        steps_data, col_widths=[0.4 * inch, 6.0 * inch], font_size=10,
    )
    steps_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), AIRBYTE_INDIGO_700),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('TOPPADDING', (0, 0), (-1, -1), 6),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ('GRID', (0, 0), (-1, -1), 0.5, AIRBYTE_COOL_200),
        ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, AIRBYTE_COOL_50]),
        ('ALIGN', (0, 0), (0, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    elements.append(steps_table)
    elements.append(Spacer(1, 20))

    # Sign-off
    elements.append(Paragraph(
        "Happy to walk through any of this live and tune the numbers against your trial data \u2014",
        styles['body'],
    ))
    elements.append(Paragraph(
        f"<b>{prepared_by}</b>, {prepared_by_title}.",
        styles['body'],
    ))

    # Build with branded footer (icon + text + page number)
    footer_fn = make_branded_footer(customer_name, date_str)
    doc.build(elements, onFirstPage=footer_fn, onLaterPages=footer_fn)


# =============================================================================
# UTILITIES
# =============================================================================

def _ordinal(n: int) -> str:
    """Return ordinal string for a number (e.g., 4 -> 'fourth')."""
    ordinals = {1: "first", 2: "second", 3: "third", 4: "fourth",
                5: "fifth", 6: "sixth", 7: "seventh", 8: "eighth"}
    return ordinals.get(n, f"{n}th")
