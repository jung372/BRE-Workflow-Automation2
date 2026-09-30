(function (root) {
  'use strict';
  const KST_OFFSET = 9 * 60 * 60 * 1000;
  function kstDate(now = new Date()) { return new Date(+now + KST_OFFSET).toISOString().slice(0, 10); }
  function statusForDate(issueDate, now = new Date()) {
    if (!issueDate) return '첫 브리핑 발간 준비 중';
    const shifted = new Date(+now + KST_OFFSET);
    if (issueDate === kstDate(now)) return '오늘 브리핑 발행 완료';
    return shifted.getUTCHours() * 60 + shifted.getUTCMinutes() >= 495 ? '오늘 브리핑 발행 지연' : '오늘 브리핑 준비 중';
  }
  function safePath(path, kind) {
    const prefix = kind === 'search' ? 'data/wind-news/search/' : 'data/wind-news/issues/';
    return typeof path === 'string' && path.startsWith(prefix) && path.endsWith('.json') &&
      /^[a-zA-Z0-9_./-]+$/.test(path) && !path.split('/').some(p => !p || p === '.' || p === '..');
  }
  function safeSource(url) {
    try { const parsed = new URL(url); return parsed.protocol === 'https:' && !parsed.username && !parsed.password ? parsed.href : null; }
    catch (_) { return null; }
  }
  function validDate(value) {
    return /^\d{4}-\d{2}-\d{2}$/.test(value || '') && Number.isFinite(Date.parse(value + 'T00:00:00Z')) &&
      new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  }
  function validDescriptor(d) {
    return d && validDate(d.issue_date) && typeof d.issue_id === 'string' && Number.isInteger(d.revision) && d.revision >= 1 &&
      safePath(d.path) && /^[a-f0-9]{64}$/i.test(d.sha256 || '');
  }
  function validIssue(issue, descriptor) {
    return !!issue && issue.issue_date === descriptor.issue_date && issue.issue_id === descriptor.issue_id && issue.revision === descriptor.revision &&
      Array.isArray(issue.items) && issue.items.length <= 15 && Array.isArray(issue.headline_summary) && issue.headline_summary.every(line => typeof line === 'string') &&
      issue.items.every(item => item && typeof (item.headline || item.title) === 'string' && typeof item.summary === 'string' &&
        Array.isArray(item.companies) && item.companies.every(company => typeof company === 'string')) &&
      ['warnings', 'collection_warnings', 'corrections'].every(key => issue[key] == null || Array.isArray(issue[key]));
  }
  function statusLabel(status) {
    return { normal: '정상', published: '발행 완료', no_news: '적합 기사 없음', limited_fallback: '일부 근거 제한', partial: '일부 수집·검증 제한' }[String(status || '').toLowerCase()] || '상태 미확인';
  }
  function countLabel(key) {
    return { fetched: '수집 기사', eligible_articles: '적합 기사', merged_duplicates: '중복 통합', published_topics: '게시 사건',
      eligible: '적합 기사', deduplicated: '중복 통합', published: '게시 사건', held: '검증 보류', held_articles: '검증 보류', selected: '선정 기사', merged: '중복 통합' }[key] || key;
  }
  function timestampBasisLabel(basis) {
    return { publisher_article_meta: '언론사 원문 발행시각', naver_pubDate: '네이버 뉴스 API 제공 발행시각', discovered_at: '수집 시각 (원문 발행시각 미확인)',
      collected_at: '수집 시각 (원문 발행시각 미확인)', date_only: '발행일만 확인', unknown: '미확인' }[basis] || '기타 기사시각 기준';
  }
  function correctionEntries(issue) {
    const entries = (issue.corrections || []).filter(Boolean).map(note => typeof note === 'string' ? { reason: note } : note);
    if (issue.correction_reason) entries.unshift({ reason: issue.correction_reason, corrected_at: issue.corrected_at });
    const unique = new Map();
    entries.forEach(note => unique.set(String(note.reason || '') + ':' + String(note.corrected_at || ''), note));
    return Array.from(unique.values());
  }
  async function verifyBytes(bytes, expected, cryptoApi = root.crypto) {
    if (!/^[a-f0-9]{64}$/i.test(expected || '') || !cryptoApi || !cryptoApi.subtle) throw new Error('무결성 검증을 사용할 수 없습니다. HTTPS로 접속해 주세요.');
    const digest = await cryptoApi.subtle.digest('SHA-256', bytes);
    const actual = Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
    if (actual !== expected.toLowerCase()) throw new Error('게시 파일의 해시가 일치하지 않습니다.');
  }
  const api = { kstDate, statusForDate, safePath, safeSource, validDate, validDescriptor, validIssue, statusLabel, countLabel, timestampBasisLabel, correctionEntries, verifyBytes };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.WindNews = api;
  if (!root.document) return;
  const host = document.getElementById('dailyContent');
  const issueCache = new Map();
  const lastIssue = new Map();
  let latest = null, index = null, lastCheck = 0, latestError = null, activeKey = '', activeAt = 0, activeDay = '', generation = 0, pendingLatest = null;
  function el(tag, text, cls) {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = String(text);
    if (cls) node.className = cls;
    return node;
  }
  function link(text, href, cls) {
    const node = el('a', text, cls); node.href = href; return node;
  }
  function message(title, detail, tone = '', retry) {
    const box = el('div', null, 'wn-message ' + tone);
    box.append(el('h2', title), el('p', detail, 'wn-muted'));
    if (retry) { const btn = el('button', '다시 시도', 'wn-button'); btn.type = 'button'; btn.addEventListener('click', retry); box.append(btn); }
    return box;
  }
  async function fetchJSON(path, hash, revalidate = false) {
    if (hash && !safePath(path, path.startsWith('data/wind-news/search/') ? 'search' : 'issue')) throw new Error('잘못된 게시 경로입니다.');
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(new URL(path, document.baseURI), { cache: hash && !revalidate ? 'default' : 'no-store', signal: controller.signal });
      if (!response.ok) throw new Error('게시 자료 조회 실패 (HTTP ' + response.status + ')');
      const bytes = await response.arrayBuffer();
      if (hash) await verifyBytes(bytes, hash);
      const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
      if (value.schema_version !== 1) throw new Error('지원하지 않는 게시 자료 형식입니다.');
      return value;
    } finally { clearTimeout(timer); }
  }
  function updateShortcut() {
    document.getElementById('dailyShortcutStatus').textContent = latestError ? '최신 브리핑 조회 오류' :
      latest ? latest.issue_date + ' · ' + statusForDate(latest.issue_date) : '발간 이력 없음 · ' + statusForDate(null);
  }
  async function getLatest(force) {
    if (!force && lastCheck && Date.now() - lastCheck < 60000) { updateShortcut(); return latest; }
    if (pendingLatest) return pendingLatest;
    pendingLatest = (async () => {
      try {
        const manifest = await fetchJSON('data/wind-news/latest.json');
        if (manifest.latest !== null && !validDescriptor(manifest.latest)) throw new Error('최신 호 정보가 올바르지 않습니다.');
        latest = manifest.latest; latestError = null; lastCheck = Date.now();
      } catch (error) { latestError = error; lastCheck = Date.now(); }
      updateShortcut();
      return latest;
    })();
    try { return await pendingLatest; } finally { pendingLatest = null; }
  }
  async function getIndex(force) {
    if (index && !force && Date.now() - index.checked < 60000) return index.issues;
    const manifest = await fetchJSON('data/wind-news/index.json');
    if (!Array.isArray(manifest.issues) || !manifest.issues.every(validDescriptor)) throw new Error('발간 목록 형식이 올바르지 않습니다.');
    const byDate = new Map();
    manifest.issues.forEach(d => { if (!byDate.has(d.issue_date) || byDate.get(d.issue_date).revision < d.revision) byDate.set(d.issue_date, d); });
    index = { issues: Array.from(byDate.values()).sort((a, b) => b.issue_date.localeCompare(a.issue_date)), checked: Date.now() };
    return index.issues;
  }
  async function getIssue(descriptor, force) {
    const key = descriptor.sha256;
    if (!force && issueCache.has(key)) return issueCache.get(key);
    const issue = await fetchJSON(descriptor.path, descriptor.sha256, force);
    if (!validIssue(issue, descriptor)) throw new Error('호 정보와 게시 내용이 일치하지 않습니다.');
    issueCache.set(key, issue); lastIssue.set(issue.issue_date, issue); return issue;
  }
  function formatTime(value) {
    if (!value || !Number.isFinite(Date.parse(value))) return '미확인';
    return new Intl.DateTimeFormat('ko-KR', { timeZone: 'Asia/Seoul', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value)) + ' KST';
  }
  function articleCard(item, position, archive) {
    const card = el('article', null, 'wn-article');
    card.id = 'event-' + String(item.event_id || position).replace(/[^a-zA-Z0-9_-]/g, '_');
    const meta = el('div', null, 'wn-article-meta');
    if (archive) meta.append(el('span', item.issue_date + ' 브리핑', 'wn-chip'));
    [item.wind_type, item.primary_category, item.contract_stage].filter(Boolean).forEach(v => meta.append(el('span', v, 'wn-chip')));
    if (item.late_arrival) meta.append(el('span', '지연 수집', 'wn-chip wn-chip-warning'));
    card.append(meta, el('h3', item.headline || item.title || '제목 미확인'), el('p', item.summary || '요약 없음', 'wn-summary'));
    const detail = [(item.companies || []).join(', '), item.project_name, item.region].filter(Boolean).join(' · ');
    if (detail) card.append(el('p', detail, 'wn-muted'));
    const evidenceLabels = { description: '제목·검색 요약 기준', title: '제목 기준', title_only: '제목 기준', search_summary: '제목·검색 요약 기준', snippet: '제목·검색 요약 기준', headline_snippet: '제목·검색 요약 기준', metadata: '제목·검색 요약 기준' };
    if (item.evidence_scope && !['full_text', 'FULL_TEXT', 'full'].includes(item.evidence_scope)) card.append(el('p', '근거 범위: ' + (evidenceLabels[item.evidence_scope] || item.evidence_scope), 'wn-evidence'));
    if (item.timestamp_basis && !['source', 'source_published_at', 'published_at'].includes(item.timestamp_basis)) card.append(el('p', '기사시각 기준: ' + timestampBasisLabel(item.timestamp_basis), 'wn-evidence'));
    const foot = el('div', null, 'wn-article-footer');
    foot.append(el('span', (item.source_name || '출처 미확인') + ' · ' + formatTime(item.source_published_at), 'wn-muted'));
    const actions = el('div', null, 'wn-links');
    const url = safeSource(item.source_url);
    if (url) { const source = link('원문 보기 ↗', url); source.target = '_blank'; source.rel = 'noopener noreferrer'; actions.append(source); }
    else actions.append(el('span', '원문 주소 미확인', 'wn-muted'));
    if (archive && validDate(item.issue_date)) actions.append(link('해당 호 보기 →', '#/daily/' + item.issue_date + '?event=' + encodeURIComponent(item.event_id || '')));
    foot.append(actions); card.append(foot); return card;
  }
  function renderIssue(issue, today) {
    const section = el('div', null, 'wn-issue');
    const heading = el('div', null, 'wn-issue-heading');
    heading.append(el('span', issue.issue_date === kstDate() && today ? '오늘 브리핑' : '발간 브리핑', 'wn-eyebrow'), el('h2', '풍력 일간 시황 · ' + issue.issue_date));
    heading.append(el('p', '집계 마감 ' + formatTime(issue.window_end || issue.cutoff_at || issue.cutoff) + ' · 웹 반영 ' + formatTime(issue.published_at) + ' · revision ' + issue.revision, 'wn-muted'));
    heading.append(el('p', '수집·편집 상태: ' + statusLabel(issue.content_status), 'wn-muted'));
    section.append(heading);
    if (issue.headline_summary.length) {
      const summary = el('div', null, 'wn-highlights'); summary.append(el('h3', issue.issue_date === kstDate() && today ? '오늘의 핵심' : '이 호의 핵심'));
      const list = el('ul'); issue.headline_summary.forEach(line => list.append(el('li', line))); summary.append(list); section.append(summary);
    }
    if (!issue.items.length) section.append(message('선정 기사 없음', '이 호에 공개 승인된 적합 기사가 없습니다. 발간 호의 수집·편집 상태를 함께 확인하세요.'));
    issue.items.forEach((item, i) => section.append(articleCard(item, i, false)));
    const counts = issue.counts || {};
    const notes = el('div', null, 'wn-editorial'); notes.append(el('h3', '수집·편집 상태'));
    const metrics = Object.entries(counts).map(([key, value]) => countLabel(key) + ' ' + value + '건').join(' · ');
    notes.append(el('p', metrics || '게시 사건 ' + issue.items.length + '건', 'wn-muted'));
    (issue.collection_warnings || issue.warnings || []).forEach(note => notes.append(el('p', typeof note === 'string' ? note : JSON.stringify(note), 'wn-evidence')));
    correctionEntries(issue).forEach(note => notes.append(el('p', '정정: ' + (note.reason || '사유 미확인') + (note.corrected_at ? ' · ' + formatTime(note.corrected_at) : ''), 'wn-evidence')));
    section.append(notes); return section;
  }
  async function activate(route, force) {
    const key = route.page + ':' + route.tab + ':' + (route.date || '') + ':' + (route.params ? route.params.toString() : '');
    if (!force && key === activeKey && Date.now() - activeAt < 60000 && activeDay === kstDate()) { updateShortcut(); return; }
    const entering = key !== activeKey;
    activeKey = key;
    activeAt = Date.now(); activeDay = kstDate();
    const run = ++generation;
    if (route.page === 'notices') { await getLatest(force); return; }
    const retry = () => activate(route, true);
    document.getElementById('dailyRefresh').disabled = true;
    if (route.tab === 'archive') {
      try { await root.WindNewsSearch.activate(host, route.params, force, () => run === generation); }
      finally { if (run === generation) document.getElementById('dailyRefresh').disabled = false; }
      return;
    }
    host.replaceChildren(message('브리핑 확인 중…', '검증된 발간 자료를 불러오고 있습니다.'));
    let descriptor = null;
    try {
      if (route.tab === 'dates') {
        const issues = await getIndex(force);
        if (run !== generation) return;
        const picker = el('div', null, 'wn-date-picker');
        const label = el('label', '브리핑 발행일'); label.htmlFor = 'issueDate';
        const select = el('select'); select.id = 'issueDate';
        select.append(new Option('발행일 선택', ''));
        issues.forEach(d => select.append(new Option(d.issue_date + ' · revision ' + d.revision, d.issue_date)));
        select.value = route.date || '';
        select.addEventListener('change', () => { if (select.value) location.hash = '#/daily/' + select.value; });
        picker.append(label, select); host.replaceChildren(picker);
        if (!route.date) { host.append(message(issues.length ? '날짜를 선택해 주세요' : '발간 이력 없음', issues.length ? '선택한 날짜의 최신 정정 호를 확인할 수 있습니다.' : '아직 공개된 브리핑이 없습니다.')); return; }
        descriptor = issues.find(d => d.issue_date === route.date);
        if (!descriptor) { host.append(message('해당 날짜의 발간 호 없음', route.date + '에 공개된 브리핑을 찾지 못했습니다.')); return; }
      } else {
        descriptor = await getLatest(force || entering);
        if (run !== generation) return;
        host.replaceChildren();
        if (latestError) host.append(message('최신 호 조회 실패', latestError.message + (latest ? ' 마지막으로 확인한 호를 표시합니다.' : ''), 'wn-error', retry));
        if (!descriptor || descriptor.issue_date !== kstDate()) host.append(message(statusForDate(descriptor && descriptor.issue_date), '오늘 ' + kstDate() + ' · 집계 마감 07:30 KST · 웹 발행 목표 08:15 KST' + (descriptor ? ' · 이전 호 ' + descriptor.issue_date : ' · 발간 이력 없음'), 'wn-pending'));
        if (!descriptor) return;
      }
      const issue = await getIssue(descriptor, force);
      if (run !== generation) return;
      host.append(renderIssue(issue, route.tab === 'today'));
      const requestedEvent = new URLSearchParams(location.hash.split('?')[1] || '').get('event');
      if (requestedEvent) document.getElementById('event-' + requestedEvent.replace(/[^a-zA-Z0-9_-]/g, '_'))?.scrollIntoView({ block: 'center' });
    } catch (error) {
      if (run !== generation) return;
      if (!host.querySelector('.wn-date-picker') && !host.querySelector('.wn-pending') && !host.querySelector('.wn-error')) host.replaceChildren();
      host.append(message('브리핑 로딩 실패', error.message, 'wn-error', retry));
      const fallback = lastIssue.get(descriptor ? descriptor.issue_date : route.date);
      if (fallback) { host.append(message('마지막 유효 호 표시', '현재 게시 자료 조회에 실패하여 이전에 검증한 revision ' + fallback.revision + '을 표시합니다.', 'wn-pending'), renderIssue(fallback, false)); }
    } finally { if (run === generation) document.getElementById('dailyRefresh').disabled = false; }
  }
  Object.assign(api, { el, link, message, fetchJSON, articleCard, activate });
  setInterval(() => {
    if (!document.hidden && root.WindNavigation) {
      const route = root.WindNavigation.parseRoute(location.hash);
      if (route.page === 'daily' && route.tab === 'today') activate(route, false);
      else if (route.page === 'notices') updateShortcut();
    }
  }, 60000);
})(typeof globalThis !== 'undefined' ? globalThis : this);
