import argparse
import asyncio
from contextlib import contextmanager
import getpass
import logging
import os
from pathlib import Path
import sqlite3
import sys
from zoneinfo import ZoneInfo
from .config import Settings
from .store import Store


def employee_id(value):
    return int(value.upper().removeprefix('E-'))


@contextmanager
def instance_lock(database):
    import fcntl
    path = Path(database + '.lock')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open('a') as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another bot process already uses this database.') from None
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


async def doctor(settings, smoke=False):
    import socket
    from .telegram import Telegram
    from openai import AsyncOpenAI
    ok = True
    for host in ('api.telegram.org', 'api.openai.com'):
        try:
            await asyncio.to_thread(socket.getaddrinfo, host, 443)
            print(host + ': DNS reachable')
        except OSError:
            print(host + ': DNS unavailable from this process')
            ok = False
    if not ok:
        print('Run this command from a normal network-enabled local terminal to verify credentials.')
        return False
    if settings.telegram_token:
        api = Telegram(settings.telegram_token)
        try:
            me = await api.call('getMe')
            print('Telegram token valid for @' + me['username'])
            if settings.bot_username and me['username'].casefold() != settings.bot_username.casefold().lstrip('@'):
                print('Configured bot username does not match the token.')
                ok = False
            info = await api.call('getWebhookInfo')
            print('Webhook: ' + ('present — remove explicitly before polling' if info.get('url') else 'none; ready for long polling'))
        except Exception as exc:
            print('Telegram check failed (' + type(exc).__name__ + '); credentials withheld.')
            ok = False
        finally:
            await api.close()
    else:
        print('Telegram token missing. Run: python3 -m meetingbot configure')
        ok = False
    if settings.openai_key:
        client = AsyncOpenAI(api_key=settings.openai_key, timeout=15, max_retries=0)
        try:
            model = await client.models.retrieve(settings.model)
            print('OpenAI model accessible: ' + model.id)
            if smoke:
                from .ai import OpenAIInterpreter
                store = Store(':memory:')
                interpreter = OpenAIInterpreter(settings, store)
                result = await interpreter.interpret('Help me use the meeting coordinator.', {
                    'source_timestamp': __import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
                    'employee_timezone': settings.timezone, 'employee_name': 'Test', 'can_request': False,
                    'existing_draft': None, 'meetings': [], 'authorized_company_directory': [],
                }, [])
                print('Live structured-output test passed; interpreted intent: ' + result.intent)
                await interpreter.client.close()
                store.db.close()
        except Exception as exc:
            print('OpenAI check failed (' + type(exc).__name__ + '); response bodies and credentials withheld.')
            ok = False
        finally:
            await client.close()
    else:
        print('OpenAI API key missing. Run: python3 -m meetingbot configure')
        ok = False
    return ok


def configure():
    path = Path('.env')
    existing = path.read_text().splitlines() if path.exists() else []
    values = {}
    for line in existing:
        if '=' in line and not line.startswith('#'):
            key, value = line.split('=', 1)
            values[key] = value
    for key, prompt in (('OPENAI_API_KEY', 'OpenAI API key (hidden; Enter keeps current): '),
                        ('TELEGRAM_BOT_TOKEN', 'Telegram bot token (hidden; Enter keeps current): ')):
        value = getpass.getpass(prompt).strip()
        if value:
            values[key] = value
    values.setdefault('OPENAI_MODEL', 'gpt-6-luna')
    values.setdefault('COMPANY_TIMEZONE', 'Asia/Dhaka')
    values.setdefault('DATABASE_PATH', 'data/meetingbot.sqlite3')
    values.setdefault('MAX_AI_CALLS_PER_DAY', '100')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    with os.fdopen(fd, 'w') as stream:
        stream.write('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
    print('Local .env updated with owner-only permissions; secret values withheld.')


def parser():
    result = argparse.ArgumentParser(description='Local Telegram meeting coordinator with GPT-6 Luna.')
    subs = result.add_subparsers(dest='command', required=True)
    subs.add_parser('configure', help='Enter secrets locally using hidden prompts')
    subs.add_parser('run', help='Run the bot using long polling')
    web = subs.add_parser('web', help='Open the local administration dashboard')
    web.add_argument('--port', type=int, default=8765)
    web.add_argument('--open', action='store_true', help='Open the dashboard in your browser')
    d = subs.add_parser('doctor', help='Check network, credentials, and model access without printing secrets')
    d.add_argument('--ai-smoke', action='store_true', help='Also make one small billable structured-output call')
    subs.add_parser('demo', help='Run the offline simulated multi-person demonstration')
    check = subs.add_parser('check-conversation', help='Evaluate the live AI against fictional conversation scenarios; billable model calls')
    check.add_argument('--live', action='store_true', required=True, help='Explicitly run up to twelve billable calls; never sends Telegram messages')
    add = subs.add_parser('add-member', help='Create a company employee record')
    add.add_argument('--name', required=True)
    add.add_argument('--team', required=True)
    add.add_argument('--timezone', default='Asia/Dhaka')
    add.add_argument('--requester', action='store_true')
    subs.add_parser('members', help='List local company roster and enrollment state')
    invite = subs.add_parser('invite', help='Issue an employee-specific, one-hour enrollment code')
    invite.add_argument('employee', type=employee_id)
    subs.add_parser('pending', help='List Telegram enrollment requests awaiting identity verification')
    approve = subs.add_parser('approve', help='Approve after independently verifying the person')
    approve.add_argument('employee', type=employee_id)
    approve.add_argument('telegram_id', type=int)
    approve.add_argument('--verified', action='store_true', required=True, help='Attest that you independently verified this employee/account')
    subs.add_parser('meetings', help='Inspect local meeting states as operator')
    subs.add_parser('queues', help='Inspect processing and delivery states without message content')
    subs.add_parser('usage', help='Inspect actual recorded model token usage')
    return result


async def run_bot(settings):
    from .ai import OpenAIInterpreter
    from .engine import Engine
    from .telegram import Telegram, Runner
    if not settings.telegram_token or not settings.openai_key:
        raise ValueError('Run configure first; both TELEGRAM_BOT_TOKEN and OPENAI_API_KEY are needed.')
    with instance_lock(settings.database):
        store = Store(settings.database)
        store.recover()
        interpreter = OpenAIInterpreter(settings, store)
        api = Telegram(settings.telegram_token)
        try:
            await Runner(api, store, Engine(store, interpreter, settings), settings).run()
        finally:
            await interpreter.client.close()
            await api.close()
            store.db.close()


def main():
    # httpx/openai diagnostic URLs can include tokens; only our sanitized log is enabled.
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s: %(message)s')
    logging.getLogger('httpx').setLevel(logging.CRITICAL)
    logging.getLogger('httpcore').setLevel(logging.CRITICAL)
    logging.getLogger('openai').setLevel(logging.CRITICAL)
    logging.getLogger('meetingbot').setLevel(logging.INFO)
    args = parser().parse_args()
    if args.command == 'web':
        from .web import serve
        serve(args.port, args.open)
        return
    settings = Settings.from_env()
    if args.command == 'configure':
        configure()
        return
    if args.command == 'doctor':
        sys.exit(0 if asyncio.run(doctor(settings, args.ai_smoke)) else 1)
    if args.command == 'demo':
        from .demo import run_demo
        asyncio.run(run_demo())
        return
    if args.command == 'check-conversation':
        from .conversation_eval import run_language_checks
        sys.exit(0 if asyncio.run(run_language_checks(settings)) else 1)
    if args.command == 'run':
        asyncio.run(run_bot(settings))
        return
    store = Store(settings.database)
    try:
        if args.command == 'add-member':
            ZoneInfo(args.timezone)
            uid = store.add_employee(args.name, args.team, args.timezone, args.requester)
            print(f'Created E-{uid}: {args.name}, {args.team}. Requester: {args.requester}')
        elif args.command == 'members':
            for row in store.all('SELECT id,name,team,can_request,telegram_id,opted_in FROM employees'):
                print(f"E-{row['id']} | {row['name']} | {row['team']} | requester={bool(row['can_request'])} | "
                      f"enrolled={row['telegram_id'] is not None} | notifications={bool(row['opted_in'])}")
        elif args.command == 'invite':
            code = store.invite(args.employee)
            print('One-hour, single-use enrollment code. Local admin approval is still required.')
            print(f'https://t.me/{settings.bot_username or "Meeteing_bot"}?start={code}')
        elif args.command == 'pending':
            rows = store.all('SELECT e.name,p.* FROM enrollment p JOIN employees e ON e.id=p.employee_id')
            for row in rows:
                print(f"E-{row['employee_id']} {row['name']} | Telegram ID {row['telegram_id']} | "
                      f"claimed display name: {row['display_name']}")
            if not rows:
                print('No pending enrollments.')
        elif args.command == 'approve':
            store.approve(args.employee, args.telegram_id)
            print('Enrollment approved. Welcome message queued for the running bot.')
        elif args.command == 'meetings':
            for row in store.all('SELECT id,title,state,version FROM meetings ORDER BY id DESC'):
                print(f"M-{row['id']} | {row['title']} | {row['state']} | proposal version {row['version']}")
        elif args.command == 'queues':
            for table, column in (('inbox', 'state'), ('outbox', 'status')):
                for row in store.all(f'SELECT {column} AS state,count(*) AS n FROM {table} GROUP BY {column}'):
                    print(f"{table}: {row['state']} = {row['n']}")
            for row in store.all("SELECT id,employee_id,meeting_id,error_type FROM outbox WHERE status IN ('failed','uncertain')"):
                print(f"Outbox #{row['id']}: E-{row['employee_id']}, M-{row['meeting_id']}, {row['error_type']}; operator review needed.")
        elif args.command == 'usage':
            for row in store.all('SELECT day,model,state,count(*) AS calls,sum(input_tokens) AS inputs,sum(output_tokens) AS outputs FROM ai_usage GROUP BY day,model,state'):
                print(f"{row['day']} | {row['model']} | {row['state']} | calls={row['calls']} | input={row['inputs']} | output={row['outputs']}")
    finally:
        store.db.close()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nBot stopped; saved state remains in the local database.')
    except Exception as exc:
        # Do not print exception bodies that may contain credential-bearing URLs.
        if isinstance(exc, (ValueError, sqlite3.IntegrityError)):
            print('Error: ' + str(exc)[:300], file=sys.stderr)
        else:
            print('Operation failed (' + type(exc).__name__ + '). Run doctor; secrets are withheld.', file=sys.stderr)
        sys.exit(1)
