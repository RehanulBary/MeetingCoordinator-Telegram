from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def instant(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('Please include a time zone with the date and time.')
    return dt.astimezone(UTC)


def valid_intervals(intervals: list[dict], start: str, end: str) -> list[dict]:
    low, high = instant(start), instant(end)
    result = []
    if len(intervals) > 40:
        raise ValueError('Please provide at most 40 availability intervals.')
    for item in intervals:
        a, b = instant(item['start']), instant(item['end'])
        if a >= b or a < low or b > high:
            raise ValueError('Availability must have an end after its start, within the meeting date range.')
        result.append({'start': a.isoformat(), 'end': b.isoformat()})
    return result


def overlap(a: tuple[datetime, datetime], b: tuple[datetime, datetime]) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def work_windows(start: str, end: str, zone: str, start_hour: int, end_hour: int, workdays: str) -> list[dict]:
    tz = ZoneInfo(zone)
    low, high = instant(start), instant(end)
    day = low.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    allowed = {int(x) for x in workdays.split(',')}
    windows = []
    while day.astimezone(UTC) < high:
        if day.weekday() in allowed:
            a = max(low, (day + timedelta(hours=start_hour)).astimezone(UTC))
            b = min(high, (day + timedelta(hours=end_hour)).astimezone(UTC))
            if a < b:
                windows.append({'start': a.isoformat(), 'end': b.isoformat()})
        day += timedelta(days=1)
    return windows


def common_slots(availability: list[list[dict]], minutes: int,
                 busy: list[tuple[str, str]] | None = None, limit: int = 3) -> list[dict]:
    if not availability or any(not intervals for intervals in availability):
        return []
    intersection = [(instant(i['start']), instant(i['end'])) for i in availability[0]]
    for intervals in availability[1:]:
        intersection = [(max(a, instant(i['start'])), min(b, instant(i['end'])))
                        for a, b in intersection for i in intervals
                        if max(a, instant(i['start'])) < min(b, instant(i['end']))]
    blocks = [(instant(a), instant(b)) for a, b in (busy or [])]
    duration = timedelta(minutes=minutes)
    result = []
    # Fifteen-minute boundaries; keep offered options non-overlapping.
    for start, end in sorted(intersection):
        cursor = start.replace(second=0, microsecond=0)
        if cursor < start:
            cursor += timedelta(minutes=1)
        cursor += timedelta(minutes=(-cursor.minute) % 15)
        while cursor + duration <= end:
            slot = (cursor, cursor + duration)
            if not any(overlap(slot, block) for block in blocks):
                if not any(overlap(slot, (instant(x['start']), instant(x['end']))) for x in result):
                    result.append({'start': cursor.isoformat(), 'end': (cursor + duration).isoformat()})
                    if len(result) >= limit:
                        return result
            cursor += timedelta(minutes=15)
    return result


def show_slot(slot: dict, zone: str) -> str:
    tz = ZoneInfo(zone)
    start, end = instant(slot['start']).astimezone(tz), instant(slot['end']).astimezone(tz)
    end_label = end.strftime('%H:%M') if start.date() == end.date() else end.strftime('%a %d %b %Y %H:%M')
    return f"{start:%a %d %b %Y, %H:%M}–{end_label} ({zone})"
