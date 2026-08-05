#!/usr/bin/env python3
"""
Quartz Cron Expression Generator

This module provides utilities for generating and formatting 6-value Quartz cron
expressions for Airbyte connection scheduling.

Quartz Cron Format (6 values):
    seconds minutes hours day-of-month month day-of-week
    Example: "0 0 3 * * ?" = Every day at 03:00:00 UTC

Usage:
    from src.cron_generator import (
        generate_quartz_cron,
        format_cron_with_explanation,
        get_recommended_schedule_times,
    )

    # Generate cron for 3:00 AM UTC
    cron = generate_quartz_cron(hour=3, minute=0)
    # Returns: "0 0 3 * * ?"

    # Get formatted explanation
    explanation = format_cron_with_explanation(cron)
"""

from typing import Dict, List, Optional, Tuple


# Common timezone offsets from UTC (for reference)
TIMEZONE_OFFSETS = {
    "UTC": 0,
    "US/Pacific": -8,      # PST (or -7 for PDT)
    "US/Mountain": -7,     # MST (or -6 for MDT)
    "US/Central": -6,      # CST (or -5 for CDT)
    "US/Eastern": -5,      # EST (or -4 for EDT)
    "Europe/London": 0,    # GMT (or +1 for BST)
    "Europe/Paris": 1,     # CET (or +2 for CEST)
    "Europe/Berlin": 1,    # CET (or +2 for CEST)
    "Asia/Tokyo": 9,
    "Asia/Singapore": 8,
    "Australia/Sydney": 11,  # AEDT (or +10 for AEST)
}


def generate_quartz_cron(
    hour: int,
    minute: int = 0,
    second: int = 0,
    day_of_month: str = "*",
    month: str = "*",
    day_of_week: str = "?"
) -> str:
    """
    Generate a 6-value Quartz cron expression.

    Quartz cron format: seconds minutes hours day-of-month month day-of-week

    Args:
        hour: Hour (0-23)
        minute: Minute (0-59), default 0
        second: Second (0-59), default 0
        day_of_month: Day of month (1-31, *, ?), default "*"
        month: Month (1-12, *, JAN-DEC), default "*"
        day_of_week: Day of week (1-7, *, ?, SUN-SAT), default "?"

    Returns:
        Quartz cron expression string

    Note:
        Either day_of_month or day_of_week must be "?" but not both.
        Airbyte uses "?" for day_of_week by default.
    """
    # Validate inputs
    if not 0 <= hour <= 23:
        raise ValueError(f"Hour must be 0-23, got {hour}")
    if not 0 <= minute <= 59:
        raise ValueError(f"Minute must be 0-59, got {minute}")
    if not 0 <= second <= 59:
        raise ValueError(f"Second must be 0-59, got {second}")

    return f"{second} {minute} {hour} {day_of_month} {month} {day_of_week}"


def generate_hourly_cron(minute: int = 0) -> str:
    """
    Generate a cron expression for hourly execution.

    Args:
        minute: Minute of each hour (0-59)

    Returns:
        Quartz cron expression for hourly execution
    """
    return generate_quartz_cron(hour="*", minute=minute, day_of_month="*")


def generate_daily_cron(hour: int, minute: int = 0) -> str:
    """
    Generate a cron expression for daily execution at a specific time.

    Args:
        hour: Hour of day (0-23 UTC)
        minute: Minute (0-59)

    Returns:
        Quartz cron expression for daily execution
    """
    return generate_quartz_cron(hour=hour, minute=minute)


def generate_cron_alternatives(
    recommended_hour: int,
    count: int = 3,
    include_half_hours: bool = True
) -> List[str]:
    """
    Generate alternative cron expressions around a recommended hour.

    This provides options for AEs to choose from when rescheduling connections.

    Args:
        recommended_hour: The primary recommended hour (0-23 UTC)
        count: Number of alternatives to generate
        include_half_hours: Whether to include :30 options

    Returns:
        List of alternative cron expressions
    """
    alternatives = []

    # Start with the recommended hour
    alternatives.append(generate_daily_cron(recommended_hour, 0))

    if include_half_hours:
        alternatives.append(generate_daily_cron(recommended_hour, 30))

    # Add adjacent hours
    for offset in [1, -1, 2, -2]:
        if len(alternatives) >= count:
            break
        adj_hour = (recommended_hour + offset) % 24
        alternatives.append(generate_daily_cron(adj_hour, 0))
        if include_half_hours and len(alternatives) < count:
            alternatives.append(generate_daily_cron(adj_hour, 30))

    return alternatives[:count]


def format_cron_with_explanation(cron_expression: str) -> Dict[str, str]:
    """
    Parse a cron expression and provide human-readable explanation.

    Args:
        cron_expression: 6-value Quartz cron expression

    Returns:
        Dictionary with cron parts and explanation
    """
    parts = cron_expression.split()
    if len(parts) != 6:
        return {
            "error": f"Invalid cron expression: expected 6 parts, got {len(parts)}",
            "expression": cron_expression,
        }

    second, minute, hour, day_of_month, month, day_of_week = parts

    # Build human-readable description
    time_desc = _format_time_part(hour, minute, second)
    day_desc = _format_day_part(day_of_month, month, day_of_week)

    return {
        "expression": cron_expression,
        "parts": {
            "second": second,
            "minute": minute,
            "hour": hour,
            "day_of_month": day_of_month,
            "month": month,
            "day_of_week": day_of_week,
        },
        "description": f"{time_desc} {day_desc}".strip(),
        "breakdown": (
            f"  {second} - Second: {_explain_cron_field(second, 'second')}\n"
            f"  {minute} - Minute: {_explain_cron_field(minute, 'minute')}\n"
            f"  {hour} - Hour: {_explain_cron_field(hour, 'hour')}\n"
            f"  {day_of_month} - Day of month: {_explain_cron_field(day_of_month, 'day_of_month')}\n"
            f"  {month} - Month: {_explain_cron_field(month, 'month')}\n"
            f"  {day_of_week} - Day of week: {_explain_cron_field(day_of_week, 'day_of_week')}"
        ),
    }


def _format_time_part(hour: str, minute: str, second: str) -> str:
    """Format the time portion of a cron expression."""
    if hour == "*":
        if minute == "*":
            return "Every minute"
        elif minute == "0":
            return "Every hour at :00"
        else:
            return f"Every hour at :{minute.zfill(2)}"

    hour_int = int(hour)
    minute_int = int(minute) if minute != "*" else 0

    # Format as 24-hour time
    time_str = f"{hour_int:02d}:{minute_int:02d}"

    # Add 12-hour format
    if hour_int == 0:
        time_12h = f"12:{minute_int:02d} AM"
    elif hour_int < 12:
        time_12h = f"{hour_int}:{minute_int:02d} AM"
    elif hour_int == 12:
        time_12h = f"12:{minute_int:02d} PM"
    else:
        time_12h = f"{hour_int - 12}:{minute_int:02d} PM"

    return f"At {time_str} UTC ({time_12h})"


def _format_day_part(day_of_month: str, month: str, day_of_week: str) -> str:
    """Format the day portion of a cron expression."""
    parts = []

    if day_of_month not in ("*", "?"):
        parts.append(f"on day {day_of_month}")

    if month not in ("*", "?"):
        parts.append(f"in month {month}")

    if day_of_week not in ("*", "?"):
        day_names = {
            "1": "Sunday", "SUN": "Sunday",
            "2": "Monday", "MON": "Monday",
            "3": "Tuesday", "TUE": "Tuesday",
            "4": "Wednesday", "WED": "Wednesday",
            "5": "Thursday", "THU": "Thursday",
            "6": "Friday", "FRI": "Friday",
            "7": "Saturday", "SAT": "Saturday",
        }
        day_name = day_names.get(day_of_week.upper(), day_of_week)
        parts.append(f"on {day_name}")

    if not parts:
        return "every day"

    return ", ".join(parts)


def _explain_cron_field(value: str, field_type: str) -> str:
    """Provide explanation for a single cron field."""
    if value == "*":
        return "Every " + field_type.replace("_", " ")
    elif value == "?":
        return "No specific value"
    elif value.isdigit():
        if field_type == "hour":
            return f"{int(value):02d}:00 UTC"
        elif field_type == "minute":
            return f":{int(value):02d}"
        elif field_type == "second":
            return f":{int(value):02d}"
        else:
            return value
    else:
        return value


def get_recommended_schedule_times(
    quiet_hours: List[int],
    existing_schedules: Optional[List[int]] = None,
    stagger_minutes: int = 15
) -> List[Dict[str, any]]:
    """
    Get recommended schedule times based on quiet hours.

    Distributes recommendations across quiet hours to avoid clustering.

    Args:
        quiet_hours: List of quiet hour integers (0-23 UTC)
        existing_schedules: Optional list of hours already in use
        stagger_minutes: Minutes to stagger between recommendations

    Returns:
        List of recommended schedule options with cron expressions
    """
    if not quiet_hours:
        # Default to overnight hours if no data
        quiet_hours = [2, 3, 4, 5, 6]

    existing_schedules = existing_schedules or []

    recommendations = []
    minute_offset = 0

    for hour in sorted(quiet_hours):
        # Skip hours that are heavily used
        if existing_schedules.count(hour) >= 3:
            continue

        recommendations.append({
            "hour": hour,
            "minute": minute_offset,
            "cron": generate_daily_cron(hour, minute_offset),
            "description": f"{hour:02d}:{minute_offset:02d} UTC",
            "priority": "primary" if hour == quiet_hours[0] else "alternative",
        })

        # Stagger the minute for next recommendation
        minute_offset = (minute_offset + stagger_minutes) % 60

    return recommendations


def convert_utc_to_timezone(hour_utc: int, timezone: str) -> Tuple[int, str]:
    """
    Convert UTC hour to a specific timezone.

    Args:
        hour_utc: Hour in UTC (0-23)
        timezone: Timezone name from TIMEZONE_OFFSETS

    Returns:
        Tuple of (local_hour, formatted_time_string)
    """
    offset = TIMEZONE_OFFSETS.get(timezone, 0)
    local_hour = (hour_utc + offset) % 24

    # Handle day change
    day_indicator = ""
    if hour_utc + offset < 0:
        day_indicator = " (previous day)"
    elif hour_utc + offset >= 24:
        day_indicator = " (next day)"

    return local_hour, f"{local_hour:02d}:00{day_indicator}"


def format_timezone_table(hour_utc: int, minute: int = 0) -> str:
    """
    Generate a timezone conversion table for a given UTC time.

    Args:
        hour_utc: Hour in UTC (0-23)
        minute: Minute (0-59)

    Returns:
        Formatted string showing time in common timezones
    """
    lines = [f"UTC Time: {hour_utc:02d}:{minute:02d}"]
    lines.append("-" * 40)

    for tz_name, offset in sorted(TIMEZONE_OFFSETS.items(), key=lambda x: x[1]):
        local_hour = (hour_utc + offset) % 24
        day_note = ""
        if hour_utc + offset < 0:
            day_note = " (prev day)"
        elif hour_utc + offset >= 24:
            day_note = " (next day)"

        # Format AM/PM
        if local_hour == 0:
            time_12h = f"12:{minute:02d} AM"
        elif local_hour < 12:
            time_12h = f"{local_hour}:{minute:02d} AM"
        elif local_hour == 12:
            time_12h = f"12:{minute:02d} PM"
        else:
            time_12h = f"{local_hour - 12}:{minute:02d} PM"

        lines.append(f"  {tz_name:20} {local_hour:02d}:{minute:02d} ({time_12h}){day_note}")

    return "\n".join(lines)


def validate_quartz_cron(cron_expression: str) -> Tuple[bool, str]:
    """
    Validate a Quartz cron expression.

    Args:
        cron_expression: The cron expression to validate

    Returns:
        Tuple of (is_valid, error_message)
    """
    parts = cron_expression.split()

    if len(parts) != 6:
        return False, f"Expected 6 parts, got {len(parts)}"

    second, minute, hour, day_of_month, month, day_of_week = parts

    # Validate second (0-59 or *)
    if second != "*" and not (second.isdigit() and 0 <= int(second) <= 59):
        return False, f"Invalid second: {second}"

    # Validate minute (0-59 or *)
    if minute != "*" and not (minute.isdigit() and 0 <= int(minute) <= 59):
        return False, f"Invalid minute: {minute}"

    # Validate hour (0-23 or *)
    if hour != "*" and not (hour.isdigit() and 0 <= int(hour) <= 23):
        return False, f"Invalid hour: {hour}"

    # Validate day_of_month (1-31, *, or ?)
    if day_of_month not in ("*", "?") and not (day_of_month.isdigit() and 1 <= int(day_of_month) <= 31):
        return False, f"Invalid day_of_month: {day_of_month}"

    # Validate month (1-12, *, or month names)
    valid_months = ["*", "1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11", "12",
                    "JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    if month.upper() not in valid_months:
        return False, f"Invalid month: {month}"

    # Validate day_of_week (1-7, *, ?, or day names)
    valid_days = ["*", "?", "1", "2", "3", "4", "5", "6", "7",
                  "SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"]
    if day_of_week.upper() not in valid_days:
        return False, f"Invalid day_of_week: {day_of_week}"

    # Ensure either day_of_month or day_of_week is ?
    if day_of_month == "?" and day_of_week == "?":
        return False, "Both day_of_month and day_of_week cannot be '?'"

    return True, "Valid cron expression"


def parse_existing_cron(cron_expression: Optional[str]) -> Dict[str, any]:
    """
    Parse an existing cron expression to extract scheduling details.

    Args:
        cron_expression: The cron expression to parse (may be None)

    Returns:
        Dictionary with parsed schedule details
    """
    if not cron_expression:
        return {
            "has_schedule": False,
            "schedule_type": "manual",
            "description": "Manual trigger only",
        }

    is_valid, error = validate_quartz_cron(cron_expression)
    if not is_valid:
        return {
            "has_schedule": True,
            "schedule_type": "unknown",
            "error": error,
            "raw": cron_expression,
        }

    parts = cron_expression.split()
    second, minute, hour, day_of_month, month, day_of_week = parts

    result = {
        "has_schedule": True,
        "raw": cron_expression,
    }

    # Determine schedule type
    if hour == "*":
        result["schedule_type"] = "hourly"
        result["description"] = f"Every hour at :{minute.zfill(2)}"
    elif day_of_week not in ("*", "?"):
        result["schedule_type"] = "weekly"
        result["hour"] = int(hour)
        result["minute"] = int(minute)
        result["day_of_week"] = day_of_week
    else:
        result["schedule_type"] = "daily"
        result["hour"] = int(hour)
        result["minute"] = int(minute)
        result["description"] = f"Daily at {int(hour):02d}:{int(minute):02d} UTC"

    return result
