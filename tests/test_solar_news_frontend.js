'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { webcrypto, createHash } = require('node:crypto');
const N = require('../assets/navigation.js');
const wind = require('../assets/wind-news.js');
const search = require('../assets/wind-news-search.js');
const solar = wind.create('solar');
const solarSearch = search.create('solar');

test('solar routes and filters round-trip without changing wind bookmarks', () => {
  for (const suffix of ['', '/dates', '/archive', '/2026-10-16']) {
    assert.equal(N.parseRoute('#/solar' + suffix).page, 'solar');
    assert.equal(N.parseRoute('#/daily' + suffix).page, 'daily');
  }
  assert.equal(N.parseRoute('#/solar/2026-02-30').page, 'notices');
  const f = solarSearch.readFilters(new URLSearchParams('period=all&solar=지붕형&q=태양광&company=기업'), '2026-10-16');
  const hash = solarSearch.buildHash(f);
  assert.ok(hash.startsWith('#/solar/archive?'));
  assert.deepEqual(solarSearch.readFilters(N.parseRoute(hash).params, '2026-10-16'), f);
  assert.ok(search.buildHash({ q: '풍력' }).startsWith('#/daily/archive?'));
});

test('artifact allowlists reject cross-industry, traversal and remote paths', () => {
  assert.equal(solar.safePath('data/solar-news/issues/2026/10/2026-10-16.r1.json'), true);
  assert.equal(solar.safePath('data/wind-news/issues/test.json'), false);
  assert.equal(wind.safePath('data/solar-news/issues/test.json'), false);
  for (const path of ['data/solar-news/issues/../../secret.json', 'https://example.kr/solar.json', 'data/solar-news/search/../x.json']) {
    assert.equal(solar.safePath(path), false);
  }
  assert.throws(() => wind.create('other'));
});

test('solar search uses solar type and retains independent date/keyword filters', () => {
  const filters = solarSearch.readFilters(new URLSearchParams('solar=영농형&q=태양광&company=대한'), '2026-10-16');
  const item = { issue_id: 'solar-2026-10-16', revision: 1, event_id: 'e', issue_date: '2026-10-16', headline: '태양광 EPC', summary: '계약 체결', companies: ['대한발전'], solar_type: '영농형' };
  assert.equal(solarSearch.filterItems([item], filters).length, 1);
  assert.equal(solarSearch.filterItems([{ ...item, solar_type: '수상형' }], filters).length, 0);
});

test('browser module instances never fetch hashed artifacts from another industry', async () => {
  const bytes = new TextEncoder().encode('{"schema_version":1}');
  const hash = createHash('sha256').update(bytes).digest('hex');
  const requested = [];
  const context = { console, URL, URLSearchParams, TextDecoder, AbortController, crypto: webcrypto,
    setTimeout, clearTimeout, setInterval: () => 0,
    document: { baseURI: 'https://example.kr/site/', getElementById: () => ({}) },
    fetch: async url => { requested.push(String(url)); return { ok: true, arrayBuffer: async () => bytes.buffer }; }
  };
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(require.resolve('../assets/wind-news.js'), 'utf8'), context);
  await context.SolarNews.fetchJSON('data/solar-news/search/fixture.json', hash);
  await assert.rejects(() => context.SolarNews.fetchJSON('data/wind-news/search/fixture.json', hash));
  assert.deepEqual(requested, ['https://example.kr/site/data/solar-news/search/fixture.json']);
});
