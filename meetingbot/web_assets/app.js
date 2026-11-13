'use strict';
const $ = (s, p=document) => p.querySelector(s);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths = {
  overview:'<rect x="3" y="3" width="7" height="7" rx="1.5"/><rect x="14" y="3" width="7" height="7" rx="1.5"/><rect x="3" y="14" width="7" height="7" rx="1.5"/><rect x="14" y="14" width="7" height="7" rx="1.5"/>',
  members:'<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2m20 0v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/><circle cx="9" cy="7" r="4"/>',
  meetings:'<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 11h18M8 15h2m4 0h2m-8 3h2"/>',
  settings:'<path d="m9 3-1 3-3 1-2 3 2 2-1 3 2 3 3-1 2 3h3l1-3 3-1 2-3-2-2 1-3-2-3-3 1-2-3Z"/><circle cx="11.5" cy="11.5" r="3"/>',
  activity:'<path d="M3 12h4l3-8 4 16 3-8h4"/>',
  play:'<path d="m8 5 11 7-11 7Z"/>', stop:'<rect x="6" y="6" width="12" height="12" rx="1"/>',
  check:'<path d="m5 12 4 4L19 6"/>', arrow:'<path d="M5 12h14m-5-5 5 5-5 5"/>',
  plus:'<path d="M12 5v14M5 12h14"/>', search:'<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  close:'<path d="m6 6 12 12M6 18 18 6"/>', external:'<path d="M15 3h6v6m0-6L10 14m-1-9H5a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-4"/>',
  bot:'<rect x="4" y="6" width="16" height="14" rx="4"/><path d="M12 2v4M1 11v4m22-4v4M8 15h8M8 10h.01M16 10h.01"/>',
  clock:'<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  team:'<rect x="4" y="3" width="16" height="18" rx="2"/><path d="M8 7h2m4 0h2M8 11h2m4 0h2m-6 9v-5h4v5"/>',
  usage:'<path d="M5 20V10m7 10V4m7 16v-7M3 21h18"/>', refresh:'<path d="M20 7v5h-5M4 17v-5h5m-4.6-4a8 8 0 0 1 13.3-3L20 8M4 16l2.3 3A8 8 0 0 0 19.6 16"/>',
};
const icon = key => `<svg viewBox="0 0 24 24" aria-hidden="true">${paths[key] || paths.meetings}</svg>`;
const pages = {
  overview:['Overview','Your meeting coordinator, in one place.'],
  members:['Members','Build your teams and connect everyone to Telegram.'],
  meetings:['Meetings','Follow each request from availability to confirmation.'],
  settings:['Settings','Configure your bot, AI model, and company working hours.'],
  activity:['Activity & usage','Connection checks, delivery health, and recorded AI usage.'],
};
let state, csrf='', page='', polling=false, refreshAgain=false, navSignature='', toastTimer, memberSearch='', memberTeam='', meetingFilter='';
const fmt = (value, time=true) => {
  if (!value) return '—';
  const date = new Date(typeof value==='number' ? value*1000 : value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat(undefined, {month:'short',day:'numeric', ...(time ? {hour:'2-digit',minute:'2-digit'} : {}),timeZone:state?.settings.COMPANY_TIMEZONE || 'Asia/Dhaka'}).format(date);
};
const amount = n => Number(n || 0).toLocaleString();
const statusInfo = s => ({online:['Online','success'],starting:['Starting','warning'],reconnecting:['Reconnecting','warning'],stopped:['Stopped','neutral'],failed:['Start failed','error'],external:['Running elsewhere','warning'],collecting:['Collecting availability','warning'],options:['Time options ready','success'],confirming:['Awaiting confirmation','warning'],confirmed:['Confirmed','success'],pending:['Pending','neutral'],cancelled:['Cancelled','neutral'],needs_decision:['Needs a decision','warning'],accepted:['Accepted','success'],available:['Available','success'],invited:['Invited','neutral'],unreachable:['Unreachable','error'],declined:['Declined','error'],opted_out:['Opted out','neutral'],optedout:['Opted out','neutral'],waiting:['Waiting','neutral']}[s] || [String(s || 'Pending').replaceAll('_',' '),'neutral']);
const badge = (s, label) => {const [text,color]=statusInfo(s);return `<span class="badge ${color}">${esc(label || text)}</span>`;};
const empty = (title, text, action='', symbol='meetings') => `<div class="empty">${icon(symbol)}<h3>${esc(title)}</h3><p>${esc(text)}</p>${action}</div>`;
const initials = name => name.split(/\s+/).slice(0,2).map(w=>w[0] || '').join('').toUpperCase();
const personCell = m => `<div class="member-cell"><span class="initials" aria-hidden="true">${esc(initials(m.name))}</span><div><strong>${esc(m.name)}</strong><small class="mono">E-${m.id}</small></div></div>`;

function toast(message, error=false) {
  clearTimeout(toastTimer); const el=$('#toast'); el.textContent=message;el.className='toast'+(error?' error':'');el.hidden=false;
  toastTimer=setTimeout(()=>{el.hidden=true;},error?9000:5500);
}
async function api(path, data) {
  const response=await fetch(path, data===undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(data)});
  const result=await response.json();
  if(!response.ok){const error=new Error(result.error || 'The action failed. Try again.');error.field=result.field;throw error;}
  return result;
}
async function refresh(render=false) {
  if(polling){if(render)refreshAgain=true;return;} polling=true;
  try {
    state=await api('/api/state');csrf=state.csrf;
    $('#offline').hidden=true;$('#content').setAttribute('aria-busy','false');
    chrome();
    if(render || !page) navigate();
    else if(!$('#modal').open && !$('#content').contains(document.activeElement) && page!=='settings') renderPage();
  } catch(error) {
    $('#offline').textContent='Cannot reach the local dashboard. Keep the dashboard terminal open, then refresh this page.';$('#offline').hidden=false;
    $('#bot-toggle').disabled=true;
    $('#last-updated').textContent='Connection lost · displayed data may be out of date';
    if(!state) $('#content').innerHTML=empty('Workspace unavailable','Start the local dashboard, then reload this page.');
  } finally {polling=false;if(refreshAgain){refreshAgain=false;refresh(true);}}
}
function chrome() {
  const r=state.runtime;
  const update=$('#update-notice');
  update.hidden=!r.restart_required;
  update.textContent=r.restart_required?(r.managed?
    'A coordinator update is ready. Use Stop bot, then Start bot to load the new conversation handling. Your meeting details stay saved.':
    'The bot running in another Terminal has an older coordinator loaded. Stop it there and start it again to use the updated conversation handling.') : '';
  $('#runtime-badge').outerHTML=`<span id="runtime-badge" class="badge ${statusInfo(r.state)[1]}">${esc(statusInfo(r.state)[0])}</span>`;
  const button=$('#bot-toggle');button.disabled=r.busy&&!r.managed;
  button.innerHTML=icon(r.managed?'stop':'play')+(r.managed?'Stop bot':r.busy?'Started in terminal':'Start bot');
  button.className='button '+(r.managed?'':'primary');
  $('#last-updated').textContent='Updated '+fmt(state.updated_at)+' · '+state.settings.COMPANY_TIMEZONE;
  const signature=page+':'+state.pending.length;
  if(navSignature!==signature){
  navSignature=signature;
  $('#navigation').innerHTML=Object.entries(pages).map(([key,[label]])=>`<a href="#${key}" ${page===key?'aria-current="page"':''}>${icon(key)}<span>${esc(label)}</span>${key==='members'&&state.pending.length?`<span class="nav-count">${state.pending.length}</span>`:''}</a>`).join('');
  }
  $('.footer>span').textContent='Meeting Bot · Telegram + '+state.settings.OPENAI_MODEL;
}
function navigate() {
  const previous=page,key=location.hash.slice(1) || 'overview';page=pages[key]?key:'overview';
  if(previous!==page)window.scrollTo(0,0);
  if(!state) return;
  $('#page-title').textContent=pages[page][0];$('#page-subtitle').textContent=pages[page][1];
  $('#breadcrumb').innerHTML=`Workspace <span>/</span> ${esc(pages[page][0])}`;
  document.title=pages[page][0]+' · Meeting Bot';chrome();renderPage();
}
function renderPage(){
  $('#content').innerHTML=({overview:overview,members:members,meetings:meetings,settings:settings,activity:activity}[page])();
  if(page==='members'){ $('#member-search').value=memberSearch;$('#member-team').value=memberTeam; }
  if(page==='meetings') $('#meeting-filter').value=meetingFilter;
}
function stat(label,n,foot,symbol){return `<div class="stat"><div class="stat-label">${esc(label)}${icon(symbol)}</div><div class="stat-number">${amount(n)}</div><div class="stat-foot">${foot}</div></div>`;}
function checklist(){
  const s=state.settings,checks=[
    [s.telegram_token_saved&&s.openai_key_saved,'Configure your connections','Add your Telegram token and OpenAI API key.','settings','Review settings'],
    [state.summary.enrolled>0,'Connect your team','Invite members and verify their Telegram accounts.','members',state.pending.length?`Review ${state.pending.length} pending`:'Manage members'],
    [state.runtime.state==='online','Bring your coordinator online','Start the bot and check its connection status.','activity','Check connections'],
    [state.summary.meetings>0,'Schedule your first meeting','Message your bot on Telegram to get started.','meetings','View meetings'],
  ];
  return `<div class="checklist">${checks.map(([done,title,text,link,cta],i)=>`<div class="check-row"><span class="step-icon ${done?'done':''}" aria-label="${done?'Complete':'Incomplete'}">${done?icon('check'):i+1}</span><div><h3>${title}</h3><p>${text}</p><a href="#${link}">${cta} &rarr;</a></div></div>`).join('')}</div>`;
}
function overview(){
  const s=state.settings,r=state.runtime,n=state.summary;
  const description={online:'Your coordinator is receiving Telegram updates. Employees can request meetings and respond to availability questions.',starting:'The backend is starting and checking its Telegram connection. Online status appears after the first successful poll.',reconnecting:'The backend is retrying its Telegram connection. Check this computer’s internet connection and review Activity.',stopped:'Start your coordinator to receive requests, ask members for availability, and send meeting confirmations.',failed:'The backend could not stay running. Review Activity for the latest error, or run connection checks to find the problem.',external:'A bot process is using this database. Manage it in the terminal where it was started.'}[r.state];
  return `<div class="stats">${stat('Team members',n.members,`<span class="green-text">${n.enrolled} connected</span> to Telegram`,'members')}${stat('Company teams',n.teams,'Your shared company directory','team')}${stat('Active meetings',n.active,`${n.meetings} total meeting requests`,'meetings')}${stat('AI calls today',n.calls_today,`of ${amount(s.MAX_AI_CALLS_PER_DAY)} daily limit · UTC`,'usage')}</div>
  <div class="dashboard-grid"><section class="card"><div class="card-header"><h2>Your coordinator</h2>${badge(r.state)}</div><div class="card-body"><div class="bot-card-top"><span class="bot-avatar">${icon('bot')}</span><div class="title-wrap"><div class="bot-name">Meeting Bot</div><span class="muted text-small">${s.TELEGRAM_BOT_USERNAME?'@'+esc(s.TELEGRAM_BOT_USERNAME):'Telegram username not set'}</span></div></div><p class="bot-status-description">${description}</p><dl class="connection-details"><div><dt>AI model</dt><dd>${esc(s.OPENAI_MODEL)}</dd></div><div><dt>Company time zone</dt><dd>${esc(s.COMPANY_TIMEZONE)}</dd></div></dl><div class="card-actions">${botLink('Open in Telegram') || '<a href="#settings" class="button">Set up Telegram</a>'}<a href="#settings" class="subtle-link">Configure bot &rarr;</a></div><p class="external-hint">${r.last_poll?'Last Telegram poll: '+fmt(r.last_poll):'An internet connection is required for Telegram and OpenAI.'}</p></div></section>
  <section class="card"><div class="card-header"><h2>Workspace setup</h2><span class="muted text-small">Getting started</span></div><div class="card-body">${checklist()}</div></section></div>
  <section class="card section-gap"><div class="card-header"><div><h2>Recent meeting requests</h2><p>Progress from the conversations happening on Telegram.</p></div><a class="text-link" href="#meetings">View all &rarr;</a></div>${meetingTable(state.meetings.slice(0,4))}</section>
  <section class="card"><div class="card-header"><h2>How a meeting comes together</h2><span class="muted text-small">Inside Telegram</span></div><div class="flow">${[['Send a request','Tell the bot who, when, and how long.'],['Collect availability','The bot asks each member privately.'],['Choose a time','The organizer picks a matching slot.'],['Everyone confirms','A final update goes to the whole group.']].map(([a,b],i)=>`<div class="flow-step"><span class="flow-number">0${i+1}</span><div><h3>${a}</h3><p>${b}</p></div></div>`).join('')}</div></section>`;
}
function botLink(label){const name=state.settings.TELEGRAM_BOT_USERNAME;return name&&/^[A-Za-z0-9_]{5,32}$/.test(name)?`<a class="button" href="https://t.me/${encodeURIComponent(name)}" target="_blank" rel="noopener noreferrer">${icon('external')}${esc(label)}</a>`:'';}
function members(){
  return `${state.pending.length?`<section class="card pending-card"><div class="card-header"><div><h2>Waiting for your approval</h2><p>Verify who owns each Telegram account before giving company access.</p></div>${badge('options',String(state.pending.length)+' pending')}</div>${state.pending.map(m=>`<div class="pending-item"><div><strong>${esc(m.name)}</strong><p>${esc(m.team)} · E-${m.employee_id} · Telegram ID <span class="mono">${m.telegram_id}</span><br>Claimed display name: ${esc(m.display_name)}</p></div><button class="button small" data-action="review" data-id="${m.employee_id}">Review enrollment</button></div>`).join('')}</section>`:''}
  <div class="toolbar"><div class="search">${icon('search')}<input id="member-search" type="search" placeholder="Search members or teams…" aria-label="Search members or teams"></div><select id="member-team" aria-label="Filter by team"><option value="">All teams</option>${[...new Set(state.members.map(m=>m.team))].sort().map(t=>`<option value="${esc(t)}" ${memberTeam===t?'selected':''}>${esc(t)}</option>`).join('')}</select><button class="button primary" data-action="add-member">${icon('plus')}Add member</button></div>
  <section class="card"><div class="card-header"><div><h2>Company directory</h2><p>${state.summary.members} members across ${state.summary.teams} teams</p></div></div><div id="member-results">${memberTable()}</div></section>
  <p class="activity-caption">Members join through a personal invitation link. Each link expires in one hour and requires your approval.</p>`;
}
function memberTable(){
  const pending=new Set(state.pending.map(m=>m.employee_id));
  const rows=state.members.filter(m=>(!memberTeam||m.team===memberTeam)&&`${m.name} ${m.team} E-${m.id}`.toLowerCase().includes(memberSearch.toLowerCase()));
  if(!rows.length)return empty(state.members.length?'No matching members':'Start with your team',state.members.length?'Try another name or team.':'Add your company members, then send each person an invitation to connect.','','members');
  return `<div class="table-wrap"><table><thead><tr><th scope="col">Member</th><th scope="col">Team</th><th scope="col">Permission</th><th scope="col">Telegram</th><th scope="col">Actions</th></tr></thead><tbody>${rows.map(m=>`<tr><td>${personCell(m)}</td><td>${esc(m.team)}<small>${esc(m.timezone)}</small></td><td>${m.can_request?'Requester':'Member'}</td><td>${badge(pending.has(m.id)?'confirming':m.telegram_id===null?'stopped':!m.opted_in?'opted_out':!m.reachable?'unreachable':'online',pending.has(m.id)?'Approval pending':m.telegram_id===null?'Not connected':m.opted_in&&m.reachable?'Connected':undefined)}</td><td><div class="row-actions"><button class="button small" data-action="edit-member" data-id="${m.id}">Edit</button>${pending.has(m.id)?`<button class="button small" data-action="review" data-id="${m.id}">Review</button>`:m.telegram_id===null?`<button class="button small" data-action="invite" data-id="${m.id}">Invite</button>`:''}</div></td></tr>`).join('')}</tbody></table></div>`;
}
function meetingTable(rows){
  if(!rows.length)return empty('No meeting requests yet','After enrollment, a requester can message the bot: “Arrange a 30-minute meeting with Engineering tomorrow afternoon.”',botLink('Open your bot'));
  return `<div class="table-wrap"><table><thead><tr><th scope="col">Meeting</th><th scope="col">Organizer</th><th scope="col">Attendees</th><th scope="col">Status</th><th scope="col">Details</th></tr></thead><tbody>${rows.map(m=>`<tr><td><strong>${esc(m.title)}</strong><small>M-${m.id} · ${m.duration} min · ${fmt(m.created,false)}</small></td><td>${esc(m.organizer)}</td><td>${m.attendees} people</td><td>${badge(m.state)}</td><td><button class="button small" data-action="meeting" data-id="${m.id}">View<span class="sr-only"> M-${m.id}</span></button></td></tr>`).join('')}</tbody></table></div>`;
}
function meetings(){
  const rows=state.meetings.filter(m=>!meetingFilter||m.state===meetingFilter);
  return `<div class="toolbar"><span class="muted text-small">Latest 100 requests · ${esc(state.settings.COMPANY_TIMEZONE)}</span><select id="meeting-filter" aria-label="Filter meetings by status"><option value="">All statuses</option>${['collecting','options','confirming','confirmed','needs_decision','cancelled'].map(s=>`<option value="${s}" ${meetingFilter===s?'selected':''}>${statusInfo(s)[0]}</option>`).join('')}</select></div><section class="card"><div class="card-header"><h2>Meeting requests</h2>${botLink('Request in Telegram')}</div><div id="meeting-results">${rows.length?meetingTable(rows):empty(meetingFilter?'No meetings with this status':'No meeting requests yet',meetingFilter?'Choose another status to see more requests.':'Enroll a requester and a teammate, start the bot, then send your first request on Telegram.',botLink('Open your bot'))}</div></section><div class="notice section-gap"><strong>Conversations happen in Telegram</strong>The dashboard shows progress. Organizers choose a time and members confirm it in their private chat with the bot. Calendar invitations are not connected yet.</div>`;
}
function input(key,label,value,hint='',type='text',extra=''){return `<div class="field"><label for="${key}">${label}</label><input id="${key}" name="${key}" type="${type}" value="${esc(value)}" ${extra} aria-describedby="${key}-hint ${key}-error"><p class="field-hint" id="${key}-hint">${hint}</p><span id="${key}-error" class="field-error"></span></div>`;}
function settings(){
  const s=state.settings,busy=state.runtime.busy;
  return `${busy?'<div class="notice warning"><strong>Stop the bot to change settings</strong>Your coordinator uses the configuration from its last start. Stop it using the control above, then save your changes.</div>':''}<div class="settings-layout"><form id="settings-form" class="settings-main" novalidate><div class="form-error" role="alert" tabindex="-1"></div><fieldset ${busy?'disabled':''}>
  <section class="card settings-card"><div class="card-header"><div><h2>Telegram connection</h2><p>Your company’s private meeting coordinator.</p></div>${icon('bot')}</div><div class="card-body">${input('TELEGRAM_BOT_USERNAME','Bot username',s.TELEGRAM_BOT_USERNAME,'From BotFather. Use the username, for example Meeteing_bot.','text','autocomplete="off" spellcheck="false"')}${input('TELEGRAM_BOT_TOKEN',`Bot token <span class="secret-state">${s.telegram_token_saved?'Saved securely on this computer':'Not configured'}</span>`,'','Leave blank to keep the saved token. Paste a replacement only when needed.','password','autocomplete="new-password" spellcheck="false"')}</div></section>
  <section class="card settings-card"><div class="card-header"><div><h2>AI connection</h2><p>The backend interprets messages using your OpenAI project.</p></div>${icon('usage')}</div><div class="card-body">${input('OPENAI_API_KEY',`OpenAI API key <span class="secret-state">${s.openai_key_saved?'Saved securely on this computer':'Not configured'}</span>`,'','Leave blank to keep the saved key. The dashboard never displays an existing credential.','password','autocomplete="new-password" spellcheck="false"')}<div class="form-grid">${input('OPENAI_MODEL','Model',s.OPENAI_MODEL,'Your requested model is gpt-6-luna.','text','spellcheck="false"')}${input('MAX_AI_CALLS_PER_DAY','Daily AI call limit',s.MAX_AI_CALLS_PER_DAY,'Resets at midnight UTC. Includes failed calls; this is not an account billing cap.','number','min="1" max="100000"')}</div></div></section>
  <section class="card settings-card"><div class="card-header"><div><h2>Company schedule</h2><p>Suggested times stay inside these working hours.</p></div>${icon('clock')}</div><div class="card-body">${input('COMPANY_TIMEZONE','Company time zone',s.COMPANY_TIMEZONE,'Use an IANA time zone, such as Asia/Dhaka. Members can have their own time zones.','text','list="timezones"')}<datalist id="timezones">${['Asia/Dhaka','Asia/Kolkata','Asia/Singapore','Asia/Dubai','Europe/London','America/New_York','America/Los_Angeles','UTC'].map(z=>`<option value="${z}">`).join('')}</datalist><div class="form-grid">${input('WORKDAY_START_HOUR','Workday starts',s.WORKDAY_START_HOUR,'Hour in 24-hour format. 9 means 09:00.','number','min="0" max="23"')}${input('WORKDAY_END_HOUR','Workday ends',s.WORKDAY_END_HOUR,'18 means 18:00. Use 24 for midnight.','number','min="1" max="24"')}</div><div class="field no-margin"><span class="field-label" id="days-label">Working days</span><div class="workdays" role="group" aria-labelledby="days-label">${['Mon','Tue','Wed','Thu','Fri','Sat','Sun'].map((d,i)=>`<label class="day-check"><input type="checkbox" name="workdays" value="${i}" ${s.WORKDAYS.split(',').includes(String(i))?'checked':''}>${d}</label>`).join('')}</div><p class="field-hint">These days apply to meeting suggestions. Reminders also respect each member’s local daytime hours.</p><span class="field-error" id="WORKDAYS-error"></span></div></div></section>
  <div class="settings-save"><p>Changes apply when you next start the bot.</p><button type="submit" class="button primary">Save settings</button></div></fieldset></form>
  <aside class="settings-note"><h2>A few things to know</h2><p>Credentials are stored in the local <span class="mono">.env</span> file with access limited to its owner. They are never returned to this page.</p><p>Keep this computer awake and connected to the internet while the bot is running.</p><p>The daily call limit controls this application’s AI usage. Telegram’s normal bot messages do not have an added messaging fee.</p><p>Database location<br><span class="mono">${esc(s.DATABASE_PATH)}</span></p><a class="text-link" href="#activity">Check your connections &rarr;</a></aside></div>`;
}
function connectionResults(){const c=state.checks;return c.running?'<div class="loading-state"><span class="spinner" aria-hidden="true"></span>Checking Telegram and OpenAI…</div>':c.rows.length?`<div class="checks-list">${c.rows.map(r=>`<div class="check-result"><strong>${esc(r.name)}</strong><p><span class="badge ${r.ok?'success':'error'}">${r.ok?'Passed':'Needs attention'}</span>${esc(r.detail)}</p></div>`).join('')}</div><p class="activity-caption">Checked ${fmt(c.checked_at)}. Model metadata checks do not prove that a generated response will succeed.</p>`:'<p class="activity-caption">Verify the bot identity, long-polling setup, and model access. These checks do not send Telegram messages or generate AI responses.</p>';}
function activity(){
  const queue=(title,rows,desc)=>`<section class="card"><div class="card-header"><h2>${title}</h2>${icon('activity')}</div><div class="card-body"><p class="muted text-small">${desc}</p><div class="queue-tags">${rows.length?rows.map(r=>`<span class="queue-tag">${esc(r.state)}<strong>${amount(r.count)}</strong></span>`).join(''):'<span class="muted text-small">No jobs recorded yet.</span>'}</div></div></section>`;
  return `<section class="card section-gap"><div class="card-header"><div><h2>Connection checks</h2><p>Check this computer’s access to your services.</p></div><button class="button small" data-action="check" ${state.checks.running?'disabled':''}>${icon('refresh')}${state.checks.running?'Checking…':'Run checks'}</button></div><div class="card-body">${connectionResults()}</div></section>
  <div class="queue-grid">${queue('Incoming messages',state.inbox,'Processing status of saved Telegram updates.')}${queue('Outgoing messages',state.outbox,'Delivery status of replies, invitations, and reminders.')}</div>
  ${state.failures.length||state.input_failures.length?`<section class="card section-gap"><div class="card-header"><div><h2>Needs operator review</h2><p>Uncertain deliveries may have arrived. Review before retrying.</p></div></div><div class="table-wrap"><table><thead><tr><th>Job</th><th>Reference</th><th>Status</th><th>Reason</th><th>Attempts</th></tr></thead><tbody>${state.failures.map(r=>`<tr><td>Outgoing #${r.id}</td><td>${r.meeting_id?'M-'+r.meeting_id:'—'} · ${r.employee_id?'E-'+r.employee_id:'—'}</td><td>${esc(r.status)}</td><td>${esc(r.error_type||'—')}</td><td>${r.attempts}</td></tr>`).join('')}${state.input_failures.map(r=>`<tr><td>Incoming #${r.update_id}</td><td>—</td><td>Failed</td><td>${esc(r.error_type)}</td><td>${r.attempts}</td></tr>`).join('')}</tbody></table></div></section>`:''}
  <section class="card section-gap"><div class="card-header"><div><h2>Recorded AI usage</h2><p>Actual API token counts saved by this application · dates in UTC</p></div>${icon('usage')}</div>${state.usage.length?`<div class="table-wrap"><table><thead><tr><th>Date / model</th><th>State</th><th>Calls</th><th>Input tokens</th><th>Output tokens</th></tr></thead><tbody>${state.usage.map(u=>`<tr><td>${esc(u.day)}<small>${esc(u.model)}</small></td><td>${esc(u.state)}</td><td>${amount(u.calls)}</td><td>${amount(u.input_tokens)}</td><td>${amount(u.output_tokens)}</td></tr>`).join('')}</tbody></table></div>`:empty('No AI usage recorded','Usage appears after the bot interprets its first message. Deterministic commands and connection checks do not generate model responses.','','usage')}</section>
  <section class="card"><div class="card-header"><div><h2>Bot events</h2><p>Recent events from the process started in this dashboard.</p></div>${state.runtime.managed?badge(state.runtime.state):''}</div><div class="card-body">${state.events.length?[...state.events].reverse().map(e=>`<div class="event"><time>${fmt(e.time)}</time><p>${esc(e.message)}</p></div>`).join(''):'<p class="muted text-small">Start the bot to see backend events here. Message contents and credentials are omitted.</p>'}</div></section>`;
}

function modal(title,body){$('#modal-content').innerHTML=`<div class="modal-header"><h2 id="modal-title">${esc(title)}</h2><button class="close-button" data-action="close" aria-label="Close dialog">${icon('close')}</button></div><div class="modal-body">${body}</div>`;$('#modal').showModal();}
function memberDialog(id){
  const m=state.members.find(m=>m.id===id)||{name:'',team:'',timezone:state.settings.COMPANY_TIMEZONE,can_request:0};
  modal(id?'Edit member':'Add a team member',`<p>${id?'Update this member’s directory details. Existing meeting attendees stay the same.':'Add a company record, then invite this person to connect their Telegram account.'}</p><form id="member-form" data-id="${id||''}" novalidate><div class="form-error" role="alert" tabindex="-1"></div>${input('name','Full name',m.name,'','text','required maxlength="120" autocomplete="name"')}${input('team','Team',m.team,'Use the same team name to group members.','text','required maxlength="80" list="team-names"')}<datalist id="team-names">${[...new Set(state.members.map(m=>m.team))].map(t=>`<option value="${esc(t)}">`).join('')}</datalist>${input('timezone','Time zone',m.timezone,'For example Asia/Dhaka or Europe/London.','text','required maxlength="80"')}<label class="check-field"><input type="checkbox" name="can_request" ${m.can_request?'checked':''}><span><strong>Can request meetings</strong><small>Allows company-wide team and member selection. All members can respond to invitations.</small></span></label><div class="modal-footer"><button class="button" type="button" data-action="close">Cancel</button><button class="button primary" type="submit">${id?'Save changes':'Add member'}</button></div></form>`);
  $('#name').focus();
}
function approvalDialog(id){
  const m=state.pending.find(m=>m.employee_id===id);if(!m)return;
  modal('Review enrollment',`<p>Confirm that this Telegram account belongs to the employee. A display name alone does not verify identity.</p><dl class="meeting-detail"><div><dt>Company member</dt><dd>${esc(m.name)} · E-${m.employee_id}</dd></div><div><dt>Team</dt><dd>${esc(m.team)}</dd></div><div><dt>Telegram ID</dt><dd class="mono">${m.telegram_id}</dd></div><div><dt>Claimed display name</dt><dd>${esc(m.display_name)}</dd></div></dl><form id="approval-form" data-id="${id}" data-telegram="${m.telegram_id}"><div class="form-error" role="alert" tabindex="-1"></div><label class="check-field"><input type="checkbox" name="verified" required><span>I independently verified that this Telegram account belongs to ${esc(m.name)}.</span></label><span id="verified-error" class="field-error"></span><div class="modal-footer"><button class="button" type="button" data-action="close">Cancel</button><button class="button primary" type="submit">Approve enrollment</button></div></form>`);
}
async function inviteDialog(id,button){
  const result=await action(button,()=>api(`/api/members/${id}/invite`,{}));if(!result)return;
  modal('Invite '+result.name,`<p>Share this personal link with ${esc(result.name)}. They must open it in Telegram and press Start while the bot is running.</p><label class="field-label" for="invite-url">Personal enrollment link</label><input id="invite-url" class="invite-link" readonly value="${esc(result.url)}"><p class="field-hint">Expires ${fmt(result.expires_at)} · single use. Creating a new link invalidates the previous one.</p><div class="notice warning"><strong>Approval is still required</strong>After they join, review the enrollment in Members and verify their Telegram account.</div><div class="modal-footer"><button class="button" data-action="close">Done</button><button class="button primary" data-action="copy-invite">Copy link</button></div>`);
}
async function meetingDialog(id,button){
  const result=await action(button,()=>api(`/api/meetings/${id}`));if(!result)return;
  const {meeting:m,attendees}=result;let proposal;
  try{proposal=JSON.parse(m.proposal);}catch{}
  modal(`M-${m.id} · ${m.title}`,`${badge(m.state)}<dl class="meeting-detail"><div><dt>Duration</dt><dd>${m.duration} minutes</dd></div><div><dt>Time zone</dt><dd>${esc(state.settings.COMPANY_TIMEZONE)}</dd></div><div><dt>Requested window starts</dt><dd>${fmt(m.start)}</dd></div><div><dt>Requested window ends</dt><dd>${fmt(m.end)}</dd></div>${proposal?`<div><dt>Proposed meeting</dt><dd>${fmt(proposal.start || proposal[0])}</dd></div><div><dt>Until</dt><dd>${fmt(proposal.end || proposal[1])}</dd></div>`:''}</dl><h3>Required attendees</h3>${attendees.map(a=>`<div class="attendee"><div>${esc(a.name)}<small class="muted"> · ${esc(a.team)}</small></div>${badge(a.status)}</div>`).join('')}<p class="activity-caption">Availability responses are separate from accepting the final time. Each required attendee must confirm the exact proposal on Telegram.</p><div class="modal-footer"><button class="button" data-action="close">Close</button></div>`);
}
async function action(button,fn){
  const old=button?.innerHTML;if(button){button.disabled=true;button.textContent='Working…';}
  try{return await fn();}catch(e){toast(e.message,true);}finally{if(button){button.innerHTML=old;button.disabled=false;}}
}
function formError(form,error){
  const summary=$('.form-error',form);summary.textContent=error.message;summary.focus();
  if(error.field){const field=form.elements[error.field];if(field instanceof HTMLElement)field.setAttribute('aria-invalid','true');const el=$('#'+error.field+'-error',form);if(el)el.textContent=error.message;}
}
async function submitForm(form,path,payload,close=true){
  const button=$('button[type=submit]',form),old=button.innerHTML;button.disabled=true;button.textContent='Saving…';
  $('.form-error',form).textContent='';form.querySelectorAll('.field-error').forEach(el=>el.textContent='');form.querySelectorAll('[aria-invalid]').forEach(el=>el.removeAttribute('aria-invalid'));
  try{const result=await api(path,payload);if(close)$('#modal').close();form.querySelectorAll('input[type=password]').forEach(el=>el.value='');toast(result.message);await refresh(true);}catch(error){formError(form,error);}finally{button.disabled=false;button.innerHTML=old;}
}

document.addEventListener('click',async event=>{
  const button=event.target.closest('[data-action]');if(!button)return;
  const id=Number(button.dataset.id);
  switch(button.dataset.action){
    case 'close':$('#modal').close();break;
    case 'add-member':memberDialog();break;
    case 'edit-member':memberDialog(id);break;
    case 'invite':await inviteDialog(id,button);break;
    case 'review':approvalDialog(id);break;
    case 'meeting':await meetingDialog(id,button);break;
    case 'copy-invite':
      try{await navigator.clipboard.writeText($('#invite-url').value);toast('Invitation link copied.');button.textContent='Copied';}
      catch{$('#invite-url').select();toast('Select and copy the invitation link.');}break;
    case 'check':{const result=await action(button,()=>api('/api/checks',{}));if(result){toast(result.message);await refresh(true);}break;}
  }
});
document.addEventListener('input',event=>{if(event.target.id==='member-search'){memberSearch=event.target.value;$('#member-results').innerHTML=memberTable();}});
document.addEventListener('change',event=>{
  if(event.target.id==='member-team'){memberTeam=event.target.value;$('#member-results').innerHTML=memberTable();}
  if(event.target.id==='meeting-filter'){meetingFilter=event.target.value;renderPage();$('#meeting-filter').focus();}
});
document.addEventListener('submit',async event=>{
  const form=event.target;event.preventDefault();
  if(form.id==='member-form'){
    const f=new FormData(form),id=form.dataset.id;
    await submitForm(form,id?`/api/members/${id}/edit`:'/api/members',{name:f.get('name'),team:f.get('team'),timezone:f.get('timezone'),can_request:f.has('can_request')});
  } else if(form.id==='approval-form'){
    await submitForm(form,`/api/members/${form.dataset.id}/approve`,{telegram_id:form.dataset.telegram,verified:form.elements.verified.checked});
  } else if(form.id==='settings-form'){
    const f=new FormData(form),payload=Object.fromEntries(f);payload.WORKDAYS=f.getAll('workdays').map(Number);delete payload.workdays;
    await submitForm(form,'/api/settings',payload,false);
  }
});
$('#bot-toggle').addEventListener('click',async event=>{
  const button=event.currentTarget,stop=state.runtime.managed;
  const result=await action(button,()=>api('/api/bot/'+(stop?'stop':'start'),{}));
  if(result){toast(result.message);await refresh(true);}
});
window.addEventListener('hashchange',()=>{navigate();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refresh();});
setInterval(()=>{if(!document.hidden)refresh();},4000);
refresh(true);
