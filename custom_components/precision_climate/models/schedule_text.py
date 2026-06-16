"""Parse a human-friendly text schedule into ScheduleBlock objects.

Home Assistant config-flow forms cannot render a rich time-block editor, so a
room's schedule for a given day is entered as plain text, one block per line:

    00:00-08:00 18 passive
    08:00-22:00 21 active
    22:00-24:00 17 passive

Each line is: ``HH:MM-HH:MM <target> <active|passive>``. ``24:00`` denotes end
of day. This parser is pure and unit-tested; the config flow uses it and surfaces
any ParseError back to the user.
"""

from __future__ import annotations

from .schedule import MINUTES_PER_DAY, ScheduleBlock

_ACTIVE_WORDS = {"active", "a", "true", "1", "yes"}
_PASSIVE_WORDS = {"passive", "p", "false", "0", "no"}


class ParseError(ValueError):
    """Raised when a schedule line cannot be parsed."""


def _parse_hhmm(token: str) -> int:
    try:
        hh, mm = token.split(":")
        minutes = int(hh) * 60 + int(mm)
    except (ValueError, AttributeError) as err:
        raise ParseError(f"invalid time '{token}' (expected HH:MM)") from err
    if not 0 <= minutes <= MINUTES_PER_DAY:
        raise ParseError(f"time '{token}' out of range")
    return minutes


def parse_day_schedule(text: str) -> list[ScheduleBlock]:
    """Parse one day's worth of schedule text into ordered ScheduleBlocks."""
    blocks: list[ScheduleBlock] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ParseError(
                f"line {lineno}: expected 'HH:MM-HH:MM target active|passive', got '{line}'"
            )
        span, target_s, active_s = parts
        if "-" not in span:
            raise ParseError(f"line {lineno}: missing '-' in time span '{span}'")
        start_s, end_s = span.split("-", 1)
        start = _parse_hhmm(start_s)
        end = _parse_hhmm(end_s)
        if end <= start:
            raise ParseError(f"line {lineno}: end must be after start in '{span}'")
        try:
            target = float(target_s)
        except ValueError as err:
            raise ParseError(f"line {lineno}: invalid target '{target_s}'") from err
        active_l = active_s.lower()
        if active_l in _ACTIVE_WORDS:
            is_active = True
        elif active_l in _PASSIVE_WORDS:
            is_active = False
        else:
            raise ParseError(
                f"line {lineno}: '{active_s}' must be active or passive"
            )
        blocks.append(
            ScheduleBlock(start_min=start, end_min=end, target=target, is_active=is_active)
        )
    blocks.sort(key=lambda b: b.start_min)
    return blocks


def _fmt_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def dicts_to_text(block_dicts: list[dict]) -> str:
    """Render stored block dicts back to the editable text form (for pre-filling)."""
    lines = []
    for b in sorted(block_dicts, key=lambda d: d["start_min"]):
        active = "active" if b["is_active"] else "passive"
        target = b["target"]
        target_s = f"{target:g}"
        lines.append(
            f"{_fmt_hhmm(b['start_min'])}-{_fmt_hhmm(b['end_min'])} {target_s} {active}"
        )
    return "\n".join(lines)


def blocks_to_dicts(blocks: list[ScheduleBlock]) -> list[dict]:
    """Serialise blocks to the plain-dict form stored in the config entry."""
    return [
        {
            "start_min": b.start_min,
            "end_min": b.end_min,
            "target": b.target,
            "is_active": b.is_active,
        }
        for b in blocks
    ]
