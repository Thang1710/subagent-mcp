/* Subagent MCP — Windows Stable UI behaviour.
 *
 * Contract:
 *   - An optional bootstrap token arrives only in location.hash and is erased
 *     with history.replaceState before anything else runs.
 *   - POST /api/v1/session opens or restores a loopback-bound browser session
 *     and returns a CSRF token kept in memory only.
 *   - GET /api/v1/snapshot renders local state; POST /api/v1/refresh checks providers.
 *   - PATCH /api/v1/config saves.
 *
 * Deliberately absent: storage, cookies, innerHTML, external requests,
 * telemetry, hardcoded model identifiers, lifecycle controls, and any prompt,
 * transcript, or raw event content.
 */
(() => {
  'use strict';

  const API_SESSION = '/api/v1/session';
  const API_SNAPSHOT = '/api/v1/snapshot';
  const API_ACTIVITY = '/api/v1/activity/';
  const API_REFRESH = '/api/v1/refresh';
  const API_AUTH = '/api/v1/runtime-authenticate';
  const API_CONFIG = '/api/v1/config';
  const TOKEN_HEADER = 'X-Subagent-MCP-Token';
  const CSRF_HEADER = 'X-CSRF-Token';
  const SAVED_MS = 4000;

  /* ---------- module state (never persisted) ---------- */

  let csrf = null;
  let revision = null;
  let ready = false;
  let refreshing = false;
  let dead = false;
  let selectedExecutionId = null;
  let activityPollTimer = null;
  let activityPollBusy = false;
  const authenticating = new Set();

  const cards = new Map();       // runtime id -> card entry
  const savedTimers = new Map(); // runtime id -> timeout handle
  let trustState = null;

  /* ---------- dom ---------- */

  const byId = (id) => document.getElementById(id);

  const dom = {
    root: document.documentElement,
    live: byId('live'),
    dist: byId('dist-line'),
    channel: byId('channel-badge'),
    healthPill: byId('health-pill'),
    healthPillText: byId('health-pill-text'),
    refresh: byId('refresh'),
    bannerPanel: byId('banner-panel'),
    banners: byId('banners'),
    healthPanel: byId('health-panel'),
    healthStamp: byId('health-stamp'),
    healthBody: byId('health-body'),
    healthContent: byId('health-content'),
    healthMessages: byId('health-messages'),
    circuitsSection: byId('circuits-section'),
    circuits: byId('circuits'),
    quota: byId('quota-state'),
    updateRow: byId('update-row'),
    update: byId('update-state'),
    version: byId('version-state'),
    statusNote: byId('status-note'),
    runtimesPanel: byId('runtimes-panel'),
    runtimes: byId('runtimes'),
    runtimesEmpty: byId('runtimes-empty'),
    trustList: byId('trust-list'),
    trustEmpty: byId('trust-empty'),
    trustError: byId('trust-error'),
    trustSave: byId('trust-save'),
    activityPanel: byId('activity-panel'),
    activity: byId('activity'),
    activityEmpty: byId('activity-empty'),
    activityDetail: byId('activity-detail'),
    activityDetailIdentity: byId('activity-detail-identity'),
    activityDetailFacts: byId('activity-detail-facts'),
    activityDetailStage: byId('activity-detail-stage'),
    activityDetailSteps: byId('activity-detail-steps'),
    activityResultSection: byId('activity-result-section'),
    activityDetailResult: byId('activity-detail-result'),
    activityDetailArtifact: byId('activity-detail-artifact'),
    activityDetailCapability: byId('activity-detail-capability'),
    fatal: byId('fatal'),
    fatalText: byId('fatal-text'),
    tplRuntime: byId('tpl-runtime'),
    tplGroup: byId('tpl-group'),
    tplField: byId('tpl-field'),
  };

  /* ---------- small helpers ---------- */

  function setText(node, text) {
    if (node) node.textContent = text == null ? '' : String(text);
  }

  // Several containers set `display` in app.css, which beats the UA [hidden]
  // rule, so hiding always pairs the attribute with an inline display.
  function setHidden(node, hidden) {
    if (!node) return;
    node.hidden = !!hidden;
    node.style.display = hidden ? 'none' : '';
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function make(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = String(text);
    return node;
  }

  function dot() {
    const span = make('span', 'dot');
    span.setAttribute('aria-hidden', 'true');
    return span;
  }

  function pick(obj, ...keys) {
    if (!obj || typeof obj !== 'object') return undefined;
    for (const key of keys) {
      const value = obj[key];
      if (value !== undefined && value !== null && value !== '') return value;
    }
    return undefined;
  }

  function str(value) {
    return value == null ? '' : String(value);
  }

  function toList(value) {
    if (value == null) return [];
    return Array.isArray(value) ? value : [value];
  }

  function asArray(value) {
    if (Array.isArray(value)) return value;
    if (value && typeof value === 'object') {
      return Object.keys(value).map((key) => {
        const item = value[key];
        return item && typeof item === 'object' ? Object.assign({ id: key }, item) : { id: key, value: item };
      });
    }
    return [];
  }

  function humanize(value) {
    const text = str(value).replace(/[_-]+/g, ' ').trim();
    if (!text) return '';
    return text.charAt(0).toUpperCase() + text.slice(1);
  }

  const TONE_WORDS = {
    ok: ['ok', 'good', 'healthy', 'ready', 'pass', 'passed', 'closed', 'online', 'active', 'available',
      'trusted', 'up_to_date', 'uptodate', 'current', 'done', 'complete', 'completed', 'succeeded', 'success', 'idle'],
    warn: ['warn', 'warning', 'degraded', 'half_open', 'halfopen', 'pending', 'needs_canary', 'canary',
      'not_configured', 'setup_required', 'stale', 'throttled', 'limited', 'near_limit', 'update_available',
      'outdated', 'paused', 'waiting', 'queued', 'partial'],
    error: ['error', 'fail', 'failed', 'failing', 'down', 'open', 'offline', 'unavailable', 'missing',
      'exhausted', 'blocked', 'denied', 'crashed', 'timeout', 'timed_out', 'cancelled', 'canceled', 'fatal'],
    busy: ['busy', 'starting', 'checking', 'loading', 'probing', 'initializing', 'launching', 'working',
      'in_progress', 'running', 'restarting', 'saving'],
  };

  function toneFor(value, fallback) {
    const key = str(value).toLowerCase().replace(/[\s-]+/g, '_');
    if (!key) return fallback || 'unknown';
    for (const tone of Object.keys(TONE_WORDS)) {
      if (TONE_WORDS[tone].indexOf(key) !== -1) return tone;
    }
    return fallback || 'unknown';
  }

  function toneOf(obj, fallback) {
    const explicit = pick(obj, 'tone', 'severity', 'level');
    if (explicit) return toneFor(explicit, fallback);
    return toneFor(pick(obj, 'state', 'status', 'phase', 'result'), fallback);
  }

  function toDate(value) {
    if (value == null || value === '') return null;
    const date = typeof value === 'number' ? new Date(value) : new Date(str(value));
    return Number.isFinite(date.getTime()) ? date : null;
  }

  function formatClock(value) {
    const date = toDate(value);
    if (!date) return '';
    return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }

  function formatDuration(ms) {
    const value = Number(ms);
    if (!Number.isFinite(value) || value < 0) return '';
    if (value < 1000) return Math.round(value) + 'ms';
    if (value < 60000) return (value / 1000).toFixed(1) + 's';
    const minutes = Math.floor(value / 60000);
    const seconds = Math.round((value % 60000) / 1000);
    return minutes + 'm ' + String(seconds).padStart(2, '0') + 's';
  }

  function shortHash(value) {
    const text = str(value);
    if (!text) return '';
    const bare = text.indexOf(':') === -1 ? text : text.slice(text.indexOf(':') + 1);
    return bare.length > 12 ? bare.slice(0, 12) + '…' : bare;
  }

  function say(message) {
    setText(dom.live, message);
  }

  function setDocState(state) {
    dom.root.dataset.state = state;
  }

  function setPill(node, textNode, tone, text) {
    if (node) node.dataset.tone = tone || 'unknown';
    setText(textNode, text);
  }

  /* ---------- transport ---------- */

  class ApiError extends Error {
    constructor(message, status) {
      super(message);
      this.name = 'ApiError';
      this.status = status || 0;
    }
  }

  async function describeError(response) {
    let detail = '';
    try {
      const text = await response.text();
      if (text) {
        try {
          const data = JSON.parse(text);
          detail = str(pick(data, 'message', 'error', 'detail', 'title'));
        } catch (parseError) {
          detail = text.length <= 200 ? text.trim() : '';
        }
      }
    } catch (readError) {
      detail = '';
    }
    if (detail) return detail;
    if (response.status === 409) return 'Configuration changed elsewhere.';
    return 'Request failed (' + response.status + ').';
  }

  async function request(method, path, body, extraHeaders) {
    const headers = { Accept: 'application/json' };
    if (body !== undefined && body !== null) headers['Content-Type'] = 'application/json';
    if (csrf && method !== 'GET') headers[CSRF_HEADER] = csrf;
    if (extraHeaders) Object.assign(headers, extraHeaders);

    let response;
    try {
      response = await fetch(path, {
        method,
        headers,
        body: body === undefined || body === null ? undefined : JSON.stringify(body),
        mode: 'same-origin',
        credentials: 'same-origin',
        cache: 'no-store',
        redirect: 'error',
        referrerPolicy: 'no-referrer',
      });
    } catch (networkError) {
      throw new ApiError('Cannot reach the local server.', 0);
    }

    if (!response.ok) throw new ApiError(await describeError(response), response.status);
    if (response.status === 204) return {};

    let text = '';
    try {
      text = await response.text();
    } catch (readError) {
      text = '';
    }
    if (!text) return {};
    try {
      const data = JSON.parse(text);
      return data && typeof data === 'object' ? data : {};
    } catch (parseError) {
      throw new ApiError('The server returned a malformed response.', response.status);
    }
  }

  function isAuthError(error) {
    return error instanceof ApiError && (error.status === 401 || error.status === 403);
  }

  /* ---------- bootstrap token ---------- */

  function takeToken() {
    const raw = window.location.hash ? window.location.hash.slice(1) : '';
    try {
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
    } catch (historyError) {
      // A hash we cannot rewrite must not stop the handover; it is never re-read.
    }
    if (!raw) return '';
    if (raw.indexOf('=') !== -1) {
      const params = new URLSearchParams(raw);
      return str(params.get('token') || params.get('t') || params.get('bootstrap') || '');
    }
    try {
      return decodeURIComponent(raw);
    } catch (decodeError) {
      return raw;
    }
  }

  async function openSession(token) {
    const headers = token ? { [TOKEN_HEADER]: token } : {};
    const data = await request('POST', API_SESSION, null, headers);
    csrf = str(pick(data, 'csrfToken', 'csrf_token', 'csrf'));
    if (!csrf) throw new ApiError('The server did not issue a CSRF token.', 0);
    return data;
  }

  /* ---------- fatal state ---------- */

  function fatal(message) {
    dead = true;
    window.clearTimeout(activityPollTimer);
    activityPollTimer = null;
    csrf = null;
    setDocState('fatal');
    setText(dom.fatalText, message);
    setHidden(dom.fatal, false);
    dom.refresh.disabled = true;
    dom.trustSave.disabled = true;
    cards.forEach((entry) => { entry.save.disabled = true; });

    const supportsInert = 'inert' in HTMLElement.prototype;
    Array.prototype.forEach.call(document.body.children, (node) => {
      if (node === dom.fatal || node.tagName === 'TEMPLATE') return;
      if (supportsInert) node.inert = true;
    });

    const card = dom.fatal.querySelector('.veil-card');
    if (card) {
      card.tabIndex = -1;
      card.setAttribute('aria-modal', 'true');
      card.focus();
    }
    say(message);
  }

  document.addEventListener('focusin', (event) => {
    if (!dead || dom.fatal.contains(event.target)) return;
    const card = dom.fatal.querySelector('.veil-card');
    if (card) card.focus();
  });

  /* ---------- loading / busy ---------- */

  function setBusy(busy) {
    dom.healthPanel.setAttribute('aria-busy', busy ? 'true' : 'false');
    dom.runtimesPanel.setAttribute('aria-busy', busy ? 'true' : 'false');
    dom.activityPanel.setAttribute('aria-busy', busy ? 'true' : 'false');
    dom.refresh.disabled = busy || dead;
    if (busy) setPill(dom.healthPill, dom.healthPillText, 'busy', ready ? 'Refreshing…' : 'Checking…');
  }

  function showLoadingSkeletons() {
    setHidden(dom.healthBody, false);
    setHidden(dom.healthContent, true);
    clear(dom.runtimes);
    setHidden(dom.runtimesEmpty, true);
    for (let i = 0; i < 2; i += 1) {
      const block = make('div', 'skeleton-block');
      block.setAttribute('aria-hidden', 'true');
      dom.runtimes.appendChild(block);
    }
  }

  /* ---------- banners ---------- */

  function addBanner(tone, title, message, action) {
    const banner = make('div', 'banner');
    banner.dataset.tone = tone;
    banner.appendChild(dot());

    const text = make('div', 'banner-text');
    text.appendChild(make('b', null, title));
    if (message) text.appendChild(make('span', null, message));
    if (action) {
      const button = make('button', 'btn btn-quiet', action.label);
      button.type = 'button';
      button.addEventListener('click', action.onClick);
      text.appendChild(button);
    }
    banner.appendChild(text);
    dom.banners.appendChild(banner);
  }

  function showBanners() {
    setHidden(dom.bannerPanel, dom.banners.childElementCount === 0);
  }

  function renderBanners(data, runtimes) {
    clear(dom.banners);

    asArray(pick(data, 'banners', 'notices', 'alerts')).forEach((raw) => {
      const title = str(pick(raw, 'title', 'headline', 'label'));
      const message = str(pick(raw, 'message', 'detail', 'body', 'description'));
      if (!title && !message) return;
      addBanner(toneOf(raw, 'info'), title || humanize(pick(raw, 'id', 'kind')) || 'Notice', title ? message : '');
    });

    const canary = runtimes.filter((runtime) => runtime.needsCanary).map((runtime) => runtime.name);
    if (canary.length) {
      addBanner('warn', 'Canary verification required',
        canary.join(', ') + ' must complete a canary run before options can be published.');
    }

    runtimes.filter((runtime) => runtime.authRequired).forEach((runtime) => {
      addBanner('warn', runtime.name + ' sign-in required',
        'Sign in through the native harness before Codex delegates work.', {
          label: authenticating.has(runtime.id) ? 'Opening…' : 'Sign in',
          onClick: (event) => authenticateRuntime(runtime, event.currentTarget),
        });
    });

    const gaps = [];
    runtimes.forEach((runtime) => {
      const missing = runtime.capabilities.filter((cap) => !cap.available).map((cap) => cap.label);
      if (missing.length) gaps.push(runtime.name + ': ' + missing.join(', '));
    });
    if (gaps.length) {
      addBanner('info', 'Some capabilities are unavailable',
        gaps.join(' · ') + '. Controls that depend on them stay disabled.');
    }

    const update = pick(data, 'update', 'updates');
    if (update) {
      const available = update.available === true || toneFor(pick(update, 'state', 'status')) === 'warn';
      const latest = str(pick(update, 'latestVersion', 'latest', 'availableVersion', 'version'));
      if (available) {
        addBanner('info', latest ? 'Update available — ' + latest : 'Update available',
          str(pick(update, 'detail', 'message', 'notes')));
      }
    }

    const quota = pick(data, 'quota', 'quotas');
    const quotaTone = toneOf(quota, '');
    if (quota && (quotaTone === 'warn' || quotaTone === 'error')) {
      addBanner(quotaTone, 'Quota ' + (humanize(pick(quota, 'state', 'status')) || 'attention').toLowerCase(),
        str(pick(quota, 'detail', 'message')));
    }

    showBanners();
  }

  function renderErrorBanner(message) {
    clear(dom.banners);
    addBanner('error', 'Could not load the current snapshot', message, {
      label: 'Retry',
      onClick: (event) => {
        const button = event.currentTarget;
        const wasFocused = document.activeElement === button;
        refresh().then(() => {
          if (wasFocused && !document.body.contains(button)) dom.refresh.focus();
        });
      },
    });
    showBanners();
  }

  /* ---------- health ---------- */

  function renderHealth(data) {
    const health = pick(data, 'health', 'runtimeHealth') || {};
    const tone = toneOf(health, 'unknown');
    const label = str(pick(health, 'label', 'summary')) || humanize(pick(health, 'state', 'status')) || 'Unknown';

    setPill(dom.healthPill, dom.healthPillText, tone, label);
    const checked = pick(health, 'checkedAt', 'checked_at', 'updatedAt', 'timestamp');
    setText(dom.healthStamp, checked ? 'Checked ' + formatClock(checked) : '');

    clear(dom.healthMessages);
    const messages = asArray(pick(health, 'messages', 'checks', 'items'));
    if (!messages.length) {
      dom.healthMessages.appendChild(make('li', null, 'No checks reported.'));
    } else {
      messages.forEach((raw) => {
        const item = make('li');
        item.dataset.tone = toneOf(raw, 'unknown');
        item.appendChild(dot());
        item.appendChild(make('b', null, str(pick(raw, 'label', 'title', 'name', 'check')) || 'Check'));
        const detail = str(pick(raw, 'detail', 'message', 'description'));
        if (detail) item.appendChild(make('span', null, detail));
        dom.healthMessages.appendChild(item);
      });
    }

    clear(dom.circuits);
    const circuits = asArray(pick(health, 'circuits') || pick(data, 'circuits'));
    setHidden(dom.circuitsSection, circuits.length === 0);
    if (circuits.length) {
      circuits.forEach((raw) => {
        const item = make('li');
        item.dataset.tone = toneOf(raw, 'unknown');
        item.appendChild(dot());
        item.appendChild(make('b', null, str(pick(raw, 'name', 'label', 'id')) || 'Circuit'));

        const parts = [];
        const state = humanize(pick(raw, 'state', 'status'));
        if (state) parts.push(state);
        const failures = pick(raw, 'failures', 'failureCount', 'consecutiveFailures');
        if (Number.isFinite(Number(failures))) parts.push(Number(failures) + ' failures');
        const opened = pick(raw, 'openedAt', 'opened_at');
        if (opened) parts.push('opened ' + formatClock(opened));
        const retry = pick(raw, 'retryAt', 'retry_at', 'resetAt', 'cooldownUntil');
        if (retry) parts.push('retry ' + formatClock(retry));
        const detail = str(pick(raw, 'detail', 'message'));
        if (detail) parts.push(detail);
        if (parts.length) item.appendChild(make('span', null, parts.join(' · ')));
        dom.circuits.appendChild(item);
      });
    }

    setHidden(dom.healthBody, true);
    setHidden(dom.healthContent, false);
  }

  function renderHealthUnavailable(message) {
    setPill(dom.healthPill, dom.healthPillText, 'error', 'Unavailable');
    setText(dom.healthStamp, '');
    clear(dom.healthMessages);
    const item = make('li');
    item.dataset.tone = 'error';
    item.appendChild(dot());
    item.appendChild(make('b', null, 'Snapshot unavailable'));
    item.appendChild(make('span', null, message));
    dom.healthMessages.appendChild(item);
    clear(dom.circuits);
    setHidden(dom.circuitsSection, true);
    setHidden(dom.healthBody, true);
    setHidden(dom.healthContent, false);
  }

  /* ---------- identity + status ---------- */

  function renderIdentity(data) {
    const dist = pick(data, 'distribution', 'dist', 'package');
    const distText = typeof dist === 'string' ? dist : str(pick(dist, 'name', 'package', 'id'));
    if (distText) setText(dom.dist, distText);

    const channel = pick(data, 'channel', 'release');
    const channelText = typeof channel === 'string' ? channel : str(pick(channel, 'label', 'name'));
    if (channelText) setText(dom.channel, channelText);
  }

  function renderStatus(data) {
    const quota = pick(data, 'quota', 'quotas');
    const quotaParts = [];
    const quotaLabel = str(pick(quota, 'label', 'summary')) || humanize(pick(quota, 'state', 'status'));
    if (quotaLabel) quotaParts.push(quotaLabel);
    const used = pick(quota, 'used', 'consumed');
    const limit = pick(quota, 'limit', 'total', 'allowance');
    if (Number.isFinite(Number(used)) && Number.isFinite(Number(limit))) {
      quotaParts.push(Number(used) + ' of ' + Number(limit) + ' used');
    }
    const resets = pick(quota, 'resetsAt', 'resets_at', 'resetAt');
    if (resets) quotaParts.push('resets ' + formatClock(resets));
    setText(dom.quota, quotaParts.length ? quotaParts.join(' · ') : '—');
    dom.quota.dataset.tone = toneOf(quota, 'unknown');

    const update = pick(data, 'update', 'updates');
    const updateParts = [];
    const updateLabel = str(pick(update, 'label', 'summary')) || humanize(pick(update, 'state', 'status'));
    if (updateLabel) updateParts.push(updateLabel);
    const latest = str(pick(update, 'latestVersion', 'latest', 'availableVersion'));
    if (latest) updateParts.push(latest);
    const checkedAt = pick(update, 'checkedAt', 'checked_at');
    if (checkedAt) updateParts.push('checked ' + formatClock(checkedAt));
    setText(dom.update, updateParts.length ? updateParts.join(' · ') : '—');
    dom.update.dataset.tone = toneOf(update, 'unknown');
    const updateState = str(pick(update, 'state', 'status')).toLowerCase();
    setHidden(dom.updateRow, !update || updateState === 'not_checked');

    const version = str(pick(data, 'version')) ||
      str(pick(pick(data, 'distribution', 'dist', 'build'), 'version')) ||
      str(pick(update, 'currentVersion', 'installedVersion'));
    setText(dom.version, version || '—');

    const note = str(pick(update, 'detail', 'message', 'notes')) || str(pick(quota, 'detail', 'message'));
    setText(dom.statusNote, note);
    setHidden(dom.statusNote, !note);
  }

  /* ---------- runtime normalisation ---------- */

  function normalizeCapability(raw, index) {
    const id = str(pick(raw, 'id', 'key', 'name')) || 'capability-' + index;
    const available = pick(raw, 'available', 'supported', 'present', 'enabled');
    return {
      id,
      label: str(pick(raw, 'label', 'title', 'name')) || humanize(id),
      available: available === undefined ? true : available === true,
      detail: str(pick(raw, 'detail', 'reason', 'description')),
    };
  }

  function normalizeOption(raw, index) {
    if (raw === null || typeof raw !== 'object') {
      return { value: str(raw), label: str(raw) || 'Option ' + (index + 1), available: true, when: null, requires: [], detail: '' };
    }
    const value = pick(raw, 'value', 'id', 'key', 'name');
    const available = pick(raw, 'available', 'supported', 'enabled');
    return {
      value: str(value),
      label: str(pick(raw, 'label', 'title', 'name', 'value')) || str(value),
      available: available === undefined ? raw.disabled !== true : available === true,
      detail: str(pick(raw, 'detail', 'description', 'help')),
      state: str(pick(raw, 'state', 'status')),
      when: pick(raw, 'when', 'showWhen', 'visibleWhen', 'dependsOn', 'variants') || null,
      requires: toList(pick(raw, 'requires', 'requiresCapability', 'capability')).map(str),
    };
  }

  function optionsOf(def) {
    return asArray(pick(def, 'options', 'choices', 'values', 'variants')).map(normalizeOption);
  }

  function fieldKind(def, options) {
    const raw = str(pick(def, 'kind', 'type', 'control')).toLowerCase();
    if (['boolean', 'bool', 'toggle', 'checkbox', 'switch'].indexOf(raw) !== -1) return 'boolean';
    if (['number', 'integer', 'int', 'float', 'range'].indexOf(raw) !== -1) return 'number';
    if (['select', 'choice', 'enum', 'options', 'variant'].indexOf(raw) !== -1) return 'select';
    if (['model-priority', 'model_priority'].indexOf(raw) !== -1) return 'model-priority';
    if (['model', 'suggested-text'].indexOf(raw) !== -1) return 'model';
    if (['text', 'string'].indexOf(raw) !== -1) return 'text';
    if (options.length) return 'select';
    const value = pick(def, 'value', 'current', 'default');
    if (typeof value === 'boolean') return 'boolean';
    if (typeof value === 'number') return 'number';
    return 'text';
  }

  function numberOrNull(value) {
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
  }

  function normalizeField(raw, index) {
    const id = str(pick(raw, 'id', 'key', 'name')) || 'field-' + index;
    const options = optionsOf(raw);
    const kind = fieldKind(raw, options);
    let value = raw && typeof raw === 'object'
      ? (raw.value !== undefined ? raw.value : pick(raw, 'current', 'selected', 'default'))
      : undefined;
    if (kind === 'boolean') value = value === true;
    if (kind === 'model-priority') value = asArray(value).map(str).filter(Boolean);
    return {
      id,
      label: str(pick(raw, 'label', 'title', 'name')) || humanize(id),
      help: str(pick(raw, 'help', 'description', 'hint')),
      kind,
      options,
      value,
      placeholder: str(pick(raw, 'placeholder')),
      min: numberOrNull(pick(raw, 'min', 'minimum')),
      max: numberOrNull(pick(raw, 'max', 'maximum')),
      step: numberOrNull(pick(raw, 'step')),
      when: pick(raw, 'when', 'showWhen', 'visibleWhen', 'dependsOn') || null,
      requires: toList(pick(raw, 'requires', 'requiresCapability', 'capability', 'capabilities')).map(str),
      required: pick(raw, 'required') === true,
      format: str(pick(raw, 'format')).toLowerCase(),
      readOnly: pick(raw, 'readOnly', 'readonly', 'locked') === true,
      disabled: raw && raw.disabled === true,
    };
  }

  function normalizeGroups(runtime) {
    const groups = asArray(pick(runtime, 'groups', 'settingGroups', 'sections'));
    if (groups.length) {
      return groups.map((group, index) => ({
        id: str(pick(group, 'id', 'key', 'name')) || 'group-' + index,
        label: str(pick(group, 'label', 'title', 'legend', 'name')) || 'Options',
        fields: asArray(pick(group, 'fields', 'settings', 'options', 'controls')).map(normalizeField),
      }));
    }
    const loose = asArray(pick(runtime, 'fields', 'settings'));
    if (!loose.length) return [];
    return [{ id: 'settings', label: 'Settings', fields: loose.map(normalizeField) }];
  }

  function normalizeRuntime(raw, index) {
    const id = str(pick(raw, 'id', 'key', 'name')) || 'runtime-' + index;
    const status = pick(raw, 'status', 'state') || {};
    const statusObject = typeof status === 'object' ? status : { state: status };
    const needsCanary = raw.needsCanary === true || raw.needs_canary === true ||
      toneFor(pick(statusObject, 'state', 'status')) === 'warn' &&
      str(pick(statusObject, 'state', 'status')).toLowerCase().indexOf('canary') !== -1;
    const authRequired = str(pick(statusObject, 'state', 'status')).toLowerCase() === 'auth_required';

    return {
      id,
      name: str(pick(raw, 'name', 'label', 'title')) || humanize(id),
      subtitle: str(pick(raw, 'subtitle', 'description', 'transportLabel')),
      statusTone: toneOf(statusObject, 'unknown'),
      statusText: str(pick(statusObject, 'label', 'summary')) ||
        humanize(pick(statusObject, 'state', 'status')) || 'Unknown',
      statusDetail: str(pick(statusObject, 'detail', 'message', 'reason')) ||
        str(pick(raw, 'detail', 'message')),
      needsCanary,
      authRequired,
      enabled: pick(raw, 'enabled', 'active') === true,
      enabledLabel: str(pick(raw, 'enabledLabel')) || 'Enabled',
      enabledHelp: str(pick(raw, 'enabledHelp')),
      canEnable: pick(raw, 'canEnable', 'canToggle') !== false,
      locked: raw.locked === true || raw.readOnly === true,
      capabilities: asArray(pick(raw, 'capabilities', 'caps', 'features')).map(normalizeCapability),
      groups: normalizeGroups(raw),
    };
  }

  function normalizeRuntimes(data) {
    const source = pick(data, 'runtimes', 'adapters') ||
      pick(pick(data, 'config', 'configuration'), 'runtimes', 'adapters');
    return asArray(source).map(normalizeRuntime);
  }

  /* ---------- variant + capability resolution ---------- */

  function capabilityAvailable(runtime, capabilityId) {
    const found = runtime.capabilities.find((cap) => cap.id === capabilityId || cap.label === capabilityId);
    return found ? found.available : true;
  }

  function missingCapabilities(runtime, requires) {
    return requires.filter((cap) => cap && !capabilityAvailable(runtime, cap));
  }

  // `when` accepts { fieldId: value | [values] } or { field, in|equals|value }.
  function matchesWhen(when, values) {
    if (!when) return true;
    if (Array.isArray(when)) return when.every((clause) => matchesWhen(clause, values));
    if (typeof when !== 'object') return true;

    if (typeof when.field === 'string') {
      const allowed = toList(when.in !== undefined ? when.in
        : (when.equals !== undefined ? when.equals : when.value));
      if (!allowed.length) return true;
      return allowed.some((candidate) => str(candidate) === str(values.get(when.field)));
    }

    return Object.keys(when).every((key) => {
      const allowed = toList(when[key]);
      if (!allowed.length) return true;
      return allowed.some((candidate) => str(candidate) === str(values.get(key)));
    });
  }

  /* ---------- field controls ---------- */

  function buildControl(field, controlId) {
    if (field.kind === 'boolean') {
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.id = controlId;
      input.checked = field.value === true;
      return input;
    }
    if (field.kind === 'number') {
      const input = document.createElement('input');
      input.type = 'number';
      input.id = controlId;
      if (field.min !== null) input.min = String(field.min);
      if (field.max !== null) input.max = String(field.max);
      if (field.step !== null) input.step = String(field.step);
      if (field.placeholder) input.placeholder = field.placeholder;
      input.value = field.value === undefined || field.value === null ? '' : String(field.value);
      return input;
    }
    if (field.kind === 'select') {
      const select = document.createElement('select');
      select.id = controlId;
      return select;
    }
    if (field.kind === 'model-priority') {
      const button = document.createElement('button');
      button.type = 'button';
      button.id = controlId;
      button.className = 'model-priority-trigger';
      return button;
    }
    if (field.kind === 'textarea') {
      const textarea = document.createElement('textarea');
      textarea.id = controlId;
      textarea.rows = 3;
      if (field.placeholder) textarea.placeholder = field.placeholder;
      textarea.value = field.value === undefined || field.value === null ? '' : String(field.value);
      return textarea;
    }
    const input = document.createElement('input');
    input.type = 'text';
    input.id = controlId;
    if (field.placeholder) input.placeholder = field.placeholder;
    input.value = field.value === undefined || field.value === null ? '' : String(field.value);
    return input;
  }

  function moveItem(values, from, to) {
    if (from === to || from < 0 || to < 0 || from >= values.length || to >= values.length) return;
    const moved = values.splice(from, 1)[0];
    values.splice(to, 0, moved);
  }

  function setupModelPriority(entry, item, controlId) {
    const dialog = document.createElement('dialog');
    dialog.className = 'model-priority-dialog';
    const title = make('h3', null, 'Model priority');
    title.id = controlId + '-title';
    dialog.setAttribute('aria-labelledby', title.id);
    dialog.appendChild(title);
    dialog.appendChild(make('p', 'model-priority-intro',
      'Drag models into priority order. The first available model is preferred for future tasks.'));

    const list = make('ol', 'model-priority-list');
    dialog.appendChild(list);

    const advanced = document.createElement('details');
    advanced.className = 'model-priority-advanced';
    advanced.appendChild(make('summary', null, 'Advanced: add exact model ID'));
    const addRow = make('div', 'model-priority-add');
    const exact = document.createElement('input');
    exact.type = 'text';
    exact.placeholder = 'Exact provider-native model ID';
    exact.setAttribute('aria-label', 'Exact provider-native model ID');
    const add = make('button', 'btn btn-quiet', 'Add model');
    add.type = 'button';
    const addError = make('p', 'err');
    setHidden(addError, true);
    addRow.appendChild(exact);
    addRow.appendChild(add);
    advanced.appendChild(addRow);
    advanced.appendChild(addError);
    dialog.appendChild(advanced);

    const actions = make('div', 'model-priority-actions');
    const cancel = make('button', 'btn btn-quiet', 'Cancel');
    cancel.type = 'button';
    const apply = make('button', 'btn btn-primary', 'Apply order');
    apply.type = 'button';
    actions.appendChild(cancel);
    actions.appendChild(apply);
    dialog.appendChild(actions);
    item.wrap.appendChild(dialog);

    let draft = [];
    let dragging = -1;
    const optionFor = (value) => item.field.options.find((option) => option.value === value) || {
      value,
      label: value,
      available: true,
      state: '',
    };

    function updateSummary() {
      const count = item.order.length;
      const available = item.field.options.length;
      const first = count ? optionFor(item.order[0]).label : 'Choose priority';
      const extra = Math.max(0, available - count);
      let summary = count
        ? '#1 ' + first + ' · ' + count + ' configured'
        : first;
      if (extra) summary += ' · ' + extra + ' more available';
      setText(item.control, summary);
      item.control.value = item.order.join('\n');
    }

    function renderDraft() {
      clear(list);
      draft.forEach((value, index) => {
        const option = optionFor(value);
        const row = make('li', 'model-priority-item');
        row.draggable = true;
        row.dataset.index = String(index);
        row.appendChild(make('span', 'model-priority-grip', '⋮⋮'));
        row.appendChild(make('span', 'model-priority-rank', String(index + 1)));
        const copy = make('span', 'model-priority-copy');
        copy.appendChild(make('b', null, option.label));
        copy.appendChild(make('code', null, value));
        row.appendChild(copy);
        if (option.state === 'recheck_on_task') {
          row.appendChild(make('span', 'model-priority-paused', 'Checks live on next task'));
        } else if (!option.available || option.state === 'quota_paused') {
          row.appendChild(make('span', 'model-priority-paused', 'Quota paused'));
        }
        const controls = make('span', 'model-priority-move');
        const up = make('button', 'btn btn-quiet', '↑');
        up.type = 'button';
        up.title = 'Move up';
        up.setAttribute('aria-label', 'Move up ' + option.label);
        up.disabled = index === 0;
        const down = make('button', 'btn btn-quiet', '↓');
        down.type = 'button';
        down.title = 'Move down';
        down.setAttribute('aria-label', 'Move down ' + option.label);
        down.disabled = index === draft.length - 1;
        up.addEventListener('click', () => { moveItem(draft, index, index - 1); renderDraft(); });
        down.addEventListener('click', () => { moveItem(draft, index, index + 1); renderDraft(); });
        controls.appendChild(up);
        controls.appendChild(down);
        row.appendChild(controls);
        row.addEventListener('dragstart', (event) => {
          dragging = index;
          row.classList.add('is-dragging');
          if (event.dataTransfer) {
            event.dataTransfer.effectAllowed = 'move';
            event.dataTransfer.setData('text/plain', String(index));
          }
        });
        row.addEventListener('dragend', () => { dragging = -1; row.classList.remove('is-dragging'); });
        row.addEventListener('dragover', (event) => { event.preventDefault(); });
        row.addEventListener('drop', (event) => {
          event.preventDefault();
          const from = dragging >= 0 ? dragging : Number(event.dataTransfer && event.dataTransfer.getData('text/plain'));
          moveItem(draft, from, index);
          dragging = -1;
          renderDraft();
        });
        list.appendChild(row);
      });
    }

    item.order = Array.isArray(item.field.value) ? item.field.value.slice() : [];
    item.setOrder = (value) => {
      item.order = Array.isArray(value) ? value.slice() : [];
      updateSummary();
    };
    updateSummary();
    item.control.addEventListener('click', () => {
      draft = item.order.slice();
      item.field.options.forEach((option) => {
        if (draft.indexOf(option.value) === -1) draft.push(option.value);
      });
      setHidden(addError, true);
      exact.value = '';
      renderDraft();
      dialog.showModal();
    });
    cancel.addEventListener('click', () => dialog.close());
    apply.addEventListener('click', () => {
      item.setOrder(draft);
      dialog.close();
      item.control.dispatchEvent(new Event('change', { bubbles: true }));
    });
    add.addEventListener('click', () => {
      const value = exact.value.trim();
      let message = '';
      if (!value) message = 'Enter an exact model ID.';
      else if (draft.indexOf(value) !== -1) message = 'That model is already in the list.';
      else if (draft.length >= 8) message = 'At most eight models are supported.';
      if (message) {
        setText(addError, message);
        setHidden(addError, false);
        return;
      }
      item.field.options.push({ value, label: value, available: true, state: '', detail: '', when: null, requires: [] });
      draft.push(value);
      exact.value = '';
      setHidden(addError, true);
      renderDraft();
    });
  }

  function fillSelect(entry, item, values) {
    const select = item.control;
    const wanted = select.dataset.selected !== undefined && select.dataset.selected !== ''
      ? select.dataset.selected
      : str(item.field.value);
    clear(select);

    const usable = item.field.options.filter((option) => matchesWhen(option.when, values));
    if (!usable.length) {
      const placeholder = make('option', null, 'No options available');
      placeholder.value = '';
      placeholder.disabled = true;
      placeholder.selected = true;
      select.appendChild(placeholder);
      select.dataset.selected = '';
      item.emptyOptions = true;
      return;
    }
    item.emptyOptions = false;

    let matched = false;
    usable.forEach((option) => {
      const missing = missingCapabilities(entry.runtime, option.requires);
      const blocked = !option.available || missing.length > 0;
      const node = make('option', null, option.label + (blocked ? ' (unavailable)' : ''));
      node.value = option.value;
      node.disabled = blocked;
      if (!blocked && option.value === wanted) {
        node.selected = true;
        matched = true;
      }
      select.appendChild(node);
    });

    if (!matched) {
      const firstUsable = Array.prototype.find.call(select.options, (option) => !option.disabled);
      if (firstUsable) firstUsable.selected = true;
    }
    select.dataset.selected = select.value;
  }

  function buildField(entry, field, groupIndex, fieldIndex) {
    const wrap = dom.tplField.content.firstElementChild.cloneNode(true);
    wrap.dataset.field = field.id;
    const label = wrap.querySelector('[data-label]');
    const help = wrap.querySelector('[data-help]');
    const controlId = 'f-' + entry.index + '-' + groupIndex + '-' + fieldIndex;
    const control = buildControl(field, controlId);
    let picker = null;
    if (field.required) control.required = true;

    if (field.kind === 'boolean') {
      label.classList.add('inline');
      label.appendChild(control);
      label.appendChild(document.createTextNode(field.label));
    } else if (field.kind === 'model') {
      label.textContent = field.label;
      picker = document.createElement('select');
      picker.id = controlId + '-picker';
      picker.dataset.modelPicker = 'true';
      const placeholder = make('option', null, 'Choose a model');
      placeholder.value = '';
      placeholder.disabled = true;
      picker.appendChild(placeholder);
      field.options.filter((option) => option.available).forEach((option) => {
        const choice = make('option', null, option.label);
        choice.value = option.value;
        picker.appendChild(choice);
      });
      const custom = make('option', null, 'Custom exact model ID…');
      custom.value = '__custom__';
      picker.appendChild(custom);
      label.appendChild(picker);
      control.classList.add('model-custom');
      label.appendChild(control);
    } else {
      label.textContent = field.label;
      label.appendChild(control);
    }

    const item = { field, wrap, control, picker, help, kind: field.kind, emptyOptions: false, gated: [] };
    if (field.kind === 'model-priority') setupModelPriority(entry, item, controlId);

    if (picker) {
      item.syncModelPicker = () => {
        const known = field.options.some((option) => option.available && option.value === control.value);
        if (known) {
          picker.value = control.value;
          setHidden(control, true);
        } else if (control.value) {
          picker.value = '__custom__';
          setHidden(control, false);
        } else {
          picker.value = '';
          setHidden(control, true);
        }
      };
      picker.addEventListener('change', () => {
        if (picker.value === '__custom__') {
          control.value = '';
          setHidden(control, false);
          control.focus();
        } else {
          control.value = picker.value;
          setHidden(control, true);
        }
      });
      item.syncModelPicker();
    }

    if (field.help) {
      help.id = controlId + '-help';
      setText(help, field.help);
      setHidden(help, false);
      control.setAttribute('aria-describedby', help.id);
      if (picker) picker.setAttribute('aria-describedby', help.id);
    } else {
      setHidden(help, true);
    }

    return item;
  }

  function readValue(item) {
    if (item.kind === 'boolean') return item.control.checked;
    if (item.kind === 'model-priority') return item.order.slice();
    if (item.kind === 'number') {
      if (item.control.value === '') return null;
      const value = Number(item.control.value);
      return Number.isFinite(value) ? value : null;
    }
    return item.control.value;
  }

  function currentValues(entry) {
    const values = new Map();
    entry.fields.forEach((item, id) => { values.set(id, readValue(item)); });
    return values;
  }

  function signature(entry) {
    const pairs = [];
    entry.fields.forEach((item, id) => { pairs.push([id, readValue(item)]); });
    pairs.sort((a, b) => (a[0] < b[0] ? -1 : 1));
    return JSON.stringify({ enabled: entry.enabledInput.checked, values: pairs });
  }

  function applyDependencies(entry) {
    const values = currentValues(entry);

    entry.fields.forEach((item) => {
      const visible = matchesWhen(item.field.when, values);
      setHidden(item.wrap, !visible);

      const missing = missingCapabilities(entry.runtime, item.field.requires);
      item.gated = missing;

      if (item.kind === 'select' && visible) fillSelect(entry, item, values);

      const blocked = !visible || missing.length > 0 || item.field.readOnly ||
        item.field.disabled || entry.runtime.locked || entry.saving || item.emptyOptions;
      item.control.disabled = blocked;
      if (item.picker) item.picker.disabled = blocked;

      if (missing.length && item.help) {
        item.help.id = item.help.id || item.control.id + '-help';
        setText(item.help, (item.field.help ? item.field.help + ' ' : '') +
          'Unavailable: requires ' + missing.join(', ') + '.');
        setHidden(item.help, false);
        item.control.setAttribute('aria-describedby', item.help.id);
      } else if (item.field.help && item.help) {
        setText(item.help, item.field.help);
        setHidden(item.help, false);
      } else if (item.help) {
        setHidden(item.help, true);
      }
    });

    entry.groups.forEach((group) => {
      const anyVisible = group.items.some((item) => !item.wrap.hidden);
      setHidden(group.node, !anyVisible);
    });
  }

  function updateDirty(entry) {
    entry.dirty = signature(entry) !== entry.initial;
    entry.save.disabled = dead || entry.saving || entry.runtime.locked || !entry.dirty;
  }

  /* ---------- runtime cards ---------- */

  function buildCard(runtime, index) {
    const card = dom.tplRuntime.content.firstElementChild.cloneNode(true);
    card.dataset.runtime = runtime.id;

    const heading = card.querySelector('[data-name]');
    const headingId = 'rt-' + index + '-title';
    heading.id = headingId;
    setText(heading, runtime.name);
    card.setAttribute('aria-labelledby', headingId);

    const subtitle = card.querySelector('[data-subtitle]');
    setText(subtitle, runtime.subtitle);
    setHidden(subtitle, !runtime.subtitle);

    const pill = card.querySelector('[data-status]');
    setPill(pill, card.querySelector('[data-status-text]'), runtime.statusTone, runtime.statusText);

    const detail = card.querySelector('[data-status-detail]');
    const detailText = runtime.statusDetail ||
      (runtime.needsCanary ? 'A canary run must verify this adapter before its options are published.' : '');
    setText(detail, detailText);
    setHidden(detail, !detailText);

    const supports = card.querySelector('[data-supports]');
    const caps = supports.querySelector('[data-caps]');
    clear(caps);
    runtime.capabilities.forEach((cap) => {
      const item = make('li');
      item.dataset.available = cap.available ? 'true' : 'false';
      item.appendChild(make('b', null, cap.label));
      if (cap.detail) item.appendChild(make('span', null, cap.detail));
      if (!cap.available) {
        const note = make('span', 'sr-only', ' (unavailable)');
        item.appendChild(note);
      }
      caps.appendChild(item);
    });
    setHidden(supports, runtime.capabilities.length === 0);

    const form = card.querySelector('[data-form]');
    const enabledInput = card.querySelector('[data-enabled]');
    const enabledLabel = card.querySelector('[data-enabled-label]');
    const enabledHelp = card.querySelector('[data-enabled-help]');
    enabledInput.id = 'rt-' + index + '-enabled';
    enabledInput.checked = runtime.enabled;
    enabledInput.disabled = !runtime.canEnable || runtime.locked;
    setText(enabledLabel, runtime.enabledLabel);
    setText(enabledHelp, runtime.enabledHelp);
    setHidden(enabledHelp, !runtime.enabledHelp);

    const groupsHost = card.querySelector('[data-groups]');
    const error = card.querySelector('[data-error]');
    const saved = card.querySelector('[data-saved]');
    const save = card.querySelector('[data-save]');
    error.setAttribute('role', 'alert');
    saved.setAttribute('role', 'status');
    setHidden(error, true);
    setHidden(saved, true);

    const entry = {
      runtime,
      index,
      card,
      form,
      enabledInput,
      groupsHost,
      error,
      saved,
      save,
      fields: new Map(),
      groups: [],
      dirty: false,
      saving: false,
      initial: '',
    };

    clear(groupsHost);
    runtime.groups.forEach((group, groupIndex) => {
      const node = dom.tplGroup.content.firstElementChild.cloneNode(true);
      node.dataset.group = group.id;
      setText(node.querySelector('[data-legend]'), group.label);
      const fields = node.querySelector('[data-fields]');
      clear(fields);

      const items = group.fields.map((field, fieldIndex) => {
        const item = buildField(entry, field, groupIndex, fieldIndex);
        fields.appendChild(item.wrap);
        entry.fields.set(field.id, item);
        return item;
      });

      entry.groups.push({ group, node, items });
      groupsHost.appendChild(node);
    });

    if (!runtime.groups.length) {
      groupsHost.appendChild(make('p', 'empty', runtime.needsCanary
        ? 'Options appear once the adapter publishes its capabilities.'
        : 'This runtime publishes no configurable options.'));
    }

    applyDependencies(entry);
    entry.initial = signature(entry);
    updateDirty(entry);

    form.addEventListener('input', () => onCardChange(entry));
    form.addEventListener('change', () => onCardChange(entry));
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      saveRuntime(entry);
    });

    return entry;
  }

  function onCardChange(entry) {
    entry.fields.forEach((item) => {
      item.control.setCustomValidity('');
      item.control.removeAttribute('aria-invalid');
      if (item.kind === 'select') item.control.dataset.selected = item.control.value;
    });
    setHidden(entry.error, true);
    setHidden(entry.saved, true);
    applyDependencies(entry);
    updateDirty(entry);
  }

  function captureDirty() {
    const pending = new Map();
    cards.forEach((entry, id) => {
      if (!entry.dirty || entry.saving) return;
      const values = new Map();
      entry.fields.forEach((item, fieldId) => { values.set(fieldId, readValue(item)); });
      pending.set(id, { enabled: entry.enabledInput.checked, values });
    });
    return pending;
  }

  function restoreDirty(entry, pending) {
    const state = pending.get(entry.runtime.id);
    if (!state) return;
    entry.enabledInput.checked = state.enabled;
    entry.fields.forEach((item, fieldId) => {
      if (!state.values.has(fieldId)) return;
      const value = state.values.get(fieldId);
      if (item.kind === 'boolean') item.control.checked = value === true;
      else if (item.kind === 'model-priority') item.setOrder(value);
      else if (value === null || value === undefined) item.control.value = '';
      else item.control.value = String(value);
      if (item.kind === 'select') item.control.dataset.selected = item.control.value;
      if (item.syncModelPicker) item.syncModelPicker();
    });
    applyDependencies(entry);
    updateDirty(entry);
  }

  function renderRuntimes(runtimes) {
    const pending = captureDirty();
    savedTimers.forEach((handle) => window.clearTimeout(handle));
    savedTimers.clear();
    cards.clear();
    clear(dom.runtimes);

    runtimes.forEach((runtime, index) => {
      const entry = buildCard(runtime, index);
      cards.set(runtime.id, entry);
      dom.runtimes.appendChild(entry.card);
      restoreDirty(entry, pending);
    });

    setHidden(dom.runtimesEmpty, runtimes.length > 0);
  }

  /* ---------- saving ---------- */

  function validate(entry) {
    let firstInvalid = null;
    entry.fields.forEach((item) => {
      item.control.removeAttribute('aria-invalid');
      item.control.setCustomValidity('');
      if (item.wrap.hidden || item.control.disabled) return;
      const value = readValue(item);
      const raw = item.kind === 'model-priority' ? value.join('\n') : item.control.value;
      let message = '';
      if (item.field.required && str(raw).trim() === '') {
        message = 'A value is required.';
      } else if (item.kind === 'number' && raw !== '') {
        const value = Number(raw);
        const outOfRange = !Number.isFinite(value) ||
          (item.field.min !== null && value < item.field.min) ||
          (item.field.max !== null && value > item.field.max);
        if (outOfRange) message = 'The value is out of range.';
      } else if (item.field.format === 'json-object' && raw !== '') {
        try {
          const parsed = JSON.parse(raw);
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
            message = 'Enter a JSON object.';
          }
        } catch (_error) {
          message = 'Enter valid JSON.';
        }
      }
      if (message) {
        item.control.setCustomValidity(message);
        item.control.setAttribute('aria-invalid', 'true');
        if (!firstInvalid) firstInvalid = { item, message };
      }
    });
    return firstInvalid;
  }

  function collectPayload(entry) {
    // Only visible, writable fields are sent; hidden variant branches keep
    // whatever the server already holds.
    const options = {};
    entry.fields.forEach((item, id) => {
      if (item.wrap.hidden || item.field.readOnly || item.emptyOptions) return;
      if (item.control.disabled && item.gated.length) return;
      const value = readValue(item);
      if (value === null || value === undefined) return;
      options[id] = value;
    });
    return { enabled: entry.enabledInput.checked, options };
  }

  function flashSaved(entry) {
    setHidden(entry.saved, false);
    const handle = window.setTimeout(() => {
      setHidden(entry.saved, true);
      savedTimers.delete(entry.runtime.id);
    }, SAVED_MS);
    savedTimers.set(entry.runtime.id, handle);
  }

  async function saveRuntime(entry) {
    if (dead || entry.saving) return;

    const invalid = validate(entry);
    if (invalid) {
      setText(entry.error, 'Check ' + invalid.item.field.label + ': ' + invalid.message);
      setHidden(entry.error, false);
      invalid.item.control.focus();
      return;
    }

    entry.saving = true;
    entry.save.disabled = true;
    entry.form.setAttribute('aria-busy', 'true');
    setHidden(entry.error, true);
    setHidden(entry.saved, true);
    const label = entry.save.textContent;
    setText(entry.save, 'Saving…');
    say('Saving ' + entry.runtime.name + '…');

    const body = { runtimes: {} };
    body.runtimes[entry.runtime.id] = collectPayload(entry);
    if (revision !== null && revision !== undefined) body.revision = revision;

    try {
      const result = await request('PATCH', API_CONFIG, body);
      const nextRevision = pick(result, 'revision', 'configRevision');
      if (nextRevision !== undefined) revision = nextRevision;
      entry.saving = false;
      entry.initial = signature(entry);
      setText(entry.save, label);
      entry.form.removeAttribute('aria-busy');
      say(entry.runtime.name + ' saved.');
      await refresh({ silent: true });
      const fresh = cards.get(entry.runtime.id);
      if (fresh) flashSaved(fresh);
    } catch (error) {
      entry.saving = false;
      entry.form.removeAttribute('aria-busy');
      setText(entry.save, label);
      if (isAuthError(error)) {
        fatal(error.message);
        return;
      }
      setText(entry.error, error.message);
      setHidden(entry.error, false);
      say(entry.runtime.name + ': ' + error.message);
      updateDirty(entry);
      if (error instanceof ApiError && error.status === 409) refresh({ silent: true });
    }
  }

  async function authenticateRuntime(runtime, button) {
    if (dead || authenticating.has(runtime.id)) return;
    authenticating.add(runtime.id);
    if (button) button.disabled = true;
    say('Opening ' + runtime.name + ' sign-in in your default browser…');
    try {
      const requestId = 'runtime-auth-' + runtime.id + '-' + window.crypto.randomUUID();
      await request('POST', API_AUTH, {
        request_id: requestId,
        runtime_id: runtime.id,
      });
      say('Complete sign-in in your default browser, then click Refresh.');
    } catch (error) {
      if (isAuthError(error)) {
        fatal(error.message);
        return;
      }
      say(runtime.name + ': ' + error.message);
    } finally {
      authenticating.delete(runtime.id);
      if (button) button.disabled = false;
    }
  }

  /* ---------- trust ---------- */

  function renderTrust(data) {
    const source = pick(data, 'trust', 'projects') || pick(pick(data, 'trust'), 'projects');
    const entries = asArray(Array.isArray(source) ? source : pick(source, 'projects', 'entries', 'items'))
      .map((raw, index) => ({
        path: str(pick(raw, 'path', 'projectPath', 'root', 'id')) || 'project-' + index,
        hash: str(pick(raw, 'hash', 'contentHash', 'sha256')),
        trusted: pick(raw, 'trusted', 'isTrusted') === true,
        state: str(pick(raw, 'state', 'status')),
        scannedAt: pick(raw, 'scannedAt', 'lastScanned', 'seenAt', 'updatedAt'),
      }));

    clear(dom.trustList);
    trustState = { entries, inputs: new Map(), saving: false };

    entries.forEach((item, index) => {
      const li = make('li');
      li.appendChild(make('span', 'path', item.path));

      const meta = [];
      if (item.state) meta.push(humanize(item.state));
      if (item.hash) meta.push('sha256 ' + shortHash(item.hash));
      if (item.scannedAt) meta.push('scanned ' + formatClock(item.scannedAt));
      if (meta.length) li.appendChild(make('span', 'meta', meta.join(' · ')));

      const label = make('label');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.id = 'trust-' + index;
      input.checked = item.trusted;
      input.disabled = dead;
      input.addEventListener('change', updateTrustDirty);
      label.appendChild(input);
      label.appendChild(document.createTextNode('Trusted'));
      li.appendChild(label);

      trustState.inputs.set(item.path, input);
      dom.trustList.appendChild(li);
    });

    setHidden(dom.trustEmpty, entries.length > 0);
    setHidden(dom.trustError, true);
    updateTrustDirty();
  }

  function changedTrust() {
    if (!trustState) return [];
    return trustState.entries.filter((item) => {
      const input = trustState.inputs.get(item.path);
      return input && input.checked !== item.trusted;
    }).map((item) => ({
      path: item.path,
      hash: item.hash,
      trusted: trustState.inputs.get(item.path).checked,
    }));
  }

  function updateTrustDirty() {
    setHidden(dom.trustError, true);
    dom.trustSave.disabled = dead || !trustState || trustState.saving || changedTrust().length === 0;
  }

  async function saveTrust() {
    if (dead || !trustState || trustState.saving) return;
    const changes = changedTrust();
    if (!changes.length) return;

    trustState.saving = true;
    dom.trustSave.disabled = true;
    const label = dom.trustSave.textContent;
    setText(dom.trustSave, 'Saving…');
    setHidden(dom.trustError, true);
    say('Saving project trust…');

    const body = { trust: changes };
    if (revision !== null && revision !== undefined) body.revision = revision;

    try {
      const result = await request('PATCH', API_CONFIG, body);
      const nextRevision = pick(result, 'revision', 'configRevision');
      if (nextRevision !== undefined) revision = nextRevision;
      setText(dom.trustSave, label);
      say('Project trust saved.');
      await refresh({ silent: true });
    } catch (error) {
      trustState.saving = false;
      setText(dom.trustSave, label);
      if (isAuthError(error)) {
        fatal(error.message);
        return;
      }
      setText(dom.trustError, error.message);
      setHidden(dom.trustError, false);
      say('Project trust: ' + error.message);
      updateTrustDirty();
      if (error instanceof ApiError && error.status === 409) refresh({ silent: true });
    }
  }

  /* ---------- activity (read-only, whitelisted fields) ---------- */

  const ACTIVE_ACTIVITY_STATES = ['queued', 'starting', 'running', 'needs_input'];
  const ACTIVITY_ICON_TONES = ['blue', 'green', 'purple', 'teal'];

  function formatTimestamp(value) {
    const date = toDate(value);
    if (!date) return '';
    return date.toLocaleString([], {
      year: 'numeric', month: 'short', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  }

  function normalizedIcon(raw) {
    const candidate = raw && typeof raw === 'object' ? raw : {};
    const text = str(candidate.text).replace(/[^A-Za-z0-9]/g, '').slice(0, 2) || 'S';
    const tone = ACTIVITY_ICON_TONES.indexOf(candidate.tone) === -1 ? 'teal' : candidate.tone;
    return { text, tone };
  }

  function makeActivityIcon(raw) {
    const icon = normalizedIcon(raw);
    const badge = make('span', 'activity-icon', icon.text);
    badge.dataset.tone = icon.tone;
    badge.setAttribute('aria-hidden', 'true');
    return badge;
  }

  function updateActivitySelection() {
    const buttons = dom.activity.querySelectorAll('button[data-execution-id]');
    Array.prototype.forEach.call(buttons, (button) => {
      const selected = button.dataset.executionId === selectedExecutionId;
      button.dataset.selected = selected ? 'true' : 'false';
      if (selected) button.setAttribute('aria-current', 'true');
      else button.removeAttribute('aria-current');
    });
  }

  function renderActivityLoading() {
    setHidden(dom.activityDetail, false);
    dom.activityDetail.dataset.freshness = 'loading';
    dom.activityDetail.setAttribute('aria-busy', 'true');
    clear(dom.activityDetailIdentity);
    dom.activityDetailIdentity.appendChild(make('p', 'empty', 'Loading activity…'));
    clear(dom.activityDetailFacts);
    setText(dom.activityDetailStage, 'Loading local execution state…');
    clear(dom.activityDetailSteps);
    setHidden(dom.activityResultSection, true);
    setText(dom.activityDetailResult, '');
    setText(dom.activityDetailArtifact, '');
    setText(dom.activityDetailCapability, '');
  }

  function selectActivity(executionId) {
    if (!executionId) return;
    const changed = selectedExecutionId !== executionId;
    selectedExecutionId = executionId;
    updateActivitySelection();
    if (changed) renderActivityLoading();
    loadActivityDetail(executionId, { silent: true });
    scheduleActivityPoll(2000);
  }

  function addActivityFact(label, value) {
    const text = str(value);
    if (!text) return;
    const row = make('div');
    row.appendChild(make('dt', null, label));
    row.appendChild(make('dd', null, text));
    dom.activityDetailFacts.appendChild(row);
  }

  function renderActivityDetail(detail) {
    clear(dom.activityDetailIdentity);
    const identity = make('div', 'activity-detail-id');
    identity.appendChild(makeActivityIcon(pick(detail, 'icon')));
    const copy = make('div', 'activity-detail-copy');
    copy.appendChild(make('h3', null, str(pick(detail, 'title')) || 'External-agent execution'));
    const identityMeta = [
      str(pick(detail, 'displayName', 'runtime')),
      str(pick(detail, 'harness')),
      str(pick(detail, 'modelDisplayName')),
    ].filter(Boolean);
    if (identityMeta.length) copy.appendChild(make('p', null, identityMeta.join(' · ')));
    identity.appendChild(copy);
    const state = str(pick(detail, 'state'));
    const pill = make('span', 'pill');
    pill.dataset.tone = toneFor(state, 'unknown');
    pill.appendChild(dot());
    pill.appendChild(make('span', null, humanize(state) || 'Unknown'));
    identity.appendChild(pill);
    dom.activityDetailIdentity.appendChild(identity);

    clear(dom.activityDetailFacts);
    addActivityFact('Model', pick(detail, 'modelDisplayName'));
    const reasoning = pick(detail, 'reasoning');
    if (reasoning && typeof reasoning === 'object' && Object.keys(reasoning).length) {
      addActivityFact('Reasoning', JSON.stringify(reasoning));
    }
    addActivityFact('Native harness', pick(detail, 'harness'));
    addActivityFact('Provider', pick(detail, 'provider'));
    addActivityFact('Transport', pick(detail, 'transport'));
    addActivityFact('Workspace', pick(detail, 'workspace'));
    addActivityFact('Mode', humanize(pick(detail, 'mode')));
    const permissions = asArray(pick(detail, 'permissions')).map(str).filter(Boolean);
    addActivityFact('Permissions', permissions.join(' · '));
    const writeSet = asArray(pick(detail, 'writeSet')).map(str).filter(Boolean);
    addActivityFact('Write set', writeSet.length ? writeSet.join(' · ') : 'Read-only');
    addActivityFact('Started', formatTimestamp(pick(detail, 'startedAt')));
    addActivityFact('Updated', formatTimestamp(pick(detail, 'updatedAt')));
    addActivityFact('Finished', formatTimestamp(pick(detail, 'finishedAt')));
    addActivityFact('Elapsed', formatDuration(pick(detail, 'durationMs')));

    setText(dom.activityDetailStage,
      humanize(pick(detail, 'currentStage')) || humanize(state) || 'Unknown');

    clear(dom.activityDetailSteps);
    const steps = asArray(pick(detail, 'steps'));
    if (!steps.length) {
      dom.activityDetailSteps.appendChild(make('li', 'activity-step-empty', 'No lifecycle events recorded.'));
    } else {
      steps.forEach((step) => {
        const item = make('li');
        item.appendChild(make('span', 'activity-step-marker'));
        const stepCopy = make('span', 'activity-step-copy');
        stepCopy.appendChild(make('b', null, humanize(pick(step, 'kind')) || 'Event'));
        const at = formatTimestamp(pick(step, 'at'));
        if (at) stepCopy.appendChild(make('time', null, at));
        item.appendChild(stepCopy);
        dom.activityDetailSteps.appendChild(item);
      });
    }

    const result = pick(detail, 'result');
    const resultText = result && typeof result === 'object' ? str(result.text) : '';
    setHidden(dom.activityResultSection, !resultText);
    setText(dom.activityDetailResult, resultText);
    const artifact = [];
    if (result && typeof result === 'object') {
      if (Number.isFinite(Number(result.charCount))) artifact.push(Number(result.charCount) + ' characters');
      if (result.sha256) artifact.push('sha256 ' + shortHash(result.sha256));
    }
    setText(dom.activityDetailArtifact, artifact.join(' · '));

    const gaps = asArray(pick(detail, 'capabilityGaps')).map(humanize).filter(Boolean);
    setText(dom.activityDetailCapability, gaps.length
      ? 'Capability gaps: ' + gaps.join(', ') + '. Progress remains limited to events the native harness publishes.'
      : 'Progress reflects normalized lifecycle events published by the native harness.');

    setHidden(dom.activityDetail, false);
    dom.activityDetail.dataset.freshness = 'fresh';
    dom.activityDetail.setAttribute('aria-busy', 'false');
  }

  async function loadActivityDetail(executionId, options) {
    const opts = options || {};
    if (!executionId || activityPollBusy || dead) return;
    activityPollBusy = true;
    try {
      const detail = await request('GET', API_ACTIVITY + encodeURIComponent(executionId));
      if (selectedExecutionId !== executionId) return;
      renderActivityDetail(detail);
    } catch (error) {
      if (isAuthError(error)) {
        fatal(error.message);
        return;
      }
      if (selectedExecutionId === executionId) {
        dom.activityDetail.dataset.freshness = 'stale';
        dom.activityDetail.setAttribute('aria-busy', 'false');
      }
      if (!opts.silent) say(error.message);
    } finally {
      activityPollBusy = false;
    }
  }

  function renderActivity(data) {
    const items = asArray(pick(data, 'activity', 'agents', 'tasks'));
    const previousSelection = selectedExecutionId;
    const activeElement = document.activeElement;
    const focusedExecutionId = activeElement && dom.activity.contains(activeElement)
      ? str(activeElement.dataset && activeElement.dataset.executionId)
      : '';
    const ids = items.map((raw) => str(pick(raw, 'id', 'taskId', 'agentId'))).filter(Boolean);
    if (ids.indexOf(selectedExecutionId) === -1) {
      const active = items.find((raw) => {
        const state = str(pick(raw, 'state', 'status', 'phase')).toLowerCase();
        return ACTIVE_ACTIVITY_STATES.indexOf(state) !== -1;
      });
      selectedExecutionId = str(pick(active, 'id', 'taskId', 'agentId')) || ids[0] || null;
    }

    clear(dom.activity);
    items.forEach((raw, index) => {
      const title = str(pick(raw, 'title', 'label', 'name'));
      const id = str(pick(raw, 'id', 'taskId', 'agentId'));
      if (!id) return;
      const runtime = str(pick(raw, 'displayName', 'runtime', 'adapter', 'runtimeName'));
      const model = str(pick(raw, 'modelDisplayName'));
      const state = pick(raw, 'state', 'status', 'phase');
      const duration = formatDuration(pick(raw, 'durationMs', 'duration_ms', 'elapsedMs'));

      const li = make('li');
      const button = make('button', 'activity-row');
      button.type = 'button';
      button.dataset.executionId = id;
      button.appendChild(makeActivityIcon(pick(raw, 'icon')));

      const rowCopy = make('span', 'activity-row-copy');
      rowCopy.appendChild(make('span', 'a-title', title || 'Task ' + (index + 1)));
      const meta = [runtime, model].filter(Boolean);
      if (meta.length) rowCopy.appendChild(make('span', 'a-meta', meta.join(' · ')));
      button.appendChild(rowCopy);

      const rowState = make('span', 'activity-row-state');
      const statePill = make('span', 'pill');
      statePill.dataset.tone = toneOf(raw, 'unknown');
      statePill.appendChild(dot());
      statePill.appendChild(make('span', null, humanize(state) || 'Unknown'));
      rowState.appendChild(statePill);
      if (duration) rowState.appendChild(make('span', 'a-duration', duration));
      button.appendChild(rowState);

      button.addEventListener('click', () => selectActivity(id));
      li.appendChild(button);
      dom.activity.appendChild(li);
    });

    updateActivitySelection();
    if (focusedExecutionId) {
      const replacement = Array.prototype.find.call(
        dom.activity.querySelectorAll('button[data-execution-id]'),
        (button) => button.dataset.executionId === focusedExecutionId,
      );
      if (replacement) replacement.focus({ preventScroll: true });
    }
    setHidden(dom.activityEmpty, items.length > 0);
    if (!selectedExecutionId) {
      setHidden(dom.activityDetail, true);
      clear(dom.activityDetailIdentity);
    } else if (selectedExecutionId !== previousSelection) {
      renderActivityLoading();
      loadActivityDetail(selectedExecutionId, { silent: true });
    }
  }

  /* ---------- render + refresh ---------- */

  function render(data) {
    const runtimes = normalizeRuntimes(data);
    const config = pick(data, 'config', 'configuration');
    const nextRevision = pick(data, 'revision', 'configRevision') ?? pick(config, 'revision');
    if (nextRevision !== undefined) revision = nextRevision;
    renderIdentity(data);
    renderHealth(data);
    renderStatus(data);
    renderRuntimes(runtimes);
    renderTrust(data);
    renderActivity(data);
    renderBanners(data, runtimes);
  }

  function renderUnavailable(message) {
    renderHealthUnavailable(message);
    clear(dom.runtimes);
    setHidden(dom.runtimesEmpty, false);
    clear(dom.trustList);
    setHidden(dom.trustEmpty, false);
    dom.trustSave.disabled = true;
    clear(dom.activity);
    setHidden(dom.activityEmpty, false);
    selectedExecutionId = null;
    setHidden(dom.activityDetail, true);
  }

  async function refresh(options) {
    const opts = options || {};
    if (dead || refreshing) return;
    refreshing = true;
    if (!opts.localOnly) setBusy(true);
    if (!opts.silent) say(opts.provider ? 'Checking provider…' : ready ? 'Refreshing…' : 'Loading…');

    try {
      const data = await request(
        opts.provider ? 'POST' : 'GET',
        opts.provider ? API_REFRESH : API_SNAPSHOT,
      );
      if (opts.localOnly) renderActivity(data);
      else render(data);
      ready = true;
      setDocState('ready');
      if (!opts.silent) say('Updated ' + formatClock(Date.now()) + '.');
    } catch (error) {
      if (isAuthError(error)) {
        fatal(error.message);
        return;
      }
      if (opts.localOnly) {
        dom.activityPanel.dataset.freshness = 'stale';
        return;
      }
      setDocState(ready ? 'stale' : 'error');
      renderErrorBanner(error.message);
      if (!ready) renderUnavailable(error.message);
      else setPill(dom.healthPill, dom.healthPillText, 'error', 'Unavailable');
      say(error.message);
    } finally {
      refreshing = false;
      if (!dead && !opts.localOnly) setBusy(false);
    }
  }

  function scheduleActivityPoll(delayMs) {
    window.clearTimeout(activityPollTimer);
    activityPollTimer = window.setTimeout(async () => {
      if (!dead && document.visibilityState === 'visible') {
        await refresh({ silent: true, localOnly: true });
        if (selectedExecutionId) {
          await loadActivityDetail(selectedExecutionId, { silent: true });
        }
        dom.activityPanel.dataset.freshness = 'fresh';
      }
      if (!dead) scheduleActivityPoll(selectedExecutionId ? 2000 : 5000);
    }, delayMs);
  }

  document.addEventListener('visibilitychange', () => {
    if (!dead && document.visibilityState === 'visible') scheduleActivityPoll(0);
  });

  /* ---------- boot ---------- */

  dom.refresh.addEventListener('click', () => refresh({ provider: true }));
  dom.trustSave.addEventListener('click', () => saveTrust());

  (async function boot() {
    const token = takeToken();
    setDocState('loading');
    showLoadingSkeletons();
    setBusy(true);

    try {
      await openSession(token);
    } catch (error) {
      setBusy(false);
      fatal(error instanceof ApiError && error.status
        ? error.message
        : 'The local UI session could not be opened.');
      return;
    }

    await refresh();
    if (!dead) scheduleActivityPoll(selectedExecutionId ? 2000 : 5000);
  }());
})();
