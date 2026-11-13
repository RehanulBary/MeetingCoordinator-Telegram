from typing import Literal
from pydantic import BaseModel, ConfigDict


class Interval(BaseModel):
    model_config = ConfigDict(extra='forbid')
    start: str
    end: str


class AvailabilityNotes(BaseModel):
    """Explicit partial facts retained while an availability answer is clarified."""
    model_config = ConfigDict(extra='forbid')
    date: str | None
    start_time: str | None
    end_time: str | None
    timezone: str | None


class ParticipantTarget(BaseModel):
    """Canonical directory identity selected from the current user's authorized roster."""
    model_config = ConfigDict(extra='forbid')
    kind: Literal['member', 'team']
    member_id: int | None
    team_name: str | None
    source_text: str


class Interpretation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    intent: Literal['request', 'availability', 'confirm', 'decline', 'status', 'cancel', 'help', 'other',
                    'chat', 'directory', 'start', 'choose', 'discard']
    meeting_reference: str | None
    title: str | None
    participants: list[str]
    participant_targets: list[ParticipantTarget]
    duration_minutes: int | None
    range_start: str | None
    range_end: str | None
    intervals: list[Interval]
    question: str | None
    reply: str | None
    new_request: bool
    participant_action: Literal['replace', 'add', 'remove']
    clear_fields: list[Literal['duration_minutes', 'range_start', 'range_end', 'participants', 'owner_availability']]
    option_number: int | None
    availability_notes: AvailabilityNotes | None
    availability_mode: Literal['replace', 'add']


def interpreted(intent: str, **fields) -> Interpretation:
    """Construct a complete interpretation for deterministic demos and tests."""
    return Interpretation.model_validate({
        'intent': intent, 'meeting_reference': None, 'title': None,
        'participants': [], 'participant_targets': [], 'duration_minutes': None, 'range_start': None,
        'range_end': None, 'intervals': [], 'question': None, 'reply': None,
        'new_request': False, 'participant_action': 'replace', 'clear_fields': [],
        'option_number': None, 'availability_notes': None, 'availability_mode': 'replace', **fields,
    })
