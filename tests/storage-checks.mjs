import assert from 'node:assert/strict';

export async function storageChecks({ evaluate, command, until }) {
  await command('Page.enable');
  const reload = async () => {
    await evaluate('document.documentElement.dataset.ready = "reloading"');
    await command('Page.reload');
    await until('document.documentElement.dataset.ready === "true"');
  };
  await reload();
  const catalogs = await evaluate(`(async () => {
    const key='storage-regression-shelf', database=await ReaderStorage.open('storage-regression-catalogs');
    window.storageFixture={key,database};
    const makeBook=b=>({id:'book-'+b,title:'Synthetic '+b,catalog:'https://example.com/books/'+b+'/',lastUrl:'https://example.com/books/'+b+'/1.html',progress:0.61,chapters:Array.from({length:3000},(_,i)=>({title:'Chapter '+i+' '+'sample'.repeat(10),url:'https://example.com/books/'+b+'/'+i+'.html'}))});
    const old={books:Array.from({length:8},(_,b)=>makeBook(b)),settings:{size:24}};
    localStorage.setItem(key,JSON.stringify(old));
    const migrated=await ReaderStorage.loadShelf(key,database);
    for(let b=8;b<16;b++) {
      const item=makeBook(b);await ReaderStorage.putCatalog(database,item.id,item.chapters,item.catalog);
      migrated.books.push(item);
    }
    ReaderStorage.saveShelf(key,migrated);
    const saved=localStorage.getItem(key), restored=await ReaderStorage.loadShelf(key,database);
    const counts=[];
    for(const item of restored.books) counts.push((await ReaderStorage.catalog(database,item.id)).chapters.length);
    return {books:restored.books.length,size:saved.length,hasEmbedded:JSON.parse(saved).books.some(b=>'chapters' in b),counts,progress:restored.books[0].progress,setting:restored.settings.size};
  })()`);
  assert.equal(catalogs.books, 16);
  assert.ok(catalogs.size < 20000, 'Shelf metadata must not grow with 48,000 catalog entries');
  assert.equal(catalogs.hasEmbedded, false);
  assert.deepEqual(catalogs.counts, Array(16).fill(3000));
  assert.equal(catalogs.progress, 0.61);
  assert.equal(catalogs.setting, 24);

  const migrationFailure = await evaluate(`(async () => {
    const {database}=storageFixture,key='storage-regression-failed';
    const original=JSON.stringify({books:[{id:'failed',title:'Legacy',progress:0.4,chapters:[{title:'One',url:'https://example.com/1'}]}],settings:{}});
    localStorage.setItem(key,original);
    const put=IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put=function(...args) {if(this.name==='catalogs')throw new DOMException('full','QuotaExceededError');return put.apply(this,args);};
    let fallback;
    try {fallback=await ReaderStorage.loadShelf(key,database);} finally {IDBObjectStore.prototype.put=put;}
    const retained=localStorage.getItem(key)===original;
    fallback.books[0].progress=0.7;ReaderStorage.saveShelf(key,fallback);
    const stillHasCatalog=JSON.parse(localStorage.getItem(key)).books[0].chapters.length===1;
    const set=Storage.prototype.setItem;
    Storage.prototype.setItem=function(k,v){if(k===key)throw new DOMException('full','QuotaExceededError');return set.call(this,k,v);};
    try {await ReaderStorage.loadShelf(key,database);} finally {Storage.prototype.setItem=set;}
    const interrupted=!!JSON.parse(localStorage.getItem(key)).books[0].chapters;
    const restored=await ReaderStorage.loadShelf(key,database);
    return {retained,stillHasCatalog,interrupted,books:restored.books.length,progress:restored.books[0].progress,hasEmbedded:'chapters' in JSON.parse(localStorage.getItem(key)).books[0],chapters:(await ReaderStorage.catalog(database,'failed')).chapters.length};
  })()`);
  assert.deepEqual(migrationFailure, {retained:true,stillHasCatalog:true,interrupted:true,books:1,progress:0.7,hasEmbedded:false,chapters:1});

  await evaluate(`(async()=>{
    const db=await ReaderStorage.open('storage-regression-remove');db.close();
    const key='storage-regression-remove-shelf';
    const legacy={books:[{id:'remove-legacy',title:'Keep this catalog',catalog:'https://example.com/catalog',progress:0.6,chapters:[{title:'Only persistent catalog copy',url:'https://example.com/1'}]}],settings:{...DEFAULTS}};
    window.removeFixture={state,key:DB_KEY,database:cacheDB,confirm:window.confirm,raw:JSON.stringify(legacy)};
    localStorage.setItem(key,removeFixture.raw);state=legacy;DB_KEY=key;cacheDB=Promise.resolve(db);window.confirm=()=>true;
    renderShelf();document.querySelector('#shelf .book-actions button').click();
  })()`);
  await until('!pending');
  const removal=await evaluate(`(()=>{
    const raw=localStorage.getItem(DB_KEY),parsed=JSON.parse(raw);
    const result={same:raw===removeFixture.raw,books:parsed.books.length,chapters:parsed.books[0].chapters?.length,progress:parsed.books[0].progress};
    state=removeFixture.state;DB_KEY=removeFixture.key;cacheDB=removeFixture.database;window.confirm=removeFixture.confirm;renderShelf();return result;
  })()`);
  assert.deepEqual(removal,{same:true,books:1,chapters:1,progress:0.6},'Failed removal restores the exact legacy shelf, including its only catalog copy');

  const ebook = await evaluate(`(async () => {
    const db=storageFixture.database,id='a'.repeat(64);
    const legacy={id,title:'Large synthetic',author:'Test',sections:Array.from({length:100},(_,i)=>({title:'Section '+i,paragraphs:[String(i).padStart(3,'0')+'x'.repeat(9997)]}))};
    await ReaderStorage.transact(db,['ebooks'],'readwrite',tx=>tx.objectStore('ebooks').put(legacy));
    const migrated=await ReaderStorage.book(db,id);
    const old=await ReaderStorage.transact(db,['ebooks'],'readonly',tx=>tx.objectStore('ebooks').get(id));
    const get=IDBObjectStore.prototype.get, getAll=IDBObjectStore.prototype.getAll, reads=[];
    IDBObjectStore.prototype.get=function(key){const req=get.call(this,key);if(['ebooks','ebookSections'].includes(this.name)){const name=this.name;req.addEventListener('success',()=>reads.push({store:name,characters:req.result?.paragraphs?.join('').length||0}));}return req;};
    IDBObjectStore.prototype.getAll=function(...args){if(['ebooks','ebookSections'].includes(this.name))throw new Error('Full book scan during chapter navigation');return getAll.apply(this,args);};
    const originalDB=cacheDB;cacheDB=Promise.resolve(db);
    let one,two;
    try {one=await localEbookPage('ebook:'+id+'/37');two=await localEbookPage('ebook:'+id+'/38');}
    finally {cacheDB=originalDB;IDBObjectStore.prototype.get=get;IDBObjectStore.prototype.getAll=getAll;}
    return {sections:migrated.meta.sectionCount,legacyDeleted:!old,reads,title:one.title,next:one.next,previous:two.previous,paragraph:one.paragraphs[0].slice(0,3),catalogIncluded:one.chapters.length};
  })()`);
  assert.equal(ebook.sections, 100);
  assert.equal(ebook.legacyDeleted, true);
  assert.deepEqual(ebook.reads, [{store:'ebookSections',characters:10000},{store:'ebookSections',characters:10000}]);
  assert.equal(ebook.title, 'Section 37');
  assert.equal(ebook.paragraph, '037');
  assert.equal(ebook.catalogIncluded, 0);
  assert.equal(ebook.next, `ebook:${'a'.repeat(64)}/38`);
  assert.equal(ebook.previous, `ebook:${'a'.repeat(64)}/37`);

  const ebookFailure = await evaluate(`(async () => {
    const db=storageFixture.database,id='b'.repeat(64);
    const legacy={id,title:'Migration fallback',sections:[{title:'One',paragraphs:['original one']},{title:'Two',paragraphs:['original two']}]};
    await ReaderStorage.transact(db,['ebooks'],'readwrite',tx=>tx.objectStore('ebooks').put(legacy));
    const put=IDBObjectStore.prototype.put;
    IDBObjectStore.prototype.put=function(...args){if(this.name==='ebookSections')throw new Error('Simulated full storage');return put.apply(this,args);};
    let failed;
    try {failed=await ReaderStorage.book(db,id);} finally {IDBObjectStore.prototype.put=put;}
    const stillOld=!!(await ReaderStorage.transact(db,['ebooks'],'readonly',tx=>tx.objectStore('ebooks').get(id)));
    // An interrupted prior run left only one new section and no verified marker.
    await ReaderStorage.transact(db,['ebookMeta','ebookSections','migrationState'],'readwrite',tx=>{
      tx.objectStore('ebookMeta').put({id,status:'staged'});
      tx.objectStore('ebookSections').put({bookId:id,index:0,title:'Wrong',paragraphs:['incomplete']});
      tx.objectStore('migrationState').put({key:'ebook:'+id,id,kind:'migration',phase:'copied'});
    });
    const recovered=await ReaderStorage.book(db,id),second=await ReaderStorage.section(db,id,1);
    const duplicate=await ReaderStorage.book(db,id);
    let rejected=false;
    try {await ReaderStorage.importBook(db,legacy,()=>{throw new Error('metadata unavailable');});} catch {rejected=true;}
    const preserved=await ReaderStorage.section(db,id,1);
    await ReaderStorage.transact(db,['ebookSections'],'readwrite',tx=>tx.objectStore('ebookSections').delete([id,1]));
    await ReaderStorage.importBook(db,legacy,()=>{});
    const repaired=!!(await ReaderStorage.section(db,id,1));
    const fresh={...legacy,id:'c'.repeat(64)};
    try {await ReaderStorage.importBook(db,fresh,()=>{throw new Error('metadata unavailable');});} catch {}
    const rollback=!(await ReaderStorage.transact(db,['ebookMeta'],'readonly',tx=>tx.objectStore('ebookMeta').get(fresh.id))) && !(await ReaderStorage.section(db,fresh.id,0));
    return {fallback:!!failed.legacy,stillOld,sections:recovered.meta.sectionCount,second:second.paragraphs[0],duplicate:duplicate.meta.sectionCount,rejected,preserved:preserved.paragraphs[0],repaired,rollback};
  })()`);
  assert.deepEqual(ebookFailure,{fallback:true,stillOld:true,sections:2,second:'original two',duplicate:2,rejected:true,preserved:'original two',repaired:true,rollback:true});

  const publication = await evaluate(`(async () => {
    const db=storageFixture.database,key='storage-regression-publish',id='d'.repeat(64);
    const data={id,title:'Durable import',sections:[{title:'One',paragraphs:['durable']} ]};
    const remove=IDBObjectStore.prototype.delete;
    IDBObjectStore.prototype.delete=function(k){if(this.name==='ebooks'&&k===id)throw new Error('Interrupted final cleanup');return remove.call(this,k);};
    try {await ReaderStorage.importBook(db,data,()=>ReaderStorage.saveShelf(key,{books:[{id,catalog:'ebook:'+id,title:data.title,progress:0.35}],settings:{}}));}
    finally {IDBObjectStore.prototype.delete=remove;}
    const before=await ReaderStorage.transact(db,['migrationState'],'readonly',tx=>tx.objectStore('migrationState').get('ebook:'+id));
    let retryFailed=false;
    try {await ReaderStorage.importBook(db,data,()=>{throw new Error('Retry metadata write failed');});}catch{retryFailed=true;}
    const retryTask=await ReaderStorage.transact(db,['migrationState'],'readonly',tx=>tx.objectStore('migrationState').get('ebook:'+id));
    const retryBody=(await ReaderStorage.section(db,id,0))?.paragraphs[0];
    const shelf=await ReaderStorage.loadShelf(key,db);
    const after=await ReaderStorage.transact(db,['migrationState'],'readonly',tx=>tx.objectStore('migrationState').get('ebook:'+id));
    const content=await ReaderStorage.section(db,id,0);
    await ReaderStorage.removeBook(db,id,id);
    return {pending:before?.phase,retryFailed,retryPhase:retryTask?.phase,retryBody,progress:shelf.books[0].progress,recovered:!after,body:content.paragraphs[0],deleted:!(await ReaderStorage.section(db,id,0)),otherSurvives:!!(await ReaderStorage.section(db,'a'.repeat(64),37))};
  })()`);
  assert.deepEqual(publication,{pending:'verified',retryFailed:true,retryPhase:'verified',retryBody:'durable',progress:0.35,recovered:true,body:'durable',deleted:true,otherSurvives:true});

  const blocked = await evaluate(`(async () => {
    const name='storage-regression-blocked';
    const old=await new Promise((resolve,reject)=>{const r=indexedDB.open(name,2);r.onupgradeneeded=()=>{r.result.createObjectStore('ebooks',{keyPath:'id'});r.result.createObjectStore('pages',{keyPath:'url'});};r.onsuccess=()=>resolve(r.result);r.onerror=()=>reject(r.error);});
    const messages=[];const failed=await ReaderStorage.open(name,m=>messages.push(m));old.close();
    const upgraded=await ReaderStorage.open(name);const result={bounded:failed===null,prompted:messages.length>0,version:upgraded.version};upgraded.close();return result;
  })()`);
  assert.deepEqual(blocked,{bounded:true,prompted:true,version:3});

  await evaluate(`(async () => {
    const id='e'.repeat(64),data={id,title:'Offline synthetic',sections:[{title:'Offline chapter',paragraphs:['Stored entirely on this device.']}]};
    await ReaderStorage.importBook(cacheDB,data,()=>{
      state={books:[{id,title:data.title,catalog:'ebook:'+id,lastUrl:'ebook:'+id+'/0',progress:0.4,chapters:[]}],settings:{...DEFAULTS}};
      persistState();
    });
    await cachePage('https://offline.example/1',{kind:'chapter',url:'https://offline.example/1',title:'Cached',bookTitle:'Cached',paragraphs:['Offline cached chapter'],warnings:[],pages:1,chapters:[],next:null,previous:null,catalog:null});
    await ReaderStorage.putCatalog(cacheDB,'offline-directory',[{title:'Cached',url:'https://offline.example/1'}],'https://offline.example/catalog');
    state.books.push({id:'offline-directory',title:'Offline directory',catalog:'https://offline.example/catalog',lastUrl:null,progress:0,chapters:[]});save();
    await navigator.serviceWorker.ready;
  })()`);
  await until('!!navigator.serviceWorker.controller');
  await command('Network.enable');
  await command('Network.emulateNetworkConditions',{offline:true,latency:0,downloadThroughput:-1,uploadThroughput:-1});
  await reload();
  const offline = await evaluate(`(async()=>({home:!document.getElementById('home').hidden,banner:!document.getElementById('offline-banner').hidden,book:(await localEbookPage(state.books[0].lastUrl)).title,cached:(await getPage('https://offline.example/1')).offline,progress:state.books[0].progress}))()`);
  assert.deepEqual(offline,{home:true,banner:true,book:'Offline chapter',cached:true,progress:0.4});
  const offlineDirectory=await evaluate(`(async()=>{const item=state.books.find(b=>b.id==='offline-directory');await load(item.catalog,item);const result={open:document.getElementById('catalog-dialog').open,chapters:book.chapters.length};document.getElementById('catalog-dialog').close();return result;})()`);
  assert.deepEqual(offlineDirectory,{open:true,chapters:1});
  for (const mode of ['cloud', null]) {
    await evaluate(mode ? `serviceMode('${mode}')` : 'localStorage.removeItem(MODE_KEY)');
    await reload();
    assert.equal(await evaluate('document.getElementById("home").hidden && cacheDB === null'),true,'Unknown/cloud mode must not unlock offline');
  }
  await command('Network.emulateNetworkConditions',{offline:false,latency:0,downloadThroughput:-1,uploadThroughput:-1});
  for (const status of [401,403,503]) {
    await evaluate('serviceMode("local")');
    const hook=await command('Page.addScriptToEvaluateOnNewDocument',{source:`const realFetch=window.fetch;window.fetch=(url,options)=>url==='/api/health'?Promise.resolve(new Response('{"error":"unavailable"}',{status:${status}})):realFetch(url,options);`});
    await reload();
    assert.deepEqual(await evaluate('({hidden:document.getElementById("home").hidden, database:cacheDB===null})'),{hidden:true,database:true},`HTTP ${status} must not unlock offline`);
    await command('Page.removeScriptToEvaluateOnNewDocument',{identifier:hook.identifier});
  }
  await reload();
  assert.equal(await evaluate('!document.getElementById("home").hidden && !localOffline'),true);
  const cancelled=await evaluate(`(async()=>{const original=window.fetch;window.fetch=(url,opts)=>new Promise((resolve,reject)=>opts.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError'))));try{await fetchTimed('/stalled',{},10);return false;}catch(e){return e.name==='AbortError';}finally{window.fetch=original;}})()`);
  assert.equal(cancelled,true,'Request timeout must cancel a stalled fetch');
  console.log('PASS: 48,000 catalog links without localStorage overflow, migration interruption/rollback, per-section ebook reads, import recovery, upgrade blocking, local offline restart and fail-closed account modes');
}
