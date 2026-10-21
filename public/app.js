'use strict';
const $ = id => document.getElementById(id);
let DB_KEY = 'pure-novel-v1';
let account = null, cloudMode = false, cloudVersion = 0, dirty = false, syncTimer = null, syncing = false, syncBlocked = false;
let cacheName = 'pure-novel-chapters';
const DEFAULTS = { size: 21, line: '1.95', font: 'serif', theme: 'paper' };
let state = { books: [], settings: { ...DEFAULTS } };
try {
  const saved = JSON.parse(localStorage.getItem(DB_KEY) || 'null');
  if (saved && Array.isArray(saved.books)) state = { books: saved.books, settings: { ...DEFAULTS, ...saved.settings } };
} catch { /* A corrupted preference must not prevent reading. */ }
let current = null, book = null, pending = false, catalogNext = null, catalogUrl = null;
let requestSequence = 0, saving = null, toastTimer = null, cacheDB = null;
let searchBusy = false, sourceChecks = 0;
let searchSession = null;
let restoringProgress = false;
let prefetchTimer = null;
const pageRequests = new Map();
let preparedEbook = null, importSequence = 0;

function cancelEbookImport() {
  if (pending) return;
  preparedEbook = null; ++importSequence;
  $('ebook-confirm').close();
  $('ebook-status').textContent = '已取消，未保存本次文件。';
}
$('ebook-cancel').addEventListener('click', cancelEbookImport);
$('ebook-confirm').addEventListener('cancel', event => { event.preventDefault(); cancelEbookImport(); });
$('ebook-file').addEventListener('change', async event => {
  const file = event.target.files[0];
  event.target.value = '';
  if (!file || pending) return;
  const scope = DB_KEY, seq = ++importSequence;
  preparedEbook = null;
  busy(true);
  try {
    const data = await Ebooks.parse(file, message => { if (seq === importSequence && scope === DB_KEY) $('ebook-status').textContent = message; });
    if (seq !== importSequence || scope !== DB_KEY) return;
    const existing = state.books.find(b => b.catalog === `ebook:${data.id}`);
    preparedEbook = { data, scope, seq };
    $('ebook-title').value = existing?.title || data.title;
    $('ebook-author').value = existing?.author || data.author;
    $('ebook-title').disabled = $('ebook-author').disabled = Boolean(existing);
    $('ebook-info').textContent = `${data.filename} · ${data.format}${data.encoding ? ` / ${data.encoding}` : ''} · ${(data.size / 1048576).toFixed(2)} MiB · ${data.sections.length} 节`;
    $('ebook-warning').textContent = data.warnings.join(' ');
    $('ebook-preview').textContent = data.sections[0].paragraphs.join('\n').slice(0, 1800);
    $('ebook-match').textContent = existing ? '同一文件已在书架；确认后补全本机正文并继续原进度，不重复入架。' :
      (state.books.some(b => b.title === data.title) ? '书架已有同名但不同文件，将作为独立版本保存，不覆盖原书。' : '请核对书名、作者与正文。');
    $('ebook-accept').textContent = existing ? '继续阅读' : '导入并阅读';
    $('ebook-confirm-error').textContent = '';
    $('ebook-status').textContent = '解析完成，等待确认，尚未保存。';
    $('ebook-confirm').showModal();
  } catch (error) {
    if (seq === importSequence && scope === DB_KEY) $('ebook-status').textContent = error.message;
  } finally { if (seq === importSequence) busy(false); }
});
$('ebook-confirm-form').addEventListener('submit', async event => {
  event.preventDefault();
  const prepared = preparedEbook;
  if (!prepared || pending || prepared.scope !== DB_KEY) return;
  const title = $('ebook-title').value.trim(), author = $('ebook-author').value.trim();
  if (!title) { $('ebook-confirm-error').textContent = '请输入书名。'; return; }
  const database = cacheDB, scope = DB_KEY;
  const catalog = `ebook:${prepared.data.id}`;
  const existing = state.books.find(b => b.catalog === catalog);
  const data = { ...prepared.data, title: existing?.title || title, author: existing?.author || author };
  busy(true); $('ebook-confirm-error').textContent = ''; $('ebook-status').textContent = '正在保存至当前浏览器…';
  let previous, wrote = false;
  try {
    previous = await ebookStore('readonly', store => store.get(data.id), database);
    if (scope !== DB_KEY || prepared.seq !== importSequence) throw new Error('账号已变化，已停止导入。');
    await ebookStore('readwrite', store => store.put(data), database); wrote = true;
    if (scope !== DB_KEY || prepared.seq !== importSequence) throw new Error('账号已变化，已停止导入。');
    const record = existing || { id: data.id, title: data.title, author: data.author, catalog, lastUrl: `${catalog}/0`, progress: 0, chapters: [] };
    const nextState = existing ? state : { ...state, books: [record, ...state.books] };
    // Only publish the new shelf after both the file transaction and metadata write succeed.
    localStorage.setItem(DB_KEY, JSON.stringify(nextState));
    state = nextState; save(); renderShelf();
    preparedEbook = null; $('ebook-confirm').close();
    $('ebook-status').textContent = `已保存《${data.title}》，共 ${data.sections.length} 节，仅本机可用。`;
    busy(false);
    await load(record.lastUrl || catalog, record, true);
  } catch (error) {
    if (wrote) {
      try { await ebookStore('readwrite', store => previous ? store.put(previous) : store.delete(data.id), database); }
      catch { /* Best effort cleanup; no shelf entry was published. */ }
    }
    if (scope === DB_KEY) {
      $('ebook-confirm-error').textContent = `未完成导入：${error.message} 请检查存储空间或浏览器存储权限后重试。`;
      $('ebook-status').textContent = '导入未完成，未新增书架记录。';
    }
  } finally { if (scope === DB_KEY) busy(false); }
});

function siteKey(host) { return host.replace(/^(www\.|m\.)/, ''); }
function searchSummary() {
  if (!searchSession) return;
  const s = searchSession;
  const checked = s.cards.filter(c => c.checked()).length;
  const ready = new Set(s.cards.filter(c => c.isReady()).map(c => siteKey(c.entry.host))).size;
  $('search-status').textContent = `找到 ${s.cards.length} 个候选页面，来自 ${new Set(s.cards.map(c => siteKey(c.entry.host))).size} 个网站；已检查 ${checked} 个页面，${ready} 个网站正文样本可读。${ready ? '默认仅展示通过检查的来源，不保证全书免费或完整。' : (searchBusy || sourceChecks ? '正在筛选可读来源…' : '本次未找到通过检查的来源，可展开详情或继续寻找。')} ${s.warnings.join('；')}`;
  $('search-more').hidden = s.round >= 2 || s.cards.length >= 20;
  $('search-more').disabled = searchBusy || Boolean(sourceChecks);
}

async function searchAPI(path, body) {
  let response;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 55000);
  try {
    response = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), signal: controller.signal });
    if ([502, 503, 504].includes(response.status)) throw new Error(`搜索服务暂时不可用或请求超时（HTTP ${response.status}），请稍后重试，不代表没有这本书。`);
    let data;
    try { data = await response.json(); }
    catch { throw new Error(`搜索接口返回非 JSON 响应（HTTP ${response.status}），请稍后刷新重试。线上网站不需要在本机启动 Python。`); }
    if (!response.ok) throw new Error(data.error || '搜索接口请求失败。');
    return data;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('搜索或来源检查超时，请稍后重试或更换来源；不代表没有这本书。');
    if (error instanceof TypeError) throw new Error('无法连接阅读器后端，请检查网络后重试。线上网站无需在电脑上启动 Python。');
    throw error;
  } finally { clearTimeout(timer); }
}

function sourceCard(entry, query) {
  const card = element('div', undefined, 'source-card');
  // Unverified and failed candidates live only in the collapsed diagnostic list.
  const status = element('p', '未检查：搜索结果不代表可以读取。', 'source-status');
  status.dataset.state = 'pending';
  const actions = element('div', undefined, 'source-actions');
  const button = element('button', '检查此来源');
  const original = element('a', '原网页 ↗');
  original.href = entry.url; original.target = '_blank'; original.rel = 'noopener noreferrer';
  const session = searchSession;
  let verified = null, checking = false;
  const check = async () => {
    if (checking || verified || sourceChecks >= 2) return;
    checking = true; sourceChecks++; button.disabled = true;
    status.textContent = '正在检查书名、目录及正文样本…'; status.dataset.state = 'checking';
    try {
      const result = await searchAPI('/api/check', { query, url: entry.url });
      if (session !== searchSession) return;
      verified = result;
      status.textContent = `《${verified.bookTitle}》 · ${verified.author || '作者未识别，请自行核对'} · 正文样本可读：${verified.sampleTitle} · ${verified.characters} 字。${verified.note}`;
      status.dataset.state = 'ready'; button.textContent = '在纯阅阅读';
      $('search-results').append(card);
    } catch (err) {
      status.textContent = err.message; status.dataset.state = 'failed'; button.textContent = '重新检查';
    } finally { checking = false; sourceChecks--; button.disabled = false; attempted = true; searchSummary(); }
  };
  button.addEventListener('click', () => {
    if (verified) load(verified.url, null, true);
    else if (sourceChecks >= 2) toast('正在检查其他来源，请稍后再试。');
    else check();
  });
  actions.append(button, original);
  card.append(element('h3', entry.title), element('div', `${entry.host} · ${entry.engines.join(' / ')}`, 'source-meta'), status, actions);
  let attempted = false;
  return { card, entry, check, checked: () => attempted, isReady: () => Boolean(verified), result: () => verified };
}

async function searchBooks(event, more = false, automatic = false) {
  event?.preventDefault();
  if (searchBusy || sourceChecks) return toast('请等待当前搜索或来源检查完成。');
  const query = more ? searchSession?.query : $('book-query').value.trim();
  if (!query) return;
  if (!more) {
    searchSession = { query, round: -1, cards: [], warnings: [], autoChecked: new Set() };
    $('search-results').replaceChildren();
    let details = $('search-diagnostics');
    if (!details) {
      details = element('details'); details.id = 'search-diagnostics';
      details.append(element('summary', '查看检查详情（未通过及未检查的来源）'));
      const list = element('div'); list.id = 'search-diagnostic-list'; details.append(list);
      $('search-results').after(details);
    }
    details.open = false; $('search-diagnostic-list').replaceChildren();
  }
  const s = searchSession;
  let checksRemaining = 8;
  searchBusy = true; $('search-button').disabled = true;
  $('search-more').disabled = true; $('search-error').textContent = '';
  try {
    // Automatically use one supplemental round. A third round requires an explicit click.
    const lastRound = more ? Math.min(2, s.round + 1) : 1;
    while (s.round < lastRound && s.cards.length < 20) {
      const round = s.round + 1;
      $('search-status').textContent = `正在进行第 ${round + 1} 轮多引擎搜索…`;
      const data = await searchAPI('/api/search', { query, round });
      if (s !== searchSession) return;
      s.round = round;
      s.warnings = (data.warnings || []).map(w => `${w.engine}：${w.message}`);
      const seen = new Set(s.cards.map(c => c.entry.url));
      // Sort fresh sites first; preserve additional pages as manual alternatives.
      const knownSites = new Set(s.cards.map(c => siteKey(c.entry.host)));
      const fresh = data.results.filter(e => !seen.has(e.url)).sort((a, b) => Number(knownSites.has(siteKey(a.host))) - Number(knownSites.has(siteKey(b.host))));
      for (const entry of fresh) {
        if (s.cards.length >= 20 || seen.has(entry.url)) continue;
        seen.add(entry.url);
        const card = sourceCard(entry, data.query); s.cards.push(card); $('search-diagnostic-list').append(card.card);
      }
      const readySites = () => new Set(s.cards.filter(c => c.isReady()).map(c => siteKey(c.entry.host))).size;
      // Two checks at most; do not let one slow site serialize all candidates.
      while (readySites() < 2 && checksRemaining > 0) {
        const batch = [];
        for (const card of s.cards) {
          const host = siteKey(card.entry.host);
          if (card.checked() || s.autoChecked.has(host)) continue;
          s.autoChecked.add(host); batch.push(card); checksRemaining--;
          if (batch.length === 2 || checksRemaining === 0) break;
        }
        if (!batch.length) break;
        await Promise.all(batch.map(card => card.check()));
      }
      searchSummary();
      if (!more && (readySites() >= 2 || checksRemaining === 0 || data.hasMore === false)) break;
    }
  } catch (err) { $('search-error').textContent = err.message; }
  finally { searchBusy = false; $('search-button').disabled = false; searchSummary(); }
  if (automatic) {
    if ($('search-error').textContent) {
      $('entry-status').textContent = '本次搜索请求未完成，请查看错误提示后重试；不代表没有这本书。';
      return;
    }
    const ready = s.cards.filter(c => c.isReady()).map(c => c.result());
    $('entry-status').textContent = ready.length ? '请选择下方通过样本验证的来源。同名作品请核对作者和章节。' : '本次未找到通过检查的来源，可查看详情或继续寻找。';
  }
}

let resolving = false;
async function startReading(event) {
  event?.preventDefault();
  if (resolving || pending || searchBusy || sourceChecks) return toast('正在处理，请稍候。');
  const input = $('url').value.trim();
  if (!input) return;
  resolving = true; $('import-button').disabled = true;
  $('import-error').textContent = ''; $('entry-status').textContent = '正在识别输入…';
  try {
    if (/^https?:\/\//i.test(input)) {
      $('entry-status').textContent = '正在识别网页并整理目录或正文…';
      await load(input, null, true);
    } else if (/^[a-z][a-z\d+.-]*:\/\//i.test(input)) {
      throw new Error('仅支持 HTTP / HTTPS 小说网址。');
    } else {
      if (input.length < 2 || input.length > 60) throw new Error('书名请输入 2～60 个字符；网址请包含 https://。');
      const matches = state.books.filter(b => b.title === input);
      if (matches.length === 1 && matches[0].lastUrl) {
        $('entry-status').textContent = '正在恢复书架进度…';
        await load(matches[0].lastUrl, matches[0], true);
      } else {
        $('discovery').hidden = false; $('search-form').hidden = true;
        $('book-query').value = input;
        await searchBooks(null, false, true);
      }
    }
    if (!$('reader').hidden) $('entry-status').textContent = '';
  } catch (err) { $('import-error').textContent = err.message; }
  finally { resolving = false; $('import-button').disabled = false; }
}

function toast(text) {
  $('toast').textContent = text; $('toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4200);
}
function save() {
  try {
    localStorage.setItem(DB_KEY, JSON.stringify(state));
    if (account) localStorage.setItem(`${DB_KEY}:dirty`, '1');
  }
  catch { toast('本机存储不可用，阅读进度可能无法保存。'); }
  if (account) {
    dirty = true;
    clearTimeout(syncTimer); syncTimer = setTimeout(pushState, 2500);
  }
}
function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function openDB() {
  return new Promise(resolve => {
    if (!window.indexedDB) return resolve(null);
    const request = indexedDB.open(cacheName, 2);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains('pages')) request.result.createObjectStore('pages', { keyPath: 'url' });
      if (!request.result.objectStoreNames.contains('ebooks')) request.result.createObjectStore('ebooks', { keyPath: 'id' });
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
  });
}
async function cached(url, database = cacheDB) {
  const db = await database;
  if (!db) return null;
  return new Promise(resolve => {
    let req;
    try { req = db.transaction('pages').objectStore('pages').get(url); }
    catch { resolve(null); return; }
    req.onsuccess = () => resolve(req.result?.data || null); req.onerror = () => resolve(null);
  });
}
async function cachePage(url, data, database = cacheDB) {
  if (data.kind !== 'chapter' || data.warnings.length) return;
  const db = await database;
  if (!db) return;
  try {
    await new Promise(resolve => {
      const tx = db.transaction('pages', 'readwrite'), store = tx.objectStore('pages');
      tx.oncomplete = tx.onerror = tx.onabort = () => resolve();
      store.put({ url, data, time: Date.now() });
      const req = store.getAll();
      req.onsuccess = () => req.result.sort((a, b) => b.time - a.time).slice(20).forEach(row => store.delete(row.url));
    });
  } catch { /* Storage eviction or private mode: online reading still works. */ }
}
async function ebookStore(mode, operation, database = cacheDB) {
  const db = await database;
  if (!db) throw new Error('本机存储不可用，无法保存电子书。');
  return new Promise((resolve, reject) => {
    const tx = db.transaction('ebooks', mode), request = operation(tx.objectStore('ebooks'));
    tx.oncomplete = () => resolve(request.result);
    tx.onerror = tx.onabort = () => reject(new Error('电子书存储失败，可能空间不足。'));
  });
}
async function localEbookPage(url) {
  const match = /^ebook:([a-f0-9]{64})(?:\/(\d+))?$/.exec(url);
  if (!match) throw new Error('本机电子书地址无效。');
  const data = await ebookStore('readonly', store => store.get(match[1]));
  if (!data) throw new Error('此设备没有该电子书正文，请回书架重新导入同一 EPUB / TXT 文件；云端只同步书架和进度。');
  const base = `ebook:${data.id}`;
  const chapters = data.sections.map((s, i) => ({ title: s.title, url: `${base}/${i}` }));
  const result = { url, bookTitle: data.title, author: data.author, catalog: base, chapters,
    paragraphs: [], previous: null, next: null, nextPage: null, catalogNext: null, pages: 1, warnings: [] };
  if (match[2] === undefined) return { ...result, kind: 'catalog', title: data.title };
  const index = Number(match[2]), section = data.sections[index];
  if (!section) throw new Error('此电子书章节不存在。');
  return { ...result, kind: 'chapter', title: section.title, paragraphs: section.paragraphs,
    previous: chapters[index - 1]?.url || null, next: chapters[index + 1]?.url || null };
}
async function getPage(url, refresh = false) {
  if (url.startsWith('ebook:')) return localEbookPage(url);
  const scope = DB_KEY, database = cacheDB;
  const key = `${scope}:${url}:${refresh}`;
  if (pageRequests.has(key)) return pageRequests.get(key);
  const request = readPage(url, refresh, scope, database);
  pageRequests.set(key, request);
  try { return await request; }
  finally { if (pageRequests.get(key) === request) pageRequests.delete(key); }
}
async function readPage(url, refresh, scope, database) {
  const stored = await cached(url, database);
  if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
  if (!refresh && stored?.kind === 'chapter' && !stored.warnings?.length) {
    return { ...stored, fromCache: true, offline: !navigator.onLine };
  }
  if (!navigator.onLine) {
    if (stored) return { ...stored, offline: true };
  }
  let response;
  try {
    response = await fetch('/api/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
  } catch {
    if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
    if (stored) return { ...stored, offline: true };
    if (!navigator.onLine) throw new Error('浏览器当前处于离线状态，且该章节尚未缓存。连接网络后请重试。');
    throw new Error('无法连接阅读器后端，且该章节尚未缓存。请检查网络后重试；线上网站无需在电脑上启动 Python，此错误不代表小说来源不可用。');
  }
  let data;
  try { data = await response.json(); }
  catch {
    throw new Error(`阅读器接口返回了非 JSON 响应（HTTP ${response.status}）。服务可能暂时不可用或请求超时，请稍后刷新重试。`);
  }
  if (!data || typeof data !== 'object') throw new Error('阅读器接口响应格式异常，请稍后刷新重试。');
  if (!response.ok) throw new Error(data.error || '读取失败。');
  if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
  await cachePage(url, data, database);
  if (url !== data.url) await cachePage(data.url, data, database);
  return data;
}
function schedulePrefetch() {
  clearTimeout(prefetchTimer);
  const chapter = current, scope = DB_KEY;
  if (!chapter?.next || chapter.warnings.length || chapter.restrictedNext || chapter.url.startsWith('ebook:')) return;
  let next;
  try {
    next = new URL(chapter.next);
    if (!['http:', 'https:'].includes(next.protocol) || next.origin !== new URL(chapter.url).origin || next.href === chapter.url) return;
  } catch { return; }
  prefetchTimer = setTimeout(() => {
    if (scope !== DB_KEY || current !== chapter || document.hidden || !navigator.onLine || navigator.connection?.saveData) return;
    // One chapter only. This never renders, changes progress, or schedules another prefetch.
    getPage(next.href).catch(() => {});
  }, 600);
}
function renderShelf() {
  $('shelf').replaceChildren(); $('book-count').textContent = state.books.length;
  if (!state.books.length) {
    const empty = element('div', undefined, 'empty-shelf');
    empty.append(element('strong', '书架的第一本，等你来选。'), element('span', '粘贴目录或章节网址，开始你的安静时光。'));
    $('shelf').append(empty); return;
  }
  for (const item of state.books) {
    const card = element('div', undefined, 'book-card');
    const open = element('button', undefined, 'book-open');
    open.append(element('span', 'PRIVATE COLLECTION', 'eyebrow'), element('h3', item.title), element('p', item.lastTitle || '打开目录，开始阅读'));
    open.addEventListener('click', () => load(item.lastUrl || item.catalog, item));
    const actions = element('div', undefined, 'book-actions');
    const remove = element('button', '移除', 'text-button');
    remove.setAttribute('aria-label', `移除 ${item.title}`);
    remove.addEventListener('click', async () => {
      if (!confirm(`从书架移除《${item.title}》？`)) return;
      if (item.catalog?.startsWith('ebook:')) {
        try { await ebookStore('readwrite', s => s.delete(item.catalog.slice(6))); }
        catch (err) { toast(err.message); return; }
      }
      state.books = state.books.filter(x => x.id !== item.id); save(); renderShelf();
    });
    actions.append(element('span', `${Math.round((item.progress || 0) * 100)}% 本章`), remove);
    card.append(open, actions); $('shelf').append(card);
  }
}
function applySettings() {
  const s = state.settings;
  if (!['paper', 'green', 'night'].includes(s.theme)) s.theme = 'paper';
  s.size = Math.max(16, Math.min(30, Number(s.size) || 21));
  if (!['1.7', '1.95', '2.2'].includes(s.line)) s.line = '1.95';
  if (!['serif', 'sans-serif'].includes(s.font)) s.font = 'serif';
  document.body.dataset.theme = s.theme;
  document.documentElement.style.setProperty('--font-size', `${s.size}px`);
  document.documentElement.style.setProperty('--line-height', s.line);
  document.documentElement.style.setProperty('--reading-font', s.font);
  $('font-size').value = s.size; $('font-output').textContent = `${s.size}px`;
  $('line-height').value = s.line; $('font-family').value = s.font;
  document.querySelectorAll('[data-theme]').forEach(n => { if (n.tagName === 'BUTTON') n.setAttribute('aria-pressed', String(n.dataset.theme === s.theme)); });
}
function busy(value) {
  pending = value; $('busy').hidden = !value; $('import-button').disabled = value;
  $('ebook-file').disabled = value;
  $('ebook-accept').disabled = $('ebook-cancel').disabled = value;
}
function makeBook(data, existing) {
  const catalog = data.kind === 'catalog' ? data.url : data.catalog;
  const known = existing || state.books.find(b => (catalog && b.catalog === catalog) || b.lastUrl === data.url);
  if (known) { if (catalog) known.catalog = catalog; return known; }
  const fresh = { id: crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`, title: data.bookTitle,
    catalog, lastUrl: null, progress: 0, chapters: [] };
  state.books.unshift(fresh); return fresh;
}
async function load(url, existing = null, automatic = false, refresh = false) {
  if (pending || !url) return;
  saveProgress();
  clearTimeout(prefetchTimer);
  const seq = ++requestSequence;
  const scope = DB_KEY;
  busy(true); $('import-error').textContent = '';
  try {
    let data = await getPage(url, refresh);
    if (seq !== requestSequence || scope !== DB_KEY) return;
    book = makeBook(data, existing);
    if (data.kind === 'catalog') {
      book.chapters = data.chapters; book.catalog = data.url;
      catalogNext = data.catalogNext; catalogUrl = data.url;
      save(); renderShelf(); renderCatalog();
      if (!automatic) {
        $('catalog-dialog').showModal();
        return;
      }
      const target = book.lastUrl || data.chapters.find(c => !c.locked && !/新书|感言|公告|请假/.test(c.title))?.url;
      if (!target) throw new Error('目录中没有已识别的公开章节，请前往原站查看。');
      $('entry-status').textContent = '目录已整理，正在打开章节…';
      data = await getPage(target);
      if (seq !== requestSequence || scope !== DB_KEY) return;
      if (data.kind !== 'chapter') throw new Error('阅读入口未返回章节正文，请在目录中选择。');
    }
    if (data.kind !== 'chapter') throw new Error('未取得章节正文，请检查输入或选择其他来源。');
    const restore = book.lastUrl === data.url ? book.progress || 0 : 0;
    book.lastUrl = data.url; book.lastTitle = data.title; book.progress = restore;
    restoringProgress = true;
    current = data; save(); renderChapter();
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (seq !== requestSequence || scope !== DB_KEY || current !== data || $('reader').hidden) return;
      window.scrollTo(0, restore * Math.max(0, document.documentElement.scrollHeight - innerHeight));
      restoringProgress = false;
      updateProgress();
      schedulePrefetch();
    }));
  } catch (err) {
    $('entry-status').textContent = '未能打开阅读，可查看错误原因或尝试其他来源。';
    if (!$('home').hidden) $('import-error').textContent = err.message;
    toast(err.message);
  } finally { if (seq === requestSequence) busy(false); }
}
function renderChapter() {
  $('home').hidden = true; $('reader').hidden = false;
  $('reader-book').textContent = book.title; $('chapter-title').textContent = current.title;
  $('chapter-caption').textContent = current.url.startsWith('ebook:') ? '本机电子书 · 不上传正文' : `${new URL(current.url).hostname} · ${current.pages} 页合并`;
  $('content').replaceChildren(...current.paragraphs.map(p => element('p', p)));
  const warning = $('reading-warning'); warning.replaceChildren();
  if (current.warnings.length) warning.append(element('span', current.warnings.join(' ')));
  if (current.accessNote) warning.append(element('p', current.accessNote));
  if (current.restrictedNext) {
    const vip = element('a', '前往原站阅读下一章（VIP） ↗');
    vip.href = current.restrictedNext; vip.target = '_blank'; vip.rel = 'noopener noreferrer'; warning.append(vip);
  }
  const source = element('a', '打开原网页 ↗'); source.href = current.url; source.target = '_blank'; source.rel = 'noopener noreferrer';
  if (!current.url.startsWith('ebook:')) warning.append(source);
  warning.hidden = !warning.childNodes.length;
  $('previous').disabled = !current.previous;
  $('next').disabled = $('next-bottom').disabled = !current.next;
  $('cache-label').textContent = current.url.startsWith('ebook:') ? '本机电子书 · 正文不上传' : (current.offline ? '本机离线缓存' : (current.fromCache ? '本机缓存 · 可重新获取本章' : (current.warnings.length ? '内容可能不完整 · 未缓存' : '最近完整章节自动缓存')));
  $('refresh-chapter').hidden = current.url.startsWith('ebook:');
  document.title = `${current.title} · 纯阅`;
}
function updateProgress() {
  if (!current || restoringProgress || $('reader').hidden) return;
  const max = Math.max(0, document.documentElement.scrollHeight - innerHeight);
  const ratio = max ? Math.min(1, Math.max(0, scrollY / max)) : 1;
  $('progress-label').textContent = `${Math.round(ratio * 100)}% 本章`;
  const chapter = current, owner = book, scope = DB_KEY;
  clearTimeout(saving);
  saving = setTimeout(() => {
    if (current === chapter && book === owner && DB_KEY === scope) saveProgress();
  }, 300);
}
function saveProgress() {
  clearTimeout(saving);
  if (!current || !book || restoringProgress || $('reader').hidden || book.lastUrl !== current.url) return;
  const max = Math.max(0, document.documentElement.scrollHeight - innerHeight);
  book.progress = max ? Math.min(1, Math.max(0, scrollY / max)) : 1; save();
}
function mergeChapters(items) {
  const seen = new Set(book.chapters.map(c => c.url));
  for (const c of items) if (!seen.has(c.url)) { seen.add(c.url); book.chapters.push(c); }
}
function renderCatalog() {
  $('chapters').replaceChildren();
  for (const chapter of book.chapters || []) {
    const button = element('button', `${chapter.title}${chapter.locked ? ' · VIP（原站订阅）' : ''}`, chapter.url === current?.url ? 'current' : '');
    button.addEventListener('click', () => {
      if (pending) return;
      if (chapter.locked) {
        toast('该章节为 VIP，请前往原站按要求订阅。');
        return;
      }
      $('catalog-dialog').close(); load(chapter.url, book);
    });
    $('chapters').append(button);
  }
  $('catalog-status').textContent = `${book.title} · 已载入 ${book.chapters.length} 个章节链接${catalogNext ? '（目录还有下一页）' : ''}`;
  const locked = book.chapters.filter(c => c.locked).length;
  if (locked) $('catalog-status').textContent += ` · ${locked} 章 VIP，仅支持原站订阅；普通章节未逐章验证`;
  const oldLink = $('catalog-dialog').querySelector('.catalog-original');
  if (oldLink) oldLink.remove();
  if (locked && book.catalog) {
    const original = element('a', '前往原站目录 / 订阅 ↗', 'catalog-original');
    original.href = book.catalog; original.target = '_blank'; original.rel = 'noopener noreferrer';
    $('catalog-status').after(original);
  }
  $('catalog-more').hidden = !catalogNext;
}
async function showCatalog(more = false) {
  if (!book || pending) return;
  const url = more ? catalogNext : book.catalog;
  if (!url) return toast('此页未识别到目录，请从小说目录页重新导入。');
  if (!$('catalog-dialog').open) $('catalog-dialog').showModal();
  if (!more) { renderCatalog(); $('catalog-status').textContent = '正在刷新目录…'; }
  busy(true);
  try {
    const data = await getPage(url);
    if (data.kind !== 'catalog') throw new Error('该链接未返回目录。');
    if (!more) book.chapters = [];
    mergeChapters(data.chapters);
    catalogNext = data.catalogNext === url || data.catalogNext === catalogUrl ? null : data.catalogNext;
    catalogUrl = data.url; save(); renderCatalog();
  } catch (err) { renderCatalog(); toast(`${err.message}${book.chapters.length ? ' 已显示本机保存的目录。' : ''}`); }
  finally { busy(false); }
}
function toggleFocus() {
  const on = document.body.classList.toggle('focus');
  $('exit-focus').hidden = !on;
}
$('import-form').addEventListener('submit', startReading);
$('search-form').addEventListener('submit', searchBooks);
$('search-more').addEventListener('click', event => searchBooks(event, true));
$('previous').addEventListener('click', () => load(current?.previous, book));
$('refresh-chapter').addEventListener('click', () => load(current?.url, book, false, true));
for (const id of ['next', 'next-bottom']) $(id).addEventListener('click', () => load(current?.next, book));
$('back').addEventListener('click', () => {
  if (pending) return;
  saveProgress(); $('reader').hidden = true; $('home').hidden = false;
  clearTimeout(prefetchTimer); ++requestSequence; restoringProgress = false;
  document.body.classList.remove('focus'); $('exit-focus').hidden = true;
  current = null; document.title = '纯阅 · 把世界留在书外'; renderShelf(); scrollTo(0, 0);
});
$('catalog-button').addEventListener('click', () => showCatalog());
$('catalog-more').addEventListener('click', () => showCatalog(true));
$('settings-button').addEventListener('click', () => $('settings-dialog').showModal());
$('help-button').addEventListener('click', () => $('help-dialog').showModal());
document.querySelectorAll('.close-dialog').forEach(button => button.addEventListener('click', () => button.closest('dialog').close()));
for (const id of ['focus-button', 'exit-focus']) $(id).addEventListener('click', toggleFocus);
$('content').addEventListener('click', () => { if (!getSelection()?.toString()) toggleFocus(); });
for (const [id, key] of [['font-size', 'size'], ['line-height', 'line'], ['font-family', 'font']]) {
  $(id).addEventListener('input', event => { state.settings[key] = event.target.value; applySettings(); save(); });
}
document.querySelectorAll('button[data-theme]').forEach(button => button.addEventListener('click', () => { state.settings.theme = button.dataset.theme; applySettings(); save(); }));
$('clear-cache').addEventListener('click', async () => {
  const db = await cacheDB;
  if (!db) return toast('章节缓存不可用。');
  const tx = db.transaction('pages', 'readwrite'); tx.objectStore('pages').clear();
  tx.oncomplete = () => toast('本机章节缓存已清除，书架与进度仍保留。');
});
addEventListener('scroll', updateProgress, { passive: true });
addEventListener('pagehide', saveProgress);
document.addEventListener('visibilitychange', () => { if (document.hidden) saveProgress(); });
async function accountAPI(path, body) {
  const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const data = await response.json();
  if (!response.ok) { const error = new Error(data.error || '账号服务异常。'); error.status = response.status; throw error; }
  return data;
}
function syncPayload() {
  // Catalogs can contain thousands of links; sync only personal metadata.
  return { books: state.books.map(({ chapters, ...metadata }) => metadata), settings: state.settings };
}
function usePayload(payload) {
  const localCatalogs = new Map(state.books.map(b => [b.id, b.chapters || []]));
  state = { books: (payload.books || []).map(b => ({ ...b, chapters: localCatalogs.get(b.id) || [] })), settings: { ...DEFAULTS, ...payload.settings } };
  localStorage.setItem(DB_KEY, JSON.stringify(state)); applySettings(); renderShelf();
}
async function pushState() {
  if (!account || !dirty || syncing || syncBlocked) return;
  syncing = true; const payload = syncPayload(); const snapshot = JSON.stringify(payload);
  try {
    const result = await accountAPI('/api/state', { version: cloudVersion, payload });
    cloudVersion = result.version;
    localStorage.setItem(`${DB_KEY}:version`, String(cloudVersion));
    if (snapshot === JSON.stringify(syncPayload())) {
      dirty = false; localStorage.removeItem(`${DB_KEY}:dirty`);
    }
    $('account-status').textContent = '已同步';
  } catch (error) {
    $('account-status').textContent = error.status === 409 ? '同步冲突，请选择版本' : '未同步，已保存在本机';
    if (error.status === 409) { syncBlocked = true; $('conflict-dialog').showModal(); }
    if (error.status === 401 || error.status === 403) { syncBlocked = true; toast('会话失效或账号停用，请退出后重新登录。'); }
  } finally {
    syncing = false;
    if (dirty && !syncBlocked) { clearTimeout(syncTimer); syncTimer = setTimeout(pushState, 15000); }
  }
}
async function startAccount(user) {
  ++importSequence; preparedEbook = null; $('ebook-confirm').close(); $('ebook-status').textContent = '';
  saveProgress();
  ++requestSequence; clearTimeout(saving); clearTimeout(prefetchTimer); clearTimeout(syncTimer);
  current = null; book = null; restoringProgress = false; busy(false);
  $('reader').hidden = true;
  account = user; DB_KEY = `pure-novel-user:${user.id}`; cacheName = `pure-novel-chapters:${user.id}`;
  state = { books: [], settings: { ...DEFAULTS } };
  try { const local = JSON.parse(localStorage.getItem(DB_KEY)); if (local) state = local; } catch {}
  dirty = localStorage.getItem(`${DB_KEY}:dirty`) === '1';
  cloudVersion = Number(localStorage.getItem(`${DB_KEY}:version`) || 0);
  const remote = await accountAPI('/api/state');
  if (dirty && remote.version !== cloudVersion) {
    syncBlocked = true; $('conflict-dialog').showModal();
  } else if (!dirty) {
    cloudVersion = remote.version;
    if (remote.payload) usePayload(remote.payload);
  }
  localStorage.setItem(`${DB_KEY}:version`, String(cloudVersion));
  cacheDB = openDB(); applySettings(); renderShelf();
  $('account-name').textContent = user.username || '受邀成员'; $('account-bar').hidden = false;
  $('login-screen').hidden = true; $('home').hidden = false;
  $('account-status').textContent = dirty ? '本机有待同步变更' : '已同步';
  if (dirty) pushState();
  $('import-local').hidden = !localStorage.getItem('pure-novel-v1');
}
async function initialize() {
  try {
    const health = await accountAPI('/api/health'); cloudMode = health.authRequired;
    if (!cloudMode) { $('login-screen').hidden = true; cacheDB = openDB(); applySettings(); renderShelf(); $('home').hidden = false; }
    else {
      state = { books: [], settings: { ...DEFAULTS } };
      $('login-screen').hidden = false;
      try { const session = await accountAPI('/api/session'); await startAccount(session.user); }
      catch (error) { $('login-error').textContent = error.message; }
    }
  } catch {
    $('login-screen').hidden = false;
    $('login-screen').querySelector('h1').textContent = '阅读服务暂不可用';
    $('login-screen').querySelector('.intro > p:last-child').textContent = '无法确认服务状态，这不是要求你注册或填写账号密码。请先启动服务，再刷新页面。';
    $('login-form').hidden = true;
    const retry = element('button', '重新连接');
    retry.addEventListener('click', () => location.reload());
    $('login-screen').append(retry);
  } finally { document.documentElement.dataset.ready = 'true'; }
}
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('login-submit').disabled = true; $('login-error').textContent = '';
  try {
    const data = await accountAPI('/api/login', { username: $('username').value.trim(), password: $('password').value });
    $('password').value = ''; await startAccount(data.user);
  } catch (error) { $('login-error').textContent = error.message; }
  finally { $('password').value = ''; $('login-submit').disabled = false; }
});
$('logout').addEventListener('click', async () => {
  if (pending || searchBusy || sourceChecks || syncing) return toast('请等待当前请求完成后退出。');
  if (!confirm('退出并清除当前账号在此浏览器的书架和章节缓存？未同步的变更会丢失。')) return;
  try {
    await accountAPI('/api/logout', {}); clearTimeout(syncTimer); clearTimeout(saving); clearTimeout(prefetchTimer); ++requestSequence;
    current = null; book = null; account = null;
    localStorage.removeItem(DB_KEY); localStorage.removeItem(`${DB_KEY}:version`); localStorage.removeItem(`${DB_KEY}:dirty`);
    localStorage.removeItem(`${DB_KEY}:import-task`);
    const db = await cacheDB; if (db) db.close(); indexedDB.deleteDatabase(cacheName);
    location.reload();
  } catch (error) { toast(error.message); }
});
$('sync-now').addEventListener('click', async () => {
  if (dirty) return pushState();
  if (syncing) return;
  if (current) return toast('请回到书架再刷新云端数据。');
  syncing = true;
  try {
    const remote = await accountAPI('/api/state');
    if (dirty || current) return toast('本机刚有变更，请再次同步。');
    cloudVersion = remote.version; if (remote.payload) usePayload(remote.payload);
    localStorage.setItem(`${DB_KEY}:version`, String(cloudVersion)); toast('已刷新云端书架。');
  }
  catch (error) { toast(error.message); }
  finally { syncing = false; if (dirty) pushState(); }
});
for (const [id, keepLocal] of [['keep-local', true], ['keep-cloud', false]]) $(id).addEventListener('click', async () => {
  try {
    const remote = await accountAPI('/api/state'); cloudVersion = remote.version;
    if (!keepLocal) {
      if (current) $('back').click();
      usePayload(remote.payload || { books: [], settings: DEFAULTS }); dirty = false; localStorage.removeItem(`${DB_KEY}:dirty`);
    }
    localStorage.setItem(`${DB_KEY}:version`, String(cloudVersion)); syncBlocked = false; $('conflict-dialog').close();
    if (keepLocal) { dirty = true; await pushState(); }
  } catch (error) { toast(error.message); }
});
$('import-local').addEventListener('click', () => {
  if (!confirm('将旧版未登录书架合并到当前账号？请确认这是你自己的数据。')) return;
  try {
    const legacy = JSON.parse(localStorage.getItem('pure-novel-v1'));
    for (const item of legacy.books || []) if (!state.books.some(b => b.id === item.id)) state.books.push(item);
    save(); renderShelf(); localStorage.removeItem('pure-novel-v1'); $('import-local').hidden = true;
  } catch { toast('旧书架无法读取。'); }
});
addEventListener('online', pushState);
initialize();
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});