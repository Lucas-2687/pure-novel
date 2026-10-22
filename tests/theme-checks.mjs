import assert from 'node:assert/strict';

const expected = {
  paper: ['#f6f3ec', '#292e29'], almond: ['#f1e4d2', '#3e342a'],
  green: ['#e6efdf', '#2d3a2b'], mist: ['#e9eceb', '#303835'],
  blue: ['#e6eef3', '#2d3944'], night: ['#1e2522', '#c9d1c4']
};
const rgb = hex => hex.slice(1).match(/../g).map(v => parseInt(v, 16));
const luminance = hex => rgb(hex).map(v => v / 255).map(v => v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4).reduce((n, v, i) => n + v * [.2126, .7152, .0722][i], 0);
const contrast = (a, b) => (Math.max(luminance(a), luminance(b)) + .05) / (Math.min(luminance(a), luminance(b)) + .05);
const cssRGB = hex => `rgb(${rgb(hex).join(', ')})`;

export async function themeChecks({ evaluate, command, until }) {
  const execute = source => evaluate(`(async()=>{${source}})()`);
  const reload = async () => {
    await evaluate('document.documentElement.dataset.ready="reloading"');
    await command('Page.reload');
    await until('document.documentElement.dataset.ready === "true"');
  };
  await command('Page.bringToFront');
  await reload();
  await command('Page.bringToFront');
  await execute(`
    window.themeReads=0;const original=window.fetch;
    window.fetch=async(url,options)=>{
      if(url!=='/api/read')return original(url,options);
      themeReads++;
      return new Response(JSON.stringify({kind:'chapter',url:JSON.parse(options.body).url,bookTitle:'Palette regression',title:'Synthetic chapter',paragraphs:Array(140).fill('A generated paragraph for stable reading geometry and saved progress.'),catalog:null,chapters:[],warnings:[],pages:1,next:null,previous:null,nextPage:null}));
    };
    await load('https://palette.example/chapter');
  `);
  try { await until('!pending && !restoringProgress'); }
  catch (error) { throw new Error(`${error.message}: ${JSON.stringify(await evaluate('({hidden:document.hidden,focused:document.hasFocus(),pending,restoringProgress,current:current?.title,offline:localOffline,cloud:cloudMode,error:document.getElementById("import-error").textContent,toast:document.getElementById("toast").textContent})'))}`); }
  await evaluate('scrollTo(0,(document.documentElement.scrollHeight-innerHeight)*0.47)');
  await evaluate(`document.getElementById('settings-button').click();window.themeOriginal={chapter:current,owner:book,node:document.getElementById('content').firstChild,scroll:scrollY,height:document.documentElement.scrollHeight,size:state.settings.size,line:state.settings.line,font:state.settings.font};`);
  for (const [theme, [background, ink]] of Object.entries(expected)) {
    await evaluate(`document.querySelector('#settings-dialog button[data-theme="${theme}"]').click()`);
    const result = await evaluate(`(()=>{
      const s=getComputedStyle(document.body),dialog=document.getElementById('settings-dialog');
      return {theme:document.body.dataset.theme,bg:s.backgroundColor,ink:s.color,panel:s.getPropertyValue('--panel').trim(),muted:s.getPropertyValue('--muted').trim(),accent:s.getPropertyValue('--accent').trim(),button:s.getPropertyValue('--button-ink').trim(),error:s.getPropertyValue('--error').trim(),scheme:document.documentElement.style.colorScheme,slider:getComputedStyle(document.getElementById('font-size')).accentColor,meta:document.querySelector('meta[name="theme-color"]').content,selected:dialog.querySelectorAll('button[data-theme][aria-pressed="true"]').length,sameChapter:current===themeOriginal.chapter&&book===themeOriginal.owner&&document.getElementById('content').firstChild===themeOriginal.node,sameLayout:document.documentElement.scrollHeight===themeOriginal.height&&Math.abs(scrollY-themeOriginal.scroll)<1,typography:[state.settings.size,state.settings.line,state.settings.font],originalTypography:[themeOriginal.size,themeOriginal.line,themeOriginal.font],reads:themeReads};
    })()`);
    assert.equal(result.theme, theme);
    assert.equal(result.bg, cssRGB(background));
    assert.equal(result.ink, cssRGB(ink));
    assert.equal(result.meta.toLowerCase(), background);
    assert.equal(result.scheme, theme === 'night' ? 'dark' : 'light');
    assert.equal(result.slider, cssRGB(result.accent));
    assert.equal(result.selected, 1);
    assert.equal(result.sameChapter && result.sameLayout, true, 'Theme changes must not re-render, reflow or move the chapter');
    assert.deepEqual(result.typography, result.originalTypography);
    assert.equal(result.reads, 1, 'Changing a palette must not fetch chapter content again');
    for (const [foreground, surface] of [[ink, background], [result.muted, background], [result.muted, result.panel], [result.button, result.accent], [result.error, background]]) {
      assert.ok(contrast(foreground, surface) >= 4.5, `${theme}: ${foreground} on ${surface}`);
    }
  }
  for (const dimensions of [{width:360,height:640},{width:390,height:844},{width:844,height:390}]) {
    await command('Emulation.setDeviceMetricsOverride', {...dimensions,deviceScaleFactor:1,mobile:true});
    const layout=await evaluate(`(()=>{const d=document.getElementById('settings-dialog'),r=d.getBoundingClientRect();return {overflow:document.documentElement.scrollWidth>innerWidth,dialogOverflow:d.scrollWidth>d.clientWidth,visible:r.top>=0&&r.bottom<=innerHeight+1,sizes:[...d.querySelectorAll('.theme-card')].map(n=>({width:n.getBoundingClientRect().width,height:n.getBoundingClientRect().height}))};})()`);
    assert.equal(layout.overflow || layout.dialogOverflow, false);
    assert.equal(layout.visible, true);
    assert.ok(layout.sizes.every(size=>size.width>=44&&size.height>=44));
  }
  await command('Emulation.setDeviceMetricsOverride',{width:390,height:844,deviceScaleFactor:1,mobile:true});
  await command('Page.bringToFront');
  await evaluate(`document.querySelector('#settings-dialog button[data-theme="paper"]').focus()`);
  assert.equal(await evaluate('document.activeElement?.dataset.theme'),'paper');
  await command('Input.dispatchKeyEvent',{type:'keyDown',key:'Enter',code:'Enter',windowsVirtualKeyCode:13,text:'\r',unmodifiedText:'\r'});
  await command('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter',windowsVirtualKeyCode:13});
  assert.equal(await evaluate('document.body.dataset.theme'), 'paper', 'Palette choices support keyboard activation');
  await command('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await command('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await until('!document.getElementById("settings-dialog").open');
  await evaluate(`selectReadingTheme('blue');document.getElementById('back').click()`);
  await reload();
  assert.deepEqual(await evaluate('({theme:state.settings.theme,palette:state.settings.dayPalette,shown:document.body.dataset.theme})'),{theme:'paper',palette:'blue',shown:'blue'});
  const legacy=await evaluate(`(()=>{
    const saved=JSON.parse(localStorage.getItem(DB_KEY));
    if(!['paper','green','night'].includes(saved.settings.theme))saved.settings.theme='paper';
    saved.books.find(b=>b.title==='Palette regression').progress=0.63;
    localStorage.setItem(DB_KEY,JSON.stringify(saved));return saved.settings.dayPalette;
  })()`);
  assert.equal(legacy,'blue');
  await reload();
  assert.equal(await evaluate('document.body.dataset.theme'),'blue','A legacy-compatible metadata save preserves the extra palette');
  const normalization=await evaluate(`(()=>{
    const kept={...state.settings},results=[];
    for(const theme of ['paper','green','night']){state.settings={...DEFAULTS,theme};applySettings();results.push(document.body.dataset.theme);}
    state.settings={...DEFAULTS,dayPalette:{invalid:true}};applySettings();results.push(document.body.dataset.theme);
    state.settings={...DEFAULTS,theme:'unknown'};applySettings();results.push(document.body.dataset.theme);
    state.settings=kept;selectReadingTheme('night');const retained=state.settings.dayPalette;selectReadingTheme('paper');const cleared=!('dayPalette' in state.settings);state.settings=kept;applySettings();return {results,retained,cleared};
  })()`);
  assert.deepEqual(normalization,{results:['paper','green','night','paper','paper'],retained:'blue',cleared:true});
  console.log('PASS: six reading palettes, actual CSS contrast, 44px controls, keyboard, zero fetch/reflow/scroll changes, legacy settings and reload persistence');

  try {
    await execute(`
      window.themeOriginalFetch=window.fetch;window.themeActive='palette-a';window.themePosts=[];
      window.themeRemote={'palette-a':{version:0,payload:null},'palette-b':{version:0,payload:{books:[],settings:{...DEFAULTS,theme:'green'}}}};
      window.fetch=async(url,options={})=>{
        const remote=themeRemote[themeActive],reply=(value,status=200)=>new Response(JSON.stringify(value),{status});
        if(url==='/api/health')return reply({ok:true,authRequired:true});
        if(url==='/api/bootstrap')return reply({user:{id:themeActive,username:'palette_reader'},...remote});
        if(url==='/api/state'&&options.method==='POST'){
          const body=JSON.parse(options.body);themePosts.push(body);
          if(body.version!==remote.version)return reply({error:'Conflict'},409);
          remote.payload=body.payload;remote.version++;return reply({ok:true,version:remote.version});
        }
        if(url==='/api/state')return reply(remote);
        throw new Error('Unexpected palette test request');
      };
      const remote=await verifiedAccount();await startAccount(remote.user,remote);
      selectReadingTheme('almond');clearTimeout(syncTimer);await pushState();
    `);
    assert.deepEqual(await evaluate('({theme:themeRemote[themeActive].payload.settings.theme,palette:themeRemote[themeActive].payload.settings.dayPalette,posts:themePosts.length,dirty})'),{theme:'paper',palette:'almond',posts:1,dirty:false});
    await execute(`selectReadingTheme('almond');clearTimeout(syncTimer);await pushState()`);
    assert.equal(await evaluate('themePosts.length'),1,'Selecting the same palette does not POST again');
    await evaluate(`enterAccountOffline();selectReadingTheme('blue')`);
    assert.deepEqual(await evaluate('({offline:localOffline,dirty,palette:JSON.parse(localStorage.getItem(DB_KEY)).settings.dayPalette,posts:themePosts.length})'),{offline:true,dirty:true,palette:'blue',posts:1});
    await execute(`await reconnectLocal(true)`);
    await until('!localOffline && !dirty && !syncing');
    assert.equal(await evaluate('themeRemote[themeActive].payload.settings.dayPalette'),'blue');
    await execute(`themeActive='palette-b';const remote=await verifiedAccount();await startAccount(remote.user,remote)`);
    assert.equal(await evaluate('document.body.dataset.theme'),'green','The next account cannot inherit the previous palette');
    await execute(`themeActive='palette-a';const remote=await verifiedAccount();await startAccount(remote.user,remote)`);
    assert.equal(await evaluate('document.body.dataset.theme'),'blue','Returning to an account restores its own palette');
    await execute(`themeRemote[themeActive].version++;selectReadingTheme('mist');clearTimeout(syncTimer);await pushState()`);
    assert.equal(await evaluate('syncBlocked && document.getElementById("conflict-dialog").open'),true);
    assert.equal(await evaluate('document.body.dataset.theme'),'mist','A conflict must not silently discard the local palette');
    await evaluate(`document.getElementById('keep-local').click()`);
    await until('!dirty && !syncing && !document.getElementById("conflict-dialog").open');
    assert.equal(await evaluate('themeRemote[themeActive].payload.settings.dayPalette'),'mist');
    console.log('PASS: palette cloud payload, no-op clicks, offline save/reconnect, per-account restoration and explicit version conflicts');
  } finally {
    await evaluate(`clearTimeout(syncTimer);window.fetch=themeOriginalFetch;revokeOffline();localStorage.removeItem(LOGOUT_KEY)`).catch(()=>{});
    await reload();
  }
}
