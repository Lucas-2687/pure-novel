'use strict';
window.ReaderStorage = (() => {
  const queues = new Map();
  const notices = new WeakMap();
  const stores = ['ebookMeta', 'ebookSections', 'ebooks', 'migrationState'];

  function open(name, notify = () => {}) {
    return new Promise(resolve => {
      let request, finished = false, timer;
      const finish = db => { if (finished) { db?.close(); return; } finished = true; clearTimeout(timer); resolve(db); };
      try { request = indexedDB.open(name, 3); }
      catch { finish(null); return; }
      request.onupgradeneeded = () => {
        const db = request.result;
        for (const [name, keyPath] of [['pages', 'url'], ['ebooks', 'id'], ['catalogs', 'bookId'], ['ebookMeta', 'id'], ['migrationState', 'key']]) {
          if (!db.objectStoreNames.contains(name)) db.createObjectStore(name, { keyPath });
        }
        if (!db.objectStoreNames.contains('ebookSections')) {
          db.createObjectStore('ebookSections', { keyPath: ['bookId', 'index'] }).createIndex('bookId', 'bookId');
        }
      };
      request.onblocked = () => {
        notify('本机存储升级被其他标签页阻挡，请关闭旧标签页后重新打开。');
        timer ??= setTimeout(() => finish(null), 4000);
      };
      request.onsuccess = () => {
        const db = request.result;
        notices.set(db, notify);
        db.onversionchange = () => { db.close(); notify('本机存储已升级，请刷新当前标签页后继续。'); };
        finish(db);
      };
      request.onerror = () => finish(null);
    });
  }

  async function transact(database, names, mode, operation) {
    const db = await database;
    if (!db) throw new Error('本机存储不可用，请检查浏览器存储权限。');
    return new Promise((resolve, reject) => {
      let tx, result;
      try {
        tx = db.transaction(names, mode);
        tx.oncomplete = () => resolve(typeof result === 'function' ? result() : result?.result);
        tx.onerror = tx.onabort = () => reject(new Error('本机存储操作失败，可能空间不足；原数据会保留。'));
        result = operation(tx);
      } catch (error) { try { tx?.abort(); } catch {} reject(error); }
    });
  }
  const get = (db, store, key) => transact(db, [store], 'readonly', tx => tx.objectStore(store).get(key));

  async function exclusive(database, key, action) {
    const db = await database;
    if (!db) throw new Error('本机存储不可用。');
    const name = `pure-novel:${db.name}:${key}`;
    if (navigator.locks?.request) return navigator.locks.request(name, () => action(db));
    const previous = queues.get(name) || Promise.resolve();
    const pending = previous.catch(() => {}).then(() => action(db));
    queues.set(name, pending);
    try { return await pending; }
    finally { if (queues.get(name) === pending) queues.delete(name); }
  }

  function snapshot(state) {
    return { schemaVersion: 2, books: state.books.map(({ chapters, ...metadata }) => metadata), settings: state.settings };
  }
  function parseShelf(raw) {
    const data = JSON.parse(raw || 'null');
    if (!data || !Array.isArray(data.books)) return { books: [], settings: {} };
    return { ...data, books: data.books.map(b => ({ ...b, chapters: Array.isArray(b.chapters) ? b.chapters : [] })), settings: data.settings || {} };
  }
  function saveShelf(key, state) {
    const next = snapshot(state);
    const raw = localStorage.getItem(key);
    let old;
    try { old = parseShelf(raw); } catch { old = { books: [], settings: {} }; }
    // A failed migration must not discard the only persistent copy of a catalog.
    if (old.schemaVersion !== 2 && old.books.some(b => b.chapters.length)) {
      const previous = new Map(old.books.map(b => [b.id, b.chapters]));
      next.schemaVersion = 1;
      next.books = next.books.map((b, i) => ({ ...b, chapters: state.books[i].chapters?.length ? state.books[i].chapters : previous.get(b.id) || [] }));
    }
    localStorage.setItem(key, JSON.stringify(next));
  }
  async function loadShelf(key, database, notify = () => {}) {
    let raw, state;
    try { raw = localStorage.getItem(key); state = parseShelf(raw); }
    catch { notify('本机书架无法读取，请检查浏览器存储权限。'); return { books: [], settings: {} }; }
    if (state.schemaVersion === 2) { await recoverImports(database, key, notify); return state; }
    const catalogs = state.books.filter(b => b.chapters.length);
    if (!catalogs.length) {
      try { saveShelf(key, state); } catch { notify('本机书架无法保存，请检查存储权限。'); }
      await recoverImports(database, key, notify); return state;
    }
    try {
      await exclusive(database, 'shelf', async db => {
        await transact(db, ['catalogs', 'migrationState'], 'readwrite', tx => {
          for (const book of catalogs) tx.objectStore('catalogs').put({ bookId: book.id, chapters: book.chapters, url: book.catalog, next: null });
          tx.objectStore('migrationState').put({ key: 'shelf', phase: 'copied' });
        });
        for (const book of catalogs) {
          const copied = await get(db, 'catalogs', book.id);
          if (JSON.stringify(copied.chapters) !== JSON.stringify(book.chapters)) throw new Error('目录核验失败。');
        }
        if (localStorage.getItem(key) !== raw) throw new Error('其他标签页更新了书架，请刷新后重试迁移。');
        localStorage.setItem(key, JSON.stringify(snapshot(state)));
        await transact(db, ['migrationState'], 'readwrite', tx => tx.objectStore('migrationState').delete('shelf'));
      });
      state = parseShelf(localStorage.getItem(key));
    } catch (error) { notify(`目录升级未完成，旧目录已保留。${error.message}`); }
    await recoverImports(database, key, notify);
    return state;
  }

  async function putCatalog(database, bookId, chapters, url, next = null) {
    return transact(database, ['catalogs'], 'readwrite', tx => tx.objectStore('catalogs').put({ bookId, chapters, url, next }));
  }
  const catalog = (database, bookId) => get(database, 'catalogs', bookId);

  function sectionRange(id) { return IDBKeyRange.bound([id, 0], [id, Number.MAX_SAFE_INTEGER]); }
  function eraseSections(tx, id) { tx.objectStore('ebookSections').delete(sectionRange(id)); }
  function metadata(data, status) {
    const { sections, ...meta } = data;
    return { ...meta, sectionCount: sections.length, titles: sections.map(s => s.title), status };
  }
  async function stage(db, data, kind) {
    if (!data.sections?.length) throw new Error('电子书没有章节。');
    await transact(db, stores, 'readwrite', tx => {
      eraseSections(tx, data.id);
      tx.objectStore('ebookMeta').put(metadata(data, 'staged'));
      data.sections.forEach((section, index) => tx.objectStore('ebookSections').put({ bookId: data.id, index, title: section.title, paragraphs: section.paragraphs }));
      tx.objectStore('migrationState').put({ key: `ebook:${data.id}`, id: data.id, kind, phase: 'copied' });
    });
    const rows = await transact(db, ['ebookSections'], 'readonly', tx => tx.objectStore('ebookSections').getAll(sectionRange(data.id)));
    if (rows.length !== data.sections.length || rows.some((row, i) => row.index !== i || row.title !== data.sections[i].title || JSON.stringify(row.paragraphs) !== JSON.stringify(data.sections[i].paragraphs))) {
      throw new Error('电子书迁移核验失败，旧正文已保留。');
    }
    await transact(db, ['migrationState'], 'readwrite', tx => tx.objectStore('migrationState').put({ key: `ebook:${data.id}`, id: data.id, kind, phase: 'verified' }));
  }
  async function finishBook(db, id, expectedLegacy) {
    await transact(db, stores, 'readwrite', tx => {
      const req = tx.objectStore('ebookMeta').get(id);
      req.onsuccess = () => {
        if (!req.result) { tx.abort(); return; }
        const finish = () => {
          tx.objectStore('ebookMeta').put({ ...req.result, status: 'ready' });
          tx.objectStore('ebooks').delete(id);
          tx.objectStore('migrationState').delete(`ebook:${id}`);
        };
        if (expectedLegacy) {
          const original = tx.objectStore('ebooks').get(id);
          original.onsuccess = () => { if (JSON.stringify(original.result) === JSON.stringify(expectedLegacy)) finish(); else tx.abort(); };
        } else finish();
      };
    });
  }
  async function discardStaged(db, id) {
    await transact(db, ['ebookMeta', 'ebookSections', 'migrationState'], 'readwrite', tx => {
      tx.objectStore('ebookMeta').delete(id); eraseSections(tx, id);
      tx.objectStore('migrationState').delete(`ebook:${id}`);
    });
  }
  async function recoverImports(database, shelfKey, notify) {
    const db = await database;
    if (!db) return;
    try {
      const tasks = await transact(db, ['migrationState'], 'readonly', tx => tx.objectStore('migrationState').getAll());
      for (const task of tasks.filter(t => t.kind === 'import')) {
        await exclusive(db, task.key, async () => {
          const active = await get(db, 'migrationState', task.key);
          if (!active || active.kind !== 'import') return;
          const shelf = parseShelf(localStorage.getItem(shelfKey));
          if (shelf.books.some(b => b.catalog === task.key)) {
            if (active.phase === 'verified') await finishBook(db, task.id);
          } else if (navigator.locks?.request) {
            // Without cross-tab locks, leave an unpublished record for the next import.
            await discardStaged(db, task.id);
          }
        });
      }
    } catch (error) { notify(`电子书整理稍后重试。${error.message}`); }
  }
  async function importBook(database, data, publish) {
    return exclusive(database, `ebook:${data.id}`, async db => {
      const existing = await get(db, 'ebookMeta', data.id);
      const previousTask = await get(db, 'migrationState', `ebook:${data.id}`);
      const durable = existing && (existing.status === 'ready' || previousTask?.phase === 'verified');
      const previousSections = durable ? await transact(db, ['ebookSections'], 'readonly', tx => tx.objectStore('ebookSections').getAll(sectionRange(data.id))) : null;
      let published = false;
      try {
        await stage(db, data, 'import');
        publish(); published = true;
        try { await finishBook(db, data.id); }
        catch { notices.get(db)?.('正文和书架已保存，存储整理将在下次打开时继续。'); }
      } catch (error) {
        if (!published) {
          try {
            if (previousSections) {
              await transact(db, ['ebookMeta', 'ebookSections', 'migrationState'], 'readwrite', tx => {
                tx.objectStore('ebookMeta').put(existing); eraseSections(tx, data.id);
                for (const section of previousSections) tx.objectStore('ebookSections').put(section);
                if (previousTask) tx.objectStore('migrationState').put(previousTask);
                else tx.objectStore('migrationState').delete(`ebook:${data.id}`);
              });
            } else await discardStaged(db, data.id);
          } catch {}
        }
        throw error;
      }
    });
  }
  async function book(database, id) {
    return exclusive(database, `ebook:${id}`, async db => {
      let meta = await get(db, 'ebookMeta', id);
      if (meta?.status === 'ready') return { meta };
      const task = await get(db, 'migrationState', `ebook:${id}`);
      if (meta && task?.kind === 'import' && task.phase === 'verified') {
        try { await finishBook(db, id); } catch {}
        return { meta };
      }
      const legacy = await get(db, 'ebooks', id);
      if (!legacy) return null;
      try {
        await stage(db, legacy, 'migration');
        await finishBook(db, id, legacy);
        return { meta: metadata(legacy, 'ready') };
      } catch (error) {
        try { await discardStaged(db, id); } catch {}
        notices.get(db)?.(`电子书升级未完成，继续读取原格式。${error.message}`);
        return { meta: metadata(legacy, 'legacy'), legacy };
      }
    });
  }
  const section = (database, id, index) => get(database, 'ebookSections', [id, index]);
  async function removeBook(database, bookId, ebookId) {
    return exclusive(database, ebookId ? `ebook:${ebookId}` : `catalog:${bookId}`, db => transact(db, [...stores, 'catalogs'], 'readwrite', tx => {
      tx.objectStore('catalogs').delete(bookId);
      if (ebookId) {
        tx.objectStore('ebookMeta').delete(ebookId); tx.objectStore('ebooks').delete(ebookId);
        eraseSections(tx, ebookId); tx.objectStore('migrationState').delete(`ebook:${ebookId}`);
      }
    }));
  }
  return { open, transact, snapshot, saveShelf, loadShelf, putCatalog, catalog, importBook, book, section, removeBook };
})();
