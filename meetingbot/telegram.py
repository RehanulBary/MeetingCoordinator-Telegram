import asyncio
import json
import logging
import os
import time
import httpx
from . import COORDINATOR_VERSION, LOADED_REVISION

log = logging.getLogger('meetingbot')


class TelegramError(Exception):
    def __init__(self, code, retry_after=None):
        self.code, self.retry_after = code, retry_after
        super().__init__(f'Telegram API error {code}')


class Telegram:
    def __init__(self, token):
        self._base = 'https://api.telegram.org/bot' + token + '/'
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(65, connect=10))

    async def call(self, method, **payload):
        response = await self.client.post(self._base + method, json=payload)
        try:
            data = response.json()
        except ValueError:
            raise TelegramError(response.status_code) from None
        if not response.is_success or not data.get('ok'):
            raise TelegramError(data.get('error_code', response.status_code), data.get('parameters', {}).get('retry_after'))
        return data['result']

    async def close(self):
        await self.client.aclose()


def eligible(store, row):
    if row['employee_id'] is not None:
        person = store.employee(row['employee_id'])
        if not person or not person['active']:
            return False
        if row['purpose'] != 'control' and (not person['opted_in'] or not person['reachable']):
            return False
    if row['meeting_id'] is not None:
        m = store.meeting(row['meeting_id'])
        if not m or m['state'] == 'cancelled':
            return False
        if row['version'] is not None and row['version'] != m['version']:
            return False
        allowed = {'invitation': ('collecting',), 'options': ('options',),
                   'confirmation': ('confirming',), 'reminder': ('collecting', 'confirming'),
                   'final': ('confirmed',)}
        if row['purpose'] in allowed and m['state'] not in allowed[row['purpose']]:
            return False
    return True


class Runner:
    def __init__(self, api, store, engine, settings):
        self.api, self.store, self.engine, self.settings = api, store, engine, settings
        self.chat_last_sent = {}
        self.last_sent = 0.0

    def health(self, state, polled=False):
        previous = self.store.one("SELECT value FROM metadata WHERE key='runtime'")
        data = json.loads(previous['value']) if previous else {}
        if data.get('pid') != os.getpid() or state == 'starting':
            data = {}
        data.update(pid=os.getpid(), state=state, heartbeat=time.time(),
                    coordinator_version=COORDINATOR_VERSION, code_revision=LOADED_REVISION)
        if polled:
            data['last_poll'] = time.time()
        self.store.db.execute("INSERT OR REPLACE INTO metadata VALUES('runtime',?)", (json.dumps(data),))

    async def initialize(self):
        me = await self.api.call('getMe')
        if self.settings.bot_username and me['username'].casefold() != self.settings.bot_username.casefold().lstrip('@'):
            raise ValueError('The token identifies a different bot than TELEGRAM_BOT_USERNAME.')
        webhook = await self.api.call('getWebhookInfo')
        if webhook.get('url'):
            raise ValueError('A webhook is already configured. Remove it explicitly before running this long-polling process.')
        commands = [{'command': command, 'description': description} for command, description in (
            ('start', 'Enrollment and account status'), ('help', 'How to coordinate a meeting'),
            ('schedule', 'Request a meeting'), ('teams', 'Available company teams'),
            ('status', 'Meeting progress'), ('settings', 'Your time zone and preferences'),
            ('cancel', 'Cancel your meeting'), ('new', 'Clear an unfinished draft'),
            ('stop', 'Stop scheduling notifications'), ('resume', 'Enable notifications'),
            ('version', 'Check the running coordinator version'),
        )]
        await self.api.call('setMyCommands', commands=commands)
        return me

    async def poll(self):
        while True:
            try:
                updates = await self.api.call('getUpdates', offset=self.store.offset(), timeout=40,
                                              limit=50, allowed_updates=['message', 'callback_query'])
                self.store.ingest(updates)
                self.health('online', polled=True)
            except TelegramError as exc:
                self.health('reconnecting')
                if exc.code in (401, 409):
                    raise ValueError('Telegram authentication failed or another polling process is running.') from None
                log.warning('Telegram polling error %s; retrying.', exc.code)
                await asyncio.sleep(max(5, exc.retry_after or 0))
            except httpx.HTTPError:
                self.health('reconnecting')
                log.warning('Telegram network unavailable; retrying without logging token-bearing URLs.')
                await asyncio.sleep(5)

    async def process(self):
        while True:
            row = self.store.one("SELECT * FROM inbox WHERE state='pending' ORDER BY update_id LIMIT 1")
            if not row:
                await asyncio.sleep(0.2)
                continue
            self.store.db.execute("UPDATE inbox SET state='processing',attempts=attempts+1 WHERE update_id=?", (row['update_id'],))
            update = json.loads(row['payload'])
            if 'callback_query' in update:
                try:
                    await self.api.call('answerCallbackQuery', callback_query_id=update['callback_query']['id'])
                except (TelegramError, httpx.HTTPError):
                    pass
            try:
                await self.engine.handle_update(update)
            except Exception as exc:
                # Keep the failing update for local operator review; never print payloads/secrets.
                self.store.db.execute("UPDATE inbox SET state='failed',error_type=? WHERE update_id=?", (type(exc).__name__, row['update_id']))
                log.error('Update processing failed (%s). Inspect local queue status.', type(exc).__name__)

    def delivery_failed(self, employee_id):
        if employee_id is None:
            return
        with self.store.transaction():
            self.store.db.execute('UPDATE employees SET reachable=0 WHERE id=?', (employee_id,))
            person = self.store.employee(employee_id)
            for m in self.store.meetings_for(employee_id, active_only=True):
                self.store.db.execute("UPDATE attendees SET status='unreachable' WHERE meeting_id=? AND employee_id=?", (m['id'], employee_id))
                self.store.db.execute("UPDATE meetings SET state='needs_decision',version=version+1 WHERE id=?", (m['id'],))
                if m['owner'] != employee_id:
                    self.store.tell(m['owner'], f"M-{m['id']}: {person['name']} is unreachable on Telegram. Your decision is needed.", purpose='report')

    async def send(self):
        while True:
            row = self.store.one("SELECT * FROM outbox WHERE status='pending' AND due<=? ORDER BY id LIMIT 1", (time.time(),))
            if not row:
                await asyncio.sleep(0.2)
                continue
            if not eligible(self.store, row):
                self.store.db.execute("UPDATE outbox SET status='skipped' WHERE id=?", (row['id'],))
                continue
            delay = max(self.last_sent + 0.06, self.chat_last_sent.get(row['chat_id'], 0) + 1.1) - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
            # Cancellation or opt-out can arrive during rate-limit waits.
            if not eligible(self.store, row):
                self.store.db.execute("UPDATE outbox SET status='skipped' WHERE id=?", (row['id'],))
                continue
            self.store.db.execute("UPDATE outbox SET status='sending',attempts=attempts+1 WHERE id=?", (row['id'],))
            payload = {'chat_id': row['chat_id'], 'text': row['text']}
            if row['keyboard']:
                payload['reply_markup'] = {'inline_keyboard': json.loads(row['keyboard'])}
            try:
                result = await self.api.call('sendMessage', **payload)
            except TelegramError as exc:
                if exc.code == 429 and row['attempts'] < 4:
                    self.store.db.execute("UPDATE outbox SET status='pending',due=?,error_type='rate_limit' WHERE id=?", (time.time()+max(1, exc.retry_after or 5), row['id']))
                elif exc.code >= 500:
                    self.store.db.execute("UPDATE outbox SET status='uncertain',error_type='server_error' WHERE id=?", (row['id'],))
                else:
                    self.store.db.execute("UPDATE outbox SET status='failed',error_type=? WHERE id=?", (str(exc.code), row['id']))
                    if exc.code == 403:
                        self.delivery_failed(row['employee_id'])
                    if exc.code == 401:
                        raise ValueError('Telegram rejected the bot token. Update the local credential.') from None
            except (httpx.ReadTimeout, httpx.WriteError, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError):
                # A send can succeed before its response is lost: avoid blind duplicates.
                self.store.db.execute("UPDATE outbox SET status='uncertain',error_type='network_after_send' WHERE id=?", (row['id'],))
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
                state = 'pending' if row['attempts'] < 4 else 'failed'
                self.store.db.execute('UPDATE outbox SET status=?,due=?,error_type=? WHERE id=?',
                                      (state, time.time()+10, 'connection', row['id']))
            else:
                self.store.db.execute("UPDATE outbox SET status='sent',message_id=? WHERE id=?", (result['message_id'], row['id']))
            self.last_sent = time.monotonic()
            self.chat_last_sent[row['chat_id']] = self.last_sent

    async def tick(self):
        while True:
            current = self.store.one("SELECT value FROM metadata WHERE key='runtime'")
            self.health(json.loads(current['value']).get('state', 'starting') if current else 'starting')
            self.engine.sweep()
            await asyncio.sleep(30)

    async def run(self):
        self.health('starting')
        me = await self.initialize()
        self.health('starting')
        log.info('Running @%s with %s using local long polling. Coordinator %s (%s).',
                 me['username'], self.settings.model, COORDINATOR_VERSION, LOADED_REVISION)
        tasks = [asyncio.create_task(coro()) for coro in (self.poll, self.process, self.send, self.tick)]
        try:
            await asyncio.gather(*tasks)
        finally:
            self.health('stopped')
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.api.close()
