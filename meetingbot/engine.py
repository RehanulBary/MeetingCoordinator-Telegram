from datetime import datetime, timedelta, timezone
import json
import re
import secrets
import time
from zoneinfo import ZoneInfo
from . import COORDINATOR_VERSION, LOADED_REVISION
from .models import Interpretation, interpreted
from .scheduling import common_slots, instant, overlap, show_slot, valid_intervals, work_windows

HELP = '''I coordinate meetings through private chats.

Request: "Arrange a 30-minute Engineering meeting tomorrow between 14:00 and 17:00, Dhaka time."
I preview the exact people and dates before outreach. Your chosen range is your own stated availability.

/teams — teams you can request
/schedule — start a request
/new — discard only your unfinished draft
/status [M-1] — meeting progress
/cancel M-1 — cancel your meeting
/settings [IANA timezone] — your time zone
/stop — stop scheduling notifications
/resume — explicitly enable notifications again
/version — check the running coordinator update

Reply naturally; I keep track of the current question. Reply to a particular message to switch meetings.
Confirmed means agreement in chat; calendar invitations are not connected yet.'''


def buttons(*items):
    return [[{'text': label, 'callback_data': data}] for label, data in items]


class Engine:
    def __init__(self, store, interpreter, settings, now=None):
        self.store, self.interpreter, self.settings = store, interpreter, settings
        self.now = now or (lambda: datetime.now(timezone.utc))

    def prompt_info(self, row):
        if not row:
            return None
        actions = [button['callback_data'] for line in json.loads(row['keyboard'] or '[]') for button in line]
        kind = ('preview' if any(a.startswith('startdraft:') for a in actions) else
                'options' if any(a.startswith('choose:') for a in actions) else
                'confirmation' if any(a.startswith('accept:') for a in actions) else
                'cancellation' if any(a.startswith('cancel:') for a in actions) else
                'availability' if row['purpose'] in ('invitation', 'reminder', 'clarification') else 'message')
        return {'outbox_id': row['id'], 'meeting_id': row['meeting_id'], 'version': row['version'],
                'kind': kind, 'text': row['text'], 'delivered': row['status'] == 'sent',
                'preview_nonce': next((a.split(':', 1)[1] for a in actions if a.startswith('startdraft:')), None)}

    def context(self, person, timestamp, reply_to=None):
        memory = self.store.conversation(person['id'])
        pending = self.prompt_info(self.store.prompt(person['id']))
        replied = self.prompt_info(self.store.prompt(person['id'], reply_to)) if reply_to is not None else None
        focus = (replied or {}).get('meeting_id') or (memory.get('clarification') or {}).get('meeting_id') or \
                (pending or {}).get('meeting_id') or memory.get('focus_meeting_id')
        if reply_to is not None and not replied:
            focus = None
        meetings = []
        for meeting in self.store.meetings_for(person['id']):
            item = {k: meeting[k] for k in ('id', 'title', 'duration', 'start', 'end', 'state', 'version', 'deadline', 'rounds')}
            item['proposal'] = json.loads(meeting['proposal']) if meeting['proposal'] else None
            item['options'] = json.loads(meeting['options']) if meeting['state'] == 'options' else []
            item['you_are_organizer'] = meeting['owner'] == person['id']
            own = next(x for x in self.store.members(meeting['id']) if x['employee_id'] == person['id'])
            item['your_status'] = own['status']
            item['your_availability'] = json.loads(own['availability'])
            if item['you_are_organizer']:
                item['attendees'] = [{'id': x['employee_id'], 'name': x['name'], 'status': x['status']} for x in self.store.members(meeting['id'])]
            meetings.append(item)
        active = [m for m in meetings if m['state'] not in ('cancelled', 'confirmed')]
        if not focus and len(active) == 1:
            focus = active[0]['id']
        directory = []
        if person['can_request']:
            directory = self.store.all('SELECT id,name,team,timezone FROM employees WHERE active=1 ORDER BY name')
        return {
            'source_timestamp': datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            'employee_timezone': person['timezone'],
            'employee_name': person['name'], 'can_request': bool(person['can_request']),
            'existing_draft': json.loads(person['draft']) if person['draft'] else None,
            'conversation_memory': memory, 'focus_meeting_id': focus,
            'pending_prompt': pending, 'reply_to_prompt': replied,
            'reply_to_message_id': reply_to,
            'meetings': meetings, 'authorized_company_directory': directory,
            'company_teams': [{'name': name, 'member_ids': [x['id'] for x in directory if x['team'] == name]}
                              for name in sorted({x['team'] for x in directory})],
            'company_working_hours': f'{self.settings.work_start}:00–{self.settings.work_end}:00 ({self.settings.timezone})',
            'company_workdays_monday_zero': self.settings.workdays,
        }

    async def handle_update(self, update):
        existing = self.store.one('SELECT state FROM inbox WHERE update_id=?', (update['update_id'],))
        if existing and existing['state'] == 'done':
            return
        callback = update.get('callback_query')
        message = callback.get('message', {}) if callback else update.get('message', {})
        sender = callback.get('from', {}) if callback else message.get('from', {})
        chat = message.get('chat', {})
        if not chat or not sender or sender.get('is_bot'):
            self._done(update)
            return
        if chat.get('type') != 'private':
            self._done(update)
            return
        text = str(message.get('text', ''))[:6000] if not callback else ''
        person = self.store.by_telegram(sender['id'])
        parsed = None
        context = None
        ai_error = None
        if person and person['opted_in'] and not callback and text and not text.startswith('/'):
            try:
                context = self.context(person, message.get('date', time.time()), (message.get('reply_to_message') or {}).get('message_id'))
                prompt = context.get('reply_to_prompt') if context.get('reply_to_message_id') is not None else context.get('pending_prompt')
                short_yes = re.fullmatch(r'(?:yes(?:[,! ]+(?:please|go ahead|that works))?|go ahead|looks good|start(?: coordination)?|confirm(?:ed)?|yes[,! ]+start it)[.! ]*', text.strip(), re.I)
                if short_yes and prompt and prompt['kind'] in ('preview', 'confirmation', 'cancellation'):
                    parsed = interpreted('start' if prompt['kind'] == 'preview' else 'confirm',
                                         meeting_reference=str(prompt['meeting_id']) if prompt['meeting_id'] else None)
                else:
                    parsed = await self.interpreter.interpret(text, context, self.store.conversation_history(person['id']))
            except Exception as exc:
                known = ('The daily AI call limit is reached.', 'I could not interpret that reply just now.')
                ai_error = str(exc) if isinstance(exc, ValueError) and str(exc).startswith(known) else \
                           'I couldn’t interpret that reply just now. Your saved meeting details are still here. Please try your last message again.'
        with self.store.transaction():
            existing = self.store.one('SELECT state FROM inbox WHERE update_id=?', (update['update_id'],))
            if existing and existing['state'] == 'done':
                return
            person = self.store.by_telegram(sender['id'])
            if person:
                self.store.db.execute('UPDATE employees SET reachable=1 WHERE id=?', (person['id'],))
                remembered = ('Selected ' + callback.get('data', '').split(':')[0]) if callback else \
                             '/start' if text.startswith('/start') else text
                self.store.remember(person['id'], 'user', remembered, update_id=update['update_id'])
            try:
                if callback:
                    if person and person['opted_in']:
                        self.action(person, callback.get('data', ''))
                    else:
                        self.store.queue(chat['id'], 'Complete company enrollment and enable notifications first.')
                elif text.startswith('/start'):
                    self.start(person, text, sender, chat['id'])
                elif not person:
                    self.store.queue(chat['id'], 'This is a private company meeting bot. Ask the company admin '
                                     'for your enrollment link. Your Telegram ID is ' + str(sender['id']) + '.')
                elif text.startswith('/'):
                    self.command(person, text)
                elif not person['opted_in']:
                    self.store.tell(person['id'], 'Notifications are stopped. Send /resume to explicitly enable them.', purpose='control')
                elif ai_error:
                    self.store.tell(person['id'], ai_error)
                elif parsed:
                    self.apply(person, parsed, context)
                else:
                    self.store.tell(person['id'], 'Please send a text message or use /help.')
            except (ValueError, KeyError) as exc:
                # These are validated domain errors, never external API error bodies.
                if person:
                    self.store.tell(person['id'], str(exc)[:500], purpose='control')
                else:
                    self.store.queue(chat['id'], str(exc)[:500], purpose='control')
            self._done(update)

    def _done(self, update):
        self.store.db.execute("UPDATE inbox SET state='done' WHERE update_id=?", (update['update_id'],))

    def start(self, person, text, sender, chat_id):
        if person:
            self.store.tell(person['id'], f"Welcome back, {person['name']}. " +
                            ('Use /help to coordinate a meeting.' if person['opted_in'] else 'Notifications remain stopped. Use /resume to enable them.'), purpose='control')
            return
        parts = text.split(maxsplit=1)
        if len(parts) == 2 and self.store.claim(parts[1], sender['id'], chat_id, sender.get('first_name', '')):
            self.store.queue(chat_id, 'Enrollment request saved. The company admin must verify your identity '
                             'and approve it before you can access company meetings. Your Telegram ID: ' + str(sender['id']))
        else:
            self.store.queue(chat_id, 'Ask the company admin for an unused enrollment link. '
                             'Your Telegram ID: ' + str(sender['id']))

    def command(self, person, text):
        parts = text.split(maxsplit=1)
        cmd = parts[0].split('@')[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ''
        uid = person['id']
        if cmd in ('/help', '/schedule'):
            self.store.tell(uid, HELP if cmd == '/help' else 'Tell me the team or members, duration, date range, '
                            'and your available times. I will show a preview before contacting anyone.', purpose='control')
        elif cmd == '/teams':
            if not person['can_request']:
                raise ValueError('Only authorized meeting requesters can browse the team directory.')
            rows = self.store.all('SELECT team,count(*) AS n FROM employees WHERE active=1 GROUP BY team ORDER BY team')
            self.store.tell(uid, 'Company teams:\n' + '\n'.join(f"• {x['team']} ({x['n']} members)" for x in rows))
        elif cmd == '/version':
            self.store.tell(uid, f'Coordinator {COORDINATOR_VERSION} ({LOADED_REVISION}).\n'
                            'Conversation memory and canonical team/member resolution are enabled.', purpose='control')
        elif cmd == '/settings':
            if arg:
                try:
                    ZoneInfo(arg)
                except Exception:
                    raise ValueError('Use an IANA time zone, such as Asia/Dhaka or Europe/London.')
                self.store.db.execute('UPDATE employees SET timezone=? WHERE id=?', (arg, uid))
            self.store.tell(uid, 'Time zone: ' + (arg or person['timezone']) + '\nNotifications: ' +
                            ('enabled' if person['opted_in'] else 'stopped'), purpose='control')
        elif cmd == '/new':
            self.store.db.execute('UPDATE employees SET draft=NULL,history=? WHERE id=?', ('[]', uid))
            self.store.db.execute("UPDATE employees SET conversation='{}' WHERE id=?", (uid,))
            self.store.tell(uid, 'Unfinished draft cleared. Existing meetings are unchanged.', purpose='control')
        elif cmd in ('/status', '/cancel'):
            meeting = self.resolve(person, arg or None, allow_many=cmd == '/status')
            if cmd == '/cancel':
                self.cancel(person, meeting)
            elif isinstance(meeting, list):
                self.store.tell(uid, '\n\n'.join(self.report(m, person) for m in meeting) or 'No meetings yet.', purpose='control')
            else:
                self.store.tell(uid, self.report(meeting, person), purpose='control')
        elif cmd == '/stop':
            self.store.db.execute('UPDATE employees SET opted_in=0 WHERE id=?', (uid,))
            for m in self.store.meetings_for(uid, active_only=True):
                self.store.db.execute("UPDATE attendees SET status='opted_out',accepted_version=NULL WHERE meeting_id=? AND employee_id=?", (m['id'], uid))
                self.store.db.execute("UPDATE meetings SET state='needs_decision',version=version+1 WHERE id=?", (m['id'],))
                self.store.tell(m['owner'], f"M-{m['id']}: {person['name']} stopped scheduling notifications. Your decision is needed.", purpose='report')
            self.store.tell(uid, 'Scheduling notifications stopped. Use /resume to enable them again.', purpose='control')
        elif cmd == '/resume':
            self.store.db.execute('UPDATE employees SET opted_in=1 WHERE id=?', (uid,))
            self.store.tell(uid, 'Notifications enabled. Your availability and acceptance status will be '
                            'shared with organizers for meetings you participate in.', purpose='control')
        elif cmd == '/available':
            fields = arg.split()
            if len(fields) != 3:
                raise ValueError('Format: /available M-1 2026-11-18T14:00+06:00 2026-11-18T16:00+06:00')
            self.availability(person, self.resolve(person, fields[0]), [{'start': fields[1], 'end': fields[2]}])
        else:
            self.store.tell(uid, 'Unknown command. Use /help.', purpose='control')

    def resolve(self, person, reference, allow_many=False):
        if reference:
            match = re.fullmatch(r'(?:M-)?(\d+)', str(reference).strip(), re.I)
            if not match:
                raise ValueError('Use a meeting reference such as M-1.')
            m = self.store.meeting(int(match.group(1)))
            if not m or not self.store.one('SELECT 1 FROM attendees WHERE meeting_id=? AND employee_id=?', (m['id'], person['id'])):
                raise ValueError('That meeting is unavailable to your account.')
            return m
        meetings = self.store.meetings_for(person['id'], active_only=not allow_many)
        if allow_many:
            return meetings
        if len(meetings) != 1:
            raise ValueError('Please include the meeting reference, for example M-1.')
        return meetings[0]

    def resolve_reply(self, person, reference, context=None):
        if reference:
            return self.resolve(person, reference)
        if (context or {}).get('reply_to_message_id') is not None and not context.get('reply_to_prompt'):
            raise ValueError('I can’t match that older message to a meeting. Which meeting did you mean?')
        focus = (context or {}).get('focus_meeting_id')
        if focus:
            return self.resolve(person, str(focus))
        return self.resolve(person, None)

    def approval_prompt(self, person, context, kind, meeting=None):
        context = context or {}
        prompt = context.get('reply_to_prompt') if context.get('reply_to_message_id') is not None else context.get('pending_prompt')
        if not prompt or prompt['kind'] != kind or not prompt['delivered']:
            return None
        row = self.store.one("SELECT * FROM outbox WHERE id=? AND employee_id=? AND status='sent'", (prompt['outbox_id'], person['id']))
        if not row:
            return None
        if meeting and (prompt['meeting_id'] != meeting['id'] or prompt['version'] != meeting['version']):
            raise ValueError('That proposal has changed. Please respond to the latest times I sent you.')
        return prompt

    def apply(self, person, result: Interpretation, context=None):
        if result.intent == 'request':
            self.draft(person, result)
        elif result.intent == 'start':
            prompt = self.approval_prompt(person, context, 'preview')
            if prompt:
                self.begin(person, prompt['preview_nonce'])
            elif person['draft']:
                self.draft(person, result)
            else:
                self.store.tell(person['id'], 'Tell me who you want to meet and when, and I’ll put together a request.')
        elif result.intent == 'discard':
            self.store.db.execute('UPDATE employees SET draft=NULL WHERE id=?', (person['id'],))
            self.store.remember_state(person['id'], clarification=None)
            self.store.tell(person['id'], 'I’ve discarded the unfinished request. Your existing meetings are unchanged.')
        elif result.intent == 'choose':
            meeting = self.resolve_reply(person, result.meeting_reference, context)
            prompt = self.approval_prompt(person, context, 'options', meeting)
            if not prompt:
                raise ValueError('Please choose from the latest time options I sent you, or use the option buttons.')
            if result.option_number is None:
                raise ValueError('Which of those time options would you like? You can say “the second one”.')
            self.choose(person, meeting, result.option_number - 1)
        elif result.intent == 'availability':
            self.availability_reply(person, self.resolve_reply(person, result.meeting_reference, context), result)
        elif result.intent in ('confirm', 'decline'):
            meeting = self.resolve_reply(person, result.meeting_reference, context)
            if result.question:
                self.store.tell(person['id'], result.question)
            elif result.intent == 'confirm':
                if self.approval_prompt(person, context, 'cancellation', meeting):
                    self.cancel(person, meeting)
                    return
                # A yes to an availability invitation is not acceptance of a meeting slot.
                if self.approval_prompt(person, context, 'confirmation', meeting):
                    self.accept(person, meeting, meeting['version'])
                    return
                if meeting['state'] == 'collecting':
                    self.store.tell(person['id'], 'Great—what times work for you within ' +
                                    show_slot({'start': meeting['start'], 'end': meeting['end']}, person['timezone']) + '?',
                                    meeting_id=meeting['id'], purpose='clarification')
                    return
                if meeting['state'] != 'confirming' or not meeting['proposal']:
                    raise ValueError('There is no current confirmation proposal. Use /status.')
                self.store.tell(person['id'], f"Please use a button to confirm the exact M-{meeting['id']} proposal:\n" +
                                show_slot(json.loads(meeting['proposal']), person['timezone']),
                                keyboard=buttons(('Confirm', f"accept:{meeting['id']}:{meeting['version']}"),
                                                 ('Decline', f"decline:{meeting['id']}:{meeting['version']}")),
                                meeting_id=meeting['id'], version=meeting['version'], purpose='confirmation')
            else:
                self.decline(person, meeting)
        elif result.intent == 'status':
            if result.meeting_reference or (context or {}).get('focus_meeting_id'):
                m = self.resolve_reply(person, result.meeting_reference, context)
            else:
                m = self.resolve(person, None, allow_many=True)
            self.store.tell(person['id'], ('\n\n'.join(self.report(x, person) for x in m) or 'No meetings yet.') if isinstance(m, list) else self.report(m, person))
        elif result.intent == 'cancel':
            meeting = self.resolve_reply(person, result.meeting_reference, context)
            if meeting['owner'] != person['id']:
                raise ValueError('Only the organizer can cancel this meeting.')
            self.store.tell(person['id'], f"Cancel M-{meeting['id']} and notify its participants?",
                            keyboard=buttons(('Cancel this meeting', f"cancel:{meeting['id']}:{meeting['version']}")),
                            meeting_id=meeting['id'], version=meeting['version'])
        elif result.intent == 'directory':
            if not person['can_request']:
                raise ValueError('Only authorized meeting requesters can browse the team directory.')
            rows = self.store.all('SELECT id,name,team FROM employees WHERE active=1 ORDER BY team,name')
            if result.participant_targets:
                queries = self.canonical_queries(result)
                allowed_ids = {x['id'] for query in queries for x in self.matching_people(query, rows)}
                rows = [x for x in rows if x['id'] in allowed_ids]
            elif result.participants:
                terms = [self.person_words(x) for x in result.participants]
                rows = [x for x in rows if any(words <= self.person_words(x['name'] + ' ' + x['team']) for words in terms)]
            self.store.tell(person['id'], '\n'.join(f"• {x['name']} — {x['team']} (E-{x['id']})" for x in rows) or
                            'I couldn’t find that name or team in the company directory. Which person did you mean?')
        else:
            self.store.tell(person['id'], result.reply or result.question or
                            'I can help coordinate a meeting. Tell me who you’d like to meet and when.')

    def availability_reply(self, person, meeting, result):
        if meeting['state'] == 'cancelled':
            raise ValueError('This meeting was cancelled. There’s no availability to collect for it.')
        if self.now().timestamp() >= meeting['deadline']:
            raise ValueError('The response deadline passed. Ask the organizer to revise this meeting.')
        memory = self.store.conversation(person['id'])
        drafts = memory.get('availability_drafts', {})
        intervals = [x.model_dump() for x in result.intervals]
        notes = dict(drafts.get(str(meeting['id']), {}))
        if not notes and result.availability_mode == 'replace' and result.availability_notes and \
                any(getattr(result.availability_notes, field) for field in ('date', 'start_time', 'end_time')):
            own = next(x for x in self.store.members(meeting['id']) if x['employee_id'] == person['id'])
            previous = json.loads(own['availability'])
            if len(previous) == 1:
                tz = ZoneInfo(person['timezone'])
                start, end = (instant(previous[0][key]).astimezone(tz) for key in ('start', 'end'))
                if start.date() == end.date():
                    notes = {'date': start.date().isoformat(), 'start_time': start.strftime('%H:%M'),
                             'end_time': end.strftime('%H:%M'), 'timezone': person['timezone']}
        if result.availability_notes:
            notes.update({k: v for k, v in result.availability_notes.model_dump().items() if v is not None})
        if not intervals and notes:
            zone = notes.get('timezone') or person['timezone']
            try:
                tz = ZoneInfo(zone)
            except Exception:
                raise ValueError('Which time zone are those times in? Your saved time zone is ' + person['timezone'] + '.') from None
            notes['timezone'] = zone
            local_start, local_end = instant(meeting['start']).astimezone(tz), instant(meeting['end']).astimezone(tz)
            if not notes.get('date') and local_start.date() == local_end.date():
                notes['date'] = local_start.date().isoformat()
            for key in ('start_time', 'end_time'):
                if notes.get(key) and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', notes[key]):
                    raise ValueError('Please clarify that time, for example “2:30 pm”.')
            if notes.get('date'):
                try:
                    datetime.strptime(notes['date'], '%Y-%m-%d')
                except ValueError:
                    raise ValueError('Which date did you mean?') from None
            if all(notes.get(k) for k in ('date', 'start_time', 'end_time')):
                endpoints = [datetime.fromisoformat(notes['date'] + 'T' + notes[k]).replace(tzinfo=tz)
                             for k in ('start_time', 'end_time')]
                if any(t.replace(fold=0).utcoffset() != t.replace(fold=1).utcoffset() for t in endpoints):
                    raise ValueError('That local time crosses a daylight-saving transition. Please include the UTC offset.')
                intervals = [{'start': endpoints[0].isoformat(), 'end': endpoints[1].isoformat()}]
        if intervals:
            if result.availability_mode == 'add':
                own = next(x for x in self.store.members(meeting['id']) if x['employee_id'] == person['id'])
                intervals = json.loads(own['availability']) + intervals
            self.availability(person, meeting, intervals)
            drafts.pop(str(meeting['id']), None)
            self.store.remember_state(person['id'], availability_drafts=drafts, clarification=None,
                                      focus_meeting_id=meeting['id'])
            return
        if notes:
            drafts[str(meeting['id'])] = notes
        missing = next((key for key in ('date', 'start_time', 'end_time') if not notes.get(key)), 'end_time')
        question = {'date': 'Which day works for you?', 'start_time': 'What time can you start?',
                    'end_time': 'Until what time are you available?'}[missing] if notes else result.question or \
                   'What time works for you within ' + show_slot({'start': meeting['start'], 'end': meeting['end']}, person['timezone']) + '?'
        self.store.remember_state(person['id'], availability_drafts=drafts,
                                  clarification={'task': 'availability', 'meeting_id': meeting['id'], 'field': missing, 'question': question})
        self.store.tell(person['id'], question, meeting_id=meeting['id'], purpose='clarification')

    def draft(self, person, result):
        if not person['can_request']:
            raise ValueError('Your account can reply to meetings; ask an admin for requester access.')
        if not person['opted_in']:
            raise ValueError('Use /resume before starting a meeting.')
        data = json.loads(person['draft']) if person['draft'] and not result.new_request else {}
        if result.new_request:
            self.store.remember_state(person['id'], clarification=None, focus_meeting_id=None)
        data.pop('employee_ids', None)
        data.pop('preview_nonce', None)
        # Invalidate the old preview before resolving corrections that may need clarification.
        self.store.db.execute('UPDATE employees SET draft=? WHERE id=?', (json.dumps(data), person['id']))
        queries = self.canonical_queries(result) if result.participant_targets else result.participants
        old_range = (data.get('range_start'), data.get('range_end'))
        for key in result.clear_fields:
            data.pop(key, None)
        for key in ('title', 'duration_minutes', 'range_start', 'range_end'):
            value = getattr(result, key)
            if value is not None:
                data[key] = value
        if queries:
            if result.participant_action == 'replace' or not data.get('participants'):
                if result.participant_action != 'remove':
                    data['participants'] = queries
            else:
                data['pending_participant_change'] = {'action': result.participant_action, 'queries': queries}
                self.store.db.execute('UPDATE employees SET draft=? WHERE id=?', (json.dumps(data), person['id']))
                existing = set(self.resolve_people(data['participants'], person['id']))
                rows = self.store.all('SELECT * FROM employees WHERE active=1')
                delta = {x['id'] for query in queries for x in self.matching_people(query, rows)}
                existing = existing | delta if result.participant_action == 'add' else existing - delta
                data['participants'] = [f'E-{uid}' for uid in sorted(existing - {person['id']})]
            data.pop('pending_participant_change', None)
        if old_range != (data.get('range_start'), data.get('range_end')):
            data.pop('owner_availability', None)
        if result.intervals:
            data['owner_availability'] = [x.model_dump() for x in result.intervals]
        if 'duration_minutes' not in data and 'duration_minutes' not in result.clear_fields:
            data['duration_minutes'] = 30
            data['duration_defaulted'] = True
        elif result.duration_minutes is not None:
            data['duration_defaulted'] = False
        if not data.get('title'):
            data['title'] = 'Meeting with ' + ', '.join(data.get('participants', [])) if data.get('participants') else 'Team meeting'
        self.store.db.execute('UPDATE employees SET draft=? WHERE id=?', (json.dumps(data), person['id']))
        required = ('participants', 'range_start', 'range_end', 'duration_minutes')
        missing = [key for key in required if not data.get(key)]
        if missing:
            field = missing[0]
            question = {'participants': 'Who should join—one of your teams, or specific people?',
                        'range_start': 'When would you like to meet? A day or a date range is enough to start.',
                        'range_end': 'Until what time, or through which date, should I look?',
                        'duration_minutes': 'How long should the meeting be?'}[field]
            # A model question can express a real time ambiguity, but must not request a title
            # or details we already know. Defaults are shown explicitly in the preview.
            if result.question and not re.search(r'\b(title|time\s*zone|timezone|how long|duration)\b', result.question, re.I):
                question = result.question
            self.store.remember_state(person['id'], clarification={'task': 'request', 'field': field, 'question': question})
            self.store.tell(person['id'], question, purpose='clarification')
            return
        try:
            selected = self.resolve_people(data['participants'], person['id'])
        except ValueError as exc:
            data['participant_resolution'] = {'status': 'unresolved', 'queries': data['participants'], 'detail': str(exc)}
            self.store.db.execute('UPDATE employees SET draft=? WHERE id=?', (json.dumps(data), person['id']))
            self.store.remember_state(person['id'], clarification={'task': 'request', 'field': 'participants', 'question': str(exc)})
            raise
        data.pop('participant_resolution', None)
        if not 5 <= data['duration_minutes'] <= 240:
            raise ValueError('Choose a duration between 5 and 240 minutes.')
        start, end = instant(data['range_start']), instant(data['range_end'])
        if end <= start or start <= self.now() or end - start > timedelta(days=31):
            raise ValueError('Choose a future date range with an end after its start, at most 31 days long.')
        if end - start < timedelta(minutes=data['duration_minutes']):
            raise ValueError('The requested time range is shorter than the meeting duration.')
        windows = work_windows(data['range_start'], data['range_end'], self.settings.timezone,
                               self.settings.work_start, self.settings.work_end, self.settings.workdays)
        if not common_slots([windows], data['duration_minutes'], limit=1):
            raise ValueError('That range has no time within the configured company working hours/days. Please revise it.')
        if data.get('owner_availability'):
            valid_intervals(data['owner_availability'], data['range_start'], data['range_end'])
        data['employee_ids'] = selected
        data['preview_nonce'] = secrets.token_hex(6)
        self.store.db.execute('UPDATE employees SET draft=? WHERE id=?', (json.dumps(data), person['id']))
        people = [self.store.employee(uid) for uid in selected]
        names = ', '.join(p['name'] for p in people if p['id'] != person['id'])
        own_availability = '\nYour stated availability:\n' + '\n'.join(
            show_slot(slot, person['timezone']) for slot in data['owner_availability']) if data.get('owner_availability') else ''
        self.store.remember_state(person['id'], clarification=None)
        default_note = ' (default; you can change it)' if data.get('duration_defaulted') else ''
        self.store.tell(person['id'], f"Preview: {data['title']}\n{data['duration_minutes']} minutes\n"
                        + (f'Duration: 30 minutes{default_note}.\n' if default_note else '')
                        + show_slot({'start': data['range_start'], 'end': data['range_end']}, person['timezone'])
                        + own_availability
                        + '\nRequired people: ' + names + f" and you ({person['name']}).\n"
                        'Starting confirms that this range is your own stated availability. '
                        f'Company hours: {self.settings.work_start}:00–{self.settings.work_end}:00 ({self.settings.timezone}). '
                        'Say “go ahead” or use Start coordination, and I’ll ask the others. No calendar event is created.',
                        keyboard=buttons(('Start coordination', 'startdraft:' + data['preview_nonce']), ('Discard draft', 'discarddraft')))

    @staticmethod
    def person_words(value):
        return set(re.findall(r'\w+', value.casefold())) - {'the', 'team', 'department', 'from', 'with'}

    def canonical_queries(self, result):
        queries = []
        for target in result.participant_targets:
            if target.kind == 'member' and target.member_id is not None and target.team_name is None:
                person = self.store.employee(target.member_id)
                if not person or not person['active']:
                    raise ValueError('That person is no longer in the active company directory. Who did you mean?')
                queries.append(f'E-{person["id"]}')
            elif target.kind == 'team' and target.team_name and target.member_id is None:
                teams = [x for x in self.store.all('SELECT DISTINCT team FROM employees WHERE active=1')
                         if x['team'].casefold() == target.team_name.casefold()]
                if len(teams) != 1:
                    raise ValueError('I couldn’t identify that company team. Which team did you mean?')
                queries.append(teams[0]['team'])
            else:
                raise ValueError('I couldn’t identify that attendee clearly. Which person or team did you mean?')
        return list(dict.fromkeys(queries))

    def matching_people(self, query, rows):
        q = query.strip().casefold()
        teams = [x for x in rows if x['team'].casefold() == q]
        if not teams and self.person_words(q):
            teams = [x for x in rows if self.person_words(x['team']) == self.person_words(q)]
            if len({x['team'] for x in teams}) > 1:
                raise ValueError('Which company team did you mean? ' + ', '.join(sorted({x['team'] for x in teams})))
        names = [x for x in rows if x['name'].casefold() == q or str(x['id']) == q.removeprefix('e-')]
        if teams:
            return teams
        if not names:
            words = self.person_words(q)
            names = [x for x in rows if words and words <= self.person_words(x['name'] + ' ' + x['team'])]
        if len(names) == 1:
            return names
        if len(names) > 1:
            raise ValueError('That name is ambiguous. Which ' + query[:80] + ' did you mean?\n' +
                             '\n'.join(f"• {x['name']} — {x['team']} (E-{x['id']})" for x in names[:10]))
        raise ValueError('I couldn’t find a company team or member called ' + query[:120] + '. Which person did you mean?')

    def resolve_people(self, queries, owner):
        if not queries or len(queries) > 20:
            raise ValueError('Specify between 1 and 20 names or team names.')
        rows = self.store.all('SELECT * FROM employees WHERE active=1')
        chosen = {owner}
        for query in queries:
            chosen.update(x['id'] for x in self.matching_people(query, rows))
        if not 2 <= len(chosen) <= 20:
            raise ValueError('Choose 1–19 other attendees for this pilot.')
        return sorted(chosen)

    def action(self, person, data):
        if data == 'discarddraft':
            self.store.db.execute('UPDATE employees SET draft=NULL,history=? WHERE id=?', ('[]', person['id']))
            self.store.remember_state(person['id'], clarification=None)
            self.store.tell(person['id'], 'Draft discarded.')
            return
        if data.startswith('startdraft:'):
            self.begin(person, data.split(':', 1)[1])
            return
        parts = data.split(':')
        if len(parts) < 3 or parts[0] not in ('choose', 'accept', 'decline', 'cancel'):
            raise ValueError('This button is no longer valid. Use /status.')
        try:
            meeting = self.resolve(person, parts[1])
            version = int(parts[2])
        except (ValueError, TypeError):
            raise ValueError('This button is not valid for your account.')
        if version != meeting['version']:
            raise ValueError('This proposal changed. Use /status and respond to the newest message.')
        if parts[0] == 'choose':
            if len(parts) != 4:
                raise ValueError('Invalid option.')
            self.choose(person, meeting, int(parts[3]))
        elif parts[0] == 'accept':
            self.accept(person, meeting, version)
        elif parts[0] == 'cancel':
            self.cancel(person, meeting)
        else:
            self.decline(person, meeting)

    def begin(self, person, nonce):
        if not person['can_request'] or not person['draft']:
            raise ValueError('No current preview. Send a new request first.')
        data = json.loads(person['draft'])
        if nonce != data.get('preview_nonce') or not nonce:
            raise ValueError('This preview is out of date. Review the latest request preview.')
        # Resolve again: a roster or draft can change after a preview was shown.
        selected = self.resolve_people(data.get('participants', []), person['id'])
        if selected != data.get('employee_ids'):
            raise ValueError('The attendee list changed. Send the request again to review a new preview.')
        start, end = instant(data['range_start']), instant(data['range_end'])
        if start <= self.now():
            raise ValueError('The request starts in the past. Create a new preview.')
        cur = self.store.db.execute('''INSERT INTO meetings(owner,title,duration,start,end,state,deadline,created)
                        VALUES(?,?,?,?,?,'collecting',?,?)''',
                        (person['id'], data['title'][:200], data['duration_minutes'], start.isoformat(), end.isoformat(),
                         min(self.now().timestamp() + 48 * 3600, start.timestamp()), self.now().timestamp()))
        mid = cur.lastrowid
        for uid in selected:
            employee = self.store.employee(uid)
            status = 'available' if uid == person['id'] else (
                'pending' if employee['telegram_id'] is not None and employee['opted_in'] and employee['reachable'] else 'unreachable')
            availability = (data.get('owner_availability') or [{'start': start.isoformat(), 'end': end.isoformat()}]) if uid == person['id'] else []
            self.store.db.execute('INSERT INTO attendees(meeting_id,employee_id,status,availability) VALUES(?,?,?,?)',
                                  (mid, uid, status, json.dumps(availability)))
            if uid != person['id'] and status == 'pending':
                self.store.tell(uid, f"M-{mid}: {person['name']} requests {data['title']} ({data['duration_minutes']} minutes).\n"
                                + show_slot({'start': start.isoformat(), 'end': end.isoformat()}, employee['timezone'])
                                + '\nWhat time works for you within that range? You can say “2:30 to 4 pm” '
                                  'or “any time in that range”. I’ll use your saved time zone (' + employee['timezone'] + '). '
                                  'You can also decline. Availability and acceptance status are shared with the organizer.',
                                meeting_id=mid, purpose='invitation')
        self.store.db.execute('UPDATE employees SET draft=NULL WHERE id=?', (person['id'],))
        self.store.remember_state(person['id'], focus_meeting_id=mid, clarification=None)
        self.store.tell(person['id'], f"M-{mid}: I’ve queued availability requests for the attendees. I’ll suggest times once they reply.\n" +
                        self.report(self.store.meeting(mid), person), meeting_id=mid, purpose='report')

    def busy(self, ids, exclude):
        placeholders = ','.join('?' for _ in ids)
        rows = self.store.all('''SELECT DISTINCT m.proposal FROM meetings m JOIN attendees a ON a.meeting_id=m.id
                         WHERE m.state='confirmed' AND m.id<>? AND a.employee_id IN (''' + placeholders + ')', (exclude, *ids))
        return [(json.loads(x['proposal'])['start'], json.loads(x['proposal'])['end']) for x in rows if x['proposal']]

    def availability(self, person, meeting, intervals):
        if meeting['state'] == 'cancelled':
            raise ValueError('This meeting was cancelled.')
        if self.now().timestamp() >= meeting['deadline']:
            raise ValueError('The response deadline passed. Ask the organizer to create a revised request.')
        intervals = valid_intervals(intervals, meeting['start'], meeting['end'])
        if not intervals:
            raise ValueError('Please give both a start and an end time within the meeting range.')
        if any(instant(i['start']) <= self.now() for i in intervals):
            raise ValueError('Please give availability in the future.')
        if meeting['state'] in ('confirming', 'confirmed', 'options'):
            self.store.db.execute("UPDATE attendees SET accepted_version=NULL,status=CASE WHEN status='confirmed' THEN 'available' ELSE status END WHERE meeting_id=?", (meeting['id'],))
            self.store.db.execute("UPDATE meetings SET state='collecting',proposal=NULL,version=version+1 WHERE id=?", (meeting['id'],))
            self.store.tell(meeting['owner'], f"M-{meeting['id']}: availability changed; the previous proposal's confirmations are cleared.", purpose='report')
        self.store.db.execute("UPDATE attendees SET status='available',availability=?,accepted_version=NULL WHERE meeting_id=? AND employee_id=?",
                              (json.dumps(intervals), meeting['id'], person['id']))
        self.store.tell(person['id'], f"M-{meeting['id']}: availability saved. I will separately ask you to confirm the chosen time.")
        self.match(self.store.meeting(meeting['id']))

    def match(self, meeting):
        members = self.store.members(meeting['id'])
        if not all(x['status'] in ('available', 'confirmed') and x['opted_in'] and x['active'] for x in members):
            return
        windows = work_windows(meeting['start'], meeting['end'], self.settings.timezone,
                               self.settings.work_start, self.settings.work_end, self.settings.workdays)
        slots = common_slots([windows] + [json.loads(x['availability']) for x in members], meeting['duration'],
                             self.busy([x['employee_id'] for x in members], meeting['id']))
        if not slots:
            if meeting['rounds'] < 2:
                self.store.db.execute("UPDATE meetings SET state='collecting',rounds=rounds+1 WHERE id=?", (meeting['id'],))
                for x in members:
                    if x['employee_id'] == meeting['owner']:
                        continue
                    self.store.db.execute("UPDATE attendees SET status='pending' WHERE meeting_id=? AND employee_id=?", (meeting['id'], x['employee_id']))
                    other_slots = common_slots([windows] + [json.loads(y['availability']) for y in members if y['employee_id'] != x['employee_id']],
                                               meeting['duration'], self.busy([y['employee_id'] for y in members], meeting['id']))
                    suggestions = ('\nThese times fit the other attendees:\n' + '\n'.join(show_slot(slot, x['timezone']) for slot in other_slots)) if other_slots else ''
                    self.store.tell(x['employee_id'], f"M-{meeting['id']}: those times have no common overlap. "
                                    + suggestions + ('\nCould any of those work, or can you offer another time within the original range? ' if suggestions else
                                                       '\nCould you offer another time within the original range? ')
                                    + 'If you can’t move your availability, say so and I’ll let the organizer know.',
                                    meeting_id=meeting['id'], purpose='invitation')
                self.store.tell(meeting['owner'], f"M-{meeting['id']}: no overlap yet. I asked members for alternatives within your range.", purpose='report')
            else:
                self.store.db.execute("UPDATE meetings SET state='needs_decision' WHERE id=?", (meeting['id'],))
                self.store.tell(meeting['owner'], f"M-{meeting['id']}: no common time after two alternative rounds. "
                                'Please cancel and create a request with a wider date range or different required attendees.', purpose='report')
            return
        version = meeting['version'] + 1
        self.store.db.execute("UPDATE meetings SET state='options',options=?,version=? WHERE id=?", (json.dumps(slots), version, meeting['id']))
        owner = self.store.employee(meeting['owner'])
        self.store.tell(meeting['owner'], f"M-{meeting['id']}: possible times based on everyone's availability:\n" +
                        '\n'.join(f"{i+1}. {show_slot(slot, owner['timezone'])}" for i, slot in enumerate(slots)) +
                        '\nChoose a time. I will then ask the required attendees to accept that exact proposal.',
                        keyboard=buttons(*[(f'Choose option {i+1}', f"choose:{meeting['id']}:{version}:{i}") for i in range(len(slots))]),
                        meeting_id=meeting['id'], version=version, purpose='options')

    def choose(self, person, meeting, index):
        if person['id'] != meeting['owner'] or meeting['state'] != 'options':
            raise ValueError('Only the organizer can choose a current meeting option.')
        options = json.loads(meeting['options'])
        if index < 0 or index >= len(options):
            raise ValueError('Invalid meeting option.')
        proposal = options[index]
        members = self.store.members(meeting['id'])
        if self._conflicts(meeting, proposal, members):
            self.match(meeting)
            raise ValueError('Another bot meeting took that slot. I recalculated the options.')
        self.store.db.execute("UPDATE meetings SET state='confirming',proposal=? WHERE id=?", (json.dumps(proposal), meeting['id']))
        self.store.db.execute("UPDATE attendees SET status='available',accepted_version=NULL WHERE meeting_id=?", (meeting['id'],))
        self.store.db.execute("UPDATE attendees SET status='confirmed',accepted_version=? WHERE meeting_id=? AND employee_id=?",
                              (meeting['version'], meeting['id'], person['id']))
        for x in members:
            if x['employee_id'] != person['id']:
                self.store.tell(x['employee_id'], f"M-{meeting['id']}: please accept or decline this exact proposal:\n" +
                                show_slot(proposal, x['timezone']),
                                keyboard=buttons(('Confirm', f"accept:{meeting['id']}:{meeting['version']}"),
                                                 ('Decline', f"decline:{meeting['id']}:{meeting['version']}")),
                                meeting_id=meeting['id'], version=meeting['version'], purpose='confirmation')
        self.store.tell(person['id'], f"M-{meeting['id']}: confirmation requests queued. Awaiting required attendees.", purpose='report')

    def _conflicts(self, meeting, slot, members):
        if instant(slot['start']) <= self.now():
            return True
        return any(overlap((instant(slot['start']), instant(slot['end'])), (instant(a), instant(b)))
                   for a, b in self.busy([x['employee_id'] for x in members], meeting['id']))

    def accept(self, person, meeting, version):
        if meeting['state'] != 'confirming' or meeting['version'] != version or not meeting['proposal']:
            raise ValueError('No current confirmation proposal. Use the latest proposal buttons.')
        if self.now().timestamp() >= meeting['deadline']:
            self.store.db.execute("UPDATE meetings SET state='needs_decision',version=version+1 WHERE id=?", (meeting['id'],))
            self.store.tell(meeting['owner'], f"M-{meeting['id']}: confirmation deadline reached. Your decision is needed.", purpose='report')
            raise ValueError('The response deadline has passed. Please ask the organizer to revise the meeting.')
        members = self.store.members(meeting['id'])
        if self._conflicts(meeting, json.loads(meeting['proposal']), members):
            self.store.db.execute("UPDATE meetings SET state='needs_decision',version=version+1 WHERE id=?", (meeting['id'],))
            self.store.tell(meeting['owner'], f"M-{meeting['id']}: the proposed slot has a conflict or has passed. Please revise the request.", purpose='report')
            raise ValueError('That proposal is no longer available.')
        self.store.db.execute("UPDATE attendees SET status='confirmed',accepted_version=? WHERE meeting_id=? AND employee_id=?",
                              (version, meeting['id'], person['id']))
        self.store.tell(person['id'], f"M-{meeting['id']}: your acceptance is saved.")
        members = self.store.members(meeting['id'])
        if all(x['status'] == 'confirmed' and x['accepted_version'] == version and x['opted_in'] and x['active'] for x in members):
            self.store.db.execute("UPDATE meetings SET state='confirmed' WHERE id=?", (meeting['id'],))
            for x in members:
                self.store.tell(x['employee_id'], f"M-{meeting['id']} CONFIRMED: {meeting['title']}\n" +
                                show_slot(json.loads(meeting['proposal']), x['timezone']) +
                                '\nEvery required attendee accepted. Calendar event creation is not connected.',
                                meeting_id=meeting['id'], version=version, purpose='final')
        else:
            owner = self.store.employee(meeting['owner'])
            self.store.tell(meeting['owner'], self.report(self.store.meeting(meeting['id']), owner), purpose='report')

    def decline(self, person, meeting):
        if meeting['state'] == 'cancelled':
            raise ValueError('This meeting was cancelled.')
        self.store.db.execute("UPDATE attendees SET status='declined',accepted_version=NULL WHERE meeting_id=? AND employee_id=?", (meeting['id'], person['id']))
        self.store.db.execute("UPDATE meetings SET state='needs_decision',version=version+1 WHERE id=?", (meeting['id'],))
        self.store.tell(person['id'], f"M-{meeting['id']}: decline recorded. Reminders for this request will stop.")
        self.store.tell(meeting['owner'], f"M-{meeting['id']}: required attendee {person['name']} declined. "
                        'Please decide whether to create a revised request.', purpose='report')

    def cancel(self, person, meeting):
        if meeting['owner'] != person['id']:
            raise ValueError('Only this meeting\'s organizer can cancel it.')
        if meeting['state'] == 'cancelled':
            self.store.tell(person['id'], f"M-{meeting['id']} is already cancelled.")
            return
        self.store.db.execute("UPDATE meetings SET state='cancelled',version=version+1 WHERE id=?", (meeting['id'],))
        for x in self.store.members(meeting['id']):
            self.store.tell(x['employee_id'], f"M-{meeting['id']} cancelled by {person['name']}. Reminders stopped.", purpose='report')

    def report(self, meeting, viewer):
        text = f"M-{meeting['id']}: {meeting['title']}\nStatus: {meeting['state'].replace('_', ' ')}"
        if meeting['proposal']:
            text += '\n' + show_slot(json.loads(meeting['proposal']), viewer['timezone'])
        if viewer['id'] == meeting['owner']:
            text += '\n' + '\n'.join(f"• {x['name']}: {x['status'].replace('_', ' ')}" for x in self.store.members(meeting['id']))
        else:
            attendee = next(x for x in self.store.members(meeting['id']) if x['employee_id'] == viewer['id'])
            text += '\nYour response: ' + attendee['status'].replace('_', ' ')
        return text

    def sweep(self):
        now = self.now()
        with self.store.transaction():
            for m in self.store.all("SELECT * FROM meetings WHERE state IN ('collecting','options','confirming')"):
                if now.timestamp() >= m['deadline']:
                    self.store.db.execute("UPDATE meetings SET state='needs_decision',version=version+1 WHERE id=?", (m['id'],))
                    self.store.tell(m['owner'], f"M-{m['id']}: response deadline reached.\n" +
                                    self.report(self.store.meeting(m['id']), self.store.employee(m['owner'])), purpose='report')
                    continue
                for member in self.store.members(m['id']):
                    if member['employee_id'] == m['owner'] or member['status'] not in ('pending', 'available'):
                        continue
                    if m['state'] == 'options' or (m['state'] == 'collecting' and member['status'] != 'pending'):
                        continue
                    local = now.astimezone(ZoneInfo(member['timezone']))
                    if local.weekday() not in {int(x) for x in self.settings.workdays.split(',')}:
                        continue
                    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
                    reminded = self.store.one("SELECT 1 FROM outbox WHERE employee_id=? AND purpose='reminder' AND created>=?", (member['employee_id'], day_start))
                    last = max(member['last_reminder'], m['created'])
                    if not (9 <= local.hour < 18) or reminded or member['reminders'] >= 2 or now.timestamp() - last < 12*3600:
                        continue
                    if not member['opted_in'] or not member['active']:
                        continue
                    keyboard = buttons(('Confirm', f"accept:{m['id']}:{m['version']}"), ('Decline', f"decline:{m['id']}:{m['version']}")) if m['state'] == 'confirming' else None
                    self.store.tell(member['employee_id'], f"Reminder for M-{m['id']}: " +
                                    ('please accept or decline ' + show_slot(json.loads(m['proposal']), member['timezone'])
                                     if m['state'] == 'confirming' else 'please reply with your available dates and start/end times.'),
                                    keyboard=keyboard, meeting_id=m['id'], version=m['version'], purpose='reminder')
                    self.store.db.execute('UPDATE attendees SET reminders=reminders+1,last_reminder=? WHERE meeting_id=? AND employee_id=?', (now.timestamp(), m['id'], member['employee_id']))
