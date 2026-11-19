"""Local wall-clock recurrence with stable UTC occurrence identifiers."""

from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


FREQUENCIES = ("none", "days", "weeks", "months")


def parse_local(value):
    if not isinstance(value, str) or len(value) != 16 or value[10] != "T":
        raise ValueError("Enter a local date and time.")
    try:
        local = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter a local date and time.") from None
    if local.tzinfo or not 1970 <= local.year < 9999:
        raise ValueError("Enter a local date and time without a timezone offset.")
    return local


def actual_time(local, zone):
    chosen = local.replace(tzinfo=zone, fold=0)
    utc = chosen.astimezone(timezone.utc)
    return utc if utc.astimezone(zone).replace(tzinfo=None) == local else None


def validate(raw):
    if not isinstance(raw, dict):
        raise ValueError("Enter an event schedule.")
    try:
        zone = ZoneInfo(raw.get("timezone", ""))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError("Choose a time zone such as America/Denver.") from None
    start, end = parse_local(raw.get("start")), parse_local(raw.get("end"))
    opening, closing = actual_time(start, zone), actual_time(end, zone)
    if not opening or not closing:
        raise ValueError("The selected time is skipped when the clocks change.")
    if not timedelta(0) < closing - opening <= timedelta(days=7):
        raise ValueError("End the event after it starts and within seven days.")
    repeat = raw.get("repeat", "none")
    step = raw.get("interval", 1)
    if repeat not in FREQUENCIES or type(step) is not int or not 1 <= step <= 52:
        raise ValueError("Choose a recurrence and an interval from 1 to 52.")
    days = raw.get("weekdays", [])
    if not isinstance(days, list) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
        raise ValueError("Choose valid weekdays.")
    if repeat == "weeks" and days and start.weekday() not in days:
        raise ValueError("Include the first event's weekday in the weekly schedule.")
    count = raw.get("count")
    if count is not None and (type(count) is not int or not 1 <= count <= 10000):
        raise ValueError("Choose a valid number of occurrences.")
    until = raw.get("until")
    if until:
        try:
            until = date.fromisoformat(until)
        except (ValueError, TypeError):
            raise ValueError("Enter a valid last date.") from None
        if until < start.date():
            raise ValueError("Choose a last date on or after the first event.")
    if until and count:
        raise ValueError("Choose a last date or a number of occurrences.")
    return dict(timezone=zone.key, start=start.isoformat(timespec="minutes"), end=end.isoformat(timespec="minutes"),
                repeat=repeat, interval=step if repeat != "none" else 1,
                weekdays=sorted(set(days or [start.weekday()])) if repeat == "weeks" else [],
                count=count if repeat != "none" else None, until=until.isoformat() if until and repeat != "none" else None)


def calendar_dates(start, last, schedule):
    repeat = schedule["repeat"]
    step = schedule["interval"]
    if repeat == "none":
        if start <= last:
            yield start
    elif repeat == "days":
        for offset in range(0, (last - start).days + 1, step):
            yield start + timedelta(days=offset)
    elif repeat == "weeks":
        monday = start - timedelta(days=start.weekday())
        for offset in range(0, (last - monday).days + 1, step * 7):
            for weekday in schedule["weekdays"]:
                day = monday + timedelta(days=offset + weekday)
                if start <= day <= last:
                    yield day
    else:
        first_month = start.year * 12 + start.month - 1
        last_month = last.year * 12 + last.month - 1
        for index in range(first_month, last_month + 1, step):
            year, month = divmod(index, 12)
            month += 1
            if start.day <= monthrange(year, month)[1]:
                day = date(year, month, start.day)
                if day <= last:
                    yield day


def occurrences(schedule, first, last):
    zone = ZoneInfo(schedule["timezone"])
    start = datetime.fromisoformat(schedule["start"])
    end = datetime.fromisoformat(schedule["end"])
    duration = actual_time(end, zone) - actual_time(start, zone)
    last_day = last.astimezone(zone).date()
    if schedule["until"]:
        last_day = min(last_day, date.fromisoformat(schedule["until"]))
    total = 0
    for day in calendar_dates(start.date(), last_day, schedule):
        local = datetime.combine(day, start.time())
        opening = actual_time(local, zone)
        if opening is None:
            continue
        total += 1
        if schedule["count"] and total > schedule["count"]:
            break
        closing = opening + duration
        if closing <= first or opening >= last:
            continue
        yield dict(id=int(opening.timestamp()), start=opening.isoformat(), end=closing.isoformat(),
                   local=local.isoformat(timespec="minutes"))
