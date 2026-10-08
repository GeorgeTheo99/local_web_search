'use strict';

// Relative endpoints keep the dashboard working at /ui on loopback and behind
// the directory's /local-search/ path-prefix proxy.
const ENDPOINTS = {
  health: 'health',
  config: 'config',
  stats: windowName => `stats?window=${windowName}`,
  activity: windowName => `activity?window=${windowName}`,
};
const REFRESH_MS = 15000;
const TIERS = [
  { key: 'direct', name: 'Direct', role: 'Pinned public fetch' },
  { key: 'decodo', name: 'Decodo', role: 'Premium proxy · JS render' },
  { key: 'jina', name: 'Jina Reader', role: 'Final reader fallback' },
];

const $ = id => document.getElementById(id);
let windowName = '24h';
let loading = false;

const number = value => (Number.isFinite(Number(value)) ? Number(value) : 0);
const count = value => number(value).toLocaleString();
const percent = value => `${Math.round(number(value) * 100)}%`;
const ms = value => {
  const n = number(value);
  if (n <= 0) return '—';
  return n >= 10000 ? `${(n / 1000).toFixed(1)} s` : `${Math.round(n)} ms`;
};
const seconds = value => `${number(value).toLocaleString()} s`;
const bytes = value => {
  const n = number(value);
  if (n >= 1024 * 1024) return `${+(n / (1024 * 1024)).toFixed(1)} MiB`;
  if (n >= 1024) return `${+(n / 1024).toFixed(1)} KiB`;
  return `${n} B`;
};
const yesNo = value => (value ? 'Yes' : 'No');
const present = value => (value ? 'Configured' : 'Missing');
const set = (id, value) => { $(id).textContent = value; };

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function relativeTime(date) {
  const delta = Math.round((Date.now() - date.getTime()) / 1000);
  if (delta < 60) return 'just now';
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

function shortTime(date) {
  return date.toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

function showError(message) {
  const box = $('error');
  box.textContent = message;
  box.hidden = false;
  clearTimeout(showError.timer);
  showError.timer = setTimeout(() => { box.hidden = true; }, 6000);
}

async function getJson(path) {
  const response = await fetch(path, { cache: 'no-store', signal: AbortSignal.timeout(8000) });
  if (!response.ok) throw new Error(`${path.split('?')[0]} returned HTTP ${response.status}`);
  return response.json();
}

// Mirrors the directory's alert policy so this page explains a "degraded" card.
function providerMetrics(record, { failureKey } = {}) {
  const r = record || {};
  const requests = number(r.attempts);
  const failures = failureKey ? number(r[failureKey]) : Math.max(0, requests - number(r.successes));
  return {
    requests,
    failures,
    auth: number(r.http_401s) + number(r.http_403s),
    payment: number(r.payment_required_402s),
    rateLimited: number(r.rate_limited_429s),
  };
}

function providerAlerts(label, m) {
  const alerts = [];
  if (m.payment) alerts.push(`${label}: ${count(m.payment)} payment-required (402) responses`);
  if (m.auth) alerts.push(`${label}: ${count(m.auth)} authorization or account (401/403) responses`);
  if (m.rateLimited) alerts.push(`${label}: ${count(m.rateLimited)} rate-limited (429) responses`);
  if (!alerts.length && m.requests >= 3 && m.failures >= 3 && m.failures / m.requests >= 0.5) {
    alerts.push(`${label}: ${count(m.failures)} of ${count(m.requests)} attempts failed`);
  }
  return alerts;
}

function attentionItems(health, config, stats24) {
  const items = [];
  if (!health.ready) items.push({ level: 'critical', text: 'Brave is not ready — check the credential or circuit breaker.' });
  else if (health.status === 'degraded') items.push({ level: 'warn', text: 'The most recent search finished degraded or with an error.' });
  const decodoConfig = ((config || {}).fetch || {}).decodo || {};
  if (decodoConfig.enabled && !decodoConfig.credential_configured) {
    items.push({ level: 'warn', text: 'Decodo fallback is enabled but its credential is missing.' });
  }
  if (!stats24 || !stats24.available) return items;
  const attempts = (stats24.fetches || {}).attempts || {};
  const brave = providerMetrics((stats24.providers || {}).brave, { failureKey: 'attempt_errors' });
  const decodo = providerMetrics(attempts.decodo);
  const jina = providerMetrics(attempts.jina);
  const jinaRecovered = ((config || {}).fetch || {}).jina?.enabled && jina.requests > jina.failures;
  providerAlerts('Brave', brave).forEach(text => items.push({ level: 'critical', text }));
  providerAlerts('Decodo', decodo).forEach(text => {
    const recoverable = text.includes('attempts failed') && jinaRecovered;
    items.push({ level: recoverable ? 'info' : 'warn', text: recoverable ? `${text} (Jina recovered later attempts)` : text });
  });
  providerAlerts('Jina', jina).forEach(text => items.push({ level: 'warn', text }));
  return items;
}

function renderAttention(items) {
  const list = $('attention-list');
  list.replaceChildren(...items.map(item => el('li', item.level, item.text)));
  $('attention').hidden = items.length === 0;
}

function renderState(health, items) {
  const state = $('state');
  let label = 'Healthy';
  let tone = 'ok';
  if (!health.ready) { label = 'Down'; tone = 'down'; }
  else if (items.some(item => item.level !== 'info')) { label = 'Degraded'; tone = 'warn'; }
  state.className = `state ${tone}`;
  state.querySelector('span').textContent = label;
}

function renderMetrics(stats) {
  const searches = stats.searches || {};
  const statuses = searches.statuses || {};
  const fetches = stats.fetches || {};
  const cache = stats.cache || {};
  const latency = searches.latency_ms || {};
  set('search-total', count(searches.total));
  set('search-outcomes', `${count(statuses.ok)} ok · ${count(statuses.empty)} empty · ${count(number(statuses.error) + number(statuses.timeout))} failed`);
  set('search-latency', ms(latency.average));
  set('search-latency-range', number(searches.total) ? `Range ${ms(latency.minimum)} – ${ms(latency.maximum)}` : 'Average completion');
  set('fetch-total', count(fetches.total));
  set('fetch-success', `${percent(fetches.success_rate)} succeeded · ${count(fetches.errors)} failed`);
  const searchCache = cache.search || {};
  const fetchCache = cache.fetch || {};
  set('cache-rate', percent(searchCache.hit_rate));
  set('cache-detail', `Search ${count(searchCache.hits)} hits · fetch ${percent(fetchCache.hit_rate)}`);
}

function renderProvider(health, stats) {
  const provider = (health.providers || []).find(item => item.name === 'brave') || {};
  const providerStats = (stats.providers || {}).brave || {};
  const circuit = provider.circuit || {};
  const fallback = stats.fallback || {};
  set('credential', provider.credential_usable ? 'Usable' : provider.credential_configured ? 'Configured · unusable' : 'Missing');
  set('circuit', circuit.state ? `${circuit.state}${number(circuit.consecutive_failures) ? ` · ${circuit.consecutive_failures} failures` : ''}` : 'Unknown');
  set('attempts', count(providerStats.attempts));
  set('provider-errors', count(providerStats.attempt_errors));
  set('provider-latency', ms((providerStats.latency_ms || {}).average));
  set('fallback', `${percent(fallback.rate)} · ${count(fallback.searches)} searches`);
  const badge = $('provider-state');
  if (provider.credential_usable && circuit.state !== 'open') { badge.textContent = 'Available'; badge.className = 'badge ok'; }
  else if (provider.credential_configured) { badge.textContent = 'Attention'; badge.className = 'badge warn'; }
  else { badge.textContent = 'Unavailable'; badge.className = 'badge down'; }
}

function renderLastSearch(health) {
  const last = health.last_search;
  const badge = $('last-state');
  if (!last || !last.completed_at) {
    ['last-at', 'last-backend', 'last-results', 'last-latency', 'last-cache', 'last-cost'].forEach(id => set(id, '—'));
    badge.textContent = 'None yet';
    badge.className = 'badge';
    return;
  }
  const at = new Date(last.completed_at * 1000);
  set('last-at', `${relativeTime(at)} · ${shortTime(at)}`);
  set('last-backend', last.backend || 'none');
  set('last-results', count(last.result_count));
  set('last-latency', ms((last.timings_ms || {}).total));
  set('last-cache', last.cache_hit ? `Hit · ${count(last.cache_age_seconds)} s old` : 'Miss');
  set('last-cost', `$${number(last.estimated_cost_usd).toFixed(3)}`);
  badge.textContent = last.status || 'unknown';
  badge.className = `badge ${last.status === 'ok' ? 'ok' : last.status === 'empty' ? '' : 'warn'}`;
}

function renderChart(id, timeline, totalKey, failKey, bucketSeconds) {
  const peak = Math.max(1, ...timeline.map(bucket => number(bucket[totalKey])));
  const bars = timeline.map(bucket => {
    const total = number(bucket[totalKey]);
    const failed = Math.min(total, number(bucket[failKey]));
    const column = el('div', 'bar');
    const start = new Date(bucket.start);
    const end = new Date(start.getTime() + bucketSeconds * 1000);
    column.title = `${shortTime(start)} – ${end.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}\n${count(total)} total · ${count(failed)} failed`;
    const ok = el('i', 'ok');
    ok.style.height = `${((total - failed) / peak) * 100}%`;
    const fail = el('i', 'fail');
    fail.style.height = `${(failed / peak) * 100}%`;
    column.append(fail, ok);
    return column;
  });
  const chart = $(id);
  chart.replaceChildren(...bars);
  $(`${id}-peak`).textContent = `peak ${count(peak)} per bar`;
}

function renderActivity(activity) {
  const timeline = activity.timeline || [];
  renderChart('chart-searches', timeline, 'searches', 'search_failures', activity.bucket_seconds);
  renderChart('chart-fetches', timeline, 'fetches', 'fetch_failures', activity.bucket_seconds);
  if (timeline.length) {
    set('axis-start', shortTime(new Date(timeline[0].start)));
    set('axis-end', 'Now');
  }
  const rows = (activity.recent_fetch_failures || []).map(item => {
    const row = el('tr');
    const at = new Date(item.at);
    const when = el('td', 'mono', relativeTime(at));
    when.title = at.toLocaleString();
    const status = item.provider_http_status && item.provider_http_status !== item.http_status
      ? `${item.http_status ?? '—'} (provider ${item.provider_http_status})`
      : `${item.http_status ?? '—'}`;
    row.append(
      when,
      el('td', 'host', item.host),
      el('td', `tier tier-${item.provider}`, item.provider),
      el('td', 'mono', item.outcome.replaceAll('_', ' ')),
      el('td', 'mono', item.trigger === 'none' ? '—' : item.trigger.replaceAll('_', ' ')),
      el('td', 'mono', status),
      el('td', 'mono num', ms(item.latency_ms)),
    );
    return row;
  });
  $('failures').replaceChildren(...rows);
  $('failures-empty').hidden = rows.length > 0;
}

function renderTiers(stats, config) {
  const fetchConfig = (config || {}).fetch || {};
  const attempts = (stats.fetches || {}).attempts || {};
  const served = (stats.fetches || {}).providers || {};
  set('operation-budget', fetchConfig.operation_timeout_s ? `${seconds(fetchConfig.operation_timeout_s)} end-to-end budget` : '—');
  const tiers = TIERS.map(tier => {
    const record = attempts[tier.key] || {};
    const tierConfig = tier.key === 'direct' ? { enabled: true, timeout_s: fetchConfig.direct_timeout_s } : (fetchConfig[tier.key] || {});
    const enabled = tierConfig.enabled && (tier.key !== 'decodo' || tierConfig.credential_configured);
    const item = el('li', `tier-card${enabled ? '' : ' off'}`);
    const head = el('div', 'tier-head');
    head.append(el('h3', '', tier.name), el('span', `badge ${enabled ? 'ok' : ''}`, enabled ? 'Enabled' : tierConfig.enabled ? 'No credential' : 'Disabled'));
    const attemptsCount = number(record.attempts);
    const rate = el('strong', 'tier-rate', attemptsCount ? percent(record.success_rate) : '—');
    if (attemptsCount && number(record.success_rate) < 0.5) rate.classList.add('bad');
    const signals = [
      number(record.http_401s) + number(record.http_403s) ? `${count(number(record.http_401s) + number(record.http_403s))} auth` : '',
      number(record.payment_required_402s) ? `${count(record.payment_required_402s)} payment` : '',
      number(record.rate_limited_429s) ? `${count(record.rate_limited_429s)} rate-limited` : '',
    ].filter(Boolean).join(' · ');
    const dl = el('dl');
    [
      ['Attempts', count(attemptsCount)],
      ['Failed', count(record.errors)],
      ['Avg latency', ms(record.average_latency_ms)],
      ['Served', count((served[tier.key] || {}).operations)],
      ['Tier budget', tierConfig.timeout_s ? seconds(tierConfig.timeout_s) : '—'],
    ].forEach(([label, value]) => dl.append(el('dt', '', label), el('dd', '', value)));
    item.append(head, el('p', 'tier-role', tier.role), rate, el('small', '', attemptsCount ? 'attempt success rate' : 'no attempts in window'), dl);
    if (signals) item.append(el('p', 'tier-signals', signals));
    return item;
  });
  $('tiers').replaceChildren(...tiers);
}

function renderConfig(config) {
  if (!config) return;
  const search = config.search || {};
  const brave = search.brave || {};
  const breaker = search.circuit_breaker || {};
  const fetchConfig = config.fetch || {};
  const decodo = fetchConfig.decodo || {};
  const jina = fetchConfig.jina || {};
  const groups = [
    ['Search', [
      ['Provider stack', config.provider_stack],
      ['Routing mode', search.mode],
      ['Total budget', seconds(search.total_timeout_s)],
      ['Max results', count(search.max_results)],
      ['Max query length', `${count(search.max_query_chars)} chars`],
      ['Response cap', bytes(search.response_max_bytes)],
      ['Batch', `${count(search.batch_max_queries)} queries · ${count(search.batch_max_concurrency)} parallel`],
      ['Circuit breaker', `${count(breaker.failure_threshold)} failures · ${seconds(breaker.cooldown_s)} cooldown`],
    ]],
    ['Brave', [
      ['Credential', brave.credential_usable ? 'Usable' : present(brave.credential_configured)],
      ['Timeout', seconds(brave.timeout_s)],
    ]],
    ['Fetch', [
      ['Fallback order', (fetchConfig.fallback_order || []).join(' → ')],
      ['Operation budget', seconds(fetchConfig.operation_timeout_s)],
      ['Direct timeout', seconds(fetchConfig.direct_timeout_s)],
      ['DNS timeout', seconds(fetchConfig.dns_timeout_s)],
      ['Body cap', bytes(fetchConfig.max_bytes)],
      ['Max redirects', count(fetchConfig.max_redirects)],
      ['PDF pages', count(fetchConfig.pdf_max_pages)],
    ]],
    ['Decodo', [
      ['Enabled', yesNo(decodo.enabled)],
      ['Credential', present(decodo.credential_configured)],
      ['Timeout', seconds(decodo.timeout_s)],
      ['Response cap', bytes(decodo.response_max_bytes)],
    ]],
    ['Jina Reader', [
      ['Enabled', yesNo(jina.enabled)],
      ['API key', jina.api_key_configured ? 'Configured' : 'Keyless'],
      ['Timeout', seconds(jina.timeout_s)],
      ['Response cap', bytes(jina.response_max_bytes)],
    ]],
    ['Telemetry', [
      ['Enabled', yesNo((config.telemetry || {}).enabled)],
    ]],
  ];
  $('config').replaceChildren(...groups.map(([title, entries]) => {
    const group = el('section', 'config-group');
    const dl = el('dl');
    entries.forEach(([label, value]) => dl.append(el('dt', '', label), el('dd', '', value ?? '—')));
    group.append(el('h3', '', title), dl);
    return group;
  }));
}

async function load() {
  if (loading) return;
  loading = true;
  $('refresh').disabled = true;
  try {
    const [health, config, stats, activity, stats24] = await Promise.allSettled([
      getJson(ENDPOINTS.health),
      getJson(ENDPOINTS.config),
      getJson(ENDPOINTS.stats(windowName)),
      getJson(ENDPOINTS.activity(windowName)),
      windowName === '24h' ? null : getJson(ENDPOINTS.stats('24h')),
    ]);
    if (health.status !== 'fulfilled') throw health.reason;
    const healthData = health.value;
    const configData = config.status === 'fulfilled' ? config.value : null;
    const statsData = stats.status === 'fulfilled' ? stats.value : { available: false };
    const alertStats = windowName === '24h' ? statsData : (stats24.status === 'fulfilled' ? stats24.value : null);
    const items = attentionItems(healthData, configData, alertStats);
    renderState(healthData, items);
    renderAttention(items);
    renderMetrics(statsData);
    renderProvider(healthData, statsData);
    renderLastSearch(healthData);
    renderTiers(statsData, configData);
    renderConfig(configData);
    if (activity.status === 'fulfilled') renderActivity(activity.value);
    const partial = [config, stats, activity].some(result => result.status !== 'fulfilled');
    const telemetry = statsData.complete ? 'telemetry complete' : statsData.available ? 'telemetry partial' : 'telemetry unavailable';
    set('updated', `Updated ${new Date().toLocaleTimeString()} · ${windowName} window · ${telemetry}${partial ? ' · some panels unavailable' : ''}`);
  } catch (error) {
    $('state').className = 'state down';
    $('state').querySelector('span').textContent = 'Unavailable';
    showError(`Could not load broker status: ${error.message}`);
  } finally {
    loading = false;
    $('refresh').disabled = false;
  }
}

$('windows').addEventListener('click', event => {
  const button = event.target.closest('[data-window]');
  if (!button) return;
  windowName = button.dataset.window;
  document.querySelectorAll('[data-window]').forEach(item => item.setAttribute('aria-pressed', String(item === button)));
  load();
});
$('refresh').addEventListener('click', load);
load();
setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
