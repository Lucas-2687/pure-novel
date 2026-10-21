'use strict';
// Text-only, bounded EPUB importer. No source markup is ever mounted in the page.
window.Ebooks = (() => {
  const MAX = 24 * 1024 * 1024, TOTAL = 64 * 1024 * 1024;
  const crcTable = Uint32Array.from({ length: 256 }, (_, n) => {
    for (let i = 0; i < 8; i++) n = n & 1 ? 0xedb88320 ^ (n >>> 1) : n >>> 1;
    return n >>> 0;
  });
  function crc32(bytes) {
    let crc = 0xffffffff;
    for (const byte of bytes) crc = crcTable[(crc ^ byte) & 255] ^ (crc >>> 8);
    return (crc ^ 0xffffffff) >>> 0;
  }
  function xml(text) {
    if (/<!DOCTYPE|<!ENTITY/i.test(text)) throw new Error('不支持含 DTD 或实体声明的电子书。');
    const doc = new DOMParser().parseFromString(text, 'application/xml');
    if (doc.querySelector('parsererror')) throw new Error('电子书 XML 结构损坏。');
    return doc;
  }
  function path(base, href) {
    if (/^[a-z][a-z\d+.-]*:|^\/|\\/i.test(href)) throw new Error('电子书含不安全路径。');
    const parts = base.split('/').slice(0, -1);
    for (const part of decodeURIComponent(href.split('#')[0]).split('/')) {
      if (part === '..') { if (!parts.length) throw new Error('电子书路径越界。'); parts.pop(); }
      else if (part && part !== '.') parts.push(part);
    }
    return parts.join('/');
  }
  async function unzip(buffer) {
    const v = new DataView(buffer), b = new Uint8Array(buffer), files = new Map();
    let end = -1;
    for (let i = b.length - 22; i >= Math.max(0, b.length - 65557); i--) {
      if (v.getUint32(i, true) === 0x06054b50 && i + 22 + v.getUint16(i + 20, true) === b.length) { end = i; break; }
    }
    if (end < 0 || v.getUint16(end + 4, true) || v.getUint16(end + 6, true)) throw new Error('不是受支持的 EPUB ZIP 文件。');
    const count = v.getUint16(end + 10, true);
    if (count !== v.getUint16(end + 8, true)) throw new Error('不支持分卷 ZIP。');
    if (count > 3000) throw new Error('电子书文件数量过多。');
    let offset = v.getUint32(end + 16, true), total = 0;
    const directoryStart = offset;
    if (offset + v.getUint32(end + 12, true) !== end) throw new Error('ZIP 目录边界异常。');
    for (let i = 0; i < count; i++) {
      if (offset + 46 > end || v.getUint32(offset, true) !== 0x02014b50) throw new Error('ZIP 目录损坏。');
      const flags = v.getUint16(offset + 8, true), method = v.getUint16(offset + 10, true);
      const checksum = v.getUint32(offset + 16, true);
      const packed = v.getUint32(offset + 20, true), size = v.getUint32(offset + 24, true);
      const n = v.getUint16(offset + 28, true), extra = v.getUint16(offset + 30, true), comment = v.getUint16(offset + 32, true);
      const local = v.getUint32(offset + 42, true);
      if (offset + 46 + n + extra + comment > end || size > MAX || (total += size) > TOTAL || flags & 1) throw new Error('电子书过大、加密或目录异常。');
      const name = new TextDecoder('utf-8', { fatal: true }).decode(b.slice(offset + 46, offset + 46 + n));
      if (name.startsWith('/') || name.includes('\\') || name.split('/').includes('..') || files.has(name)) throw new Error('电子书路径不安全或重复。');
      if (local + 30 > b.length || v.getUint32(local, true) !== 0x04034b50) throw new Error('ZIP 文件头损坏。');
      const start = local + 30 + v.getUint16(local + 26, true) + v.getUint16(local + 28, true);
      if (start + packed > directoryStart || v.getUint16(local + 8, true) !== method || v.getUint16(local + 6, true) !== flags) throw new Error('ZIP 文件被截断或文件头不一致。');
      const localName = new TextDecoder('utf-8', { fatal: true }).decode(b.slice(local + 30, local + 30 + v.getUint16(local + 26, true)));
      if (localName !== name) throw new Error('ZIP 文件名不一致。');
      files.set(name, { method, size, checksum, bytes: b.slice(start, start + packed) });
      offset += 46 + n + extra + comment;
    }
    if (offset !== end) throw new Error('ZIP 目录长度异常。');
    let actualTotal = 0;
    const read = async name => {
      const entry = files.get(name);
      if (!entry) throw new Error(`电子书缺少文件：${name}`);
      let bytes = entry.bytes;
      if (entry.method === 8) {
        let stream;
        try { stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream('deflate-raw')); }
        catch { throw new Error('此浏览器不支持 EPUB 解压，请更新 Safari，或改用 TXT。'); }
        const reader = stream.getReader(), chunks = []; let length = 0;
        while (true) {
          const { done, value } = await reader.read(); if (done) break;
          length += value.length;
          if (length > entry.size || length > MAX) { await reader.cancel(); throw new Error('电子书解压超过限制。'); }
          chunks.push(value);
        }
        bytes = new Uint8Array(length); let at = 0;
        for (const c of chunks) { bytes.set(c, at); at += c.length; }
      } else if (entry.method !== 0) throw new Error('不支持此 EPUB 压缩方式。');
      actualTotal += bytes.length;
      if (bytes.length !== entry.size || actualTotal > TOTAL) throw new Error('电子书大小校验失败。');
      if (crc32(bytes) !== entry.checksum) throw new Error('电子书 CRC 校验失败，文件可能已损坏。');
      return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
    };
    read.has = name => files.has(name);
    return read;
  }
  function paragraphs(text) { return text.split(/\r\n?|\n/).map(x => x.trim()).filter(Boolean); }
  const chapterHeading = /^(?:第[零〇一二三四五六七八九十百千万两\d]+[章回节卷部篇]|chapter\s+(?:\d+|[ivxlcdm]+)\b).{0,70}$/i;
  async function parse(file, report = () => {}) {
    if (!globalThis.crypto?.subtle) throw new Error('电子书导入需要 HTTPS 或 localhost，请使用安全地址打开阅读器。');
    if (!file.size || file.size > MAX) throw new Error('请选择不超过 24 MiB 的 EPUB 或 TXT 文件。');
    report('正在读取文件并校验格式…');
    const buffer = await file.arrayBuffer();
    const bytes = new Uint8Array(buffer);
    const signature = new TextDecoder('latin1').decode(bytes.slice(0, 80));
    if (signature.startsWith('%PDF-') || /\.(pdf|mobi|azw3?|kfx|djvu)$/i.test(file.name) || signature.includes('BOOKMOBI')) {
      throw new Error('暂不支持 PDF、MOBI、AZW/AZW3 等格式，请选择有权使用的无 DRM EPUB 或 TXT 版本；重命名扩展名不能转换格式。');
    }
    const zip = bytes[0] === 0x50 && bytes[1] === 0x4b;
    const format = zip ? 'EPUB' : 'TXT';
    if (/\.epub$/i.test(file.name) && !zip) throw new Error('文件标为 EPUB，但内容不是有效 ZIP/EPUB，可能损坏或下载成了网页。');
    if (!zip && !/\.txt$/i.test(file.name)) throw new Error('无法确定文件格式。请选择 EPUB 或 TXT 文件。');
    const id = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256', buffer)), b => b.toString(16).padStart(2, '0')).join('');
    let title = file.name.replace(/\.[^.]+$/, ''), author = '', sections = [], encoding = '';
    const warnings = [];
    report('正在解析正文和目录…');
    if (format === 'TXT') {
      let text;
      try {
        if (bytes[0] === 0xff && bytes[1] === 0xfe) encoding = 'utf-16le';
        else if (bytes[0] === 0xfe && bytes[1] === 0xff) encoding = 'utf-16be';
        else encoding = 'utf-8';
        try { text = new TextDecoder(encoding, { fatal: true }).decode(buffer); }
        catch (err) {
          if (encoding !== 'utf-8') throw err;
          encoding = 'gb18030'; text = new TextDecoder(encoding, { fatal: true }).decode(buffer);
        }
      } catch { throw new Error('无法识别文字编码，请将文件另存为 UTF-8 TXT 后重试。'); }
      if (/[\u0000-\u0008\u000b\u000c\u000e-\u001f\ufffd]/.test(text)) throw new Error('文件包含二进制内容或无效文字，无法可靠识别编码。');
      if (/<\s*(?:!doctype|html|head|body|script|form)\b/i.test(text)) throw new Error('文件疑似 HTML 网页或登录页，不是可导入的 TXT 正文。');
      warnings.push('TXT 目录按标题启发式识别，可能不完全准确；请核对正文预览。');
      if (encoding === 'gb18030') warnings.push('编码按 GB18030 推测，请确认预览没有乱码；必要时转换为 UTF-8。');
      let length = 0;
      for (const line of paragraphs(text)) {
        // Bound individual lines as well as long books without chapter headings.
        for (let offset = 0; offset < line.length; offset += 12000) {
          const part = line.slice(offset, offset + 12000);
          const heading = offset === 0 && chapterHeading.test(line);
          if (!sections.length || heading || length + part.length > 20000) {
            sections.push({ title: heading ? line : (sections.length ? `正文 · 第 ${sections.length + 1} 节` : '正文'), paragraphs: [] });
            length = 0;
          }
          sections.at(-1).paragraphs.push(part); length += part.length;
          if (sections.length > 3000) throw new Error('章节数过多，最多支持 3000 节。');
        }
      }
    } else {
      const read = await unzip(buffer);
      if (read.has('META-INF/encryption.xml')) throw new Error('此 EPUB 声明了加密资源，当前不支持；请使用有权取得的无 DRM 版本。');
      if (read.has('mimetype') && (await read('mimetype')).trim() !== 'application/epub+zip') throw new Error('ZIP 内容不是 EPUB 文件。');
      if (!read.has('META-INF/container.xml')) throw new Error('ZIP 缺少 EPUB 入口，不是可识别的电子书。');
      const container = xml(await read('META-INF/container.xml'));
      const root = container.getElementsByTagNameNS('*', 'rootfile')[0]?.getAttribute('full-path');
      if (!root) throw new Error('EPUB 缺少 OPF 入口。');
      const opf = xml(await read(path('', root)));
      title = opf.getElementsByTagNameNS('*', 'title')[0]?.textContent.trim() || title;
      author = opf.getElementsByTagNameNS('*', 'creator')[0]?.textContent.trim() || '';
      const items = new Map(Array.from(opf.getElementsByTagNameNS('*', 'item'), n => [n.getAttribute('id'), n]));
      const labels = new Map();
      const navigation = Array.from(items.values()).find(n => (n.getAttribute('properties') || '').split(/\s+/).includes('nav'));
      const ncx = Array.from(items.values()).find(n => n.getAttribute('media-type') === 'application/x-dtbncx+xml');
      try {
        if (navigation) {
          const navPath = path(root, navigation.getAttribute('href') || '');
          const nav = xml(await read(navPath));
          const toc = Array.from(nav.getElementsByTagNameNS('*', 'nav')).find(n => /\btoc\b/.test(n.getAttribute('epub:type') || n.getAttributeNS('http://www.idpf.org/2007/ops', 'type') || ''));
          for (const link of toc?.getElementsByTagNameNS('*', 'a') || []) {
            const target = path(navPath, link.getAttribute('href') || '');
            if (!labels.has(target)) labels.set(target, link.textContent.trim());
          }
        } else if (ncx) {
          const navPath = path(root, ncx.getAttribute('href') || '');
          const nav = xml(await read(navPath));
          for (const point of nav.getElementsByTagNameNS('*', 'navPoint')) {
            const src = point.getElementsByTagNameNS('*', 'content')[0]?.getAttribute('src');
            const label = point.getElementsByTagNameNS('*', 'navLabel')[0]?.textContent.trim();
            if (src && label) { const target = path(navPath, src); if (!labels.has(target)) labels.set(target, label); }
          }
        }
      } catch { warnings.push('附带导航目录无法解析，已按正文阅读顺序生成目录。'); }
      warnings.push('仅保留文字；图片、复杂排版及书内链接不会保留。目录按正文文件划分，同文件内多个目录锚点不单独拆章。');
      for (const ref of opf.getElementsByTagNameNS('*', 'itemref')) {
        if (ref.getAttribute('linear') === 'no') continue;
        const item = items.get(ref.getAttribute('idref'));
        if (!item || item.getAttribute('media-type') !== 'application/xhtml+xml') throw new Error('仅支持可重排的 XHTML EPUB。');
        const contentPath = path(root, item.getAttribute('href') || '');
        const doc = xml(await read(contentPath));
        const body = doc.getElementsByTagNameNS('*', 'body')[0];
        if (!body) continue;
        for (const n of Array.from(body.querySelectorAll('*'))) {
          if (['script', 'style', 'iframe', 'object', 'svg', 'form', 'nav'].includes(n.localName)) n.remove();
        }
        const heading = labels.get(contentPath) || body.querySelector('h1,h2,h3')?.textContent.trim() || `第 ${sections.length + 1} 节`;
        let text = '';
        function walk(n) {
          if (n.nodeType === 3) text += n.textContent;
          else { for (const c of n.childNodes) walk(c); if (['p', 'div', 'br', 'h1', 'h2', 'h3', 'li', 'section'].includes(n.localName)) text += '\n'; }
        }
        walk(body);
        const lines = paragraphs(text);
        if (lines.length) sections.push({ title: heading, paragraphs: lines });
      }
    }
    if (!sections.length || sections.length > 3000) throw new Error('未取得可读正文，或章节数过多。');
    return { id, title: title.slice(0, 200), author: author.slice(0, 200), sections, format, encoding, warnings, filename: file.name, size: file.size };
  }
  return { parse };
})();