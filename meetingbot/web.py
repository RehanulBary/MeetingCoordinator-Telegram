"""Loopback-only administration UI. No public listener or third-party web assets."""
import asyncio
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit
import webbrowser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Settings, read_env
from . import COORDINATOR_VERSION, source_revision
from .store import Store


DEFAULTS = {
    'OPENAI_API_KEY': '', 'TELEGRAM_BOT_TOKEN': '', 'OPENAI_MODEL': 'gpt-6-luna',
    'TELEGRAM_BOT_USERNAME': '', 'COMPANY_TIMEZONE': 'Asia/Dhaka',
    'DATABASE_PATH': 'data/meetingbot.sqlite3', 'MAX_AI_CALLS_PER_DAY': '100',
    'WORKDAY_START_HOUR': '9', 'WORKDAY_END_HOUR': '18', 'WORKDAYS': '0,1,2,3,4,5,6',
}
SECRET_KEYS = {'OPENAI_API_KEY', 'TELEGRAM_BOT_TOKEN'}
ASSETS = Path(__file__).with_name('web_assets')


class WebError(ValueError):
    def __init__(self, message, field=None, status=400):
        super().__init__(message)
        self.field, self.status = field, status


def text_field(data, key, limit=120, required=True):
    value = data.get(key, '')
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        raise WebError('Use a single line of text.', key)
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise WebError(f'Enter between {1 if required else 0} and {limit} characters.', key)
    return value


def number(data, key, low, high):
    value = data.get(key)
    if isinstance(value, bool) or not re.fullmatch(r'-?\d+', str(value)):
        raise WebError('Enter a whole number.', key)
    result = int(value)
    if not low <= result <= high:
        raise WebError(f'Enter a number from {low:,} to {high:,}.', key)
    return result


def zone(value, field='timezone'):
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise WebError('Enter an IANA time zone, such as Asia/Dhaka or Europe/London.', field) from None
    return value


async def connection_checks(settings):
    """Metadata-only checks: no generated response, no Telegram messages."""
    from .telegram import Telegram
    from openai import AsyncOpenAI
    rows = []

    async def reachable(host):
        try:
            await asyncio.wait_for(asyncio.to_thread(socket.getaddrinfo, host, 443), timeout=6)
            return True
        except (OSError, asyncio.TimeoutError):
            return False

    tg_dns, ai_dns = await asyncio.gather(reachable('api.telegram.org'), reachable('api.openai.com'))
    if not settings.telegram_token:
        rows.append({'name': 'Telegram', 'ok': False, 'detail': 'Add your bot token in Settings.'})
    elif not tg_dns:
        rows.append({'name': 'Telegram', 'ok': False, 'detail': 'Cannot resolve api.telegram.org. Check this computer’s internet or DNS.'})
    else:
        api = Telegram(settings.telegram_token)
        try:
            me = await asyncio.wait_for(api.call('getMe'), 15)
            info = await asyncio.wait_for(api.call('getWebhookInfo'), 15)
            matched = not settings.bot_username or me['username'].casefold() == settings.bot_username.casefold()
            ok = matched and not info.get('url')
            detail = ('Bot identity does not match the configured username.' if not matched else
                      'An existing webhook prevents long polling. Remove it before starting.' if info.get('url') else
                      '@' + me['username'] + ' verified; ready for long polling.')
            rows.append({'name': 'Telegram', 'ok': ok, 'detail': detail})
        except Exception as exc:
            rows.append({'name': 'Telegram', 'ok': False, 'detail': 'Connection or credential check failed (' + type(exc).__name__ + ').'})
        finally:
            await api.close()
    if not settings.openai_key:
        rows.append({'name': 'OpenAI', 'ok': False, 'detail': 'Add your API key in Settings.'})
    elif not ai_dns:
        rows.append({'name': 'OpenAI', 'ok': False, 'detail': 'Cannot resolve api.openai.com. Check this computer’s internet or DNS.'})
    else:
        client = AsyncOpenAI(api_key=settings.openai_key, timeout=15, max_retries=0)
        try:
            await client.models.retrieve(settings.model)
            rows.append({'name': 'OpenAI', 'ok': True, 'detail': settings.model + ' is accessible. No response was generated.'})
        except Exception as exc:
            rows.append({'name': 'OpenAI', 'ok': False, 'detail': 'Model or credential check failed (' + type(exc).__name__ + ').'})
        finally:
            await client.close()
    return rows


class Dashboard:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.env_path = self.root / '.env'
        self.guard = threading.RLock()
        self.process = None
        self.events = deque(maxlen=30)
        self.checks = {'running': False, 'checked_at': None, 'rows': []}
        self.csrf = secrets.token_urlsafe(32)
        self.session = secrets.token_urlsafe(32)
        self.secret_history = set()
        self.started_at = None
        self.values()  # Capture credentials for log redaction, never for a response.

    def values(self):
        saved = read_env(self.env_path)
        values = {key: saved.get(key, os.environ.get(key, default)) for key, default in DEFAULTS.items()}
        self.secret_history.update(value for key, value in values.items() if key in SECRET_KEYS and value)
        return values

    def settings(self):
        values = self.values()
        settings = Settings.from_values(values)
        path = Path(settings.database)
        settings.database = str(path if path.is_absolute() else self.root / path)
        return settings

    @contextmanager
    def store(self):
        store = Store(self.settings().database)
        try:
            yield store
        finally:
            store.db.close()

    def event(self, message):
        for secret in tuple(self.secret_history):
            message = message.replace(secret, '[hidden]')
        message = re.sub(r'https?://\S+', '[URL hidden]', message)
        with self.guard:
            self.events.append({'time': time.time(), 'message': message[:400]})

    def locked(self):
        path = Path(self.settings().database + '.lock')
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open('a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(stream, fcntl.LOCK_UN)
        return False

    def runtime(self):
        with self.guard:
            owned = self.process is not None and self.process.poll() is None
            external = not owned and self.locked()
            result = {'state': 'starting' if owned else 'external' if external else 'stopped',
                      'managed': owned, 'busy': owned or external, 'last_poll': None,
                      'exit_code': self.process.poll() if self.process else None}
            with self.store() as store:
                row = store.one("SELECT value FROM metadata WHERE key='runtime'")
            try:
                health = json.loads(row['value']) if row else {}
            except (ValueError, TypeError):
                health = {}
            same = owned and health.get('pid') == self.process.pid
            if (same or external) and time.time() - health.get('heartbeat', 0) < 90:
                result['state'] = health.get('state', 'starting')
                result['last_poll'] = health.get('last_poll')
            if not result['busy'] and result['exit_code'] not in (None, 0, -signal.SIGINT):
                result['state'] = 'failed'
            result['coordinator_version'] = health.get('coordinator_version') if result['busy'] else None
            result['available_version'] = COORDINATOR_VERSION
            result['restart_required'] = bool(result['busy'] and health.get('code_revision') != source_revision()
                                               and (external or time.time() - (self.started_at or 0) > 15))
            return result

    def public_settings(self):
        values = self.values()
        return {**{k: v for k, v in values.items() if k not in SECRET_KEYS},
                'openai_key_saved': bool(values['OPENAI_API_KEY']),
                'telegram_token_saved': bool(values['TELEGRAM_BOT_TOKEN'])}

    def snapshot(self):
        with self.guard:
            settings = self.public_settings()
            runtime = self.runtime()
            checks = json.loads(json.dumps(self.checks))
            events = list(self.events)
        with self.store() as store:
            members = store.all('SELECT id,name,team,timezone,can_request,telegram_id,opted_in,active,reachable FROM employees ORDER BY name COLLATE NOCASE')
            pending = store.all('SELECT p.*,e.name,e.team FROM enrollment p JOIN employees e ON e.id=p.employee_id ORDER BY p.created')
            meetings = store.all('''SELECT m.id,m.title,m.state,m.duration,m.created,m.proposal,m.start,m.end,
                e.name AS organizer,(SELECT count(*) FROM attendees a WHERE a.meeting_id=m.id) AS attendees
                FROM meetings m JOIN employees e ON e.id=m.owner ORDER BY m.id DESC LIMIT 100''')
            inbox = store.all('SELECT state,count(*) AS count FROM inbox GROUP BY state')
            outbox = store.all('SELECT status AS state,count(*) AS count FROM outbox GROUP BY status')
            failures = store.all('''SELECT id,employee_id,meeting_id,status,error_type,attempts FROM outbox
                WHERE status IN ('failed','uncertain') ORDER BY id DESC LIMIT 50''')
            input_failures = store.all("SELECT update_id,error_type,attempts FROM inbox WHERE state='failed' ORDER BY update_id DESC LIMIT 50")
            usage = store.all('''SELECT day,model,state,count(*) AS calls,sum(input_tokens) AS input_tokens,
                sum(output_tokens) AS output_tokens FROM ai_usage GROUP BY day,model,state ORDER BY day DESC LIMIT 100''')
            totals = store.one("SELECT count(*) AS meetings,sum(state NOT IN ('confirmed','cancelled')) AS active FROM meetings")
            today = datetime.now(timezone.utc).date().isoformat()
            calls_today = store.one('SELECT count(*) AS n FROM ai_usage WHERE day=?', (today,))['n']
        return {'settings': settings, 'runtime': runtime, 'members': members, 'pending': pending,
                'meetings': meetings, 'inbox': inbox, 'outbox': outbox, 'failures': failures,
                'input_failures': input_failures, 'usage': usage, 'checks': checks, 'events': events,
                'summary': {'members': len(members), 'enrolled': sum(m['telegram_id'] is not None for m in members),
                            'teams': len({m['team'] for m in members}), 'active': totals['active'] or 0,
                            'meetings': totals['meetings'], 'calls_today': calls_today},
                'updated_at': time.time()}

    def save_settings(self, data):
        with self.guard:
            if self.runtime()['busy'] or self.checks['running']:
                raise WebError('Stop the bot and wait for connection checks to finish before changing settings.', status=409)
            current = self.values()
            candidate = dict(current)
            for key in ('OPENAI_MODEL', 'COMPANY_TIMEZONE', 'TELEGRAM_BOT_USERNAME'):
                candidate[key] = text_field(data, key, required=key != 'TELEGRAM_BOT_USERNAME')
            candidate['TELEGRAM_BOT_USERNAME'] = candidate['TELEGRAM_BOT_USERNAME'].lstrip('@')
            if candidate['TELEGRAM_BOT_USERNAME'] and not re.fullmatch(r'[A-Za-z0-9_]{5,32}', candidate['TELEGRAM_BOT_USERNAME']):
                raise WebError('Use the bot username from BotFather without a link.', 'TELEGRAM_BOT_USERNAME')
            if not re.fullmatch(r'[A-Za-z0-9._:-]+', candidate['OPENAI_MODEL']):
                raise WebError('Use a model ID, such as gpt-6-luna.', 'OPENAI_MODEL')
            zone(candidate['COMPANY_TIMEZONE'], 'COMPANY_TIMEZONE')
            for key, low, high in (('MAX_AI_CALLS_PER_DAY', 1, 100000), ('WORKDAY_START_HOUR', 0, 23), ('WORKDAY_END_HOUR', 1, 24)):
                candidate[key] = str(number(data, key, low, high))
            if int(candidate['WORKDAY_END_HOUR']) <= int(candidate['WORKDAY_START_HOUR']):
                raise WebError('Closing time must be later than opening time.', 'WORKDAY_END_HOUR')
            days = data.get('WORKDAYS')
            if not isinstance(days, list) or not days or any(type(d) is not int or d not in range(7) for d in days):
                raise WebError('Select at least one working day.', 'WORKDAYS')
            candidate['WORKDAYS'] = ','.join(str(d) for d in sorted(set(days)))
            for key in SECRET_KEYS:
                value = text_field(data, key, limit=500, required=False)
                if value:
                    if not re.fullmatch(r'[A-Za-z0-9_:\-\.]+', value):
                        raise WebError('This credential contains an unsupported character.', key)
                    candidate[key] = value
            Settings.from_values(candidate)
            # Preserve unrelated entries; write and replace atomically with owner-only permissions.
            merged = {**read_env(self.env_path), **candidate}
            fd, temporary = tempfile.mkstemp(prefix='.env-save-', dir=self.root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    stream.write(''.join(f'{key}={value}\n' for key, value in merged.items()))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.env_path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self.secret_history.update(candidate[k] for k in SECRET_KEYS if candidate[k])
            self.checks = {'running': False, 'checked_at': None, 'rows': []}
            self.event('Settings saved. Run connection checks before starting the bot.')
            return {'message': 'Settings saved. Your next start will use these settings.'}

    def member(self, data, employee_id=None):
        name, team = text_field(data, 'name'), text_field(data, 'team', 80)
        tz = zone(text_field(data, 'timezone', 80))
        if type(data.get('can_request')) is not bool:
            raise WebError('Choose the requester permission.', 'can_request')
        with self.store() as store, store.transaction():
            if employee_id is None:
                employee_id = store.add_employee(name, team, tz, data['can_request'])
            else:
                if not store.employee(employee_id):
                    raise WebError('Member not found.', status=404)
                store.db.execute('UPDATE employees SET name=?,team=?,timezone=?,can_request=? WHERE id=?',
                                 (name, team, tz, int(data['can_request']), employee_id))
        return {'message': 'Member saved.', 'id': employee_id}

    def invitation(self, employee_id):
        username = self.settings().bot_username
        if not username or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            raise WebError('Set the bot username in Settings before creating invitations.')
        with self.store() as store, store.transaction():
            person = store.employee(employee_id)
            if not person:
                raise WebError('Member not found.', status=404)
            if person['telegram_id'] is not None:
                raise WebError('This member is already connected to Telegram.')
            if store.one('SELECT employee_id FROM enrollment WHERE employee_id=?', (employee_id,)):
                raise WebError('This member has a pending enrollment. Review it in Members.')
            code = store.invite(employee_id)
        return {'url': 'https://t.me/' + username + '?start=' + code, 'name': person['name'], 'expires_at': time.time() + 3600}

    def approve(self, employee_id, data):
        if data.get('verified') is not True:
            raise WebError('Verify the employee and Telegram account independently, then tick the confirmation.', 'verified')
        telegram_id = number(data, 'telegram_id', 1, 2**63 - 1)
        with self.store() as store:
            store.approve(employee_id, telegram_id)
        return {'message': 'Enrollment approved. The welcome message will send when the bot is running.'}

    def start(self):
        with self.guard:
            if self.runtime()['busy']:
                raise WebError('The bot is already running. Stop that process before starting another.', status=409)
            settings = self.settings()
            if not settings.telegram_token or not settings.openai_key:
                raise WebError('Add both credentials in Settings before starting the bot.')
            env = dict(os.environ)
            env.update(self.values())
            env['PYTHONUNBUFFERED'] = '1'
            self.process = subprocess.Popen([sys.executable, '-m', 'meetingbot', 'run'], cwd=self.root,
                                            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            self.started_at = time.time()
            self.event('Starting the Telegram coordinator…')
            process = self.process
            threading.Thread(target=self._read_process, args=(process,), daemon=True).start()
        return {'message': 'Bot is starting. Connection status will update here.'}

    def _read_process(self, process):
        try:
            for line in process.stdout:
                self.event(line.strip())
            code = process.wait()
            self.event('Bot process ended (exit ' + str(code) + ').')
        finally:
            process.stdout.close()

    def stop(self):
        with self.guard:
            if self.process is None or self.process.poll() is not None:
                if self.locked():
                    raise WebError('This bot was started outside the dashboard. Stop it in its original terminal.', status=409)
                return {'message': 'Bot is already stopped.'}
            process = self.process
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            self.event('Bot stopped. Meetings and queued work remain saved.')
        return {'message': 'Bot stopped. Saved meeting state is preserved.'}

    def check_connections(self):
        with self.guard:
            if self.checks['running']:
                return {'message': 'Connection checks are already running.'}
            settings = self.settings()
            self.checks = {'running': True, 'checked_at': None, 'rows': []}

        def work():
            try:
                rows = asyncio.run(connection_checks(settings))
            except Exception:
                rows = [{'name': 'Connections', 'ok': False, 'detail': 'Checks could not complete. Review your settings and try again.'}]
            with self.guard:
                self.checks = {'running': False, 'checked_at': time.time(), 'rows': rows}

        threading.Thread(target=work, daemon=True).start()
        return {'message': 'Checking bot identity and model access. No messages or AI responses will be generated.'}

    def meeting(self, meeting_id):
        with self.store() as store:
            meeting = store.meeting(meeting_id)
            if not meeting:
                raise WebError('Meeting not found.', status=404)
            attendees = store.all('''SELECT e.name,e.team,a.status,a.accepted_version FROM attendees a
                JOIN employees e ON e.id=a.employee_id WHERE a.meeting_id=? ORDER BY e.name''', (meeting_id,))
        return {'meeting': meeting, 'attendees': attendees}

    def close(self):
        if self.process and self.process.poll() is None:
            self.stop()


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, app):
        self.app = app
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = 'MeetingBot'
    sys_version = ''

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, *args):
        pass  # Never log request paths, invitation codes, or request bodies.

    def respond(self, status, payload, content_type='application/json; charset=utf-8', cookie=False):
        body = json.dumps(payload).encode() if content_type.startswith('application/json') else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', 'meetingbot_session=' + self.server.app.session + '; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(body)

    def allowed(self, mutate=False):
        port = self.server.server_address[1]
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts:
            raise WebError('Use the dashboard’s local address.', status=403)
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise WebError('Open the dashboard directly in your browser.', status=403)
        origin = self.headers.get('Origin')
        if origin is not None and origin not in {f'http://{h}' for h in hosts}:
            raise WebError('Request origin is not allowed.', status=403)
        if mutate:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get('Cookie', ''))
            except Exception:
                raise WebError('Reload the dashboard and try again.', status=403) from None
            session = cookie.get('meetingbot_session')
            if not session or not secrets.compare_digest(session.value, self.server.app.session) or not secrets.compare_digest(self.headers.get('X-CSRF-Token', ''), self.server.app.csrf):
                raise WebError('Reload the dashboard and try again.', status=403)

    def do_GET(self):
        try:
            self.allowed()
            path = urlsplit(self.path).path
            if path == '/api/state':
                self.respond(200, {**self.server.app.snapshot(), 'csrf': self.server.app.csrf}, cookie=True)
            elif re.fullmatch(r'/api/meetings/\d+', path):
                self.respond(200, self.server.app.meeting(int(path.rsplit('/', 1)[1])))
            elif path in ('/', '/app.js', '/styles.css'):
                name, mime = {'/': ('index.html', 'text/html; charset=utf-8'),
                              '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
                              '/styles.css': ('styles.css', 'text/css; charset=utf-8')}[path]
                self.respond(200, (ASSETS / name).read_bytes(), mime)
            elif path == '/favicon.ico':
                self.respond(204, b'', 'image/x-icon')
            else:
                self.respond(404, {'error': 'Page not found.'})
        except Exception as exc:
            self.error(exc)

    def do_POST(self):
        try:
            self.allowed(mutate=True)
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                raise WebError('Send JSON data.', status=415)
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= 16384:
                raise WebError('Request is empty or too large.', status=413)
            try:
                data = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise WebError('Invalid JSON data.') from None
            if not isinstance(data, dict):
                raise WebError('Send a JSON object.')
            path = urlsplit(self.path).path
            app = self.server.app
            if path == '/api/settings':
                result = app.save_settings(data)
            elif path == '/api/members':
                result = app.member(data)
            elif match := re.fullmatch(r'/api/members/(\d+)/(edit|invite|approve)', path):
                employee_id, action = int(match[1]), match[2]
                result = (app.member(data, employee_id) if action == 'edit' else
                          app.invitation(employee_id) if action == 'invite' else app.approve(employee_id, data))
            elif path == '/api/bot/start':
                result = app.start()
            elif path == '/api/bot/stop':
                result = app.stop()
            elif path == '/api/checks':
                result = app.check_connections()
            else:
                raise WebError('Action not found.', status=404)
            self.respond(200, result)
        except Exception as exc:
            self.error(exc)

    def error(self, exc):
        try:
            if isinstance(exc, WebError):
                self.respond(exc.status, {'error': str(exc), 'field': exc.field})
            elif isinstance(exc, sqlite3.IntegrityError):
                self.respond(409, {'error': 'This account is already linked. Refresh and review the member record.'})
            elif isinstance(exc, ValueError):
                # Known storage errors contain no external API responses or credentials.
                self.respond(400, {'error': 'The action could not be completed. Refresh and check the values.'})
            else:
                self.respond(500, {'error': 'The local action failed. Check that the database and settings file are accessible.'})
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            pass


def serve(port=8765, open_browser=False):
    if not 0 <= port <= 65535:
        raise ValueError('Choose a port between 0 and 65535.')
    app = Dashboard(Path.cwd())
    try:
        server = DashboardServer(('127.0.0.1', port), app)
    except PermissionError:
        raise ValueError('This execution environment blocks local web listeners. Double-click Meeting Bot.command or run python3 -m meetingbot web --open in your normal Terminal.') from None
    except OSError as exc:
        if exc.errno in (48, 98):
            raise ValueError('This dashboard port is already in use. Open the existing dashboard or choose --port 8766.') from None
        raise
    url = 'http://127.0.0.1:' + str(server.server_address[1])
    print('Meeting Bot dashboard: ' + url, flush=True)
    print('Local administrator access. Keep this terminal open; Ctrl+C stops the dashboard and its bot.', flush=True)
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.close()
