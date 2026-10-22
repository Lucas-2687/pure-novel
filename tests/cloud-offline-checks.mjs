import assert from 'node:assert/strict';

export async function cloudOfflineChecks({ evaluate, command, until, sleep, fixtures }) {
  const modelKey = 'pure-novel-test:cloud';
  const execute = expression => evaluate(`(async()=>{${expression}})()`);
  const source = `(() => {
    const original = window.fetch;
    window.cloudCalls = [];
    window.fetch = async (url, options = {}) => {
      if (typeof url !== 'string' || !url.startsWith('/api/')) return original(url, options);
      const model = JSON.parse(localStorage.getItem('${modelKey}'));
      if (!model || !navigator.onLine) return original(url, options);
      const method = options.method || 'GET', body = options.body ? JSON.parse(options.body) : null;
      cloudCalls.push({url,method,body});
      const reply = (data, status = 200, headers = {}) => new Response(JSON.stringify(data), {status,headers});
      const failure = model[url];
      if (failure === 'network') throw new TypeError('Controlled network failure');
      if (failure === 'abort') throw new DOMException('Controlled abort', 'AbortError');
      if (failure === 'json') return new Response('<html>invalid</html>');
      if (typeof failure === 'number') return reply({error:'Controlled HTTP '+failure}, failure);
      if (url === '/api/health') {
        if (model.holdHealth) return new Promise(resolve => {window.releaseHealth = () => resolve(reply({ok:true,authRequired:true}));});
        return reply({ok:true,authRequired:true});
      }
      if (url === '/api/logout') {
        if (model.logoutFailure) return reply({error:'Logout unavailable'},503);
        model.loggedIn = false; localStorage.setItem('${modelKey}',JSON.stringify(model));
        return reply({ok:true});
      }
      if (url === '/api/login') {
        if (model.loginFailure) return reply({error:'Invalid credentials'},401);
        model.loggedIn=true;localStorage.setItem('${modelKey}',JSON.stringify(model));
        return reply({user:model.user});
      }
      if (!model.loggedIn) return reply({error:'Login required'},401);
      const remote = model.states[model.user.id] || {version:0,payload:null};
      if (url === '/api/bootstrap') {
        if (model.holdBootstrap) return new Promise(resolve => {window.releaseBootstrap = () => resolve(reply({user:model.user,...remote}));});
        if (model.badBootstrap) return reply({user:model.user,version:remote.version});
        return reply({user:model.user,...remote});
      }
      if (url === '/api/session') return reply({user:model.user});
      if (url === '/api/state' && method === 'GET') return reply(remote);
      if (url === '/api/state' && method === 'POST') {
        if (model.holdPost) return new Promise(resolve => {window.releasePost = version => resolve(reply({ok:true,version}));});
        if (model.postStatus) return reply({error:'Controlled sync failure'},model.postStatus,{'Retry-After':'4'});
        if (body.version !== remote.version) return reply({error:'Conflict'},409);
        model.states[model.user.id]={version:remote.version+1,payload:body.payload};
        localStorage.setItem('${modelKey}',JSON.stringify(model));
        return reply({ok:true,version:remote.version+1});
      }
      throw new Error('Unexpected API request: '+url);
    };
  })();`;
  const hook = await command('Page.addScriptToEvaluateOnNewDocument', { source });
  const userA = { id: 'offline-a', username: '离线甲' }, userB = { id: 'offline-b', username: '离线乙' };
  const setModel = async changes => evaluate(`(() => {const key=${JSON.stringify(modelKey)},model=JSON.parse(localStorage.getItem(key));Object.assign(model,${JSON.stringify(changes)});localStorage.setItem(key,JSON.stringify(model));})()`);
  const network = offline => command('Network.emulateNetworkConditions', { offline, latency: 0, downloadThroughput: -1, uploadThroughput: -1 });
  const reload = async () => {
    await evaluate('document.documentElement.dataset.ready="reloading"');
    await command('Page.reload');
    await until('document.documentElement.dataset.ready === "true"');
  };
  const calls = () => evaluate('cloudCalls');
  const closed = async message => assert.deepEqual(await evaluate('({home:document.getElementById("home").hidden,reader:document.getElementById("reader").hidden,login:!document.getElementById("login-screen").hidden,database:cacheDB===null})'), { home: true, reader: true, login: true, database: true }, message);
  const grantA = async () => evaluate(`rememberAccount(${JSON.stringify(userA)})`);
  const resetFailures = async () => setModel({ '/api/health': null, '/api/bootstrap': null, '/api/session': null, '/api/state': null, badBootstrap: false, holdHealth: false, holdBootstrap: false, holdPost: false, postStatus: 0, loginFailure: false, logoutFailure: false, user: userA, loggedIn: true });
  const resumeA = async () => { await resetFailures(); await reload(); await until('account?.id === "offline-a" && !localOffline'); };
  const importFile = async (name, text, encoded = false) => {
    await command('Page.bringToFront');
    await evaluate(`(() => {
      const value=${JSON.stringify(text)},transfer=new DataTransfer();
      transfer.items.add(new File([${encoded ? 'Uint8Array.from(atob(value),c=>c.charCodeAt(0))' : 'value'}],${JSON.stringify(name)}));
      const input=document.getElementById('ebook-file');input.files=transfer.files;input.dispatchEvent(new Event('change'));
    })()`);
    await until('document.getElementById("ebook-confirm").open');
    await evaluate('document.getElementById("ebook-confirm-form").requestSubmit()');
    try { await until('!pending && current && !restoringProgress && !document.getElementById("ebook-confirm").open'); }
    catch (error) { throw new Error(`${error.message}: ${JSON.stringify(await evaluate('({pending,current:current?.title,restoringProgress,hidden:document.hidden,readerHidden:document.getElementById("reader").hidden,scope:DB_KEY,dialog:document.getElementById("conflict-dialog").open,conflict:document.getElementById("conflict-message").textContent,importError:document.getElementById("ebook-confirm-error").textContent,toast:document.getElementById("toast").textContent,login:document.getElementById("login-error").textContent})'))}`); }
    const result = await evaluate('({id:book.id,url:book.lastUrl,title:book.title})');
    await evaluate('document.getElementById("back").click();clearTimeout(syncTimer);syncTimer=null');
    return result;
  };
  let otherTarget;
  try {
    await command('Network.enable');
    await network(false);
    await evaluate(`localStorage.removeItem(OFFLINE_KEY);localStorage.removeItem(LOGOUT_KEY);serviceMode('cloud');localStorage.setItem(${JSON.stringify(modelKey)},JSON.stringify({loggedIn:true,user:${JSON.stringify(userA)},states:{}}))`);
    await reload();
    await until('account?.id === "offline-a"');
    assert.deepEqual((await calls()).map(c => c.url), ['/api/health', '/api/bootstrap'], 'Bootstrap combines identity and state without duplicate session/state GETs');
    const grant = await evaluate('JSON.parse(localStorage.getItem(OFFLINE_KEY))');
    assert.deepEqual(Object.keys(grant).sort(), ['checkedAt', 'expiresAt', 'id', 'username']);
    assert.equal(grant.expiresAt - grant.checkedAt, 7 * 86400000);
    assert.equal(await evaluate('document.cookie.includes("session")'), false, 'No session token is exposed by frontend storage');

    const txt = await importFile('offline.txt', 'Chapter 1 Start\n' + 'A long local paragraph for reliable progress.\n'.repeat(160) + 'Chapter 2 Continue\nSecond chapter.');
    const epub = await importFile('offline.epub', fixtures.nav, true);
    await evaluate(`(async () => {
      await cachePage('https://offline-check.example/1',{kind:'chapter',url:'https://offline-check.example/1',title:'Cached web chapter',bookTitle:'Cached web book',paragraphs:Array(80).fill('Cached on this device.'),warnings:[],pages:1,chapters:[],next:'https://offline-check.example/2',previous:null,catalog:'https://offline-check.example/catalog'});
      await ReaderStorage.putCatalog(cacheDB,'offline-web',[{title:'Cached web chapter',url:'https://offline-check.example/1'}],'https://offline-check.example/catalog');
      state.books.push({id:'offline-web',title:'Cached web book',catalog:'https://offline-check.example/catalog',lastUrl:'https://offline-check.example/1',progress:0.42,chapters:[]});
      save();clearTimeout(syncTimer);await pushState();
    })()`);
    await until('!syncing && !dirty');
    const postsBefore = (await calls()).filter(c => c.method === 'POST').length;
    await execute('state.schema=2;state.books[0].schema=2;state.books[0].chapters.push({title:"Local catalog only",url:"ebook:local"});save();save();await pushState()');
    assert.equal((await calls()).filter(c => c.method === 'POST').length, postsBefore, 'Catalog/schema-only and identical saves never POST');
    assert.equal(await evaluate('dirty || syncTimer !== null'), false, 'No metadata change means no dirty marker or timer');
    await execute('state.settings.size=23;save();clearTimeout(syncTimer);await pushState();save();await pushState()');
    assert.equal((await calls()).filter(c => c.method === 'POST').length, postsBefore + 1, 'One actual metadata change posts once');
    const noOp = await execute(`
      const original=window.fetch,version=cloudVersion;
      window.fetch=async(url,options)=>{
        if(url==='/api/state'&&options?.method==='POST'){
          const body=JSON.parse(options.body),key=${JSON.stringify(modelKey)},model=JSON.parse(localStorage.getItem(key));
          model.states[model.user.id]={version:body.version,payload:body.payload};localStorage.setItem(key,JSON.stringify(model));
          return new Response(JSON.stringify({ok:true,version:body.version}),{headers:{'Content-Type':'application/json'}});
        }
        return original(url,options);
      };
      try{state.settings.size=22;save();clearTimeout(syncTimer);await pushState();return {clean:!dirty,versionUnchanged:cloudVersion===version,retries:syncRetryCount};}
      finally{window.fetch=original;}
    `);
    assert.deepEqual(noOp,{clean:true,versionUnchanged:true,retries:0},'A successful idempotent save may retain its version and must clear dirtiness');

    await evaluate('navigator.serviceWorker.ready');
    await until('!!navigator.serviceWorker.controller');
    await network(true);
    await reload();
    assert.deepEqual(await evaluate('({id:account.id,key:DB_KEY,offline:localOffline,banner:!document.getElementById("offline-banner").hidden})'), { id: userA.id, key: 'pure-novel-user:offline-a', offline: true, banner: true });
    assert.match(await evaluate('document.getElementById("offline-message").textContent'), /离线甲.*7 天.*未同步/);
    assert.equal(await execute(`return (await localEbookPage(${JSON.stringify(txt.url)})).title`), 'Chapter 1 Start');
    assert.equal(await execute(`return (await localEbookPage(${JSON.stringify(epub.url)})).title`), 'Navigation One');
    await execute(`await load(${JSON.stringify(epub.url)},state.books.find(b=>b.id===${JSON.stringify(epub.id)}));document.getElementById('next').click()`);
    await until('current?.title === "Navigation Two" && !restoringProgress');
    await evaluate('document.getElementById("back").click()');
    assert.equal(await evaluate('state.books.find(b=>b.id==="offline-web").progress'), 0.42);
    assert.equal(await execute('return (await getPage("https://offline-check.example/1")).offline'), true);
    const offlineImport = await importFile('offline-new.txt', 'Chapter 1 Offline import\nOnly local bytes.');
    assert.ok(offlineImport.id);
    const offlineRequests = (await calls()).length;
    await evaluate(`(async()=>{try{await searchAPI('/api/search',{query:'test'});}catch{}try{await getPage('https://offline-check.example/missing');}catch{}await pushState();schedulePrefetch();})()`);
    await sleep(650);
    assert.equal((await calls()).length, offlineRequests, 'Offline reading/import/save/search/prefetch never issue new fetches');
    assert.equal(await evaluate('dirty && syncTimer === null && prefetchTimer === null'), true);
    await execute(`await load(${JSON.stringify(txt.url)},state.books.find(b=>b.id===${JSON.stringify(txt.id)}))`);
    await until('!restoringProgress');
    await evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*0.57);cloudCalls=[]');
    await network(false);
    await until('account?.id === "offline-a" && !localOffline && !authChecking && !dirty && !syncing');
    const reconnectCalls = await calls();
    assert.equal(reconnectCalls[0].url, '/api/bootstrap', 'Reconnect verifies identity before syncing');
    assert.deepEqual(reconnectCalls.filter(c => c.method === 'GET').map(c => c.url), ['/api/bootstrap']);
    const synced = reconnectCalls.find(c => c.url === '/api/state' && c.method === 'POST');
    assert.ok(synced, 'Offline metadata must sync after identity verification');
    const progress = synced.body.payload.books.find(b => b.id === txt.id).progress;
    assert.ok(progress > 0.54 && progress < 0.6, 'Reconnect flushes the currently open chapter before sending metadata');
    assert.equal(await evaluate('current===null && document.getElementById("reader").hidden'), true);
    assert.equal(synced.body.payload.books.find(b => b.id === epub.id).lastUrl.endsWith('/1'), true, 'EPUB navigation survives offline and sync');

    await network(true);
    await evaluate('state.settings.size=24;save();cloudCalls=[]');
    await evaluate(`(()=>{const key=${JSON.stringify(modelKey)},m=JSON.parse(localStorage.getItem(key));m.states[m.user.id].version++;m.states[m.user.id].payload.settings.size=18;localStorage.setItem(key,JSON.stringify(m));})()`);
    await network(false);
    await until('document.getElementById("conflict-dialog").open && !authChecking');
    assert.equal(await evaluate('state.settings.size'), 24, 'A newer remote version cannot overwrite dirty offline metadata');
    assert.equal((await calls()).filter(c => c.method === 'POST').length, 0, 'Reconnect conflict stops POST until a version is selected');
    await evaluate('document.getElementById("keep-local").click()');
    await until('!dirty && !syncing && !authChecking && !document.getElementById("conflict-dialog").open');
    assert.equal(await evaluate('state.settings.size'), 24);

    await network(true);
    await evaluate('state.settings.size=25;save();cloudCalls=[]');
    await setModel({ user: userB });
    await network(false);
    await until('account?.id === "offline-b" && !authChecking');
    assert.equal(await evaluate('state.books.length'), 0, 'A different online account opens only its own namespace');
    assert.equal((await calls()).filter(c => c.method === 'POST').length, 0, 'Old offline account progress is never posted into a newly authenticated account');
    assert.equal(await evaluate('localStorage.getItem("pure-novel-user:offline-a:dirty")'), '1');
    assert.equal(await evaluate(`localEbookPage(${JSON.stringify(txt.url)}).then(()=>false,()=>true)`), true, 'Another account cannot read private ebook cache');

    await evaluate('state.books.push({id:"late-b",title:"B private",progress:0.5,chapters:[]});save();clearTimeout(syncTimer)');
    await setModel({ holdPost: true });
    await evaluate('void pushState()');
    await until('typeof releasePost === "function"');
    const bVersion = await evaluate('localStorage.getItem("pure-novel-user:offline-b:version")');
    await setModel({ user: userA, holdPost: false });
    await execute('await reconnectLocal(true)');
    await until('account?.id === "offline-a" && !dirty && !authChecking');
    const aVersion = await evaluate('localStorage.getItem("pure-novel-user:offline-a:version")');
    await evaluate('releasePost(777)');
    await sleep(50);
    assert.equal(await evaluate('localStorage.getItem("pure-novel-user:offline-a:version")'), aVersion, 'Late POST cannot write the new account version key');
    assert.equal(await evaluate('localStorage.getItem("pure-novel-user:offline-b:version")'), bVersion, 'Stale generations cannot commit an old account version either');
    assert.equal(await evaluate('localStorage.getItem("pure-novel-user:offline-b:dirty")'), '1');

    await setModel({ holdPost: true });
    await evaluate('releasePost=null;state.settings.size=26;save();clearTimeout(syncTimer);void pushState()');
    await until('typeof releasePost === "function"');
    await evaluate('state.settings.size=27;save();releasePost(cloudVersion+1)');
    await until('!syncing');
    assert.equal(await evaluate('dirty'), true, 'A changed payload remains dirty after an earlier snapshot succeeds');
    assert.equal((await calls()).filter(c => c.method === 'POST').at(-1).body.payload.settings.size, 26, 'Submitted payload is immutable while in flight');
    await evaluate('clearTimeout(syncTimer);syncTimer=null');
    await setModel({ holdPost: false });
    // The held mock response represents a committed version without publishing the later local edit.
    await evaluate(`(()=>{const k=${JSON.stringify(modelKey)},m=JSON.parse(localStorage.getItem(k));m.states[m.user.id].version=cloudVersion;localStorage.setItem(k,JSON.stringify(m));})()`);
    await execute('await pushState()');
    await until('!dirty');
    await setModel({ holdPost: true });
    await evaluate('window.beforeRevert=state.settings.size;releasePost=null;state.settings.size=25;save();clearTimeout(syncTimer);void pushState()');
    await until('typeof releasePost === "function"');
    await evaluate('state.settings.size=beforeRevert;save();releasePost(cloudVersion+1)');
    await until('!syncing');
    assert.equal(await evaluate('dirty'), true, 'Reverting to the old server snapshot while a different payload is in flight still needs synchronization');
    await evaluate('clearTimeout(syncTimer);syncTimer=null');
    await setModel({ holdPost: false });
    await evaluate(`(()=>{const k=${JSON.stringify(modelKey)},m=JSON.parse(localStorage.getItem(k));m.states[m.user.id].version=cloudVersion;localStorage.setItem(k,JSON.stringify(m));})()`);
    await execute('await pushState()');
    await until('!dirty');

    await setModel({ postStatus: 429 });
    await evaluate(`(() => {
      window.retryDelays=[];window.originalTimer=window.setTimeout;
      window.setTimeout=(fn,delay,...args)=>{if(String(fn).includes('pushState()')&&delay>=3000&&delay<=60000){retryDelays.push(delay);return originalTimer(fn,15,...args);}return originalTimer(fn,delay,...args);};
      cloudCalls=[];state.settings.size=28;save();clearTimeout(syncTimer);void pushState();
    })()`);
    await until('syncRetryCount===MAX_SYNC_RETRIES && !syncing');
    assert.equal((await calls()).filter(c => c.method === 'POST').length, 5, 'Retry attempts are bounded');
    assert.deepEqual(await evaluate('retryDelays'), [4000, 6000, 12000, 24000], '429 Retry-After and exponential backoff both apply');
    assert.equal(await evaluate('dirty && syncTimer===null'), true);
    await evaluate('window.setTimeout=originalTimer');
    await setModel({ postStatus: 0 });
    await evaluate('document.getElementById("sync-now").click()');
    await until('!dirty && !syncing');
    console.log('PASS: bootstrap request reduction, seven-day grants, EPUB/TXT/cache offline progress, zero-change writes, reconnect ordering/conflict, account isolation, late snapshots and bounded 429 retry');

    for (const status of [401, 403, 503, 'json', 'shape']) {
      await resetFailures(); await grantA();
      const before = await evaluate('localStorage.getItem(OFFLINE_KEY)');
      await setModel(status === 'shape' ? { badBootstrap: true } : { '/api/bootstrap': status });
      await reload(); await closed(`Bootstrap ${status} must fail closed`);
      assert.equal((await calls()).some(c => c.url === '/api/session'), false, 'Bootstrap errors other than 404/405 never downgrade authentication');
      if ([401, 403].includes(status)) assert.equal(await evaluate('localStorage.getItem(OFFLINE_KEY)'), null, 'Explicit denial revokes offline access');
      else assert.equal(await evaluate('localStorage.getItem(OFFLINE_KEY)'), before, 'Invalid/bootstrap service failures never extend qualification');
      assert.ok(await evaluate('localStorage.getItem("pure-novel-user:offline-a")'), 'Locked shelf stays available for a later valid login');
    }
    await resetFailures(); await grantA(); await setModel({ '/api/health': 'network' }); await reload();
    assert.equal(await evaluate('localOffline && account.id==="offline-a"'), true, 'Network health failure permits only a trusted account');
    await network(true);
    await evaluate('localStorage.removeItem(OFFLINE_KEY)'); await reload(); await closed('Unknown offline account stays locked');
    await grantA();
    await evaluate('(()=>{const g=JSON.parse(localStorage.getItem(OFFLINE_KEY));g.checkedAt=Date.now()-OFFLINE_LIFETIME-1000;g.expiresAt=g.checkedAt+OFFLINE_LIFETIME;localStorage.setItem(OFFLINE_KEY,JSON.stringify(g));})()');
    await reload(); await closed('Expired account cannot resume offline');
    await network(false);
    for (const failure of ['network', 'abort']) {
      await resetFailures(); await grantA(); await setModel({ '/api/bootstrap': failure }); await reload();
      assert.equal(await evaluate('localOffline && account.id==="offline-a"'), true, `Healthy service plus bootstrap ${failure} uses trusted offline shelf`);
    }
    for (const legacyStatus of [404, 405]) {
      await resetFailures(); await setModel({ '/api/bootstrap': legacyStatus }); await reload();
      assert.equal(await evaluate('account.id'), userA.id);
      assert.deepEqual((await calls()).filter(c => c.method === 'GET').map(c => c.url), ['/api/health', '/api/bootstrap', '/api/session', '/api/state'], 'Only 404/405 enable legacy session/state compatibility');
    }
    for (const path of ['/api/session', '/api/state']) {
      await resetFailures(); await grantA(); await setModel({ '/api/bootstrap': 404, [path]: 'network' }); await reload();
      assert.equal(await evaluate('localOffline && account.id==="offline-a"'), true, `Healthy service plus ${path} network failure permits trusted offline access`);
      await resetFailures(); await grantA(); await setModel({ '/api/bootstrap': 404, [path]: 503 }); await reload();
      await closed(`${path} HTTP 503 cannot unlock offline content`);
    }

    await resumeA();
    await setModel({ loginFailure: true });
    await evaluate('lockAccount("Test re-login");document.getElementById("username").value="离线甲";document.getElementById("password").value="incorrect-test-password"');
    const loginGrant = await evaluate('localStorage.getItem(OFFLINE_KEY)');
    await evaluate('document.getElementById("login-form").requestSubmit()');
    await until('!document.getElementById("login-submit").disabled');
    assert.equal(await evaluate('localStorage.getItem(OFFLINE_KEY)'), loginGrant, 'Failed login cannot grant or extend offline access');
    await evaluate('revokeOffline();document.getElementById("password").value="incorrect-test-password";document.getElementById("login-form").requestSubmit()');
    await until('!document.getElementById("login-submit").disabled');
    assert.equal(await evaluate('localStorage.getItem(OFFLINE_KEY)'), null, 'Failed login never creates a new grant');

    for (const action of ['operation', 'visibility', 'timer']) {
      await resumeA(); await network(true);
      await evaluate(`(()=>{const g=JSON.parse(localStorage.getItem(OFFLINE_KEY));g.checkedAt=Date.now()-OFFLINE_LIFETIME+${action === 'timer' ? 150 : 10000};g.expiresAt=g.checkedAt+OFFLINE_LIFETIME;localStorage.setItem(OFFLINE_KEY,JSON.stringify(g));${action === 'timer' ? 'refreshExpiryTimer();' : 'g.checkedAt-=20000;g.expiresAt-=20000;localStorage.setItem(OFFLINE_KEY,JSON.stringify(g));'}})()`);
      if (action === 'operation') await evaluate('document.querySelector("#shelf .book-open").click()');
      if (action === 'visibility') await evaluate('document.dispatchEvent(new Event("visibilitychange"))');
      await until('account===null && cacheDB===null'); await closed(`Long-open offline ${action} must enforce expiry`);
      assert.ok(await evaluate('localStorage.getItem("pure-novel-user:offline-a")'));
      await network(false);
    }
    for (const status of [401, 403]) {
      await resumeA(); await setModel({ postStatus: status });
      await execute('state.settings.size=state.settings.size===29?30:29;save();clearTimeout(syncTimer);await pushState()');
      await closed(`Sync ${status} must expose a login entry without deleting dirty data`);
      assert.equal(await evaluate('localStorage.getItem(OFFLINE_KEY)'), null);
      assert.equal(await evaluate('localStorage.getItem("pure-novel-user:offline-a:dirty")'), '1');
      assert.equal(await evaluate('syncTimer'), null);
      await resetFailures();
      await evaluate('document.getElementById("username").value="离线甲";document.getElementById("password").value="test-password";document.getElementById("login-form").requestSubmit()');
      await until('account?.id==="offline-a" && !dirty && !document.getElementById("login-submit").disabled');
    }

    await network(true);
    await evaluate('window.confirm=()=>true;cloudCalls=[];document.getElementById("logout").click()');
    await until('account===null && localStorage.getItem("pure-novel-user:offline-a")===null');
    assert.equal(await evaluate('localStorage.getItem(OFFLINE_KEY)'), null);
    assert.ok(await evaluate('localStorage.getItem(LOGOUT_KEY)'), 'Offline logout persists pending cookie revocation');
    assert.ok(await evaluate('localStorage.getItem("pure-novel-user:offline-b")'), 'Logout removes only this account');
    assert.equal((await calls()).length, 0, 'Offline logout does not attempt a network request');
    await reload(); await closed('Pending offline logout prevents automatic cookie session recovery');
    await setModel({ logoutFailure: true });
    await network(false);
    await until('!reconnecting && !authChecking');
    assert.deepEqual((await calls()).map(c => c.url), ['/api/logout'], 'A failed pending logout blocks every automatic session/bootstrap request');
    await setModel({ logoutFailure: false });
    await execute('cloudCalls=[];await reconnectLocal(true)');
    assert.deepEqual((await calls()).map(c => c.url), ['/api/logout', '/api/bootstrap'], 'Server logout succeeds before any automatic session recovery');
    await closed('Revoked cookie cannot automatically sign the offline-logged-out user back in');
    assert.equal(await evaluate('localStorage.getItem(LOGOUT_KEY)'), null);

    await resumeA();
    const location = await evaluate('location.href');
    let otherSession;
    const otherEvaluate = async expression => {
      const result = await command('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, otherSession);
      if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
      return result.result.value;
    };
    const otherUntil = async expression => {
      for (let i = 0; i < 80; i++) { if (await otherEvaluate(expression)) return; await sleep(100); }
      throw new Error(`Second tab condition timed out: ${expression}`);
    };
    const otherNetwork = offline => command('Network.emulateNetworkConditions', { offline, latency: 0, downloadThroughput: -1, uploadThroughput: -1 }, otherSession);
    const reloadOther = async () => {
      await otherEvaluate('document.documentElement.dataset.ready="reloading"');
      await command('Page.reload', {}, otherSession);
      await otherUntil('document.documentElement.dataset.ready==="true"');
    };
    const openOther = async () => {
      otherTarget = (await command('Target.createTarget', { url: 'about:blank' })).targetId;
      otherSession = (await command('Target.attachToTarget', { targetId: otherTarget, flatten: true })).sessionId;
      await command('Page.enable', {}, otherSession); await command('Network.enable', {}, otherSession);
      await command('Page.addScriptToEvaluateOnNewDocument', { source }, otherSession);
      await command('Page.navigate', { url: location }, otherSession);
      await otherUntil('document.documentElement.dataset.ready==="true"');
      assert.equal(await otherEvaluate('account?.id'), userA.id);
    };
    await openOther();
    await execute(`state.books=[{id:'shared-offline-book',title:'Shared offline progress',catalog:'https://shared.example/catalog',lastUrl:'https://shared.example/1',progress:0.1,chapters:[]}];save();clearTimeout(syncTimer);await pushState()`);
    await reloadOther();
    await network(true); await otherNetwork(true);
    assert.equal(await evaluate('dirty'), false);
    await evaluate('cloudCalls=[]');
    await otherEvaluate('state.books[0].progress=0.75;save();clearTimeout(syncTimer)');
    assert.equal(await evaluate('state.books[0].progress'), 0.1, 'The surviving tab really has stale in-memory metadata');
    assert.equal(await evaluate('JSON.parse(localStorage.getItem(DB_KEY)).books[0].progress'), 0.75, 'The other real target writes the shared localStorage shelf');
    // Closing a CDP target can reset its offline emulation before teardown completes.
    await otherEvaluate('window.fetch=async()=>{throw new TypeError("Writer stays offline through shutdown")}');
    await command('Target.closeTarget', { targetId: otherTarget }); otherTarget = null;
    await network(false);
    await until('account?.id==="offline-a" && !localOffline && !dirty && !authChecking && !syncing');
    assert.equal(await evaluate('state.books[0].progress'), 0.75, 'Reconnect reloads a dirty shelf saved by a now-closed tab');
    assert.equal(await evaluate('JSON.parse(localStorage.getItem(DB_KEY)).books[0].progress'), 0.75);
    const sharedPost = (await calls()).find(c => c.method === 'POST');
    assert.ok(sharedPost, `Shared shelf must be posted: ${JSON.stringify({calls:await calls(),current:await evaluate('({dirty,localOffline,version:cloudVersion,synced:syncedSnapshot})')})}`);
    assert.equal(sharedPost.body.payload.books[0].progress, 0.75, 'The actual POST carries shared progress rather than the old cloud 10%');
    assert.equal(await evaluate(`JSON.parse(localStorage.getItem(${JSON.stringify(modelKey)})).states['offline-a'].payload.books[0].progress`), 0.75);

    await openOther(); await network(true); await otherNetwork(true);
    await otherEvaluate('state.books[0].progress=0.65;save();clearTimeout(syncTimer)');
    await evaluate('window.sharedSize=state.settings.size===22?23:22;state.settings.size=sharedSize;cloudCalls=[]');
    await network(false);
    await until('!localOffline && !dirty && !authChecking && !syncing');
    const combined = (await calls()).find(c => c.method === 'POST').body.payload;
    assert.equal(combined.books[0].progress, 0.65, 'Another tab progress survives an unsaved local settings change');
    assert.equal(combined.settings.size, await evaluate('sharedSize'), 'Disjoint in-memory edits are merged into the submitted shared shelf');
    await otherNetwork(false); await otherUntil('!localOffline && !dirty && !authChecking && !syncing');

    await network(true); await otherNetwork(true);
    await otherEvaluate('state.books[0].progress=0.8;save();clearTimeout(syncTimer)');
    await evaluate('state.books[0].progress=0.55;cloudCalls=[]'); await network(false);
    await until('document.getElementById("conflict-dialog").open && !authChecking');
    assert.match(await evaluate('document.getElementById("conflict-title").textContent'), /其他标签页/);
    assert.equal(await evaluate('JSON.parse(localStorage.getItem(DB_KEY)).books[0].progress'), 0.8, 'Overlapping local edits cannot overwrite the other tab persistent copy');
    assert.equal(await evaluate('state.books[0].progress'), 0.55, 'The current tab conflicting edit stays available');
    const backup = await evaluate('JSON.parse(localStorage.getItem(DB_KEY+":tab-conflict:"+tabId))');
    assert.equal(backup.local.books[0].progress, 0.55); assert.equal(backup.stored.books[0].progress, 0.8);
    assert.equal((await calls()).some(c => c.method === 'POST'), false, 'A local cross-tab conflict requires explicit selection');
    await reload();
    assert.equal(await evaluate('state.books[0].progress'), 0.55, 'A reload recovers the unsaved conflicting tab copy');
    assert.equal(await evaluate('JSON.parse(localStorage.getItem(DB_KEY)).books[0].progress'), 0.8);
    assert.equal(await evaluate('document.getElementById("conflict-dialog").open'), true);
    await evaluate('document.getElementById("keep-cloud").click()');
    await until('!dirty && !syncing && !document.getElementById("conflict-dialog").open');
    assert.equal(await evaluate('state.books[0].progress'), 0.8);
    assert.equal(await evaluate('localStorage.getItem(DB_KEY+":tab-conflict:"+tabId)'), null, 'The backup is retired only after explicit conflict resolution');
    await otherNetwork(false); await otherUntil('!localOffline && !dirty && !authChecking && !syncing');

    await network(true); await otherNetwork(true);
    await otherEvaluate('state.books[0].progress=0.9;save();clearTimeout(syncTimer)');
    await evaluate(`(()=>{const k=${JSON.stringify(modelKey)},m=JSON.parse(localStorage.getItem(k));m.states['offline-a'].version++;m.states['offline-a'].payload.books[0].progress=0.2;localStorage.setItem(k,JSON.stringify(m));cloudCalls=[];})()`);
    await network(false);
    await until('document.getElementById("conflict-dialog").open && !authChecking');
    assert.equal(await evaluate('state.books[0].progress'), 0.9, 'The shared local version is retained when remote version also changed');
    assert.equal(await evaluate('JSON.parse(localStorage.getItem(DB_KEY)).books[0].progress'), 0.9);
    assert.equal((await calls()).some(c => c.method === 'POST'), false);
    await evaluate('document.getElementById("keep-local").click()');
    await until('!dirty && !syncing && !authChecking && !document.getElementById("conflict-dialog").open');
    await otherNetwork(false); await otherUntil('!localOffline && !dirty && !authChecking && !syncing');
    console.log('PASS: closed-tab 75% shared progress, unsaved edit merge, persistent cross-tab conflict copies and remote version conflict');

    await command('Page.bringToFront');
    const reconnectBook = await importFile('race-reader.txt', 'Chapter 1 Race\n' + 'Offline reconnect reading content.\n'.repeat(160));
    await execute('await pushState()'); await until('!dirty && !syncing');
    await network(true);
    await execute(`await load(${JSON.stringify(reconnectBook.url)},state.books.find(b=>b.id===${JSON.stringify(reconnectBook.id)}))`);
    await until('!restoringProgress && !document.getElementById("reader").hidden');
    await setModel({ '/api/bootstrap': 'network' });
    await evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*0.61)'); await network(false);
    await until('localOffline && !authChecking && !reconnecting');
    assert.deepEqual(await evaluate('({home:!document.getElementById("home").hidden,reader:document.getElementById("reader").hidden,current:current===null})'), {home:true,reader:true,current:true}, 'A failed reconnect from the reader explicitly returns to a visible shelf');
    const failedReconnectProgress = await evaluate(`state.books.find(b=>b.id===${JSON.stringify(reconnectBook.id)}).progress`);
    assert.ok(failedReconnectProgress > 0.58 && failedReconnectProgress < 0.64);
    await evaluate(`document.querySelectorAll('#shelf .book-open')[state.books.findIndex(b=>b.id===${JSON.stringify(reconnectBook.id)})].click()`);
    await until('current?.title==="Chapter 1 Race" && !restoringProgress');
    assert.match(await evaluate('document.getElementById("content").textContent'), /Offline reconnect reading content/);
    assert.ok(Math.abs(await evaluate('book.progress') - failedReconnectProgress) < 0.02, 'The visible offline shelf can reopen the actual cached book at the flushed progress');
    await evaluate('document.getElementById("back").click()'); await resetFailures();
    await execute('await reconnectLocal(true)'); await until('!localOffline && !dirty && !syncing && !authChecking');
    console.log('PASS: failed reader reconnect restores a usable offline shelf and cached progress');

    for (const held of ['Health', 'Bootstrap']) {
      await reloadOther(); await network(true);
      await setModel({ holdHealth: held === 'Health', holdBootstrap: held === 'Bootstrap' });
      await otherEvaluate('document.documentElement.dataset.ready="reloading"');
      await command('Page.reload', {}, otherSession);
      await otherUntil(`typeof release${held} === 'function' && account===null`);
      await otherEvaluate('window.logoutEvents=0;addEventListener("storage",e=>{if(e.key==="pure-novel:pending-logout"||e.key==="pure-novel:offline-account")logoutEvents++;})');
      await evaluate('window.confirm=()=>true;document.getElementById("logout").click()');
      await until('account===null && localStorage.getItem(LOGOUT_KEY)!==null');
      await otherUntil('logoutEvents>0');
      await otherEvaluate(`release${held}()`);
      await otherUntil('document.documentElement.dataset.ready==="true"');
      assert.deepEqual(await otherEvaluate('({account:account===null,home:document.getElementById("home").hidden,reader:document.getElementById("reader").hidden,login:!document.getElementById("login-screen").hidden,grant:localStorage.getItem(OFFLINE_KEY),pending:!!localStorage.getItem(LOGOUT_KEY),database:cacheDB===null})'), {account:true,home:true,reader:true,login:true,grant:null,pending:true,database:true}, `Logout while account-less ${held.toLowerCase()} is pending cannot reopen identity, cache or shelf`);
      const beforeRetry = await otherEvaluate('cloudCalls.map(c=>c.url)');
      assert.deepEqual(beforeRetry, held === 'Health' ? ['/api/health'] : ['/api/health','/api/bootstrap'], 'A stale authentication completion makes no further recovery requests');
      await otherEvaluate('void reconnectLocal(true)'); await otherUntil('!reconnecting && !authChecking');
      assert.deepEqual((await otherEvaluate('cloudCalls.map(c=>c.url)')).slice(beforeRetry.length), ['/api/logout','/api/bootstrap'], 'A fresh retry revokes the cookie before inspecting any session');
      assert.equal(await otherEvaluate('account===null && document.getElementById("home").hidden && localStorage.getItem(OFFLINE_KEY)===null'), true);
      assert.equal(await otherEvaluate(`JSON.parse(localStorage.getItem(${JSON.stringify(modelKey)})).loggedIn`), false, 'The controlled server session is actually revoked');
      await network(false); await resumeA();
    }
    await reloadOther();
    console.log('PASS: real storage logout events cancel account-less held health/bootstrap, including late successful identity responses and cookie-first retry');
    await otherEvaluate('localStorage.removeItem(OFFLINE_KEY)');
    await until('account===null'); await closed('Revocation in a real second tab immediately locks this account');
    await resumeA();
    await otherEvaluate('localStorage.setItem("pure-novel:pending-logout",String(Date.now()))');
    await until('account===null'); await closed('Pending logout from another tab invalidates the active session');
    console.log('PASS: fail-closed 401/403/5xx/invalid JSON/unknown/expiry, legacy fallback, session/state network failures, failed login, long-open expiry, offline logout ordering and cross-tab revocation');
  } finally {
    if (otherTarget) await command('Target.closeTarget', { targetId: otherTarget }).catch(() => {});
    await command('Page.removeScriptToEvaluateOnNewDocument', { identifier: hook.identifier });
    await network(false);
    await evaluate(`localStorage.removeItem(${JSON.stringify(modelKey)});localStorage.removeItem(LOGOUT_KEY);revokeOffline()`).catch(() => {});
    await reload();
  }
}
