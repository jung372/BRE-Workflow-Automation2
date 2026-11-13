(function (root) {
  'use strict';
  const PREFIX = 'data/wind-map/';
  const SCOPES = { valid: '확인된 유효 EBL', review: '유효성 확인 중', approved_pending: '가결 · 허가일 미확인', cancelled: '취소·승계 종료', all: '전체 상태' };
  const COVERAGE = { complete: '관할 전체 확인', central_only: '3 MW 초과 대장 중심', partial: '일부 자료 확인', not_collected: '자료 미확보' };
  const TYPES = { construction_plan: '공사계획인가', environment: '환경영향평가', development: '개발행위허가', construction: '착공', inspection: '사용전검사', completion: '준공' };
  const STATES = { unknown: '공개자료 미확인', not_applied: '신청 전', applied: '신청', reviewing: '협의 중', conditional: '조건부 의결', approved: '허가·인가', planned: '예정', notice_received: '착공계 접수', not_started: '미착공 확인', in_progress: '공사 중 확인', completed: '완료 확인', passed: '합격', failed: '불합격', conflict: '자료 상충' };
  const EVENT_NAMES = { grant: '신규 허가', capacity_change: '용량 변경', registry_observation: '대장 확인', extend: '준비기간 연장', rename: '명칭 변경', transfer: '양수도', cancel: '허가취소', split_close: '분할 승계', merge_close: '통합 승계', approved_pending: '가결', business_commenced: '사업개시', validity_confirmed: '유효성 확인' };
  const idOK = value => typeof value === 'string' && /^[A-Za-z0-9_-]{1,100}$/.test(value);
  function readFilters(params = new URLSearchParams()) {
    const number = (key, fallback, min, max) => {
      const raw = params.get(key), value = Number(raw);
      return raw !== null && raw !== '' && Number.isFinite(value) && value >= min && value <= max ? value : fallback;
    };
    return { region: idOK(params.get('region')) ? params.get('region') : 'KR',
      scope: Object.hasOwn(SCOPES, params.get('scope')) ? params.get('scope') : 'valid',
      q: (params.get('q') || '').slice(0, 120), lat: number('lat', 36.1, 32, 40),
      lng: number('lng', 127.8, 124, 132), level: Math.round(number('level', 12, 1, 14)) };
  }
  function buildHash(filters, projectId) {
    const p = new URLSearchParams();
    for (const key of ['region', 'scope', 'q', 'lat', 'lng', 'level']) if (filters[key] !== undefined && filters[key] !== '') p.set(key, filters[key]);
    return '#/wind-map' + (projectId && idOK(projectId) ? '/project/' + projectId : '') + '?' + p.toString();
  }
  function mw(watts) {
    return watts === null || watts === undefined ? '미확인' : (watts / 1000000).toLocaleString('ko-KR', { maximumFractionDigits: 3 }) + ' MW';
  }
  function dateLabel(value) {
    if (!value || value.precision === 'unknown') return '일자 미확인';
    const { start, end, precision, estimated } = value;
    let label = precision === 'month' ? start.slice(0, 4) + '년 ' + Number(start.slice(5, 7)) + '월' :
      precision === 'year' ? start.slice(0, 4) + '년' :
      precision === 'range' ? start + ' ~ ' + end :
      precision === 'before' ? end + ' 이전' : precision === 'after' ? start + ' 이후' : start;
    return label + (estimated ? ' 추정' : '');
  }
  function sourceURL(value) {
    try {
      const u = new URL(value);
      return u.protocol === 'https:' && !u.username && !u.password && !/(token|password|secret|api_?key)=/i.test(u.search) ? u.href : null;
    } catch (_) { return null; }
  }
  function below(region, ancestor, regions) {
    const seen = new Set();
    while (region && !seen.has(region)) {
      if (region === ancestor) return true;
      seen.add(region); region = regions.find(r => r.id === region)?.parent_id;
    }
    return false;
  }
  function selectProjects(projects, filters, regions) {
    return projects.filter(p => p.permits.some(permit => (filters.scope === 'all' || permit.status === filters.scope) &&
      permit.allocations.some(a => below(a.region_id, filters.region, regions))));
  }
  function searchProjects(projects, query) {
    const q = query.normalize('NFC').toLocaleLowerCase();
    return q ? projects.filter(p => [p.name, ...(p.aliases || [])].some(s => s.normalize('NFC').toLocaleLowerCase().includes(q))).slice(0, 30) : [];
  }
  function projectCapacity(project, filters, regions) {
    return project.permits.filter(p => filters.scope === 'all' || p.status === filters.scope)
      .reduce((sum, p) => sum + p.allocations.filter(a => below(a.region_id, filters.region, regions)).reduce((n, a) => n + a.capacity_w, 0), 0);
  }
  function descriptorOK(d) {
    return d && /^[a-f0-9]{24}$/.test(d.snapshot_id) && /^[a-f0-9]{64}$/.test(d.sha256) &&
      d.manifest === PREFIX + 'versions/' + d.snapshot_id + '/manifest.json';
  }
  function relativeOK(path) {
    return /^(regions|index)\.json$/.test(path) || /^projects\/[A-Za-z0-9_-]{1,100}\.json$/.test(path);
  }
  async function verifyBytes(bytes, expected, crypto = root.crypto) {
    if (!/^[a-f0-9]{64}$/.test(expected || '') || !crypto?.subtle) throw new Error('자료 검증 기능을 사용할 수 없습니다.');
    const actual = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', bytes)), b => b.toString(16).padStart(2, '0')).join('');
    if (actual !== expected) throw new Error('자료 해시가 일치하지 않습니다.');
  }
  function createLoader(fetcher = root.fetch?.bind(root), crypto = root.crypto) {
    let good = null, pointer = null;
    async function json(path, hash) {
      const abort = new AbortController(), timer = setTimeout(() => abort.abort(), 15000);
      try {
        const response = await fetcher(path, { cache: 'no-store', signal: abort.signal });
        if (!response.ok) throw new Error('검증된 자료를 아직 불러올 수 없습니다.');
        const bytes = new Uint8Array(await response.arrayBuffer());
        if (bytes.byteLength > 30000000) throw new Error('자료 크기를 확인해야 합니다.');
        if (hash) await verifyBytes(bytes, hash, crypto);
        const data = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(bytes));
        if (!data || data.schema_version !== 1) throw new Error('자료 형식을 확인해야 합니다.');
        return data;
      } finally { clearTimeout(timer); }
    }
    async function version(d) {
      if (!descriptorOK(d)) throw new Error('자료 버전 경로가 올바르지 않습니다.');
      const manifest = await json(d.manifest, d.sha256);
      if (manifest.snapshot_id !== d.snapshot_id || typeof manifest.preview !== 'boolean' || !manifest.files ||
        !manifest.files['regions.json'] || !manifest.files['index.json'] ||
        Object.entries(manifest.files).some(([path, hash]) => !relativeOK(path) || !/^[a-f0-9]{64}$/.test(hash))) throw new Error('자료 목록을 확인해야 합니다.');
      const cache = new Map();
      const state = { descriptor: d, manifest, cache, stale: false };
      const [regions, index] = await Promise.all([part(state, 'regions.json'), part(state, 'index.json')]);
      if (!Array.isArray(regions.regions) || !Array.isArray(index.projects) ||
        Object.keys(SCOPES).some(scope => !Array.isArray(regions.totals?.[scope])) ||
        index.projects.some(p => !idOK(p.project_id) || !Array.isArray(p.permits)) ||
        regions.preview !== manifest.preview || regions.as_of !== manifest.as_of) throw new Error('지역·사업 목록을 확인해야 합니다.');
      for (const p of index.projects) if (!manifest.files['projects/' + p.project_id + '.json']) throw new Error('상세 자료가 누락되었습니다.');
      state.regions = regions; state.index = index;
      return state;
    }
    async function part(state, path) {
      if (!relativeOK(path) || !state.manifest.files[path]) throw new Error('등록되지 않은 자료입니다.');
      if (state.cache.has(path)) return state.cache.get(path);
      const value = await json(PREFIX + 'versions/' + state.descriptor.snapshot_id + '/' + path, state.manifest.files[path]);
      if (value.snapshot_id !== state.descriptor.snapshot_id) throw new Error('서로 다른 자료 버전입니다.');
      state.cache.set(path, value);
      return value;
    }
    async function load() {
      try {
        pointer = await json(PREFIX + 'current.json');
        try { good = await version(pointer.current); }
        catch (error) {
          if (!descriptorOK(pointer.previous)) throw error;
          good = await version(pointer.previous); good.stale = true;
        }
        return good;
      } catch (error) {
        if (good) { good.stale = true; return good; }
        throw error;
      }
    }
    async function detail(state, id) {
      if (!idOK(id) || !state.index.projects.some(p => p.project_id === id)) throw new Error('해당 사업을 찾을 수 없습니다.');
      try {
        const value = await part(state, 'projects/' + id + '.json');
        if (value.project_id !== id || !Array.isArray(value.sources) || !Array.isArray(value.milestones)) throw new Error('상세 자료를 확인해야 합니다.');
        return { state, detail: value };
      } catch (error) {
        if (!descriptorOK(pointer?.previous) || pointer.previous.snapshot_id === state.descriptor.snapshot_id) throw error;
        const prior = await version(pointer.previous);
        if (!prior.index.projects.some(p => p.project_id === id)) throw error;
        const value = await part(prior, 'projects/' + id + '.json');
        if (value.project_id !== id || !Array.isArray(value.sources) || !Array.isArray(value.milestones)) throw error;
        prior.stale = true; good = prior;
        return { state: prior, detail: value };
      }
    }
    return { load, detail };
  }
  const api = { readFilters, buildHash, mw, dateLabel, sourceURL, below, selectProjects, searchProjects, projectCapacity, descriptorOK, relativeOK, verifyBytes, createLoader };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.WindMap = api;
  if (!root.document) return;
  const el = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const link = (text, hash) => {
    const a = el('a', text); a.href = hash;
    if (hash.startsWith('#/wind-map')) a.addEventListener('click', event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      const [path, query = ''] = hash.split('?');
      const params = new URLSearchParams(query), current = new URLSearchParams(location.hash.split('?')[1] || '');
      for (const key of ['lat', 'lng', 'level', 'q']) {
        if (current.has(key)) params.set(key, current.get(key)); else params.delete(key);
      }
      event.preventDefault(); location.hash = path + '?' + params.toString();
    });
    return a;
  };
  const content = document.getElementById('windMapContent');
  const loader = createLoader();
  let state = null, generation = 0, activeRoute = null, scrollY = 0, sdkPromise = null, lastRouteHash = null, cleanupMap = () => {};
  function loadSDK() {
    if (sdkPromise) return sdkPromise;
    sdkPromise = (async () => {
      const response = await fetch('config/wind-map-public.json', { cache: 'no-store' });
      if (!response.ok) throw new Error('지도 연결 준비 중');
      const config = await response.json();
      if (!/^[a-f0-9]{32}$/i.test(config.kakao_javascript_key || '')) throw new Error('지도 연결 준비 중');
      return new Promise((resolve, reject) => {
        const script = document.createElement('script');
        const timer = setTimeout(() => reject(new Error('지도 응답 지연')), 12000);
        script.src = 'https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&libraries=clusterer&appkey=' + encodeURIComponent(config.kakao_javascript_key);
        script.onerror = () => { clearTimeout(timer); reject(new Error('지도 연결 오류')); };
        script.onload = () => {
          if (!root.kakao?.maps?.load) { clearTimeout(timer); reject(new Error('지도 연결 오류')); return; }
          root.kakao.maps.load(() => { clearTimeout(timer); resolve(root.kakao.maps); });
        };
        document.head.append(script);
      });
    })();
    return sdkPromise.catch(error => { sdkPromise = null; throw error; });
  }
  async function renderMap(host, projects, filters, ticket) {
    const fallback = el('div', null, 'wm-map-fallback');
    fallback.append(el('strong', '지도를 준비하고 있습니다'), el('p', '지역표와 사업 목록은 지도 연결과 관계없이 조회할 수 있습니다.'));
    host.append(fallback);
    try {
      const maps = await loadSDK();
      if (ticket !== generation || !host.isConnected) return;
      const canvas = el('div', null, 'wm-map-canvas');
      canvas.setAttribute('aria-label', '육상풍력 사업 위치 지도'); host.append(canvas);
      const map = new maps.Map(canvas, { center: new maps.LatLng(filters.lat, filters.lng), level: filters.level });
      map.addControl(new maps.ZoomControl(), maps.ControlPosition.RIGHT);
      const chooser = el('div', null, 'wm-marker-chooser'); chooser.hidden = true; host.append(chooser);
      const markerProjects = new Map();
      const markers = projects.filter(p => p.coordinate).map(p => {
        const marker = new maps.Marker({ position: new maps.LatLng(p.coordinate.latitude, p.coordinate.longitude), title: p.name });
        markerProjects.set(marker, p);
        maps.event.addListener(marker, 'click', () => {
          const same = projects.filter(q => q.coordinate && q.coordinate.latitude === p.coordinate.latitude && q.coordinate.longitude === p.coordinate.longitude);
          if (same.length === 1) location.hash = buildHash(filters, p.project_id);
          else choose(same);
        });
        return marker;
      });
      function choose(items) {
        chooser.replaceChildren(el('strong', '같은 위치의 사업 선택'));
        const close = el('button', '닫기'); close.type = 'button'; close.onclick = () => { chooser.hidden = true; };
        chooser.append(close);
        items.forEach(p => chooser.append(link(p.name, buildHash(filters, p.project_id))));
        chooser.hidden = false;
      }
      const clusterer = new maps.MarkerClusterer({ map, averageCenter: true, minLevel: 6, disableClickZoom: true, markers });
      const clusterClick = cluster => {
        const members = cluster.getMarkers().map(m => markerProjects.get(m)).filter(Boolean);
        if (map.getLevel() <= 6 || new Set(members.map(p => p.coordinate.longitude + ',' + p.coordinate.latitude)).size === 1) choose(members);
        else map.setLevel(Math.max(1, map.getLevel() - 2), { anchor: cluster.getCenter() });
      };
      maps.event.addListener(clusterer, 'clusterclick', clusterClick);
      const idle = () => {
        if (ticket !== generation) return;
        const center = map.getCenter();
        filters.lat = Number(center.getLat().toFixed(6)); filters.lng = Number(center.getLng().toFixed(6)); filters.level = map.getLevel();
        history.replaceState(null, '', buildHash(filters, activeRoute?.projectId));
        lastRouteHash = location.hash;
      };
      maps.event.addListener(map, 'idle', idle);
      cleanupMap = () => {
        maps.event.removeListener(map, 'idle', idle);
        maps.event.removeListener(clusterer, 'clusterclick', clusterClick);
        clusterer.clear(); markers.forEach(m => m.setMap(null));
      };
      fallback.remove();
    } catch (_) {
      if (ticket !== generation) return;
      fallback.replaceChildren(el('strong', '지도를 연결하지 못했습니다'), el('p', '아래 지역표와 사업 검색·상세 조회는 계속 사용할 수 있습니다.'));
    }
  }
  function stat(title, value, note) {
    const card = el('div', null, 'wm-stat');
    card.append(el('span', title), el('strong', value), el('small', note)); return card;
  }
  function renderOverview(data, filters, ticket) {
    const regions = data.regions.regions;
    if (!regions.some(r => r.id === filters.region)) filters.region = 'KR';
    const selected = regions.find(r => r.id === filters.region);
    const rowFor = scope => data.regions.totals[scope].find(r => r.region_id === filters.region);
    const row = rowFor(filters.scope);
    const intro = el('div', null, 'wm-intro');
    intro.append(el('p', 'ONSHORE WIND · PERMIT ATLAS', 'wm-eyebrow'), el('h1', '풍력대동여지도'),
      el('p', '전국 육상풍력의 허가 현황과 사업별 진행 근거를 살펴봅니다.', 'wm-muted'));
    const badges = el('div', null, 'wm-badges');
    badges.append(el('span', '기준일 ' + data.regions.as_of), el('span', '반영 분기 ' + (data.regions.quarters.join(' · ') || '미확인')));
    intro.append(badges); content.append(intro);
    if (data.manifest.preview) content.append(el('p', '검증용 예시 · 운영 통계가 아닙니다.', 'wm-warning'));
    if (data.stale) content.append(el('p', '최신 자료를 확인하지 못해 직전 정상본을 표시합니다. 기준일 ' + data.regions.as_of, 'wm-warning'));
    const filtersBar = el('div', null, 'wm-filters');
    const regionSelect = el('select'); regionSelect.setAttribute('aria-label', '조회 지역');
    regions.forEach(r => { const o = el('option', (r.parent_id && r.parent_id !== 'KR' ? '　' : '') + r.name); o.value = r.id; regionSelect.append(o); });
    regionSelect.value = filters.region;
    regionSelect.onchange = () => { filters.region = regionSelect.value; location.hash = buildHash(filters); };
    const scopeSelect = el('select'); scopeSelect.setAttribute('aria-label', '허가 상태');
    Object.entries(SCOPES).forEach(([value, name]) => { const o = el('option', name); o.value = value; scopeSelect.append(o); });
    scopeSelect.value = filters.scope; scopeSelect.onchange = () => { filters.scope = scopeSelect.value; location.hash = buildHash(filters); };
    const refresh = el('button', '자료 새로고침'); refresh.type = 'button'; refresh.onclick = () => api.activate({ ...activeRoute, params: new URLSearchParams(location.hash.split('?')[1] || '') }, true);
    filtersBar.append(regionSelect, scopeSelect, refresh); content.append(filtersBar);
    const stats = el('div', null, 'wm-stats');
    stats.append(stat(selected.name + ' · 유효 EBL', mw(rowFor('valid')?.display_capacity_w), (rowFor('valid')?.permit_count || 0) + '개 허가'),
      stat('유효성 확인 중', mw(rowFor('review')?.display_capacity_w), '만료·충돌·효력일 후속 확인'),
      stat('가결 · 허가일 미확인', mw(rowFor('approved_pending')?.display_capacity_w), '유효 EBL에 포함하지 않음'),
      stat('검사 확인 설비', mw(data.regions.inspection_totals?.find(r => r.region_id === filters.region)?.display_capacity_w), '설비별 합격 근거로 별도 집계'));
    content.append(stats);
    content.append(el('p', (COVERAGE[row?.coverage] || '자료 미확보') + ' · 선택 상태의 하위 지역 미배분 ' + mw(row?.unallocated_w) +
      ' · 검토 대기 ' + data.regions.pending_count + '건', 'wm-coverage'));
    const projects = selectProjects(data.index.projects, filters, regions);
    const mapShell = el('div', null, 'wm-map'); content.append(mapShell);
    content.append(el('p', '지도 숫자는 묶인 사업 마커 수입니다. 행정구역 합계는 지도 이동·확대와 관계없이 유지됩니다. 좌표 미확인 사업은 목록에 포함됩니다.', 'wm-muted'));
    renderMap(mapShell, projects, filters, ticket);
    const grid = el('div', null, 'wm-grid');
    const regionSection = el('section', null, 'wm-panel'); regionSection.append(el('h2', selected.name + ' 지역별 현황'));
    if (selected.parent_id) regionSection.append(link('← 상위 지역', buildHash({ ...filters, region: selected.parent_id })));
    const table = el('table', null, 'wm-table'); const head = el('thead'), hrow = el('tr');
    ['지역', SCOPES[filters.scope], '허가', '수집 범위'].forEach(t => hrow.append(el('th', t))); head.append(hrow); table.append(head);
    const body = el('tbody');
    const rows = data.regions.totals[filters.scope].filter(r => r.parent_id === filters.region);
    for (const r of rows.length ? rows : [row]) {
      if (!r) continue;
      const tr = el('tr'), td = el('td'); td.append(link(r.name, buildHash({ ...filters, region: r.region_id })));
      tr.append(td, el('td', mw(r.display_capacity_w)), el('td', r.has_observations ? String(r.permit_count) : '—'), el('td', COVERAGE[r.coverage])); body.append(tr);
    }
    table.append(body); regionSection.append(table);
    const projectSection = el('section', null, 'wm-panel');
    projectSection.append(el('h2', '선택 지역의 사업 ' + projects.length));
    const list = el('ul', null, 'wm-project-list');
    projects.forEach(p => {
      const item = el('li'), a = link(p.name, buildHash(filters, p.project_id));
      a.addEventListener('click', () => { scrollY = root.scrollY; });
      item.append(a, el('strong', mw(projectCapacity(p, filters, regions))), el('small', p.coordinate ? p.coordinate.approximate ? '필지 대표점 · 근사' : '근거 확인 위치' : '좌표 미확인'));
      list.append(item);
    });
    if (!projects.length) projectSection.append(el('p', '선택한 범위에서 확인된 사업이 없습니다. 자료 미확보 지역은 0 MW로 해석하지 않습니다.', 'wm-muted'));
    projectSection.append(list); grid.append(regionSection, projectSection); content.append(grid);
    const search = el('section', null, 'wm-panel wm-search'); search.append(el('h2', '전국 사업명·과거명 검색'));
    const input = el('input'); input.type = 'search'; input.placeholder = '사업명을 입력하세요'; input.setAttribute('aria-label', '전국 사업 검색'); input.value = filters.q;
    const results = el('ul', null, 'wm-project-list');
    function updateSearch() {
      filters.q = input.value.slice(0, 120);
      history.replaceState(null, '', buildHash(filters, activeRoute?.projectId));
        lastRouteHash = location.hash;
      results.replaceChildren();
      searchProjects(data.index.projects, filters.q).forEach(p => { const li = el('li'); li.append(link(p.name, buildHash(filters, p.project_id))); results.append(li); });
      if (filters.q && !results.children.length) results.append(el('li', '일치하는 공개 사업이 없습니다.'));
    }
    input.addEventListener('input', updateSearch); search.append(input, results); content.append(search);
    if (filters.q) updateSearch();
    content.append(el('p', '자료 확인 범위: ' + data.regions.known_at + ' · 버전 ' + data.descriptor.snapshot_id, 'wm-footnote'));
  }
  function renderDetail(detail, filters) {
    const panel = el('section', null, 'wm-detail'); panel.setAttribute('role', 'dialog'); panel.setAttribute('aria-modal', 'true'); panel.setAttribute('aria-labelledby', 'wmDetailTitle');
    const close = link('상세 닫기 ×', buildHash(filters)); close.className = 'wm-close';
    const title = el('h2', detail.name); title.id = 'wmDetailTitle'; title.tabIndex = -1;
    panel.append(close, el('p', detail.name_kind === 'register_name' ? '대장 표기' : '허가 사업명', 'wm-eyebrow'), title);
    if (detail.aliases?.length) panel.append(el('p', '과거명: ' + detail.aliases.map(a => a.name + (a.until ? ' (~' + a.until + ')' : '')).join(', ')));
    if (!detail.coordinate) panel.append(el('p', '확인된 좌표가 없어 지도 핀을 표시하지 않습니다.', 'wm-muted'));
    if (detail.coordinate?.approximate) panel.append(el('p', '필지 대표점 · 근사 위치', 'wm-muted'));
    const sourceMap = new Map(detail.sources.map(s => [s.id, s]));
    function evidence(ids) {
      const box = el('details', null, 'wm-evidence'); box.append(el('summary', '근거 ' + ids.length + '건'));
      ids.forEach(id => {
        const s = sourceMap.get(id); if (!s) return;
        const article = el('article');
        article.append(el('strong', s.agency + ' · ' + s.title), el('p', [s.document_number, s.page ? s.page + '쪽' : '', s.section].filter(Boolean).join(' · ')),
          el('blockquote', s.quote), el('small', '확인일 ' + s.checked_at));
        const url = sourceURL(s.url);
        if (url) { const a = link('공개 원문 열기 ↗', url); a.target = '_blank'; a.rel = 'noopener noreferrer'; article.append(a); }
        box.append(article);
      });
      return box;
    }
    detail.permits.forEach(p => {
      const section = el('section', null, 'wm-permit');
      section.append(el('h3', p.authority + ' · ' + p.number), el('strong', mw(p.capacity_w) + ' · ' + SCOPES[p.status]));
      const dl = el('dl');
      [['최초 EBL', p.first_ebl_date ? dateLabel(p.first_ebl_date) : '최초 허가일 미확인'],
        ['현 용량 EBL', dateLabel(p.capacity_ebl_date)], ['사업준비기한', dateLabel(p.preparation_deadline)],
        ['공사계획 인가기간 종료', dateLabel(p.construction_plan_deadline)], ['현 사업자', p.operator || '공개자료 미확인']].forEach(([k, v]) => { dl.append(el('dt', k), el('dd', v)); });
      section.append(dl);
      if (p.conflicts.length) section.append(el('p', '자료 확인 중 · 효력일 또는 유효성에 후속 확인이 필요합니다.', 'wm-warning'));
      section.append(evidence(p.evidence_ids));
      const history = el('details'); history.append(el('summary', '허가 변경 이력'));
      p.history.forEach(event => {
        const item = el('div', null, 'wm-history');
        item.append(el('p', dateLabel(event.date) + ' · ' + (EVENT_NAMES[event.type] || event.type) + (event.capacity_w !== undefined ? ' · ' + mw(event.capacity_w) : '')));
        if (event.previous_capacity_w !== undefined && event.capacity_w !== undefined) item.append(el('small', '변경량 ' + ((event.capacity_w - event.previous_capacity_w) >= 0 ? '+' : '') + mw(event.capacity_w - event.previous_capacity_w)));
        item.append(evidence([event.evidence_id])); history.append(item);
      });
      section.append(history); panel.append(section);
    });
    const milestones = el('section'); milestones.append(el('h3', '인허가·공사 진행'));
    Object.entries(TYPES).forEach(([type, name]) => {
      const group = el('div', null, 'wm-milestone'); group.append(el('h4', name));
      const rows = detail.milestones.filter(m => m.type === type);
      if (!rows.length) group.append(el('p', '공개자료 미확인', 'wm-muted'));
      rows.forEach(m => {
        const assessment = { main: '환경영향평가', small: '소규모 평가', strategic: '전략 평가' };
        const stage = { draft: '초안', main: '본안', reconsultation: '재협의', change: '변경협의' };
        const context = [m.disposition_kind === 'initial' ? '최초' : m.disposition_kind === 'amendment' ? '변경' : '', m.assessment_type ? assessment[m.assessment_type] : '', m.stage ? stage[m.stage] : '',
          m.round ? m.round + '차' : '', m.equipment?.join(' · ')].filter(Boolean).join(' / ');
        group.append(el('p', (m.conflict ? '자료 상충 · ' : '') + context + (context ? ' · ' : '') + (m.type === 'environment' && m.status === 'applied' ? '신청 후 협의 중' : STATES[m.status]) + ' · ' + dateLabel(m.date)),
          el('small', '적용 허가: ' + m.permit_ids.map(id => detail.permits.find(p => p.permit_id === id)?.number || id).join(', ')), evidence([m.evidence_id]));
      });
      milestones.append(group);
    });
    if (detail.construction_start_window) {
      const w = detail.construction_start_window;
      milestones.append(el('p', w.inferred_month ? w.inferred_month + ' 착공 추정' : dateLabel(w.date) + ' 사이 착공'), evidence(w.evidence_ids));
    }
    if (detail.inspection) milestones.append(el('p', (detail.inspection.all_passed ? '전 설비 검사 완료 · ' : '확인된 검사 설비 · ') + mw(detail.inspection.capacity_w)), evidence(detail.inspection.evidence_ids));
    panel.append(milestones);
    document.body.classList.add('wm-dialog-open');
    const sidebar = document.querySelector('.site-sidebar'); if (sidebar) sidebar.inert = true;
    [...content.children].forEach(child => { child.inert = true; });
    const backdrop = el('div', null, 'wm-backdrop'); backdrop.append(panel); content.append(backdrop);
    title.focus();
    panel.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); location.hash = buildHash(filters); }
      if (event.key === 'Tab') {
        const focusable = [...panel.querySelectorAll('a,button,input,select,summary,[tabindex="0"]')];
        if (!focusable.length) return;
        const first = focusable[0], last = focusable.at(-1);
        if (event.shiftKey && (document.activeElement === first || document.activeElement === title)) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      }
    });
  }
  api.activate = async function (route, force) {
    if (route.page !== 'wind-map') {
      ++generation; cleanupMap(); cleanupMap = () => {}; lastRouteHash = null;
      document.body.classList.remove('wm-dialog-open');
      const sidebar = document.querySelector('.site-sidebar'); if (sidebar) sidebar.inert = false;
      return;
    }
    if (!force && lastRouteHash === location.hash && activeRoute?.page === 'wind-map') return;
    lastRouteHash = location.hash;
    document.body.classList.remove('wm-dialog-open');
    const sidebar = document.querySelector('.site-sidebar'); if (sidebar) sidebar.inert = false;
    const ticket = ++generation; cleanupMap(); cleanupMap = () => {};
    const returning = !!activeRoute?.projectId && !route.projectId;
    if (!activeRoute?.projectId && route.projectId) scrollY = root.scrollY;
    activeRoute = route;
    const filters = readFilters(route.params);
    content.replaceChildren(el('p', '검증된 자료를 불러오고 있습니다…', 'wm-loading'));
    try {
      if (!state || force) state = await loader.load();
      if (ticket !== generation) return;
      let detail = null, detailError = null;
      if (route.projectId) {
        try { const loaded = await loader.detail(state, route.projectId); state = loaded.state; detail = loaded.detail; }
        catch (error) { detailError = error.message; }
      }
      if (ticket !== generation) return;
      content.replaceChildren(); renderOverview(state, filters, ticket);
      if (detail) renderDetail(detail, filters);
      else if (detailError) {
        const box = el('div', null, 'wm-warning'); box.append(el('p', detailError), link('사업 목록으로 돌아가기', buildHash(filters))); content.prepend(box);
      }
      if (returning) requestAnimationFrame(() => root.scrollTo(0, scrollY));
    } catch (_) {
      if (ticket !== generation) return;
      content.replaceChildren(el('h1', '풍력대동여지도'), el('p', '검증된 공개 자료를 준비하고 있습니다.', 'wm-empty'),
        el('p', '원본 대사와 지역별 검산을 마친 자료부터 표시합니다. 자료가 없는 상태를 0 MW로 표시하지 않습니다.', 'wm-muted'));
      const retry = el('button', '다시 불러오기'); retry.type = 'button'; retry.onclick = () => api.activate(route, true); content.append(retry);
    }
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
