/* AI 桌游厅 —— 前端。原生 JS，无构建步骤。
 *
 * 三款游戏共用同一套骨架：公开日志 + 行动区 + 在场名单。
 * 差异只在「行动区渲染什么」和「复盘展示什么」，用 kind 分派。
 */
import * as bgm from '/bgm.js';
import * as detective from '/detective.js';
import * as voice from '/voice.js';
import * as gen from '/gen.js';
import * as cfg from '/cfg.js';

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const S = {
  view: 'home', kind: null, scriptId: null, charId: null,
  game: null, seen: 0, busy: false, scripts: [],
};

// ── 网络 ──────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: { 'Content-Type': 'application/json' }, ...opts,
  });
  if (!r.ok) {
    let msg = `HTTP ${r.status}`;
    try { msg = (await r.json()).detail || msg; } catch { /* 非 JSON 错误体 */ }
    throw new Error(msg);
  }
  return r.json();
}

function showErr(msg) {
  const el = $('err');
  el.className = 'err wrap';
  el.textContent = msg;
  setTimeout(() => { el.className = 'hidden'; }, 9000);
}

// ── 视图切换 ──────────────────────────────────────────
function view(name) {
  S.view = name;
  for (const v of document.querySelectorAll('.view')) v.classList.add('hidden');
  $('v-' + name).classList.remove('hidden');
  // 探案和对局一样要锁死页面高度：场景图和日志各自滚，整页不滚
  document.body.classList.toggle('locked', name === 'game' || name === 'case');
  $('btnHome').classList.toggle('hidden', name === 'home');
  // 记事本只在对局里有意义
  $('btnPad').classList.toggle('hidden', name !== 'game' && name !== 'case');
  $('btnVoice').classList.toggle('hidden', name !== 'game' && name !== 'case');
  if (name === 'home') voice.stop();
  if (name === 'home') $('padDrawer').classList.add('hidden');
  $('phaseBadge').classList.toggle('hidden', name !== 'game' && name !== 'case');
  window.scrollTo(0, 0);
}

// ── 头像 ──────────────────────────────────────────────
// 剧本杀的头像挂在剧本上，狼人杀/卧底的挂在存档上——两个来源
function avUrl(id) {
  if (S.kind === 'mystery') return `/api/avatar/${S.scriptId}/${id}.svg`;
  return `/api/avatar/game/${S.game?.game_id}/${id}.svg`;
}
function av(id, size = 40, cls = '', url = null) {
  return `<img class="av av-${size} ${cls}" alt="" src="${url || avUrl(id)}">`;
}
function idOf(name) {
  const list = S.game?.players || S.game?.roster || [];
  return (list.find((x) => x.name === name) || {}).id || null;
}

function clickable(el, fn) {
  el.onclick = fn;
  el.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn(); }
  };
}

// ── 健康检查 ──────────────────────────────────────────
async function health() {
  const b = $('healthBadge');
  try {
    const h = await api('/api/health');
    if (h.mode === 'mock') {
      b.className = 'badge mock'; b.textContent = '离线模式';
      b.title = 'AI 说预置台词。配置 .env 的 SG_API_KEY 后接入真实模型。';
    } else if (h.ok) {
      b.className = 'badge live'; b.textContent = h.model_chat;
      b.title = `已接入 ${h.base_url}`;
    } else {
      b.className = 'badge err'; b.textContent = '连接失败';
      b.title = h.detail || '';
    }
  } catch {
    b.className = 'badge err'; b.textContent = '服务未响应';
  }
}

// ── 大厅 ──────────────────────────────────────────────
const KIND_ICON = { mystery: '🕯️', werewolf: '🌙', spy: '🎭', detective: '🔎' };

async function loadHome() {
  const cat = await api('/api/catalog');
  $('gameCards').innerHTML = cat.map((c) => `
    <div class="card gameCard" data-kind="${esc(c.kind)}" role="button" tabindex="0"
         aria-label="${esc(c.label)}">
      <span class="kindIcon">${KIND_ICON[c.kind] || '🎲'}</span>
      <h3>${esc(c.label)}</h3>
      <div class="tagline">${esc(c.tagline)}</div>
      <div class="meta"><span>${esc(c.players)}</span><span>${esc(c.minutes)}</span></div>
    </div>`).join('');
  for (const el of document.querySelectorAll('.gameCard')) {
    clickable(el, () => pickKind(el.dataset.kind));
  }
  loadSaves();
  view('home');
}

async function loadSaves() {
  const box = $('saves');
  try {
    const saves = await api('/api/saves');
    if (!saves.length) { box.innerHTML = '<p class="muted">还没有对局记录。</p>'; return; }
    box.innerHTML = saves.map((s) => `
      <div class="saveRow" data-id="${esc(s.game_id)}">
        <span class="kindTag ${esc(s.kind)}">${esc(s.kind_label)}</span>
        <div class="grow">
          <div class="sTitle">${esc(s.title)}</div>
          <div class="sMeta">${esc(s.phase)}　·　${esc(s.human_name || '')}　·　${fmtTime(s.updated_at)}</div>
        </div>
        <div class="bar"><i style="width:${Math.round((s.progress || 0) * 100)}%"></i></div>
        <button class="resume">${s.finished ? '看复盘' : '继续'}</button>
        <button class="del" title="删除存档">✕</button>
      </div>`).join('');
    for (const row of document.querySelectorAll('.saveRow')) {
      const gid = row.dataset.id;
      row.querySelector('.resume').onclick = () => resume(gid);
      row.querySelector('.del').onclick = async () => {
        if (!confirm('删除这个存档？不可恢复。')) return;
        await api('/api/saves/' + gid, { method: 'DELETE' });
        loadSaves();
      };
    }
  } catch (e) {
    box.innerHTML = `<p class="muted">存档读取失败：${esc(e.message)}</p>`;
  }
}

function fmtTime(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function pickKind(kind) {
  S.kind = kind;
  if (kind === 'mystery') return loadScripts();
  if (kind === 'detective') return loadCases();
  showSetup(kind);
}


// ── 开局配置（狼人杀 / 谁是卧底）────────────────────────
const ROLE_META = [
  { key: 'wolf',     label: '狼人',   hint: '每晚刀人',       min: 1, max: 5 },
  { key: 'seer',     label: '预言家', hint: '每晚验一人',     min: 0, max: 2 },
  { key: 'witch',    label: '女巫',   hint: '一解药一毒药',   min: 0, max: 2 },
  { key: 'hunter',   label: '猎人',   hint: '出局可开枪',     min: 0, max: 2 },
  { key: 'guard',    label: '守卫',   hint: '每晚守一人挡刀', min: 0, max: 1 },
  { key: 'knight',   label: '骑士',   hint: '白天翻牌决斗',   min: 0, max: 1 },
  { key: 'idiot',    label: '白痴',   hint: '被票免死没票权', min: 0, max: 1 },
  { key: 'villager', label: '平民',   hint: '无能力',         min: 0, max: 9, auto: true },
];
// 要和后端 PRESETS 保持一致（backend/games/werewolf.py）。
// 神职数量压着平民数量走，神多民少的板子实测不好玩。
const WOLF_PRESETS = {
  6: { wolf: 2, seer: 1, witch: 1 },
  7: { wolf: 2, seer: 1, witch: 1, hunter: 1 },
  8: { wolf: 2, seer: 1, witch: 1, guard: 1 },
  9: { wolf: 3, seer: 1, witch: 1, hunter: 1 },
  10: { wolf: 3, seer: 1, witch: 1, hunter: 1, guard: 1 },
  11: { wolf: 4, seer: 1, witch: 1, guard: 1, idiot: 1 },
  12: { wolf: 4, seer: 1, witch: 1, hunter: 1, knight: 1 },
};

function showSetup(kind) {
  S.kind = kind;
  const isWolf = kind === 'werewolf';
  $('setupTitle').textContent = isWolf ? '狼人杀 · 开局配置' : '谁是卧底 · 开局配置';
  $('setupDesc').textContent = isWolf
    ? '选人数和阵营。桌上一律用「N号」互相称呼，你的座位号是随机的。'
    : '选人数。8 人以上会放两个卧底——单卧底在大局里几乎必死。没人会被告知自己是不是卧底。';
  $('rolesBox').classList.toggle('hidden', !isWolf);

  const r = $('pcRange');
  r.min = isWolf ? 6 : 5;
  r.max = isWolf ? 12 : 10;
  r.value = isWolf ? 9 : 8;
  S.roles = isWolf ? { ...WOLF_PRESETS[9] } : null;

  const sync = () => {
    const n = +r.value;
    $('pcVal').textContent = n;
    if (isWolf) renderRoleCfg(n);
    else $('cfgSummary').textContent = '';
  };
  r.oninput = sync;
  $('btnSetupPreset').classList.toggle('hidden', !isWolf);
  $('btnSetupPreset').onclick = () => { S.roles = { ...WOLF_PRESETS[+r.value] }; sync(); };
  $('btnSetupGo').onclick = () => startGame({
    kind, player_count: +r.value, roles: isWolf ? S.roles : null,
  });
  sync();
  view('setup');
}

function renderRoleCfg(n) {
  const cfg = S.roles || (S.roles = { ...WOLF_PRESETS[n] });
  const fixed = ROLE_META.filter((m) => !m.auto)
    .reduce((a, m) => a + (cfg[m.key] || 0), 0);
  const villagers = Math.max(0, n - fixed);

  $('roleCfg').innerHTML = ROLE_META.map((m) => {
    const v = m.auto ? villagers : (cfg[m.key] || 0);
    return `<div class="roleCfgItem ${m.auto ? 'auto' : ''}">
      <div class="rcTop"><b>${esc(m.label)}</b><span class="muted">${esc(m.hint)}</span></div>
      <div class="rcCtl">
        ${m.auto ? '' : `<button class="stp" data-k="${m.key}" data-d="-1" aria-label="减少">−</button>`}
        <span class="rcNum ${m.auto ? 'dim' : ''}">${v}</span>
        ${m.auto ? '' : `<button class="stp" data-k="${m.key}" data-d="1" aria-label="增加">+</button>`}
      </div>
    </div>`;
  }).join('');

  for (const b of $('roleCfg').querySelectorAll('.stp')) {
    b.onclick = () => {
      const m = ROLE_META.find((x) => x.key === b.dataset.k);
      const next = (cfg[m.key] || 0) + (+b.dataset.d);
      if (next < m.min || next > m.max) return;
      const otherFixed = ROLE_META.filter((x) => !x.auto && x.key !== m.key)
        .reduce((a, x) => a + (cfg[x.key] || 0), 0);
      if (otherFixed + next > n) return;      // 神职+狼不能挤掉所有平民
      cfg[m.key] = next;
      renderRoleCfg(n);
    };
  }

  const good = n - (cfg.wolf || 0);
  $('cfgSummary').innerHTML =
    `狼 ${cfg.wolf} 人 vs 好人 ${good} 人　·　平民由剩余人数自动补齐` +
    (cfg.wolf >= good ? '　<b style="color:var(--danger)">⚠ 狼已经不少于好人，开局即结束</b>' : '');
  $('btnSetupGo').disabled = cfg.wolf >= good;
}

// ── 剧本杀：选本 / 选角色 ─────────────────────────────
async function loadScripts() {
  S.kind = 'mystery';
  $('scriptsTitle').textContent = '选一个本';
  $('btnGen').textContent = '✨ AI 生成新本';
  S.scripts = await api('/api/scripts');
  renderScriptFilters();
  renderScriptCards();
  view('scripts');
}

const FILTERS = [
  ['all', '全部', () => true],
  ['thick', '厚本', (s) => s.acts >= 3 && !s.community],
  ['ai', 'AI 生成', (s) => s.community],
  ['hard', '硬核', (s) => s.difficulty === 'hard'],
];
S.scriptFilter = 'all';

function renderScriptFilters() {
  const n = (f) => S.scripts.filter(f[2]).length;
  $('scriptFilters').innerHTML = FILTERS.filter((f) => f[0] === 'all' || n(f))
    .map((f) => `<button class="chip ${S.scriptFilter === f[0] ? 'on' : ''}" data-f="${f[0]}">${f[1]}<i>${n(f)}</i></button>`)
    .join('');
  for (const b of document.querySelectorAll('#scriptFilters .chip')) {
    b.onclick = () => { S.scriptFilter = b.dataset.f; renderScriptFilters(); renderScriptCards(); };
  }
}

function renderScriptCards() {
  const diff = { easy: '简单', medium: '中等', hard: '硬核' };
  const f = FILTERS.find((x) => x[0] === S.scriptFilter) || FILTERS[0];
  const list = S.scripts.filter(f[2]);
  $('scriptCards').innerHTML = list.length ? list.map((s) => `
    <div class="card scriptCard d-${esc(s.difficulty)}" data-id="${esc(s.script_id)}" role="button" tabindex="0">
      <h3>${esc(s.title)}</h3>
      <div class="tagline">${esc(s.tagline)}</div>
      <div class="metaRow">
        <span class="tag ${esc(s.difficulty)}">${diff[s.difficulty] || s.difficulty}</span>
        <span class="tag">${s.suspect_count || s.player_count} 人${
          s.player_count > (s.suspect_count || s.player_count)
            ? `＋${s.player_count - s.suspect_count} 证人` : ''}</span>
        ${s.acts >= 3 ? '<span class="tag actTag">三幕</span>' : ''}
        ${(s.tags || []).filter((t) => !['三幕', '硬核', '简单', '中等'].includes(t)).slice(0, 5).map((t) => `<span class="tag">${esc(t)}</span>`).join('')}
        <span class="tag">约 ${s.estimated_minutes} 分钟</span>
        ${s.community ? '<span class="tag commTag">社区投稿</span>' : ''}
      </div>
      ${s.author ? `<div class="byline">作者　${esc(s.author)}</div>` : ''}
      <div class="avRow">${(s.roles || []).map((r) =>
        av(r.id, 40, '', `/api/avatar/${s.script_id}/${r.id}.svg`)).join('')}</div>
    </div>`).join('') : '<p class="muted emptyNote">这个分类下还没有本子。点右上角「✨ AI 生成新本」现写一个。</p>';
  for (const c of document.querySelectorAll('#scriptCards .card')) {
    clickable(c, () => showRoles(c.dataset.id));
  }
}

/** 探案选案件。以前只有一个案件所以直接开局，现在得让人挑。 */
async function loadCases() {
  S.kind = 'detective';
  const cases = await api('/api/cases');
  $('scriptsTitle').textContent = '选一个案子';
  $('btnGen').textContent = '✨ AI 生成新案子';
  $('scriptFilters').innerHTML = '';
  $('scriptCards').innerHTML = cases.map((c) => `
    <div class="card scriptCard d-medium" data-id="${esc(c.case_id)}" role="button" tabindex="0">
      <h3>${esc(c.title)}</h3>
      <div class="tagline">${esc(c.tagline || '')}</div>
      <div class="metaRow">
        <span class="tag">死者 ${esc(c.victim || '—')}</span>
        <span class="tag">${c.scene_count || ''} 个场景</span>
        <span class="tag">${c.people_count || ''} 个人可以问</span>
        ${c.evidence_count ? `<span class="tag">${c.evidence_count} 件物证</span>` : ''}
        ${c.author ? '<span class="tag commTag">AI 生成</span>' : ''}
      </div>
    </div>`).join('');
  for (const el of document.querySelectorAll('#scriptCards .card')) {
    clickable(el, () => startGame({ kind: 'detective', case_id: el.dataset.id }));
  }
  view('scripts');
}

async function showRoles(scriptId) {
  const s = await api('/api/scripts/' + scriptId);
  S.scriptId = scriptId;
  S.charId = null;
  $('rolesTitle').textContent = s.title;
  $('rolesBg').textContent = s.background;
  // 证人（NPC）不能扮演——他们不投票也没有个人目标，选了只能干坐着。
  // 但要让玩家知道场上有这么几个人可以问，所以单列在下面。
  const pickable = s.roles.filter((r) => !r.is_npc);
  const witnesses = s.roles.filter((r) => r.is_npc);
  $('roleGrid').innerHTML = pickable.map((r) => `
    <div class="roleCard" data-id="${esc(r.id)}" role="radio" tabindex="0" aria-checked="false">
      ${av(r.id, 56, '', `/api/avatar/${scriptId}/${r.id}.svg`)}
      <div class="txt">
        <div class="nm">${esc(r.name)}</div>
        <div class="rl">${esc(r.public_role)}</div>
      </div>
    </div>`).join('') + (witnesses.length ? `
    <div class="witnessNote">在场证人（不能扮演，但可以问话）：${
      witnesses.map((r) => `${esc(r.name)}（${esc(r.public_role)}）`).join('、')}</div>` : '');
  for (const el of document.querySelectorAll('.roleCard')) {
    clickable(el, () => {
      for (const x of document.querySelectorAll('.roleCard')) {
        x.classList.remove('sel'); x.setAttribute('aria-checked', 'false');
      }
      el.classList.add('sel'); el.setAttribute('aria-checked', 'true');
      S.charId = el.dataset.id;
      $('btnStart').disabled = false;
    });
  }
  $('btnStart').disabled = true;
  view('roles');
}

// ── 开局 / 续玩 ───────────────────────────────────────
async function startGame(opts) {
  try {
    const body = { kind: opts.kind };
    if (opts.kind === 'mystery') {
      body.script_id = S.scriptId;
      body.player_char_id = S.charId;
    } else {
      if (opts.player_count) body.player_count = opts.player_count;
      if (opts.roles) body.roles = opts.roles;
      // 探案选了哪个案子。不透传的话永远开第一个案子，
      // 案件列表就白做了。
      if (opts.case_id) body.case_id = opts.case_id;
    }
    enterGame(await api('/api/g', { method: 'POST', body: JSON.stringify(body) }), true);
  } catch (e) { showErr('开局失败：' + e.message); }
}

async function resume(gid) {
  try { enterGame(await api('/api/g/' + gid), true); }
  catch (e) { showErr('读取存档失败：' + e.message); }
}

function enterCase(st) {
  S.kind = 'detective';
  S.game = st;
  detective.init({ api, showErr, view });
  bgm.play(bgm.defaultFor('detective', st.case_id));
  renderBgmMenu();
  detective.enter(st);
}

function enterGame(st, fresh) {
  if (st.kind === 'detective') return enterCase(st);
  S.kind = st.kind;
  S.game = st;
  S.scriptId = st.script_id || S.scriptId;
  if (fresh) { S.seen = 0; $('log').innerHTML = ''; }
  $('brandTitle').textContent = st.kind_label || 'AI 桌游厅';
  bgm.play(bgm.defaultFor(st.kind, st.script_id));
  renderBgmMenu();
  $('btnBgm').classList.toggle('on', bgm.currentTrack() !== 'none');
  view('game');
  applyState(st);
  pinBottom();   // 续玩要停在断点，不是开局
  pump();
}

// ── 日志渲染 + 自动滚动 ───────────────────────────────
function nearBottom(el, slack = 90) {
  return el.scrollHeight - el.scrollTop - el.clientHeight < slack;
}

function renderEvent(e) {
  const g = S.game || {};
  const meId = g.human_id || g.player_char_id;
  const me = e.speaker_id ? e.speaker_id === meId
                          : e.speaker_name === (g.human_name || g.player_name);
  switch (e.kind) {
    case 'narration':
      return `<div class="ev narration">${esc(e.text)}</div>`;
    case 'system':
      return `<div class="ev system"><span>${esc(e.text)}</span></div>`;
    case 'elimination':
      return `<div class="ev elimination">${esc(e.text)}</div>`;
    case 'private': {
      // 狼人杀那边的 private 是"只有你知道"的单向告知，没有 target_name；
      // 剧本杀的私聊是一对一往返，要按对话渲染。
      if (!e.target_name) {
        return `<div class="ev private"><div class="hd">只有你知道</div>${esc(e.text)}</div>`;
      }
      const sid = e.speaker_id || idOf(e.speaker_name);
      return `<div class="ev privTalk ${me ? 'mine' : ''}">
        ${sid ? av(sid, 40, me ? 'me' : '') : ''}
        <div class="body">
          <div class="who">${esc(e.speaker_name)}${me ? '（你）' : ''}
            <span class="to">→ ${esc(e.target_name)}</span>
            <span class="seal">密谈</span></div>
          <div class="txt">${esc(e.text)}</div>
        </div></div>`;
    }
    case 'private_meta':
      return `<div class="ev privMeta">${esc(e.text)}</div>`;
    case 'act_open':
      // 新一幕发给「你」的那一页。这是整局最重要的时刻之一：
      // 它往往会推翻你前面说过的话，得让人一眼看到，不能混在发言流里划过去。
      return `<div class="ev actOpen">
        <div class="aoHd"><span>第${e.act}幕</span>${esc(e.title || '你的剧本多了一页')}</div>
        <div class="aoTxt">${esc(e.text).replace(/\n/g, '<br>')}</div>
        <div class="aoFt">这一页只有你看得到。左边「你的剧本」里也已经补上了。</div></div>`;
    case 'deduce':
      // 线索自己拼出了新东西——探案里最爽的那一下，剧本杀这边只有你自己看得见
      return `<div class="ev deduceEv">
        <div class="dvHd">💡 你把几条线索对到了一起</div>
        <div class="dvNote">${esc(e.text || '').replace(/\n/g, '<br>')}</div>
        <div class="dvGot"><b>得出：</b>${esc(e.clue_text || '')}</div></div>`;
    case 'interlude':
      return `<div class="ev interlude">
        <div class="ilHd"><span>幕间</span>${esc(e.title || '')}</div>
        <div class="ilTxt">${esc(e.text).replace(/\n/g, '<br>')}</div></div>`;
    case 'speech': {
      const sid = e.speaker_id || idOf(e.speaker_name);
      // 遗言单独标一下。这是全场信息量最大的一句，混在普通发言里会被划过去。
      const lw = e.last_words;
      return `<div class="ev speech ${me ? 'mine' : ''} ${lw ? 'lastWords' : ''}">
        ${sid ? av(sid, 46, me ? 'me' : '') : ''}
        <div class="body">
          <div class="who ${me ? 'me' : ''}">${esc(e.speaker_name)}${me ? '（你）' : ''}${
            lw ? '<span class="lwTag">遗言</span>' : ''}</div>
          <div class="txt">${esc(e.text)}</div>
        </div></div>`;
    }
    case 'clue_revealed': {
      const sid = e.speaker_id || idOf(e.speaker_name);
      return `<div class="ev clue">
        <div class="hd">${sid ? av(sid, 28) : ''}${esc(e.speaker_name)} 公开了线索 · ${esc(e.location || '')}</div>
        <div class="txt">${esc(e.text)}</div></div>`;
    }
    case 'vote': {
      const sid = e.speaker_id || idOf(e.speaker_name);
      return `<div class="ev vote">${sid ? av(sid, 28) : ''}<span>${esc(e.speaker_name)} 投给 <b>${
        esc(e.target_name)}</b>${e.text ? '　—— ' + esc(e.text) : ''}</span></div>`;
    }
    default: return '';
  }
}

function appendLog(events) {
  const box = $('log');
  speakNew(events);
  const stick = nearBottom(box);
  let added = 0;
  for (const e of events || []) {
    if (e.seq <= S.seen) continue;
    S.seen = e.seq;
    box.insertAdjacentHTML('beforeend', renderEvent(e));
    added++;
  }
  if (!added) return;
  if (stick) pinBottom(true);
  else $('btnToBottom').classList.remove('hidden');
}

/** 滚到底，并且扛得住异步撑高。
 *
 * 头像是 <img> 异步加载的：滚动发生的那一刻 scrollHeight 还没把它们算进去，
 * 于是"滚到底"滚了个寂寞，图片一到位内容撑开，日志就卡在半截。
 * 续玩存档时最明显——12 张头像，一路停在开局第一行。
 * 所以除了当帧滚一次，还要等图片 load 和布局落定各补一次。
 */
function pinBottom(smooth = false) {
  const box = $('log');

  /** 瞬时置底。
   *
   * CSS 里 .logScroll 带 scroll-behavior:smooth，会把每次滚动变成动画，
   * 而动画由合成器驱动——标签页在后台、或页面所在面板不参与合成时，
   * 它一帧都不推进，日志就永久卡在原地。
   * 而且实测有的引擎**不认** scrollTo({behavior:'auto'}) 这个显式覆盖，
   * 所以只能临时改内联样式，这是唯一稳的做法。
   */
  const jump = () => {
    const prev = box.style.scrollBehavior;
    box.style.scrollBehavior = 'auto';
    box.scrollTop = box.scrollHeight;
    box.style.scrollBehavior = prev;
  };

  const go = smooth
    ? () => box.scrollTo({ top: box.scrollHeight, behavior: 'smooth' })
    : jump;

  requestAnimationFrame(go);

  // 头像是异步加载的：滚动发生的那一刻 scrollHeight 还没把它们算进去，
  // 图片一到位内容撑开，日志又落在半截。等它们落地再补一次。
  const pending = [...box.querySelectorAll('img')].filter((i) => !i.complete);
  let left = pending.length;
  for (const img of pending) {
    const done = () => { if (--left <= 0) go(); };
    img.addEventListener('load', done, { once: true });
    img.addEventListener('error', done, { once: true });
  }

  // 安全网：动画播不动（或被打断）时兜底硬滚。
  // 自动滚动是明确需求，宁可牺牲动效也不能让它停在半路。
  setTimeout(() => { if (!nearBottom(box, 4)) jump(); }, smooth ? 700 : 320);

  $('btnToBottom').classList.add('hidden');
}

// ── 状态应用 ──────────────────────────────────────────
function applyState(st) {
  S.game = st;
  const ph = st.phase || '';
  const isNight = ph.startsWith('night') || ph === 'hunter_shoot';
  document.body.classList.toggle('night', isNight);
  const pb = $('phaseBadge');
  pb.textContent = st.phase_label || ph || '';
  pb.className = 'badge ' + (st.kind === 'werewolf' ? (isNight ? 'night' : 'day') : '');
  // 卧底用对照板取代聊天流；其余游戏照旧
  const boardMode = st.kind === 'spy' && st.board;
  // 剧本杀多一个「时间线板」，和记录切换。八人本四十格行踪，
  // 光靠往上翻聊天流根本理不清谁什么时候在哪。
  const hasTL = st.kind === 'mystery' && st.board;
  $('midTabs').classList.toggle('hidden', !hasTL);
  if (hasTL) renderTimeline(st);
  $('tlBoard').classList.toggle('hidden', !(hasTL && S.midTab === 'board'));
  $('spyBoard').classList.toggle('hidden', !boardMode);
  $('log').classList.toggle('hidden', boardMode || (hasTL && S.midTab === 'board'));
  $('btnToBottom').classList.toggle('hidden', boardMode || $('btnToBottom').classList.contains('hidden'));
  if (boardMode) renderSpyBoard(st);
  else appendLog(st.log || st.public_log);
  renderBrief(st);
  renderRoster(st);
  renderSide(st);
  if (st.finished && st.result) { renderReveal(st); return; }
  renderAction(st);
}

/** 剧本杀的时间线整理板。
 *
 * 格子分两种：**确凿**（来自你自己的剧本——你的行踪、你能为谁作证）
 * 和**空白**（只能靠听来的，你自己记）。
 * 后端只下发这两类，别人的真实行踪一个字都不给——
 * 给了就等于把本子的答案摊开。
 */
function renderTimeline(st) {
  const b = st.board;
  const box = $('tlBoard');
  const filled = b.rows.reduce((a, r) => a + r.cells.filter((c) => c.note).length, 0);
  $('noteCount').textContent = filled ? `· ${filled}` : '';

  // 重绘会丢掉正在输入的焦点，所以只在结构变了（人数/时段变了）时重建
  const sig = `${b.rows.length}x${b.slots.length}`;
  if (box.dataset.sig !== sig) {
    box.dataset.sig = sig;
    box.innerHTML = `
      <p class="tlHint">灰色是你剧本里写死的、确凿的；空格自己填。
        只有你看得到，也不会被 AI 读到。</p>
      <table class="tlTable">
        <thead><tr><th class="tlWho">　</th>
          ${b.slots.map((s) => `<th>${esc(s)}</th>`).join('')}</tr></thead>
        <tbody>
          ${b.rows.map((r) => `
            <tr class="${r.is_me ? 'me' : ''}">
              <th class="tlWho">${av(r.id, 28)}
                <span><b>${esc(r.name)}</b><i>${esc(r.role || '')}</i></span></th>
              ${r.cells.map((c) => `
                <td>${c.fact
                  ? `<div class="tlFact">${esc(c.fact)}</div>`
                  : `<textarea class="tlNote" rows="2" placeholder="…"
                       data-k="${esc(r.id)}|${esc(c.slot)}">${esc(c.note)}</textarea>`}</td>`).join('')}
            </tr>`).join('')}
        </tbody>
      </table>`;

    for (const ta of box.querySelectorAll('.tlNote')) {
      let timer = null;
      ta.oninput = () => {
        clearTimeout(timer);
        // 防抖：每敲一个字就存一次会把服务端打爆
        timer = setTimeout(() => saveNote(ta.dataset.k, ta.value), 600);
      };
      ta.onblur = () => { clearTimeout(timer); saveNote(ta.dataset.k, ta.value); };
    }
  }
}

async function saveNote(key, text) {
  try {
    const r = await api(`/api/g/${S.game.game_id}/note`, {
      method: 'POST', body: JSON.stringify({ key, text }),
    });
    $('noteCount').textContent = r.count ? `· ${r.count}` : '';
  } catch { /* 笔记存不上不该打断游戏 */ }
}

/** 把新到的发言念出来。只念别人的——自己刚打的字不用再读给自己听。 */
function speakNew(events) {
  if (!voice.enabled()) return;
  const g = S.game || {};
  const meId = g.human_id || g.player_char_id;
  const list = g.players || g.roster || [];
  for (const e of events || []) {
    if (e.kind !== 'speech' || !e.text) continue;
    const sid = e.speaker_id || idOf(e.speaker_name);
    if (!sid || sid === meId) continue;
    const idx = Math.max(0, list.findIndex((p) => p.id === sid));
    const p = list[idx] || {};
    voice.speak(e.text, sid, idx, p.speech_style || p.style || '', p.personality || '', p.voice_hint || '');
  }
}

/** 只处理 **加粗**，用于短句。整段 markdown 渲染用 mdToHtml。 */
function mdInline(t) {
  return esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
}

/** 谁是卧底的描述对照板。
 *
 * 一人一行，一轮一列。这游戏的全部推理动作就是「横着扫一眼，
 * 谁那句话套不上我手里的词」——聊天流把这件事变得极难做，
 * 你得往上翻好几屏才能凑齐同一轮的六句话。
 */
function renderSpyBoard(st) {
  const b = st.board;
  const box = $('spyBoard');
  const rs = b.rounds;
  const evs = (st.log || []).filter((e) => ['narration', 'elimination', 'system'].includes(e.kind));
  const last = evs[evs.length - 1];
  // 出局时后端会私发一条「你是不是卧底」。对照板模式下日志是隐藏的，
  // 所以这条要单独捞出来常驻在顶上，否则玩家永远看不到自己的身份。
  const reveal = (st.log || []).filter((e) => e.kind === 'private' && !e.target_name).pop();

  box.innerHTML = `
    ${reveal ? `<div class="sbReveal">${mdInline(reveal.text)}</div>` : ''}
    ${last ? `<div class="sbNote">${esc(last.text)}</div>` : ''}
    <table class="sbTable">
      <thead><tr><th class="sbWho">　</th>
        ${rs.map((r) => `<th class="${r === b.current_round ? 'now' : ''}">第 ${r} 轮</th>`).join('')}
      </tr></thead>
      <tbody>
        ${b.rows.map((p) => `
          <tr class="${p.alive ? '' : 'out'} ${p.is_human ? 'me' : ''} ${p.in_pk ? 'pk' : ''}">
            <th class="sbWho">
              ${av(p.id, 28, p.alive ? '' : 'dead')}
              <span><b>${esc(p.name)}</b>${p.is_human ? '<i>你</i>' : ''}
              ${p.alive ? '' : `<i class="outTag">第${p.out_round}轮出局</i>`}</span>
            </th>
            ${rs.map((r) => {
              const t = p.said[String(r)];
              const speaking = b.speaking === p.id && r === b.current_round;
              return `<td class="${t ? '' : 'empty'} ${speaking ? 'speaking' : ''}">${
                t ? esc(t) : (speaking ? '<span class="dots">正在想…</span>' : '')}</td>`;
            }).join('')}
          </tr>`).join('')}
      </tbody>
      ${votesRow(b, rs)}
    </table>`;
}

/** 票型行。
 *
 *  谁投了谁，原来只写在日志里——可卧底这边走的是对照板、日志是隐藏的，
 *  于是整局下来玩家连票型都看不见。在这个游戏里票型本身就是线索：
 *  两个人连着投同一个人，要么真看出来了，要么是心虚的在互相打掩护。
 */
function votesRow(b, rs) {
  const v = b.votes || {};
  if (!Object.keys(v).length) return '';
  return `<tfoot><tr class="sbVotes">
      <th class="sbWho"><span><b>投票</b></span></th>
      ${rs.map((r) => {
        const list = v[String(r)] || [];
        if (!list.length) return '<td class="empty"></td>';
        const tally = {};
        for (const x of list) tally[x.to] = (tally[x.to] || 0) + 1;
        const top = Object.entries(tally).sort((a, c) => c[1] - a[1]);
        return `<td>
          <div class="vTally">${top.map(([n, c]) =>
            `<span><b>${esc(n)}</b> ${c}票</span>`).join('')}</div>
          <div class="vWho">${list.map((x) =>
            `${esc(x.from)}→${esc(x.to)}`).join('　')}</div>
        </td>`;
      }).join('')}
    </tr></tfoot>`;
}

function renderBrief(st) {
  $('briefTitle').textContent = st.brief_title || (st.kind === 'mystery' ? '你的剧本' : '你的身份');
  $('brief').innerHTML = mdToHtml(st.brief || '');
}

const ROLE_CLS = {
  wolf: 'rWolf', seer: 'rSeer', witch: 'rWitch', hunter: 'rHunter',
  guard: 'rGuard', knight: 'rKnight', idiot: 'rIdiot', villager: 'rVill',
};

function renderRoster(st) {
  const list = st.players || st.roster || [];
  const meId = st.human_id || st.player_char_id;
  // 出局后开天眼：后端只在真人已出局时才下发 god_view
  const god = {};
  for (const g of st.god_view || []) god[g.id] = g;
  $('roster').innerHTML = list.map((p) => `
    <div class="rosterItem ${p.alive === false ? 'dead' : ''}">
      ${av(p.id, 40, (p.id === meId ? 'me ' : '') + (p.alive === false ? 'dead' : ''))}
      <div style="min-width:0">
        <div class="nm">${esc(p.name)}${god[p.id]
          ? `<span class="godRole ${ROLE_CLS[god[p.id].role] || ''}">${esc(god[p.id].role_label)}</span>` : ''}</div>
        <div class="rl">${esc(p.public_role || p.out_reason || '')}</div>
        ${p.display_name && p.display_name !== p.name
          ? `<div class="realname">${esc(p.display_name)}</div>` : ''}
      </div>
      ${p.id === meId ? '<span class="meTag">你</span>'
        : p.is_npc ? '<span class="npcTag">证人</span>'
        : (p.alive === false ? '<span class="outTag">出局</span>' : '')}
    </div>`).join('');
}

/** 右栏下半：剧本杀放线索，卧底放我的词，狼人杀放身份 */
function renderSide(st) {
  const box = $('sideExtra');
  if (st.kind === 'mystery') {
    const cl = st.my_clues || [];
    box.innerHTML = '<h4 style="margin-top:20px">我的线索</h4>' + (cl.length
      ? cl.map((c) => `
        <div class="clueItem ${c.synth ? 'synth' : ''}">
          <div class="loc">${c.synth ? '💡 ' : ''}${esc(c.location)}</div>
          <div class="tx">${esc(c.text)}</div>
          <label><input type="checkbox" class="revealChk" value="${esc(c.id)}"> 发言时公开</label>
        </div>`).join('')
      : '<p class="muted">还没搜到线索。</p>');
  } else if (st.kind === 'spy') {
    box.innerHTML = `<div class="myCard">
      <div class="lb">你的词</div><div class="val">${esc(st.my_word || '')}</div>
      <div class="sub">没人知道自己是不是卧底</div></div>`;
  } else if (st.kind === 'werewolf') {
    let extra = '';
    if (st.wolf_mates?.length) {
      extra = `<div class="sub">同伴：${st.wolf_mates.map((m) => esc(m.name)).join('、')}</div>`;
    } else if (st.my_role === 'seer' && st.seer_results?.length) {
      extra = '<div class="sub">' + st.seer_results.map((r) =>
        `${esc(r.name)}→${esc(r.result)}`).join('　') + '</div>';
    } else if (st.my_role === 'witch') {
      extra = `<div class="sub">解药 ${st.antidote ? '✅' : '❌'}　毒药 ${st.poison ? '✅' : '❌'}</div>`;
    }
    box.innerHTML = `<div class="myCard">
      <div class="lb">你的身份</div><div class="val">${esc(st.my_role_label || '')}</div>
      ${extra}</div>`;
  } else box.innerHTML = '';
}

function pickedClues() {
  return [...document.querySelectorAll('.revealChk:checked')].map((c) => c.value);
}

// ── 行动区 ────────────────────────────────────────────
function personPills(list, withAvatar = true) {
  return list.map((p) => `<span class="pill ${withAvatar ? '' : 'plain'}" data-id="${esc(p.id)}"
    role="radio" tabindex="0" aria-checked="false">${withAvatar ? av(p.id, 28) : ''}${esc(p.name)}</span>`).join('');
}

function bindPick(scope, onPick) {
  let picked = null;
  for (const p of scope.querySelectorAll('.pill[data-id]')) {
    clickable(p, () => {
      for (const x of scope.querySelectorAll('.pill[data-id]')) {
        x.classList.remove('sel'); x.setAttribute('aria-checked', 'false');
      }
      p.classList.add('sel'); p.setAttribute('aria-checked', 'true');
      picked = p.dataset.id;
      onPick(picked);
    });
  }
  return () => picked;
}

function renderAction(st) {
  const box = $('action');

  // 断线优先于一切：这时候任何操作都发不出去，先让玩家看到怎么接上
  if (S.stalled) return renderStalled();

  // 出局了但局没完：你已经没有操作了，但局面还在走，让你看完。
  // 这一支必须排在 S.busy 之前——观战期间 pump 一直是 busy 状态，
  // 放后面的话永远只会显示「其他人正在思考」，观战条根本出不来。
  if (st.spectating && !st.finished) {
    box.innerHTML = `<div class="specBar">
        <span class="specDot"></span>
        <span class="specTxt"><b>你已出局</b>　局面还在继续，剩下的会按正常节奏放完。</span>
        <button id="specSkip" class="ghost">${S.skipToEnd ? '快进中…' : '直接看结局'}</button>
      </div>`;
    $('specSkip').onclick = () => {
      S.skipToEnd = true;
      renderAction(st);
      // pump 还在跑的话，它下一圈自己就不睡了；但要是它已经退出来了
      // （600 步的保险丝烧了、或者中途报过错），光重绘没用，得把它踢起来，
      // 否则这个按钮点了没反应，玩家只能看着一个不动的局面。
      if (!S.busy) pump();
    };
    return;
  }

  if (S.busy) { box.innerHTML = '<div class="thinking">其他人正在思考</div>'; return; }
  if (!st.waiting || !st.action) {
    box.innerHTML = st.finished ? '' : '<div class="thinking">其他人正在行动</div>';
    return;
  }
  const list = st.players || st.roster || [];
  const meId = st.human_id || st.player_char_id;
  const others = list.filter((p) => p.id !== meId && p.alive !== false);

  const textAction = (label, hint, type) => {
    box.innerHTML = `<div class="lbl">${label}</div>
      ${hint ? `<p class="muted">${hint}</p>` : ''}
      <textarea id="aTxt" rows="3" placeholder="说点什么…（Ctrl+Enter 发送）"></textarea>
      <div class="row"><button class="primary" id="aGo">发送</button>
        <button id="aHint" class="hintBtn">💡 给我点方向</button>
        <button id="aAuto" class="hintBtn">🎭 替我说</button></div>
      <div id="hintBox" class="hintBox hidden"></div>`;
    if (S.draft) $('aTxt').value = S.draft;   // 上一次没发出去，原样还给他
    $('aTxt').focus();
    $('aTxt').onkeydown = (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('aGo').click();
    };
    $('aGo').onclick = () => {
      const t = $('aTxt').value.trim();
      if (t) act({ type, text: t, reveal_clues: pickedClues() });
    };
    bindHint();
  };

  /** 骑士的翻牌决斗。挂在发言面板下面，而不是做成独立阶段——
   *  它本来就是「发言时可以顺手做的一件事」，且用不用完全看你。
   *  默认折叠：这是一次性的、可能直接把自己玩死的操作，不该一眼就点到。 */
  const appendDuel = () => {
    const d = document.createElement('div');
    d.className = 'duelBox';
    d.innerHTML = `<button id="aDuelOpen" class="hintBtn">⚔ 翻牌决斗（一局一次）</button>
      <div id="duelPanel" class="hidden" style="margin-top:10px">
        <p class="muted"><b>猜中狼</b>：他当场出局，今天不再投票。<br>
          <b>猜错了</b>：你自己出局，今天同样不再投票。<br>
          用掉它就等于放弃今天的一次放逐机会——没有把握就别开。</p>
        <div class="row" role="radiogroup">${personPills(others)}</div>
        <div class="row"><button class="primary" id="aDuelGo" disabled>就是他，开牌</button></div>
      </div>`;
    box.appendChild(d);
    $('aDuelOpen').onclick = () => $('duelPanel').classList.toggle('hidden');
    const get = bindPick($('duelPanel'), () => { $('aDuelGo').disabled = false; });
    $('aDuelGo').onclick = () => {
      const t = get();
      if (t && confirm(`向 ${(others.find((p) => p.id === t) || {}).name} 发起决斗？猜错你就出局了。`)) {
        act({ type: 'knight_duel', target_id: t });
      }
    };
  };

  /** 「替我说」——AI 用你自己的角色视角写一整句成稿，填进输入框。
   *
   * 走的是和 AI 角色发言同一个函数，只是换成你的视角，
   * 所以它说不出你不知道的事。
   * **只填不发**：最后按不按发送还是你决定——
   * 直接替你发出去，这局就不是你在玩了。
   */
  function bindAuto() {
    const btn = $('aAuto');
    if (!btn) return;
    btn.onclick = async () => {
      const ta = $('aTxt') || $('aQ');
      if (!ta) return;
      btn.disabled = true;
      const old = btn.textContent;
      btn.textContent = '正在替你想…';
      try {
        const r = await api(`/api/g/${S.game.game_id}/autospeak`, { method: 'POST' });
        if (r.text) {
          ta.value = r.text;
          ta.focus();
          // 光标移到末尾，方便直接改
          if (ta.setSelectionRange) ta.setSelectionRange(ta.value.length, ta.value.length);
          // AI 决定要亮哪几条线索，就把左栏对应的勾自动打上——
          // 不然它写了「我手里这条线索能说明…」，线索却没真的公开，话就落空了。
          const want = new Set(r.reveal_clues || []);
          let picked = 0;
          for (const c of document.querySelectorAll('.revealChk')) {
            if (want.has(c.value)) { c.checked = true; picked++; }
          }
          if (picked) showErr(`已替你勾上 ${picked} 条要公开的线索，发送前可以取消。`);
        } else {
          showErr('没想出来，你自己发挥吧。');
        }
      } catch (e) {
        showErr('代发言失败：' + e.message);
      } finally {
        btn.disabled = false;
        btn.textContent = old;
      }
    };
  }

  /** 「不知道说什么」——拉几个发言方向，点一条填进输入框。
   *
   * 刻意只填不发：提示是给你起个头，最后说什么、怎么说还是你决定。
   * 直接替你发出去的话，这局就不是你在玩了。
   */
  function bindHint() {
    bindAuto();
    const btn = $('aHint');
    if (!btn) return;
    btn.onclick = async () => {
      const hb = $('hintBox');
      btn.disabled = true;
      btn.textContent = '想想…';
      hb.classList.remove('hidden');
      hb.innerHTML = '<div class="thinking">正在替你想</div>';
      try {
        const { tips } = await api(`/api/g/${S.game.game_id}/hint`, { method: 'POST' });
        if (!tips || !tips.length) {
          hb.innerHTML = '<p class="muted">一时也没想出什么，你自己发挥吧。</p>';
        } else {
          hb.innerHTML = `<div class="hbHd">点一条填进输入框，再自己改</div>` +
            tips.map((t, i) => `<button class="hintItem" data-i="${i}">${esc(t)}</button>`).join('');
          for (const el of hb.querySelectorAll('.hintItem')) {
            el.onclick = () => {
              const ta = $('aTxt') || $('aQ') || $('askIn');
              if (!ta) return;
              ta.value = (ta.value ? ta.value.trimEnd() + '\n' : '') + tips[+el.dataset.i];
              ta.focus();
              hb.classList.add('hidden');
            };
          }
        }
      } catch (e) {
        hb.innerHTML = `<p class="muted">没要到提示：${esc(e.message)}</p>`;
      } finally {
        btn.disabled = false;
        btn.textContent = '💡 不知道说什么';
      }
    };
  }

  const votePanel = (label, hint, type) => {
    // 剧本杀的 NPC（证人）不参与投票——others 是 ask/private 也在用的共享列表，
    // 不能整体过滤掉 NPC（那样就问不到他们了），所以只在投票这一处单独筛。
    // 其他游戏没有 is_npc 字段，p.is_npc 是 undefined，filter 结果等于不过滤。
    const voteTargets = others.filter((p) => !p.is_npc);
    box.innerHTML = `<div class="lbl">${label}</div>
      ${hint ? `<p class="muted">${hint}</p>` : ''}
      <div class="row" role="radiogroup">${personPills(voteTargets)}</div>
      <div class="row"><button class="primary" id="aGo" disabled>确定</button></div>`;
    const get = bindPick(box, () => { $('aGo').disabled = false; });
    // 投票只选人，不填理由。后端三处都是 action.get("reason", "")，不传即可。
    $('aGo').onclick = () => act({ type, target_id: get() });
  };

  switch (st.action) {
    // ---- 剧本杀 ----
    case 'continue':
      box.innerHTML = `<div class="lbl">读本</div>
        <p class="muted">左边是你的剧本，读完再开始。里面有你的秘密、当晚行踪，
          以及<b>个人目标</b>——个人目标没达成的话，就算票对了凶手也不算完全胜利。</p>
        <div class="row"><button class="primary" id="aGo">读完了，开始</button></div>`;
      $('aGo').onclick = () => act({ type: 'continue' });
      break;
    case 'speak': {
      const ph = st.phase || '';
      const isIntro = ph.startsWith('intro');
      const isFinal = ph === 'final_words';
      textAction(
        isIntro ? '自我介绍' : isFinal ? '最后陈述' : '轮到你发言',
        isIntro ? '介绍你的公开身份、和死者的关系、当晚大致行踪。可以有所保留。'
          : isFinal ? '投票前最后的机会。把你的判断串成一条完整的线。'
            : '点名回应别人说过的话，说出你怀疑谁、依据是什么。',
        'speak');
      if (st.can_duel) appendDuel();
      break;
    }
    case 'answer': {
      const q = st.pending_question;
      box.innerHTML = `<div class="lbl">${esc(q?.asker_name || '')} 质问你</div>
        <div class="ev speech"><div class="body"><div class="txt">「${esc(q?.question || '')}」</div></div></div>
        <textarea id="aTxt" rows="3" placeholder="你怎么回应？"></textarea>
        <div class="row"><button class="primary" id="aGo">回答</button>
          <button id="aHint" class="hintBtn">💡 给我点方向</button>
          <button id="aAuto" class="hintBtn">🎭 替我说</button></div>
        <div id="hintBox" class="hintBox hidden"></div>`;
      if (S.draft) $('aTxt').value = S.draft;
      $('aTxt').focus();
      bindHint();
      $('aGo').onclick = () => {
        const t = $('aTxt').value.trim();
        if (t) act({ type: 'answer', text: t, reveal_clues: pickedClues() });
      };
      break;
    }
    case 'search':
      box.innerHTML = `<div class="lbl">搜证　剩余 ${st.searches_left} 次</div>
        <p class="muted">同一地点可反复搜，每次出一条新线索，越往后越难找。</p>
        <div class="row">${(st.map || []).map((m) =>
          `<span class="pill plain" data-loc="${esc(m)}" role="button" tabindex="0">${esc(m)}</span>`).join('')}</div>`;
      for (const p of box.querySelectorAll('.pill[data-loc]')) {
        clickable(p, () => act({ type: 'search', location: p.dataset.loc }));
      }
      break;
    case 'ask': {
      box.innerHTML = `<div class="lbl">自由质询　剩余 ${st.questions_left} 次</div>
        <div class="row" role="radiogroup">${personPills(others)}</div>
        <div style="margin-top:10px"><input id="aQ" placeholder="你要问他什么？"></div>
        <div class="row"><button class="primary" id="aGo" disabled>提问</button>
          <button id="aSkip">跳过</button>
          <button id="aHint" class="hintBtn">💡 问什么好</button></div>
        <div id="hintBox" class="hintBox hidden"></div>`;
      bindHint();
      const get = bindPick(box, () => { $('aGo').disabled = false; });
      $('aGo').onclick = () => {
        const q = $('aQ').value.trim();
        if (q) act({ type: 'ask', target_id: get(), question: q });
      };
      $('aSkip').onclick = () => act({ type: 'ask', skip: true });
      break;
    }
    case 'private': {
      // 私下出示线索和圆桌公开是两回事，所以这里单独给一组勾选，
      // 不复用左栏那个「发言时公开」——混用会让人误把底牌摊给全场。
      const mine = st.my_clues || [];
      box.innerHTML = `<div class="lbl">私下密谈　剩余 ${st.private_left ?? 0} 次</div>
        <p class="muted">拉一个人到角落说话。<b>内容只有你们俩知道</b>，
          但别人看得见你俩走开了。你可以试探、换情报、结盟，也可以骗他——
          当然他也可能转头就把你的话抖到台面上。</p>
        <div class="row" role="radiogroup">${personPills(others)}</div>
        <div style="margin-top:10px"><textarea id="aTxt" rows="2"
          placeholder="你想单独跟他说什么？"></textarea></div>
        ${mine.length ? `<div class="showClues">
          <div class="scHd">私下给他看（可不选）</div>
          ${mine.map((c) => `<label class="scItem">
            <input type="checkbox" class="showChk" value="${esc(c.id)}">
            <span>${esc(c.text)}<i>${esc(c.location)}</i></span></label>`).join('')}
        </div>` : ''}
        <div class="row"><button class="primary" id="aGo" disabled>去说</button>
          <button id="aSkip">不聊了</button>
          <button id="aHint" class="hintBtn">💡 私下聊什么</button>
          <button id="aAuto" class="hintBtn">🎭 替我说</button></div>
        <div id="hintBox" class="hintBox hidden"></div>`;
      if (S.draft) $('aTxt').value = S.draft;
      bindHint();
      const get = bindPick(box, () => { $('aGo').disabled = false; });
      $('aGo').onclick = () => {
        const t = $('aTxt').value.trim();
        if (!t) { $('aTxt').focus(); return; }
        act({
          type: 'private', target_id: get(), text: t,
          show_clues: [...box.querySelectorAll('.showChk:checked')].map((c) => c.value),
        });
      };
      $('aSkip').onclick = () => act({ type: 'private', skip: true });
      break;
    }

    // ---- 谁是卧底 ----
    case 'describe':
      textAction('轮到你描述',
        '用一句话形容你的词。<b>不能出现这个词的任何一个字</b>，也不能用谐音、拆字。', 'describe');
      break;

    // ---- 狼人杀 ----
    case 'wolf_talk': {
      const mates = st.wolf_mates || [];
      box.innerHTML = `<div class="lbl">🌙 狼队商议　第 ${st.wolf_round || 1} 轮</div>
        <p class="muted">同伴：${mates.map((m) => esc(m.name)).join('、') || '（无）'}　·
          这些话只有狼队听得见。<br>
          先商量，等会儿一起投——<b>票必须全队一致才算数</b>，不一致就再议一轮。</p>
        <textarea id="aTxt" rows="2" placeholder="跟同伴说点什么…（Ctrl+Enter 发送）"></textarea>
        <div class="row"><button class="primary" id="aGo">说</button>
          <button id="aHint" class="hintBtn">💡 给我点方向</button>
          <button id="aAuto" class="hintBtn">🎭 替我说</button></div>
        <div id="hintBox" class="hintBox hidden"></div>`;
      if (S.draft) $('aTxt').value = S.draft;
      $('aTxt').focus();
      $('aTxt').onkeydown = (e) => {
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) $('aGo').click();
      };
      $('aGo').onclick = () => {
        const t = $('aTxt').value.trim();
        if (t) act({ type: 'wolf_talk', text: t });
      };
      bindHint();
      break;
    }
    case 'wolf_kill': {
      const mates = st.wolf_mates || [];
      const targets = others.filter((p) => !mates.some((m) => m.id === p.id));
      box.innerHTML = `<div class="lbl">🌙 狼队表决　第 ${st.wolf_round || 1} 轮</div>
        <p class="muted">同伴：${mates.map((m) => esc(m.name)).join('、') || '（无）'}　·
          <b>全队投出同一个人才算数</b>，不一致就回去再议。</p>
        <div class="row" role="radiogroup">${personPills(targets)}</div>
        <div class="row"><button class="primary" id="aGo" disabled>确定</button></div>`;
      const get = bindPick(box, () => { $('aGo').disabled = false; });
      $('aGo').onclick = () => act({ type: 'wolf_kill', target_id: get() });
      break;
    }
    case 'hunter_shoot': {
      // 这个分支之前是漏的：真人当猎人被票出/被刀时，后端会停在
      // waiting=true / action=hunter_shoot 等操作，前端却落到 default 显示
      // 「等待中」——枪永远开不出去，pump 也不推进，整局就死在这儿。
      box.innerHTML = `<div class="lbl">🔫 你倒下前的最后一枪</div>
        <p class="muted">你是猎人。挑一个人带走，或者把枪咽回去。
          <br>枪一响，你带走的人立刻出局。</p>
        <div class="row" role="radiogroup">${personPills(others)}</div>
        <div class="row"><button class="primary" id="aGo" disabled>开枪</button>
          <button id="aSkip">不开枪</button></div>`;
      const get = bindPick(box, () => { $('aGo').disabled = false; });
      $('aGo').onclick = () => act({ type: 'hunter_shoot', target_id: get() });
      $('aSkip').onclick = () => act({ type: 'hunter_shoot', target_id: '' });
      break;
    }
    case 'last_words':
      textAction('⚰️ 你的遗言',
        '你出局了。这是最后一次说话的机会——把你知道的、你怀疑谁、依据是什么，'
        + '一次交代清楚。说完就再也插不上嘴了。', 'last_words');
      break;
    case 'guard_protect': {
      const gt = st.guard_targets || others;
      box.innerHTML = `<div class="lbl">🛡 守护一个人</div>
        <p class="muted">被你守的人今晚免于狼刀。不能守自己${
          st.last_guard_name ? `，也不能再守昨晚守过的 <b>${esc(st.last_guard_name)}</b>` : ''
        }。<br>
          <b>同守同救</b>：万一女巫今晚也用解药救了同一个人，他反而会死——两股力量顶在一起，谁也救不回来。</p>
        <div class="row" role="radiogroup">${personPills(gt)}</div>
        <div class="row"><button class="primary" id="aGo" disabled>守他</button>
          <button id="aSkip">今晚不守</button></div>`;
      const get = bindPick(box, () => { $('aGo').disabled = false; });
      $('aGo').onclick = () => act({ type: 'guard_protect', target_id: get() });
      $('aSkip').onclick = () => act({ type: 'guard_protect', target_id: '' });
      break;
    }
    case 'seer_check': {
      box.innerHTML = `<div class="lbl">🔮 查验身份</div>
        <p class="muted">选一个人，你会得知他是好人还是狼人。</p>
        <div class="row" role="radiogroup">${personPills(others)}</div>
        <div class="row"><button class="primary" id="aGo" disabled>查验</button></div>`;
      const get = bindPick(box, () => { $('aGo').disabled = false; });
      $('aGo').onclick = () => act({ type: 'seer_check', target_id: get() });
      break;
    }
    case 'witch_act': {
      const killed = st.night_kill_name;
      box.innerHTML = `<div class="lbl">🧪 女巫的夜晚</div>
        <p class="muted">${killed ? `今晚 <b>${esc(killed)}</b> 被刀了。` : '今晚没有人被刀。'}
          解药 ${st.antidote ? '可用' : '已用完'}，毒药 ${st.poison ? '可用' : '已用完'}。</p>
        <div class="row">
          ${st.antidote && killed ? '<button id="aSave" class="primary">用解药救他</button>' : ''}
          ${st.poison ? '<button id="aPoison">用毒药</button>' : ''}
          <button id="aPass">什么都不做</button>
        </div>
        <div id="poisonBox" class="hidden" style="margin-top:12px">
          <div class="row" role="radiogroup">${personPills(others)}</div>
          <div class="row"><button class="primary" id="aGo" disabled>确认下毒</button></div>
        </div>`;
      if ($('aSave')) $('aSave').onclick = () => act({ type: 'witch_act', action: 'save' });
      $('aPass').onclick = () => act({ type: 'witch_act', action: 'pass' });
      if ($('aPoison')) {
        $('aPoison').onclick = () => $('poisonBox').classList.remove('hidden');
        const get = bindPick($('poisonBox'), () => { $('aGo').disabled = false; });
        $('aGo').onclick = () => act({ type: 'witch_act', action: 'poison', target_id: get() });
      }
      break;
    }

    // ---- 推理答卷（分层真相）----
    case 'deduce': {
      // 投票之前先交一份答卷：谁先动的手、他怎么死的、为什么……各自判分。
      // 全是选择题，纯代码判分——这里不展示分值以外的任何提示，题目里也没有答案。
      const qs = st.questions || [];
      const picked = {};
      for (const q of qs) if (q.mine) picked[q.id] = q.mine;
      box.innerHTML = `<div class="lbl">📝 推理答卷</div>
        <p class="muted">投票之前，先写下你对这起案子的判断。每题只选一个；
          拿不准的可以留空（不扣分，也不得分）。<br>
          答对一半以上，才算把案子<b>还原</b>了——只靠蒙对凶手，最多是惨胜。</p>
        ${qs.map((q) => `
          <div class="tqBlock" data-q="${esc(q.id)}">
            <div class="tqPrompt">${esc(q.prompt)}<i>${q.points} 分</i></div>
            <div class="row" role="radiogroup">${q.options.map((o) => `
              <span class="pill plain ${picked[q.id] === o.id ? 'sel' : ''}"
                    data-q="${esc(q.id)}" data-o="${esc(o.id)}" role="radio" tabindex="0"
                    aria-checked="${picked[q.id] === o.id}">${esc(o.text)}</span>`).join('')}
            </div>
          </div>`).join('')}
        <div class="row"><button class="primary" id="aGo">交卷，去投票</button></div>`;
      for (const pill of box.querySelectorAll('.pill[data-q]')) {
        clickable(pill, () => {
          const qid = pill.dataset.q;
          for (const x of box.querySelectorAll(`.pill[data-q="${qid}"]`)) {
            x.classList.remove('sel'); x.setAttribute('aria-checked', 'false');
          }
          // 再点一次已选的 = 取消，回到「不答」
          if (picked[qid] === pill.dataset.o) { delete picked[qid]; return; }
          pill.classList.add('sel'); pill.setAttribute('aria-checked', 'true');
          picked[qid] = pill.dataset.o;
        });
      }
      $('aGo').onclick = () => act({ type: 'deduce', answers: { ...picked } });
      break;
    }

    // ---- 通用投票 ----
    case 'vote':
      votePanel(st.kind === 'werewolf' ? '投票放逐' : '投票', '票最多的人出局。', 'vote');
      break;

    default:
      box.innerHTML = '<div class="thinking">等待中</div>';
  }
}

// ── 推进循环 ──────────────────────────────────────────
async function act(payload) {
  if (S.busy) return;
  S.busy = true;
  // 网不好的时候这次请求会挂掉，然后整个面板重绘——玩家刚敲的那一大段
  // 发言就跟着没了。发之前先留一份底稿，失败了 textAction 会填回去。
  S.draft = typeof payload.text === 'string' ? payload.text : '';
  renderAction(S.game);
  try {
    const out = await api(`/api/g/${S.game.game_id}/act`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    S.busy = false;
    S.draft = '';
    applyState(out.state);
    pump();
  } catch (e) {
    S.busy = false;
    // 连不上和「这一步不合法」要分开处理：前者要给接续入口，
    // 后者是玩家操作问题，弹一下提示就够了。
    if (/连不上|超时|Failed to fetch|NetworkError|限流/.test(e.message)) {
      S.stalled = e.message;
    } else {
      showErr(e.message);
    }
    renderAction(S.game);
  }
}

/** 只要不轮到玩家、又没结束，就一直 step。 */
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function pump() {
  if (S.busy) return;
  S.busy = true;
  renderAction(S.game);
  try {
    let guard = 0;
    // 观战时局面还会走很久（整个白天+投票+可能好几轮），120 步不够
    while (!S.game.finished && !S.game.waiting && guard++ < 600) {
      const out = await api(`/api/g/${S.game.game_id}/step`, { method: 'POST' });
      S.game = out.state;
      // 卧底走对照板，日志元素是隐藏的——这里再往 #log 里塞就等于
      // 把每个人的描述写进看不见的地方，屏幕上什么都不会变。
      if (S.game.kind === 'spy' && S.game.board) renderSpyBoard(S.game);
      else appendLog(out.events);
      renderRoster(S.game);
      renderSide(S.game);
      renderBrief(S.game);
      $('phaseBadge').textContent = S.game.phase_label || S.game.phase || '';

      // 人出局后没有任何 waiting 点，循环会一路冲到底。
      // 这里主动停一拍，让剩下的局面按看得清的节奏放完。
      if (S.game.spectating && !S.skipToEnd) {
        renderAction(S.game);
        await sleep(1200);
      }
    }
  } catch (e) {
    // 断网时不能只弹个会自己消失的提示——玩家回头一看什么都没有，
    // 以为局废了。留一个常驻的重试入口，网好了点一下接着走。
    S.stalled = e.message;
  } finally {
    S.busy = false;
    S.skipToEnd = false;
    if (S.game.finished && S.game.result) renderReveal(S.game);
    else renderAction(S.game);
  }
}

/** 网络中断后的接续面板。 */
function renderStalled() {
  const box = $('action');
  box.innerHTML = `<div class="stallBar">
      <div class="stTxt"><b>连接中断了</b>
        <span>${esc(S.stalled || '')}</span>
        <i>这一局的进度已经存下来了，网络恢复后点下面接着走，什么都不会丢。</i></div>
      <div class="row">
        <button class="primary" id="stRetry">继续这一局</button>
        <button id="stHome">先回大厅</button>
      </div>
    </div>`;
  $('stRetry').onclick = async () => {
    $('stRetry').disabled = true;
    $('stRetry').textContent = '正在连…';
    try {
      // 先拉一次最新状态，避免本地和服务端不同步
      S.game = await api(`/api/g/${S.game.game_id}`);
      S.stalled = null;
      applyState(S.game);
      pump();
    } catch (err) {
      S.stalled = err.message;
      renderStalled();
    }
  };
  $('stHome').onclick = () => { S.stalled = null; loadHome(); };
}

// ── 复盘 ──────────────────────────────────────────────
function renderReveal(st) {
  const r = st.result;
  const win = r.human_win ?? r.caught;
  const title = r.verdict || (win ? '你赢了' : '你输了');
  // 四档判定各给一种颜色。塌成赢/输两色的话，「惨胜」和「彻底失败」
  // 看上去一模一样，玩家根本看不出个人目标到底有没有拖后腿。
  const TIER = { 完全胜利: 'win', 惨胜: 'partial', 虽败犹荣: 'honor', 彻底失败: 'lose' };
  const tier = TIER[title] || (win ? 'win' : 'lose');
  let h = `<div class="verdict ${tier}">
    <h2>${esc(title)}</h2><p>${esc(r.note || '')}</p></div>`;

  // 剧本杀：个人目标是胜负的一半，放在最前面
  if (r.goals?.length) {
    const lab = { conceal: '隐瞒', acquire: '取得', protect: '保护', expose: '揭穿' };
    h += `<h2 class="section">你的个人目标</h2><div class="panel">` +
      r.goals.map((g) => `
        <div class="goalRow ${g.achieved ? 'ok' : 'fail'}">
          <span class="gIcon">${g.achieved ? '✅' : '❌'}</span>
          <div style="min-width:0">
            <div><b>[${esc(lab[g.type] || g.type)}]</b> ${esc(g.text)}</div>
            <div class="muted" style="font-size:12.5px;margin-top:3px">${esc(g.reason || '')}</div>
          </div>
        </div>`).join('') +
      `<div class="tally">
        <span class="chip ${r.faction_win ? 'ok' : 'fail'}">
          ${r.faction_win ? '✓' : '✕'} 阵营目标</span>
        <span class="chip ${r.goals_all_done ? 'ok' : 'fail'}">
          ${r.goals_all_done ? '✓' : '✕'} 个人目标</span>
        <span class="muted">两者都满足才算完全胜利</span>
      </div></div>`;
  }

  // 全场每个人各自的输赢。阵营赢了不等于这个人赢了——
  // 秘密被扒光的凶手、把自己丑事抖干净的好人，都不算赢。
  if (r.everyone?.length) {
    h += `<h2 class="section">这一局谁真的赢了</h2>
      <p class="muted">每个人都有自己的个人目标。阵营赢 + 个人目标全达成，才算他赢。</p>
      <div class="wonList">` +
      r.everyone.map((p) => `
        <div class="wonRow ${p.won ? 'won' : 'lost'} ${p.is_me ? 'me' : ''}">
          ${av(p.id, 40)}
          <div class="wnMain">
            <div class="wnName">${esc(p.name)}
              ${p.is_me ? '<i class="meTag">你</i>' : ''}
              ${p.is_murderer ? '<i class="kTag">凶手</i>' : ''}</div>
            <div class="wnGoals">${(p.goals || []).map((g) =>
              `<span class="${g.achieved ? 'ok' : 'no'}">${g.achieved ? '✓' : '✕'} ${esc(g.text)}</span>`
            ).join('') || '<span class="muted">（无个人目标）</span>'}</div>
          </div>
          <div class="wnFlags">
            <span class="${p.faction_win ? 'ok' : 'no'}">阵营</span>
            <span class="${p.goals_all_done ? 'ok' : 'no'}">目标</span>
            <b>${p.won ? '赢' : '没赢'}</b>
          </div>
        </div>`).join('') + '</div>';
  }

  if (st.kind === 'spy') {
    h += `<h2 class="section">底牌</h2><div class="panel">
      <p>平民词：<b style="color:var(--accent)">${esc(r.civil_word)}</b>
         卧底词：<b style="color:var(--danger)">${esc(r.spy_word)}</b></p></div>`;
    h += '<h2 class="section">每个人拿到的词</h2>' + r.words.map((w) => `
      <div class="secret ${w.is_spy ? 'killer' : ''}">
        ${av(w.id, 46)}
        <div style="min-width:0">
          <div class="nm">${esc(w.name)}${w.is_spy ? '　← 卧底' : ''}</div>
          <div class="sc">拿到的词：<b>${esc(w.word)}</b>　${w.alive ? '存活' : '已出局'}</div>
        </div></div>`).join('');

    // 每个 AI 开口前对"我是不是那个例外"的私下判断。对局中严格保密。
    if (r.self_reads?.length) {
      const FIT = { 对不上: 'alarm', 对得上: 'calm', 说不准: 'unsure' };
      let last = 0;
      h += `<h2 class="section">他们心里怎么想的</h2>
        <p class="muted">每个 AI 每轮开口前都会拿别人的描述对照自己的词。
          「对不上」意味着他察觉到自己可能是卧底，接下来会改说更含糊、往大家方向靠的话。</p>
        <div class="panel">` +
        r.self_reads.map((s) => {
          const hdr = s.round !== last ? (last = s.round, `<div class="readRound">第 ${s.round} 轮</div>`) : '';
          return `${hdr}<div class="readRow ${FIT[s.fit] || 'unsure'}">
            <span class="rdName">${esc(s.name)}${s.is_spy ? '<i>卧底</i>' : ''}</span>
            <span class="rdFit">${esc(s.fit)}</span>
            <span class="rdWhy">${esc(s.why || '')}</span>
          </div>`;
        }).join('') + '</div>';
    }
  } else if (st.kind === 'werewolf') {
    h += '<h2 class="section">身份公开</h2>' + r.roles.map((p) => `
      <div class="secret ${p.role === 'wolf' ? 'killer' : ''}">
        ${av(p.id, 46)}
        <div style="min-width:0">
          <div class="nm">${esc(p.name)}　<span style="font-size:13px">${esc(p.role_label)}</span></div>
          <div class="sc">${p.alive ? '存活到最后' : esc(p.out_reason || '出局')}</div>
        </div></div>`).join('');
    if (r.seer_results?.length) {
      h += '<h2 class="section">预言家验人记录</h2><div class="panel">' +
        r.seer_results.map((x) => `<div>${esc(x.name)} → <b>${esc(x.result)}</b></div>`).join('') +
        '</div>';
    }
  } else {
    h += '<h2 class="section">票型</h2><div class="panel">' +
      (r.tally || []).map((t) => `<div class="rosterItem">${av(t.id || idOf(t.name), 28)}
        <span class="nm">${esc(t.name)}</span>
        <span style="margin-left:auto;color:var(--accent)">${'●'.repeat(t.votes)} ${t.votes} 票</span>
      </div>`).join('') + '</div>';
    // 凶手栽赃的那一条。对局中它和真线索长得一模一样，这里才拆穿。
    if (r.fabricated) {
      const f = r.fabricated;
      h += `<h2 class="section">有一条是假的</h2>
        <div class="panel fakeClue">
          <div class="fcHd">${esc(f.by_name)} 声称在${esc(f.location)}找到的这条，是他当场编的：</div>
          <div class="fcTxt">「${esc(f.text)}」</div>
          ${f.frame ? `<div class="fcWho">他想把嫌疑推给 <b>${esc(f.frame)}</b>。</div>` : ''}
          <div class="muted" style="margin-top:9px;font-size:12.5px">
            搜证结果是各人自己报的，没人能复核——凶手一局只能用这一次。</div>
        </div>`;
    }
    // 分层真相的逐题得分。老本子 r.truth 是 null，这一段整个不出现。
    if (r.truth) {
      const t = r.truth;
      h += `<h2 class="section">答卷　<span class="truthScore ${t.pass ? 'ok' : 'no'}">${t.score} / ${t.max}</span></h2>
        <div class="panel truthPanel">
        ${t.skipped ? '<p class="muted">你是凶手，这份答卷不计入你的胜负。</p>' : ''}
        ${t.detail.map((d) => `
          <div class="tqRow ${d.right ? 'right' : 'wrong'}">
            <div class="tqHd"><span class="tqMark">${d.right ? '✓' : '✕'}</span>${esc(d.prompt)}
              <i>${d.points} 分</i></div>
            <div class="tqAns">你的答案：<b>${esc(d.yours)}</b>${d.right ? '' :
              `　　正确答案：<b class="tqRight">${esc(d.correct)}</b>`}</div>
            ${d.explain ? `<div class="tqWhy">${esc(d.explain)}</div>` : ''}
          </div>`).join('')}
        </div>`;
    }
    if ((r.missed_deductions || []).length) {
      h += `<h2 class="section">没人拼出来的推论</h2>
        <div class="panel"><p class="muted">这些结论，只要有一个人同时握住下面几条，就能当场得出——
          差一步，没人凑齐。</p>
        ${r.missed_deductions.map((m) => `
          <div class="missedDed">
            <div class="mdFrom">${m.from.map((x) => `<span>${esc(x)}</span>`).join('<i>＋</i>')}</div>
            <div class="mdGot">⇒ ${esc(m.text)}</div>
          </div>`).join('')}
        </div>`;
    }
    h += '<h2 class="section">推理链</h2><div class="panel">' +
      (r.reasoning_chain || []).map((s, i) =>
        `<div style="padding:6px 0"><b style="color:var(--accent)">${i + 1}.</b> ${esc(s)}</div>`).join('') +
      '</div>';
    h += '<h2 class="section">真实时间线</h2><div class="panel"><div class="tl">';
    let slot = null;
    for (const t of r.timeline || []) {
      if (t.slot !== slot) { slot = t.slot; h += `<div class="slot">${esc(slot)}</div>`; }
      h += `<div class="item ${t.is_crime ? 'crime' : ''}"><b>${esc(t.name)}</b>　${
        esc(t.location)}　${esc(t.action)}${t.is_crime ? '　← 案发' : ''}</div>`;
    }
    h += '</div></div><h2 class="section">每个人的秘密</h2>' +
      (r.secrets || []).map((s) => `
        <div class="secret ${s.is_murderer ? 'killer' : ''}">
          ${av(s.id || idOf(s.name), 46)}
          <div style="min-width:0">
            <div class="nm">${esc(s.name)}${s.is_murderer ? '　← 凶手' : ''}</div>
            <div class="muted" style="font-size:12.5px">${esc(s.role)}</div>
            <div class="sc"><b>秘密：</b>${esc(s.secret)}</div>
            <div class="sc"><b>动机：</b>${esc(s.motive)}</div>
          </div></div>`).join('');
  }

  $('reveal').innerHTML = h;
  view('reveal');
}

// ── 背景音乐 ──────────────────────────────────────────
function renderBgmMenu() {
  $('bgmList').innerHTML = bgm.list().map((t) => `
    <button class="bgmItem ${t.key === bgm.currentTrack() ? 'sel' : ''}" data-k="${t.key}">
      ${esc(t.label)}${t.desc ? `<small>${esc(t.desc)}</small>` : ''}
    </button>`).join('');
  for (const b of $('bgmList').querySelectorAll('.bgmItem')) {
    b.onclick = () => {
      bgm.play(b.dataset.k);
      renderBgmMenu();
      $('btnBgm').classList.toggle('on', bgm.currentTrack() !== 'none');
    };
  }
}

// ── 极简 markdown（剧本 / 身份说明用）─────────────────
function mdToHtml(md) {
  const lines = String(md).split('\n');
  let html = '', inUl = false;
  const inline = (t) => esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>');
  for (const ln of lines) {
    if (/^#\s/.test(ln)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<h1>${inline(ln.slice(2))}</h1>`;
    } else if (/^###?\s/.test(ln)) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<h2>${inline(ln.replace(/^#+\s/, ''))}</h2>`;
    } else if (/^[-*]\s/.test(ln)) {
      if (!inUl) { html += '<ul>'; inUl = true; }
      html += `<li>${inline(ln.slice(2))}</li>`;
    } else if (/^---+$/.test(ln.trim())) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += '<hr>';
    } else if (ln.trim()) {
      if (inUl) { html += '</ul>'; inUl = false; }
      html += `<p>${inline(ln)}</p>`;
    }
  }
  if (inUl) html += '</ul>';
  return html;
}

// ── 事件绑定 / 启动 ───────────────────────────────────
$('log').addEventListener('scroll', () => {
  if (nearBottom($('log'))) $('btnToBottom').classList.add('hidden');
});
// ── 朗读开关 ────────────────────────────────────────
{
  const b = $('btnVoice');
  b.onclick = () => {
    if (!voice.available()) { showErr('这个浏览器不支持语音合成。'); return; }
    voice.setEnabled(!voice.enabled());
    b.classList.toggle('on', voice.enabled());
    b.title = voice.enabled()
      ? `朗读已开（可用中文语音 ${voice.voiceCount()} 个）`
      : '朗读发言';
    if (!voice.enabled()) voice.stop();
  };
}

// ── 通用记事本 ──────────────────────────────────────
{
  const drawer = $('padDrawer'), ta = $('padText');
  let timer = null;
  const savePad = async () => {
    if (!S.game) return;
    try {
      await api(`/api/g/${S.game.game_id}/pad`, {
        method: 'POST', body: JSON.stringify({ text: ta.value }),
      });
      $('padState').textContent = '已保存';
      setTimeout(() => { $('padState').textContent = ''; }, 1500);
    } catch { $('padState').textContent = '没存上'; }
  };
  $('btnPad').onclick = () => {
    drawer.classList.toggle('hidden');
    if (!drawer.classList.contains('hidden')) {
      ta.value = S.game?.pad || '';
      ta.focus();
    }
  };
  $('padClose').onclick = () => { savePad(); drawer.classList.add('hidden'); };
  ta.oninput = () => {
    $('padState').textContent = '…';
    clearTimeout(timer);
    timer = setTimeout(savePad, 800);   // 防抖，别每敲一个字就发一次
  };
  ta.onblur = () => { clearTimeout(timer); savePad(); };
}

for (const t of document.querySelectorAll('.midTab')) {
  t.onclick = () => {
    for (const x of document.querySelectorAll('.midTab')) x.classList.toggle('on', x === t);
    S.midTab = t.dataset.t;
    if (S.game) applyState(S.game);
  };
}

$('btnToBottom').onclick = () => {
  const box = $('log');
  box.scrollTo({ top: box.scrollHeight, behavior: 'smooth' });
  $('btnToBottom').classList.add('hidden');
};

$('btnBgm').onclick = (e) => {
  e.stopPropagation();
  $('bgmMenu').classList.toggle('hidden');
  renderBgmMenu();
};
document.addEventListener('click', (e) => {
  if (!e.target.closest('.bgmBox')) $('bgmMenu').classList.add('hidden');
});
$('bgmVol').oninput = (e) => bgm.setVolume(e.target.value / 100);

const goHome = () => {
  document.body.classList.remove('night');
  bgm.stop();
  $('btnBgm').classList.remove('on');
  $('brandTitle').textContent = 'AI 桌游厅';
  loadHome();
};
$('btnHome').onclick = goHome;
$('btnHome2').onclick = goHome;
$('btnStart').onclick = () => startGame({ kind: 'mystery' });
$('btnAgain').onclick = () => {
  if (S.kind === 'mystery') loadScripts();
  else showSetup(S.kind);
};

$('heroPlay').onclick = () => pickKind('mystery');
$('heroGen').onclick = async () => { await pickKind('mystery'); $('btnGen').click(); };
gen.init({
  api, esc, getKind: () => (S.kind === 'detective' ? 'cases' : 'mystery'),
  onDone: (kind) => (kind === 'cases' ? loadCases() : loadScripts()),
});
cfg.init({ api, onChange: () => health() });

health();
loadHome();
