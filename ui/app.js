/* AgentFeed dashboard. Vanilla JS: no build step, one file you can read. */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

const state = {
  view: 'latest', filters: {}, text: '', rendition: 'brief',
  items: [], active: null, domain: null, showOriginal: false,
  // Where the reader was before you followed a citation. Losing a
  // minute-old analysis because you clicked one of its own links is the
  // kind of small betrayal that makes a tool feel untrustworthy.
  back: null,
  lang: 'en',
  languages: [],
  poller: null,
  // When this reader last opened the app; "new" is what arrived after it.
  // Falls back to 24h on a first visit or a cleared browser.
  lastSeen: (() => {
    try { const v = localStorage.getItem('agentfeed.lastSeen'); if (v) return new Date(v); }
    catch (_) { /* storage may be unavailable */ }
    return new Date(Date.now() - 86400000);
  })(),
  topic: null,        // the open topic, or null for the whole corpus
  topics: [],
  facetOpen: {},      // facet key -> showing every value
  collections: [],
  draft: null,        // the rule being built, before it is saved
  thread: { collection: null, answers: [] },   // the running conversation
  collection: null,   // the open collection, or null
};

function setBack(label, fn) { state.back = { label, fn }; }

function syncReaderStar(on) {
  const b = $('#btn-star');
  if (!b) return;
  b.textContent = on ? '★' : '☆';
  b.classList.toggle('on', !!on);
  b.title = on ? 'Saved — click to unsave' : 'Save to Favourites';
}

function clearReader() {
  state.active = null; state.activeData = null; state.back = null;
  $('#reader-actions').classList.add('hidden');
  $('#reader').innerHTML = `<div class="empty-state">
      <div class="empty-mark">⇶</div>
      <p>Select an item, or open a view.</p></div>`;
}
function backBar() {
  return state.back
    ? `<button class="mini-btn" id="btn-back">← ${esc(state.back.label)}</button>`
    : '';
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let d = res.statusText;
    try { d = (await res.json()).detail || d; } catch (_) {}
    throw new Error(d);
  }
  return res.json();
}
const esc = (s) => String(s ?? '').replace(/[&<>"']/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function fmtDate(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const days = Math.floor((Date.now() - d) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days < 7) return `${days}d ago`;
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}
// An undated item never borrows its fetch time: a 2019 article surfacing
// today is not today's news.
const whenOf = (i) => i.published_at ? fmtDate(i.published_at)
                                     : `undated · added ${fmtDate(i.fetched_at).toLowerCase()}`;

// Enough markdown for what a local model writes: headings, bullet lists,
// bold, italic, inline code, paragraphs. Everything is escaped first, so the
// only tags on the page are the ones this function makes.
function inline(t) {
  return esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')
    .replace(/(^|[^*])\*([^*\n]+?)\*(?!\*)/g, '$1<i>$2</i>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}
function md(src) {
  const out = [];
  let list = null, para = [];
  const flush = () => {
    if (para.length) { out.push(`<p>${inline(para.join(' '))}</p>`); para = []; }
  };
  const endList = () => { if (list) { out.push(`<ul>${list.join('')}</ul>`); list = null; } };
  for (const raw of String(src || '').split('\n')) {
    const l = raw.trim();
    const h = l.match(/^(#{1,4})\s+(.*)$/);
    const b = l.match(/^(?:[-*•]|\d+[.)])\s+(.*)$/);
    if (h) { flush(); endList(); out.push(`<h${h[1].length + 1}>${inline(h[2])}</h${h[1].length + 1}>`); }
    else if (b) { flush(); (list ||= []).push(`<li>${inline(b[1])}</li>`); }
    else if (!l) { flush(); endList(); }
    else { endList(); para.push(l); }
  }
  flush(); endList();
  return out.join('');
}

/* ── boot ─────────────────────────────────────────────────── */
async function loadTopics() {
  const { topics } = await api('/api/topics?period=day');
  state.topics = topics;
  $('#topic-list').innerHTML = topics.map((t) => `
    <div class="nav-item topic-row ${String(t.id) === state.topic ? 'active' : ''}"
         data-topic="${t.id}">
      <span class="nav-label" title="${esc(t.description || '')}">${esc(t.name)}</span>
      ${t.recent ? `<span class="chip new">${t.recent}</span>` : ''}
      <span class="nav-count">${t.count}</span>
      <button class="topic-btn" data-digest="${t.id}" title="Summarise this topic">▤</button>
      <button class="topic-btn" data-edit-topic="${t.id}" title="Edit this topic">✎</button>
      <button class="topic-btn del" data-del-topic="${t.id}"
              title="Remove this topic">✕</button>
    </div>`).join('') || '<div class="nav-item dim">No topics yet — press +</div>';
}

async function openTopic(id) {
  const { topic, items } = await api(`/api/topics/${id}/items?limit=80`);
  state.topic = String(id);
  state.items = items;
  $('#list-meta').textContent = `${topic.name} · ${items.length} items`;
  $('#list').innerHTML = items.map((i) => rowHTML(i, {
    extra: `<div class="why">matched ${esc((i.matched || []).join(' · '))}</div>`,
  })).join('') || emptyTopic(topic);
}

async function showDigest(id, period = 'day') {
  $('#reader-actions').classList.add('hidden');
  $('#reader').innerHTML = `<div class="art-body"><p class="dim">
    Selecting deterministically, then summarising…</p></div>`;
  const d = await api(`/api/topics/${id}/digest?period=${period}`);
  const st = d.stats || {};
  $('#reader').innerHTML = `
    <div class="art-body">
      <div class="dim">${esc(d.topic)} · ${esc(d.period)} from ${esc(d.period_start)}</div>
      <h2>${esc(d.headline || d.topic)}</h2>
      <div class="funnel">
        <span>topic index → <b>${st.items ?? 0}</b> selected</span>
        <span>${st.sources ?? 0} sources</span>
        <span>budget <b>${st.budget ?? 0}</b> tokens</span>
        <span>1 model call</span>
      </div>
      ${d.empty ? `<p>${esc(d.note || '')}</p>
        <button class="mini-btn" data-scout="${id}">Find sources for this</button>` : ''}
      ${d.error ? `<div class="disclaimer">Prose unavailable: ${esc(d.error)}
        <br>The selection below is deterministic and unaffected.</div>` : ''}
      ${d.summary ? `<p>${esc(d.summary)}</p>` : ''}
      ${(d.bullets || []).length ? `<ul>${d.bullets.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}
      <div class="period-switch" style="margin:14px 0">
        ${['day', 'week', 'month'].map((p) =>
          `<button class="mini-btn" data-digest="${id}" data-period="${p}"
            ${p === d.period ? 'disabled' : ''}>${p}</button>`).join(' ')}
      </div>
      <h3>Items</h3>
      ${(d.items || []).map((i) => `
        <div class="src-row"><div class="src-name">
          <b>${esc(i.headline || i.title)}</b>
          <span>${esc(i.source_name || '')} · ${esc((i.published_at || '').slice(0,10) || 'undated')}</span>
        </div></div>`).join('')}
    </div>`;
}

async function showSources() {
  const [{ sources }, health] = await Promise.all([
    api('/api/sources'), api('/api/health'),
  ]);
  $('#reader-actions').classList.add('hidden');
  const byKind = {};
  for (const s of sources) (byKind[s.kind] ||= []).push(s);
  const kindName = {
    rss: 'Feeds', search: 'Web watches', html_list: 'Page scrapers',
    europepmc: 'Research queries', openalex: 'Citation queries',
    import: 'Imported', manual: 'Manual captures',
  };
  $('#reader').innerHTML = `
    <div class="art-body">
      <h2>Sources</h2>
      <div class="add-box">
        <input id="src-add" placeholder="Paste a name or URL — techcrunch, bbc.co.uk/news, https://…">
        <button class="mini-btn" id="btn-src-add">Add</button>
      </div>
      <div id="src-add-out" class="dim"></div>
      <div class="field-note">
        Turning a source off keeps everything it already collected. Removing it
        keeps the articles too, and only stops future fetches.
      </div>
      ${Object.entries(byKind).map(([kind, list]) => `
        <h3 class="sec-h">${esc(kindName[kind] || kind)} (${list.length})</h3>
        ${list.map((s) => `
          <div class="src-row">
            <input type="checkbox" ${s.enabled ? 'checked' : ''} data-toggle-src="${s.id}">
            <div class="src-name">
              <b>${esc(s.name)}</b>
              <span><a href="${esc(s.url)}" target="_blank">${esc(s.config?.query || s.url)}</a></span>
            </div>
            <span class="src-badge ${s.failing ? 'bad' : (s.last_status === 'ok' ? 'ok' : '')}"
                  title="${esc(s.last_error || '')}">${s.failing ? 'failing' : (s.last_status || 'new')}</span>
            <span class="src-badge">${s.items_total} items</span>
            <button class="mini-btn" data-del-src="${s.id}">Remove</button>
          </div>`).join('')}`).join('')}
      <div class="field-note">Fetching from ${health.counts.sources} enabled
        source(s) collected ${health.counts.items} items so far.</div>
    </div>`;
}

async function showStatus() {
  const h = await api('/api/health');
  const llm = h.llm || {};
  const b = llm.budget || {};
  $('#reader-actions').classList.add('hidden');
  // Which database am I looking at? Two instances on one port with different
  // data directories is a genuinely confusing state, and nothing in the UI
  // said which one you had.
  $('#reader').innerHTML = `
    <div class="art-body">
      <h2>Status</h2>
      <div class="stat-grid">
        <div class="stat"><b>${h.counts.items}</b><span>items</span></div>
        <div class="stat"><b>${h.counts.enriched}</b><span>filed</span></div>
        <div class="stat"><b>${h.counts.sources}</b><span>sources</span></div>
        <div class="stat"><b>${h.counts.subscriptions}</b><span>agent subscriptions</span></div>
        <div class="stat"><b>${h.counts.entities}</b><span>entities</span></div>
      </div>

      <h3 class="sec-h">This instance</h3>
      <dl class="kv">
        <dt>Database</dt><dd>${esc(h.data_dir)}</dd>
        <dt>Domain pack</dt><dd>
          <select id="pack-pick">${(state.domain?.packs || []).map((pk) =>
            `<option value="${esc(pk.name)}" ${pk.name === h.domain.name ? 'selected' : ''}
              >${esc(pk.label)}</option>`).join('')}</select>
          <button class="mini-btn" id="btn-pack">Switch</button>
          <div class="field-note">Switching changes the facets this feed
            offers. Items keep the labels they were filed with until you
            re-file them (<code>agentfeed enrich --refile-all</code>).</div>
        </dd>
        <dt>Facets</dt><dd>${h.domain.facets.map((f) => esc(f.key)).join(', ')}</dd>
        <dt>Feed id</dt><dd>${esc(h.feed.id)}</dd>
      </dl>

      <h3 class="sec-h">Local model</h3>
      ${llm.ok ? `<dl class="kv">
        <dt>Runtime</dt><dd>${esc(llm.provider_label || '')}</dd>
        <dt>Endpoint</dt><dd>${esc(llm.base_url || '')}</dd>
        <dt>Chat model</dt><dd>${esc(llm.chat_model || '')} ${llm.chat_loaded ? '✓' : '— not loaded'}</dd>
        <dt>Embeddings</dt><dd>${esc(llm.embed_model || '')} ${llm.embed_loaded ? '✓' : '— keyword search only'}</dd>
        <dt>Context</dt><dd>${b.ctx || 'unknown'} tokens · ${b.words || '?'} words/article · concurrency ${b.concurrency || '?'}</dd>
      </dl>
      ${b.ctx && b.ctx < 16384 ? `<div class="disclaimer">
        <b>Small context window.</b> ${esc(llm.context_hint || '')}</div>` : ''}`
      : `<div class="disclaimer"><b>No model runtime is answering.</b><br>
         ${esc(llm.error || '')}</div>`}

      <h3 class="sec-h">Agent protocol</h3>
      <dl class="kv">
        <dt>Discovery</dt><dd>${esc(h.feed.base_url)}/.well-known/agent-feed</dd>
        <dt>Subscribe</dt><dd>POST ${esc(h.feed.base_url)}/agentfeed/subscriptions</dd>
      </dl>
      <div class="field-note">Run <code>agentfeed doctor</code> for the same
        checks in the terminal, plus label/pack mismatch detection.</div>
    </div>`;
}

// The box takes a site ("techcrunch", a URL) and guesses a domain for it.
// That guess is meaningless for a subject ("fish vaccines", "ai news") --
// squashed to "fishvaccines.com" and tried as a hostname, which is not
// what was meant. Either resolve fails outright, or it lands on some
// unrelated host with no real feed on it. Both cases get the same offer:
// search the web for sources about the words themselves.
function findSourcesHint(text, fromSources) {
  return `<button class="mini-btn" data-find-sources="${esc(text)}"
            ${fromSources ? 'data-from-sources="1"' : ''}
            >Search the web for sources about “${esc(text)}”</button>`;
}

async function addSite(inputSel = '#add-site', outSel = '#add-out') {
  const input = $(inputSel);
  const v = (input?.value || '').trim();
  if (!v) return;
  const out = $(outSel);
  const fromSources = inputSel === '#src-add';
  out.textContent = 'resolving…';
  try {
    const d = await api('/api/sources/resolve', { method: 'POST', body: { text: v } });
    if (!d.ok) {
      out.innerHTML = `${esc(d.reason)}<br>${findSourcesHint(v, fromSources)}`;
      return;
    }
    out.textContent = '';
    confirmAddSource(d, v, { inputSel, outSel, fromSources });
  } catch (e) { out.textContent = 'failed: ' + e.message; }
}

const SOURCE_KIND_LABEL = {
  afp: 'Speaks the AgentFeed protocol — already read and filed.',
  rss: 'Publishes an RSS/Atom feed.',
  search: 'No feed on this site at all. Adding it means a standing weekly '
          + 'search scoped to just this one site, not its articles directly.',
};

// Resolving used to add the best guess straight away, silently. One bad
// guess later ("fish vaccines" landing on some unrelated host that happens
// to answer) and there is no undo but the Sources page. So now it only
// proposes -- the same single best candidate as before, nothing new to
// pick from -- and adding it is one explicit click, same as everywhere
// else in the app that changes what gets fetched.
function confirmAddSource(d, text, origin) {
  const best = d.candidates[0];
  const title = d.site_title || best.title || text;
  const weak = best.kind === 'search';
  openModal('Add this source?', `
    <div class="scout-body">
      <b>${esc(title)}</b>
      <span class="scout-why">${esc(SOURCE_KIND_LABEL[best.kind] || best.kind)}</span>
      <span class="scout-url">${esc(best.url)}</span>
    </div>
    <div class="form-btns">
      <button class="primary-btn" id="confirm-add-yes">Add source</button>
      <button class="mini-btn" id="confirm-add-cancel">Cancel</button>
    </div>
    ${weak ? `<div class="field-note">Looking for <b>${esc(text)}</b> the subject,
      not a specific site? ${findSourcesHint(text, origin.fromSources)}</div>` : ''}
    <div id="confirm-add-out" class="form-out"></div>`);
  state.pendingSource = { best, title, ...origin };
}

async function addConfirmedSource() {
  const p = state.pendingSource;
  if (!p) return;
  $('#confirm-add-out').textContent = 'adding…';
  try {
    await api('/api/sources', { method: 'POST', body: {
      kind: p.best.kind, name: p.title, url: p.best.url,
      config: p.best.config || {}, tags: ['user'] } });
  } catch (e) {
    $('#confirm-add-out').textContent = e.message.includes('already exists')
      ? 'Already in your sources.' : 'failed: ' + e.message;
    return;
  }
  closeModal();
  const input = $(p.inputSel);
  if (input) input.value = '';
  const added = `added <b>${esc(p.title)}</b><br>${esc(p.best.url)}`;
  // Adding from inside the Sources panel used to leave you looking at a
  // list that did not contain what you had just added.
  if (p.fromSources) {
    await showSources();
    $('#src-add-out').innerHTML = added;
  } else {
    const out = $(p.outSel);
    if (out) out.innerHTML = added;
  }
  await boot0();
}

function emptyTopic(topic) {
  return `<div class="empty-state">
      <p>Nothing matched <b>${esc(topic.name)}</b> yet.</p>
      <p class="dim">Either nothing has been published, or no source you
        subscribe to covers it.</p>
      <button class="mini-btn" data-scout="${topic.id}">Find sources for this</button>
    </div>`;
}

/* ── modal ────────────────────────────────────────────────── */
function openModal(title, html) {
  $('#modal-title').textContent = title;
  $('#modal-body').innerHTML = html;
  $('#modal').classList.remove('hidden');
}
function closeModal() { $('#modal').classList.add('hidden'); }

/* ── scouting for sources ─────────────────────────────────── */
// Everything the model proposes is checked against the live web before it
// is shown, so this waits on real network work — a background job with
// progress, not a fetch the browser gives up on. Two ways in: a saved
// topic that is finding nothing, or a subject typed straight into the
// add-source box with no topic at all. Same modal either way.
async function scoutSources(topicId) {
  openModal('Finding sources', `<p class="dim" id="scout-msg">starting…</p>`);
  const r = await api(`/api/topics/${topicId}/scout`, { method: 'POST' });
  if (!r.ok) { $('#scout-msg').textContent = r.reason; return; }
  await pollScout('/api/scout/status', { mode: 'topic', topicId });
}

async function findSourcesForText(text, fromSources) {
  openModal('Finding sources', `<p class="dim" id="scout-msg">starting…</p>`);
  const r = await api('/api/sources/scout', { method: 'POST', body: { text } });
  if (!r.ok) { $('#scout-msg').textContent = r.reason; return; }
  await pollScout('/api/sources/scout/status', { mode: 'subject', fromSources });
}

async function pollScout(statusUrl, origin) {
  await new Promise((done) => {
    const t = setInterval(async () => {
      let s;
      try {
        s = await api(statusUrl);
      } catch (err) {
        clearInterval(t);
        $('#scout-msg').textContent = 'Lost contact with the backend: ' + err.message;
        return done();
      }
      const msg = $('#scout-msg');
      if (msg) msg.textContent = s.message || 'looking…';
      if (!s.active) {
        clearInterval(t);
        renderScout(s.result || {}, origin);
        done();
      }
    }, 1200);
  });
}

function renderScout(res, origin) {
  // Nothing checked out: say so and leave the page as it was. The message
  // underneath is still the true one.
  if (!res.ok) {
    openModal('No sources found', `
      <p>${esc(res.reason || 'Nothing found.')}</p>
      ${res.terms ? `<p class="dim">Looked for: ${esc(res.terms.join(', '))}</p>` : ''}
      <div class="form-btns"><button class="mini-btn" id="scout-close">Close</button></div>`);
    return;
  }
  const label = res.topic || res.subject || '';
  openModal(`Sources for “${label}”`, `
    <p class="dim">Each of these was fetched just now, and is listed only
      because its recent posts match these words. Weak matches are
      shown but left unticked.</p>
    ${res.suggestions.map((x, i) => `
      <label class="scout-row">
        <input type="checkbox" data-sugg="${i}" ${x.hits > 1 ? 'checked' : ''}>
        <div class="scout-body">
          <b>${esc(x.name)}</b>
          <span class="scout-why">${esc(x.why)}</span>
          <span class="scout-url">${esc(x.url)}</span>
          ${(x.samples || []).length ? `<ul class="scout-samples">${
            x.samples.map((h) => `<li>${esc(h)}</li>`).join('')}</ul>` : ''}
        </div>
      </label>`).join('')}
    ${res.model_error ? `<p class="dim">The local model was not available
      (${esc(res.model_error)}), so only the web search leg ran.</p>` : ''}
    <div class="form-btns">
      <button class="primary-btn" id="scout-add">Add selected and fetch</button>
      <button class="mini-btn" id="scout-close">Cancel</button>
    </div>
    <div id="scout-out" class="form-out"></div>`);
  state.scout = { ...origin, suggestions: res.suggestions };
}

async function adoptScouted() {
  const picked = $$('[data-sugg]').filter((c) => c.checked)
    .map((c) => state.scout.suggestions[Number(c.dataset.sugg)]);
  if (!picked.length) { $('#scout-out').textContent = 'Nothing selected.'; return; }
  $('#scout-out').textContent = 'adding…';
  const sources = picked.map((x) => ({
    kind: x.kind, name: x.name, url: x.url,
    config: x.config || {}, tags: ['scout'] }));
  const url = state.scout.mode === 'topic'
    ? `/api/topics/${state.scout.topicId}/adopt` : '/api/sources/adopt';
  const d = await api(url, { method: 'POST', body: { sources } });
  if (!d.added.length) {
    $('#scout-out').textContent = 'Those are already in your sources.';
    return;
  }
  if (state.scout.mode === 'subject' && state.scout.fromSources) await showSources();
  // Fetch just the new ones, through the same run machinery and the same
  // progress toast as everything else.
  const run = await api('/api/run', { method: 'POST',
                                      body: { source_ids: d.added } });
  closeModal();
  if (!run.ok) { toast(run.reason || 'Could not start a fetch', 5000); return; }
  toast(`Added ${d.added.length} source(s) — fetching…`);
  await loadTopics();
  return pollRun();
}

/* ── forms ────────────────────────────────────────────────── */
const csv = (v) => String(v || '').split(',').map((x) => x.trim()).filter(Boolean);

function facetPicker(selected = {}) {
  return (state.domain?.facets || []).map((f) => `
    <div class="pick-group">
      <div class="pick-key">${esc(f.label)}</div>
      <div class="pick-vals">${f.terms.map((t) => `
        <label class="pick"><input type="checkbox" data-pick-facet="${esc(f.key)}"
          value="${esc(t.id)}" ${(selected[f.key] || []).includes(t.id) ? 'checked' : ''}>
          <span>${esc(t.label)}</span></label>`).join('')}
      </div>
    </div>`).join('');
}

function readFacetPicker() {
  const out = {};
  $$('[data-pick-facet]').forEach((el) => {
    if (el.checked) (out[el.dataset.pickFacet] ||= []).push(el.value);
  });
  return out;
}

// Following a subject is one job, so it is one button and one running
// commentary -- not "draft", then "save", then notice it is empty, then
// "find sources", then "fetch". Each of those was a step the person had to
// know about, and not one of them was their idea.
function showTopicForm(id = null) {
  const t = id ? state.topics.find((x) => String(x.id) === String(id)) : null;
  state.topic = null;
  state.draft = t ? { ...(t.rule || {}) } : null;
  $('#reader-actions').classList.add('hidden');
  $('#reader').innerHTML = `
    <div class="art-body">
      <h2>${t ? 'Edit topic' : 'Follow a subject'}</h2>
      <div class="form" data-form="topic" data-id="${t ? t.id : ''}">
        <label>${t ? 'Name' : 'What do you want to follow?'}
          <input id="tf-name" value="${esc(t ? t.name : '')}"
            placeholder="ransomware attacks on hospitals"></label>
        ${t ? '' : `<div class="field-note">One sentence. It works out the
          vocabulary, searches what you already have, and goes looking for
          sources if there is nothing to show you.</div>`}
        <div class="form-btns">
          ${t ? `<button class="primary-btn" id="tf-save">Save changes</button>
                 <button class="mini-btn danger" data-del-topic="${t.id}">Delete topic</button>`
               : `<button class="primary-btn" id="tf-follow">Follow it</button>`}
        </div>
        <div id="tf-out" class="form-out"></div>
        <div id="tf-built">${t ? builtHTML(t.rule || {}) : ''}</div>
      </div>
    </div>`;
}

// A saved topic's rule, drawn as things you can remove: the words it
// matches on, the search anchors, the relevance test. This is the whole of
// the edit view -- the pipeline built the rule, the person prunes it.
function builtHTML(rule) {
  const chips = (items, kind) => (items || []).map((x) =>
    `<span class="term-chip" data-drop-term="${esc(kind)}|${esc(x)}"
      >${esc(x)}<span class="x">×</span></span>`).join('');
  const test = (rule.agent || {}).instruction || '';
  return `
    <div class="built">
      <div class="built-row"><span class="built-key">Words</span>
        <div>${chips(rule.include, 'include') || '<span class="dim">none — this topic matches on labels only</span>'}</div></div>
      ${(rule.exclude || []).length ? `<div class="built-row">
        <span class="built-key">Never</span>
        <div>${chips(rule.exclude, 'exclude')}</div></div>` : ''}
      ${(rule.context_orgs || []).length ? `<div class="built-row">
        <span class="built-key">Anchors</span>
        <div>${chips(rule.context_orgs, 'context_orgs')}
          <div class="field-note">Steer the similarity search; they never gate
            anything.</div></div></div>` : ''}
      ${Object.keys(rule.facets || {}).length ? `<div class="built-row">
        <span class="built-key">Labels</span>
        <div>${Object.entries(rule.facets).map(([k, vs]) =>
          `<span class="dim">${esc(facetLabel(k))}: ${esc(vs.join(', '))}</span>`).join('')}</div></div>` : ''}
      <div class="built-row"><span class="built-key">Relevance test</span>
        <div><textarea id="tf-test" rows="2"
          placeholder="only items substantively about … — a passing mention does not qualify">${esc(test)}</textarea>
          <div class="field-note">The model applies this to weak word matches
            and to near-misses found by meaning. Empty it and the topic is a
            plain word match. <button class="mini-btn" id="tf-try">Try it</button></div>
        </div></div>
    </div>`;
}

// The work, narrated as it happens. A minute of silence reads as a hang;
// a minute of "rejected 9 weak matches" reads as thinking.
async function followSubject() {
  const desc = $('#tf-name').value.trim();
  if (!desc) { $('#tf-out').textContent = 'Say what you want to follow.'; return; }
  $('#tf-follow').disabled = true;
  const out = $('#tf-out');
  const r = await api('/api/topics/follow', { method: 'POST',
                                              body: { description: desc } });
  if (!r.ok) { out.textContent = r.reason; $('#tf-follow').disabled = false; return; }

  await new Promise((done) => {
    const t = setInterval(async () => {
      let s;
      try { s = await api('/api/topics/follow/status'); }
      catch (err) { clearInterval(t); out.textContent = err.message; return done(); }
      out.innerHTML = `<div class="steps">${s.steps.map((x, i) => `
        <div class="step ${i === s.steps.length - 1 && s.active ? 'now' : 'done'}">
          <span class="step-mark">${i === s.steps.length - 1 && s.active ? '◐' : '✓'}</span>
          <span>${esc(x.text)}${x.detail
            ? `<span class="step-detail">${esc(x.detail)}</span>` : ''}</span>
        </div>`).join('')}</div>`;
      if (!s.active) { clearInterval(t); renderFollowed(s.result || {}); done(); }
    }, 900);
  });
}

async function renderFollowed(res) {
  await loadTopics();
  if (!res.ok) {
    $('#tf-out').innerHTML += `<div class="disclaimer">${esc(res.reason || '')}</div>`;
    return;
  }
  state.draft = res.rule;
  if (res.suggestions && res.suggestions.length) {
    // Nothing to read yet, but sources that would fix that. Offer them here
    // rather than making the person go and find the button.
    renderScout({ ok: true, topic: '', suggestions: res.suggestions },
                { mode: 'topic', topicId: res.topic_id });
    return;
  }
  await openTopic(res.topic_id);
}


function topicRuleFromForm() {
  const rule = { ...(state.draft || {}) };
  const test = $('#tf-test');
  if (test && test.value.trim()) {
    rule.agent = { instruction: test.value.trim(),
                   mode: (rule.agent || {}).mode || 'strict' };
  } else {
    delete rule.agent;
  }
  return rule;
}

async function saveTopic() {
  const id = $('[data-form="topic"]').dataset.id;
  const name = $('#tf-name').value.trim();
  const out = $('#tf-out');
  if (!name) { out.textContent = 'A topic needs a name.'; return; }
  const body = { name, description: (state.draft || {}).seed || '',
                 rule: topicRuleFromForm() };
  out.textContent = 'saving and routing…';
  try {
    const r = await api(id ? `/api/topics/${id}` : '/api/topics',
                        { method: id ? 'PUT' : 'POST', body });
    await loadTopics();
    const topicId = id || r.id;
    await openTopic(topicId);
    if (r.routed.matches) {
      toast(`Saved — ${r.routed.matches} item(s) matched`, 4000);
      return;
    }
    // A topic that matches nothing is not finished. Go straight to looking
    // for sources rather than leaving the person on an empty list wondering
    // what they did wrong.
    toast('Nothing in the corpus matches yet — looking for sources…', 4000);
    return scoutSources(topicId);
  } catch (e) {
    // The server explains itself (a rule with no positive clause, a facet
    // this pack does not have); show that rather than swallowing it.
    out.textContent = e.message;
  }
}

async function tryAgentTest() {
  // Try the relevance test against the corpus before committing to it.
  const rule = state.draft || {};
  const test = $('#tf-test');
  const instruction = test ? test.value.trim() : '';
  const out = $('#tf-out');
  if (!instruction) { out.textContent = 'There is no relevance test to try.'; return; }
  const prefilter = (rule.include || [])[0] || '';
  out.innerHTML = `<span class="dim">judging items containing “${esc(prefilter)}”…</span>`;
  const d = await api('/api/filters/preview', { method: 'POST',
    body: { instruction, mode: (rule.agent || {}).mode || 'strict',
            prefilter, limit: 12 } });
  out.innerHTML = `
    <div class="funnel">
      <span>corpus <b>${d.corpus}</b></span>
      <span>after words <b>${d.stats.candidates}</b></span>
      <span>judged <b>${d.stats.judged ?? 0}</b> (${d.stats.cached ?? 0} cached)</span>
      <span>kept <b>${d.passed.length}</b></span>
    </div>
    ${d.passed.map((x) => `<div class="dim">· ${esc(x.headline)}</div>`).join('')
      || '<p class="dim">Nothing passed.</p>'}
    ${(d.withheld || []).length ? `<div class="dim" style="margin-top:8px">
      withheld: ${d.withheld.slice(0, 3).map((w) =>
        esc((w.headline || w.title || '').slice(0, 46))).join('; ')}</div>` : ''}`;
}

async function loadLanguages() {
  const d = await api('/api/languages');
  state.languages = d.languages;
  state.lang = d.reader_language || 'en';
  $('#reader-lang').innerHTML = d.languages.map((l) =>
    `<option value="${esc(l.code)}" ${l.code === state.lang ? 'selected' : ''}>${esc(l.name)}</option>`
  ).join('');
}

async function boot0() {
  const h = await api('/api/health');
  $('#domain-label').textContent = `${h.domain.label} · ${h.counts.items} items`;
  $('#afp-text').textContent = `agentfeed/0.1 · ${h.counts.subscriptions} subs`;
  return h;
}

async function boot() {
  const h = await api('/api/health');
  state.domain = h.domain;
  $('#domain-label').textContent = `${h.domain.label} · ${h.counts.items} items`;
  $('#afp-text').textContent = `agentfeed/0.1 · ${h.counts.subscriptions} subs`;
  const d = await api('/api/domain');
  state.domain = d;
  await loadLanguages();
  renderFacetNav(d);
  await loadTopics();
  await loadCollections();
  await loadList();
  try { localStorage.setItem('agentfeed.lastSeen', new Date().toISOString()); }
  catch (_) { /* cosmetic */ }
}

function renderFacetNav(d) {
  $('#facet-groups').innerHTML = d.facets.map((f) => `
    <section class="nav-group">
      <h3 class="nav-title collapsible" data-toggle>${esc(f.label)}</h3>
      <div class="nav-body collapsed" id="facet-${esc(f.key)}"></div>
    </section>`).join('');
  loadFacetCounts();
}

async function loadFacetCounts() {
  //  `unread` is a filter, not a facet; the facet endpoint refuses unknown keys.
  const { unread: _u, ...facetsOnly } = state.filters;
  const counts = await api('/api/facets', { method: 'POST', body: { facets: facetsOnly, days: 120 } });
  for (const [key, rows] of Object.entries(counts)) {
    const el = $(`#facet-${key}`);
    if (!el) continue;
    // Reflect the current selection rather than owning it: the filter bar
    // and the sidebar must never disagree about what is applied.
    const selected = state.filters[key] || [];
    // Values beyond the cap used to be unreachable: on a pack with 19
    // regions you could not filter by six of them at all.
    const open = state.facetOpen[key];
    const shown = open ? rows : rows.slice(0, 14);
    el.innerHTML = shown.map((r) => `
      <div class="nav-item ${selected.includes(r.value) ? 'active' : ''}"
           data-facet="${esc(key)}" data-value="${esc(r.value)}">
        <span class="nav-label">${esc(labelFor(key, r.value))}</span>
        <span class="nav-count">${r.count}</span>
      </div>`).join('')
      + (rows.length > 14 ? `<div class="nav-item more" data-more="${esc(key)}">
           ${open ? 'show fewer' : `show all ${rows.length}`}</div>` : '')
      || '<div class="nav-item" style="color:var(--text-faint)">—</div>';
  }
}

function labelFor(key, value) {
  const f = (state.domain?.facets || []).find((x) => x.key === key);
  const t = f?.terms?.find((x) => x.id === value);
  return t ? t.label : value.replace(/_/g, ' ');
}

function facetLabel(key) {
  const f = (state.domain?.facets || []).find((x) => x.key === key);
  return f ? f.label : key;
}

// Say what the filter actually is. Values within one facet are OR'd,
// separate facets are AND'd, and neither was visible before.
function renderFilterBar() {
  const bar = $('#filter-bar');
  const keys = Object.keys(state.filters).filter((k) => state.filters[k]?.length);
  if (!keys.length) { bar.classList.add('hidden'); bar.innerHTML = ''; return; }
  bar.classList.remove('hidden');
  bar.innerHTML = keys.map((k, i) => `
    ${i ? '<div class="filter-join">and</div>' : ''}
    <div class="filter-group">
      <span class="filter-key">${esc(facetLabel(k))}</span>
      ${state.filters[k].map((v, j) => `
        ${j ? '<span class="filter-op">or</span>' : ''}
        <span class="filter-chip" data-unfilter="${esc(k)}" data-value="${esc(v)}"
              title="Remove">${esc(labelFor(k, v))}<span class="x">×</span></span>`).join('')}
    </div>`).join('')
    + '<button class="filter-clear" id="btn-clear-filters">Clear all filters</button>';
}

/* ── list ─────────────────────────────────────────────────── */
function leaveCollection() {
  if (!state.collection) return false;
  state.collection = null;
  $$('.coll-row').forEach((r) => r.classList.remove('active'));
  const v = $(`.nav-item[data-view="${state.view}"]`);
  if (v) v.classList.add('active');
  return true;
}

function leaveTopic() {
  // A facet click used to swap the list back to the whole corpus while the
  // topic stayed highlighted, so the sidebar and the list disagreed.
  if (!state.topic) return false;
  state.topic = null;
  $$('.topic-row').forEach((r) => r.classList.remove('active'));
  const v = $(`.nav-item[data-view="${state.view}"]`);
  if (v) v.classList.add('active');
  return true;
}

// The unread count lives on the "Latest" row in the sidebar, where a feed
// reader has always put it.
async function refreshUnreadCount() {
  try {
    const c = await api('/api/items/unread-count');
    const el = $('.nav-item[data-view="latest"] .nav-count')
      || $('.nav-item[data-view="latest"]').appendChild(Object.assign(
           document.createElement('span'), { className: 'nav-count' }));
    el.textContent = c.unread ? c.unread : '';
    el.classList.toggle('unread-count', c.unread > 0);
  } catch (_) { /* cosmetic */ }
}

async function toggleFilter(facet, value) {
  leaveTopic(); leaveCollection();
  const cur = state.filters[facet] || [];
  state.filters[facet] = cur.includes(value)
    ? cur.filter((v) => v !== value) : [...cur, value];
  if (!state.filters[facet].length) delete state.filters[facet];
  await loadList();
  // Counts are conditional on the other filters, so they have to follow.
  await loadFacetCounts();
}

async function loadList() {
  $('#list-meta').textContent = 'Loading…';
  const { unread, ...facets } = state.filters;
  const body = {
    text: state.text, facets, unread: !!unread, limit: 100,
    sort: state.view === 'top' ? 'impact' : 'newest',
    days: state.view === 'top' ? 120 : null,
  };
  const res = await api('/api/items', { method: 'POST', body });
  state.items = res.items;
  renderFilterBar();
  const bits = [`${res.total} item${res.total === 1 ? '' : 's'}`];
  if (state.filters.unread) bits.push('unread only');
  if (res.exact_matches !== undefined && state.text) bits.push(`${res.exact_matches} exact`);
  if (res.related_only) bits.push('no exact match — showing related');
  $('#list-meta').innerHTML = `<span>${esc(bits.join(' · '))}</span>
    <span class="meta-acts">
      <button class="link-btn ${state.filters.unread ? 'on' : ''}" id="btn-unread-only"
        title="Show only what you have not opened">unread only</button>
      <button class="link-btn" id="btn-mark-read" title="Clear every unread marker">mark all read</button>
    </span>`;
  refreshUnreadCount();

  $('#list').innerHTML = res.items.map((i) => rowHTML(i)).join('')
    || `<div class="empty-state"><p>Nothing here yet.</p></div>`;
}

/* ── collections ──────────────────────────────────────────── */
async function loadCollections() {
  const { collections } = await api('/api/collections');
  state.collections = collections;
  $('#collection-list').innerHTML = collections.map((c) => `
    <div class="nav-item coll-row ${String(c.id) === state.collection ? 'active' : ''}"
         data-collection="${c.id}">
      <span class="nav-label" title="${esc(c.description || '')}">${esc(c.name)}</span>
      <span class="nav-count">${c.count}</span>
      <button class="topic-btn ask-chip" data-ask-coll="${c.id}"
              title="Summarise or ask a question">ask</button>
      ${c.id !== 1 ? `<button class="topic-btn del" data-del-coll="${c.id}"
              title="Delete this collection">✕</button>` : ''}
    </div>`).join('') || '<div class="nav-item dim">None yet — press +</div>';
}

async function openCollection(id, text = '') {
  const q = text ? `&text=${encodeURIComponent(text)}` : '';
  const d = await api(`/api/collections/${id}/items?limit=100${q}`);
  state.collection = String(id);
  state.topic = null;
  $$('.topic-row, .nav-item[data-view]').forEach((n) => n.classList.remove('active'));
  $$('.coll-row').forEach((n) =>
    n.classList.toggle('active', n.dataset.collection === String(id)));
  state.items = d.items;
  $('#list-meta').textContent = `${d.collection.name} · ${d.total} saved`;
  $('#list').innerHTML = d.items.map((i) => rowHTML(i, {
    extra: `<button class="row-btn note" data-note="${i.id}"
              data-coll="${id}">${i.note ? 'edit note' : '+ note'}</button>`,
  })).join('') || `<div class="empty-state">
      <p>Nothing saved in <b>${esc(d.collection.name)}</b> yet.</p>
      <p class="dim">Star an article with ☆ and it lands here.</p></div>`;
  // The reader shows what you can do with the collection, not an empty pane:
  // summarise it, ask it, or resume a saved conversation.
  if (!text && d.items.length) await askCollection(id, '');
}

async function newCollection() {
  const name = prompt('Name this collection (e.g. "Vaccine trials")');
  if (!name) return;
  const r = await api('/api/collections', { method: 'POST', body: { name } });
  await loadCollections();
  return openCollection(r.id);
}

// Create a collection from the picker and put the open article in it.
// The picker draws its ticks from the article's own membership list, so
// that list has to be refreshed before re-rendering -- otherwise the new
// collection appears unticked and the whole thing reads as "nothing
// happened", which is exactly how it was reported.
async function saveToNewCollection() {
  const name = ($('#save-new')?.value || '').trim();
  if (!name) { toast('Give the collection a name first', 3000); return; }
  const r = await api('/api/collections', { method: 'POST', body: { name } });
  await api(`/api/collections/${r.id}/items`, { method: 'POST',
                                               body: { item_id: Number(state.active) } });
  await loadCollections();
  state.activeData = await api(`/api/items/${state.active}`);
  syncReaderStar((state.activeData.collections || []).length > 0);
  toast(`Saved to “${name}”`, 3000);
  return saveToPicker(state.active);
}

// Saving to somewhere other than Favourites, without leaving the article.
function saveToPicker(itemId) {
  const inC = (state.activeData?.collections) || [];
  openModal('Save this article', `
    <div class="form">
      ${state.collections.map((c) => `
        <label class="pick" style="padding:6px 0">
          <input type="checkbox" data-save-to="${c.id}" data-item="${itemId}"
                 ${inC.includes(c.id) ? 'checked' : ''}>
          <span><b>${esc(c.name)}</b>
            <span class="dim">${c.count} saved</span></span>
        </label>`).join('')}
      <label>New collection
        <input id="save-new" placeholder="name it and press Add"></label>
      <div class="form-btns">
        <button class="mini-btn" id="save-new-go">Add</button>
        <button class="primary-btn" id="scout-close">Done</button>
      </div>
    </div>`);
}

/* ── asking a collection ──────────────────────────────────── */
// The one place the model reads what a person chose rather than what a rule
// matched — so the selection is still deterministic, inside the collection.
async function askCollection(id, question, opts = {}) {
  const c = state.collections.find((x) => String(x.id) === String(id)) || {};
  $('#reader-actions').classList.add('hidden');
  if (String(state.thread.collection) !== String(id)) {
    state.thread = { collection: String(id), answers: [] };
  }
  if (!question && !opts.summary) {
    const { answers } = await api(`/api/collections/${id}/answers`);
    $('#reader').innerHTML = `
      <div class="art-body">
        <h2>“${esc(c.name)}”</h2>
        <div class="field-note">Answered from the ${c.count || 0} article(s)
          you saved here and nothing else. Your notes on <i>why</i> you saved
          each one are read as your priorities. Every finding cites the items
          it rests on.</div>
        <div class="form">
          <div class="form-btns">
            <button class="primary-btn" data-summarise="${id}">Summarise what I kept</button>
          </div>
          <label>Or ask it something
            <textarea id="ask-q" rows="2"
              placeholder="What do these say about third-party breach exposure?"></textarea></label>
          <div class="form-btns">
            <button class="primary-btn" data-ask-go="${id}">Ask</button>
          </div>
        </div>
        <div id="ask-out"></div>
        ${answers.length ? `<h3 class="sec-h">Saved conversations</h3>${
          conversations(answers).map((c) => `<div class="src-row"><div class="src-name">
            <b><span class="linkish" data-answer="${c.last.id}">${esc(c.root.question)}</span></b>
            <span>${esc((c.root.created_at || '').slice(0, 16))} ·
              ${c.turns} turn${c.turns === 1 ? '' : 's'} ·
              read ${c.root.stats?.read ?? 0} of ${c.root.stats?.collection_size ?? 0}${
              c.root.stats?.notes_used ? ` · ${c.root.stats.notes_used} note(s) used` : ''}</span>
          </div>
          <button class="mini-btn" data-del-answer="${c.last.id}">Remove</button>
          </div>`).join('')}` : ''}
      </div>`;
    return;
  }
  const history = state.thread.answers.map((a) => a.id);
  $('#reader').innerHTML = `<div class="art-body">
    ${threadHTML()}
    <h2>${opts.summary ? 'Summarising' : 'Reading'} “${esc(c.name)}”…</h2>
    <p class="dim">${opts.summary ? 'Newest first, your notes leading.'
      : 'Ranking what you saved against the question.'} Then one call to the
      local model. This takes a moment.</p></div>`;
  const d = opts.summary
    ? await api(`/api/collections/${id}/summary`, { method: 'POST' })
    : await api(`/api/collections/${id}/ask`, { method: 'POST',
                                                body: { question, history } });
  if (d.ok && d.id) state.thread.answers.push({ id: d.id, question: d.question,
                                                answer: (d.answer || {}).answer });
  renderAnswer(d);
}

// Group saved answers into conversations by their parent links: each root
// with its latest turn, so one row per conversation and one click resumes it.
function conversations(answers) {
  const byId = new Map(answers.map((a) => [a.id, a]));
  const rootOf = (a) => { let x = a, n = 0; while ((x.stats || {}).parent_id && byId.has(x.stats.parent_id) && n++ < 12) x = byId.get(x.stats.parent_id); return x; };
  const groups = new Map();
  for (const a of answers) {
    const r = rootOf(a);
    const g = groups.get(r.id) || { root: r, last: r, turns: 0 };
    g.turns += 1;
    if (a.id > g.last.id) g.last = a;
    groups.set(r.id, g);
  }
  return [...groups.values()].sort((x, y) => y.last.id - x.last.id);
}

// Earlier turns of the open conversation, stacked above the newest.
function threadHTML() {
  const prior = state.thread.answers.slice(0, -1);
  if (!prior.length) return '';
  return `<div class="thread">${prior.map((a) => `
    <div class="turn"><div class="turn-q">${esc(a.question)}</div>
      <div class="turn-a">${md(a.answer || '')}</div></div>`).join('')}</div>`;
}

// Reopening an answer restores the conversation it belongs to: walk the
// parent links back to the root, so "and which of those…" still has its
// "those", and the next question continues the thread rather than starting one.
async function openAnswer(answerId) {
  const chain = [];
  let id = answerId;
  for (let i = 0; i < 12 && id; i++) {
    const a = await api(`/api/answers/${id}`);
    chain.unshift(a);
    id = (a.stats || {}).parent_id;
  }
  const a = chain[chain.length - 1];
  state.thread = { collection: String(a.collection_id),
                   answers: chain.map((x) => ({ id: x.id, question: x.question,
                                                answer: (x.answer || {}).answer })) };
  renderAnswer({ ok: true, id: a.id, collection: a.collection,
                 collection_id: a.collection_id, question: a.question,
                 answer: a.answer, cited: a.cited, stats: a.stats,
                 model: a.model });
}

function renderAnswer(d) {
  $('#reader-actions').classList.add('hidden');
  if (!d.ok) {
    $('#reader').innerHTML = `<div class="art-body">
      <button class="mini-btn" data-ask-coll="${d.collection_id || 1}">← Back</button>
      <div class="disclaimer">${esc(d.reason || 'No answer.')}
      ${d.note ? '<br>' + esc(d.note) : ''}</div></div>`;
    return;
  }
  const a = d.answer || {};
  const st = d.stats || {};
  if (d.id) setBack(`answer: ${d.question}`, () => openAnswer(d.id));
  $('#reader').innerHTML = `
    <div class="art-body">
      <button class="mini-btn" data-ask-coll="${d.collection_id}">← Ask another</button>
      <h2>${esc(d.question)}</h2>
      ${state.thread.answers.length > 1 && String(state.thread.collection) === String(d.collection_id)
        ? threadHTML() : ''}
      <div class="funnel">
        <span>${esc(d.collection)} · <b>${st.collection_size ?? 0}</b> saved</span>
        <span>read <b>${st.read ?? 0}</b></span>
        <span>notes used <b>${st.notes_used ?? 0}</b></span>
        <span>${st.escalated ? 'escalated to full text' : '1 model call'}</span>
      </div>
      ${a.answered === false ? `<div class="disclaimer">
        <b>The saved articles do not answer this.</b> What follows is what
        they do say, and what is missing.</div>` : ''}
      <div class="answer-body">${md(a.answer || '')}</div>
      ${(a.findings || []).length ? `<h3 class="sec-h">Findings</h3>${
        a.findings.map((f) => `<div class="obs">
          <div>${esc(f.statement)}</div>
          <div class="dim">evidence: ${f.item_ids.map((i) =>
            `<span class="cite" data-item="${i}">${i}</span>`).join(' ')}</div>
        </div>`).join('')}` : ''}
      ${(a.gaps || []).length ? `<h3 class="sec-h">What is missing</h3><ul>${
        a.gaps.map((g) => `<li>${esc(g)}</li>`).join('')}</ul>` : ''}
      <h3 class="sec-h">Sources cited</h3>
      ${(d.cited || []).map((ci) => `
        <div class="src-row"><div class="src-name">
          <b><span class="linkish" data-item="${ci.id}">[${ci.id}] ${esc(ci.headline)}</span></b>
          <span>${esc(ci.source)} · ${esc(ci.published)} ·
            <a href="${esc(ci.url)}" target="_blank" rel="noopener">open original ↗</a></span>
        </div></div>`).join('') || '<p class="dim">Nothing survived citation-checking.</p>'}
      <div class="dim" style="margin-top:12px">${esc(d.model || '')}${
        st.uncited_dropped ? ` · ${st.uncited_dropped} uncited finding(s) dropped` : ''}</div>
      ${d.collection_id ? `<div class="form" style="margin-top:18px">
        <label>Follow up — it remembers this conversation
          <textarea id="ask-q" rows="2" placeholder="And which of those were in Europe?"></textarea></label>
        <div class="form-btns">
          <button class="primary-btn" data-ask-go="${d.collection_id}">Ask</button>
          <button class="mini-btn" data-ask-coll="${d.collection_id}">Start over</button>
        </div></div>` : ''}
    </div>`;
}

/* ── one row, everywhere ──────────────────────────────────── */
// Keep and discard live on the row itself. A judgement you have to open an
// article to record is a judgement most people will not bother recording.
function rowHTML(i, opts = {}) {
  const saved = (i.collections || []).length > 0;
  const chips = Object.entries(i).filter(([k, v]) =>
      Array.isArray(v) && v.length && !['claims', 'key_points', 'orgs',
        'numbers', 'source_tags', 'entities', 'collections'].includes(k))
    .flatMap(([, v]) => v.slice(0, 2))
    .slice(0, 4).map((t) => `<span class="chip">${esc(t.replace(/_/g, ' '))}</span>`)
    .join('');
  const fresh = i.fetched_at && new Date(i.fetched_at) > state.lastSeen;
  return `<div class="row ${i.unread ? 'unread' : ''}" data-id="${i.id}">
      <div class="row-top">
        ${i.unread ? '<span class="unread-dot" title="Unread"></span>' : ''}
        ${fresh ? '<span class="new-pill">new</span>' : ''}
        <span class="row-src">${esc(i.source_name || '')}</span>
        <span class="${i.published_at ? '' : 'undated'}">${esc(whenOf(i))}</span>
        <span class="impact">${i.impact_score ? Math.round(i.impact_score) : ''}</span>
        <span class="row-acts">
          <button class="row-btn star ${saved ? 'on' : ''}" data-star="${i.id}"
                  title="${saved ? 'Saved — click to unsave' : 'Save to Favourites'}"
            >${saved ? '★' : '☆'}</button>
          <button class="row-btn drop" data-dismiss="${i.id}"
                  title="Not relevant — remove it and stop it coming back">✕</button>
        </span>
      </div>
      <div class="row-title">${esc(i.headline || i.title)}</div>
      ${state.rendition === 'headline' ? '' :
        `<div class="row-sum ${state.rendition === 'brief' ? 'clamp' : ''}">${
          esc(i.summary || i.excerpt || '')}</div>`}
      ${state.rendition === 'full' && i.so_what
        ? `<div class="row-so">${esc(i.so_what)}</div>` : ''}
      ${state.rendition === 'full' && (i.key_points || []).length
        ? `<ul class="row-points">${i.key_points.slice(0, 3).map(
            (k) => `<li>${esc(k)}</li>`).join('')}</ul>` : ''}
      ${i.note ? `<div class="row-note">${esc(i.note)}</div>` : ''}
      ${opts.extra || ''}
      <div class="row-chips">${chips}</div>
    </div>`;
}

/* ── reader ───────────────────────────────────────────────── */
async function openItem(id) {
  state.active = id; state.showOriginal = false;
  $$('.row').forEach((r) => {
    r.classList.toggle('active', r.dataset.id == id);
    if (r.dataset.id == id && r.classList.contains('unread')) {
      r.classList.remove('unread');
      r.querySelector('.unread-dot')?.remove();
      const it = state.items.find((x) => String(x.id) === String(id));
      if (it) it.unread = false;
    }
  });
  const it = await api(`/api/items/${id}`);
  refreshUnreadCount();
  state.activeData = it;
  $('#reader-actions').classList.remove('hidden');
  $('#btn-open').href = it.url;
  syncReaderStar((it.collections || []).length > 0);
  renderReader(it);
}

function renderReader(it) {
  const foreign = it.lang && it.lang !== 'en';
  const body = state.showOriginal ? (it.text || '') : (it.text_en || it.text || '');
  const meta = [it.source_name, whenOf(it), it.item_type,
                it.impact_score ? `impact ${Math.round(it.impact_score)}` : '']
    .filter(Boolean).map((m) => `<span>${esc(m)}</span>`).join('<span>·</span>');
  const claims = (it.claims && typeof it.claims === 'string' ? JSON.parse(it.claims) : it.claims) || [];
  $('#reader').innerHTML = `
    ${backBar()}
    <div class="art-head">
      <div class="art-title">${esc(it.headline || it.title)}</div>
      <div class="art-source"><a href="${esc(it.url)}" target="_blank"
        rel="noopener">${esc(it.url)}</a></div>
      ${it.title && it.title !== it.headline ? `<div class="art-orig">Original title: ${esc(it.title)}</div>` : ''}
      <div class="art-meta">${meta}</div>
    </div>
    <div class="art-body">
      <div id="abstract-slot"></div>
      ${(it.key_points || []).length ? `<ul>${it.key_points.map((k) => `<li>${esc(k)}</li>`).join('')}</ul>` : ''}
      ${it.so_what ? `<div class="callout"><h4>Why it matters</h4><p>${esc(it.so_what)}</p></div>` : ''}
      ${claims.length ? `<div class="callout"><h4>Checkable claims</h4><ul>${
        claims.map((c) => `<li>${esc(c)}</li>`).join('')}</ul></div>` : ''}
      ${foreign ? `<div class="trans-bar"><span>${state.showOriginal ? 'Showing the'
        : 'Translated from'} <b>${esc(it.lang)}</b> original.</span></div>` : ''}
      ${body ? `<div class="art-text">${esc(body)}</div>` : ''}
    </div>`;
  $('#reader').scrollTop = 0;
  loadAbstract(it);
}

/* ── abstract ─────────────────────────────────────────────── */
// A full paragraph, written by the local model, always in the reader's
// chosen language. Fetched after the article paints so the reader is
// never staring at a blank pane while a model thinks.
async function loadAbstract(it) {
  const slot = $('#abstract-slot');
  if (!slot) return;
  const langName = (state.languages.find((l) => l.code === state.lang) || {}).name
                   || state.lang;
  // Placeholder lines the size of the finished paragraph: they say "working"
  // without a spinner's fake urgency, and the article below does not jump
  // when the real text replaces them. Held back briefly so a cached
  // abstract — which arrives in a millisecond — does not flash a skeleton.
  const skeleton = setTimeout(() => {
    if (state.active != it.id) return;
    slot.innerHTML = `
      <div class="abstract-head"><span>Abstract</span>
        <span class="meta">${esc(langName)} · writing…</span></div>
      <div class="abstract-skeleton" aria-label="writing the abstract">
        ${[100, 97, 99, 94, 62].map((w) =>
          `<span style="width:${w}%"></span>`).join('')}
      </div>`;
  }, 180);
  try {
    const d = await api(`/api/items/${it.id}/abstract?lang=${encodeURIComponent(state.lang)}`);
    clearTimeout(skeleton);
    if (state.active != it.id) return;           // reader already moved on
    if (d.ok) {
      slot.innerHTML = `
        <div class="abstract-head"><span>Abstract</span>
          <span class="meta">${esc(langName)} · ${d.words} words${
            d.cached ? ' · cached' : ''}</span></div>
        <p class="abstract">${esc(d.text)}</p>`;
    } else {
      // Never a dead end: the stored summary is still worth reading.
      slot.innerHTML = `
        <div class="abstract-head"><span>Summary</span>
          <span class="meta">no abstract in ${esc(langName)} yet</span></div>
        <p class="abstract abstract-fallback">${esc(d.fallback || '')}</p>
        ${d.reason ? `<div class="dim">${esc(d.reason)}</div>` : ''}`;
    }
  } catch (e) {
    clearTimeout(skeleton);
    slot.innerHTML = `<div class="dim">Could not write an abstract: ${esc(e.message)}</div>`;
  }
}

/* ── subscriptions ────────────────────────────────────────── */
async function showSubscriptions() {
  const [{ subscriptions }, cap, embed] = await Promise.all([
    api('/agentfeed/subscriptions'), api('/.well-known/agent-feed'),
    api('/agentfeed/embed'),
  ]);
  $('#reader-actions').classList.add('hidden');
  $('#reader').innerHTML = `
    <div class="art-body">
      <h2>Agent subscriptions</h2>
      <p>Any agent that speaks the AgentFeed protocol can subscribe to this feed and stay current
      without you forwarding anything. Point it at the discovery document:</p>
      <div class="kv">
        <dt>Discovery</dt><dd>${esc(cap.endpoints.capabilities)}</dd>
        <dt>Subscribe</dt><dd>POST ${esc(cap.endpoints.subscribe)}</dd>
        <dt>Protocol</dt><dd>${esc(cap.protocol)}</dd>
        <dt>Renditions</dt><dd>${cap.renditions.join(', ')}</dd>
        <dt>Max tokens/sync</dt><dd>${cap.max_tokens_per_sync}</dd>
      </div>
      <div class="form-btns" style="margin:16px 0">
        <button class="primary-btn" id="btn-new-sub">Create a subscription</button>
      </div>
      <div id="sub-form"></div>
      <h3 class="sec-h">Put this on your site</h3>
      <div class="field-note">The orange RSS square told a reader "this page
        has a feed". This is the same affordance for agents: the link tag is
        what they read, the button is for the human who adds it.</div>
      ${embed.warning ? `<div class="disclaimer">
        <b>Not public yet.</b> ${esc(embed.warning)}</div>` : ''}
      <div class="embed-row">
        <img src="/agentfeed/button.svg" width="132" height="32"
             alt="Subscribe with an agent">
        <button class="mini-btn" id="btn-copy-embed">Copy the snippet</button>
      </div>
      <pre class="embed-snippet" id="embed-snippet">${esc(embed.link_tag)}
${esc(embed.button_html)}</pre>

      <h3 style="margin-top:22px">Active (${subscriptions.length})</h3>
      ${subscriptions.map((s) => `
        <div class="sub-card">
          <h4>${esc(s.spec.name)} <span class="tokens">${s.spec.max_tokens} tok</span></h4>
          <div class="dim">${esc(JSON.stringify(s.spec.facets))}</div>
          <div class="dim">renditions: ${s.spec.renditions.join(', ')} ·
            delivered ${s.items_delivered} · cursor ${esc(s.cursor || '0')}</div>
          <code>${esc(cap.endpoints.sync.replace('{id}', s.id))}</code>
          <div style="margin-top:8px">
            <button class="mini-btn" data-sync="${s.id}">Test sync</button>
            <button class="mini-btn" data-unsub="${s.id}">Unsubscribe</button>
          </div>
          <pre class="dim" id="out-${s.id}" style="white-space:pre-wrap;margin-top:8px"></pre>
        </div>`).join('') || '<p class="dim">None yet.</p>'}
    </div>`;
}

// The button used to open a read-only list: the one thing the whole
// protocol exists for could only be done with curl.
function showSubForm() {
  const langs = state.languages.map((l) =>
    `<option value="${esc(l.code)}" ${l.code === state.lang ? 'selected' : ''}>${esc(l.name)}</option>`).join('');
  $('#sub-form').innerHTML = `
    <div class="form" data-form="sub">
      <label>Name<input id="sf-name" placeholder="salmon disease watch"></label>
      <div class="pick-wrap">${facetPicker({})}</div>
      <label>Organisations <span class="opt">optional</span>
        <input id="sf-entities" placeholder="nvidia, tsmc"></label>
      <label>Must mention <span class="opt">optional — the cheap filter that runs first</span>
        <input id="sf-include" placeholder="nvidia"></label>
      <label>Renditions
        <span class="pick-vals">
          ${['headline', 'brief', 'abstract', 'full', 'original'].map((r) => `
            <label class="pick"><input type="checkbox" data-rend value="${r}"
              ${r === 'brief' ? 'checked' : ''}><span>${r}</span></label>`).join('')}
        </span></label>
      <label class="inline">Abstract language
        <select id="sf-lang">${langs}</select></label>
      <label>Agent test <span class="opt">optional — judged only on what the clauses above admitted</span>
        <textarea id="sf-agent" rows="2" placeholder="only items substantively about NVIDIA"></textarea></label>
      <label class="inline">Token budget per sync
        <input id="sf-tokens" type="number" value="8000" min="500" step="500"></label>
      <label>Webhook URL <span class="opt">optional — leave blank for pull</span>
        <input id="sf-webhook" placeholder="https://your-agent.example/agentfeed"></label>
      <div class="form-btns">
        <button class="mini-btn" id="btn-sub-preview">Preview what it would deliver</button>
        <button class="primary-btn" id="btn-sub-create">Create</button>
      </div>
      <div id="sf-out" class="form-out"></div>
    </div>`;
}

function subSpecFromForm() {
  const facets = readFacetPicker();
  const renditions = $$('[data-rend]').filter((c) => c.checked).map((c) => c.value);
  const webhook = $('#sf-webhook').value.trim();
  const agent = $('#sf-agent').value.trim();
  return {
    name: $('#sf-name').value.trim() || 'untitled',
    facets, entities: csv($('#sf-entities').value),
    include: csv($('#sf-include').value),
    renditions: renditions.length ? renditions : ['brief'],
    render_language: $('#sf-lang').value,
    agent_filter: agent,
    max_tokens: Number($('#sf-tokens').value) || 8000,
    delivery: webhook ? 'webhook' : 'pull',
    webhook_url: webhook,
  };
}

async function previewSub() {
  const out = $('#sf-out');
  out.innerHTML = '<span class="dim">selecting…</span>';
  const env = await api('/agentfeed/preview', { method: 'POST', body: subSpecFromForm() });
  out.innerHTML = `
    <div class="funnel">
      <span>would deliver <b>${env.budget.items_returned}</b></span>
      <span><b>${env.budget.items_available}</b> available</span>
      <span>${env.budget.tokens_returned}/${env.budget.tokens_requested} tokens</span>
    </div>
    ${(env.notices || []).map((n) => `<div class="disclaimer">${esc(n)}</div>`).join('')}
    ${env.items.slice(0, 6).map((i) => `<div class="src-row"><div class="src-name">
        <b>${esc(i.renditions.headline?.text || i.renditions.brief?.text || '')}</b>
        <span><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.url)}</a></span>
      </div></div>`).join('') || '<p class="dim">Nothing matches this yet.</p>'}`;
}

async function createSub() {
  const out = $('#sf-out');
  out.innerHTML = '<span class="dim">creating…</span>';
  const d = await api('/agentfeed/subscriptions', { method: 'POST', body: subSpecFromForm() });
  // The secret is shown once, so it gets its own box rather than a toast.
  out.innerHTML = `
    <div class="callout">
      <h4>Subscription created</h4>
      <p>Point the agent at this URL:</p>
      <code>${esc(d.sync_url)}</code>
      <p>Webhook signing secret — shown once:</p>
      <code>${esc(d.secret)}</code>
      <p class="dim">${esc(d.note)}</p>
    </div>
    ${(d.notices || []).map((n) => `<div class="disclaimer">${esc(n)}</div>`).join('')}`;
  const card = out.closest('.form');
  if (card) card.dataset.done = '1';
  await boot0();
}

async function showSignals() {
  const { analyses } = await api('/api/analyses?limit=30');
  $('#reader-actions').classList.add('hidden');
  state.back = null;
  $('#reader').innerHTML = `
    <div class="art-body">
      <h2>Market signals</h2>
      <div class="field-note">
        Evidence-linked reads of what the coverage says, each ending in a
        call. Saved, so you can follow a citation and come back. Generated
        by a local model from news alone — not investment advice.
      </div>
      <button class="mini-btn" id="btn-new-analysis">New analysis</button>
      <h3 class="sec-h">Saved (${analyses.length})</h3>
      ${analyses.map((a) => `
        <div class="src-row">
          <div class="src-name">
            <b><span class="linkish" data-analysis="${a.id}">${esc(a.subject)}</span></b>
            <span>${a.stats?.call ? `<span class="call-chip ${esc(a.stats.call)}">${
                esc(CALL_LABEL[a.stats.call] || a.stats.call)}</span> · ` : ''}${
              esc((a.created_at || '').slice(0, 16))} ·
              ${a.stats?.items_considered ?? 0} items · last ${a.days} days
              ${a.stats?.stance_conflict ? '· <span class="warn">disputed</span>' : ''}
              ${a.model ? '· ' + esc(a.model) : ''}</span>
          </div>
          <button class="mini-btn" data-pin="${a.id}" data-pinned="${a.pinned}">
            ${a.pinned ? '★' : '☆'}</button>
          <button class="mini-btn" data-del-analysis="${a.id}">Remove</button>
        </div>`).join('') || '<p class="dim">None yet.</p>'}
    </div>`;
}

async function newAnalysis() {
  const entity = prompt('Analyse coverage for which entity? (blank = whole feed)');
  if (entity === null) return;
  $('#reader').innerHTML = `<div class="art-body"><h2>Analysing…</h2>
    <p class="dim">Selecting deterministically, then asking the local model.
    This takes a minute.</p></div>`;
  try {
    const d = await api('/api/signals', { method: 'POST',
      body: { entity, days: 120, limit: 30 } });
    if (d.id) return openAnalysis(d.id);
    renderAnalysis(d);
  } catch (e) {
    $('#reader').innerHTML = `<div class="art-body">
      <div class="disclaimer">Could not analyse: ${esc(e.message)}</div></div>`;
  }
}

async function openAnalysis(id) {
  const d = await api(`/api/analyses/${id}`);
  renderAnalysis({ ...d, report: d.report, cited_items: d.cited,
                   items_considered: d.stats?.items_considered ?? 0,
                   direction_counts: d.stats?.direction_counts ?? {} });
}

const CALL_LABEL = { buy: 'Buy', accumulate: 'Accumulate', hold: 'Hold',
                     reduce: 'Reduce', sell: 'Sell' };

// The call and the disclaimer are rendered by one function, from one
// object, so there is no arrangement of this UI in which a reader sees the
// call without seeing what it is worth.
function renderStance(d) {
  const st = d.report && d.report.stance;
  if (!st) {
    return `<div class="disclaimer"><b>Not financial advice.</b>
      ${esc(d.disclaimer || '')}</div>`;
  }
  const pct = Math.round((st.confidence || 0) * 100);
  return `
    <div class="stance ${esc(st.call)}">
      <div class="stance-head">
        <span class="stance-call">${esc(CALL_LABEL[st.call] || st.call)}</span>
        <span class="stance-meta">over ${esc(st.horizon || '')} ·
          confidence ${pct}%</span>
      </div>
      <div class="conf-bar"><span style="width:${pct}%"></span></div>
      <p>${esc(st.rationale || '')}</p>
      ${st.case_against ? `<div class="stance-against">
        <b>The case against:</b> ${esc(st.case_against)}</div>` : ''}
      ${(st.supporting || []).length ? `<div class="dim">rests on: ${
        st.supporting.map((i) => `<span class="cite" data-item="${i}">${i}</span>`)
          .join(' ')}</div>` : ''}
      ${d.stance_conflict ? `<div class="stance-conflict">
        <b>This call disagrees with its own evidence.</b>
        ${esc(d.stance_conflict)}</div>` : ''}
      <div class="disclaimer"><b>Not financial advice.</b>
        ${esc(d.disclaimer || '')}</div>
    </div>`;
}

function renderAnalysis(d) {
  const r = d.report;
  const saved = d.id ? `<span class="tokens">saved #${d.id}</span>` : '';
  // Coming back here is one click, from anywhere the citations lead.
  if (d.id) setBack(`analysis: ${d.subject}`, () => openAnalysis(d.id));
  $('#reader-actions').classList.add('hidden');
  $('#reader').innerHTML = `
    <div class="art-body">
      <button class="mini-btn" id="btn-all-analyses">← All analyses</button>
      <h2>${esc(d.subject)} ${saved}</h2>
      <div class="dim">${d.items_considered} items · last ${d.days} days${
        d.model ? ' · ' + esc(d.model) : ''}</div>
      ${renderStance(d)}
      ${!r || !r.observations ? `<p>${esc(d.error || d.note || 'No analysis.')}</p>` : `
        <p>${esc(r.summary)}</p>
        <h3 class="sec-h">Observations</h3>
        ${r.observations.map((o) => `
          <div class="obs ${esc(o.direction)}">
            <div class="obs-dir">${esc(o.direction)} · strength ${Number(o.strength).toFixed(1)}</div>
            <div>${esc(o.statement)}</div>
            <div class="dim">evidence: ${o.item_ids.map((i) =>
              `<span class="cite" data-item="${i}">${i}</span>`).join(' ')}</div>
          </div>`).join('')}
        ${r.contradictions?.length ? `<h3 class="sec-h">Where sources disagree</h3><ul>${
          r.contradictions.map((c) => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}
        ${r.watch_next?.length ? `<h3 class="sec-h">What would change this</h3><ul>${
          r.watch_next.map((w) => `<li>${esc(w)}</li>`).join('')}</ul>` : ''}
        <div class="callout"><h4>Coverage</h4><p>${esc(r.coverage_note || '')}</p></div>`}
      <h3 class="sec-h">Sources cited</h3>
      ${(d.cited_items || []).map((ci) => `
        <div class="src-row"><div class="src-name">
          <b><span class="linkish" data-item="${ci.id}">[${ci.id}] ${esc(ci.headline)}</span></b>
          <span>${esc(ci.source)} · ${esc(ci.published)} ·
            <a href="${esc(ci.url)}" target="_blank" rel="noopener">open original ↗</a></span>
        </div></div>`).join('') || '<p class="dim">No citations survived verification.</p>'}
    </div>`;
}

/* ── events ───────────────────────────────────────────────── */
// Nothing here is allowed to fail invisibly. Every action below awaits the
// backend, and an unhandled rejection used to look exactly like a dead
// button -- the server said "no such subscription" and the page did not
// move.
document.addEventListener('click', (e) => {
  handleClick(e).catch((err) => toast('That did not work: ' + err.message, 6000));
});

async function handleClick(e) {
  const unf = e.target.closest('[data-unfilter]');
  if (unf) return toggleFilter(unf.dataset.unfilter, unf.dataset.value);
  if (e.target.closest('#btn-unread-only')) {
    state.filters.unread = !state.filters.unread;
    if (!state.filters.unread) delete state.filters.unread;
    return loadList();
  }
  if (e.target.closest('#btn-mark-read')) {
    const r = await api('/api/items/mark-read', { method: 'POST', body: { item_ids: [] } });
    toast(`Marked ${r.marked} as read`, 3000);
    return loadList();
  }
  if (e.target.closest('#btn-clear-filters')) {
    state.filters = {};
    await loadList();
    return loadFacetCounts();
  }

  // --- keeping and discarding ---
  const star = e.target.closest('[data-star]');
  if (star) {
    e.stopPropagation();
    const d = await api(`/api/items/${star.dataset.star}/star`, { method: 'POST' });
    star.classList.toggle('on', d.starred);
    star.textContent = d.starred ? '★' : '☆';
    star.title = d.starred ? 'Saved — click to unsave' : 'Save to Favourites';
    await loadCollections();
    if (state.collection) return openCollection(state.collection);
    return;
  }
  const drop = e.target.closest('[data-dismiss]');
  if (drop) {
    e.stopPropagation();
    const id = drop.dataset.dismiss;
    const reason = prompt('Remove this article. Why is it irrelevant?\n\n'
      + 'The reason is only for your own record. The article is deleted and '
      + 'will not come back on the next fetch.', '');
    if (reason === null) return;
    const d = await api(`/api/items/${id}/dismiss`, { method: 'POST',
                                                     body: { reason } });
    const row = drop.closest('.row');
    if (row) row.remove();
    if (String(state.active) === String(id)) clearReader();
    state.lastDismissed = d.url_key;
    toast(`Removed “${(d.title || '').slice(0, 40)}” · click Status to undo`, 6000);
    return;
  }
  const note = e.target.closest('[data-note]');
  if (note) {
    e.stopPropagation();
    const cur = state.items.find((x) => String(x.id) === note.dataset.note);
    const text = prompt('Why did you save this? The note is shown to the '
                        + 'model when you ask a question of this collection.',
                        (cur && cur.note) || '');
    if (text === null) return;
    await api(`/api/collections/${note.dataset.coll}/items/${note.dataset.note}/note`,
              { method: 'POST', body: { item_id: Number(note.dataset.note), note: text } });
    return openCollection(note.dataset.coll);
  }

  // --- collections ---
  // Buttons that live inside a collection row must be checked before the
  // row itself, or closest('[data-collection]') swallows them and the ?
  // and ✕ just open the collection.
  if (e.target.closest('#btn-new-collection')) return newCollection();
  const askC = e.target.closest('[data-ask-coll]');
  if (askC) {
    e.stopPropagation();
    state.thread = { collection: String(askC.dataset.askColl), answers: [] };
    return askCollection(askC.dataset.askColl, '');
  }
  const sm = e.target.closest('[data-summarise]');
  if (sm) return askCollection(sm.dataset.summarise, '', { summary: true });
  const askGo = e.target.closest('[data-ask-go]');
  if (askGo) {
    const q = $('#ask-q').value.trim();
    if (!q) { toast('Ask something first', 3000); return; }
    return askCollection(askGo.dataset.askGo, q);
  }
  const ans = e.target.closest('[data-answer]');
  if (ans) return openAnswer(ans.dataset.answer);
  const delAns = e.target.closest('[data-del-answer]');
  if (delAns) {
    if (!confirm('Remove this saved answer?')) return;
    await api(`/api/answers/${delAns.dataset.delAnswer}`, { method: 'DELETE' });
    return askCollection(state.collection || 1, '');
  }
  const delColl = e.target.closest('[data-del-coll]');
  if (delColl) {
    e.stopPropagation();
    const c = state.collections.find((x) => String(x.id) === delColl.dataset.delColl);
    if (!confirm(`Delete the collection “${c ? c.name : ''}”?\n\n`
                 + 'The articles themselves stay in the corpus.')) return;
    await api(`/api/collections/${delColl.dataset.delColl}`, { method: 'DELETE' });
    state.collection = null;
    await loadCollections();
    clearReader();
    return loadList();
  }
  const coll = e.target.closest('[data-collection]');
  if (coll) return openCollection(coll.dataset.collection);
  const saveTo = e.target.closest('[data-save-to]');
  if (saveTo) {
    const cid = saveTo.dataset.saveTo;
    const iid = Number(saveTo.dataset.item);
    if (saveTo.checked) {
      await api(`/api/collections/${cid}/items`, { method: 'POST',
                                                  body: { item_id: iid } });
    } else {
      await api(`/api/collections/${cid}/items/${iid}`, { method: 'DELETE' });
    }
    await loadCollections();
    if (state.activeData) state.activeData = await api(`/api/items/${iid}`);
    return;
  }
  if (e.target.closest('#save-new-go')) return saveToNewCollection();
  if (e.target.closest('#btn-star')) {
    const d = await api(`/api/items/${state.active}/star`, { method: 'POST' });
    syncReaderStar(d.starred);
    await loadCollections();
    return;
  }
  if (e.target.closest('#btn-save-to')) return saveToPicker(state.active);
  if (e.target.closest('#btn-drop')) {
    const reason = prompt('Remove this article. Why is it irrelevant?', '');
    if (reason === null) return;
    await api(`/api/items/${state.active}/dismiss`, { method: 'POST',
                                                     body: { reason } });
    clearReader();
    toast('Removed. It will not come back on the next fetch.', 5000);
    return loadList();
  }

  const sc = e.target.closest('[data-scout]');
  if (sc) { e.stopPropagation(); return scoutSources(sc.dataset.scout); }
  const fs = e.target.closest('[data-find-sources]');
  if (fs) {
    e.stopPropagation();
    return findSourcesForText(fs.dataset.findSources, fs.dataset.fromSources === '1');
  }
  if (e.target.closest('#scout-add')) return adoptScouted();
  if (e.target.closest('#confirm-add-yes')) return addConfirmedSource();
  if (e.target.closest('#scout-close') || e.target.closest('#modal-close')
      || e.target.closest('#confirm-add-cancel')
      || e.target.id === 'modal') return closeModal();

  const dig = e.target.closest('[data-digest]');
  if (dig) { e.stopPropagation(); return showDigest(dig.dataset.digest, dig.dataset.period || 'day'); }
  if (e.target.closest('#btn-new-topic')) return showTopicForm();
  const et = e.target.closest('[data-edit-topic]');
  if (et) { e.stopPropagation(); return showTopicForm(et.dataset.editTopic); }
  if (e.target.closest('#tf-save')) return saveTopic();
  if (e.target.closest('#tf-follow')) return followSubject();
  if (e.target.closest('#tf-try')) return tryAgentTest();
  const dropTerm = e.target.closest('[data-drop-term]');
  if (dropTerm) {
    const [kind, value] = dropTerm.dataset.dropTerm.split('|');
    state.draft[kind] = (state.draft[kind] || []).filter((x) => x !== value);
    dropTerm.remove();
    return;
  }
  const dt = e.target.closest('[data-del-topic]');
  if (dt) {
    e.stopPropagation();
    const id = dt.dataset.delTopic;
    const t = state.topics.find((x) => String(x.id) === String(id));
    if (!confirm(`Remove the topic “${t ? t.name : id}”?\n\n`
                 + 'Only the topic goes: every article it collected stays in '
                 + 'the corpus, and any source you added for it keeps '
                 + 'fetching.')) return;
    await api(`/api/topics/${id}`, { method: 'DELETE' });
    await loadTopics();
    toast(`Removed “${t ? t.name : id}”`, 3000);
    // Only move the reader if you were actually in the topic being removed.
    if (String(state.topic) === String(id)) {
      state.topic = null;
      clearReader();
      return loadList();
    }
  }
  if (e.target.closest('#btn-add-site')) return addSite();
  if (e.target.closest('#btn-src-add')) return addSite('#src-add', '#src-add-out');
  if (e.target.closest('#btn-sources')) return showSources();
  if (e.target.closest('#btn-status')) return showStatus();
  if (e.target.closest('#btn-signals')) {
    $$('.nav-item').forEach((n) => n.classList.remove('active'));
    return showSignals();
  }
  if (e.target.closest('#btn-agents')) {
    $$('.nav-item').forEach((n) => n.classList.remove('active'));
    return showSubscriptions();
  }
  if (e.target.closest('#btn-pack')) {
    const name = $('#pack-pick').value;
    if (!confirm(`Switch this feed to the "${name}" pack?`)) return;
    const r = await api('/api/domain', { method: 'POST', body: { name } });
    toast(r.note || 'Switched', 8000);
    state.filters = {};
    state.domain = await api('/api/domain');
    renderFacetNav(state.domain);
    await boot0();
    await loadList();
    return showStatus();
  }

  const tgl = e.target.closest('[data-toggle-src]');
  if (tgl) {
    await api(`/api/sources/${tgl.dataset.toggleSrc}?enabled=${tgl.checked}`,
              { method: 'PATCH' });
    return;
  }
  const del = e.target.closest('[data-del-src]');
  if (del) {
    if (!confirm('Remove this source? Articles already collected are kept.')) return;
    await api(`/api/sources/${del.dataset.delSrc}`, { method: 'DELETE' });
    return showSources();
  }

  const more = e.target.closest('[data-more]');
  if (more) {
    const k = more.dataset.more;
    state.facetOpen[k] = !state.facetOpen[k];
    return loadFacetCounts();
  }

  const nav = e.target.closest('.nav-item');
  if (nav) {
    if (nav.dataset.topic) {
      state.collection = null;
      $$('.nav-item').forEach((n) => n.classList.remove('active'));
      nav.classList.add('active');
      return openTopic(nav.dataset.topic);
    }
    if (nav.dataset.view) {
      state.view = nav.dataset.view;
      state.topic = null; state.collection = null;
      $$('.nav-item').forEach((n) => n.classList.remove('active'));
      nav.classList.add('active');
      if (state.view === 'subs') return showSubscriptions();
      if (state.view === 'signals') return showSignals();
      // Leaving a panel used to leave its page sitting in the reader while
      // you browsed a completely different list.
      clearReader();
      return loadList();
    }
    if (nav.dataset.facet) {
      toggleFilter(nav.dataset.facet, nav.dataset.value);
      return;
    }
  }
  const row = e.target.closest('.row');
  if (row) return openItem(row.dataset.id);
  const cite = e.target.closest('.cite');
  if (cite) return openItem(cite.dataset.item);

  if (e.target.closest('#btn-orig')) {
    state.showOriginal = !state.showOriginal;
    if (state.activeData) renderReader(state.activeData);
    return;
  }
  if (e.target.closest('#btn-signal')) return showSignals();
  if (e.target.closest('#btn-new-analysis')) return newAnalysis();
  if (e.target.closest('#btn-all-analyses')) return showSignals();
  if (e.target.closest('#btn-back')) { const b = state.back; state.back = null; return b.fn(); }

  const an = e.target.closest('[data-analysis]');
  if (an) return openAnalysis(an.dataset.analysis);
  const pin = e.target.closest('[data-pin]');
  if (pin) {
    await api(`/api/analyses/${pin.dataset.pin}/pin?pinned=${pin.dataset.pinned !== 'true'}`,
              { method: 'POST' });
    return showSignals();
  }
  const dela = e.target.closest('[data-del-analysis]');
  if (dela) {
    if (!confirm('Remove this saved analysis?')) return;
    await api(`/api/analyses/${dela.dataset.delAnalysis}`, { method: 'DELETE' });
    return showSignals();
  }
  if (e.target.closest('#btn-copy-embed')) {
    await navigator.clipboard.writeText($('#embed-snippet').textContent);
    toast('Snippet copied — paste it into your <head>', 4000);
    return;
  }
  if (e.target.closest('#btn-new-sub')) return showSubForm();
  if (e.target.closest('#btn-sub-preview')) return previewSub();
  if (e.target.closest('#btn-sub-create')) return createSub();

  if (e.target.closest('#btn-refresh')) {
    const r = await api('/api/run', { method: 'POST', body: {} });
    if (!r.ok) { toast(r.reason || 'Could not start a run', 5000); return pollRun(); }
    toast('Fetching…');
    return pollRun();
  }
  const sync = e.target.closest('[data-sync]');
  if (sync) {
    const id = sync.dataset.sync;
    const env = await api(`/agentfeed/subscriptions/${id}/sync`);
    $(`#out-${id}`).textContent =
      `${env.budget.items_returned} items · ${env.budget.tokens_returned}/${env.budget.tokens_requested} tokens`
      + ` · ${env.budget.items_available} available · cursor ${env.cursor}\n`
      + env.items.slice(0, 3).map((i) =>
          '  · ' + (i.renditions.headline?.text || i.renditions.brief?.text || '').slice(0, 80)).join('\n');
    return;
  }
  const un = e.target.closest('[data-unsub]');
  if (un) {
    await api(`/agentfeed/subscriptions/${un.dataset.unsub}`, { method: 'DELETE' });
    return showSubscriptions();
  }
  const t = e.target.closest('[data-toggle]');
  if (t) { t.nextElementSibling.classList.toggle('collapsed'); return; }
}

let searchTimer = null;
$('#search').addEventListener('input', (e) => {
  clearTimeout(searchTimer);
  state.text = e.target.value.trim();
  // Searching inside a collection stays inside it.
  if (state.collection) {
    const id = state.collection;
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => openCollection(id, state.text), 260);
    return;
  }
  leaveTopic();
  searchTimer = setTimeout(loadList, 260);
});
$('#rendition').addEventListener('change', (e) => {
  state.rendition = e.target.value; loadList();
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target && e.target.id === 'save-new') {
    e.preventDefault();
    saveToNewCollection().catch((err) => toast('That did not work: ' + err.message, 6000));
  }
});

$('#reader-lang').addEventListener('change', async (e) => {
  state.lang = e.target.value;
  await api('/api/languages', { method: 'POST', body: { language: state.lang } });
  // Re-render the open article in the new language straight away.
  if (state.activeData) loadAbstract(state.activeData);
});

function toast(text, ms = 0) {
  $('#toast').classList.remove('hidden');
  $('#toast-text').textContent = text;
  if (ms) setTimeout(() => $('#toast').classList.add('hidden'), ms);
}
async function pollRun() {
  // One poller, ever. Each press used to start another interval, and none
  // of them stopped if the backend went away mid-fetch.
  if (state.poller) return;
  state.poller = setInterval(async () => {
    let s;
    try {
      s = await api('/api/run/status');
    } catch (err) {
      clearInterval(state.poller); state.poller = null;
      toast('Lost contact with the backend: ' + err.message, 8000);
      return;
    }
    toast(`${s.stage}: ${s.message}`);
    if (!s.active) {
      clearInterval(state.poller); state.poller = null;
      const errs = (s.result && s.result.errors) || [];
      toast(errs.length ? `Finished with ${errs.length} problem(s): ${errs[0]}`
                        : 'Done', errs.length ? 9000 : 4000);
      await loadTopics(); await loadFacetCounts();
      // If the fetch was started from a topic, that topic is what the user
      // is waiting on.
      if (state.topic) { await openTopic(state.topic); } else { await loadList(); }
    }
  }, 1000);
}

boot();
