"""Dashboard contracts exercised in memory: no sockets, APIs, or real company data."""
import asyncio
import fcntl
from http.client import HTTPResponse
import io
import json
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch, MagicMock, AsyncMock

from meetingbot.config import Settings
from meetingbot.telegram import Runner
from meetingbot.web import Dashboard, Handler, WebError, connection_checks, serve


class MemoryConnection:
    def __init__(self, request=b''):
        self.input, self.output = io.BytesIO(request), io.BytesIO()

    def makefile(self, *args, **kwargs):
        return self.input

    def sendall(self, data):
        self.output.write(data)

    def settimeout(self, value):
        pass


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.env = self.root / '.env'
        # Test credentials only. Never open the workspace's .env in this suite.
        self.env.write_text('OPENAI_API_KEY=test-openai-secret\nTELEGRAM_BOT_TOKEN=123:test-telegram-secret\n'
                            'TELEGRAM_BOT_USERNAME=TestMeeting_bot\nOPENAI_MODEL=gpt-6-luna\n'
                            'DATABASE_PATH=data/company.sqlite3\nCUSTOM_SETTING=preserve-me\n')
        self.app = Dashboard(self.root)
        self.server = SimpleNamespace(app=self.app, server_address=('127.0.0.1', 8765))

    def tearDown(self):
        self.temp.cleanup()

    def request(self, path='/api/state', method='GET', data=None, headers=None, authorized=False):
        body = json.dumps(data or {}).encode() if method == 'POST' else b''
        values = {'Host': '127.0.0.1:8765', 'Connection': 'close'}
        if method == 'POST':
            values.update({'Content-Type': 'application/json', 'Content-Length': str(len(body))})
        if authorized:
            values.update({'Cookie': 'meetingbot_session=' + self.app.session, 'X-CSRF-Token': self.app.csrf,
                           'Origin': 'http://127.0.0.1:8765'})
        values.update(headers or {})
        request = f'{method} {path} HTTP/1.1\r\n' + ''.join(f'{k}: {v}\r\n' for k, v in values.items()) + '\r\n'
        connection = MemoryConnection(request.encode() + body)
        Handler(connection, ('127.0.0.1', 1), self.server)
        response = HTTPResponse(MemoryConnection(connection.output.getvalue()))
        response.begin()
        content = response.read()
        return response, json.loads(content) if response.getheader('Content-Type', '').startswith('application/json') else content

    def config_payload(self, **changes):
        values = {k: v for k, v in self.app.public_settings().items() if k.isupper()}
        values.update(OPENAI_API_KEY='', TELEGRAM_BOT_TOKEN='', WORKDAYS=[0, 1, 2, 3, 4])
        return {**values, **changes}

    def add_member(self):
        return self.app.member({'name': 'Ayesha', 'team': 'Engineering', 'timezone': 'Asia/Dhaka', 'can_request': True})['id']

    def test_state_never_returns_credentials_and_sets_protected_cookie(self):
        with self.env.open('a') as stream:
            stream.write('UNRELATED_PRIVATE_KEY=another-secret\n')
        response, data = self.request()
        self.assertEqual(response.status, 200)
        raw = json.dumps(data)
        self.assertNotIn('test-openai-secret', raw)
        self.assertNotIn('test-telegram-secret', raw)
        self.assertNotIn('another-secret', raw)
        self.assertNotIn('UNRELATED_PRIVATE_KEY', raw)
        self.assertNotIn('OPENAI_API_KEY', data['settings'])
        self.assertTrue(data['settings']['openai_key_saved'])
        self.assertIn('HttpOnly', response.getheader('Set-Cookie'))
        self.assertIn('SameSite=Strict', response.getheader('Set-Cookie'))
        self.assertEqual(response.getheader('Cache-Control'), 'no-store')
        self.assertIn("frame-ancestors 'none'", response.getheader('Content-Security-Policy'))

    def test_cross_site_and_rebound_hosts_are_rejected_before_reading_state(self):
        for headers in ({'Host': 'attacker.example:8765'}, {'Origin': 'https://attacker.example'},
                        {'Sec-Fetch-Site': 'cross-site'}, {'Host': '127.0.0.1:8888'}):
            response, _ = self.request(headers=headers)
            self.assertEqual(response.status, 403)

    def test_mutations_require_cookie_and_csrf_and_json(self):
        payload = {'name': 'Ayesha', 'team': 'Engineering', 'timezone': 'Asia/Dhaka', 'can_request': True}
        response, _ = self.request('/api/members', 'POST', payload)
        self.assertEqual(response.status, 403)
        response, _ = self.request('/api/members', 'POST', payload, {'X-CSRF-Token': 'wrong'}, True)
        self.assertEqual(response.status, 403)
        response, _ = self.request('/api/members', 'POST', payload, {'Content-Type': 'text/plain'}, True)
        self.assertEqual(response.status, 415)
        response, _ = self.request('/api/members', 'POST', payload, authorized=True)
        self.assertEqual(response.status, 200)
        self.assertEqual(self.app.snapshot()['summary']['members'], 1)

    def test_settings_save_preserves_secrets_and_unknown_values_with_private_mode(self):
        response, _ = self.request('/api/settings', 'POST', self.config_payload(MAX_AI_CALLS_PER_DAY='70'), authorized=True)
        self.assertEqual(response.status, 200)
        values = self.app.values()
        self.assertEqual(values['OPENAI_API_KEY'], 'test-openai-secret')
        self.assertIn('CUSTOM_SETTING=preserve-me', self.env.read_text())
        self.assertEqual(values['MAX_AI_CALLS_PER_DAY'], '70')
        self.assertEqual(self.env.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob('.env-save-*')))

    def test_settings_replacement_is_loaded_without_restart_and_redacted(self):
        self.app.save_settings(self.config_payload(OPENAI_API_KEY='replacement-secret'))
        self.assertEqual(self.app.settings().openai_key, 'replacement-secret')
        self.app.event('test-openai-secret replacement-secret https://api.telegram.org/bot123:abc/sendMessage')
        self.assertEqual(self.app.events[-1]['message'], '[hidden] [hidden] [URL hidden]')

    def test_invalid_settings_are_atomic_and_errors_are_field_specific(self):
        before = self.env.read_bytes()
        for changes, field in (({'WORKDAY_END_HOUR': '3'}, 'WORKDAY_END_HOUR'),
                               ({'WORKDAYS': []}, 'WORKDAYS'),
                               ({'COMPANY_TIMEZONE': 'Invalid/Zone'}, 'COMPANY_TIMEZONE'),
                               ({'MAX_AI_CALLS_PER_DAY': '0'}, 'MAX_AI_CALLS_PER_DAY'),
                               ({'OPENAI_API_KEY': 'bad\nINJECT=value'}, 'OPENAI_API_KEY'),
                               ({'TELEGRAM_BOT_USERNAME': 'https://evil.example'}, 'TELEGRAM_BOT_USERNAME')):
            response, body = self.request('/api/settings', 'POST', self.config_payload(**changes), authorized=True)
            self.assertEqual(response.status, 400)
            self.assertEqual(body['field'], field)
            self.assertEqual(self.env.read_bytes(), before)

    def test_external_runtime_cannot_be_stopped_or_reconfigured(self):
        with self.app.store() as store:
            path = Path(store.path + '.lock')
        with path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.app.runtime()['state'], 'external')
            for action in (self.app.start, self.app.stop, lambda: self.app.save_settings(self.config_payload())):
                with self.assertRaises(WebError):
                    action()

    def test_member_edit_and_enrollment_approval_use_shared_store(self):
        uid = self.add_member()
        result = self.app.invitation(uid)
        self.assertTrue(result['url'].startswith('https://t.me/TestMeeting_bot?start='))
        with self.app.store() as store:
            self.assertTrue(store.claim(result['url'].split('=')[1], 88001, 88001, 'Ayesha account'))
        with self.assertRaises(WebError):
            self.app.approve(uid, {'telegram_id': 88001, 'verified': False})
        self.app.approve(uid, {'telegram_id': 88001, 'verified': True})
        self.app.member({'name': 'Ayesha Rahman', 'team': 'Design', 'timezone': 'Europe/London', 'can_request': False}, uid)
        snapshot = self.app.snapshot()
        self.assertEqual(snapshot['summary']['enrolled'], 1)
        self.assertEqual(snapshot['pending'], [])
        self.assertEqual(snapshot['members'][0]['team'], 'Design')
        self.assertEqual(snapshot['outbox'][0]['count'], 1)
        with self.assertRaises(WebError):
            self.app.invitation(uid)

    def test_approval_id_mismatch_does_not_grant_access(self):
        uid = self.add_member()
        code = self.app.invitation(uid)['url'].split('=')[1]
        with self.app.store() as store:
            store.claim(code, 88001, 88001, 'Ayesha')
        response, _ = self.request(f'/api/members/{uid}/approve', 'POST', {'telegram_id': '88002', 'verified': True}, authorized=True)
        self.assertEqual(response.status, 400)
        self.assertEqual(self.app.snapshot()['summary']['enrolled'], 0)

    def test_admin_reads_preserve_sending_jobs_and_private_history_is_omitted(self):
        uid = self.add_member()
        with self.app.store() as store:
            store.db.execute("UPDATE employees SET history='private messages',draft='private draft' WHERE id=?", (uid,))
            store.queue(88001, 'private outgoing message', uid)
            store.db.execute("UPDATE outbox SET status='sending'")
        snapshot = self.app.snapshot()
        self.assertNotIn('private', json.dumps(snapshot))
        self.assertEqual(snapshot['outbox'], [{'state': 'sending', 'count': 1}])

    def test_managed_process_start_uses_saved_config_and_stop_signals_only_owned_child(self):
        process = MagicMock()
        process.pid, process.poll.return_value = 99111, None
        self.app.save_settings(self.config_payload(OPENAI_MODEL='gpt-6-luna', MAX_AI_CALLS_PER_DAY='25'))
        with patch('meetingbot.web.subprocess.Popen', return_value=process) as popen, patch('meetingbot.web.threading.Thread'):
            self.app.start()
        self.assertEqual(popen.call_args.kwargs['env']['OPENAI_MODEL'], 'gpt-6-luna')
        self.assertEqual(popen.call_args.kwargs['env']['MAX_AI_CALLS_PER_DAY'], '25')
        self.assertIsInstance(popen.call_args.args[0], list)
        self.assertTrue(self.app.runtime()['managed'])
        with self.assertRaises(WebError):
            self.app.save_settings(self.config_payload())
        self.app.stop()
        process.send_signal.assert_called_once_with(signal.SIGINT)

    def test_runtime_only_reports_online_from_current_process_heartbeat(self):
        process = MagicMock()
        process.pid, process.poll.return_value = 99111, None
        self.app.process = process
        with self.app.store() as store:
            for pid, heartbeat, expected in ((99110, time.time(), 'starting'), (99111, time.time()-100, 'starting'), (99111, time.time(), 'online')):
                store.db.execute("INSERT OR REPLACE INTO metadata VALUES('runtime',?)", (json.dumps({'pid': pid, 'heartbeat': heartbeat, 'state': 'online'}),))
                self.assertEqual(self.app.runtime()['state'], expected)

    def test_runtime_reports_when_loaded_code_needs_restart(self):
        from meetingbot import source_revision, COORDINATOR_VERSION
        process = MagicMock()
        process.pid, process.poll.return_value = 99111, None
        self.app.process, self.app.started_at = process, time.time() - 60
        with self.app.store() as store:
            for revision, expected in (('older-revision', True), (source_revision(), False)):
                store.db.execute("INSERT OR REPLACE INTO metadata VALUES('runtime',?)", (json.dumps({
                    'pid': process.pid, 'heartbeat': time.time(), 'state': 'online',
                    'code_revision': revision, 'coordinator_version': COORDINATOR_VERSION}),))
                runtime = self.app.runtime()
                self.assertEqual(runtime['restart_required'], expected)
                self.assertEqual(runtime['available_version'], '0.3.1')

    def test_health_resets_previous_process_last_poll(self):
        with self.app.store() as store:
            store.db.execute("INSERT INTO metadata VALUES('runtime',?)", (json.dumps({'pid': -1, 'last_poll': 123}),))
            runner = Runner(None, store, None, Settings())
            runner.health('starting')
            data = json.loads(store.one("SELECT value FROM metadata WHERE key='runtime'")['value'])
            self.assertNotIn('last_poll', data)
            runner.health('online', polled=True)
            self.assertGreater(json.loads(store.one("SELECT value FROM metadata WHERE key='runtime'")['value'])['last_poll'], 123)
            previous = json.loads(store.one("SELECT value FROM metadata WHERE key='runtime'")['value'])['last_poll']
            runner.health('online')
            self.assertEqual(json.loads(store.one("SELECT value FROM metadata WHERE key='runtime'")['value'])['last_poll'], previous)

    def test_static_routes_do_not_expose_files(self):
        for path in ('/.env', '/meetingbot/web.py', '/../.env', '/data/company.sqlite3'):
            response, _ = self.request(path)
            self.assertEqual(response.status, 404)
        for path in ('/', '/app.js', '/styles.css'):
            response, body = self.request(path)
            self.assertEqual(response.status, 200)
            self.assertTrue(body)

    def test_check_job_is_single_flight_and_settings_cannot_change_mid_check(self):
        entered, release = threading.Event(), threading.Event()

        async def fake_checks(settings):
            entered.set()
            await asyncio.to_thread(release.wait, 2)
            return [{'name': 'Test', 'ok': True, 'detail': 'Passed'}]

        with patch('meetingbot.web.connection_checks', fake_checks):
            self.app.check_connections()
            self.assertTrue(entered.wait(2))
            self.app.check_connections()
            with self.assertRaises(WebError):
                self.app.save_settings(self.config_payload())
            release.set()
            deadline = time.monotonic() + 3
            while self.app.checks['running'] and time.monotonic() < deadline:
                time.sleep(.01)
        self.assertFalse(self.app.checks['running'])
        self.assertTrue(self.app.checks['rows'][0]['ok'])

    def test_dns_failure_check_result_is_explicit_and_does_not_expose_secrets(self):
        with patch('meetingbot.web.socket.getaddrinfo', side_effect=OSError('potential sensitive body')):
            result = asyncio.run(connection_checks(self.app.settings()))
        self.assertEqual(len(result), 2)
        self.assertTrue(all(not row['ok'] for row in result))
        self.assertNotIn('sensitive', json.dumps(result))
        self.assertIn('Cannot resolve', result[0]['detail'])

    def test_sandbox_listener_failure_has_actionable_launch_message(self):
        with patch('meetingbot.web.DashboardServer', side_effect=PermissionError):
            with self.assertRaisesRegex(ValueError, 'normal Terminal'):
                serve(8765)


if __name__ == '__main__':
    unittest.main()
