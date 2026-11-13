"""Opt-in live language checks using fictional in-memory company data only."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import socket
from zoneinfo import ZoneInfo

from .ai import OpenAIInterpreter
from .demo import seed
from .engine import Engine
from .scheduling import instant
from .store import Store


def cases(now):
    day = (now + timedelta(days=2)).astimezone(ZoneInfo('Asia/Dhaka')).date().isoformat()
    start, end = day + 'T14:00:00+06:00', day + 'T17:00:00+06:00'
    draft = {'participants': ['Engineering'], 'title': 'Engineering review', 'duration_minutes': 30,
             'range_start': start, 'range_end': end}
    interval = lambda result: bool(result.intervals) and instant(result.intervals[0].start) == instant(day + 'T14:30+06:00') and instant(result.intervals[0].end) == instant(day + 'T16:00+06:00')
    return day, start, end, [
        ('greeting', 0, 'Hi there', {}, [], lambda r: r.intent in ('chat', 'help') and bool(r.reply) and not r.question),
        ('duration correction', 0, 'Make it 45 minutes instead.', {'draft': draft}, [],
         lambda r: r.intent == 'request' and r.duration_minutes == 45 and not r.new_request and not r.question),
        ('add a person', 0, 'Also include Sara from Design.', {'draft': draft}, [],
         lambda r: r.intent == 'request' and r.participant_action == 'add' and bool(r.participants or r.participant_targets) and not r.new_request),
        ('single-day availability', 1, '2:30 to 4 pm works for me.', {'meeting': True}, [],
         lambda r: r.intent == 'availability' and interval(r) and not r.question),
        ('finish a partial reply', 1, '4 pm.', {'meeting': True, 'partial': True},
         [{'role': 'assistant', 'text': 'Until what time are you available?'}],
         lambda r: r.intent == 'availability' and (interval(r) or bool(r.availability_notes and r.availability_notes.end_time == '16:00'))),
        ('select a time', 0, 'The second one, please.', {'meeting': True, 'options': True}, [],
         lambda r: r.intent == 'choose' and r.option_number == 2 and not r.question),
        ('progress question', 0, 'Who are we still waiting for?', {'meeting': True}, [],
         lambda r: r.intent == 'status' and not r.question),
        ('Bangla directory question', 0, 'ইঞ্জিনিয়ারিং টিমে কারা আছে?', {}, [],
         lambda r: r.intent == 'directory' and bool(r.participants or r.participant_targets)),
        ('general team phrase', 0, 'Set up a 30-minute internal meeting with the management folks ' + day + ' from 9 am to 11 am.', {}, [],
         lambda r: r.intent == 'request' and any(t.kind == 'team' and t.team_name == 'Management' for t in r.participant_targets)),
        ('replace stale attendee wording', 0, 'No, set up the meeting with Shanto instead.',
         {'draft': {**draft, 'participants': ['Management team'], 'participant_resolution': {'status': 'unresolved', 'queries': ['Management team']}}}, [],
         lambda r: r.intent == 'request' and not r.new_request and r.participant_action == 'replace' and
                   any(t.kind == 'member' and t.member_id == 6 for t in r.participant_targets)),
        ('resolve a pronoun', 0, 'Yes, set it up with him instead.', {'draft': draft},
         [{'role': 'assistant', 'text': 'Shanto is in Management. Do you want the meeting with Shanto?'}],
         lambda r: r.intent == 'request' and r.participant_action == 'replace' and
                   any(t.kind == 'member' and t.member_id == 6 for t in r.participant_targets)),
        ('same time on next day', 0, 'Same time, but the following day instead.', {'draft': draft}, [],
         lambda r: r.intent == 'request' and bool(r.range_start and r.range_end) and
                   instant(r.range_start) == instant(start) + timedelta(days=1) and instant(r.range_end) == instant(end) + timedelta(days=1)),
    ]


async def run_language_checks(settings):
    if not settings.openai_key:
        print('OpenAI API key is missing. Add it in the dashboard before running live language checks.')
        return False
    try:
        await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, 'api.openai.com', 443), 6)
    except (OSError, asyncio.TimeoutError):
        print('Live language checks unavailable: api.openai.com cannot be resolved from this process. No live model checks completed.')
        return False
    now = datetime.now(timezone.utc)
    day, start, end, scenarios = cases(now)
    passed = 0
    print(f'LIVE language checks with {settings.model}: up to {len(scenarios)} billable model calls; no Telegram messages or real company records.')
    for label, actor, text, setup, history, check in scenarios:
        store = Store(':memory:')
        ids = seed(store)
        store.add_employee('Sara Designer', 'Design', 'Asia/Dhaka')
        store.add_employee('Shanto', 'Management', 'Asia/Dhaka')
        interpreter = OpenAIInterpreter(replace(settings, daily_calls=min(settings.daily_calls, 20)), store)
        try:
            if setup.get('draft'):
                store.db.execute('UPDATE employees SET draft=? WHERE id=?', (json.dumps(setup['draft']), ids[0]))
            if setup.get('meeting'):
                options = [{'start': day + f'T{hour}:00:00+06:00', 'end': day + f'T{hour}:30:00+06:00'} for hour in (14, 15, 16)]
                store.db.execute('''INSERT INTO meetings(id,owner,title,duration,start,end,state,version,options,deadline,created)
                                    VALUES(1,?,?,?,?,?,?,1,?,?,?)''',
                                 (ids[0], 'Engineering review', 30, start, end, 'options' if setup.get('options') else 'collecting',
                                  json.dumps(options), now.timestamp()+86400, now.timestamp()))
                for uid in ids:
                    store.db.execute("INSERT INTO attendees(meeting_id,employee_id,status) VALUES(1,?,'pending')", (uid,))
                store.remember_state(ids[actor], focus_meeting_id=1)
            if setup.get('partial'):
                store.remember_state(ids[actor], availability_drafts={'1': {'date': day, 'start_time': '14:30', 'end_time': None, 'timezone': 'Asia/Dhaka'}},
                                     clarification={'task': 'availability', 'meeting_id': 1, 'field': 'end_time', 'question': 'Until what time?'})
            person = store.employee(ids[actor])
            context = Engine(store, interpreter, settings).context(person, now.timestamp())
            result = await interpreter.interpret(text, context, history)
            ok = bool(check(result))
            passed += int(ok)
            print(('PASS ' if ok else 'FAIL ') + label + ('' if ok else ' — inspect interpretation/prompt before release'))
        except Exception as exc:
            print('ERROR ' + label + ' (' + type(exc).__name__ + '); credentials and response body withheld.')
            # Stop after an API error rather than repeating invalid credentials/model/quota calls.
            return False
        finally:
            await interpreter.client.close()
            store.db.close()
    print(f'{passed}/{len(scenarios)} live language checks passed. These finite scenarios do not guarantee all conversations.')
    return passed == len(scenarios)
