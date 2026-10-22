'use strict';
const $ = id => document.getElementById(id);
let DB_KEY = 'pure-novel-v1';
let account = null, cloudMode = false, cloudVersion = 0, dirty = false, syncTimer = null, syncing = false, syncBlocked = false;
let cacheName = 'pure-novel-chapters';
const DEFAULTS = { size: 21, line: '1.95', font: 'serif', theme: 'paper' };
const DAY_PALETTES = ['almond', 'mist', 'blue'];
const READING_THEMES = { paper: '纸白', almond: '暖杏', green: '豆沙绿', mist: '雾灰', blue: '浅蓝', night: '夜读' };
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
let localOffline = false;
const MODE_KEY = 'pure-novel:service-mode';
const REQUEST_TIMEOUT = 55000;
const OFFLINE_KEY = 'pure-novel:offline-account';
const LOGOUT_KEY = 'pure-novel:pending-logout';
const OFFLINE_LIFETIME = 7 * 24 * 60 * 60 * 1000;
const MAX_SYNC_RETRIES = 5;
let accountEpoch = 0, authSequence = 0, authChecking = false, reconnecting = null;
let metadataSnapshot = '', syncedSnapshot = null, syncRetryCount = 0, expiryTimer = null, lockingAccount = false;
const activeRequests = new Set();
let shelfConflict = null, shelfWriteSnapshot = null;
let tabId = crypto.randomUUID();
try { tabId = sessionStorage.getItem('pure-novel:tab-id') || tabId; sessionStorage.setItem('pure-novel:tab-id', tabId); } catch {}

async function fetchTimed(url, options = {}, timeout = REQUEST_TIMEOUT, allowOffline = false) {
  if (!navigator.onLine || (localOffline && !allowOffline)) throw new TypeError('当前离线，未发送网络请求。');
  const controller = new AbortController();
  activeRequests.add(controller);
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(url, { ...options, signal: controller.signal });
    // Consume the body before clearing the timer, including stalled response streams.
    const text = await response.text();
    return { ok: response.ok, status: response.status, headers: response.headers, json: () => JSON.parse(text) };
  } finally { clearTimeout(timer); activeRequests.delete(controller); }
}
function persistState(next = state) {
  ReaderStorage.saveShelf(DB_KEY, next);
  if (account) shelfWriteSnapshot = { scope: DB_KEY, text: metadataText(syncPayload(next)) };
}
async function persistCatalog(owner, next = null, url = owner.catalog) {
  const scope = DB_KEY, database = cacheDB;
  try { await ReaderStorage.putCatalog(database, owner.id, owner.chapters || [], url, next); }
  catch (error) { if (scope === DB_KEY) toast(`目录暂未保存至本机。${error.message}`); }
}
async function restoreCatalog(owner) {
  try {
    const stored = await ReaderStorage.catalog(cacheDB, owner.id);
    if (stored) { owner.chapters = stored.chapters; return stored; }
  } catch {}
  owner.chapters ||= [];
  return null;
}

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
  try {
    const record = existing || { id: data.id, title: data.title, author: data.author, catalog, lastUrl: `${catalog}/0`, progress: 0, chapters: [] };
    let nextState;
    await ReaderStorage.importBook(database, data, () => {
      if (scope !== DB_KEY || prepared.seq !== importSequence) throw new Error('账号已变化，已停止导入。');
      nextState = existing ? state : { ...state, books: [record, ...state.books] };
      persistState(nextState);
    });
    if (scope !== DB_KEY || prepared.seq !== importSequence) return;
    state = nextState; save(); renderShelf();
    preparedEbook = null; $('ebook-confirm').close();
    $('ebook-status').textContent = `已保存《${data.title}》，共 ${data.sections.length} 节，仅本机可用。`;
    busy(false);
    await load(record.lastUrl || catalog, record, true);
  } catch (error) {
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
  if (localOffline) throw new Error('当前为本机离线模式，连接阅读服务后才能搜索或检查新来源。');
  try {
    response = await fetchTimed(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
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
  }
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
let lastSaveSucceeded = true;
function save() {
  if (cloudMode && !account) return false;
  try { if (account && !reconcileShelf()) { updateAccountStatus(); return false; } }
  catch (error) { lastSaveSucceeded = false; toast(`本机副本未能合并，已停止同步。${error.message}`); return false; }
  const snapshot = account ? metadataText() : '', changed = snapshot !== metadataSnapshot;
  try {
    if (!account || changed || !lastSaveSucceeded) persistState();
    if (account && (changed || !lastSaveSucceeded)) {
      metadataSnapshot = snapshot; shelfWriteSnapshot = null;
      dirty = snapshot !== syncedSnapshot;
      if (dirty) localStorage.setItem(`${DB_KEY}:dirty`, '1');
      else { localStorage.removeItem(`${DB_KEY}:dirty`); clearTimeout(syncTimer); syncTimer = null; }
    }
    lastSaveSucceeded = true;
  }
  catch {
    if (account && changed) dirty = true;
    lastSaveSucceeded = false; toast('本机存储不可用，本次书架或阅读进度未能保存。');
  }
  if (account && changed) { syncRetryCount = 0; scheduleSync(); updateAccountStatus(); }
  if (!lockingAccount) checkOfflineAccess();
  return lastSaveSucceeded;
}
function element(tag, text, cls) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (cls) node.className = cls;
  return node;
}
function openDB() {
  const scope = DB_KEY;
  return ReaderStorage.open(cacheName, message => { if (scope === DB_KEY) toast(message); });
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
  if (!checkOfflineAccess()) throw new Error('请重新登录后读取本机电子书。');
  const match = /^ebook:([a-f0-9]{64})(?:\/(\d+))?$/.exec(url);
  if (!match) throw new Error('本机电子书地址无效。');
  const scope = DB_KEY, database = cacheDB;
  const stored = await ReaderStorage.book(database, match[1]);
  if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
  if (!stored) throw new Error('此设备没有该电子书正文，请回书架重新导入同一 EPUB / TXT 文件；云端只同步书架和进度。');
  const data = stored.meta, base = `ebook:${data.id}`;
  const result = { url, bookTitle: data.title, author: data.author, catalog: base, chapters: [],
    paragraphs: [], previous: null, next: null, nextPage: null, catalogNext: null, pages: 1, warnings: [] };
  if (match[2] === undefined) return { ...result, kind: 'catalog', title: data.title,
    chapters: data.titles.map((title, i) => ({ title, url: `${base}/${i}` })) };
  const index = Number(match[2]);
  if (!Number.isSafeInteger(index) || index >= data.sectionCount) throw new Error('此电子书章节不存在。');
  const section = stored.legacy ? stored.legacy.sections[index] : await ReaderStorage.section(database, data.id, index);
  if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
  if (!section) throw new Error('本机章节缺失，请重新导入同一文件修复。');
  return { ...result, kind: 'chapter', title: section.title, paragraphs: section.paragraphs,
    previous: index > 0 ? `${base}/${index - 1}` : null, next: index + 1 < data.sectionCount ? `${base}/${index + 1}` : null };
}
async function getPage(url, refresh = false) {
  if (!checkOfflineAccess()) throw new Error('离线资格已失效，请联网登录；本机未同步数据仍保留。');
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
    return { ...stored, fromCache: true, offline: !navigator.onLine || localOffline };
  }
  if (!navigator.onLine || localOffline) {
    if (stored) return { ...stored, offline: true };
    const owner = state.books.find(b => b.catalog === url);
    if (owner) {
      let directory;
      try { directory = await ReaderStorage.catalog(database, owner.id); } catch {}
      if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
      const chapters = directory?.chapters || owner.chapters || [];
      if (chapters.length) return { kind: 'catalog', url, title: owner.title, bookTitle: owner.title, chapters,
        catalogNext: directory?.next || null, warnings: [], pages: 1, paragraphs: [], offline: true };
    }
    throw new Error('当前处于离线模式，且该章节尚未缓存。连接阅读服务后请重试。');
  }
  let response;
  try {
    response = await fetchTimed('/api/read', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ url }) });
  } catch (error) {
    if (scope !== DB_KEY) throw new Error('账号已切换，已停止读取。');
    if (stored) return { ...stored, offline: true };
    if (error.name === 'AbortError') throw new Error('章节读取超时，请稍后重试或更换来源。');
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
  if (localOffline || !navigator.onLine || !checkOfflineAccess() || !chapter?.next || chapter.warnings.length || chapter.restrictedNext || chapter.url.startsWith('ebook:')) return;
  let next;
  try {
    next = new URL(chapter.next);
    if (!['http:', 'https:'].includes(next.protocol) || next.origin !== new URL(chapter.url).origin || next.href === chapter.url) return;
  } catch { return; }
  prefetchTimer = setTimeout(() => {
    if (scope !== DB_KEY || current !== chapter || document.hidden || localOffline || !navigator.onLine || navigator.connection?.saveData) return;
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
      if (pending || !confirm(`从书架移除《${item.title}》？`)) return;
      const scope = DB_KEY, database = cacheDB;
      const next = { ...state, books: state.books.filter(x => x.id !== item.id) };
      busy(true);
      try {
        const previousRaw = localStorage.getItem(scope);
        persistState(next);
        const writtenRaw = localStorage.getItem(scope);
        try {
          const db = await database;
          if (db || item.catalog?.startsWith('ebook:')) await ReaderStorage.removeBook(database, item.id, item.catalog?.startsWith('ebook:') ? item.catalog.slice(6) : null);
        } catch (error) {
          if (localStorage.getItem(scope) === writtenRaw) {
            if (previousRaw === null) localStorage.removeItem(scope);
            else localStorage.setItem(scope, previousRaw);
          }
          throw error;
        }
        if (scope !== DB_KEY) return;
        state = next; save(); renderShelf();
      } catch (error) { if (scope === DB_KEY) toast(`移除未完成。${error.message}`); }
      finally { if (scope === DB_KEY) busy(false); }
    });
    actions.append(element('span', `${Math.round((item.progress || 0) * 100)}% 本章`), remove);
    card.append(open, actions); $('shelf').append(card);
  }
}
function readingTheme(settings = state.settings) {
  if (settings.theme === 'night' || settings.theme === 'green') return settings.theme;
  return settings.theme === 'paper' && DAY_PALETTES.includes(settings.dayPalette) ? settings.dayPalette : 'paper';
}
function selectReadingTheme(theme) {
  if (!Object.hasOwn(READING_THEMES, theme)) return;
  if (DAY_PALETTES.includes(theme)) {
    state.settings.theme = 'paper'; state.settings.dayPalette = theme;
  } else {
    state.settings.theme = theme;
    if (theme !== 'night') delete state.settings.dayPalette;
  }
  applySettings(); save();
}
function applySettings() {
  const s = state.settings;
  if (!['paper', 'green', 'night'].includes(s.theme)) s.theme = 'paper';
  s.size = Math.max(16, Math.min(30, Number(s.size) || 21));
  if (!['1.7', '1.95', '2.2'].includes(s.line)) s.line = '1.95';
  if (!['serif', 'sans-serif'].includes(s.font)) s.font = 'serif';
  const theme = readingTheme(s);
  document.body.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme === 'night' ? 'dark' : 'light';
  const background = getComputedStyle(document.body).getPropertyValue('--bg').trim();
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', background);
  document.documentElement.style.setProperty('--font-size', `${s.size}px`);
  document.documentElement.style.setProperty('--line-height', s.line);
  document.documentElement.style.setProperty('--reading-font', s.font);
  $('font-size').value = s.size; $('font-output').textContent = `${s.size}px`;
  $('line-height').value = s.line; $('font-family').value = s.font;
  document.querySelectorAll('button[data-theme]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.theme === theme)));
  $('theme-status').textContent = `当前配色：${READING_THEMES[theme]}${theme === 'green' ? ' · 护眼推荐' : ''}`;
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
  if (pending || !url || !checkOfflineAccess()) return;
  saveProgress();
  clearTimeout(prefetchTimer);
  const seq = ++requestSequence;
  const scope = DB_KEY;
  busy(true); $('import-error').textContent = '';
  try {
    let data = await getPage(url, refresh);
    if (seq !== requestSequence || scope !== DB_KEY) return;
    const previousBook = book;
    book = makeBook(data, existing);
    book.chapters ||= [];
    if (book !== previousBook) { catalogNext = null; catalogUrl = book.catalog; }
    if (data.kind === 'catalog') {
      book.chapters = data.chapters; book.catalog = data.url;
      catalogNext = data.catalogNext; catalogUrl = data.url;
      await persistCatalog(book, catalogNext, catalogUrl);
      if (seq !== requestSequence || scope !== DB_KEY) return;
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
  const owner = book, scope = DB_KEY;
  if (!$('catalog-dialog').open) $('catalog-dialog').showModal();
  busy(true);
  try {
    if (!more) {
      const stored = await restoreCatalog(owner);
      if (scope !== DB_KEY || book !== owner) return;
      catalogNext = stored?.next || null; catalogUrl = stored?.url || owner.catalog;
      renderCatalog();
      if ((localOffline || !navigator.onLine) && owner.chapters.length) return;
      $('catalog-status').textContent = '正在刷新目录…';
    }
    const url = more ? catalogNext : owner.catalog;
    if (!url) throw new Error('此页未识别到目录，请从小说目录页重新导入。');
    const data = await getPage(url);
    if (scope !== DB_KEY || book !== owner) return;
    if (data.kind !== 'catalog') throw new Error('该链接未返回目录。');
    if (!more) owner.chapters = [];
    mergeChapters(data.chapters);
    catalogNext = data.catalogNext === url || data.catalogNext === catalogUrl ? null : data.catalogNext;
    catalogUrl = data.url;
    await persistCatalog(owner, catalogNext, catalogUrl);
    if (scope !== DB_KEY || book !== owner) return;
    save(); renderCatalog();
  } catch (err) { if (scope === DB_KEY && book === owner) { renderCatalog(); toast(`${err.message}${owner.chapters.length ? ' 已显示本机保存的目录。' : ''}`); } }
  finally { if (scope === DB_KEY) busy(false); }
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
document.querySelectorAll('button[data-theme]').forEach(button => button.addEventListener('click', () => selectReadingTheme(button.dataset.theme)));
$('clear-cache').addEventListener('click', async () => {
  const db = await cacheDB;
  if (!db) return toast('章节缓存不可用。');
  const tx = db.transaction('pages', 'readwrite'); tx.objectStore('pages').clear();
  tx.oncomplete = () => toast('本机章节缓存已清除，书架与进度仍保留。');
});
addEventListener('scroll', updateProgress, { passive: true });
addEventListener('pagehide', saveProgress);
document.addEventListener('visibilitychange', () => { if (checkOfflineAccess() && document.hidden) saveProgress(); });
function networkFailure(error) { return error?.name === 'TypeError' || error?.name === 'AbortError'; }
function validUser(user) {
  if (!user || typeof user.id !== 'string' || !user.id || typeof user.username !== 'string' || !user.username) throw new Error('账号身份响应无效，请重新连接。');
  return { id: user.id, username: user.username };
}
function validRemote(remote) {
  if (!remote || !Number.isSafeInteger(remote.version) || remote.version < 0 || !('payload' in remote) ||
    (remote.payload !== null && (typeof remote.payload !== 'object' || !Array.isArray(remote.payload.books) ||
      remote.payload.books.some(b => !b || typeof b !== 'object' || Array.isArray(b)) ||
      (remote.payload.settings !== undefined && (!remote.payload.settings || typeof remote.payload.settings !== 'object' || Array.isArray(remote.payload.settings)))))) {
    throw new Error('云端书架响应无效，请重新连接。');
  }
  return remote;
}
function offlineGrant(id = null) {
  try {
    if (localStorage.getItem(LOGOUT_KEY)) return null;
    const grant = JSON.parse(localStorage.getItem(OFFLINE_KEY) || 'null'), now = Date.now();
    if (!grant || typeof grant.id !== 'string' || !grant.id || typeof grant.username !== 'string' || !grant.username ||
      !Number.isFinite(grant.checkedAt) || grant.checkedAt > now || grant.checkedAt <= 0 ||
      grant.expiresAt !== grant.checkedAt + OFFLINE_LIFETIME || grant.expiresAt <= now || (id && grant.id !== id)) return null;
    return grant;
  } catch { return null; }
}
function requireAuthentication(sequence = authSequence) {
  if (sequence !== authSequence || localStorage.getItem(LOGOUT_KEY)) {
    const error = new Error('认证期间已退出，请重新连接或登录。'); error.name = 'AuthChangedError'; throw error;
  }
}
function rememberAccount(user, sequence = authSequence) {
  requireAuthentication(sequence);
  const checkedAt = Date.now();
  try { localStorage.setItem(OFFLINE_KEY, JSON.stringify({ ...validUser(user), checkedAt, expiresAt: checkedAt + OFFLINE_LIFETIME })); }
  catch { toast('无法保存本设备离线资格；在线阅读仍可使用。'); }
}
function revokeOffline() { try { localStorage.removeItem(OFFLINE_KEY); } catch {} }
function stopNetworkWork() {
  clearTimeout(syncTimer); syncTimer = null; clearTimeout(prefetchTimer); prefetchTimer = null;
  for (const controller of activeRequests) controller.abort();
}
function returnToShelf() {
  saveProgress();
  if (account) save();
  clearTimeout(saving); clearTimeout(prefetchTimer); ++requestSequence;
  current = null; book = null; restoringProgress = false;
  document.body.classList.remove('focus'); $('exit-focus').hidden = true; $('reader').hidden = true;
  document.title = '纯阅 · 把世界留在书外';
}
function lockAccount(message) {
  if (lockingAccount) return;
  lockingAccount = true;
  saveProgress(); stopNetworkWork(); clearTimeout(expiryTimer); expiryTimer = null;
  ++accountEpoch; ++requestSequence; ++importSequence; ++authSequence;
  preparedEbook = null; current = null; book = null; restoringProgress = false;
  account = null; cloudMode = true; localOffline = false; dirty = false; syncing = false; syncBlocked = true;
  metadataSnapshot = ''; syncedSnapshot = null; shelfConflict = null; shelfWriteSnapshot = null;
  const previousDB = cacheDB; cacheDB = null; Promise.resolve(previousDB).then(db => db?.close()).catch(() => {});
  DB_KEY = 'pure-novel-locked'; state = { books: [], settings: { ...DEFAULTS } };
  document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close());
  document.body.classList.remove('focus'); $('exit-focus').hidden = true; $('content').replaceChildren();
  busy(false); renderShelf(); $('reader').hidden = true; $('home').hidden = true; $('account-bar').hidden = true;
  $('offline-banner').hidden = true; $('login-screen').hidden = false; $('login-form').hidden = false;
  $('login-error').textContent = message; $('password').value = '';
  lockingAccount = false;
}
function checkOfflineAccess() {
  if (lockingAccount) return true;
  if (account && localOffline && !offlineGrant(account.id)) {
    lockAccount('本设备的 7 天离线资格已到期或被撤销。请联网重新登录；本机副本和未同步进度仍保留。');
    return false;
  }
  return !cloudMode || Boolean(account);
}
function refreshExpiryTimer() {
  clearTimeout(expiryTimer); expiryTimer = null;
  if (!account || !localOffline) return;
  const grant = offlineGrant(account.id);
  if (!grant) { checkOfflineAccess(); return; }
  expiryTimer = setTimeout(() => { if (checkOfflineAccess()) refreshExpiryTimer(); }, Math.max(1, grant.expiresAt - Date.now()));
}
function updateAccountStatus() {
  if (!account) return;
  $('account-status').textContent = syncBlocked ? '同步冲突，请选择版本' : (localOffline ? '离线 · 变更未同步' : (dirty ? (lastSaveSucceeded ? '未同步，已保存在本机' : '未同步，本机保存也未成功') : '已同步'));
  $('offline-banner').hidden = !localOffline;
  if (localOffline) {
    const grant = offlineGrant(account.id);
    $('offline-message').textContent = `账号 ${account.username} · 本设备从最近成功联网身份验证起可离线 7 天${grant ? `，至 ${new Date(grant.expiresAt).toLocaleString()}` : ''}。仅可阅读本机内容，变更未同步；联网后先验证账号再同步。`;
  }
}
function enterAccountOffline() {
  if (!account || !offlineGrant(account.id)) { checkOfflineAccess(); return false; }
  localOffline = true; stopNetworkWork(); syncing = false;
  if ($('reader').hidden) { renderShelf(); $('home').hidden = false; $('login-screen').hidden = true; $('account-bar').hidden = false; }
  updateAccountStatus(); refreshExpiryTimer();
  return true;
}
async function accountAPI(path, body, timeout = REQUEST_TIMEOUT, allowOffline = false) {
  const response = await fetchTimed(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, timeout, allowOffline);
  let data;
  try { data = await response.json(); }
  catch { const error = new Error(`账号接口返回非 JSON 响应（HTTP ${response.status}）。`); error.status = response.status; throw error; }
  if (!response.ok) {
    const error = new Error(data?.error || '账号服务异常。'); error.status = response.status;
    const retry = response.headers.get('Retry-After');
    error.retryAfter = retry ? (/^\d+$/.test(retry) ? Number(retry) * 1000 : Date.parse(retry) - Date.now()) : 0;
    throw error;
  }
  return data;
}
async function drainPendingLogout() {
  const marker = localStorage.getItem(LOGOUT_KEY);
  if (!marker) return;
  const result = await accountAPI('/api/logout', {}, REQUEST_TIMEOUT, true);
  if (result?.ok !== true) throw new Error('尚未完成上次退出，请联网重试。');
  if (localStorage.getItem(LOGOUT_KEY) === marker) localStorage.removeItem(LOGOUT_KEY);
  if (localStorage.getItem(LOGOUT_KEY)) throw new Error('其他标签页再次退出，请重新连接。');
}
async function verifiedAccount(sequence = authSequence) {
  requireAuthentication(sequence);
  let data;
  try { data = await accountAPI('/api/bootstrap', undefined, REQUEST_TIMEOUT, true); }
  catch (error) {
    requireAuthentication(sequence);
    if (![404, 405].includes(error.status)) throw error;
    const session = await accountAPI('/api/session', undefined, REQUEST_TIMEOUT, true);
    requireAuthentication(sequence);
    const user = validUser(session?.user);
    rememberAccount(user, sequence);
    const remote = validRemote(await accountAPI('/api/state', undefined, REQUEST_TIMEOUT, true));
    requireAuthentication(sequence);
    return { user, ...remote };
  }
  requireAuthentication(sequence);
  if (data?.local === true) return { local: true };
  const user = validUser(data?.user), remote = validRemote(data);
  rememberAccount(user, sequence);
  return { user, version: remote.version, payload: remote.payload };
}
function syncPayload(source = state) {
  // Local catalog/schema fields never participate in cloud dirtiness.
  return { books: source.books.map(({ chapters, schema, ...metadata }) => metadata), settings: { ...DEFAULTS, ...source.settings } };
}
function metadataText(payload = syncPayload()) {
  const ordered = value => Array.isArray(value) ? value.map(ordered) : (value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(key => [key, ordered(value[key])])) : value);
  return JSON.stringify(ordered(payload));
}
function mergeShelfMetadata(base, local, stored, preferLocal = true) {
  let conflict = false;
  const choose = (before, here, there) => {
    if (metadataText([here]) === metadataText([before])) return there;
    if (metadataText([there]) === metadataText([before]) || metadataText([here]) === metadataText([there])) return here;
    conflict = true; return preferLocal ? here : there;
  };
  const originals = new Map(base.books.map(b => [b.id, b])), ours = new Map(local.books.map(b => [b.id, b])), theirs = new Map(stored.books.map(b => [b.id, b]));
  const books = [...new Set([...ours.keys(), ...theirs.keys()])].map(id => choose(originals.get(id), ours.get(id), theirs.get(id))).filter(Boolean);
  const settings = {};
  for (const key of new Set([...Object.keys(base.settings), ...Object.keys(local.settings), ...Object.keys(stored.settings)])) settings[key] = choose(base.settings[key], local.settings[key], stored.settings[key]);
  return { payload: { books, settings }, conflict };
}
function replaceShelfMetadata(payload) {
  const existing = new Map(state.books.map(b => [b.id, b]));
  state = { books: payload.books.map(metadata => {
    const owner = existing.get(metadata.id) || { chapters: [] };
    for (const key of Object.keys(owner)) if (key !== 'chapters' && !(key in metadata)) delete owner[key];
    return Object.assign(owner, metadata);
  }), settings: { ...DEFAULTS, ...payload.settings } };
}
function showSyncConflict() {
  $('conflict-title').textContent = shelfConflict ? '其他标签页更新了本机副本' : '其他设备更新了数据';
  $('conflict-message').textContent = shelfConflict ? '当前标签页与其他标签页修改了同一本书或同一项设置，两份修改均已保存在本机，尚未上传。请选择冲突部分使用哪一份；其他互不冲突的修改会一并保留。' : '请选择保留哪一份书架和进度。使用本机版本将尝试覆盖最新云端版本；使用云端版本会舍弃本机未同步变更。';
  $('keep-local').textContent = shelfConflict ? '保留当前标签页修改' : '保留本机版本';
  $('keep-cloud').textContent = shelfConflict ? '使用其他标签页修改' : '使用云端版本';
  if (!$('conflict-dialog').open) $('conflict-dialog').showModal();
}
function reconcileShelf() {
  if (!account || !metadataSnapshot) return true;
  const raw = JSON.parse(localStorage.getItem(DB_KEY) || '{"books":[],"settings":{}}');
  if (!Array.isArray(raw.books)) throw new Error('本机书架格式无效，已停止同步以保留数据。');
  const stored = syncPayload(raw), storedText = metadataText(stored), local = syncPayload();
  const base = shelfConflict?.base || JSON.parse(metadataSnapshot);
  // Imports publish metadata before save() records cloud dirtiness.
  const ownedWrite = !shelfConflict && shelfWriteSnapshot?.scope === DB_KEY && shelfWriteSnapshot.text === storedText;
  const merged = mergeShelfMetadata(base, local, ownedWrite ? base : stored);
  const version = Number(localStorage.getItem(`${DB_KEY}:version`) || 0), storedDirty = localStorage.getItem(`${DB_KEY}:dirty`) === '1';
  if (version !== cloudVersion) syncedSnapshot = storedDirty ? null : storedText;
  cloudVersion = version;
  if (merged.conflict) {
    shelfConflict = { base, local: merged.payload, stored, version };
    localStorage.setItem(`${DB_KEY}:tab-conflict:${tabId}`, JSON.stringify(shelfConflict));
    localStorage.setItem(`${DB_KEY}:dirty`, '1');
    replaceShelfMetadata(merged.payload); dirty = true; syncBlocked = true;
    clearTimeout(syncTimer); syncTimer = null; showSyncConflict(); return false;
  }
  if (shelfConflict) { shelfConflict = null; syncBlocked = false; localStorage.removeItem(`${DB_KEY}:tab-conflict:${tabId}`); $('conflict-dialog').close(); }
  if (metadataText() !== metadataText(merged.payload)) replaceShelfMetadata(merged.payload);
  metadataSnapshot = ownedWrite ? metadataSnapshot : storedText;
  dirty = storedDirty || metadataText() !== metadataSnapshot;
  return true;
}
function resolveShelfConflict(keepLocal) {
  const stored = syncPayload(JSON.parse(localStorage.getItem(DB_KEY) || '{"books":[],"settings":{}}'));
  const merged = mergeShelfMetadata(shelfConflict.base, syncPayload(), stored, keepLocal);
  replaceShelfMetadata(merged.payload); persistState();
  metadataSnapshot = metadataText(); shelfWriteSnapshot = null; dirty = true; localStorage.setItem(`${DB_KEY}:dirty`, '1');
  localStorage.removeItem(`${DB_KEY}:tab-conflict:${tabId}`); shelfConflict = null; syncBlocked = false; syncRetryCount = 0;
  $('conflict-dialog').close(); applySettings(); renderShelf(); updateAccountStatus(); scheduleSync(0);
}
function usePayload(payload) {
  const localCatalogs = new Map(state.books.map(b => [b.id, b.chapters || []]));
  const next = { books: (payload.books || []).map(b => ({ ...b, chapters: localCatalogs.get(b.id) || [] })), settings: { ...DEFAULTS, ...payload.settings } };
  persistState(next); state = next; applySettings(); renderShelf(); metadataSnapshot = metadataText(); shelfWriteSnapshot = null;
}
function accountContext() { return { scope: DB_KEY, id: account?.id, epoch: accountEpoch }; }
function sameAccount(context) { return context.epoch === accountEpoch && context.scope === DB_KEY && context.id === account?.id; }
function scheduleSync(delay = 2500) {
  if (!account || !dirty || localOffline || !navigator.onLine || authChecking || syncBlocked || syncing || syncRetryCount >= MAX_SYNC_RETRIES) return;
  clearTimeout(syncTimer); syncTimer = setTimeout(() => { syncTimer = null; pushState(); }, delay);
}
function authenticationDenied(error) {
  if (![401, 403].includes(error.status)) return false;
  revokeOffline(); lockAccount('会话已失效或账号已停用。请重新登录；本机副本和未同步进度仍保留，无需清除数据。');
  return true;
}
async function pushState() {
  if (account && !syncing && !save()) return;
  clearTimeout(syncTimer); syncTimer = null;
  if (!checkOfflineAccess() || !account || !dirty || syncing || syncBlocked || authChecking || localOffline || !navigator.onLine) return;
  const context = accountContext(), version = cloudVersion;
  const payload = JSON.parse(JSON.stringify(syncPayload())), snapshot = metadataText(payload);
  if (snapshot === syncedSnapshot) { dirty = false; localStorage.removeItem(`${context.scope}:dirty`); updateAccountStatus(); return; }
  syncing = true;
  try {
    const result = await accountAPI('/api/state', { version, payload });
    if (!sameAccount(context) || cloudVersion !== version) return;
    if (result?.ok !== true || !Number.isSafeInteger(result.version) || result.version < version || result.version > version + 1) throw new Error('同步响应无效，未确认保存。');
    reconcileShelf();
    if (cloudVersion !== version) return;
    cloudVersion = result.version; syncedSnapshot = snapshot; syncRetryCount = 0;
    localStorage.setItem(`${context.scope}:version`, String(cloudVersion));
    dirty = Boolean(shelfConflict) || snapshot !== metadataText();
    if (dirty) localStorage.setItem(`${context.scope}:dirty`, '1');
    else localStorage.removeItem(`${context.scope}:dirty`);
    updateAccountStatus();
  } catch (error) {
    if (!sameAccount(context) || authenticationDenied(error)) return;
    if (networkFailure(error)) {
      if (!enterAccountOffline()) lockAccount('无法确认离线资格，请联网重新登录；本机数据仍保留。');
      return;
    }
    if (error.status === 409) { syncBlocked = true; showSyncConflict(); }
    else {
      ++syncRetryCount;
      if (syncRetryCount < MAX_SYNC_RETRIES) {
        syncing = false;
        scheduleSync(Math.min(60000, Math.max(1000, error.retryAfter || 0, 3000 * 2 ** (syncRetryCount - 1))));
      }
    }
    updateAccountStatus();
  } finally {
    if (sameAccount(context)) { syncing = false; if (dirty && !syncTimer && syncRetryCount === 0) scheduleSync(); }
  }
}
async function startAccount(user, remote, offline = false, sequence = authSequence) {
  requireAuthentication(sequence);
  user = validUser(user);
  if (offline && !offlineGrant(user.id)) throw new Error('本设备离线资格无效，请联网登录。');
  if (!offline && remote === undefined) { remote = validRemote(await accountAPI('/api/state')); rememberAccount(user, sequence); }
  if (!offline) validRemote(remote);
  const reuse = account?.id === user.id && DB_KEY === `pure-novel-user:${user.id}`;
  const flushing = lockingAccount; lockingAccount = true;
  try { returnToShelf(); } finally { lockingAccount = flushing; }
  requireAuthentication(sequence); stopNetworkWork(); ++accountEpoch; ++importSequence;
  preparedEbook = null; document.querySelectorAll('dialog[open]').forEach(dialog => dialog.close()); $('ebook-status').textContent = ''; busy(true);
  const epoch = accountEpoch;
  if (!reuse) {
    $('home').hidden = true; $('account-bar').hidden = true;
    state = { books: [], settings: { ...DEFAULTS } }; dirty = false; cloudVersion = 0; metadataSnapshot = ''; shelfConflict = null;
    const previousDB = cacheDB; cacheDB = null; Promise.resolve(previousDB).then(db => db?.close()).catch(() => {});
    account = user; DB_KEY = `pure-novel-user:${user.id}`; cacheName = `pure-novel-chapters:${user.id}`;
    cacheDB = openDB();
    const local = await ReaderStorage.loadShelf(DB_KEY, cacheDB, toast);
    if (epoch !== accountEpoch) return;
    requireAuthentication(sequence);
    state = { books: local.books, settings: { ...DEFAULTS, ...local.settings } };
    dirty = localStorage.getItem(`${DB_KEY}:dirty`) === '1';
    cloudVersion = Number(localStorage.getItem(`${DB_KEY}:version`) || 0);
    const recovery = JSON.parse(localStorage.getItem(`${DB_KEY}:tab-conflict:${tabId}`) || 'null');
    if (recovery?.base?.books && recovery?.local?.books) {
      shelfConflict = recovery; replaceShelfMetadata(recovery.local); metadataSnapshot = metadataText(recovery.base); reconcileShelf();
    }
  }
  requireAuthentication(sequence);
  account = user; localOffline = offline; cloudMode = true; syncing = false; syncBlocked = Boolean(shelfConflict); syncRetryCount = 0;
  serviceMode('cloud'); syncedSnapshot = null;
  applySettings(); if (!shelfConflict) metadataSnapshot = metadataText();
  if (!offline) {
    const payload = remote.payload || { books: [], settings: { ...DEFAULTS } };
    syncedSnapshot = metadataText({ books: payload.books.map(({ chapters, schema, ...metadata }) => metadata), settings: { ...DEFAULTS, ...payload.settings } });
    if (dirty && remote.version !== cloudVersion) syncBlocked = true;
    else if (!dirty) { cloudVersion = remote.version; usePayload(payload); syncedSnapshot = metadataText(); }
    else if (metadataText() === syncedSnapshot) { dirty = false; localStorage.removeItem(`${DB_KEY}:dirty`); }
    localStorage.setItem(`${DB_KEY}:version`, String(cloudVersion));
  }
  applySettings(); renderShelf(); busy(false); $('account-name').textContent = user.username; $('account-bar').hidden = false;
  $('login-screen').hidden = true; $('login-form').hidden = false; $('login-error').textContent = ''; $('home').hidden = false;
  $('import-local').hidden = !localStorage.getItem('pure-novel-v1');
  updateAccountStatus(); refreshExpiryTimer();
  if (syncBlocked) showSyncConflict();
  else scheduleSync(0);
}
function serviceMode(value) {
  try {
    if (value !== undefined) localStorage.setItem(MODE_KEY, value);
    return localStorage.getItem(MODE_KEY);
  } catch { return null; }
}
async function healthCheck(sequence = authSequence) {
  requireAuthentication(sequence);
  const health = await accountAPI('/api/health', undefined, 5000, true);
  requireAuthentication(sequence);
  if (health?.ok !== true || typeof health.authRequired !== 'boolean') throw new Error('阅读服务状态无效。');
  serviceMode(health.authRequired ? 'cloud' : 'local');
  return health;
}
async function startLocal(offline) {
  returnToShelf(); stopNetworkWork(); ++accountEpoch; clearTimeout(expiryTimer);
  const previousDB = cacheDB; Promise.resolve(previousDB).then(db => db?.close()).catch(() => {});
  account = null; cloudMode = false; localOffline = offline; dirty = false; syncing = false; syncBlocked = false;
  $('home').hidden = true; state = { books: [], settings: { ...DEFAULTS } };
  DB_KEY = 'pure-novel-v1'; cacheName = 'pure-novel-chapters';
  cacheDB = openDB();
  const scope = DB_KEY, epoch = accountEpoch;
  const local = await ReaderStorage.loadShelf(scope, cacheDB, toast);
  if (epoch !== accountEpoch) return;
  state = { books: local.books, settings: { ...DEFAULTS, ...local.settings } };
  applySettings(); renderShelf(); $('account-bar').hidden = true;
  $('login-screen').hidden = true; $('home').hidden = false; $('offline-banner').hidden = !offline;
  $('offline-message').textContent = '当前为本机离线模式，可阅读已保存的电子书和章节；变更仅保存在本机。';
}
async function recoverOffline(error) {
  if (!networkFailure(error) || localStorage.getItem(LOGOUT_KEY)) return false;
  const grant = offlineGrant();
  if (grant) {
    if (account?.id === grant.id) { enterAccountOffline(); return true; }
    await startAccount({ id: grant.id, username: grant.username }, null, true); return true;
  }
  if (!cloudMode && serviceMode() === 'local') { await startLocal(true); return true; }
  return false;
}
async function reconnectLocal(force = false) {
  if (reconnecting) return reconnecting;
  if (!force && !localOffline) return;
  if (!navigator.onLine) { checkOfflineAccess(); return; }
  reconnecting = (async () => {
    $('reconnect-local').disabled = true; authChecking = true;
    const seq = ++authSequence;
    returnToShelf(); stopNetworkWork(); ++accountEpoch; syncing = false;
    try {
      await drainPendingLogout();
      if (seq !== authSequence) return;
      requireAuthentication(seq);
      if (!cloudMode && !(await healthCheck(seq)).authRequired) { await startLocal(false); return; }
      const remote = await verifiedAccount(seq);
      requireAuthentication(seq);
      if (remote.local) await startLocal(false); else await startAccount(remote.user, remote, false, seq);
    } catch (error) {
      if (seq !== authSequence || authenticationDenied(error)) return;
      if (!(await recoverOffline(error))) lockAccount(`${error.message} 请联网重新登录；本机数据仍保留。`);
    } finally {
      authChecking = false; $('reconnect-local').disabled = false; reconnecting = null;
      scheduleSync(0);
    }
  })();
  return reconnecting;
}
async function initialize() {
  const seq = ++authSequence;
  try {
    await drainPendingLogout(); requireAuthentication(seq);
    const health = await healthCheck(seq); cloudMode = health.authRequired;
    if (!cloudMode) await startLocal(false);
    else {
      state = { books: [], settings: { ...DEFAULTS } };
      const remote = await verifiedAccount(seq);
      requireAuthentication(seq);
      if (remote.local) await startLocal(false); else await startAccount(remote.user, remote, false, seq);
    }
  } catch (error) {
    if (seq !== authSequence) return;
    if (!authenticationDenied(error) && !(await recoverOffline(error))) lockAccount(`${error.message} 请联网登录或重新连接；本机副本和未同步进度仍保留。`);
  } finally { document.documentElement.dataset.ready = 'true'; }
}
$('reconnect-local').addEventListener('click', () => reconnectLocal(true));
$('login-form').addEventListener('submit', async event => {
  event.preventDefault(); $('login-submit').disabled = true; $('login-error').textContent = '';
  const seq = ++authSequence;
  let authenticated = false;
  try {
    await drainPendingLogout(); requireAuthentication(seq);
    const data = await accountAPI('/api/login', { username: $('username').value.trim(), password: $('password').value }, REQUEST_TIMEOUT, true);
    if (seq !== authSequence) return;
    const user = validUser(data?.user); authenticated = true; rememberAccount(user, seq); $('password').value = '';
    const remote = await verifiedAccount(seq);
    requireAuthentication(seq);
    if (remote.local) await startLocal(false); else await startAccount(remote.user, remote, false, seq);
  } catch (error) {
    if (seq !== authSequence) return;
    if (networkFailure(error) && await recoverOffline(error)) return;
    if ((account || authenticated) && authenticationDenied(error)) return;
    $('login-error').textContent = error.message;
  } finally { $('password').value = ''; $('login-submit').disabled = false; }
});
$('logout').addEventListener('click', async () => {
  if (!account || !confirm('退出并清除当前账号在此浏览器的书架和章节缓存？未同步的变更会丢失。离线退出将在下次联网时先撤销登录 Cookie。')) return;
  const scope = DB_KEY, database = cacheDB, name = cacheName;
  const online = navigator.onLine && !localOffline;
  try {
    localStorage.setItem(LOGOUT_KEY, String(Date.now())); revokeOffline();
    lockAccount('已退出。本机当前账号数据已清除；若仍离线，下次联网将先完成服务端退出。');
    for (const suffix of ['', ':version', ':dirty', ':import-task']) localStorage.removeItem(`${scope}${suffix}`);
    for (const key of Object.keys(localStorage)) if (key.startsWith(`${scope}:tab-conflict:`)) localStorage.removeItem(key);
    const db = await database; db?.close(); indexedDB.deleteDatabase(name);
    if (online) await drainPendingLogout();
  } catch (error) { toast(error.message); }
});
$('sync-now').addEventListener('click', () => {
  if (localOffline || !navigator.onLine || syncBlocked) return reconnectLocal(true);
  syncRetryCount = 0;
  if (dirty) return pushState();
  return reconnectLocal(true);
});
for (const [id, keepLocal] of [['keep-local', true], ['keep-cloud', false]]) $(id).addEventListener('click', async () => {
  if (account && shelfConflict) {
    try { resolveShelfConflict(keepLocal); } catch (error) { toast(`冲突副本仍保留。${error.message}`); }
    return;
  }
  if (!account || localOffline || !navigator.onLine || authChecking) return;
  returnToShelf(); stopNetworkWork(); ++accountEpoch; syncing = false; authChecking = true;
  const context = accountContext(), before = metadataText();
  try {
    const remote = await verifiedAccount();
    if (!sameAccount(context)) return;
    if (remote.local) throw new Error('账号服务模式已变化，请重新连接。');
    if (remote.user.id !== context.id) { await startAccount(remote.user, remote); return; }
    if (before !== metadataText()) throw new Error('本机刚有变更，请重新选择版本。');
    cloudVersion = remote.version;
    if (!keepLocal) {
      usePayload(remote.payload || { books: [], settings: DEFAULTS }); syncedSnapshot = metadataText();
      dirty = false; localStorage.removeItem(`${DB_KEY}:dirty`);
    } else { dirty = true; syncedSnapshot = null; localStorage.setItem(`${DB_KEY}:dirty`, '1'); }
    localStorage.setItem(`${DB_KEY}:version`, String(cloudVersion)); syncBlocked = false; syncRetryCount = 0; $('conflict-dialog').close();
  } catch (error) {
    if (sameAccount(context) && !authenticationDenied(error)) {
      if (networkFailure(error)) enterAccountOffline();
      toast(error.message);
    }
  } finally { authChecking = false; updateAccountStatus(); scheduleSync(0); }
});
$('import-local').addEventListener('click', async () => {
  if (pending || !confirm('将旧版未登录书架合并到当前账号？请确认这是你自己的数据。电子书正文需在当前账号重新导入。')) return;
  const scope = DB_KEY, database = cacheDB;
  const legacyDB = ReaderStorage.open('pure-novel-chapters', toast);
  busy(true);
  try {
    const legacy = await ReaderStorage.loadShelf('pure-novel-v1', legacyDB, toast);
    const source = localStorage.getItem('pure-novel-v1');
    const next = { ...state, books: [...state.books] };
    for (const item of legacy.books) {
      if (next.books.some(b => b.id === item.id)) continue;
      const directory = await ReaderStorage.catalog(legacyDB, item.id);
      const chapters = directory?.chapters || item.chapters || [];
      if (chapters.length) await ReaderStorage.putCatalog(database, item.id, chapters, directory?.url || item.catalog, directory?.next);
      next.books.push({ ...item, chapters: [] });
    }
    if (scope !== DB_KEY) throw new Error('账号已变化。');
    persistState(next); state = next;
    save(); renderShelf();
    if (localStorage.getItem('pure-novel-v1') === source) localStorage.removeItem('pure-novel-v1');
    $('import-local').hidden = !localStorage.getItem('pure-novel-v1');
  } catch (error) { toast(`旧书架导入未完成，原数据已保留。${error.message}`); }
  finally { const db = await legacyDB; db?.close(); if (scope === DB_KEY) busy(false); }
});
addEventListener('offline', () => {
  if (account) { if (!enterAccountOffline()) lockAccount('离线资格已到期，请联网登录；本机数据仍保留。'); }
  else if (!cloudMode && serviceMode() === 'local') {
    localOffline = true; stopNetworkWork(); $('offline-banner').hidden = false;
    $('offline-message').textContent = '当前为本机离线模式，可阅读已保存的电子书和章节；变更仅保存在本机。';
  }
});
addEventListener('online', () => { if (account || localOffline || localStorage.getItem(LOGOUT_KEY)) reconnectLocal(true); });
addEventListener('storage', event => {
  if (![null, OFFLINE_KEY, LOGOUT_KEY].includes(event.key)) return;
  if (event.key === null || localStorage.getItem(LOGOUT_KEY) ||
    (event.key === OFFLINE_KEY && event.newValue === null) || (account && !offlineGrant(account.id))) {
    lockAccount('此账号已在其他标签页退出或撤销离线资格。请重新登录；未同步数据仍保留。');
  } else if (account) refreshExpiryTimer();
});
for (const type of ['click', 'input', 'change', 'submit']) document.addEventListener(type, event => {
  if (account && localOffline && !checkOfflineAccess()) { event.preventDefault(); event.stopImmediatePropagation(); }
}, true);
initialize();
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('/sw.js').catch(() => {});