from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

HISTORY_LIMIT = 40


class Store:
    def __init__(self, path: str, clock=None):
        self.path = path
        self.clock = clock or time.time
        if path != ':memory:':
            parent = Path(path).parent
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=5000')
        if path != ':memory:':
            os.chmod(path, 0o600)
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS employees(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, team TEXT NOT NULL,
          can_request INTEGER NOT NULL DEFAULT 0, timezone TEXT NOT NULL,
          telegram_id INTEGER UNIQUE, chat_id INTEGER, opted_in INTEGER NOT NULL DEFAULT 1,
          draft TEXT, history TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
          reachable INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS invites(
          hash TEXT PRIMARY KEY, employee_id INTEGER REFERENCES employees(id), expires REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS enrollment(
          employee_id INTEGER PRIMARY KEY REFERENCES employees(id), telegram_id INTEGER UNIQUE,
          chat_id INTEGER, display_name TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS meetings(
          id INTEGER PRIMARY KEY, owner INTEGER REFERENCES employees(id), title TEXT,
          duration INTEGER, start TEXT, end TEXT, state TEXT, version INTEGER DEFAULT 0,
          options TEXT DEFAULT '[]', proposal TEXT, deadline REAL, created REAL,
          rounds INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS attendees(
          meeting_id INTEGER REFERENCES meetings(id), employee_id INTEGER REFERENCES employees(id),
          status TEXT NOT NULL, availability TEXT NOT NULL DEFAULT '[]', accepted_version INTEGER,
          reminders INTEGER DEFAULT 0, last_reminder REAL DEFAULT 0,
          PRIMARY KEY(meeting_id,employee_id));
        CREATE TABLE IF NOT EXISTS inbox(
          update_id INTEGER PRIMARY KEY, payload TEXT NOT NULL, state TEXT DEFAULT 'pending',
          attempts INTEGER DEFAULT 0, error_type TEXT);
        CREATE TABLE IF NOT EXISTS outbox(
          id INTEGER PRIMARY KEY, chat_id INTEGER, employee_id INTEGER, text TEXT, keyboard TEXT,
          meeting_id INTEGER, version INTEGER, purpose TEXT NOT NULL DEFAULT 'reply',
          status TEXT DEFAULT 'pending', attempts INTEGER DEFAULT 0, due REAL DEFAULT 0,
          message_id INTEGER, error_type TEXT, created REAL);
        CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE IF NOT EXISTS ai_usage(
          id INTEGER PRIMARY KEY, day TEXT, model TEXT, input_tokens INTEGER DEFAULT 0,
          output_tokens INTEGER DEFAULT 0, state TEXT DEFAULT 'reserved');
        ''')
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(employees)')}
        if 'reachable' not in columns:
            self.db.execute('ALTER TABLE employees ADD COLUMN reachable INTEGER NOT NULL DEFAULT 1')
        if 'conversation' not in columns:
            try:
                self.db.execute("ALTER TABLE employees ADD COLUMN conversation TEXT NOT NULL DEFAULT '{}' ")
            except sqlite3.OperationalError:
                # Concurrent dashboard readers can race on this additive migration.
                if 'conversation' not in {row['name'] for row in self.db.execute('PRAGMA table_info(employees)')}:
                    raise
    def recover(self):
        """Call only after taking the exclusive runtime lock, never from admin readers."""
        # A process crash during sending has an ambiguous external outcome.
        self.db.execute("UPDATE outbox SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE inbox SET state='pending' WHERE state='processing'")

    @contextmanager
    def transaction(self):
        nested = self.db.in_transaction
        savepoint = 'sp_' + secrets.token_hex(6)
        self.db.execute('SAVEPOINT ' + savepoint if nested else 'BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            if nested:
                self.db.execute('ROLLBACK TO ' + savepoint)
                self.db.execute('RELEASE ' + savepoint)
            else:
                self.db.execute('ROLLBACK')
            raise
        else:
            self.db.execute('RELEASE ' + savepoint if nested else 'COMMIT')

    def one(self, sql, args=()):
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args).fetchall()]

    def employee(self, employee_id):
        return self.one('SELECT * FROM employees WHERE id=?', (employee_id,))

    def by_telegram(self, telegram_id):
        return self.one('SELECT * FROM employees WHERE telegram_id=? AND active=1', (telegram_id,))

    def add_employee(self, name, team, zone, can_request=False):
        if not name.strip() or not team.strip():
            raise ValueError('Name and team are required.')
        cur = self.db.execute('INSERT INTO employees(name,team,timezone,can_request) VALUES(?,?,?,?)',
                              (name.strip(), team.strip(), zone, int(can_request)))
        return cur.lastrowid

    def invite(self, employee_id):
        if not self.employee(employee_id):
            raise ValueError('Unknown employee.')
        code = secrets.token_urlsafe(18)
        self.db.execute('DELETE FROM invites WHERE employee_id=?', (employee_id,))
        self.db.execute('INSERT INTO invites VALUES(?,?,?)',
                        (hashlib.sha256(code.encode()).hexdigest(), employee_id, self.clock() + 3600))
        return code

    def claim(self, code, user_id, chat_id, display_name):
        digest = hashlib.sha256(code.encode()).hexdigest()
        with self.transaction():
            invite = self.one('SELECT * FROM invites WHERE hash=? AND expires>?', (digest, self.clock()))
            if not invite or self.by_telegram(user_id):
                return False
            employee = self.employee(invite['employee_id'])
            if employee['telegram_id'] is not None or not employee['active']:
                return False
            self.db.execute('DELETE FROM invites WHERE hash=?', (digest,))
            self.db.execute('INSERT OR REPLACE INTO enrollment VALUES(?,?,?,?,?)',
                            (employee['id'], user_id, chat_id, display_name[:120], self.clock()))
        return True

    def approve(self, employee_id, telegram_id):
        with self.transaction():
            pending = self.one('SELECT * FROM enrollment WHERE employee_id=? AND telegram_id=?',
                               (employee_id, telegram_id))
            if not pending:
                raise ValueError('No matching pending enrollment. Verify the person before approval.')
            self.db.execute('UPDATE employees SET telegram_id=?,chat_id=?,opted_in=1 WHERE id=?',
                            (telegram_id, pending['chat_id'], employee_id))
            self.db.execute('DELETE FROM enrollment WHERE employee_id=?', (employee_id,))
            self.queue(pending['chat_id'], 'Enrollment approved. Use /help to get started. '
                       'Your scheduling availability and acceptance status will be shared with the meeting organizer.', employee_id)

    def queue(self, chat_id, text, employee_id=None, keyboard=None, meeting_id=None,
              version=None, purpose='reply'):
        cursor = self.db.execute('''INSERT INTO outbox(chat_id,employee_id,text,keyboard,meeting_id,version,purpose,created)
                           VALUES(?,?,?,?,?,?,?,?)''',
                        (chat_id, employee_id, text[:3900], json.dumps(keyboard) if keyboard else None,
                         meeting_id, version, purpose, self.clock()))
        return cursor.lastrowid

    def conversation(self, employee_id):
        person = self.employee(employee_id)
        return json.loads(person['conversation']) if person else {}

    def remember_state(self, employee_id, **changes):
        data = {**self.conversation(employee_id), **changes}
        self.db.execute('UPDATE employees SET conversation=? WHERE id=?', (json.dumps(data), employee_id))

    def remember(self, employee_id, role, text, **metadata):
        person = self.employee(employee_id)
        if not person:
            return
        history = (json.loads(person['history']) + [{'role': role, 'text': text[:3900],
                   'timestamp': self.clock(), **metadata}])[-HISTORY_LIMIT:]
        self.db.execute('UPDATE employees SET history=? WHERE id=?', (json.dumps(history), employee_id))

    def conversation_history(self, employee_id):
        person = self.employee(employee_id)
        history = json.loads(person['history']) if person else []
        for item in history:
            if item.get('outbox_id'):
                row = self.one('SELECT status FROM outbox WHERE id=?', (item['outbox_id'],))
                item['delivery'] = row['status'] if row else 'unknown'
        return history[-HISTORY_LIMIT:]

    def prompt(self, employee_id, reply_to=None):
        """Bind short approvals to a prompt actually sent to this employee."""
        if reply_to is not None:
            return self.one("SELECT * FROM outbox WHERE employee_id=? AND message_id=? AND status='sent' ORDER BY id DESC LIMIT 1",
                            (employee_id, reply_to))
        return self.one("""SELECT * FROM outbox WHERE employee_id=? AND status='sent'
            AND (keyboard IS NOT NULL OR purpose IN ('invitation','reminder','clarification')) ORDER BY id DESC LIMIT 1""", (employee_id,))

    def tell(self, employee_id, text, **kwargs):
        person = self.employee(employee_id)
        if person and person['chat_id'] is not None:
            outbox_id = self.queue(person['chat_id'], text, employee_id, **kwargs)
            self.remember(employee_id, 'assistant', text, outbox_id=outbox_id,
                          purpose=kwargs.get('purpose', 'reply'), meeting_id=kwargs.get('meeting_id'))
            if kwargs.get('meeting_id') is not None:
                self.remember_state(employee_id, focus_meeting_id=kwargs['meeting_id'])
            return outbox_id

    def members(self, meeting_id):
        return self.all('''SELECT a.*,e.name,e.chat_id,e.telegram_id,e.timezone,e.opted_in,e.active,e.reachable
                         FROM attendees a JOIN employees e ON e.id=a.employee_id WHERE meeting_id=?''', (meeting_id,))

    def meeting(self, meeting_id):
        return self.one('SELECT * FROM meetings WHERE id=?', (meeting_id,))

    def meetings_for(self, employee_id, active_only=False):
        suffix = "AND m.state NOT IN ('cancelled','confirmed')" if active_only else ''
        return self.all('''SELECT DISTINCT m.* FROM meetings m JOIN attendees a ON a.meeting_id=m.id
                        WHERE a.employee_id=? ''' + suffix + ' ORDER BY m.id DESC LIMIT 20', (employee_id,))

    def reserve_ai(self, model, limit):
        day = datetime.fromtimestamp(self.clock(), timezone.utc).date().isoformat()
        with self.transaction():
            count = self.one('SELECT count(*) AS n FROM ai_usage WHERE day=?', (day,))['n']
            if count >= limit:
                raise ValueError('The daily AI call limit is reached. Use /status or try again tomorrow.')
            return self.db.execute('INSERT INTO ai_usage(day,model) VALUES(?,?)', (day, model)).lastrowid

    def ingest(self, updates):
        with self.transaction():
            for update in updates:
                self.db.execute('INSERT OR IGNORE INTO inbox(update_id,payload) VALUES(?,?)',
                                (update['update_id'], json.dumps(update)))
            if updates:
                self.db.execute("INSERT OR REPLACE INTO metadata VALUES('offset',?)",
                                (str(max(u['update_id'] for u in updates) + 1),))

    def offset(self):
        row = self.one("SELECT value FROM metadata WHERE key='offset'")
        return int(row['value']) if row else 0
