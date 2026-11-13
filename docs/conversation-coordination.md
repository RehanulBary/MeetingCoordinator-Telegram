# Conversation-aware coordination

The backend keeps the current task in SQLite and gives GPT-6 Luna the facts needed for the next conversational step. After updating the code, use **Stop bot**, then **Start bot** in the dashboard to load the new coordinator. Existing employee records and meetings are preserved. The additional conversation-state column is created automatically when the database is opened.

The current coordinator is **0.3.1**. Send `/version` in your private Telegram chat after restarting to confirm that this version is loaded. A process started before the update keeps its imported old code until restarted. If you started the bot in a Terminal, stop it there with Ctrl+C and run it again; if the dashboard manages it, use Stop bot followed by Start bot. Reopen the dashboard's launcher to load updated dashboard code. The updated dashboard flags a running coordinator whose source revision is older than the files on disk.

## What is remembered

- The unfinished request, attendees, duration, dates, and organizer availability.
- The active meeting and the specific clarification being answered.
- Partial availability, such as a known day and start time while waiting for an end time.
- The latest 40 conversation events, including commands, bot replies, reports, reminders, and validated errors. Critical draft and availability facts are stored separately and survive history truncation and restarts.
- The person's own saved availability, current meeting state, available options, and proposal version. Requesters see their authorized directory and attendee progress. Other members do not receive coworkers' private conversations or the whole directory.
- The actual Telegram message being replied to. Short approvals bind to a prompt recorded as delivered to that recipient.
- Canonical company entities: the planner maps ordinary phrases such as “the management team,” “management folks,” or “Shanto instead” to existing team names/member IDs. The backend validates those identities against the active directory. Replacing an attendee preserves unrelated topic, duration, and date/time details. Ambiguous expressions still require clarification.

The model receives actual user/assistant turns, plus current backend state. Failed or undelivered outgoing messages are omitted from the model's spoken-message history. GPT-6 Luna remains the configured model, now using medium reasoning effort. Every generated action is validated by the backend; model text alone cannot authorize access, invent attendees, or bypass required confirmations.

This uses local application-managed memory and `store=False` for model responses. See the official [conversation-state guidance](https://developers.openai.com/api/docs/guides/conversation-state), [structured-output documentation](https://developers.openai.com/api/docs/guides/structured-outputs), and [GPT-6 Luna model documentation](https://developers.openai.com/api/docs/models/gpt-6-luna).

## Example conversations

### Build a request in several replies

> Requester: I need a meeting with Engineering.
>
> Bot: When would you like to meet? A day or a date range is enough to start.
>
> Requester: Thursday between 2 and 5 pm.
>
> Bot: Shows a preview with the Engineering members, resolved date/time, saved time zone, and a 30-minute default.
>
> Requester: Make it 45 minutes. Also include Sara from Design.
>
> Bot: Shows the updated preview with the original Engineering attendees plus Sara and the new duration.
>
> Requester: Go ahead.
>
> Bot: Starts coordination after checking the delivered preview, its nonce, and the current roster.

The title is optional. A default is generated. If the date alone is specified, the planner proposes company working hours as the search range and shows them for review. Ambiguous names or times still need a specific clarification. Request details are never sent to coworkers before the organizer approves the preview.

### Collect availability without asking for known details

> Bot: Shows a Thursday 2–5 pm invitation in the recipient's saved time zone and asks what time works.
>
> Member: After 2:30.
>
> Bot: Until what time are you available?
>
> Member: 4 pm.
>
> Bot: Saves Thursday 2:30–4 pm for the same meeting.

The pending day/start/time zone survive intervening small talk and process restarts. A correction such as “actually until 4” can reuse a previously explicit start. “Also 4:30–5” adds a separate interval when interpreted as additional availability; a replacement updates the old interval.

### Choose and confirm naturally

> Bot to requester: Offers three matching time options.
>
> Requester: The second one, please.
>
> Bot: Sends the selected proposal to the required attendees.
>
> Member: Yes, that works.
>
> Bot: Records acceptance of the exact delivered proposal after validating its version and conflicts.

Everyone required must accept before confirmation. A reply attached to an old proposal cannot confirm a new version. A yes to an availability invitation does not count as acceptance of a final time. Buttons remain available as an alternative.

If multiple meetings are active, replying to a specific Telegram message selects that meeting. When neither the reply target nor current context identifies the meeting, the bot asks which one. It does not silently pick a different request.

### Correct an ambiguous attendee

> Requester: Also add Sara.
>
> Bot: Which Sara did you mean? Lists matching company members with their teams and employee IDs.
>
> Requester: The one in Design.
>
> Bot: Completes the pending addition and retains the other attendees.

The unresolved add/remove operation is stored in the draft. A failed correction invalidates the old preview so an older approval cannot launch an incomplete attendee list.

## Coordination when there is no overlap

Previous availability stays saved when alternatives are requested. Where possible, the bot suggests concrete intervals that fit the other attendees, then asks whether the recipient can adjust. It never changes availability on their behalf. After two alternative rounds, the organizer decides how to revise the request.

`/new` explicitly clears the current draft and conversation focus; it leaves existing meetings intact. Natural-language discarding only abandons the unfinished draft. Cancelling an existing meeting still requires an organizer's explicit cancellation confirmation.

## Verify behavior

Run the offline regression suite:

```sh
python3 -m unittest discover -s tests -v
```

It tests durable memory, partial replies, corrections, attendee additions/removals, exact proposal binding, stale replies, multiple meetings, directory permissions, database persistence, and the OpenAI request contract. Scripted model interpretations isolate backend behavior; they do not prove that a live model will interpret every sentence correctly.

Run the real GPT-6 Luna language checks from a normal, network-enabled Terminal:

```sh
python3 -m meetingbot check-conversation --live
```

This makes **up to twelve billable model calls**, using fictional in-memory employees and meetings. It sends no Telegram messages and does not change real company records. Cases cover greetings, duration changes, adding a person, shorthand availability, finishing a partial answer, selecting a time, status, a Bangla directory question, ordinary team phrases, replacing stale attendee wording, pronouns, and “same time” date corrections. Evaluation usage is isolated from the company's SQLite usage view; it still appears in OpenAI project billing.

Live language checks could not run from the restricted coding environment because `api.openai.com` DNS resolution failed. No live evaluation pass is claimed. After restarting the bot, try the exchanges above with enrolled test participants and inspect actual behavior and usage.

Current limits remain: no calendar integration, recurring meetings, optional attendees, or autonomous changes to a meeting's attendees/date range after coordination has started. The organizer should cancel and create a revised request for those changes. The language planner may still need clarification for ambiguous dates, names, or incomplete time ranges.
