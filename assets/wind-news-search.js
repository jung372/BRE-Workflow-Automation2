(function (root) {
  'use strict';
  const fields = ['q', 'company', 'project', 'region', 'wind', 'category'];
  function dateValid(value) {
    return /^\d{4}-\d{2}-\d{2}$/.test(value || '') && Number.isFinite(Date.parse(value + 'T00:00:00Z')) && new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  }
  function defaultRange(today) {
    const start = new Date(today + 'T00:00:00Z'); start.setUTCDate(start.getUTCDate() - 29);
    return { from: start.toISOString().slice(0, 10), to: today };
  }
  function readFilters(params, today) {
    const period = params.get('period') === 'all' ? 'all' : 'recent';
    const defaults = period === 'all' ? { from: '', to: '' } : defaultRange(today);
    const from = params.get('from'), to = params.get('to');
    const result = { period, from: dateValid(from) ? from : defaults.from, to: dateValid(to) ? to : defaults.to,
      sort: params.get('sort') === 'oldest' ? 'oldest' : 'newest', page: Math.max(1, Math.min(100000, parseInt(params.get('page'), 10) || 1)) };
    fields.forEach(key => { result[key] = (params.get(key) || '').trim().slice(0, 250); });
    return result;
  }
  function buildHash(filters) {
    const params = new URLSearchParams();
    ['period', 'from', 'to', ...fields, 'sort', 'page'].forEach(key => { if (filters[key]) params.set(key, String(filters[key])); });
    return '#/daily/archive?' + params.toString();
  }
  function selectedMonths(months, filters) {
    return months.filter(d => (!filters.from || d.month >= filters.from.slice(0, 7)) && (!filters.to || d.month <= filters.to.slice(0, 7)));
  }
  function latestRevisionItems(items) {
    const maxRevision = new Map();
    items.forEach(item => maxRevision.set(item.issue_id, Math.max(maxRevision.get(item.issue_id) || 0, item.revision)));
    const unique = new Map();
    items.filter(item => item.revision === maxRevision.get(item.issue_id)).forEach(item => {
      const key = item.issue_id + ':' + (item.event_id || item.representative_article_id);
      if (!unique.has(key)) unique.set(key, item);
    });
    return Array.from(unique.values());
  }
  function filterItems(items, f) {
    const contains = (value, needle) => String(value || '').toLocaleLowerCase('ko-KR').includes(needle.toLocaleLowerCase('ko-KR'));
    return latestRevisionItems(items).filter(item =>
      (!f.from || item.issue_date >= f.from) && (!f.to || item.issue_date <= f.to) &&
      (!f.q || contains((item.headline || item.title || '') + ' ' + item.summary, f.q)) &&
      (!f.company || contains((item.companies || []).join(' '), f.company)) &&
      (!f.project || contains(item.project_name, f.project)) && (!f.region || contains(item.region, f.region)) &&
      (!f.wind || contains(item.wind_type, f.wind)) && (!f.category || contains(item.primary_category, f.category)))
      .sort((a, b) => {
        const dateOrder = a.issue_date.localeCompare(b.issue_date) || String(a.source_published_at || '').localeCompare(String(b.source_published_at || ''));
        return (f.sort === 'oldest' ? dateOrder : -dateOrder) || String(a.event_id).localeCompare(String(b.event_id));
      });
  }
  function paginate(items, page, size = 15) {
    const pages = Math.max(1, Math.ceil(items.length / size)); const current = Math.min(pages, Math.max(1, page));
    return { items: items.slice((current - 1) * size, current * size), page: current, pages, total: items.length };
  }
  const api = { defaultRange, readFilters, buildHash, selectedMonths, latestRevisionItems, filterItems, paginate };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.WindNewsSearch = api;
  if (!root.document) return;
  let manifest = null, checked = 0;
  const shards = new Map();
  const W = root.WindNews;
  function formFor(filters) {
    const form = W.el('form', null, 'wn-search-form');
    const title = W.el('div', null, 'wn-search-title'); title.append(W.el('h2', '누적 기사 검색'), W.el('p', '브리핑 발행일 기준 · 공개 승인된 발간 기사만 검색합니다.', 'wn-muted')); form.append(title);
    function field(label, key, type, options) {
      const wrap = W.el('label', label, 'wn-field'); let input;
      if (options) { input = W.el('select'); options.forEach(([value, text]) => input.append(new Option(text, value))); }
      else { input = W.el('input'); input.type = type || 'search'; }
      input.name = key; input.value = filters[key] || ''; if (input.type === 'search') input.placeholder = label + ' 입력';
      wrap.append(input); return wrap;
    }
    const grid = W.el('div', null, 'wn-search-grid');
    grid.append(field('검색 기간', 'period', '', [['recent', '최근 30일 / 지정 기간'], ['all', '전체 기간']]), field('시작 발행일', 'from', 'date'), field('종료 발행일', 'to', 'date'));
    grid.append(field('제목·요약 키워드', 'q'), field('기업', 'company'), field('프로젝트', 'project'), field('지역', 'region'),
      field('육상 / 해상', 'wind'), field('사건 분류', 'category'), field('정렬', 'sort', '', [['newest', '최신순'], ['oldest', '과거순']]));
    form.append(grid);
    const actions = W.el('div', null, 'wn-search-actions');
    const submit = W.el('button', '기사 검색', 'wn-button wn-button-primary'); submit.type = 'submit';
    actions.append(submit, W.link('조건 초기화', '#/daily/archive', 'wn-button')); form.append(actions);
    form.elements.period.addEventListener('change', () => {
      const range = form.elements.period.value === 'all' ? { from: '', to: '' } : defaultRange(W.kstDate());
      form.elements.from.value = range.from; form.elements.to.value = range.to;
    });
    form.addEventListener('submit', event => {
      event.preventDefault();
      const values = Object.fromEntries(new FormData(form));
      if (values.from && values.to && values.from > values.to) { form.elements.to.setCustomValidity('종료일은 시작일 이후여야 합니다.'); form.elements.to.reportValidity(); return; }
      form.elements.to.setCustomValidity(''); location.hash = buildHash({ ...values, page: 1 });
    });
    form.elements.to.addEventListener('input', () => form.elements.to.setCustomValidity(''));
    form.elements.from.addEventListener('input', () => form.elements.to.setCustomValidity(''));
    return form;
  }
  async function loadManifest(force) {
    if (!force && manifest && Date.now() - checked < 60000) return manifest;
    const value = await W.fetchJSON('data/wind-news/search/manifest.json');
    if (!Array.isArray(value.months) || !value.months.every(d => /^\d{4}-(0[1-9]|1[0-2])$/.test(d.month) && W.safePath(d.path, 'search') && /^[a-f0-9]{64}$/i.test(d.sha256 || '') && Number.isInteger(d.count) && d.count >= 0) || new Set(value.months.map(d => d.month)).size !== value.months.length) throw new Error('검색 manifest 형식이 올바르지 않습니다.');
    manifest = value; checked = Date.now(); return value;
  }
  async function loadShard(descriptor, force) {
    if (!force && shards.has(descriptor.sha256)) return shards.get(descriptor.sha256);
    const value = await W.fetchJSON(descriptor.path, descriptor.sha256, force);
    if (value.month !== descriptor.month || !Array.isArray(value.items) || value.items.length !== descriptor.count ||
        !value.items.every(item => dateValid(item.issue_date) && item.issue_date.startsWith(descriptor.month) && Number.isInteger(item.revision) && item.revision >= 1 &&
          typeof item.issue_id === 'string' && typeof item.event_id === 'string' && W.safePath(item.issue_path) && typeof (item.headline || item.title) === 'string' && typeof item.summary === 'string' && Array.isArray(item.companies))) throw new Error('검색 월별 자료 형식이 올바르지 않습니다.');
    shards.set(descriptor.sha256, value.items); return value.items;
  }
  async function activate(host, params, force, current) {
    const filters = readFilters(params, W.kstDate());
    const progress = W.el('div', '검색 목록 확인 중…', 'wn-search-progress'); progress.setAttribute('role', 'status');
    const results = W.el('div', null, 'wn-search-results');
    host.replaceChildren(formFor(filters), progress, results);
    if (filters.from && filters.to && filters.from > filters.to) { results.append(W.message('날짜 범위 확인', '시작일이 종료일보다 늦습니다. 검색 조건을 수정하세요.')); return; }
    let value;
    try { value = await loadManifest(force); }
    catch (error) {
      if (!current()) return;
      progress.textContent = '검색 목록 조회 실패';
      results.append(W.message('검색 파일 로딩 실패', error.message + (manifest ? ' 마지막으로 확인한 검색 목록으로 조회합니다. 최신 검색 결과가 아닐 수 있습니다.' : ''), 'wn-error', () => activate(host, params, true, current)));
      if (!manifest) return; value = manifest;
    }
    if (!current()) return;
    const requested = selectedMonths(value.months, filters);
    const loaded = [], failed = [], recovered = []; let complete = 0, next = 0;
    const status = W.el('p', null, 'wn-muted');
    const resultBody = W.el('div'); results.append(status, resultBody);
    function render() {
      if (!current()) return;
      const all = filterItems(loaded.flat(), filters);
      const page = paginate(all, filters.page);
      status.textContent = (filters.period === 'all' ? '전체 기간' : filters.from + ' ~ ' + filters.to) + ' · ' + all.length + '건' +
        (complete < requested.length ? ' (로딩 중)' : failed.length ? ' (일부 월 누락 · 불완전한 검색)' : ' · 검색 완료') +
        (value.generated_at && Number.isFinite(Date.parse(value.generated_at)) ? ' · 인덱스 ' + W.kstDate(new Date(value.generated_at)) : '');
      resultBody.replaceChildren();
      if (failed.length && complete === requested.length) resultBody.append(W.message('일부 월 조회 실패', failed.map(f => f.month).join(', ') + ' 자료가 검색에서 누락되었습니다. 표시 건수는 전체 결과가 아닙니다.', 'wn-error', () => activate(host, params, true, current)));
      if (recovered.length && complete === requested.length) resultBody.append(W.message('마지막 검증 자료 표시', recovered.map(f => f.month).join(', ') + ' 재조회에 실패하여 동일 해시로 검증했던 월별 자료를 표시합니다. 다른 버전의 자료는 섞지 않습니다.', 'wn-pending', () => activate(host, params, true, current)));
      if (!all.length && complete === requested.length) resultBody.append(W.message(failed.length ? '조회한 월에서 결과 없음' : '검색 결과 없음', failed.length ? '실패한 월의 기사는 아직 확인하지 못했습니다.' : value.months.length ? '검색 조건에 맞는 발간 기사가 없습니다.' : '아직 공개된 발간 기사 이력이 없습니다.'));
      page.items.forEach(item => resultBody.append(W.articleCard(item, item.event_id, true)));
      if (all.length) {
        const pager = W.el('nav', null, 'wn-pagination'); pager.setAttribute('aria-label', '검색 결과 페이지');
        if (page.page > 1) pager.append(W.link('← 이전', buildHash({ ...filters, page: page.page - 1 }), 'wn-button'));
        pager.append(W.el('span', page.page + ' / ' + page.pages));
        if (page.page < page.pages) pager.append(W.link('다음 →', buildHash({ ...filters, page: page.page + 1 }), 'wn-button'));
        resultBody.append(pager);
      }
    }
    async function worker() {
      while (next < requested.length && current()) {
        const descriptor = requested[next++];
        try { loaded.push(await loadShard(descriptor, force)); }
        catch (error) {
          if (shards.has(descriptor.sha256)) { loaded.push(shards.get(descriptor.sha256)); recovered.push({ month: descriptor.month, message: error.message }); }
          else failed.push({ month: descriptor.month, message: error.message });
        }
        complete++;
        if (!current()) return;
        progress.textContent = '월별 자료 ' + complete + ' / ' + requested.length + ' 확인 · 성공 ' + (complete - failed.length - recovered.length) + ' · 마지막 검증 자료 ' + recovered.length + ' · 실패 ' + failed.length;
        render();
      }
    }
    await Promise.all(Array.from({ length: Math.min(3, requested.length) }, worker));
    if (!current()) return;
    progress.textContent = requested.length ? '월별 자료 ' + complete + ' / ' + requested.length + ' 확인 완료' + (recovered.length ? ' · 재조회 실패 ' + recovered.length + '개 월은 마지막 검증 자료 사용' : '') : '검색 대상 월 없음'; render();
  }
  api.activate = activate;
})(typeof globalThis !== 'undefined' ? globalThis : this);
