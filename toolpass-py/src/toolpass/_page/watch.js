'use strict';
// The toolpass page: every tool call, who made it, and what the tool did in
// each system it reached. Read-only; text is only ever set with textContent.

const $ = (id) => document.getElementById(id);
const SVG = 'http://www.w3.org/2000/svg';
const PAGE = 60;           // calls shown before "Show more"
const KEEP = 5000;         // calls kept in memory

const state = {
  kind: 'toolkit',
  next: 0,
  calls: new Map(),        // key -> normalized call
  open: new Set(),         // keys of opened calls
  person: '',              // '' is everyone
  outcome: 'all',          // all | ok | refused
  query: '',
  shown: PAGE,
  focus: null,             // a map node the feed is narrowed to: {col, id}
  dirty: true,
};

// -- small helpers ----------------------------------------------------------

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined && text !== null) e.textContent = String(text);
  return e;
}
function svg(tag, attrs, text) {
  const e = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, String(v));
  if (text !== undefined) e.textContent = String(text);
  return e;
}
function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
function hue(s) { let h = 0; for (const c of s) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function listWords(xs) {
  if (xs.length <= 1) return xs.join('');
  return xs.slice(0, -1).join(', ') + ' and ' + xs[xs.length - 1];
}

const SYSTEMS = {
  github: ['GitHub', 'GH'], gitlab: ['GitLab', 'GL'], bitbucket: ['Bitbucket', 'BB'], jira: ['Jira', 'J'],
  confluence: ['Confluence', 'C'], slack: ['Slack', 'S'], linear: ['Linear', 'L'], kubernetes: ['Kubernetes', 'K8'],
  argocd: ['Argo CD', 'A'], aws: ['AWS', 'AWS'], azure: ['Azure', 'Az'], gcp: ['Google Cloud', 'GC'],
  datadog: ['Datadog', 'DD'], pagerduty: ['PagerDuty', 'PD'], vault: ['Vault', 'V'], snowflake: ['Snowflake', 'SF'],
  zendesk: ['Zendesk', 'Z'], microsoft365: ['Microsoft 365', 'M'], databricks: ['Databricks', 'DB'],
};
const COLORS = {
  github: '#3d444d', gitlab: '#e2432a', jira: '#1868db', confluence: '#1868db', slack: '#611f69', linear: '#5e6ad2',
  kubernetes: '#326ce5', argocd: '#ef7b4d', aws: '#232f3e', azure: '#0078d4', gcp: '#4285f4', datadog: '#632ca6',
  pagerduty: '#06ac38', vault: '#3d444d', snowflake: '#29b5e8', zendesk: '#03363d',
};
function systemName(s) { return (SYSTEMS[s] || [s ? s[0].toUpperCase() + s.slice(1) : 'Unknown'])[0]; }
function mark(s) {
  const m = el('span', 'mark ' + (s || ''), (SYSTEMS[s] || [null, (s || '?').slice(0, 2).toUpperCase()])[1]);
  m.title = systemName(s);
  return m;
}

function personName(email) {
  if (!email) return '';
  const local = email.split('@')[0].split(/[._+-]/)[0];
  return local ? local[0].toUpperCase() + local.slice(1) : email;
}
function avatar(email) {
  const a = el('span', 'avatar', personName(email).slice(0, 1) || '?');
  a.style.setProperty('--h', String(hue(email || '?')));
  a.title = email || '';
  return a;
}

function ago(iso) {
  if (!iso) return '';
  const t = Date.parse(iso);
  if (isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return Math.round(s / 60) + ' min ago';
  if (s < 86400) return Math.round(s / 3600) + ' h ago';
  const d = new Date(t);
  return d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
}
function took(ms) {
  if (ms === null || ms === undefined) return '';
  return ms < 1000 ? Math.round(ms) + ' ms' : (ms / 1000).toFixed(1) + ' s';
}

// -- what a request did, in words ---------------------------------------------

const VERBS = {
  'github:pr.create': 'Open a pull request on', 'github:pr.merge': 'Merge a pull request on', 'github:repo.read': 'Read',
  'github:repo.push': 'Push to', 'github:repo.admin': 'Change settings of', 'github:repo.delete': 'Delete',
  'github:issue.create': 'Open an issue on', 'github:issue.comment': 'Comment on',
  'jira:BROWSE_PROJECTS': 'Read', 'jira:ADD_COMMENTS': 'Comment on', 'jira:TRANSITION_ISSUES': 'Move',
  'jira:CREATE_ISSUES': 'Create an issue in', 'jira:EDIT_ISSUES': 'Edit', 'jira:DELETE_ISSUES': 'Delete',
  'jira:ASSIGN_ISSUES': 'Assign', 'jira:LINK_ISSUES': 'Link', 'jira:WORK_ON_ISSUES': 'Log work on',
  'jira:MANAGE_WATCHERS': 'Change watchers of', 'jira:EDIT_ALL_COMMENTS': 'Edit a comment on', 'jira:DELETE_ALL_COMMENTS': 'Delete a comment on',
  'slack:message.post': 'Post in', 'slack:channel.read': 'Read', 'slack:channel.join': 'Join', 'slack:channel.manage': 'Manage',
  'linear:team.member': 'Create in', 'linear:issue.edit': 'Edit', 'linear:issue.view': 'Read', 'linear:team.view': 'Read',
};
const K8S_VERBS = {get: 'Read', list: 'List', watch: 'Watch', create: 'Create', update: 'Replace', patch: 'Change', delete: 'Delete', deletecollection: 'Delete every'};

function objectOf(resource) {
  if (!resource) return '';
  const m = /^namespace:([^?]+)\?(.*)$/.exec(resource) || /^(cluster)\?(.*)$/.exec(resource);
  if (m) {
    const q = new URLSearchParams(m[2]);
    const kind = (q.get('resource') || '').split('.')[0].replace(/s$/, '');
    const name = q.get('name');
    const sub = q.get('subresource');
    const where = m[1] === 'cluster' ? '' : ' in ' + m[1];
    return (name ? kind + ' ' + name : kind + 's') + (sub ? ' (' + sub + ')' : '') + where;
  }
  const i = resource.indexOf(':');
  if (i < 0) return resource;
  const type = resource.slice(0, i), id = resource.slice(i + 1);
  const short = /^[0-9a-f]{8}-[0-9a-f-]{27}$/.test(id) ? id.slice(0, 8) : id;
  if (type === 'repo' || type === 'issue') return short;
  if (type === 'channel') return '#' + short;
  return type + ' ' + short;
}

function describe(a) {
  // [verb, object] for one request the tool made.
  const action = a.action || '';
  if (!action) return [a.note === 'unknown_host' ? 'Request to an unknown host' : 'Request, nothing to check', ''];
  const k8s = /^kubernetes:raw:([a-z]+):/.exec(action);
  if (k8s) return [K8S_VERBS[k8s[1]] || k8s[1], objectOf(a.resource)];
  if (VERBS[action]) return [VERBS[action], objectOf(a.resource)];
  const words = action.split(':').pop().replace(/[._]/g, ' ').toLowerCase();
  return [words[0].toUpperCase() + words.slice(1), objectOf(a.resource)];
}

function shortReason(r) {
  if (!r) return '';
  // "not_authorized: dana may not X on Y in conn: denied: <why>" -> "<why>"
  const i = r.lastIndexOf('denied: ');
  return i >= 0 ? r.slice(i + 8) : r.replace(/^[a-z_]+: /, '');
}

// -- the data ------------------------------------------------------------------

function normalize(raw) {
  // One run of a toolpass tool: the live page of one toolkit, or the dashboard over every server's runs.
  const outcome = raw.outcome === 'ran' ? 'ok' : raw.outcome === 'refused' ? 'refused' : 'error';
  return {
    key: 's' + raw.seq, time: raw.time, server: raw.server || '', tool: raw.tool, user: raw.user || '',
    arguments: raw.arguments, result: '', duration: raw.duration_ms, outcome, steps: raw.actions || [],
    reason: raw.reason || '', code: raw.code || '',
  };
}

function matches(c) {
  if (state.person && c.user !== state.person) return false;
  if (state.outcome === 'ok' && c.outcome !== 'ok') return false;
  if (state.outcome === 'refused' && c.outcome !== 'refused') return false;
  if (state.focus) {
    const f = state.focus;
    if (f.col === 0 && c.server !== f.id) return false;
    if (f.col === 1 && (c.server + '\u0000' + c.tool) !== f.id) return false;
    if (f.col === 2 && !c.steps.some((s) => (s.integration || '') === f.id)) return false;
  }
  if (state.query) {
    const hay = [c.tool, c.server, c.user, JSON.stringify(c.arguments || {}), c.reason,
      ...c.steps.map((s) => [s.action, s.resource, s.integration, systemName(s.integration), s.path].join(' '))].join(' ').toLowerCase();
    if (!hay.includes(state.query)) return false;
  }
  return true;
}

function sorted() { return [...state.calls.values()].sort((a, b) => (a.time < b.time ? 1 : a.time > b.time ? -1 : 0)); }

// -- numbers -------------------------------------------------------------------

function renderStats(all) {
  const ok = all.filter((c) => c.outcome === 'ok').length;
  const refused = all.filter((c) => c.outcome === 'refused');
  const systems = new Map();
  let requests = 0;
  for (const c of all) for (const s of c.steps) { requests++; if (s.integration) systems.set(s.integration, (systems.get(s.integration) || 0) + 1); }
  const people = new Set(all.map((c) => c.user).filter(Boolean));
  $('n-calls').textContent = all.length;
  $('h-calls').textContent = people.size ? 'by ' + plural(people.size, 'person', 'people') : ' ';
  $('n-ok').textContent = ok;
  $('h-ok').textContent = requests ? plural(requests, 'request') + ' checked' : ' ';
  $('n-refused').textContent = refused.length;
  const stoppedAt = new Map();
  for (const c of refused) for (const s of c.steps) if (s.outcome === 'refused') stoppedAt.set(s.integration, (stoppedAt.get(s.integration) || 0) + 1);
  $('h-refused').textContent = stoppedAt.size ? 'stopped at ' + listWords([...stoppedAt.keys()].map(systemName)) : (refused.length ? 'before the tool ran' : 'nothing refused');
  $('n-systems').textContent = systems.size;
  $('h-systems').textContent = systems.size ? listWords([...systems.keys()].map(systemName)) : 'none yet';
  for (const id of ['stat-calls', 'stat-ok', 'stat-refused']) $(id).classList.toggle('selected', $(id).dataset.outcome === state.outcome && state.outcome !== 'all');
}

// -- the map: server -> tool -> system ---------------------------------------

function renderMap(visible) {
  const panel = $('map-panel'), box = $('map');
  const cols = [new Map(), new Map(), new Map()];
  const links = new Map();
  const bump = (m, id, label, refused, extra) => {
    const n = m.get(id) || Object.assign({id, label, count: 0, refused: 0}, extra || {});
    n.count++; if (refused) n.refused++;
    m.set(id, n);
  };
  const link = (from, to, refused) => {
    const k = from + '\u0001' + to;
    const l = links.get(k) || {from, to, count: 0, refused: 0};
    l.count++; if (refused) l.refused++;
    links.set(k, l);
  };
  const servers = new Set(visible.map((c) => c.server).filter(Boolean));
  const byServer = servers.size > 0;
  for (const c of visible) {
    const toolId = c.server + '\u0000' + c.tool;
    if (byServer) { bump(cols[0], c.server, c.server, c.outcome === 'refused'); link('0:' + c.server, '1:' + toolId, c.outcome === 'refused'); }
    bump(cols[1], toolId, c.tool, c.outcome === 'refused');
    for (const s of c.steps) {
      if (!s.integration) continue;
      const bad = s.outcome === 'refused';
      bump(cols[2], s.integration, systemName(s.integration), bad);
      link('1:' + toolId, '2:' + s.integration, bad);
    }
  }
  panel.hidden = cols[2].size === 0 && !(byServer && cols[0].size);
  if (panel.hidden) return;
  $('map-sub').textContent = byServer ? 'Server, then tool, then the systems each tool reached' : 'Tool, then the systems it reached';

  const shown = byServer ? [0, 1, 2] : [1, 2];
  const width = Math.max(box.clientWidth, 640);
  const nodeW = Math.min(230, Math.floor((width - 40) / (shown.length * 1.6)));
  const rowH = 46, top = 28;
  const maxRows = Math.max(...shown.map((i) => cols[i].size), 1);
  const height = top + maxRows * rowH + 6;
  const gap = (width - nodeW * shown.length) / (shown.length - 1);
  const xOf = (i) => shown.indexOf(i) * (nodeW + gap);
  const pos = new Map();
  const titles = ['Servers', 'Tools', 'Systems'];
  const root = svg('svg', {width, height, viewBox: `0 0 ${width} ${height}`, role: 'img', 'aria-label': 'Where the calls went'});
  const linkLayer = svg('g'), nodeLayer = svg('g');
  root.appendChild(linkLayer); root.appendChild(nodeLayer);
  for (const i of shown) {
    root.appendChild(svg('text', {x: xOf(i), y: 12, class: 'col-title'}, titles[i]));
    const nodes = [...cols[i].values()].sort((a, b) => b.count - a.count);
    const offset = (maxRows - nodes.length) * rowH / 2;
    nodes.forEach((n, j) => pos.set(i + ':' + n.id, {x: xOf(i), y: top + offset + j * rowH, n, col: i}));
  }
  const maxLink = Math.max(...[...links.values()].map((l) => l.count), 1);
  const linkEls = [];
  for (const l of links.values()) {
    const a = pos.get(l.from), b = pos.get(l.to);
    if (!a || !b) continue;
    const x1 = a.x + nodeW, y1 = a.y + 18, x2 = b.x, y2 = b.y + 18, mx = (x1 + x2) / 2;
    const p = svg('path', {d: `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`, class: 'link' + (l.refused ? ' bad' : ''),
      'stroke-width': (1.5 + 5 * Math.sqrt(l.count / maxLink)).toFixed(1)});
    const t = svg('title', {}, l.count + (l.refused ? ', ' + l.refused + ' refused' : ''));
    p.appendChild(t);
    p.dataset.from = l.from; p.dataset.to = l.to;
    linkLayer.appendChild(p); linkEls.push(p);
  }
  const nodeEls = new Map();
  for (const [id, {x, y, n, col}] of pos) {
    const g = svg('g', {class: 'node', transform: `translate(${x},${y})`, tabindex: 0});
    g.appendChild(svg('rect', {class: 'box', width: nodeW, height: 36, rx: 9}));
    let tx = 12;
    if (col === 2) {
      const m = SYSTEMS[n.id] || [null, n.id.slice(0, 2).toUpperCase()];
      g.appendChild(svg('rect', {x: 8, y: 8, width: 20, height: 20, rx: 5, fill: COLORS[n.id] || '#6b6b75'}));
      const mk = svg('text', {x: 18, y: 22, 'text-anchor': 'middle', 'font-size': 9, 'font-weight': 700, fill: '#fff'}, m[1]);
      g.appendChild(mk);
      tx = 36;
    }
    const label = n.label.length > 22 ? n.label.slice(0, 21) + '…' : n.label;
    g.appendChild(svg('text', {x: tx, y: 16, class: col === 1 ? 'mono' : ''}, label));
    const unit = col === 2 ? 'request' : 'call';
    g.appendChild(svg('text', {x: tx, y: 29, class: 'meta' + (n.refused ? ' bad' : '')}, plural(n.count, unit) + (n.refused ? ' · ' + n.refused + ' refused' : '')));
    const tip = svg('title', {}, n.label); g.appendChild(tip);
    g.addEventListener('mouseenter', () => light(id));
    g.addEventListener('mouseleave', () => light(null));
    g.addEventListener('click', () => {
      const f = {col, id: n.id};
      state.focus = state.focus && state.focus.col === col && state.focus.id === n.id ? null : f;
      state.shown = PAGE; render();
    });
    nodeLayer.appendChild(g); nodeEls.set(id, g);
  }
  function light(id) {
    const target = id || (state.focus ? state.focus.col + ':' + state.focus.id : null);
    box.classList.toggle('dim', !!target);
    if (!target) return;
    // Everything connected to the node, upstream and downstream.
    const lit = new Set([target]);
    let grew = true;
    const col = Number(target.split(':')[0]);
    while (grew) {
      grew = false;
      for (const p of linkEls) {
        const f = p.dataset.from, t = p.dataset.to;
        const fc = Number(f.split(':')[0]);
        if (lit.has(f) && fc >= col && !lit.has(t)) { lit.add(t); grew = true; }
        if (lit.has(t) && fc < col && !lit.has(f)) { lit.add(f); grew = true; }
      }
    }
    for (const [nid, g] of nodeEls) g.classList.toggle('lit', lit.has(nid));
    for (const p of linkEls) p.classList.toggle('lit', lit.has(p.dataset.from) && lit.has(p.dataset.to));
  }
  box.replaceChildren(root);
  light(null);
}

// -- the feed --------------------------------------------------------------------

function summary(c) {
  // The second line of a call: where it stopped, or where it went.
  const stop = c.steps.find((s) => s.outcome === 'refused');
  if (stop) {
    const [verb, obj] = describe(stop);
    return {bad: true, text: 'Refused at ' + systemName(stop.integration) + ': ' + verb.toLowerCase() + (obj ? ' ' + obj : '') + (stop.reason || c.reason ? ' — ' + shortReason(stop.reason || c.reason) : '')};
  }
  if (c.outcome === 'refused') return {bad: true, text: 'Refused before it ran: ' + shortReason(c.reason)};
  if (c.outcome === 'running') return {bad: false, text: 'Running…'};
  if (c.outcome === 'noresp') return {bad: false, text: 'The server never answered'};
  const systems = [...new Set(c.steps.map((s) => s.integration).filter(Boolean))];
  if (systems.length) return {bad: c.outcome === 'error', text: plural(c.steps.length, 'request') + ' to ' + listWords(systems.map(systemName))};
  const args = Object.entries(c.arguments || {}).map(([k, v]) => k + '=' + (typeof v === 'string' ? v : JSON.stringify(v))).join(', ');
  if (c.outcome === 'error') return {bad: true, text: (c.result || 'failed').split('\n')[0]};
  return {bad: false, text: args || 'no arguments'};
}

const PILL = {ok: 'went through', refused: 'refused', error: 'failed', running: 'running', noresp: 'no answer'};

function renderCall(c) {
  const li = el('li', 'call' + (state.open.has(c.key) ? ' open' : ''));
  const row = el('button', 'row');
  row.type = 'button';
  row.setAttribute('aria-expanded', String(state.open.has(c.key)));
  row.appendChild(avatar(c.user || '?'));
  const what = el('div', 'what');
  const title = el('div', 'title');
  title.appendChild(el('b', null, c.user ? personName(c.user) : 'An agent'));
  title.appendChild(document.createTextNode('ran'));
  title.appendChild(el('span', 'tool', c.tool));
  if (c.server) title.appendChild(el('span', 'srv', 'on ' + c.server));
  what.appendChild(title);
  const s = summary(c);
  what.appendChild(el('div', 'line2' + (s.bad ? ' bad' : ''), s.text));
  row.appendChild(what);
  const trail = el('div', 'trail');
  const steps = c.steps.filter((x) => x.integration);
  for (const st of steps.slice(0, 7)) {
    const w = el('span', 'step' + (st.outcome === 'refused' ? ' refused' : ''));
    const [verb, obj] = describe(st);
    w.title = verb + (obj ? ' ' + obj : '') + (st.outcome === 'refused' ? ' (refused)' : '');
    w.appendChild(mark(st.integration));
    trail.appendChild(w);
  }
  if (steps.length > 7) trail.appendChild(el('span', 'more-steps', '+' + (steps.length - 7)));
  row.appendChild(trail);
  row.appendChild(el('span', 'pill ' + c.outcome, PILL[c.outcome] || c.outcome));
  const when = el('div', 'when');
  const t = el('span', null, ago(c.time));
  t.title = c.time || '';
  when.appendChild(t);
  if (c.duration !== null && c.duration !== undefined) when.appendChild(el('span', null, took(c.duration)));
  row.appendChild(when);
  row.addEventListener('click', () => {
    if (state.open.has(c.key)) state.open.delete(c.key); else state.open.add(c.key);
    li.replaceWith(renderCall(c));
  });
  li.appendChild(row);
  if (state.open.has(c.key)) li.appendChild(renderDetail(c));
  return li;
}

function renderDetail(c) {
  const d = el('div', 'detail');
  if (c.steps.length) {
    const ol = el('ol', 'steps');
    for (const st of c.steps) {
      const refused = st.outcome === 'refused';
      const unchecked = !st.action && !refused;
      const li = el('li', 's' + (refused ? ' refused' : unchecked ? ' unchecked' : ''));
      li.appendChild(mark(st.integration || ''));
      const body = el('div');
      const say = el('div', 'say');
      const [verb, obj] = describe(st);
      say.appendChild(document.createTextNode(verb + (obj ? ' ' : '')));
      if (obj) say.appendChild(el('span', 'obj', obj));
      if (st.as_agent) say.appendChild(el('span', 'tag', 'as the agent'));
      if (st.sends_out) say.appendChild(el('span', 'tag', 'sends out'));
      body.appendChild(say);
      body.appendChild(el('div', 'req', [st.method, st.path, st.action ? '· ' + st.action : ''].filter(Boolean).join(' ')));
      if (refused && (st.reason || c.reason)) body.appendChild(el('div', 'why', shortReason(st.reason || c.reason)));
      li.appendChild(body);
      li.appendChild(el('span', 'verdict', refused ? 'refused' : unchecked ? 'not checked' : 'allowed'));
      ol.appendChild(li);
    }
    d.appendChild(ol);
    if (c.steps.some((x) => x.outcome === 'refused')) d.appendChild(el('p', 'stopped', 'The refused request was never sent. The tool stopped there.'));
  } else if (c.outcome === 'refused' && c.reason) {
    d.appendChild(el('div', 'why', shortReason(c.reason)));
  }
  const kv = el('dl', 'kv');
  const args = Object.entries(c.arguments || {});
  if (args.length) {
    kv.appendChild(el('dt', null, 'Arguments'));
    kv.appendChild(el('dd', null, args.map(([k, v]) => k + ' = ' + JSON.stringify(v)).join('\n')));
  }
  if (c.user) { kv.appendChild(el('dt', null, 'Person')); kv.appendChild(el('dd', null, c.user)); }
  if (c.result) { kv.appendChild(el('dt', null, c.outcome === 'ok' ? 'Result' : 'Answer')); kv.appendChild(el('dd', 'result', c.result)); }
  if (c.time) { kv.appendChild(el('dt', null, 'Time')); kv.appendChild(el('dd', null, new Date(Date.parse(c.time)).toLocaleString())); }
  d.appendChild(kv);
  return d;
}

function renderPeople(all) {
  const box = $('people');
  const people = [...new Set(all.map((c) => c.user).filter(Boolean))].sort();
  box.hidden = people.length === 0;
  box.replaceChildren();
  const every = el('button', 'chip all' + (state.person === '' ? ' on' : ''), 'Everyone');
  every.type = 'button';
  every.addEventListener('click', () => { state.person = ''; state.shown = PAGE; render(); });
  box.appendChild(every);
  for (const p of people) {
    const b = el('button', 'chip' + (state.person === p ? ' on' : ''));
    b.type = 'button';
    b.title = p;
    b.appendChild(avatar(p));
    b.appendChild(document.createTextNode(personName(p)));
    b.addEventListener('click', () => { state.person = state.person === p ? '' : p; state.shown = PAGE; render(); });
    box.appendChild(b);
  }
}

function render() {
  const all = sorted();
  const visible = all.filter(matches);
  renderStats(all);
  renderPeople(all);
  renderMap(all.filter((c) => !state.person || c.user === state.person));
  for (const b of document.querySelectorAll('.seg button')) b.classList.toggle('on', b.dataset.outcome === state.outcome);
  const feed = $('feed');
  feed.replaceChildren(...visible.slice(0, state.shown).map(renderCall));
  $('more').hidden = visible.length <= state.shown;
  $('more').textContent = 'Show ' + Math.min(PAGE, visible.length - state.shown) + ' more';
  const empty = $('empty');
  empty.hidden = visible.length > 0;
  if (all.length === 0) {
    empty.querySelector('.empty-title').textContent = 'Waiting for the first call';
    $('empty-text').textContent = state.kind === 'dashboard'
      ? 'Start your MCP server or agent with TOOLPASS_DASHBOARD=1 and use one of its tools.'
      : 'Calls show up here as soon as a tool runs.';
  } else if (visible.length === 0) {
    empty.querySelector('.empty-title').textContent = 'No calls match';
    $('empty-text').textContent = 'Change the person, outcome or search above.';
  }
  state.dirty = false;
}

// -- wiring ----------------------------------------------------------------------

for (const b of document.querySelectorAll('.seg button, .stat[data-outcome]')) {
  b.addEventListener('click', () => {
    const o = b.dataset.outcome;
    state.outcome = b.classList.contains('stat') && state.outcome === o ? 'all' : o;
    state.shown = PAGE; render();
  });
}
$('filter').addEventListener('input', (e) => { state.query = e.target.value.trim().toLowerCase(); state.shown = PAGE; render(); });
$('more').addEventListener('click', () => { state.shown += PAGE; render(); });
let width = window.innerWidth;
window.addEventListener('resize', () => { if (Math.abs(window.innerWidth - width) > 40) { width = window.innerWidth; render(); } });
setInterval(() => { for (const t of document.querySelectorAll('.when span[title]')) t.textContent = ago(t.title); }, 30000);

function live(on, text) {
  $('live').classList.toggle('off', !on);
  $('live-text').textContent = text;
}

async function poll() {
  try {
    const r = await fetch('events?since=' + state.next, {cache: 'no-store'});
    if (!r.ok) throw new Error(String(r.status));
    const d = await r.json();
    if (d.kind !== state.kind) { state.kind = d.kind; state.dirty = true; }
    for (const raw of d.events) { const c = normalize(raw); state.calls.set(c.key, c); state.dirty = true; }
    if (state.calls.size > KEEP) {
      for (const c of sorted().slice(KEEP)) state.calls.delete(c.key);
    }
    state.next = d.next;
    live(true, 'live');
    if (state.dirty) render();
  } catch (err) {
    live(false, 'disconnected');
  }
  setTimeout(poll, 1000);
}

render();
poll();
