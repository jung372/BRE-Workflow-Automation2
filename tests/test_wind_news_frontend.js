'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const { webcrypto, createHash } = require('node:crypto');
const vm = require('node:vm');
const fs = require('node:fs');
const N = require('../assets/navigation.js');
const W = require('../assets/wind-news.js');
const S = require('../assets/wind-news-search.js');

test('routes preserve existing default, dated issues and archive parameters', () => {
  assert.equal(N.parseRoute('').page, 'notices');
  assert.equal(N.parseRoute('#/daily').tab, 'today');
  assert.equal(N.parseRoute('#/daily/dates').tab, 'dates');
  assert.equal(N.parseRoute('#/daily/2026-09-30?event=x').date, '2026-09-30');
  assert.equal(N.parseRoute('#/daily/2026-09-30?event=x').params.get('event'), 'x');
  assert.equal(N.parseRoute('#/daily/2026-02-30').page, 'notices');
  assert.equal(N.parseRoute('#/daily/archive?q=%ED%95%B4%EC%83%81').params.get('q'), '해상');
});
test('today and publication delay are computed in KST across midnight', () => {
  assert.equal(W.statusForDate(null, new Date('2026-09-29T23:15:00Z')), '첫 브리핑 발간 준비 중');
  assert.equal(W.kstDate(new Date('2026-09-29T15:01:00Z')), '2026-09-30');
  assert.equal(W.statusForDate('2026-09-29', new Date('2026-09-29T23:14:59Z')), '오늘 브리핑 준비 중');
  assert.equal(W.statusForDate('2026-09-29', new Date('2026-09-29T23:15:00Z')), '오늘 브리핑 발행 지연');
  assert.equal(W.statusForDate('2026-09-30', new Date('2026-09-29T23:15:00Z')), '오늘 브리핑 발행 완료');
});
test('artifact paths cannot escape the project site root', () => {
  assert.equal(W.safePath('data/wind-news/issues/2026-09-30-r1.json'), true);
  assert.equal(W.safePath('data/wind-news/search/2026-09.json', 'search'), true);
  for (const path of ['../secret.json', '/data/wind-news/issues/x.json', 'data/wind-news/issues/../../x.json', 'data/wind-news/issues/%2e%2e/x.json', 'data/wind-news/issues/a\\b.json', 'data/wind-news/issues/x.json?token=x', 'https://host/x.json', 'data/wind-news/issues//x.json']) assert.equal(W.safePath(path), false, path);
  assert.equal(new URL('data/wind-news/issues/x.json', 'https://jung372.github.io/BRE-Workflow-Automation2/').pathname, '/BRE-Workflow-Automation2/data/wind-news/issues/x.json');
});
test('only HTTPS source links without credentials are allowed', () => {
  assert.equal(W.safeSource('https://example.com/news'), 'https://example.com/news');
  for (const url of ['javascript:alert(1)', 'data:text/html,x', 'http://example.com', 'https://user:pass@example.com', '/relative']) assert.equal(W.safeSource(url), null);
});
test('bytes must match the manifest SHA-256 exactly', async () => {
  const bytes = new TextEncoder().encode('{"schema_version":1}');
  const digest = createHash('sha256').update(bytes).digest('hex');
  await W.verifyBytes(bytes, digest, webcrypto);
  await assert.rejects(() => W.verifyBytes(bytes, '0'.repeat(64), webcrypto), /해시/);
});
test('recent 30 days only loads intersecting months, all includes historical years', () => {
  const months = ['2024-01', '2026-08', '2026-09'].map(month => ({ month }));
  const recent = S.readFilters(new URLSearchParams(), '2026-09-05');
  assert.equal(recent.from, '2026-08-07');
  assert.deepEqual(S.selectedMonths(months, recent).map(d => d.month), ['2026-08', '2026-09']);
  const all = S.readFilters(new URLSearchParams('period=all'), '2026-09-05');
  assert.equal(S.selectedMonths(months, all).length, 3);
});
test('archive URL round trips combined filters and pagination', () => {
  const original = { period: 'all', from: '2024-01-01', to: '2026-09-30', q: '터빈 공급', company: 'A & B', project: '서해', region: '전남', wind: '해상', category: 'EPC', sort: 'oldest', page: 3 };
  const route = N.parseRoute(S.buildHash(original));
  assert.deepEqual(S.readFilters(route.params, '2026-09-30'), original);
});
test('latest revision removes old events and does not collapse follow-up events', () => {
  const items = [
    { issue_id: 'a', revision: 1, event_id: 'removed', issue_date: '2024-01-01' },
    { issue_id: 'a', revision: 2, event_id: 'new', issue_date: '2024-01-01' },
    { issue_id: 'a', revision: 2, event_id: 'new', issue_date: '2024-01-01' },
    { issue_id: 'b', revision: 1, event_id: 'new', issue_date: '2026-09-30' }
  ];
  assert.deepEqual(S.latestRevisionItems(items).map(i => i.issue_id + ':' + i.event_id), ['a:new', 'b:new']);
});
test('compound archive filters match title/summary and metadata with date ordering', () => {
  const base = { issue_id: 'a', revision: 1, event_id: 'e', issue_date: '2026-09-29', title: '터빈 계약', summary: '공급 확정', companies: ['기업 A'], project_name: '서해 프로젝트', region: '전남', wind_type: '해상풍력', primary_category: 'EPC' };
  const filters = S.readFilters(new URLSearchParams('q=공급&company=기업&project=서해&region=전남&wind=해상&category=EPC'), '2026-09-30');
  assert.equal(S.filterItems([base], filters).length, 1);
  assert.equal(S.filterItems([base], { ...filters, q: '미일치' }).length, 0);
  assert.equal(S.filterItems([base], { ...filters, from: '2026-09-30' }).length, 0);
});
test('archive pagination clamps pages and retains all-history results', () => {
  const items = Array.from({ length: 31 }, (_, i) => i);
  assert.deepEqual(S.paginate(items, 2).items, items.slice(15, 30));
  assert.equal(S.paginate(items, 99).page, 3);
  assert.equal(S.paginate(items, 99).items.length, 1);
  assert.equal(S.paginate([], 4).pages, 1);
});
test('canonical headline is searched without requiring a legacy title', () => {
  const item = { issue_id: 'a', revision: 1, event_id: 'e', issue_date: '2026-09-30', headline: '국내 풍력 공급', summary: '확인한 사실' };
  const filters = S.readFilters(new URLSearchParams('q=공급'), '2026-09-30');
  assert.equal(S.filterItems([item], filters).length, 1);
});
test('invalid issue bodies cannot replace last valid report', () => {
  const descriptor = { issue_id: 'a', issue_date: '2026-09-30', revision: 2 };
  const issue = { ...descriptor, headline_summary: ['확인한 사실'], items: [{ headline: '제목', summary: '요약', companies: ['기업'] }] };
  assert.equal(W.validIssue(issue, descriptor), true);
  assert.equal(W.validIssue({ ...issue, revision: 1 }, descriptor), false);
  assert.equal(W.validIssue({ ...issue, items: [{ headline: '제목', summary: '요약', companies: '기업' }] }, descriptor), false);
  assert.equal(W.validIssue({ ...issue, headline_summary: '요약' }, descriptor), false);
  assert.equal(W.validIssue({ ...issue, corrections: {} }, descriptor), false);
});
test('five years of daily fifteen-article history supports all-period and compound filters', () => {
  // Synthetic public metadata stays in memory, never in data/wind-news.
  const items = [];
  const initialDay = Date.parse('2021-01-01T00:00:00Z');
  for (let day = 0; day < 5 * 365; day++) {
    const date = new Date(initialDay + day * 86400000).toISOString().slice(0, 10);
    for (let number = 0; number < 15; number++) items.push({
      issue_id: date, issue_date: date, revision: 1, event_id: date + '-' + number,
      headline: '풍력 ' + number, summary: '확인한 사업 변화', companies: [number % 2 ? '기업 B' : '기업 A'],
      project_name: number % 3 ? '프로젝트 서해' : '프로젝트 동해', region: '전남', wind_type: '해상', primary_category: 'EPC'
    });
  }
  const all = S.readFilters(new URLSearchParams('period=all'), '2026-09-30');
  assert.equal(items.length, 5 * 365 * 15);
  assert.equal(S.filterItems(items, all).length, 27375);
  const compound = S.readFilters(new URLSearchParams('period=all&from=2024-01-01&to=2024-01-31&company=기업 A&project=프로젝트 동해&q=사업'), '2026-09-30');
  const found = S.filterItems(items, compound);
  assert.equal(found.length, 31 * 3); // Items 0, 6 and 12 in each issue.
  assert.equal(found[0].issue_date, '2024-01-31');
  assert.equal(found.at(-1).issue_date, '2024-01-01');
  assert.equal(S.paginate(found, 2).items.length, 15);
  assert.equal(S.paginate(found, 999).pages, 7);
  const original = items.find(item => item.issue_date === '2024-01-15' && item.event_id.endsWith('-0'));
  // A corrected issue entirely replaces that issue's original fifteen entries.
  const revision = { ...original, revision: 2, summary: '정정한 사업 변화' };
  const corrected = S.filterItems([...items, revision, revision], all);
  assert.equal(corrected.length, items.length - 14);
  assert.equal(corrected.filter(item => item.issue_date === '2024-01-15').length, 1);
  assert.equal(corrected.find(item => item.issue_date === '2024-01-15').revision, 2);
});
test('explicit artifact retry bypasses cached bytes after transient hash mismatch', async () => {
  const artifact = new TextEncoder().encode('{"schema_version":1,"items":[]}');
  const mismatch = new TextEncoder().encode('{"schema_version":1,"items":["stale"]}');
  const expected = createHash('sha256').update(artifact).digest('hex');
  const requests = [];
  const context = vm.createContext({
    document: { baseURI: 'https://example.com/BRE-Workflow-Automation2/', getElementById: () => ({}) },
    crypto: webcrypto, URL, TextDecoder, AbortController,
    setTimeout: () => 1, clearTimeout: () => {}, setInterval: () => 1,
    fetch: async (url, options) => {
      requests.push({ url: url.href, cache: options.cache });
      return { ok: true, arrayBuffer: async () => (requests.length === 1 ? mismatch : artifact).buffer };
    }
  });
  vm.runInContext(fs.readFileSync(require.resolve('../assets/wind-news.js'), 'utf8'), context);
  await assert.rejects(() => context.WindNews.fetchJSON('data/wind-news/search/2026-09.json', expected), /해시/);
  const result = await context.WindNews.fetchJSON('data/wind-news/search/2026-09.json', expected, true);
  assert.equal(result.items.length, 0);
  assert.deepEqual(requests.map(request => request.cache), ['default', 'no-store']);
  assert.equal(requests[1].url, 'https://example.com/BRE-Workflow-Automation2/data/wind-news/search/2026-09.json');
});
test('canonical public status and editorial metrics have Korean labels', () => {
  assert.equal(W.statusLabel('partial'), '일부 수집·검증 제한');
  assert.equal(W.statusLabel('PARTIAL'), '일부 수집·검증 제한');
  assert.equal(W.countLabel('fetched'), '수집 기사');
  assert.equal(W.countLabel('eligible_articles'), '적합 기사');
  assert.equal(W.countLabel('merged_duplicates'), '중복 통합');
  assert.equal(W.countLabel('published_topics'), '게시 사건');
  assert.equal(W.timestampBasisLabel('naver_pubDate'), '네이버 뉴스 API 제공 발행시각');
});
test('canonical correction reason and time are displayed along with legacy history', () => {
  const note = { reason: '계약 단계 정정', corrected_at: '2026-09-30T03:00:00Z' };
  assert.deepEqual(W.correctionEntries({ correction_reason: note.reason, corrected_at: note.corrected_at }), [note]);
  assert.deepEqual(W.correctionEntries({ correction_reason: note.reason, corrected_at: note.corrected_at, corrections: [note, '이전 호 문구 정정'] }), [note, { reason: '이전 호 문구 정정' }]);
});
