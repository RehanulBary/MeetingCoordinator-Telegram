"""Conversation state and action grounding, independent of live model availability."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from contextlib import redirect_stdout
import io

from meetingbot.config import Settings
from meetingbot.demo import message, callback, seed, ScriptedInterpreter
from meetingbot.engine import Engine
from meetingbot.models import interpreted
from meetingbot.scheduling import instant
from meetingbot.store import Store


class RecordingInterpreter(ScriptedInterpreter):
    def __init__(self):
        super().__init__([])
        self.calls = []

    async def interpret(self, text, context, history):
        self.calls.append({'text': text, 'context': context, 'history': history})
        return await super().interpret(text, context, history)


class ConversationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 11, 10, 4, tzinfo=timezone.utc)
        self.store = Store(':memory:', clock=lambda: self.now.timestamp())
        self.ids = seed(self.store)
        self.ai = RecordingInterpreter()
        self.engine = Engine(self.store, self.ai, Settings(), now=lambda: self.now)
        self.counter = 0
        self.start, self.end = '2026-11-12T14:00+06:00', '2026-11-12T17:00+06:00'

    def tearDown(self):
        self.store.db.close()

    def person(self, index=0):
        return self.store.employee(self.ids[index])

    async def send(self, index, text, parsed=None, reply_to=None):
        if parsed is not None:
            self.ai.responses.append(parsed)
        self.counter += 1
        update = message(self.counter, self.person(index), text)
        update['message']['date'] = int(self.now.timestamp())
        if reply_to is not None:
            update['message']['reply_to_message'] = {'message_id': reply_to}
        self.store.ingest([update])
        await self.engine.handle_update(update)

    async def click(self, index, data):
        self.counter += 1
        update = callback(self.counter, self.person(index), data)
        self.store.ingest([update])
        await self.engine.handle_update(update)

    def deliver(self):
        # Simulate successful Telegram transport, with stable distinct message IDs.
        self.store.db.execute("UPDATE outbox SET status='sent',message_id=1000+id WHERE status='pending'")

    def last_text(self):
        return self.store.one('SELECT text FROM outbox ORDER BY id DESC LIMIT 1')['text']

    async def draft(self, **changes):
        await self.send(0, 'Meet with Engineering', interpreted('request', participants=['Engineering'],
                        range_start=self.start, range_end=self.end, **changes))
        return json.loads(self.person()['draft'])

    async def begin(self):
        draft = await self.draft(new_request=True)
        await self.click(0, 'startdraft:' + draft['preview_nonce'])
        return self.store.one('SELECT * FROM meetings ORDER BY id DESC LIMIT 1')

    async def options(self):
        m = await self.begin()
        for i in (1, 2, 3):
            await self.send(i, '2 to 5 works', interpreted('availability', meeting_reference=str(m['id']),
                            intervals=[{'start': self.start, 'end': self.end}]))
        return self.store.meeting(m['id'])

    async def test_fragments_merge_without_reasking_title_timezone_or_other_facts(self):
        await self.send(0, 'I need Engineering', interpreted('request', participants=['Engineering']))
        self.assertNotIn('title', self.last_text().lower())
        await self.send(0, '30 minutes', interpreted('request', duration_minutes=30))
        self.assertEqual(json.loads(self.person()['draft'])['participants'], ['Engineering'])
        await self.send(0, 'Thursday 2 to 5', interpreted('request', range_start=self.start, range_end=self.end))
        draft = json.loads(self.person()['draft'])
        self.assertEqual(draft['duration_minutes'], 30)
        self.assertIn('preview_nonce', draft)
        self.assertIn('Preview:', self.last_text())
        self.assertEqual(self.ai.calls[-1]['context']['existing_draft']['participants'], ['Engineering'])

    async def test_complete_draft_ignores_redundant_model_question(self):
        draft = await self.draft(question='What should the title be?')
        self.assertIn('preview_nonce', draft)
        self.assertNotIn('What should', self.last_text())
        self.assertEqual(draft['duration_minutes'], 30)
        self.assertIn('default', self.last_text())

    async def test_add_remove_and_unique_first_names_preserve_remaining_people(self):
        design = self.store.add_employee('Sara Designer', 'Design', 'Asia/Dhaka')
        await self.draft()
        await self.send(0, 'Also Sara from Design', interpreted('request', participant_action='add', participants=['Sara Design']))
        draft = json.loads(self.person()['draft'])
        self.assertEqual(set(draft['employee_ids']), set(self.ids + [design]))
        await self.send(0, 'Without Nabil', interpreted('request', participant_action='remove', participants=['Nabil']))
        self.assertNotIn(self.ids[3], json.loads(self.person()['draft'])['employee_ids'])
        self.assertIn(self.ids[1], json.loads(self.person()['draft'])['employee_ids'])

    async def test_reported_management_shanto_exchange_keeps_topic_time_and_corrects_attendees(self):
        shanto = self.store.add_employee('Shanto', 'Management', 'Asia/Dhaka')
        another = self.store.add_employee('Another Manager', 'Management', 'Asia/Dhaka')
        await self.send(0, 'i want to set a meeting', interpreted('request'))
        self.assertIn('Who should join', self.last_text())
        target = {'kind': 'team', 'member_id': None, 'team_name': 'Management', 'source_text': 'management team'}
        await self.send(0, 'management team, internal topic meeting, 30 minutes tommorow 9 am to 11 am',
                        interpreted('request', participants=['Management team'], participant_targets=[target],
                                    title='internal topic meeting', duration_minutes=30,
                                    range_start='2026-11-11T09:00+06:00', range_end='2026-11-11T11:00+06:00'))
        first = json.loads(self.person()['draft'])
        self.assertEqual(set(first['employee_ids']), {self.ids[0], shanto, another})
        await self.send(0, 'shanto', interpreted('request', participants=['Management team', 'Shanto'],
                        participant_targets=[{'kind': 'member', 'member_id': shanto, 'team_name': None, 'source_text': 'shanto'}]))
        corrected = json.loads(self.person()['draft'])
        self.assertEqual(set(corrected['employee_ids']), {self.ids[0], shanto})
        self.assertEqual(corrected['title'], 'internal topic meeting')
        self.assertEqual(corrected['duration_minutes'], 30)
        self.assertEqual(corrected['range_start'], first['range_start'])
        self.assertEqual(corrected['range_end'], first['range_end'])
        self.assertIn('Shanto', self.last_text())
        self.assertNotIn('Another Manager', self.last_text())

    async def test_legacy_team_suffix_and_canonical_general_phrases_resolve_the_same_roster(self):
        shanto = self.store.add_employee('Shanto', 'Management', 'Asia/Dhaka')
        self.assertEqual(self.engine.resolve_people(['the Management team'], self.ids[0]), [self.ids[0], shanto])
        await self.send(0, 'the management folks', interpreted('request', participant_targets=[{
                        'kind': 'team', 'member_id': None, 'team_name': 'Management', 'source_text': 'the management folks'}],
                        range_start=self.start, range_end=self.end))
        self.assertEqual(json.loads(self.person()['draft'])['employee_ids'], [self.ids[0], shanto])

    async def test_invalid_canonical_entity_never_creates_a_person_or_keeps_old_preview_valid(self):
        old = await self.draft()
        await self.send(0, 'Actually an unknown person', interpreted('request', participant_targets=[{
                        'kind': 'member', 'member_id': 9999, 'team_name': None, 'source_text': 'unknown person'}]))
        self.assertNotIn('preview_nonce', json.loads(self.person()['draft']))
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM employees')['n'], 4)
        await self.click(0, 'startdraft:' + old['preview_nonce'])
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 0)

    async def test_attendee_resolution_failure_is_visible_to_next_planner_turn(self):
        await self.send(0, 'Unknown team tomorrow', interpreted('request', participants=['Unknown team'],
                        range_start=self.start, range_end=self.end))
        shanto = self.store.add_employee('Shanto', 'Management', 'Asia/Dhaka')
        await self.send(0, 'Shanto instead', interpreted('request', participant_targets=[{
                        'kind': 'member', 'member_id': shanto, 'team_name': None, 'source_text': 'Shanto instead'}]))
        before = self.ai.calls[-1]['context']['existing_draft']
        self.assertEqual(before['participant_resolution']['status'], 'unresolved')
        corrected = json.loads(self.person()['draft'])
        self.assertNotIn('participant_resolution', corrected)
        self.assertEqual(corrected['employee_ids'], [self.ids[0], shanto])

    async def test_version_reports_the_loaded_coordinator(self):
        await self.send(0, '/version')
        self.assertIn('Coordinator 0.3.1', self.last_text())

    async def test_failed_correction_invalidates_old_preview(self):
        draft = await self.draft()
        await self.send(0, 'Add Someone Unknown', interpreted('request', participant_action='add', participants=['Someone Unknown']))
        await self.click(0, 'startdraft:' + draft['preview_nonce'])
        self.assertIn('out of date', self.last_text())
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 0)

    async def test_changed_date_drops_old_organizer_availability(self):
        await self.draft(intervals=[{'start': self.start, 'end': self.end}])
        await self.send(0, 'Actually Friday, same hours', interpreted('request',
                        range_start='2026-11-13T14:00+06:00', range_end='2026-11-13T17:00+06:00'))
        self.assertNotIn('owner_availability', json.loads(self.person()['draft']))

    async def test_explicit_new_request_does_not_reuse_old_participants(self):
        await self.draft()
        await self.send(0, 'Start another request', interpreted('request', new_request=True))
        draft = json.loads(self.person()['draft'])
        self.assertNotIn('participants', draft)
        self.assertNotIn('range_start', draft)
        self.assertIn('Who should', self.last_text())

    async def test_partial_availability_remembers_start_across_small_talk(self):
        await self.begin()
        self.deliver()
        await self.send(1, 'After 2:30', interpreted('availability', availability_notes={
                        'date': None, 'start_time': '14:30', 'end_time': None, 'timezone': None}))
        self.assertEqual(self.last_text(), 'Until what time are you available?')
        await self.send(1, 'Thanks', interpreted('chat', reply='You’re welcome.'))
        await self.send(1, '4 pm', interpreted('availability', availability_notes={
                        'date': None, 'start_time': None, 'end_time': '16:00', 'timezone': None}))
        own = self.store.one('SELECT availability FROM attendees WHERE meeting_id=1 AND employee_id=?', (self.ids[1],))
        intervals = json.loads(own['availability'])
        self.assertEqual(instant(intervals[0]['start']), instant('2026-11-12T14:30:00+06:00'))
        self.assertEqual(instant(intervals[0]['end']), instant('2026-11-12T16:00:00+06:00'))
        self.assertEqual(self.ai.calls[-1]['context']['conversation_memory']['availability_drafts']['1']['start_time'], '14:30')

    async def test_complete_availability_ignores_redundant_question(self):
        await self.begin()
        await self.send(1, '2 to 4', interpreted('availability', intervals=[{'start': self.start, 'end': '2026-11-12T16:00+06:00'}], question='Which time zone?'))
        self.assertIn('availability saved', self.last_text())

    async def test_availability_correction_reuses_previous_explicit_start(self):
        await self.begin()
        await self.send(1, '2 to 3 works', interpreted('availability', intervals=[{
                        'start': self.start, 'end': '2026-11-12T15:00+06:00'}]))
        await self.send(1, 'Actually until 4', interpreted('availability', availability_notes={
                        'date': None, 'start_time': None, 'end_time': '16:00', 'timezone': None}))
        own = self.store.one('SELECT availability FROM attendees WHERE meeting_id=1 AND employee_id=?', (self.ids[1],))
        self.assertEqual(instant(json.loads(own['availability'])[0]['start']), instant(self.start))
        self.assertEqual(instant(json.loads(own['availability'])[0]['end']), instant('2026-11-12T16:00+06:00'))

    async def test_ambiguous_add_remembers_operation_and_keeps_existing_attendees(self):
        design = self.store.add_employee('Sara', 'Design', 'Asia/Dhaka')
        await self.draft()
        await self.send(0, 'Also Sara', interpreted('request', participant_action='add', participants=['Sara']))
        draft = json.loads(self.person()['draft'])
        self.assertEqual(draft['pending_participant_change']['action'], 'add')
        self.assertIn('Design', self.last_text())
        await self.send(0, 'The one in Design', interpreted('request', participant_action='add', participants=[f'E-{design}']))
        self.assertEqual(set(json.loads(self.person()['draft'])['employee_ids']), set(self.ids + [design]))

    async def test_unknown_reply_does_not_guess_between_two_meetings(self):
        await self.begin()
        await self.begin()
        await self.send(1, '2 to 4', interpreted('availability', intervals=[{
                        'start': self.start, 'end': '2026-11-12T16:00+06:00'}]), reply_to=999999)
        self.assertIn('Which meeting', self.last_text())
        self.assertTrue(all(row['status'] == 'pending' for row in self.store.all(
            'SELECT status FROM attendees WHERE employee_id=?', (self.ids[1],))))

    async def test_reply_to_older_invitation_with_multiple_meetings_binds_correct_meeting(self):
        await self.begin()
        await self.begin()
        self.deliver()
        first = self.store.one("SELECT * FROM outbox WHERE employee_id=? AND meeting_id=1 AND purpose='invitation'", (self.ids[1],))
        await self.send(1, '2:30 to 4', interpreted('availability', intervals=[{
                        'start': '2026-11-12T14:30+06:00', 'end': '2026-11-12T16:00+06:00'}]), reply_to=first['message_id'])
        statuses = self.store.all('SELECT meeting_id,status FROM attendees WHERE employee_id=? ORDER BY meeting_id', (self.ids[1],))
        self.assertEqual(statuses, [{'meeting_id': 1, 'status': 'available'}, {'meeting_id': 2, 'status': 'pending'}])
        self.assertEqual(self.ai.calls[-1]['context']['focus_meeting_id'], 1)

    async def test_delivered_preview_go_ahead_starts_once_without_another_ai_call(self):
        await self.draft()
        self.deliver()
        calls = len(self.ai.calls)
        await self.send(0, 'yes, go ahead')
        self.assertEqual(self.store.meeting(1)['state'], 'collecting')
        self.assertEqual(len(self.ai.calls), calls)
        self.assertTrue(json.loads(self.person()['history']))
        await self.send(0, 'go ahead')
        self.assertEqual(self.store.one('SELECT count(*) AS n FROM meetings')['n'], 1)

    async def test_second_option_and_natural_yes_coordinate_everyone(self):
        m = await self.options()
        self.deliver()
        await self.send(0, 'the second one', interpreted('choose', option_number=2))
        proposal = json.loads(self.store.meeting(m['id'])['proposal'])
        self.assertEqual(proposal, json.loads(m['options'])[1])
        self.deliver()
        for i in (1, 2, 3):
            await self.send(i, 'yes, that works')
            if i != 3:
                self.assertEqual(self.store.meeting(m['id'])['state'], 'confirming')
        self.assertEqual(self.store.meeting(m['id'])['state'], 'confirmed')

    async def test_stale_proposal_reply_cannot_confirm_new_time(self):
        m = await self.options()
        await self.click(0, f"choose:1:{m['version']}:0")
        self.deliver()
        old = self.store.one("SELECT * FROM outbox WHERE employee_id=? AND purpose='confirmation'", (self.ids[1],))
        await self.send(2, 'Actually 3 to 5', interpreted('availability', intervals=[{
                        'start': '2026-11-12T15:00+06:00', 'end': self.end}]))
        updated = self.store.meeting(1)
        await self.click(0, f"choose:1:{updated['version']}:0")
        await self.send(1, 'yes', reply_to=old['message_id'])
        self.assertIn('changed', self.last_text())
        own = self.store.one('SELECT accepted_version FROM attendees WHERE meeting_id=1 AND employee_id=?', (self.ids[1],))
        self.assertIsNone(own['accepted_version'])

    async def test_context_contains_own_availability_options_and_authorized_names(self):
        await self.options()
        owner_context = self.engine.context(self.person(), self.now.timestamp())
        self.assertEqual(len(owner_context['meetings'][0]['options']), 3)
        self.assertTrue(owner_context['meetings'][0]['attendees'])
        member_context = self.engine.context(self.person(1), self.now.timestamp())
        self.assertEqual(member_context['authorized_company_directory'], [])
        self.assertNotIn('attendees', member_context['meetings'][0])
        self.assertTrue(member_context['meetings'][0]['your_availability'])

    async def test_commands_reports_errors_and_reminders_are_remembered(self):
        await self.begin()
        await self.send(1, '/status')
        self.store.tell(self.ids[1], 'A reminder', meeting_id=1, purpose='reminder')
        history = self.store.conversation_history(self.ids[1])
        self.assertTrue(any(h['role'] == 'user' and h['text'] == '/status' for h in history))
        self.assertTrue(any(h.get('purpose') == 'control' for h in history))
        self.assertEqual(history[-1]['text'], 'A reminder')

    async def test_directory_is_answered_from_database_and_member_cannot_browse(self):
        await self.send(0, 'Who is in Engineering?', interpreted('directory', participants=['Engineering']))
        self.assertIn('Ayesha', self.last_text())
        await self.send(1, 'Who is in Engineering?', interpreted('directory', participants=['Engineering']))
        self.assertIn('Only authorized', self.last_text())

    async def test_new_clears_partial_context_without_cancelling_existing_meeting(self):
        await self.begin()
        self.store.remember_state(self.ids[0], clarification={'task': 'request'})
        await self.send(0, '/new')
        self.assertEqual(self.store.conversation(self.ids[0]), {})
        self.assertEqual(self.store.meeting(1)['state'], 'collecting')


class ConversationPersistenceTests(unittest.TestCase):
    def test_partial_facts_survive_restart_and_history_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'company.sqlite3')
            store = Store(path)
            uid = store.add_employee('Test Member', 'Test Team', 'Asia/Dhaka')
            store.remember_state(uid, focus_meeting_id=9, availability_drafts={'9': {'start_time': '14:30'}})
            for i in range(55):
                store.remember(uid, 'user', f'Turn {i}')
            store.db.close()
            reopened = Store(path)
            self.assertEqual(len(reopened.conversation_history(uid)), 40)
            self.assertEqual(reopened.conversation(uid)['availability_drafts']['9']['start_time'], '14:30')
            reopened.db.close()


class EvaluationHarnessTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_harness_uses_only_fictional_in_memory_context(self):
        from meetingbot.conversation_eval import run_language_checks
        captured = []

        class FakePlanner:
            def __init__(self, settings, store):
                self.client = type('Client', (), {'close': AsyncMock()})()
                self.store = store

            async def interpret(self, text, context, history):
                captured.append(context)
                index = len(captured) - 1
                if index == 0:
                    return interpreted('chat', reply='Hello!')
                if index == 1:
                    return interpreted('request', duration_minutes=45)
                if index == 2:
                    return interpreted('request', participant_action='add', participants=['E-5'])
                if index in (3, 4):
                    day = context['meetings'][0]['start'][:10]
                    return interpreted('availability', intervals=[{'start': day+'T14:30+06:00', 'end': day+'T16:00+06:00'}])
                if index == 5:
                    return interpreted('choose', option_number=2)
                if index == 6:
                    return interpreted('status')
                if index == 7:
                    return interpreted('directory', participants=['Engineering'])
                if index == 8:
                    return interpreted('request', participant_targets=[{
                        'kind': 'team', 'member_id': None, 'team_name': 'Management', 'source_text': 'management folks'}])
                if index in (9, 10):
                    return interpreted('request', participant_targets=[{
                        'kind': 'member', 'member_id': 6, 'team_name': None, 'source_text': 'Shanto'}])
                draft = context['existing_draft']
                return interpreted('request', range_start=(instant(draft['range_start']) + timedelta(days=1)).isoformat(),
                                   range_end=(instant(draft['range_end']) + timedelta(days=1)).isoformat())

        output = io.StringIO()
        with patch('meetingbot.conversation_eval.socket.getaddrinfo', return_value=[]), \
             patch('meetingbot.conversation_eval.OpenAIInterpreter', FakePlanner), redirect_stdout(output):
            ok = await run_language_checks(Settings(openai_key='unit-test-only'))
        self.assertTrue(ok)
        self.assertEqual(len(captured), 12)
        self.assertTrue(all(context['employee_name'] in ('Rehan', 'Ayesha') for context in captured))
