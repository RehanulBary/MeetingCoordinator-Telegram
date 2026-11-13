# How to use the Meeting Bot on WhatsApp or Telegram

**Prepared:** 10 November 2026\
**Audience:** company admins, meeting requesters, and team members\
**Status:** usage guide for the planned product. The bot has not been implemented or connected to either platform yet. The conversations, buttons, and actions below describe what we will build.

## 1. How it works

Open a private chat with the company bot, ask for a meeting, and answer its questions. The bot contacts the registered members, gathers availability, finds suitable times, and reports the result.

**Our backend AI handles the conversation.** WhatsApp or Telegram delivers the messages. Our company server checks permissions, uses the employee database, calculates matching times, and sends responses.

```text
Requester sends a message
         ↓
WhatsApp or Telegram forwards it to our server
         ↓
Server looks up the team and uses AI to understand the request
         ↓
Bot privately asks members about availability
         ↓
Server finds a suitable time and the bot collects confirmation
         ↓
Requester receives the result
```

The first version confirms the agreed time through chat. A Google Calendar or Outlook invitation needs a later calendar integration.

## 2. Before employees use it: company setup

An admin must first:

1. Add employees and team membership to the company database.
2. Decide who can request meetings and which teams they can contact.
3. Record working hours, time zones, notification preferences, and the permission needed for scheduling messages and availability summaries.
4. Link each employee to their verified messaging account using a company-issued enrollment process.
5. Deploy the backend, database, and message handling, then connect the selected platform.
6. Test with a small team before sharing the bot company-wide.

**Example:** Engineering contains Ayesha, Sara, and Nabil. When an authorized requester asks for “Engineering,” the server reads these members from the database.

Creating a messaging account or registering a Telegram bot is one setup step. The deployed backend supplies the scheduling behavior described in this guide.

## 3. Using WhatsApp

### A. What the company sets up

Use the official WhatsApp Business Platform with a company-controlled account and eligible business number. Connect that account to our backend. Meta supports direct and partner integration routes; our architecture assumes a direct connection. [Official platform overview](https://business.whatsapp.com/products/business-platform)

The implementation team will:

1. Complete the account and number setup required in the company's Meta dashboard.
2. Configure API access and message notifications to our server.
3. Prepare and obtain approval for the standard message templates required for proactive requests, reminders, and updates.
4. Check the actual template categories and account billing rates.
5. Register and test employees' messaging identities and notification permissions.

Meta's [getting-started documentation](https://developers.facebook.com/documentation/business-messaging/whatsapp/get-started) is the setup reference. Its contents required login during this research, so exact dashboard instructions must be checked during implementation. Developers can use our [technical reference](technical-reference.md#2-architecture-a--whatsapp) for the proposed backend connection.

### B. How an employee joins

1. Get the official bot number or company-provided chat link from the admin.
2. Open a private WhatsApp conversation with that account.
3. Complete the company's enrollment instructions so the server can verify your employee identity.
4. Agree to the scheduling notifications and availability-sharing behavior described during enrollment.
5. Check your saved team, time zone, and working hours.

Enrollment must verify company identity; typing a coworker's name or email alone will not grant that person's access.

### C. How a requester schedules a meeting

Send a request such as:

> Arrange a 30-minute meeting with Engineering on Wednesday, 18 November, between 2 and 4 PM, Dhaka time. All members must attend. I am available during that period too.

The bot will look up the team, clarify anything missing, and show a preview before contacting members. After collecting replies, it will show suitable options and ask which time to confirm.

### D. How a member replies

You will receive a meeting request with a reference such as **M-104**. Reply in the same private chat:

> For M-104, I am available on 18 November between 2:30 and 4 PM, Dhaka time.

When the bot proposes a specific time, explicitly confirm, decline, or suggest another time. Stating availability earlier does not automatically confirm the final proposal.

### E. A WhatsApp rule employees may notice

Outside the permitted reply window, messages use approved standard templates. Free-form replies are allowed within 24 hours of the member's latest message; receiving a template alone does not reset that window. Recipients must have opted in. [Official messaging policy](https://business.whatsapp.com/policy)

**Example:** Sara last replied on Monday at 9 AM. A Wednesday reminder needs an approved template. After Sara replies, the bot can continue the conversation within the new reply window.

Templates can make reminders look more standardized than ordinary replies. The 24-hour rule concerns what can be sent; it does not guarantee free messaging.

## 4. Using Telegram

### A. What the company sets up

1. Open the official [BotFather](https://t.me/BotFather) in Telegram.
2. Use its bot-creation flow (`/newbot`) and choose the company bot's name and available username.
3. Give the resulting API token to the authorized implementation team through the company's secret-management process. Keep it out of employee chats and source files.
4. Connect the bot to our deployed backend and test incoming and outgoing messages.
5. Prepare company enrollment links and share them with the pilot team.

Telegram documents bot creation and token ownership in its [official introduction](https://core.telegram.org/bots#how-do-i-create-a-bot). The company enrollment and scheduling features are our application design.

### B. How an employee joins

1. Open the bot's company-provided enrollment link.
2. Press **Start** in its private chat.
3. Complete the employee verification steps associated with your enrollment link.
4. Check your saved team, time zone, working hours, and notification preferences.

**Each member must initiate their private bot chat before the bot can contact them there.** Being in the company Telegram group alone does not enable private messages. [Telegram's private-chat rules](https://core.telegram.org/bots#how-are-bots-different-from-users)

### C. How a requester schedules a meeting

Send an ordinary message to the bot:

> Arrange a 30-minute meeting with Sara and Nabil on Wednesday, 18 November, between 2 and 4 PM, Dhaka time. Both must attend, and I am available too.

The bot will clarify the details, contact the verified members, and report proposed times. Select an option and let the bot collect everyone's final acceptance.

### D. How a member replies

Reply with your availability or use the proposed buttons:

> **Bot:** Can you attend M-104 on Wednesday, 18 November, 2:30–3:00 PM, Dhaka time?
>
> **Buttons:** Confirm · Suggest another time · Decline

Inline buttons are supported by Telegram; the meeting actions above must be implemented by our backend. [Official bot features](https://core.telegram.org/bots/features#inline-keyboards)

If you cannot use a button, reply with the meeting reference and the full date/time. The bot should clarify any ambiguous response.

## 5. Useful messages on either platform

These are **planned natural-language actions**, not commands already available in a deployed bot.

| What you want | Example message |
|---|---|
| Request a team meeting | “Arrange a 30-minute Engineering meeting on 18 November, between 2 and 4 PM, Dhaka time.” |
| Request named members | “Meet with Sara from Engineering and Nabil for 45 minutes next week.” |
| Give availability | “For M-104, I am free on 18 November, 2–4 PM, Dhaka time.” |
| Confirm a proposal | “I confirm M-104 for 18 November, 2:30–3 PM, Dhaka time.” |
| Suggest an alternative | “For M-104, could we try 18 November, 3–3:30 PM, Dhaka time?” |
| Decline | “I cannot attend M-104.” |
| Ask for progress | “What is the status of M-104?” |
| Change your availability | “Update my availability for M-104 to 18 November, 4–5 PM, Dhaka time.” |
| Cancel as the organizer | “Cancel M-104.” |
| Stop notifications | “Stop sending me scheduling notifications.” |
| Ask for help | “Help me contact the company bot administrator.” |

The server checks permissions for every action. Changing a confirmed meeting's time starts a new proposal and requires fresh acceptance from the required attendees.

## 6. How to understand the bot's updates

| Status | Meaning | Example |
|---|---|---|
| Collecting availability | The bot is still asking people for suitable times. | “Two of three members replied.” |
| Options ready | A possible time fits the stated availability. | “2:30–3 PM works with everyone's replies. Choose this time?” |
| Awaiting confirmation | A specific time has been proposed; acceptance is incomplete. | “Ayesha and Sara accepted; Nabil is pending.” |
| Confirmed | The organizer selected the proposal and every required attendee accepted that exact time. | “M-104 is confirmed.” |
| Needs your decision | The deadline or negotiation limit was reached, or requirements conflict. | “There is no common time. Broaden the date range?” |
| Cancelled | The request was cancelled and reminders stopped. | “Rehan cancelled M-104.” |

“Confirmed” describes agreement through this bot. The bot must separately report whether a calendar event was created after that integration is added.

## 7. Common problems and what to do

| Problem | What the user should do | What the bot should do |
|---|---|---|
| A person is missing from the team | Ask the company admin to correct membership. | Show the members it found before outreach. |
| A Telegram member has not joined | Have the admin share that member's enrollment link. | Report that enrollment is needed; leave them unconfirmed. |
| A WhatsApp member is not enabled for outreach | Ask the admin to check registration and messaging permission. | Explain the blocked outreach and record the member as unreachable. |
| Someone has not replied | Check the meeting status or ask the person directly. | Follow the reminder limits and show their response as pending. |
| No time works | Give a wider date range or revise optional attendance. | Explain the conflict and ask the organizer to decide. |
| The bot asks which meeting you mean | Include the reference, such as M-104. | Keep the two meetings' replies separate. |
| A suggested time is wrong | State the full date, time range, and time zone. | Clarify and correct the saved availability. |
| You need to change an accepted time | Tell the bot using the meeting reference. | Reopen coordination, inform the organizer, and collect fresh acceptance if the slot changes. |
| A delivery or backend failure occurs | Ask for status after service returns; contact the admin if needed. | Resume saved work, record uncertain sends, and avoid blindly duplicating confirmations. |

## 8. What each person can see

- **Requester:** their meeting details, suitable times, and who has confirmed, declined, or not replied.
- **Member:** the meeting request and their own private conversation with the bot.
- **Authorized admin:** the employee/team records and operational information needed to support the service.
- **Backend AI:** the relevant conversation context supplied by our server for interpreting the current request.

The server accesses the database with controlled credentials. A member's private explanation for being unavailable should stay out of the organizer's scheduling report.

## 9. Costs and further documentation

Telegram's normal bot messaging is free within its standard rate limits; hosting and AI are separate. The planned pilot leaves optional paid broadcasts disabled. [Official Telegram messaging limits](https://core.telegram.org/bots/faq#how-do-i-avoid-hitting-limits)

WhatsApp messaging costs depend on the account's applicable rates and message categories. Our existing Bangladesh estimates remain provisional until verified in the company account.

- [Easy-to-read architecture and pricing plan](architecture-and-pricing.md)
- [Technical reference and setup details](technical-reference.md)
- [Pricing assumptions](pricing-assumptions.json)

For employees, the admin will eventually provide the actual bot contact, enrollment link, support contact, and working-hours policy. Those company-specific details have not been configured yet.
