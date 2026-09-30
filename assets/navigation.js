(function (root) {
  'use strict';
  function validDate(value) {
    return /^\d{4}-\d{2}-\d{2}$/.test(value || '') &&
      Number.isFinite(Date.parse(value + 'T00:00:00Z')) &&
      new Date(value + 'T00:00:00Z').toISOString().slice(0, 10) === value;
  }
  function parseRoute(hash) {
    const [path, query = ''] = (hash || '#/notices').replace(/^#/, '').split('?');
    if (path === '/daily/archive') return { page: 'daily', tab: 'archive', params: new URLSearchParams(query) };
    if (path === '/daily/dates') return { page: 'daily', tab: 'dates' };
    if (path === '/daily') return { page: 'daily', tab: 'today' };
    const match = /^\/daily\/(\d{4}-\d{2}-\d{2})$/.exec(path);
    if (match && validDate(match[1])) return { page: 'daily', tab: 'dates', date: match[1], params: new URLSearchParams(query) };
    return { page: 'notices' };
  }
  const api = { parseRoute, validDate };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  root.WindNavigation = api;
  if (!root.document) return;
  const toggle = document.getElementById('menuToggle');
  toggle.addEventListener('click', () => {
    const open = toggle.getAttribute('aria-expanded') !== 'true';
    toggle.setAttribute('aria-expanded', String(open));
    document.getElementById('siteMenu').classList.toggle('is-open', open);
  });
  function activate(force) {
    const route = parseRoute(location.hash);
    document.body.dataset.route = route.page;
    document.getElementById('noticesPage').hidden = route.page !== 'notices';
    document.getElementById('dailyPage').hidden = route.page !== 'daily';
    document.querySelectorAll('[data-page]').forEach(link => {
      if (link.dataset.page === route.page) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    document.querySelectorAll('[data-daily-tab]').forEach(link => {
      if (link.dataset.dailyTab === route.tab) link.setAttribute('aria-current', 'page');
      else link.removeAttribute('aria-current');
    });
    toggle.setAttribute('aria-expanded', 'false');
    document.getElementById('siteMenu').classList.remove('is-open');
    document.title = route.page === 'daily' ? '풍력 일간 시황 | BRE Workflow' : '공지 모니터링 | BRE Workflow';
    root.WindNews.activate(route, !!force);
  }
  root.addEventListener('hashchange', () => activate(false));
  document.getElementById('dailyRefresh').addEventListener('click', () => activate(true));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) activate(false); });
  root.addEventListener('focus', () => activate(false));
  activate(false);
})(typeof globalThis !== 'undefined' ? globalThis : this);
