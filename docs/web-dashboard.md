# Meeting Bot web dashboard

Use the dashboard to manage the Telegram coordinator from your browser. Employees still talk to the bot in Telegram; the dashboard is for the company operator.

## Open it

On macOS, after installing the dependencies, double-click **[Meeting Bot.command](../Meeting%20Bot.command)** in the project folder. It opens a Terminal window and your browser at **http://127.0.0.1:8765**.

Alternatively, from the cloned project folder with its virtual environment active:

```sh
python3 -m meetingbot web --open
```

Keep the Terminal window open. Closing the browser tab leaves the dashboard and its bot running. Pressing **Ctrl+C in the dashboard Terminal** stops both. Meetings and queued work stay in SQLite.

No additional web packages, Node installation, public domain, tunnel, or hosting subscription are needed. The dashboard uses Python's standard library and the bot's existing dependencies. For a fresh installation, follow the [local quickstart](telegram-local-quickstart.md).

If port 8765 is already occupied, open the existing dashboard or use:

```sh
python3 -m meetingbot web --open --port 8766
```

The dashboard needs local loopback access; live coordination also needs internet access to Telegram and OpenAI. Offline endpoint tests do not prove that live API access works.

## First run: a practical example

1. Open **Settings**. Enter your Telegram bot token, exact bot username, and OpenAI API key. Credentials previously saved in `.env` appear as **Saved**; their values are never displayed. The default model is **gpt-6-luna**. Set your company's actual time zone, working days, and hours. Blank credential inputs keep existing values. Click **Save settings**.
2. Open **Activity & usage** and click **Run checks**. This verifies Telegram identity, webhook state, and OpenAI model metadata access. It generates no AI response and sends no Telegram messages. A successful metadata check does not guarantee that a later model response will succeed.
3. Click **Start bot**. The badge shows **Starting** until a successful Telegram poll, then **Online**. A failed start is visible in **Bot events**. A lost Telegram connection shows **Reconnecting** while the backend retries.
4. Open **Members**. A fresh clone starts with no company records. Add the organizer with **Can request meetings** enabled. To add Ayesha as a participant, click **Add member**, enter her name, team, and time zone, then save. Enable **Can request meetings** only if she should select company-wide teams and members.
5. Click **Invite** on Ayesha's row. Copy her one-hour, single-use invitation link and share it with her yourself. The dashboard does not send invitations automatically. A newly generated link invalidates the previous one.
6. Ayesha opens the link in Telegram and presses **Start** while the bot is running. Her request appears in **Waiting for your approval**. Review the numeric Telegram ID, verify directly with her that she owns that account, tick the verification box, and click **Approve enrollment**. A display name is not identity verification.
7. Enroll the requester in the same way. After at least two real people are connected, the requester messages the bot: “Arrange a 30-minute meeting with Ayesha tomorrow between 14:00 and 17:00, Dhaka time.”
8. Follow the request under **Meetings**. The requester reviews the bot's draft on Telegram, members supply availability, the requester chooses an option, and every required member confirms that exact time. Click **View** in the dashboard to see attendee response status.

## What each page does

| Page | Controls and information |
|---|---|
| Overview | Member/team counts, active requests, today's AI call count, current bot state, and setup steps |
| Members | Add/edit name, team, time zone, and requester permission; search/filter teams; create invitation links; verify and approve enrollment |
| Meetings | Latest 100 requests, status filtering, requested range, proposed time, and attendee status |
| Settings | Telegram username/token, OpenAI API key/model, daily call limit, company time zone, working hours and days |
| Activity & usage | Connection checks, processing/delivery queue counts, failed/uncertain jobs, recorded token usage, and recent bot process events |

The shared **Start bot / Stop bot** control manages only the process launched by this dashboard. If the bot was started using `./run.sh` or another Terminal, the dashboard detects its database lock and asks you to stop it there. It never kills an unrelated process.

After code updates, restart the bot to load them. The dashboard shows a restart notice when the running coordinator's source revision differs from the files on disk. Send `/version` to the bot to check the loaded coordinator version; the current conversation update is **0.3.1**. Restart the dashboard launcher itself if it was already open before the dashboard update.

Change settings while the bot is stopped and connection checks have finished. Save, then start again. The dashboard reads `.env` on each action and uses those saved values for its child bot process; saved file values take precedence over inherited environment values in dashboard mode. Direct CLI commands continue to allow environment variables to override `.env`.

Member edits apply to directory records immediately. Existing meetings retain their selected employee IDs. A team is a shared team name on member records; changing one person's team does not rename everyone else's team. There is no separate team-delete or employee-delete action.

## Local access and credentials

The server binds to **127.0.0.1 only**, checks the Host and request Origin, rejects cross-site browser requests, and requires a session cookie plus CSRF token for changes. It does not serve `.env`, SQLite files, or arbitrary filesystem paths. Browser responses are marked `no-store`, and external scripts, fonts, and analytics are not used.

Credentials are saved atomically to the ignored `.env` with owner-only file permissions. Existing credentials never enter API responses or form values. Saving a blank credential input preserves its value; enter a replacement to change it. Database selection stays outside the form so an accidental edit cannot switch the workspace's company data.

This is a **trusted local operator dashboard**, with no account login or remote-user permissions. Anyone who can use your local computer session or run local code can administer it. Keep it local; an internet-facing or shared-company dashboard needs authentication, HTTPS, and an appropriate deployment design first.

## Common problems

| What you see | What to do |
|---|---|
| Cannot resolve API host | Check internet/DNS from the normal Terminal; local admin pages still work without external API access |
| Start failed | Open Activity and inspect the sanitized event; run connection checks and review credentials/model access |
| Existing webhook | Remove the webhook explicitly using your Telegram setup process before long polling; the dashboard does not remove it automatically |
| Running in another Terminal | Stop that instance there, then use Start bot in the dashboard |
| A coworker never appears under approvals | Keep the bot running; have them open their personal link and press Start; generate a fresh link if it expired |
| An account is not the expected employee | Leave it unapproved; verify the numeric account ID before granting access |
| Outgoing delivery is uncertain | It may already have arrived. Review the conversation before taking manual action; the dashboard offers no blind resend |
| Browser cannot reach the dashboard | Start the launcher, keep its Terminal open, and confirm the port in the printed URL |
| Computer sleeps | Processing and reminders pause until the computer is awake and connected again |

No calendar synchronization, meeting video-link creation, browser-based employee chat, remote administration, automated roster import, or automatic retry of failed jobs is added by this dashboard. Scheduling and final confirmations remain in Telegram.

## Validation

```sh
python3 -m unittest discover -s tests -v
node --check meetingbot/web_assets/app.js
```

The tests cover the scheduling workflow and dashboard endpoints, settings validation and atomic persistence, credential redaction, local-request protections, verified enrollment, runtime ownership, health reporting, and connection-error handling. HTTP requests are exercised against the real handler with in-memory transport so tests make no network calls. Desktop, small-screen, and landscape browser previews use separate simulated data and never modify the real company roster.
