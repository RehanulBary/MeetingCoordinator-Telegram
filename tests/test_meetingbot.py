import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
import httpx
from openai import AsyncOpenAI
from meetingbot.ai import OpenAIInterpreter
from meetingbot.config import Settings
from meetingbot.demo import ScriptedInterpreter, callback, message, seed
from meetingbot.engine import Engine
from meetingbot.models import interpreted
from meetingbot.scheduling import common_slots, instant, valid_intervals, work_windows
from meetingbot.store import Store
from meetingbot.telegram import eligible, Telegram, TelegramError, Runner


class MeetingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 4, tzinfo=timezone.utc)
        self.store = Store(':memory:', clock=lambda: self.now.timestamp())
        self.ids = seed(self.store)
        self.ai = ScriptedInterpreter([])
        self.engine = Engine(self.store, self.ai, Settings(), now=lambda: self.now)
        self.counter = 0
        self.start = '2026-11-12T14:00:00+06:00'
        self.end = '2026-11-12T16:00:00+06:00'

    def tearDown(self):
        self.store.db.close()

    def person(self, i=0):
        return self.store.employee(self.ids[i])

    async def send(self, i, text, parsed=None):
        if parsed:
            self.ai.responses.append(parsed)
        self.counter += 1
        update = message(self.counter, self.person(i), text)
        self.store.ingest([update])
        await self.engine.handle_update(update)
        return update

    async def click(self, i, data):
        self.counter += 1
        update = callback(self.counter, self.person(i), data)
        self.store.ingest([update])
        await self.engine.handle_update(update)

    async def draft(self, **changes):
        fields = dict(title='Engineering review', participants=['Engineering'], duration_minutes=30,
                      range_start=self.start, range_end=self.end)
        fields.update(changes)
        await self.send(0, 'Please arrange the engineering review.', interpreted('request', **fields))
        return json.loads(self.person()['draft'])

    async def begin(self):
        draft = await self.draft()
        await self.click(0, 'startdraft:' + draft['preview_nonce'])
        return self.store.one('SELECT * FROM meetings ORDER BY id DESC LIMIT 1')

    async def available(self, i, mid=1, start=None, end=None):
        await self.send(i, f'M-{mid}: available during this interval.', interpreted(
            'availability', meeting_reference=f'M-{mid}',
            intervals=[{'start': start or self.start, 'end': end or self.end}]))

    async def propose(self, mid=1):
        for i in (1, 2, 3):
            await self.available(i, mid)
        m = self.store.meeting(mid)
        await self.click(0, f"choose:{mid}:{m['version']}:0")
        return self.store.meeting(mid)

    def last_text(self):
        return self.store.one('SELECT text FROM outbox ORDER BY id DESC LIMIT 1')['text']

    async def test_full_flow_requires_every_required_acceptance(self):
        await self.begin()
        m = await self.propose()
        await self.click(1, f"accept:1:{m['version']}")
        await self.click(2, f"accept:1:{m['version']}")
        self.assertEqual(self.store.meeting(1)['state'], 'confirming')
        await self.click(3, f"accept:1:{m['version']}")
        self.assertEqual(self.store.meeting(1)['state'], 'confirmed')
        self.assertEqual(len(self.store.all("SELECT * FROM outbox WHERE purpose='final'")), 4)

    async def test_availability_is_not_acceptance(self):
        await self.begin()
        for i in (1, 2, 3):
            await self.available(i)
        self.assertEqual(self.store.meeting(1)['state'], 'options')
        self.assertTrue(all(x['accepted_version'] is None for x in self.store.members(1)))

    async def test_only_organizer_selects_and_cancels(self):
        await self.begin()
        for i in (1, 2, 3):
            await self.available(i)
        m = self.store.meeting(1)
        await self.click(1, f"choose:1:{m['version']}:0")
        self.assertEqual(self.store.meeting(1)['state'], 'options')
        await self.send(1, '/cancel M-1')
        self.assertEqual(self.store.meeting(1)['state'], 'options')
        self.assertIn('organizer', self.last_text())

    async def test_unknown_account_cannot_read_directory_or_call_ai(self):
        update = {'update_id': 900, 'message': {'chat': {'id': 900, 'type': 'private'},
                   'from': {'id': 900}, 'text': '/teams'}}
        self.store.ingest([update])
        await self.engine.handle_update(update)
        self.assertNotIn('Engineering', self.last_text())
        self.assertIn('enrollment', self.last_text())

    async def test_member_cannot_create_meetings(self):
        await self.send(1, 'Create a meeting', interpreted('request', title='No permission'))
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 0)
        self.assertIn('requester access', self.last_text())

    async def test_duplicate_update_and_start_button_are_idempotent(self):
        update = await self.send(0, 'Hello', interpreted('help'))
        n = self.store.one('SELECT count(*) AS n FROM outbox')['n']
        await self.engine.handle_update(update)
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM outbox')['n'], n)
        draft = await self.draft()
        data = 'startdraft:' + draft['preview_nonce']
        await self.click(0, data)
        await self.click(0, data)
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 1)

    async def test_stale_preview_does_not_start_revised_unreviewed_draft(self):
        old = await self.draft()
        await self.draft(duration_minutes=300)
        await self.click(0, 'startdraft:' + old['preview_nonce'])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 0)
        self.assertIn('out of date', self.last_text())

    async def test_changed_availability_invalidates_old_acceptance(self):
        await self.begin()
        m = await self.propose()
        await self.click(1, f"accept:1:{m['version']}")
        await self.available(2, start='2026-11-12T15:00:00+06:00')
        self.assertGreater(self.store.meeting(1)['version'], m['version'])
        self.assertTrue(all(x['accepted_version'] is None for x in self.store.members(1)))
        self.assertTrue(all(x['status'] != 'confirmed' for x in self.store.members(1)))
        await self.click(3, f"accept:1:{m['version']}")
        self.assertIn('changed', self.last_text())

    async def test_cancellation_suppresses_queued_invites(self):
        await self.begin()
        rows = self.store.all("SELECT * FROM outbox WHERE purpose='invitation'")
        self.assertTrue(all(eligible(self.store, x) for x in rows))
        await self.send(0, '/cancel M-1')
        self.assertEqual(self.store.meeting(1)['state'], 'cancelled')
        self.assertTrue(all(not eligible(self.store, x) for x in rows))

    async def test_stop_suppresses_outreach_and_start_does_not_reenable(self):
        await self.begin()
        row = self.store.one("SELECT * FROM outbox WHERE purpose='invitation' AND employee_id=?", (self.ids[1],))
        await self.send(1, '/stop')
        self.assertFalse(eligible(self.store, row))
        await self.send(1, '/start')
        self.assertEqual(self.person(1)['opted_in'], 0)
        await self.send(1, '/resume')
        self.assertEqual(self.person(1)['opted_in'], 1)

    async def test_explicit_decline_returns_decision_to_organizer(self):
        await self.begin()
        await self.send(1, 'I cannot attend M-1', interpreted('decline', meeting_reference='M-1'))
        self.assertEqual(self.store.meeting(1)['state'], 'needs_decision')
        self.assertIn('declined', self.last_text())

    async def test_no_overlap_stops_after_two_alternative_rounds(self):
        await self.begin()
        for round_number in range(3):
            await self.available(1, start=self.start, end='2026-11-12T14:30:00+06:00')
            await self.available(2, start='2026-11-12T15:00:00+06:00', end=self.end)
            await self.available(3)
        self.assertEqual(self.store.meeting(1)['state'], 'needs_decision')
        self.assertEqual(self.store.meeting(1)['rounds'], 2)

    async def test_late_confirmation_does_not_finalize(self):
        await self.begin()
        m = await self.propose()
        self.now = datetime.fromtimestamp(m['deadline'] + 1, timezone.utc)
        await self.click(1, f"accept:1:{m['version']}")
        self.assertEqual(self.store.meeting(1)['state'], 'needs_decision')

    async def test_reminder_caps_apply_across_multiple_meetings(self):
        await self.begin()
        await self.begin()
        self.now += timedelta(hours=24)
        self.engine.sweep()
        for uid in self.ids[1:]:
            n = self.store.one("SELECT count(*) AS n FROM outbox WHERE purpose='reminder' AND employee_id=?", (uid,))['n']
            self.assertEqual(n, 1)
        self.engine.sweep()
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM outbox WHERE purpose='reminder'")['n'], 3)

    async def test_reminders_respect_local_quiet_hours(self):
        await self.begin()
        self.now = datetime(2026, 11, 11, 0, tzinfo=timezone.utc)  # 06:00 Dhaka
        self.engine.sweep()
        self.assertEqual(self.store.one("SELECT count(*) AS n FROM outbox WHERE purpose='reminder'")['n'], 0)

    async def test_confirmed_meeting_blocks_competing_confirmation(self):
        await self.begin()
        await self.begin()
        first = await self.propose(1)
        second = await self.propose(2)
        for i in (1, 2, 3):
            await self.click(i, f"accept:1:{first['version']}")
        self.assertEqual(self.store.meeting(1)['state'], 'confirmed')
        await self.click(1, f"accept:2:{second['version']}")
        self.assertEqual(self.store.meeting(2)['state'], 'needs_decision')

    async def test_ambiguous_name_is_not_arbitrarily_resolved(self):
        self.store.add_employee('Sara', 'Design', 'Asia/Dhaka')
        await self.draft(participants=['Sara'])
        self.assertIn('ambiguous', self.last_text())
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 0)

    async def test_ai_failure_keeps_meeting_state_unchanged(self):
        await self.begin()
        await self.send(1, 'Please interpret this unavailable scripted message')
        self.assertEqual(self.store.meeting(1)['state'], 'collecting')

    async def test_other_member_cannot_spoof_proposal_callback(self):
        await self.begin()
        other = self.store.add_employee('Outsider', 'Other', 'Asia/Dhaka')
        self.store.db.execute('UPDATE employees SET telegram_id=55,chat_id=55 WHERE id=?', (other,))
        m = await self.propose()
        update = callback(800, self.store.employee(other), f"accept:1:{m['version']}")
        self.store.ingest([update])
        await self.engine.handle_update(update)
        self.assertIn('not valid for your account', self.last_text())

    async def test_ai_context_keeps_bot_clarification(self):
        await self.send(0, 'Meet with Engineering', interpreted('request', participants=['Engineering'], question='Which day works?'))
        history = json.loads(self.person()['history'])
        self.assertEqual(history[-1]['role'], 'assistant')
        self.assertEqual(history[-1]['text'], 'Which day works?')

    async def test_free_text_acceptance_requires_explicit_button(self):
        await self.begin()
        await self.propose()
        await self.send(1, 'Yes', interpreted('confirm', meeting_reference='M-1'))
        attendee = next(x for x in self.store.members(1) if x['employee_id'] == self.ids[1])
        self.assertIsNone(attendee['accepted_version'])
        self.assertIn('use a button', self.last_text())

    async def test_natural_language_cancellation_requires_button(self):
        await self.begin()
        await self.send(0, 'Cancel the meeting', interpreted('cancel', meeting_reference='M-1'))
        self.assertEqual(self.store.meeting(1)['state'], 'collecting')
        row = self.store.one('SELECT keyboard FROM outbox ORDER BY id DESC LIMIT 1')
        data = json.loads(row['keyboard'])[0][0]['callback_data']
        await self.click(0, data)
        self.assertEqual(self.store.meeting(1)['state'], 'cancelled')

    async def test_organizer_specific_availability_is_used(self):
        draft = await self.draft(intervals=[{'start': '2026-11-12T15:00+06:00', 'end': self.end}])
        await self.click(0, 'startdraft:' + draft['preview_nonce'])
        for i in (1, 2, 3):
            await self.available(i)
        options = json.loads(self.store.meeting(1)['options'])
        self.assertEqual(instant(options[0]['start']), instant('2026-11-12T15:00+06:00'))

    async def test_request_outside_company_hours_is_not_started(self):
        await self.draft(range_start='2026-11-12T20:00+06:00', range_end='2026-11-12T22:00+06:00')
        self.assertIn('working hours', self.last_text())
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 0)


class StorageAndSchedulingTests(unittest.TestCase):
    def test_business_hours_exclude_nights_and_unconfigured_weekends(self):
        windows = work_windows('2026-11-13T00:00+06:00', '2026-11-16T23:00+06:00',
                               'Asia/Dhaka', 9, 18, '0,1,2,3,4')
        self.assertEqual(len(windows), 2)  # Friday and Monday only
        self.assertEqual(instant(windows[0]['start']), instant('2026-11-13T09:00+06:00'))

    def test_expired_enrollment_code_is_rejected(self):
        clock = [1000.0]
        s = Store(':memory:', clock=lambda: clock[0])
        uid = s.add_employee('Employee', 'Engineering', 'Asia/Dhaka')
        code = s.invite(uid)
        clock[0] += 3601
        self.assertFalse(s.claim(code, 123, 123, 'Employee'))
        s.db.close()
    def test_timezone_overlap_and_back_to_back_busy_slot(self):
        slots = common_slots([
            [{'start': '2026-11-18T14:00+06:00', 'end': '2026-11-18T16:00+06:00'}],
            [{'start': '2026-11-18T08:30+00:00', 'end': '2026-11-18T10:00+00:00'}],
        ], 30, busy=[('2026-11-18T09:00+00:00', '2026-11-18T09:30+00:00')])
        self.assertEqual(slots[0]['start'], '2026-11-18T08:30:00+00:00')
        self.assertEqual(slots[1]['start'], '2026-11-18T09:30:00+00:00')

    def test_naive_and_out_of_range_times_rejected(self):
        with self.assertRaises(ValueError):
            instant('2026-11-18T12:00')
        with self.assertRaises(ValueError):
            valid_intervals([{'start': '2026-11-18T12:00Z', 'end': '2026-11-18T13:00Z'}],
                            '2026-11-18T14:00Z', '2026-11-18T16:00Z')

    def test_enrollment_requires_operator_approval_and_code_is_single_use(self):
        s = Store(':memory:')
        uid = s.add_employee('Verified Employee', 'Engineering', 'Asia/Dhaka')
        code = s.invite(uid)
        self.assertTrue(s.claim(code, 123, 123, 'Claimed name'))
        self.assertIsNone(s.by_telegram(123))
        self.assertFalse(s.claim(code, 124, 124, 'Other name'))
        with self.assertRaises(ValueError):
            s.approve(uid, 124)
        s.approve(uid, 123)
        self.assertEqual(s.by_telegram(123)['id'], uid)
        s.db.close()

    def test_admin_reader_does_not_recover_active_sender(self):
        with tempfile.TemporaryDirectory() as temp:
            p = str(Path(temp) / 'db.sqlite3')
            s = Store(p)
            s.queue(123, 'Some message')
            s.db.execute("UPDATE outbox SET status='sending'")
            reader = Store(p)
            self.assertEqual(reader.one('SELECT status FROM outbox')['status'], 'sending')
            reader.db.close()
            s.recover()
            self.assertEqual(s.one('SELECT status FROM outbox')['status'], 'uncertain')
            s.db.close()

    def test_inbox_dedup_and_persistent_offset(self):
        s = Store(':memory:')
        s.ingest([{'update_id': 5}, {'update_id': 5}, {'update_id': 8}])
        self.assertEqual(s.offset(), 9)
        self.assertEqual(s.one('SELECT count(*) AS n FROM inbox')['n'], 2)
        s.db.close()

    def test_ai_daily_call_reservation_is_enforced(self):
        s = Store(':memory:')
        s.reserve_ai('gpt-6-luna', 1)
        with self.assertRaises(ValueError):
            s.reserve_ai('gpt-6-luna', 1)
        s.db.close()


class APIContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().slow_callback_duration = 2.0

    async def test_openai_responses_schema_and_requested_model(self):
        parsed = interpreted('help', question='How can I help with your meeting?')
        requests = []

        def respond(request):
            body = json.loads(request.content)
            requests.append(body)
            return httpx.Response(200, json={
                'id': 'resp_test', 'object': 'response', 'created_at': 1,
                'status': 'completed', 'model': 'gpt-6-luna',
                'output': [{'id': 'msg_test', 'type': 'message', 'status': 'completed',
                            'role': 'assistant', 'content': [{'type': 'output_text',
                             'text': parsed.model_dump_json(), 'annotations': []}]}],
                'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30},
            })

        s = Store(':memory:')
        interpreter = OpenAIInterpreter(Settings(openai_key='unit-test-placeholder'), s)
        await interpreter.client.close()
        interpreter.client = AsyncOpenAI(api_key='unit-test-placeholder', max_retries=0,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)))
        history = [{'role': 'user' if i % 2 == 0 else 'assistant', 'text': f'Conversation turn {i}'} for i in range(20)]
        history.append({'role': 'assistant', 'text': 'A failed, undelivered invitation', 'delivery': 'failed'})
        result = await interpreter.interpret('Help', {'focus_meeting_id': 12}, history)
        self.assertEqual(result.intent, 'help')
        self.assertEqual(requests[0]['model'], 'gpt-6-luna')
        self.assertFalse(requests[0]['store'])
        self.assertEqual(requests[0]['text']['format']['type'], 'json_schema')
        self.assertEqual(requests[0]['reasoning']['effort'], 'medium')
        self.assertNotIn('tools', requests[0])
        self.assertEqual(requests[0]['input'][1]['role'], 'developer')
        self.assertIn('focus_meeting_id', requests[0]['input'][1]['content'])
        self.assertTrue(any(x['content'] == 'Conversation turn 0' for x in requests[0]['input']))
        self.assertFalse(any(x['content'] == 'A failed, undelivered invitation' for x in requests[0]['input']))
        self.assertEqual(s.one('SELECT input_tokens FROM ai_usage')['input_tokens'], 10)
        await interpreter.client.close()
        s.db.close()

    async def test_telegram_rate_limit_preserves_retry_after(self):
        api = Telegram('test-only-placeholder')
        await api.client.aclose()
        api.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(
            429, json={'ok': False, 'error_code': 429, 'parameters': {'retry_after': 7}})))
        with self.assertRaises(TelegramError) as caught:
            await api.call('sendMessage', chat_id=123, text='Test')
        self.assertEqual(caught.exception.retry_after, 7)
        self.assertNotIn('test-only-placeholder', str(caught.exception))
        await api.close()


if __name__ == '__main__':
    unittest.main()
