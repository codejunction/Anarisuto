const $ = (sel, root = document) => root.querySelector(sel);
// Element builder. Children that are strings become text nodes, so nothing here parses HTML.
function el(tag, props = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (k === 'class') e.className = v; else if (k.startsWith('on')) e[k] = v;
    else if (v !== false && v != null) e.setAttribute(k, v === true ? '' : v);
  }
  e.append(...kids.filter(k => k != null));
  return e;
}

let token = localStorage.adminToken || '';
async function api(path, { method = 'GET', json, body } = {}) {
  const headers = { 'X-Admin-Token': token };
  if (json !== undefined) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(json); }
  const r = await fetch('/api' + path, { method, headers, body });
  if (!r.ok) {
    let msg = r.statusText;
    try { const d = (await r.json()).detail; msg = typeof d === 'string' ? d : JSON.stringify(d); } catch {}
    if (r.status === 403) msg = 'Admin token required. Enter it under Settings.';
    throw new Error(msg);
  }
  return r.json();
}
// Themed replacement for window.confirm. Resolves true when the person confirms.
function confirmDialog(title, text, ok = 'Delete') {
  const d = $('#confirm-dialog');
  $('#confirm-title').textContent = title; $('#confirm-text').textContent = text; $('#confirm-ok').textContent = ok;
  d.returnValue = '';
  d.showModal();
  return new Promise(resolve => d.addEventListener('close', () => resolve(d.returnValue === 'ok'), { once: true }));
}
let noticeTimer;
function notice(msg, bad = false) {
  const n = $('#notice');
  n.textContent = msg; n.className = bad ? 'bad' : ''; n.hidden = false;
  clearTimeout(noticeTimer); noticeTimer = setTimeout(() => n.hidden = true, bad ? 7000 : 2500);
}
const guard = fn => async (...a) => { try { return await fn(...a); } catch (e) { notice(e.message, true); } };
const when = ts => ts ? new Date(ts.replace(' ', 'T') + 'Z').toLocaleString(undefined, { day: 'numeric', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) : '';

// ---- routing ----------------------------------------------------------------------------
const views = { desk: loadDesk, assistants: loadAssistants, sources: loadSources, library: loadLibrary, documents: loadDocuments, settings: loadSettings };
function route() {
  const name = (location.hash.slice(2) || 'desk').split('/')[0];
  const view = views[name] ? name : 'desk';
  for (const v of document.querySelectorAll('.view')) v.hidden = v.id !== 'v-' + view;
  for (const a of document.querySelectorAll('.nav a')) a.toggleAttribute('aria-current', a.hash === '#/' + view);
  guard(() => views[view]())();
}
addEventListener('hashchange', route);

// ---- desk -------------------------------------------------------------------------------
let session = localStorage.session || (localStorage.session = crypto.randomUUID());
let meta = { assistants: [], mcp: [], documents: 0 };
let assistant = localStorage.assistant || 'default';
let busy = false, turnCount = 0;
const cap = s => s ? s[0].toUpperCase() + s.slice(1) : '';
const TYPES = { report: 'Full report', brief: 'Brief answer', table: 'Data table' };

function pickAssistant(name) {
  assistant = localStorage.assistant = name;
  $('#picked').textContent = name;
  $('#picker').open = false;
  for (const b of document.querySelectorAll('#picker-menu button')) b.setAttribute('aria-checked', String(b.dataset.name === name));
}
async function loadMeta() {
  meta = await api('/meta');
  if (!meta.assistants.some(a => a.name === assistant)) assistant = meta.assistants[0]?.name || 'default';
  $('#picker-menu').replaceChildren(
    ...meta.assistants.map(a => el('button', { type: 'button', role: 'menuitemradio', 'data-name': a.name, onclick: () => pickAssistant(a.name) },
      el('b', {}, a.name), el('i', {}, [a.domain, TYPES[a.type]].filter(Boolean).join(' · ')), a.description ? el('span', {}, a.description) : null)),
    el('a', { href: '#/assistants', onclick: () => $('#picker').open = false }, 'Create or edit assistants'));
  pickAssistant(assistant);
  $('#model').textContent = meta.llm;
}
document.addEventListener('click', e => { if (!e.target.closest('#picker')) $('#picker').open = false; });
async function loadConvos() {
  const list = await api('/sessions');
  $('#convos').replaceChildren(...list.map(s => el('li', {},
    el('a', { href: '#/desk', title: s.title, 'aria-current': s.id === session, onclick: () => openSession(s.id) }, s.title),
    el('button', { class: 'link danger', type: 'button', 'aria-label': 'Delete conversation', onclick: guard(async () => {
      await api('/sessions/' + s.id, { method: 'DELETE' });
      if (s.id === session) newSession(); else loadConvos();
    }) }, '×'))));
}
function welcome() {
  const ok = meta.mcp.filter(s => !s.error).length;
  return el('div', { class: 'welcome' },
    el('h1', {}, 'What do you need to know?'),
    el('p', {}, 'The desk reads your documents, queries your sources, and writes up what it finds. Every claim in the report points at the exhibit it came from.'),
    el('dl', {},
      el('div', {}, el('dt', {}, 'Sources connected'), el('dd', {}, String(ok))),
      el('div', {}, el('dt', {}, 'Document chunks'), el('dd', {}, String(meta.documents)))));
}
function newSession() { session = localStorage.session = crypto.randomUUID(); openSession(session); }
async function openSession(id) {
  session = localStorage.session = id;
  const turns = await api('/sessions/' + id);
  $('#turns').replaceChildren(...(turns.length ? [] : [welcome()]));
  for (const t of turns) finishTurn(startTurn(t.question, t.ts), t.report, t.evidence, t.steps);
  loadConvos();
  $('#turns').scrollTop = $('#turns').scrollHeight;
}

// A turn: the question and report on the left, the working notes (every stage, thought,
// tool call and result) on the right.
function startTurn(question, ts) {
  $('.welcome')?.remove();
  const t = { n: ++turnCount, calls: {}, main: el('div', { class: 'main' }), notes: el('div', { class: 'timeline' }) };
  t.aside = el('aside', { class: 'notes' }, el('h3', {}, 'Working notes'), t.notes);
  t.root = el('article', { class: 'turn' },
    el('header', {}, el('div', { class: 'dateline' }, ts ? when(ts) : 'Now'), el('h2', { class: 'question' }, question)),
    el('div', { class: 'memo' }, t.main, t.aside));
  $('#turns').append(t.root);
  return t;
}
const oneLine = v => Object.values(v || {}).map(x => typeof x === 'string' ? x : JSON.stringify(x)).join('  ');
const fold = (cls, label, preview, ...body) => el('details', { class: cls },
  el('summary', {}, el('b', {}, label), el('span', {}, preview)), ...body);
function addStep(t, s) {
  let node;
  if (s.kind === 'stage') { t.stage = s.name; node = el('div', { class: 'stage' }, el('b', {}, cap(s.name)), s.agent || ''); }
  else if (s.kind === 'thought') node = fold('thought', 'Thought', s.text.replace(/\s+/g, ' ').slice(0, 140), el('p', {}, s.text));
  else if (s.kind === 'say') node = el('p', { class: 'say' }, s.text);
  else if (s.kind === 'call') {
    node = t.calls[s.n] = fold('tool running', '···', '', el('pre', {}, JSON.stringify(s.input, null, 1)));
    node.querySelector('summary').append(el('code', {}, s.tool), el('span', { class: 'arg' }, oneLine(s.input)));
  } else if (s.kind === 'result') {
    const d = t.calls[s.n]; if (!d) return;
    d.classList.remove('running'); d.classList.toggle('refused', !s.id);
    d.querySelector('b').textContent = s.id || 'no';
    if (s.id) d.id = `x${t.n}-${s.id}`;
    d.append(el('pre', {}, s.output));
    return;
  }
  else if (s.kind === 'skill') node = el('p', { class: 'line' }, `Loaded the skill “${s.name}”.`);
  else if (s.kind === 'ask') node = el('p', { class: 'line' }, `Asked you: ${s.text}`);
  else if (s.kind === 'reply') node = el('p', { class: 'line' }, `You replied: ${s.text}`);
  else if (s.kind === 'output' && s.name !== 'report') node = fold('output', cap(s.name) + ' result', 'handed to the next stage', el('p', {}, s.text));
  if (node) { t.notes.append(node); t.aside.scrollTop = t.aside.scrollHeight; }
}
// Turn "[E1, E2]" in the rendered report into links to the matching exhibit.
function linkCitations(root, t) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const nodes = []; while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    if (node.parentElement.closest('pre, code') || !/\[[^\]]*E\d+[^\]]*\]/.test(node.data)) continue;
    const frag = document.createDocumentFragment();
    for (const part of node.data.split(/(\[[^\]]*E\d+[^\]]*\])/)) {
      if (!/^\[[^\]]*E\d+[^\]]*\]$/.test(part)) { frag.append(part); continue; }
      for (const id of part.match(/E\d+/g)) frag.append(el('a', { class: 'cite', role: 'button', tabindex: '0', onclick: () => showExhibit(t, id), onkeydown: ev => ev.key === 'Enter' && showExhibit(t, id) }, id));
    }
    node.replaceWith(frag);
  }
}
function showExhibit(t, id) {
  const d = $(`#x${t.n}-${id}`); if (!d) return;
  d.open = true; d.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  d.classList.add('lit'); setTimeout(() => d.classList.remove('lit'), 1400);
}
function stopClock(t) { clearInterval(t.clock); $('.progress', t.root)?.remove(); $('.working', t.root)?.remove(); }
function finishTurn(t, report, evidence = [], steps = []) {
  stopClock(t);
  if (!t.notes.children.length) {
    // a saved turn: replay its steps; turns saved before steps existed only have evidence
    if (!steps.length) steps = evidence.flatMap((e, i) => [{ kind: 'call', n: i, tool: e.tool, input: e.input }, { kind: 'result', n: i, id: e.id, output: e.output }]);
    steps.forEach(s => addStep(t, s));
  }
  if (!t.notes.children.length) t.aside.remove();
  const body = el('div', { class: 'report' });
  body.innerHTML = DOMPurify.sanitize(marked.parse(report || ''));
  linkCitations(body, t);
  t.main.append(body);
}
// The agent paused to ask something: show it with a reply box, send the reply back.
function askUser(t, e) {
  const input = el('input', { 'aria-label': 'Your reply', required: true, placeholder: 'Your reply' });
  const box = el('form', { class: 'ask-user' }, el('div', { class: 'dateline' }, 'The desk asks'), el('p', {}, e.text),
    el('div', { class: 'reply' }, input, el('button', { class: 'btn' }, 'Reply')));
  box.onsubmit = guard(async ev => {
    ev.preventDefault();
    const answer = input.value.trim();
    await api('/answer', { method: 'POST', json: { id: e.id, answer } });
    box.replaceChildren(el('div', { class: 'dateline' }, 'The desk asked'), el('p', {}, e.text), el('p', { class: 'replied' }, answer));
  });
  t.main.append(box); input.focus();
}
async function run(body, label) {
  if (busy) return;
  busy = true; $('#send').disabled = true;
  location.hash = '#/desk';
  const t = startTurn(label);
  const status = el('p', { class: 'progress' }), began = Date.now();
  const tick = () => { const s = Math.floor((Date.now() - began) / 1000); status.textContent = `${cap(t.stage) || 'Starting'} in progress · ${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`; };
  tick(); t.clock = setInterval(tick, 1000);
  t.main.append(status, el('div', { class: 'working' }));
  t.root.scrollIntoView({ behavior: 'smooth' });
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    if (!r.ok) throw new Error(await r.text());
    const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
    for (let buf = ''; ;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += value;
      const parts = buf.split('\n\n'); buf = parts.pop();
      for (const p of parts) {
        const e = JSON.parse(p.slice(6));
        if (e.type === 'step') addStep(t, e);
        else if (e.type === 'question') askUser(t, e);
        else if (e.type === 'done') finishTurn(t, e.report, e.evidence, e.steps);
        else if (e.type === 'error') throw new Error(e.message);
      }
    }
  } catch (err) {
    stopClock(t);
    t.main.append(el('p', { class: 'error failed' }, err.message));
  }
  busy = false; $('#send').disabled = false;
  guard(loadMeta)(); guard(loadConvos)();
}
$('#ask').onsubmit = e => {
  e.preventDefault();
  const question = $('#q').value.trim(); if (!question || busy) return;
  $('#q').value = '';
  run({ question, session, assistant }, question);
};
$('#q').onkeydown = e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); $('#ask').requestSubmit(); } };
$('#new-convo').onclick = () => { location.hash = '#/desk'; newSession(); };
let deskLoaded = false;
async function loadDesk() { await loadMeta(); if (!deskLoaded) { deskLoaded = true; await openSession(session); } }

// ---- assistants -------------------------------------------------------------------------
const aform = $('#assistant-form');
let editing = null;
const ticks = (box, names, chosen, empty) => box.replaceChildren(...(names.length
  ? names.map(n => el('label', { class: 'check' }, el('input', { type: 'checkbox', value: n, checked: chosen.includes(n) }), n))
  : [el('p', { class: 'hint' }, empty)]));
const ticked = box => [...box.querySelectorAll('input:checked')].map(i => i.value);
async function editAssistant(a) {
  editing = a?.name || null;
  a = a || { name: '', description: '', domain: '', type: 'report', prompt: '', mcp: [], docs: [] };
  aform.name.value = a.name; aform.name.readOnly = !!editing;
  aform.description.value = a.description; aform.domain.value = a.domain;
  aform.type.value = a.type; aform.prompt.value = a.prompt;
  $('#delete-assistant').hidden = $('#use-assistant').hidden = !editing;
  for (const l of document.querySelectorAll('#assistants a')) l.toggleAttribute('aria-current', l.dataset.name === editing);
  const [sources, docs] = await Promise.all([api('/mcp'), api('/documents')]);
  ticks($('#a-mcp'), sources.map(s => s.name), a.mcp, 'No sources connected yet.');
  ticks($('#a-docs'), docs.map(d => d.source), a.docs, 'No documents ingested yet.');
}
async function loadAssistants(select) {
  const list = await api('/assistants');
  $('#assistants').replaceChildren(...list.map(a => el('li', {},
    el('a', { href: '#/assistants', 'data-name': a.name, onclick: e => { e.preventDefault(); guard(editAssistant)(a); } },
      el('b', {}, a.name), el('span', {}, [a.domain, a.description].filter(Boolean).join(' · ') || ' ')))));
  await editAssistant(list.find(a => a.name === (select || editing)) || list[0]);
}
$('#new-assistant').onclick = () => guard(editAssistant)().then(() => aform.name.focus());
aform.onsubmit = guard(async e => {
  e.preventDefault();
  const name = aform.name.value.trim();
  await api('/assistants/' + encodeURIComponent(name), { method: 'PUT', json: {
    description: aform.description.value.trim(), domain: aform.domain.value.trim(), type: aform.type.value,
    prompt: aform.prompt.value, mcp: ticked($('#a-mcp')), docs: ticked($('#a-docs')) } });
  notice('Saved.'); await loadAssistants(name); loadMeta();
});
$('#delete-assistant').onclick = guard(async () => {
  if (!editing || !await confirmDialog(`Delete ${editing}?`, 'The assistant and its instructions are removed. Conversations that used it are kept.')) return;
  await api('/library/assistants/' + editing, { method: 'DELETE' });
  editing = null; await loadAssistants(); loadMeta();
});
$('#use-assistant').onclick = () => { pickAssistant(editing); location.hash = '#/desk'; };

// ---- sources (MCP) ----------------------------------------------------------------------
const PRESETS = {
  'Blank': {},
  'MongoDB (example)': { name: 'mongo', command: 'npx', args: ['-y', 'mongodb-mcp-server', '--readOnly'], env: { MDB_MCP_CONNECTION_STRING: '${MONGO_URI}' } },
  'Confluence (example)': { name: 'confluence', command: 'uvx', args: ['mcp-atlassian'], env: { CONFLUENCE_URL: '${CONFLUENCE_URL}', CONFLUENCE_USERNAME: '${CONFLUENCE_USER}', CONFLUENCE_API_TOKEN: '${CONFLUENCE_TOKEN}' } },
  'Remote server': { url: 'https://', transport: 'streamable-http', headers: { Authorization: 'Bearer ${TOKEN}' } },
};
const pairs = o => Object.entries(o || {}).map(([k, v]) => `${k}=${v}`).join('\n');
const unpairs = s => Object.fromEntries(s.split('\n').map(l => l.trim()).filter(Boolean).map(l => { const i = l.indexOf('='); return [l.slice(0, i).trim(), l.slice(i + 1).trim()]; }).filter(([k]) => k));
const form = $('#source-form');
function setMode() {
  const mode = form.mode.value;
  for (const d of form.querySelectorAll('[data-mode]')) d.hidden = d.dataset.mode !== mode;
}
function fillSource(name, c, editing) {
  form.reset();
  form.name.value = name || ''; form.name.readOnly = editing;
  form.mode.value = c.url ? 'remote' : 'stdio';
  form.command.value = c.command || ''; form.args.value = (c.args || []).join('\n'); form.env.value = pairs(c.env);
  form.url.value = c.url || ''; form.transport.value = c.transport || 'streamable-http'; form.headers.value = pairs(c.headers);
  form.tools.value = (c.tools || []).join(', '); form.allow_writes.checked = !!c.allow_writes;
  $('#source-error').hidden = true; setMode();
}
function openSource(name, config) {
  $('#source-title').textContent = name ? `Edit ${name}` : 'Add source';
  $('#preset-row').hidden = !!name;
  fillSource(name, config || {}, !!name);
  $('#source-dialog').showModal();
}
$('#preset').replaceChildren(...Object.keys(PRESETS).map(k => new Option(k)));
$('#preset').onchange = () => { const p = PRESETS[$('#preset').value]; fillSource(p.name, p, false); };
form.onchange = e => e.target.name === 'mode' && setMode();
$('#source-cancel').onclick = () => $('#source-dialog').close();
$('#add-source').onclick = () => { $('#preset').value = 'Blank'; openSource(); };
form.onsubmit = async e => {
  e.preventDefault();
  const c = form.mode.value === 'stdio'
    ? { command: form.command.value.trim(), args: form.args.value.split('\n').map(s => s.trim()).filter(Boolean), env: unpairs(form.env.value) }
    : { url: form.url.value.trim(), transport: form.transport.value, headers: unpairs(form.headers.value) };
  const tools = form.tools.value.split(',').map(s => s.trim()).filter(Boolean);
  if (tools.length) c.tools = tools;
  if (form.allow_writes.checked) c.allow_writes = true;
  const save = $('#source-save'); save.disabled = true; save.textContent = 'Connecting…';
  try {
    const r = await api('/mcp/' + encodeURIComponent(form.name.value.trim()), { method: 'PUT', json: c });
    $('#source-dialog').close();
    notice(r.error ? 'Saved, but it did not connect.' : `Connected, ${r.tools} tools.`, !!r.error);
    loadSources();
  } catch (err) { $('#source-error').textContent = err.message; $('#source-error').hidden = false; }
  save.disabled = false; save.textContent = 'Save and connect';
};
async function loadSources() {
  const list = await api('/mcp');
  const rows = list.map(s => el('tr', {},
    el('td', {}, el('span', { class: 'status' + (s.error ? ' down' : ''), title: s.error ? 'Not connected' : 'Connected' }), el('span', { class: 'name' }, s.name)),
    el('td', { class: 'detail' }, s.config.url || [s.config.command, ...(s.config.args || [])].join(' '), s.error ? el('div', { class: 'error' }, s.error) : null),
    el('td', { class: 'detail' }, s.error ? 'none' : `${s.tools.length}: ${s.tools.join(', ')}`),
    el('td', {},
      s.error ? null : el('button', { class: 'link', type: 'button', onclick: () => run({ catalog: s.name, session }, `Catalog the source “${s.name}”`) }, 'Catalog'),
      el('button', { class: 'link', type: 'button', onclick: guard(async () => { notice('Reconnecting…'); await api(`/mcp/${s.name}/reconnect`, { method: 'POST' }); loadSources(); }) }, 'Reconnect'),
      s.from_plugin ? null : el('button', { class: 'link', type: 'button', onclick: () => openSource(s.name, s.config) }, 'Edit'),
      s.from_plugin ? null : el('button', { class: 'link danger', type: 'button', onclick: guard(async () => { if (await confirmDialog(`Remove ${s.name}?`, 'The researcher can no longer query this source. Its catalog notes stay in Documents.', 'Remove')) { await api('/mcp/' + s.name, { method: 'DELETE' }); loadSources(); } }) }, 'Remove'))));
  $('#sources tbody').replaceChildren(...(rows.length ? rows : [el('tr', { class: 'empty' }, el('td', { colspan: '4' }, 'No sources yet. Add a database, Confluence, Power BI or any other MCP server.'))]));
}

// ---- library ----------------------------------------------------------------------------
const KINDS = {
  agents: { label: 'Sub-agents', blurb: 'Specialists the researcher can hand work to.',
    template: '---\nrole: \ngoal: \n# mcp: [source-a]\n# llm: \n---\nYou are \n' },
  rules: { label: 'Rules', blurb: 'Always-on instructions given to every agent on every question.',
    template: 'State the time period and filters behind every number.\n' },
  skills: { label: 'Skills', blurb: 'Step-by-step instructions the researcher loads only when a question calls for them.',
    template: '---\ndescription: When this skill applies\n---\n1. \n' },
  plugins: { label: 'Plugins', blurb: 'Python that runs inside the harness with its full permissions: tools, file loaders, extra sources.',
    template: 'def my_tool(text: str) -> str:\n    """Say what the tool does and when to use it."""\n    return text\n\n\ndef register(harness):\n    harness.tools.append(my_tool)\n' },
};
let kind = 'agents', current = null;
function editItem(name, content, error) {
  current = name;
  $('#item-name').value = name || ''; $('#item-name').readOnly = !!name;
  $('#item-body').value = content;
  $('#delete-item').hidden = !name;
  $('#item-error').textContent = error || ''; $('#item-error').hidden = !error;
  for (const a of document.querySelectorAll('#items a')) a.toggleAttribute('aria-current', a.dataset.name === name);
}
async function loadLibrary(select) {
  $('#kind-blurb').textContent = KINDS[kind].blurb;
  $('#kinds').replaceChildren(...Object.entries(KINDS).map(([k, v]) =>
    el('button', { type: 'button', role: 'tab', 'aria-selected': String(k === kind), onclick: () => { kind = k; guard(loadLibrary)(); } }, v.label)));
  const items = await api('/library/' + kind);
  const open = guard(async it => editItem(it.name, (await api(`/library/${kind}/${it.name}`)).content, it.error));
  $('#items').replaceChildren(...items.map(it => el('li', {},
    el('a', { href: '#/library', 'data-name': it.name, class: it.error ? 'broken' : '', onclick: e => { e.preventDefault(); open(it); } },
      el('b', {}, it.name), el('span', {}, it.error ? 'failed to load' : it.description || ' ')))));
  const first = items.find(i => i.name === select) || items[0];
  if (first) await open(first); else editItem(null, KINDS[kind].template);
}
$('#new-item').onclick = () => { editItem(null, KINDS[kind].template); $('#item-name').focus(); };
$('#editor').onsubmit = guard(async e => {
  e.preventDefault();
  const name = $('#item-name').value.trim();
  const r = await api(`/library/${kind}/${name}`, { method: 'PUT', json: { content: $('#item-body').value } });
  notice(r.error ? 'Saved, but the plugin failed to load.' : 'Saved.', !!r.error);
  await loadLibrary(name); loadMeta();
});
$('#delete-item').onclick = guard(async () => {
  if (!current || !await confirmDialog(`Delete ${current}?`, `This ${KINDS[kind].label.toLowerCase().replace(/s$/, '')} is removed from the harness.`)) return;
  await api(`/library/${kind}/${current}`, { method: 'DELETE' });
  await loadLibrary(); loadMeta();
});
$('#item-body').onkeydown = e => {
  if (e.key !== 'Tab' || e.shiftKey) return;
  e.preventDefault(); e.target.setRangeText('    ', e.target.selectionStart, e.target.selectionEnd, 'end');
};

// ---- documents --------------------------------------------------------------------------
async function loadDocuments() {
  const docs = await api('/documents');
  const rows = docs.map(d => el('tr', {},
    el('td', { class: 'name' }, d.source), el('td', {}, d.kind), el('td', { class: 'num' }, String(d.chunks)),
    el('td', {}, el('button', { class: 'link danger', type: 'button', onclick: guard(async () => {
      await api('/documents?source=' + encodeURIComponent(d.source), { method: 'DELETE' }); loadDocuments();
    }) }, 'Remove'))));
  $('#documents tbody').replaceChildren(...(rows.length ? rows : [el('tr', { class: 'empty' }, el('td', { colspan: '4' }, 'Nothing ingested yet. Add data dictionaries, runbooks, report definitions, or catalog a source.'))]));
}
const upload = guard(async files => {
  if (!files.length) return;
  const body = new FormData(); for (const f of files) body.append('files', f);
  notice(`Reading ${files.length} file${files.length > 1 ? 's' : ''}…`);
  const done = await api('/documents/upload', { method: 'POST', body });
  const n = Object.keys(done).length;
  notice(n ? `Added ${n} document${n > 1 ? 's' : ''}.` : 'Nothing readable in those files.', !n);
  loadDocuments();
});
$('#files').onchange = e => { upload([...e.target.files]); e.target.value = ''; };
const drop = $('#drop');
drop.ondragover = e => { e.preventDefault(); drop.classList.add('over'); };
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => { e.preventDefault(); drop.classList.remove('over'); upload([...e.dataTransfer.files]); };
$('#url-form').onsubmit = guard(async e => {
  e.preventDefault();
  notice('Fetching…');
  const r = await api('/documents/url', { method: 'POST', json: { url: $('#url').value } });
  notice(`Added, ${r.chunks} chunks.`); $('#url').value = ''; loadDocuments();
});

// ---- settings ---------------------------------------------------------------------------
async function loadSettings() {
  await loadMeta();
  $('#llm').value = meta.llm; $('#llm-base').value = meta.llm_base_url; $('#token').value = token;
}
$('#settings').onsubmit = guard(async e => {
  e.preventDefault();
  await api('/settings', { method: 'PUT', json: { llm: $('#llm').value, llm_base_url: $('#llm-base').value } });
  notice('Saved.'); loadMeta();
});
$('#token-form').onsubmit = e => { e.preventDefault(); token = localStorage.adminToken = $('#token').value; notice('Remembered in this browser.'); };

// ---- theme: light, dark, or follow the system (default) ---------------------------------
function setTheme(t) {
  if (t === 'light' || t === 'dark') document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  try { localStorage.theme = t; } catch {}
  for (const b of document.querySelectorAll('.theme button')) b.setAttribute('aria-pressed', String(b.dataset.theme === t));
}
for (const b of document.querySelectorAll('.theme button')) b.onclick = () => setTheme(b.dataset.theme);
let savedTheme = 'system';
try { savedTheme = localStorage.theme || 'system'; } catch {}
setTheme(savedTheme);

guard(loadMeta)().then(route);
guard(loadConvos)();
