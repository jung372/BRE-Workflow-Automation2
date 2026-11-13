const DATA_URL         = 'data/status.json';
const AUTO_REFRESH_SEC = 300;
const KW_COLLAPSED_MAX = 2;   // 접힌 상태에서 보여줄 칩 개수
let countdown  = AUTO_REFRESH_SEC;
let autoTimer, countdownTimer;

// 칩 펼침 상태는 카드 밖에 보관한다. render()가 5분마다 호출되므로 DOM에만
// 두면 자동 갱신마다 접혀버린다.
function kwExpanded(id) {
  try { return localStorage.getItem('kwExpanded:' + id) === '1'; } catch(e) { return false; }
}
function setKwExpanded(id, on) {
  try { localStorage.setItem('kwExpanded:' + id, on ? '1' : '0'); } catch(e) {}
}

function escapeHtml(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function toggleKeywords(id) {
  setKwExpanded(id, !kwExpanded(id));
  const host = document.getElementById('kw-' + id);
  if (host) host.innerHTML = keywordChips(id, JSON.parse(host.dataset.keywords || '[]'));
}

// 카드 헤더는 최초 1회만 생성되므로, 키워드가 서버에서 바뀌어도 반영되지
// 않는다. 그래서 칩 영역만 별도 요소로 두고 매 렌더마다 다시 그린다.
function keywordChips(id, keywords) {
  if (!keywords || !keywords.length) return '';
  const expanded = kwExpanded(id);
  const shown    = expanded ? keywords : keywords.slice(0, KW_COLLAPSED_MAX);
  const hidden   = keywords.length - shown.length;
  const chips    = shown.map(k => `<span class="kw-chip">${escapeHtml(k)}</span>`).join('');
  const label    = expanded ? '▴' : (hidden > 0 ? `+${hidden} ▾` : '▾');
  return `${chips}<button type="button" class="kw-toggle" aria-expanded="${expanded}"
      title="감시 키워드 ${keywords.length}개: ${escapeHtml(keywords.join(', '))}"
      onclick="toggleKeywords('${id}')">${label}</button>`;
}

async function loadData() {
  setBtnLoading(true);
  try {
    const res = await fetch(DATA_URL + '?t=' + Date.now());
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    render(await res.json());
  } catch(e) {
    if (!location.hash.startsWith('#/wind-map')) showToast('❌ 데이터 로드 실패. 잠시 후 다시 시도하세요.', 4000);
  } finally {
    setBtnLoading(false);
  }
}

function refresh() { resetAutoTimer(); loadData(); }

function render(data) {
  ['skel1','skel2','skel3','skel4','skel5','skel6'].forEach(id => {
    const el = document.getElementById(id); if (el) el.remove();
  });

  const isFirstTime = !data.checked_at || data.checked_at === '-';
  document.getElementById('statTime').textContent  = isFirstTime ? '--:--' : data.checked_at.slice(0, 16);

  const totalNew = data.sites.reduce((s, x) => s + (x.new_count || 0), 0);
  document.getElementById('statTotal').textContent = totalNew;
  document.getElementById('statSites').textContent = data.sites.length;
  document.getElementById('statsBar').style.display = 'flex';

  const container = document.getElementById('cards');

  data.sites.forEach(site => {
    let card = document.getElementById('card-' + site.id);
    if (!card) {
      card = document.createElement('div');
      card.className = 'card';
      card.id = 'card-' + site.id;
      card.setAttribute('data-id', site.id);
      let hostname = site.url;
      try { hostname = new URL(site.url).hostname; } catch(e) {}
      card.innerHTML = `
        <div class="card-header">
          <div style="display:flex;align-items:center;gap:12px;">
            <div class="card-icon" style="margin-bottom:0;">${site.icon || '📌'}</div>
            <div>
              <div class="card-name">${site.name}</div>
              <div class="card-url">${hostname}</div>
              <div class="kw-wrap" id="kw-${site.id}"></div>
            </div>
          </div>
        </div>
        <div class="new-count-wrap" id="count-wrap-${site.id}">
          <div class="new-count-label">신규 등록 게시글</div>
          <div class="count-content" id="count-content-${site.id}">
            <div class="skeleton sk-count"></div>
          </div>
        </div>
        <div class="items-wrap" id="items-wrap-${site.id}"></div>
        <div class="card-footer">
          <div class="total-count" id="total-${site.id}">전체 0개</div>
          <a class="visit-link" href="${site.url}" target="_blank" rel="noopener">
            방문하기 <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </a>
        </div>`;
      container.appendChild(card);
    }

    // 키워드가 바뀌어도 반영되도록 칩 영역은 매 렌더마다 다시 그린다.
    const kwHost = document.getElementById(`kw-${site.id}`);
    if (kwHost) {
      const kws = site.keywords || [];
      kwHost.dataset.keywords = JSON.stringify(kws);
      kwHost.innerHTML = keywordChips(site.id, kws);
    }

    const hasNew       = site.new_count > 0;
    const isErr        = !!site.error;
    const countContent = document.getElementById(`count-content-${site.id}`);
    const itemsWrap    = document.getElementById(`items-wrap-${site.id}`);
    const totalText    = document.getElementById(`total-${site.id}`);

    if (isErr) {
      countContent.innerHTML = `<div class="new-count is-error">오류</div><div class="error-msg">${site.error}</div>`;
    } else {
      countContent.innerHTML = `
        <a class="new-count-link" href="${site.url}" target="_blank" rel="noopener" title="바로가기">
          <div class="new-count ${hasNew ? 'has-new' : ''}">${site.new_count}</div>
        </a>`;
      itemsWrap.innerHTML = renderItems(site);
      totalText.textContent = `총 확보 ${site.total || 0}건 데이터`;
    }
  });

  if (totalNew > 0) showToast(`✨ 총 ${totalNew}건의 새 게시글이 감지되었습니다!`, 4000);

  // 풍황계측기 카드
  const mData = data.metmasts || [
    { name: 'SIRU', status: 'Offline' }, { name: 'GOGK', status: 'Offline' },
    { name: 'BLMU', status: 'Offline' }, { name: 'DKAM', status: 'Offline' }
  ];
  let rows = '', onlineCnt = 0;
  mData.forEach((m, idx) => {
    const isOnline = m.status === 'Online';
    if (isOnline) onlineCnt++;
    const color = isOnline ? 'var(--green)' : 'var(--red)';
    const pulse = isOnline ? `animation:pulse 2s infinite;animation-delay:${idx * 0.5}s;` : '';
    const link  = m.url || '#';
    rows += `
      <div style="display:flex;justify-content:space-between;align-items:center;background:rgba(0,0,0,0.3);padding:6px 14px;border-radius:8px;border:1px solid rgba(255,255,255,0.05);">
        <div style="font-size:14px;font-weight:700;color:#fff;letter-spacing:1px;">${m.name}</div>
        <div style="display:flex;align-items:center;gap:10px;">
          <span style="font-size:11px;font-weight:600;color:${color};">${m.status}</span>
          <div style="width:10px;height:10px;border-radius:50%;background:${color};box-shadow:0 0 8px ${color};${pulse}"></div>
          <a class="visit-link" href="${link}" target="_blank" rel="noopener" style="padding:4px 10px;font-size:11px;">접속</a>
        </div>
      </div>`;
  });

  const metmastInner = `
    <div class="new-count-wrap" style="padding:10px 14px;margin-bottom:0;background:transparent;border:none;">
      <div style="display:flex;flex-direction:column;gap:6px;">${rows}</div>
    </div>
    <div class="card-footer" style="margin-top:2px;">
      <div class="total-count">현재 접속 가능 기기: ${onlineCnt}/${mData.length}대</div>
    </div>`;

  let mockCard = document.getElementById('card-metmast');
  if (!mockCard) {
    mockCard = document.createElement('div');
    mockCard.className = 'card';
    mockCard.id = 'card-metmast';
    mockCard.setAttribute('data-id', 'metmast');
    mockCard.innerHTML = `
      <div class="card-header" style="margin-bottom:8px;padding-bottom:8px;">
        <div style="display:flex;align-items:center;gap:12px;">
          <div class="card-icon" style="margin-bottom:0;font-size:36px;">📡</div>
          <div><div class="card-name">풍황계측기 모니터링</div></div>
        </div>
      </div>` + metmastInner;
    container.appendChild(mockCard);
  } else {
    mockCard.querySelector('.new-count-wrap').outerHTML;
    const header = mockCard.querySelector('.card-header');
    mockCard.innerHTML = header.outerHTML + metmastInner;
  }
}

function renderItems(site) {
  if (site.new_count === 0)
    return `<div class="no-new-msg"><span style="font-size:18px;">✅</span> 새 게시글 없음</div>`;
  const items = (site.new_items || []).slice(0, 5);
  const rows  = items.map((item, i) => {
    // 발송처와 매칭 키워드는 기존 날짜와 같은 줄에 붙여 높이를 늘리지 않는다.
    const meta = [
      item.dept ? escapeHtml(item.dept) : '',
      item.date || '',
    ].filter(Boolean).join(' · ');
    const badge = item.keyword
      ? `<span class="kw-badge">${escapeHtml(item.keyword)}</span>` : '';
    return `
    <div class="new-item" style="animation-delay:${i * 0.05}s">
      <div class="new-item-dot"></div>
      <div>
        <a href="${item.url || site.url}" target="_blank" rel="noopener">${item.title}</a>
        <div class="new-item-date">${meta}${badge}</div>
      </div>
    </div>`;
  }).join('');
  return `<div class="new-items">${rows}
    ${site.new_count > items.length
      ? `<div style="font-size:13px;color:rgba(255,255,255,0.7);margin-top:10px;text-align:right;">외 ${site.new_count - items.length}건 더보기 →</div>`
      : ''}
  </div>`;
}

function resetAutoTimer() {
  clearInterval(autoTimer); clearInterval(countdownTimer);
  countdown = AUTO_REFRESH_SEC;
  updateCountdown();
  countdownTimer = setInterval(() => { countdown = Math.max(0, countdown - 1); updateCountdown(); }, 1000);
  autoTimer      = setInterval(() => { countdown = AUTO_REFRESH_SEC; loadData(); }, AUTO_REFRESH_SEC * 1000);
}

function updateCountdown() {
  const pct = (countdown / AUTO_REFRESH_SEC) * 100;
  document.getElementById('autoBar').style.width = pct + '%';
  document.getElementById('autoCountdown').textContent =
    countdown >= 60 ? Math.ceil(countdown / 60) + 'm' : countdown + 's';
}

function setBtnLoading(on) {
  const btn  = document.getElementById('btnRefresh');
  const icon = document.getElementById('refreshIcon');
  btn.disabled  = on;
  icon.className = on ? 'spin' : '';
}

function showToast(msg, ms = 3000) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(() => t.classList.remove('show'), ms);
}

loadData();
resetAutoTimer();
