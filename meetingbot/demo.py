"""End-to-end local demo with a scripted interpreter and simulated Telegram transport."""
from datetime import datetime, timedelta, timezone
import json
from .config import Settings
from .engine import Engine
from .models import interpreted
from .store import Store


class ScriptedInterpreter:
    def __init__(self, responses):
        self.responses = list(responses)

    async def interpret(self, text, context, history):
        if not self.responses:
            raise ValueError('This offline demo has no additional scripted interpretation.')
        return self.responses.pop(0)


def seed(store, zone='Asia/Dhaka'):
    ids = []
    for index, (name, team) in enumerate((('Rehan', 'Management'), ('Ayesha', 'Engineering'),
                                          ('Sara', 'Engineering'), ('Nabil', 'Engineering')), 1):
        uid = store.add_employee(name, team, zone, can_request=index == 1)
        # Negative synthetic private IDs ensure demo records are clearly fictional.
        store.db.execute('UPDATE employees SET telegram_id=?,chat_id=? WHERE id=?', (-index, -index, uid))
        ids.append(uid)
    return ids


def message(update_id, person, text):
    return {'update_id': update_id, 'message': {
        'from': {'id': person['telegram_id'], 'first_name': person['name']},
        'chat': {'id': person['chat_id'], 'type': 'private'}, 'text': text,
        'date': int(datetime.now(timezone.utc).timestamp()),
    }}


def callback(update_id, person, data):
    return {'update_id': update_id, 'callback_query': {
        'id': str(update_id), 'from': {'id': person['telegram_id']}, 'data': data,
        'message': {'chat': {'id': person['chat_id'], 'type': 'private'}},
    }}


async def run_demo():
    from .telegram import eligible
    store = Store(':memory:')
    ids = seed(store)
    now = datetime.now(timezone.utc)
    day = (now + timedelta(days=2)).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Dhaka'))
    start = day.replace(hour=14, minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=2)
    intervals = [
        [{'start': start.isoformat(), 'end': end.isoformat()}],
        [{'start': (start+timedelta(minutes=30)).isoformat(), 'end': end.isoformat()}],
        [{'start': start.isoformat(), 'end': (start+timedelta(hours=1)).isoformat()}],
    ]
    script = [interpreted('request', title='Engineering review', participants=['Engineering'],
                          duration_minutes=30, range_start=start.isoformat(), range_end=end.isoformat())]
    script += [interpreted('availability', meeting_reference='M-1', intervals=i) for i in intervals]
    engine = Engine(store, ScriptedInterpreter(script), Settings())
    last_printed = 0

    def show_outbox():
        nonlocal last_printed
        for row in store.all('SELECT * FROM outbox WHERE id>? ORDER BY id', (last_printed,)):
            last_printed = row['id']
            if not eligible(store, row):
                store.db.execute("UPDATE outbox SET status='skipped' WHERE id=?", (row['id'],))
                continue
            person = store.employee(row['employee_id']) if row['employee_id'] else None
            print(f"\nBOT → {person['name'] if person else row['chat_id']}:\n{row['text']}")
            if row['keyboard']:
                print('Buttons: ' + ' | '.join(b['text'] for line in json.loads(row['keyboard']) for b in line))
            store.db.execute("UPDATE outbox SET status='sent' WHERE id=?", (row['id'],))

    async def send(update, label):
        print('\n' + label)
        store.ingest([update])
        await engine.handle_update(update)
        show_outbox()

    print('OFFLINE DEMO — scripted AI interpretations and simulated Telegram; no API calls or real messages.')
    print('The same database, workflow, matching, permissions, and confirmation code runs in the real bot.')
    owner = store.employee(ids[0])
    await send(message(1, owner, 'Arrange a 30-minute Engineering review in the displayed date range.'),
               'REHAN → BOT: Arrange a 30-minute Engineering review.')
    draft = json.loads(store.employee(ids[0])['draft'])
    await send(callback(2, owner, 'startdraft:' + draft['preview_nonce']), 'REHAN clicks Start coordination.')
    for number, uid in enumerate(ids[1:], 3):
        person = store.employee(uid)
        await send(message(number, person, f'M-1: my available times are {intervals[number-3]}'),
                   f"{person['name'].upper()} → BOT: availability for M-1.")
    meeting = store.meeting(1)
    await send(callback(6, owner, f"choose:1:{meeting['version']}:0"), 'REHAN chooses the first matching slot.')
    for number, uid in enumerate(ids[1:], 7):
        person = store.employee(uid)
        await send(callback(number, person, f"accept:1:{meeting['version']}"), f"{person['name'].upper()} clicks Confirm.")
    assert store.meeting(1)['state'] == 'confirmed'
    print('\nRESULT: All four people accepted one exact 30-minute slot. Database meeting state: confirmed.')
    store.db.close()
