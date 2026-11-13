# Run the Telegram meeting bot locally

The local prototype uses **GPT-6 Luna**, Telegram long polling, and SQLite. No public server, tunnel, domain, webhook, or PostgreSQL installation is needed for this version.

Create a bot through [@BotFather](https://t.me/BotFather), then use its token and exact username in your local configuration. You also need an OpenAI API key with access to the configured model. Run the connection checks below from a network-enabled terminal.

**Prefer a browser?** Double-click **Meeting Bot.command**, or run `python3 -m meetingbot web --open`. The [web dashboard guide](web-dashboard.md) covers configuration, team members, invitations, verified approvals, bot controls, and meeting/usage status. The CLI instructions below remain available.

## 1. Start in the project folder

```sh
git clone https://github.com/RehanulBary/MeetingCoordinator-Telegram.git
cd MeetingCoordinator-Telegram
```

Use Python 3.11 or newer on macOS or Linux. The runtime uses Unix file locking; native Windows support is not implemented.

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

A fresh clone contains no credentials or company data. For terminal setup, copy the example once and enter credentials using hidden prompts:

```sh
cp .env.example .env
chmod 600 .env
python3 -m meetingbot configure
```

Edit `.env` to set `TELEGRAM_BOT_USERNAME` to your bot's exact username without `@`. Review the other settings below. Credentials stay in the ignored local `.env`; do not commit it. Alternatively, enter the settings in the dashboard. The default model is `gpt-6-luna`; the app does not automatically substitute another model. If your API project lacks model access, `doctor` reports the failure.

## 2. See the scheduling flow immediately

```sh
python3 -m meetingbot demo
```

This offline demo simulates the requester and three employees. It uses the real workflow, database operations, slot matching, proposal versions, and confirmation code, with **scripted AI interpretations and simulated Telegram delivery**. It makes no API calls and sends no real messages.

It demonstrates request preview → private availability questions → overlap calculation → organizer selection → each member's acceptance → final confirmation. Demo employees stay in an isolated in-memory database and are never added to the real company roster.

## 3. Review the company settings

The `.env` includes:

```text
OPENAI_MODEL=gpt-6-luna
TELEGRAM_BOT_USERNAME=YOUR_BOT_USERNAME
COMPANY_TIMEZONE=Asia/Dhaka
DATABASE_PATH=data/meetingbot.sqlite3
MAX_AI_CALLS_PER_DAY=100
WORKDAY_START_HOUR=9
WORKDAY_END_HOUR=18
WORKDAYS=0,1,2,3,4,5,6
```

Set your actual company workdays before inviting coworkers: Monday is `0`, Sunday is `6`. The prototype initially allows every day from 09:00–18:00 company time, so it does not silently assume which days your company closes. Suggested meeting times stay within these configured hours. Reminders also respect 09:00–18:00 in each recipient's own time zone and the configured weekday list.

The daily limit bounds application AI calls, including failed calls. It is not an account-wide billing cap and does not cover API usage from other applications. The app makes no automatic OpenAI retries and requests schema-validated output with medium reasoning effort for coordination planning. Conversation memory and richer context can increase token usage compared with the original extractor.

## 4. Start the bot

```sh
./run.sh
```

Alternatively:

```sh
python3 -m meetingbot run
```

Leave that terminal and computer running. The app checks the bot identity and that no webhook is active, registers its command menu, and runs the receiver, processor, sender, and reminder timer. One process owns the database's runtime lock; a second process is refused.

If the computer sleeps, shuts down, or loses internet, the bot cannot process messages or send reminders. Saved local meeting state survives restarts; do not assume Telegram retains an unlimited history of missed updates.

To test OpenAI's actual structured-output call after network connectivity works:

```sh
python3 -m meetingbot doctor --ai-smoke
```

That optional command makes one small billable model call. Normal `doctor` checks connectivity, token identity, and model metadata without generating a response. API errors are summarized by type without token-bearing URLs or response bodies.

## 5. Enroll yourself and coworkers

Company records are created locally by the trusted operator. The database starts empty on a fresh clone. First create your organizer record, using the real name, team, and time zone:

```sh
python3 -m meetingbot add-member --name 'Organizer Name' --team Management --timezone Asia/Dhaka --requester
```

List the roster and use the employee IDs returned by your installation:

```sh
python3 -m meetingbot members
```

Generate your enrollment link using the displayed employee ID, for example:

```sh
python3 -m meetingbot invite E-1
```

Open that link in Telegram and press **Start** while the bot is running. Enrollment links expire after one hour and are single-use. A link alone does not grant company access: the enrollment stays pending until the operator verifies the Telegram account and approves it.

In a second terminal in the same folder:

```sh
python3 -m meetingbot pending
```

After independently verifying the person's identity, use the numeric Telegram ID shown by `pending`:

```sh
python3 -m meetingbot approve E-1 YOUR_NUMERIC_TELEGRAM_ID --verified
```

Replace `YOUR_NUMERIC_TELEGRAM_ID`; it is a placeholder, not a command argument to paste unchanged. `--verified` means the operator has checked the real employee/account relationship. It is required for approval, including your own initial requester account.

Add real coworkers, using their actual names and teams:

```sh
python3 -m meetingbot add-member --name 'Coworker Name' --team Engineering --timezone Asia/Dhaka
```

Then issue that employee's enrollment link and approve their account in the same way. Use `--requester` when creating an employee who should be allowed to arrange meetings. In this pilot, requester access permits company-wide team/member selection; finer per-team access rules are not implemented.

An employee's Telegram username or display name is not used as proof of identity. Membership in a company group alone does not complete enrollment or enable private outreach.

## 6. Run a real meeting test

After the requester and at least one real coworker are enrolled, message the bot privately:

> Arrange a 30-minute Engineering review tomorrow between 14:00 and 17:00, Dhaka time. I am available during that range.

The bot interprets the request with GPT-6 Luna, looks up the real directory, and shows the exact dates, attendees, duration, and your availability. Review them and press **Start coordination**.

Each invited member replies, for example:

> For M-1, tomorrow from 14:30 until 16:00, Dhaka time.

The bot keeps the draft and current question across replies. You can correct a duration, add/remove a member, or provide availability in several messages. A missing title is generated automatically; the default duration is 30 minutes and is shown in the preview. Review the exact dates, people, and your availability, then say “go ahead” or use **Start coordination**.

Once all required members give availability, the organizer receives up to three options and can say “the second one” or select its button. Each member can reply “yes, that works” to the exact delivered proposal, or use **Confirm** / **Decline**. The backend binds that reply to the recipient's delivered prompt and current proposal version before recording acceptance. A bare yes without a matching prompt brings up confirmation controls. Reply to a specific Telegram message when switching between meetings.

See [conversation behavior and regression checks](conversation-coordination.md) for examples and live language evaluation.

The bot sends the final message only after every required attendee accepts. It never creates a Google Calendar or Outlook event in this prototype.

## 7. Commands and operator inspection

Employee commands:

| Command | Behavior |
|---|---|
| `/help` | Example requests and supported commands |
| `/teams` | Company teams; only available to requesters |
| `/schedule` | Explain what details to send |
| `/new` | Clear your unfinished draft; existing meetings stay intact |
| `/status` or `/status M-1` | Authorized meeting progress |
| `/settings Europe/London` | Set your own IANA time zone |
| `/cancel M-1` | Organizer explicitly cancels that request |
| `/stop` | Stop scheduling outreach |
| `/resume` | Explicitly enable scheduling notifications again |

Natural-language cancellation asks the organizer to confirm with a button. A changed slot clears old confirmations. Unknown accounts cannot browse the company directory or invoke model calls.

When you need deterministic availability input without an AI call, use a full ISO timestamp with offset:

```text
/available M-1 2026-11-18T14:00+06:00 2026-11-18T16:00+06:00
```

Replace the date/reference with your actual future meeting. Both bounds must fall inside its date range.

Local operator commands:

```sh
python3 -m meetingbot members
python3 -m meetingbot pending
python3 -m meetingbot meetings
python3 -m meetingbot queues
python3 -m meetingbot usage
```

These terminal commands are privileged local administration. `queues` shows failed/uncertain jobs without dumping message bodies. Unknown send results are left for operator review rather than blindly duplicated. There is no exactly-once guarantee for external message delivery.

## 8. Costs for this local implementation

Telegram's standard messaging and the existing computer/local database add no platform or cloud-hosting subscription. Electricity, internet, hardware, development, and support remain your own costs.

The requested model is **GPT-6 Luna**. Its official published standard token rates checked on 10 November 2026 are **$0.10/million input tokens and $0.50/million output tokens**. [Official model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna)

For an illustrative 20 calls per meeting, each with 1,000 billed input tokens and 250 total billed output tokens, model cost would be **$0.0045 per meeting**, or **$0.45 for 100 meetings** before taxes. These are assumptions, not measured results. Prompts include context, the directory, and conversation history; actual token usage can be higher. Inspect `usage` after live tests. The older architecture documents used a different model and cloud-hosting scenario; their dollar totals are not the local prototype's budget.

## 9. Validation and current limits

Run the meaningful workflow and API-contract checks:

```sh
python3 -m unittest discover -s tests -v
```

They cover required-member acceptance, stale proposals/previews, permissions, enrollment, opt-out, cancellation, duplicate updates, deadlines, reminder limits, time zones, working hours, internal meeting conflicts, persistence/recovery, and the OpenAI/Telegram request contracts using mocked HTTP responses.

**Live verification still requires network-enabled API access.** An offline passing test does not prove the supplied credentials or GPT-6 Luna project access are valid.

Prototype limits:

- SQLite and one running process; production multi-worker/PostgreSQL deployment is still future work.
- All selected attendees are required. Per-attendee optional roles are not implemented.
- Company-wide requester access; no per-team requester ACL. A local operator web dashboard is implemented; remote administration is not.
- Company-wide workdays/hours, with individual time zones; per-employee recurring work schedules are not implemented.
- Maximum 20 total people, 31-day request window, and 5–240 minute duration.
- No calendar synchronization, actual video meeting link creation, roster sync, automatic data-retention purge, or automated off-device backup.
- Up to two alternative-availability rounds. If no overlap remains, the organizer creates a revised request.
- Keep the computer and local database protected and back up consistently before using this for important company coordination.

Further references: [Telegram Bot API](https://core.telegram.org/bots/api), [Telegram private-chat rules](https://core.telegram.org/bots), [OpenAI structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).
