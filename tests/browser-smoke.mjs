// Dependency-free Chromium/Edge smoke test. Start reader.py before running.
import { spawn } from 'node:child_process';
import { mkdtemp, rm, readFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import assert from 'node:assert/strict';

const executable = process.env.BROWSER_PATH || 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe';
const profile = await mkdtemp(join(tmpdir(), 'pure-novel-smoke-'));
const browser = spawn(executable, ['--headless', '--disable-gpu', '--no-first-run', '--no-default-browser-check', '--remote-debugging-port=9224', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' });
let socket;
let id = 0;
const waiting = new Map();
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function command(method, params = {}) {
  const key = ++id;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { waiting.delete(key); reject(new Error(`CDP timeout: ${method}`)); }, 15000);
    waiting.set(key, result => { clearTimeout(timer); result.error ? reject(new Error(JSON.stringify(result.error))) : resolve(result.result); });
    socket.send(JSON.stringify({ id: key, method, params }));
  });
}
async function evaluate(expression) {
  const r = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
  return r.result.value;
}
async function until(expression) {
  for (let i = 0; i < 80; i++) { if (await evaluate(expression)) return; await sleep(100); }
  throw new Error(`UI condition timed out: ${expression}`);
}
try {
  let target;
  for (let i = 0; i < 60; i++) {
    try { target = (await (await fetch('http://127.0.0.1:9224/json/list')).json()).find(x => x.type === 'page'); if (target) break; } catch {}
    await sleep(200);
  }
  assert.ok(target, 'Browser debugging target should start');
  socket = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
  socket.onmessage = event => { const result = JSON.parse(event.data); if (waiting.has(result.id)) { waiting.get(result.id)(result); waiting.delete(result.id); } };
  await command('Emulation.setDeviceMetricsOverride', { width: 390, height: 844, deviceScaleFactor: 1, mobile: true });
  await command('Page.navigate', { url: 'http://127.0.0.1:8000/' });
  await until('document.documentElement.dataset.ready === "true"');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, 'Mobile home should not overflow');
  assert.equal(await evaluate('document.querySelector("#mode-ebook, #publication-form, #openbooks, #import-task")'), null, 'External ebook acquisition UI stays removed');
  assert.equal(await evaluate('!!document.getElementById("ebook-file")'), true, 'Manual import remains available');
  const errors = await evaluate(`(async () => {
    const original = window.fetch;
    const messages = [];
    try {
      window.fetch = async () => { throw new TypeError('Failed to fetch'); };
      try { await getPage('https://example.com/uncached'); } catch (e) { messages.push(e.message); }
      window.fetch = async () => new Response(JSON.stringify({error:'源站返回 HTTP 403，无法读取；不会绕过登录或访问限制。'}), {status:422});
      try { await getPage('https://example.com/denied'); } catch (e) { messages.push(e.message); }
      window.fetch = async () => new Response('<html>Not an API</html>', {status:404});
      try { await getPage('https://example.com/wrong-server'); } catch (e) { messages.push(e.message); }
    } finally { window.fetch = original; }
    return messages;
  })()`);
  assert.match(errors[0], /无法连接阅读器后端/);
  assert.match(errors[1], /源站返回 HTTP 403/);
  assert.match(errors[2], /非 JSON 响应（HTTP 404）/);
  const searchErrors = await evaluate(`(async () => {
    const original = window.fetch, messages = [];
    try {
      for (const mode of ['network', 'timeout', 'gateway', 'html']) {
        window.fetch = async () => {
          if (mode === 'network') throw new TypeError('Failed to fetch');
          if (mode === 'timeout') throw new DOMException('Aborted', 'AbortError');
          return new Response('<html>Error</html>', {status: mode === 'gateway' ? 504 : 404});
        };
        try { await searchAPI('/api/search', {query:'测试小说'}); } catch (e) { messages.push(e.message); }
      }
    } finally { window.fetch = original; }
    return messages;
  })()`);
  assert.match(searchErrors[0], /无需在电脑上启动 Python/);
  assert.match(searchErrors[1], /超时/);
  assert.match(searchErrors[2], /HTTP 504/);
  assert.match(searchErrors[3], /非 JSON 响应/);
  await evaluate(`(() => {
    window.fetch = async (url, opts) => {
      const body = JSON.parse(opts.body);
      if(url === '/api/search') return new Response(JSON.stringify({query:'测试小说',results:[
        {title:'测试小说受限来源',url:'https://denied.example/book/',host:'denied.example',engines:['Bing']},
        {title:'测试小说目录',url:'https://example.com/catalog/',host:'example.com',engines:['360']}
      ],warnings:[]}));
      if(body.url.includes('denied.example')) return new Response(JSON.stringify({error:'源站返回 HTTP 403'}),{status:422});
      return new Response(JSON.stringify({url:'https://example.com/catalog/',bookTitle:'测试小说',sampleTitle:'第一章 开始',characters:2000,note:'仅验证样本'}));
    };
    document.getElementById('book-query').value='测试小说';
    document.getElementById('search-form').requestSubmit();
  })()`);
  await until('!document.getElementById("search-button").disabled');
  assert.equal(await evaluate('document.querySelectorAll(".source-card").length'), 2);
  assert.equal(await evaluate('document.querySelectorAll(".source-status[data-state=ready]").length'), 1);
  assert.equal(await evaluate('document.querySelectorAll(".source-status[data-state=failed]").length'), 1);
  assert.equal(await evaluate('document.querySelectorAll("#search-results .source-card").length'), 1, 'Only verified sources should appear by default');
  assert.equal(await evaluate('document.getElementById("search-diagnostics").open'), false, 'Failures should be collapsed');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, 'Search cards should not overflow');
  await evaluate(`(() => {
    window.searchRounds = []; window.checkedHosts = [];
    window.fetch = async (url, opts) => {
      const body = JSON.parse(opts.body);
      if(url === '/api/search') {
        window.searchRounds.push(body.round);
        const hosts = body.round === 0 ? ['one.example','two.example','three.example','four.example'] : ['five.example','six.example'];
        return new Response(JSON.stringify({query:'测试小说',results:hosts.map(host=>({title:'测试小说目录',url:'https://'+host+'/catalog/',host,engines:['Bing']})),warnings:[],hasMore:body.round<2}));
      }
      const host = new URL(body.url).hostname; window.checkedHosts.push(host);
      if(!['five.example','six.example'].includes(host)) return new Response(JSON.stringify({error:'源站返回 HTTP 403'}),{status:422});
      return new Response(JSON.stringify({url:body.url,bookTitle:'测试小说',sampleTitle:'第一章',characters:2000,note:'仅验证样本'}));
    };
    document.getElementById('search-form').requestSubmit();
  })()`);
  await until('!document.getElementById("search-button").disabled');
  assert.deepEqual(await evaluate('window.searchRounds'), [0, 1], 'Failed sources should trigger supplemental search');
  assert.equal(await evaluate('window.checkedHosts.length'), 6, 'Checks must continue beyond first three failures');
  assert.equal(await evaluate('document.querySelectorAll(".source-status[data-state=ready]").length'), 2);
  assert.match(await evaluate('document.getElementById("search-status").textContent'), /来自 6 个网站/);
  // A deterministic fixture isolates UI behavior; real source fetching is checked separately.
  await evaluate(`(() => {
    const original = window.fetch;
    window.fetch = async (url, opts) => {
      if (url !== '/api/read') return original(url, opts);
      const u = JSON.parse(opts.body).url;
      const data = { url:u, bookTitle:'测试小说', title:'第一章 开始', author:'', paragraphs:['<img src=x onerror=alert(1)>这是文字，不是HTML。', ...Array(40).fill('安静阅读，故事仍在继续。')], chapters:[], previous:null, next:'https://example.com/2.html', nextPage:null, catalog:'https://example.com/catalog/', catalogNext:null, warnings:[], pages:1, kind:'chapter' };
      if(u.includes('catalog')) Object.assign(data,{kind:'catalog',title:'测试小说',chapters:[{title:'第一章 开始',url:'https://example.com/1.html'}]});
      return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});
    };
    document.getElementById('url').value='https://example.com/catalog/';
    document.getElementById('import-form').requestSubmit();
  })()`);
  await until('!document.getElementById("reader").hidden && document.getElementById("busy").hidden');
  assert.equal(await evaluate('document.getElementById("catalog-dialog").open'), false, 'Unified URL entry should open chapter automatically');
  assert.equal(await evaluate('document.querySelectorAll("#content p").length'), 41);
  assert.equal(await evaluate('document.querySelectorAll("#content img").length'), 0, 'Source text must not become HTML');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'), true, 'Mobile reader should not overflow');
  await evaluate('document.getElementById("focus-button").click()');
  assert.equal(await evaluate('document.body.classList.contains("focus")'), true);
  await evaluate('document.getElementById("exit-focus").click(); document.getElementById("settings-button").click(); document.querySelector("button[data-theme=night]").click()');
  assert.equal(await evaluate('document.body.dataset.theme'), 'night');
  await evaluate('document.querySelector("#settings-dialog .close-dialog").click(); document.getElementById("back").click()');
  assert.equal(await evaluate('document.querySelectorAll(".book-card").length'), 1);
  await evaluate(`(() => {
    const original = window.fetch;
    window.fetch = async (url, opts) => {
      if(url === '/api/search') return new Response(JSON.stringify({query:'另一部小说',results:[{title:'另一部小说',url:'https://new.example/catalog/',host:'new.example',engines:['Bing']}],warnings:[],hasMore:false}));
      if(url === '/api/check') return new Response(JSON.stringify({url:'https://new.example/catalog/',bookTitle:'另一部小说',author:'作者',sampleTitle:'第一章',characters:2000,note:'样本通过'}));
      if(url === '/api/openbooks') return new Response(JSON.stringify({books:[],notice:'测试：无直接版本'}));
      return original(url, opts);
    };
    document.getElementById('url').value='另一部小说';
    document.getElementById('import-form').requestSubmit();
  })()`);
  await until('document.querySelector("#search-results .source-status[data-state=ready]") !== null && !searchBusy && !sourceChecks');
  assert.equal(await evaluate('document.getElementById("reader").hidden'), true, 'Search must wait for explicit source selection');
  await evaluate('document.querySelector("#search-results button").click()');
  await until('!document.getElementById("reader").hidden && !pending');
  assert.equal(await evaluate('document.getElementById("catalog-dialog").open'), false, 'Selected source opens without catalog confirmation');
  await evaluate('document.getElementById("back").click()');
  assert.equal(await evaluate('document.querySelectorAll(".book-card").length'), 2);
  await evaluate('location.reload()');
  await until('document.documentElement.dataset.ready === "true"');
  assert.equal(await evaluate('document.body.dataset.theme'), 'night', 'Preferences should survive reload');
  assert.equal(await evaluate('document.querySelectorAll(".book-card").length'), 2, 'Shelf should survive reload');
  // Deterministic latency/counters distinguish cache reuse from merely fast network.
  await evaluate(`(() => {
    window.readCalls = [];
    window.fetch = async (url, opts) => {
      const target = JSON.parse(opts.body).url;
      readCalls.push(target);
      await new Promise(resolve => setTimeout(resolve, 80));
      const index = Number(target.match(/(\\d+)$/)[1]);
      return new Response(JSON.stringify({kind:'chapter',url:target,bookTitle:'Cache regression',
        title:'Chapter '+index,paragraphs:Array(100).fill('A long paragraph for deterministic scrolling and restoration.'),
        warnings:[],pages:1,chapters:[],catalog:'https://cache.example/catalog',
        previous:index>1?'https://cache.example/'+(index-1):null,
        next:index<4?'https://cache.example/'+(index+1):null}));
    };
    return load('https://cache.example/1');
  })()`);
  await until('!restoringProgress');
  await until('readCalls.includes("https://cache.example/2") && pageRequests.size === 0');
  await sleep(800);
  assert.deepEqual(await evaluate('readCalls'), ['https://cache.example/1', 'https://cache.example/2'], 'Prefetch stops after one chapter');
  await evaluate('scrollTo(0, (document.documentElement.scrollHeight-innerHeight)*0.55)');
  await sleep(50);
  // Saving synchronously on navigation must not depend on the 300ms debounce.
  await evaluate('document.getElementById("back").click()');
  const savedRatio = await evaluate('state.books.find(b=>b.title==="Cache regression").progress');
  assert.ok(savedRatio > 0.5 && savedRatio < 0.6, 'Back flushes scroll progress');
  await evaluate('location.reload()');
  await until('document.documentElement.dataset.ready === "true"');
  await evaluate(`(() => {
    window.readCalls = [];
    window.fetch = async () => { throw new Error('Cached chapter should not request network'); };
    const saved = state.books.find(b=>b.title==='Cache regression');
    return load(saved.lastUrl, saved);
  })()`);
  await until('!restoringProgress');
  assert.ok(Math.abs(await evaluate('book.progress') - savedRatio) < 0.02, 'Reload restores saved ratio');
  await evaluate('document.getElementById("next").click()');
  await until('current.url === "https://cache.example/2" && !restoringProgress');
  assert.equal(await evaluate('current.fromCache'), true, 'Next chapter uses prefetch cache online');
  await evaluate('document.getElementById("previous").click()');
  await until('current.url === "https://cache.example/1" && !restoringProgress');
  await evaluate('document.getElementById("back").click()');
  const dedup = await evaluate(`(async () => {
    let calls=0;
    window.fetch=async (url,opts)=>{
      calls++; await new Promise(r=>setTimeout(r,50));
      return new Response(JSON.stringify({kind:'chapter',url:JSON.parse(opts.body).url,warnings:[],paragraphs:['ok']}));
    };
    await Promise.all([getPage('https://dedup.example/1'),getPage('https://dedup.example/1')]);
    await getPage('https://dedup.example/1');
    const beforeRefresh=calls;
    await getPage('https://dedup.example/1',true);
    return {beforeRefresh,calls};
  })()`);
  assert.deepEqual(dedup, {beforeRefresh:1,calls:2}, 'Concurrent requests merge; refresh bypasses browser cache');
  const boundaries = await evaluate(`(async () => {
    const owner=state.books.find(b=>b.title==='Cache regression');
    await load(owner.lastUrl,owner);
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
    let calls=0;
    window.fetch=async ()=>{calls++;return new Response(JSON.stringify({error:'Denied'}),{status:403});};
    const chapter=current;
    current={...chapter,next:'https://cache.example/vip',restrictedNext:'https://cache.example/vip'};
    schedulePrefetch(); await new Promise(r=>setTimeout(r,700));
    const restrictedCalls=calls;
    current={...chapter,next:'https://other.example/chapter'};
    schedulePrefetch(); await new Promise(r=>setTimeout(r,700));
    const crossSiteCalls=calls;
    current=chapter;
    await load('https://cache.example/denied',owner);
    const preserved=current===chapter && book===owner && !pending;
    const setItem=Storage.prototype.setItem;
    Storage.prototype.setItem=()=>{throw new Error('Storage full');};
    let storageSafe=true;
    try { saveProgress(); } catch { storageSafe=false; }
    finally { Storage.prototype.setItem=setItem; }
    document.getElementById('back').click();
    return {restrictedCalls,crossSiteCalls,preserved,storageSafe};
  })()`);
  assert.deepEqual(boundaries, {restrictedCalls:0,crossSiteCalls:0,preserved:true,storageSafe:true}, 'Restricted/cross-site prefetch stops; failed read preserves chapter; storage failure does not crash');
  const oldScope = await evaluate(`(async () => {
    const originalKey=DB_KEY;
    window.fetch=async ()=>{await new Promise(r=>setTimeout(r,50));return new Response(JSON.stringify({kind:'chapter',url:'https://scope.example/1',warnings:[]}));};
    const request=getPage('https://scope.example/1').then(()=>false,()=>true);
    await new Promise(r=>setTimeout(r,10)); DB_KEY='temporary-scope';
    const rejected=await request; DB_KEY=originalKey;
    return {rejected,cached:!!(await cached('https://scope.example/1'))};
  })()`);
  assert.deepEqual(oldScope, {rejected:true,cached:false}, 'Late account-scoped response must not be cached');
  console.log('PASS: external acquisition removal, manual import entry retained, one-chapter prefetch, online cache reuse, request deduplication, refresh, progress flush/reload, stale account response rejection');
  await evaluate(`(() => {
    window.testCloud = {version:0,payload:null};
    window.fetch = async (url,opts) => {
      if(url==='/api/state') {
        if(opts?.method==='POST') {
          const data=JSON.parse(opts.body);
          if(data.version!==testCloud.version) return new Response(JSON.stringify({error:'版本冲突'}),{status:409});
          testCloud.payload=data.payload;testCloud.version++;
          return new Response(JSON.stringify({ok:true,version:testCloud.version}));
        }
        return new Response(JSON.stringify(testCloud));
      }
      return new Response('{}');
    };
    document.getElementById('username').value='测试读者';
    document.getElementById('password').value='test-password-only';
    window.loginBody=null;
    const cloudFetch=window.fetch;
    window.fetch=async (url,opts)=>{
      if(url==='/api/login') {
        window.loginBody=JSON.parse(opts.body);
        return new Response(JSON.stringify({user:{id:'account-a',username:'测试读者'}}));
      }
      return cloudFetch(url,opts);
    };
    document.getElementById('login-form').requestSubmit();
  })()`);
  await until('!document.getElementById("login-submit").disabled');
  assert.deepEqual(await evaluate('loginBody'), {username:'测试读者',password:'test-password-only'}, 'Login submits username instead of email');
  assert.equal(await evaluate('document.getElementById("password").value'), '', 'Password is cleared after login');
  assert.equal(await evaluate('document.getElementById("account-name").textContent'), '测试读者', 'Account bar shows username');
  assert.equal(await evaluate('state.books.length'), 0, 'Account must not auto-inherit anonymous shelf');
  await evaluate(`state.books.push({id:'cloud-book',title:'私人书架',chapters:[{title:'大目录'}]});save();clearTimeout(syncTimer);pushState()`);
  assert.equal(await evaluate('testCloud.version'), 1);
  assert.equal(await evaluate('testCloud.payload.books[0].chapters === undefined'), true, 'Large catalogs excluded from sync');
  await evaluate('testCloud.version=2; save(); clearTimeout(syncTimer); pushState()');
  assert.equal(await evaluate('document.getElementById("conflict-dialog").open'), true, 'Conflicts must be explicit');
  await evaluate('document.getElementById("keep-cloud").click()');
  await until('!document.getElementById("conflict-dialog").open');
  await evaluate(`testCloud={version:0,payload:null};startAccount({id:'account-b',username:'reader_02'})`);
  assert.equal(await evaluate('state.books.length'), 0, 'Second account cannot see first account shelf');
  assert.equal(await evaluate('cacheName'), 'pure-novel-chapters:account-b', 'Cache namespace is account-specific');
  assert.equal(await evaluate('!!document.getElementById("ebook-file")'), true, 'Manual import available to isolated accounts');
  await evaluate(`(async () => {
    const data = await Ebooks.parse(new File(['第一章 开始\\n这是本机电子书。\\n第二章 继续\\n接着阅读。'], '测试.txt'));
    await ebookStore('readwrite', s => s.put(data));
    window.testEbook = data;
    await load('ebook:' + data.id, null, true);
  })()`);
  assert.equal(await evaluate('current.title'), '第一章 开始');
  assert.equal(await evaluate('current.next.endsWith("/1")'), true);
  await evaluate('document.getElementById("next").click()');
  await until('current.title === "第二章 继续"');
  assert.equal(await evaluate('document.querySelectorAll("#reading-warning a").length'), 0);
  await evaluate('document.getElementById("back").click()');
  const epubFixture = 'UEsDBBQAAAAIALicU13JF3Q9NwAAAE4AAAAWAAAATUVUQS1JTkYvY29udGFpbmVyLnhtbLNJzs8rSczMSy2ysynKzy9Jy8xJLUYwFdJKc3J0CxJLMmyVkvLzs/XyC9KU9O1s9JHU6iOMAABQSwMEFAAAAAgAuJxTXVyh/jp8AAAAuAAAAAgAAABib29rLm9wZkWOPQ7CMAxGrxJlReALmAxI7CwcwGpcapEEi3pob49p+Nms7z09GZWGO904YWWjTEYJTaxwOl+up2A8G0IfEP5KpSajs4RiXIPkY6QYpiePfhyWyWqJoXIW2tuq7KNqkYFMHg02vFtcgXf0l5pVGveihzzac5v1YfB99wVQSwMEFAAAAAgAuJxTXVFEMGlxAAAAggAAAAcAAABhLnhodG1sLc0xDsIwDEbhq0Q9QC1W9GMJysIEouwoBEMqGmo5HsrtQcD43vIhexkZl+n6YuQFdzmqi4X9U0CfhnIfbxJcZm9ByqjJBnXerLfnvjvuDifQf2Eo91AtrZrsrnVJJHMsOkqbpkJuMT3EGmLQj6Ov/QZQSwECFAAUAAAACAC4nFNdyRd0PTcAAABOAAAAFgAAAAAAAAAAAAAAgAEAAAAATUVUQS1JTkYvY29udGFpbmVyLnhtbFBLAQIUABQAAAAIALicU11cof46fAAAALgAAAAIAAAAAAAAAAAAAACAAWsAAABib29rLm9wZlBLAQIUABQAAAAIALicU11RRDBpcQAAAIIAAAAHAAAAAAAAAAAAAACAAQ0BAABhLnhodG1sUEsFBgAAAAADAAMArwAAAKMBAAAAAA==';
  const epubResult = await evaluate(`(async () => {
    const bytes = Uint8Array.from(atob(${JSON.stringify(epubFixture)}), c => c.charCodeAt(0));
    const parsed = await Ebooks.parse(new File([bytes], 'test.epub'));
    return {title: parsed.title, sections: parsed.sections};
  })()`);
  assert.equal(epubResult.title, 'EPUB test');
  assert.equal(epubResult.sections[0].title, 'Chapter One');
  assert.ok(epubResult.sections[0].paragraphs.includes('Safe text.'));
  assert.ok(!JSON.stringify(epubResult).includes('BAD_SCRIPT'));
  assert.ok(!JSON.stringify(epubResult).includes('tracker'));
  const damaged = await evaluate(`(async () => {
    const bytes = Uint8Array.from(atob(${JSON.stringify(epubFixture)}), c => c.charCodeAt(0));
    const v = new DataView(bytes.buffer);
    for (let i = 0; i < bytes.length - 20; i++) if (v.getUint32(i, true) === 0x02014b50) { bytes[i + 16] ^= 1; break; }
    return Ebooks.parse(new File([bytes], 'damaged.epub')).then(() => '', e => e.message);
  })()`);
  assert.ok(damaged.includes('CRC'), 'Modified checksum must be rejected');
  const utf16 = await evaluate(`(async () => {
    const text = '第一章 开始\\n正文内容。';
    const bytes = new Uint8Array(2 + text.length * 2); bytes[0] = 255; bytes[1] = 254;
    const view = new DataView(bytes.buffer);
    for (let i = 0; i < text.length; i++) view.setUint16(2 + i * 2, text.charCodeAt(i), true);
    return (await Ebooks.parse(new File([bytes], 'utf16.txt'))).sections[0].title;
  })()`);
  assert.equal(utf16, '第一章 开始', 'UTF-16 BOM should decode correctly');
  const invalid = await evaluate(`Ebooks.parse(new File(['not a zip'], 'bad.epub')).then(() => false, () => true)`);
  assert.equal(invalid, true, 'Corrupt EPUB rejected');
  // Drive the actual file input, confirmation and persistence rather than bypassing import UI.
  await evaluate('document.getElementById("back").click()');
  await evaluate(`(() => {
    window.importText='Chapter 1 Start\\n'+('A complete local reading paragraph.\\n'.repeat(80))+'Chapter II Continue\\nSecond chapter text.';
    window.chooseBook=(name='manual.txt',text=importText)=>{
      const transfer=new DataTransfer(); transfer.items.add(new File([text],name));
      const input=document.getElementById('ebook-file');input.files=transfer.files;input.dispatchEvent(new Event('change'));
    };
    chooseBook();
  })()`);
  await until('document.getElementById("ebook-confirm").open');
  assert.equal(await evaluate('document.getElementById("ebook-preview").textContent.includes("complete local")'), true);
  const beforeImport = await evaluate('state.books.length');
  await evaluate('document.getElementById("ebook-cancel").click()');
  assert.equal(await evaluate('state.books.length'), beforeImport, 'Cancel adds no shelf record');
  await evaluate('chooseBook()');
  await until('document.getElementById("ebook-confirm").open');
  await evaluate('document.getElementById("ebook-title").value="Manual edited";document.getElementById("ebook-author").value="Local author";document.getElementById("ebook-confirm-form").requestSubmit()');
  await until('!pending && current?.bookTitle === "Manual edited" && !restoringProgress');
  assert.equal(await evaluate('state.books.length'), beforeImport + 1);
  assert.equal(await evaluate('current.author'), 'Local author');
  await evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*0.6)');
  await sleep(50);
  await evaluate('document.getElementById("back").click();chooseBook("renamed.txt")');
  await until('document.getElementById("ebook-confirm").open');
  assert.equal(await evaluate('document.getElementById("ebook-accept").textContent'), '继续阅读');
  const originalProgress = await evaluate('state.books.find(b=>b.title==="Manual edited").progress');
  await evaluate('document.getElementById("ebook-confirm-form").requestSubmit()');
  await until('!pending && !restoringProgress');
  assert.equal(await evaluate('state.books.length'), beforeImport + 1, 'Identical file renamed does not duplicate');
  assert.ok(Math.abs(await evaluate('book.progress') - originalProgress) < 0.02);
  await evaluate('document.getElementById("next").click()');
  await until('current.title === "Chapter II Continue" && !restoringProgress');
  await evaluate('document.getElementById("back").click();chooseBook("manual-2.txt",importText+"Different version")');
  await until('document.getElementById("ebook-confirm").open');
  await evaluate('document.getElementById("ebook-title").value="Manual edited";document.getElementById("ebook-confirm-form").requestSubmit()');
  await until('!pending && !document.getElementById("ebook-confirm").open');
  assert.equal(await evaluate('state.books.filter(b=>b.title==="Manual edited").length'), 2, 'Same title different bytes stays separate');
  await evaluate('document.getElementById("back").click();chooseBook("failed.txt","Chapter 1 Test\\nUnsaved text")');
  await until('document.getElementById("ebook-confirm").open');
  const failureCount = await evaluate('state.books.length');
  await evaluate(`(() => {
    window.savedSetItem=Storage.prototype.setItem;
    Storage.prototype.setItem=()=>{throw new Error('Storage full');};
    document.getElementById('ebook-confirm-form').requestSubmit();
  })()`);
  await until('!pending && document.getElementById("ebook-confirm-error").textContent.length > 0');
  assert.equal(await evaluate('state.books.length'), failureCount, 'Metadata failure publishes no shelf');
  assert.equal(await evaluate('ebookStore("readonly",s=>s.get(preparedEbook.data.id)).then(Boolean)'), false, 'Failed metadata write rolls file back');
  await evaluate('Storage.prototype.setItem=savedSetItem;document.getElementById("ebook-cancel").click()');
  const errorsImport = await evaluate(`(async () => {
    const result=[];
    for(const [name,content] of [['fake.txt','<html><body>Login</body></html>'],['disguised.txt','%PDF-1.7'],['book.mobi','data'],['broken.epub','not zip'],['binary.txt',new Uint8Array([0,1,2,3])],['empty.txt',''],['large.txt',new Uint8Array(24*1024*1024+1)]]) {
      result.push(await Ebooks.parse(new File([content],name)).then(()=>false,e=>!!e.message));
    }
    const long=await Ebooks.parse(new File(['Long text '.repeat(6000)],'long.txt'));
    return {result,longSections:long.sections.length};
  })()`);
  assert.ok(errorsImport.result.every(Boolean), 'Unsupported, disguised, binary, empty and oversized files rejected');
  assert.ok(errorsImport.longSections > 1, 'Long unchaptered text is split');
  const ebookFixtures = JSON.parse(await readFile(new URL('./ebook-fixtures.json', import.meta.url), 'utf8'));
  const navigationResult = await evaluate(`(async () => {
    window.ebookFixtures=${JSON.stringify(ebookFixtures)};
    window.fixtureFile=(key,name)=>new File([Uint8Array.from(atob(ebookFixtures[key]),c=>c.charCodeAt(0))],name);
    const nav=await Ebooks.parse(fixtureFile('nav','renamed.txt'));
    const ncx=await Ebooks.parse(fixtureFile('ncx','book.epub'));
    const encrypted=await Ebooks.parse(fixtureFile('encrypted','book.epub')).then(()=>false,e=>e.message.includes('加密'));
    const gb=await Ebooks.parse(fixtureFile('gb18030','gb.txt'));
    return {format:nav.format,nav:nav.sections.map(s=>s.title),ncx:ncx.sections.map(s=>s.title),encrypted,encoding:gb.encoding,body:gb.sections[0].paragraphs.join('')};
  })()`);
  assert.deepEqual(navigationResult.nav,['Navigation One','Navigation Two']);
  assert.deepEqual(navigationResult.ncx,['NCX One','NCX Two']);
  assert.equal(navigationResult.format,'EPUB','Content signature takes priority over misleading TXT extension');
  assert.equal(navigationResult.encrypted,true);
  assert.equal(navigationResult.encoding,'gb18030');
  assert.ok(navigationResult.body.includes('中文正文测试'));
  await evaluate(`(() => {
    const transfer=new DataTransfer();transfer.items.add(fixtureFile('nav','local.epub'));
    const input=document.getElementById('ebook-file');input.files=transfer.files;input.dispatchEvent(new Event('change'));
  })()`);
  await until('document.getElementById("ebook-confirm").open');
  assert.equal(await evaluate('document.documentElement.scrollWidth <= innerWidth'),true,'Import preview fits mobile width');
  await evaluate('document.getElementById("ebook-confirm-form").requestSubmit()');
  await until('!pending && current?.title === "Navigation One" && !restoringProgress');
  await evaluate('document.getElementById("next").click()');
  await until('current.title === "Navigation Two" && !restoringProgress');
  await evaluate('document.getElementById("previous").click()');
  await until('current.title === "Navigation One" && !restoringProgress');
  await evaluate('document.getElementById("back").click()');
  const scopeImport=await evaluate(`(async()=>{
    const original=Ebooks.parse;
    Ebooks.parse=async (...args)=>{await new Promise(r=>setTimeout(r,50));return original(...args);};
    const scope=DB_KEY; chooseBook('scope.txt','Chapter 1 Scope\\nNot saved');
    DB_KEY='changed-during-parse';
    await new Promise(r=>setTimeout(r,120));
    const result=!document.getElementById('ebook-confirm').open && preparedEbook===null;
    DB_KEY=scope;Ebooks.parse=original;
    return result;
  })()`);
  assert.equal(scopeImport,true,'Changed account during parsing does not receive import preview or data');
  await evaluate('location.reload()');
  await until('document.documentElement.dataset.ready === "true"');
  // Account B data persists in its namespace and is not visible in anonymous mode.
  assert.equal(await evaluate('state.books.some(b=>b.title==="Manual edited")'), false);
  const restored = await evaluate(`(async()=>{
    DB_KEY='pure-novel-user:account-b';cacheName='pure-novel-chapters:account-b';cacheDB=openDB();
    state=JSON.parse(localStorage.getItem(DB_KEY));
    const item=state.books.find(b=>b.title==='Manual edited' && b.lastTitle==='Chapter II Continue');
    await load(item.lastUrl,item,true);return current.title;
  })()`);
  assert.equal(restored,'Chapter II Continue','Manual book and last chapter persist across reload');
  console.log('PASS: manual file input, preview/cancel/edit/confirm, duplicate progress, same-title editions, storage rollback, format rejection, long TXT splitting, account-local persistence');
  console.log('PASS: EPUB nav/NCX labels, EPUB file-input confirmation and navigation, GB18030 text, encryption rejection, parse account guard');
  console.log('PASS: mobile layout, error diagnosis, multi-source search/check states, import, catalog, chapter, safe text, immersion, settings, shelf persistence');
  console.log('PASS: cloud account isolation, cache namespaces, metadata sync, explicit version conflicts (mock API)');
} finally {
  if (socket?.readyState === WebSocket.OPEN) { await command('Browser.close').catch(() => {}); socket.close(); }
  browser.kill();
  await sleep(500);
  await rm(profile, { recursive: true, force: true, maxRetries: 3 }).catch(() => {});
}