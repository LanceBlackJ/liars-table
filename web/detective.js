/* 《探案》前端。
 *
 * 和另外三款最大的不同：没有回合，没有「轮到你」。
 * 界面上永远只有一件事——你现在想干什么。
 *
 * 没有场景图——场景就是文字。左栏常驻「这里有什么、有谁、能去哪儿」，
 * 三样都是可点的文字选项；中栏是叙述流和行动区；右栏是攒下的证物。
 */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) =>
  ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let ST = null;          // 当前状态
let BUSY = false;
let TALKING = null;     // 正在跟谁说话
let DRAFT = '';         // 发送失败时留住玩家刚敲的那句问话
let SEEN = 0;
const OPEN_EV = new Set();   // 证物栏里展开着的条目
let API = null;         // 由 app.js 注入：{ api, showErr, view, av }

export function init(deps) { API = deps; }

// ── 进入 ──────────────────────────────────────────────
export function enter(st) {
  ST = st; SEEN = 0; TALKING = null;
  $('caseLog').innerHTML = '';
  $('brandTitle').textContent = st.kind_label || '探案';
  $('phaseBadge').textContent = st.title || '';
  API.view('case');
  render();
  bindTabs();
}

function bindTabs() {
  // 证物和「去哪儿」现在各占一栏，不再挤在标签页里
  $('btnAccuse').onclick = showAccuse;
  const box = $('caseLog');
  $('caseToBottom').onclick = () => {
    box.scrollTop = box.scrollHeight;
    $('caseToBottom').classList.add('hidden');
  };
  box.onscroll = () => {
    if (box.scrollHeight - box.scrollTop - box.clientHeight < 90) {
      $('caseToBottom').classList.add('hidden');
    }
  };
}

// ── 发动作 ────────────────────────────────────────────
async function act(payload) {
  if (BUSY) return;
  BUSY = true;
  renderAct();
  try {
    const out = await API.api(`/api/g/${ST.game_id}/act`, {
      method: 'POST', body: JSON.stringify(payload),
    });
    BUSY = false;
    ST = out.state;
    DRAFT = '';          // 发出去了，草稿就不用留了
    render();
  } catch (e) {
    BUSY = false;
    API.showErr(e.message);
    renderAct();         // DRAFT 还在，renderAct 会把那句话填回输入框
  }
}

// ── 渲染 ──────────────────────────────────────────────
function render() {
  renderScene();
  renderLog();
  renderEvidence();
  renderTravel();
  renderAct();
  if (ST.finished && ST.result) showVerdict();
}

function renderScene() {
  const sc = ST.scene;
  $('sceneName').textContent = sc.name;
  $('sceneDesc').textContent = sc.desc || '';

  // 可查看的东西：直接列成文字选项。查过的标出来，免得来回点重复的。
  $('sceneSpots').innerHTML = sc.hotspots.length
    ? sc.hotspots.map((h) => `
        <button class="opt ${h.done ? 'done' : ''}" data-id="${esc(h.id)}">
          <span class="oMark">${h.done ? '✓' : '·'}</span>
          <span class="oTxt">${esc(h.label)}</span>
          ${h.done ? '<i class="oTag">已查看</i>' : ''}
        </button>`).join('')
    : '<p class="noOne">这里没什么可看的。</p>';
  for (const b of document.querySelectorAll('#sceneSpots .opt')) {
    b.onclick = () => act({ type: 'examine', hotspot_id: b.dataset.id });
  }

  $('scenePeople').innerHTML = sc.people.length
    ? sc.people.map((p) => `
        <button class="opt person ${TALKING === p.id ? 'on' : ''}" data-id="${esc(p.id)}">
          <img class="av av-28" alt="" src="/api/avatar/case/${ST.case_id}/${esc(p.id)}.svg">
          <span class="oTxt"><b>${esc(p.name)}</b><i>${esc(p.role)}</i></span>
          ${TALKING === p.id ? '<i class="oTag on">正在问话</i>' : ''}
        </button>`).join('')
    : '<p class="noOne">这儿没有人。</p>';
  for (const b of document.querySelectorAll('#scenePeople .opt')) {
    b.onclick = () => { TALKING = (TALKING === b.dataset.id) ? null : b.dataset.id; render(); };
  }
}

const KIND_CLS = {
  brief: 'cBrief', scene: 'cScene', examine: 'cExam', gain: 'cGain',
  deduce: 'cDeduce', ask: 'cAsk', say: 'cSay', show: 'cShow',
  crack: 'cCrack', blocked: 'cBlocked', note: 'cNote', error: 'cErr',
};

/** 中栏两种模式：调查叙述 / 单人对话。
 *
 * 对话原来是直接插进调查日志里的，翻起来「查看抽屉」和「问秦棠」
 * 混成一片，既看不清案情推进，也看不清某个人到底说过什么。
 * 现在问话时中栏整个切成这个人的对话，退出问话再切回来。
 */
function renderLog() {
  if (TALKING) return renderTalk();
  $('caseLog').classList.remove('talkMode');
  const box = $('caseLog');
  const stick = box.scrollHeight - box.scrollTop - box.clientHeight < 90;
  const TALK_KINDS = ['ask', 'say', 'show', 'crack'];
  let added = 0;
  let lastWho = null;   // 连续的问话折成一条，不让对话把案情流冲散
  for (const e of ST.log || []) {
    if (e.seq <= SEEN) continue;
    SEEN = e.seq;
    if (TALK_KINDS.includes(e.kind)) {
      if (e.person_id && e.person_id !== lastWho) {
        lastWho = e.person_id;
        box.insertAdjacentHTML('beforeend',
          `<div class="ce cTalkMark" data-p="${esc(e.person_id)}" role="button" tabindex="0">
             你和 <b>${esc(e.name || '')}</b> 谈过话　<span>点这里重看</span></div>`);
        added++;
      }
      continue;
    }
    lastWho = null;
    box.insertAdjacentHTML('beforeend', renderEvent(e));
    added++;
  }
  for (const m of box.querySelectorAll('.cTalkMark')) {
    const open = () => { TALKING = m.dataset.p; render(); };
    m.onclick = open;
    m.onkeydown = (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); open(); }
    };
  }
  if (!added) return;
  if (stick) {
    requestAnimationFrame(() => {
      // 内联覆盖 CSS 的 scroll-behavior:smooth：平滑滚动是合成器驱动的，
      // 页面不合成时一帧都不推进，日志会永久卡住。
      const prev = box.style.scrollBehavior;
      box.style.scrollBehavior = 'auto';
      box.scrollTop = box.scrollHeight;
      box.style.scrollBehavior = prev;
    });
    $('caseToBottom').classList.add('hidden');
  } else {
    $('caseToBottom').classList.remove('hidden');
  }
}

function renderTalk() {
  const box = $('caseLog');
  const p = ST.scene.people.find((x) => x.id === TALKING);
  if (!p) { TALKING = null; return; }
  // 只挑跟这个人有关的往来，其余调查叙述不进来
  const mine = (ST.log || []).filter(
    (e) => ['ask', 'say', 'show', 'crack'].includes(e.kind) && e.person_id === TALKING,
  );
  box.classList.add('talkMode');
  box.innerHTML = `
    <div class="tkHead">
      <img class="av av-46" alt="" src="/api/avatar/case/${ST.case_id}/${esc(p.id)}.svg">
      <div><b>${esc(p.name)}</b><i>${esc(p.role)}</i></div>
      <button class="tkBack" id="tkBack">← 回到调查</button>
    </div>
    <div class="tkBody">${
      mine.length
        ? mine.map(renderEvent).join('')
        : '<p class="muted tkEmpty">还没跟他说过话。下面直接问就行——想到什么问什么。</p>'
    }</div>`;
  $('tkBack').onclick = () => { TALKING = null; SEEN = 0; $('caseLog').innerHTML = ''; render(); };
  const b = box.querySelector('.tkBody');
  if (b) b.scrollTop = b.scrollHeight;
}

function renderEvent(e) {
  const cls = KIND_CLS[e.kind] || 'cNote';
  // 案件文本用 **……** 标出关键处（血溅「朝外」那一面之类）。
  // 之前 esc 完直接贴，星号原样打在了屏幕上。
  const br = (t) => esc(t)
    .replace(/\*\*(.+?)\*\*/g, '<b class="key">$1</b>')
    .replace(/\n/g, '<br>');
  switch (e.kind) {
    case 'brief':
      return `<div class="ce ${cls}">${br(e.text)}</div>`;
    case 'scene':
      return `<div class="ce ${cls}"><b>${esc(e.name)}</b><div>${br(e.text)}</div></div>`;
    case 'examine':
      return `<div class="ce ${cls}"><div class="ceHd">${esc(e.label)}</div>${br(e.text)}</div>`;
    case 'gain':
      return `<div class="ce ${cls}">◆ 拿到了 <b>${esc(e.name)}</b><div class="ceSub">${br(e.detail)}</div></div>`;
    case 'deduce':
      return `<div class="ce ${cls}"><div class="ceHd">对上了</div>${br(e.text)}
        <div class="ceSub">◆ ${esc(e.name)}</div></div>`;
    case 'ask':
      return `<div class="ce ${cls}"><b>你</b> → ${esc(e.name)}<div>${br(e.text)}</div></div>`;
    case 'say':
      return `<div class="ce ${cls}">
        <img class="av av-40" alt="" src="/api/avatar/case/${ST.case_id}/${esc(e.person_id)}.svg">
        <div><b>${esc(e.name)}</b><div>${br(e.text)}</div></div></div>`;
    case 'show':
      return `<div class="ce ${cls}">你把 <b>${esc(e.ev_name)}</b> 摆到了 ${esc(e.name)} 面前。</div>`;
    case 'crack':
      return `<div class="ce ${cls}"><div class="ceHd">${esc(e.name)}</div>${br(e.text)}</div>`;
    default:
      return `<div class="ce ${cls}">${br(e.text || '')}</div>`;
  }
}

function renderEvidence() {
  const box = $('csEv');
  const ev = ST.evidence || [];
  // 只报「找到了几件」，不报总数——报了总数等于告诉玩家还差几件、
  // 找满就完事了，查案就变成了清单打勾。
  $('evCount').textContent = ev.length ? `${ev.length} 件` : '';
  box.innerHTML = ev.length
    ? ev.map((e) => `
      <div class="evItem ${OPEN_EV.has(e.id) ? 'open' : ''} ${e.combined ? 'derived' : ''}"
           data-id="${esc(e.id)}" role="button" tabindex="0">
        <div class="evHd">
          <span class="evMark">${e.combined ? '⊕' : '◆'}</span>
          <b>${esc(e.name)}</b>
          <span class="evChev">${OPEN_EV.has(e.id) ? '−' : '+'}</span>
        </div>
        <div class="evBody">
          <p>${esc(e.detail)}</p>
          ${e.found_at ? `<div class="evWhere">${esc(e.found_at)}</div>` : ''}
        </div>
      </div>`).join('')
    : '<p class="muted">还什么都没找到。左边「现场」里的东西都可以查看。</p>';

  for (const it of document.querySelectorAll('.evItem')) {
    const toggle = () => {
      const id = it.dataset.id;
      if (OPEN_EV.has(id)) OPEN_EV.delete(id); else OPEN_EV.add(id);
      renderEvidence();
    };
    it.onclick = toggle;
    it.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
    };
  }
}

function renderTravel() {
  $('csGo').innerHTML = ST.scenes.map((s) => `
    <button class="opt goItem ${s.here ? 'here' : ''} ${s.locked ? 'locked' : ''}"
            data-id="${esc(s.id)}" ${s.here ? 'disabled' : ''}>
      <span class="oMark">${s.here ? '◉' : s.locked ? '🔒' : '→'}</span>
      <span class="oTxt">${esc(s.name)}</span>
      ${s.locked ? '<i class="oTag">锁着</i>' : ''}${s.here ? '<i class="oTag on">你在这儿</i>' : ''}
    </button>`).join('');
  for (const b of document.querySelectorAll('.goItem')) {
    b.onclick = () => act({ type: 'move', scene_id: b.dataset.id });
  }
}

function renderAct() {
  const box = $('caseAct');
  if (ST.finished) { box.innerHTML = ''; return; }
  if (BUSY) { box.innerHTML = '<div class="thinking">等着</div>'; return; }

  if (!TALKING) {
    box.innerHTML = `<p class="hintLine">左边列着这里能查看的东西和在场的人——点一个开始。</p>`;
    return;
  }
  const p = ST.scene.people.find((x) => x.id === TALKING);
  const ev = ST.evidence || [];
  box.innerHTML = `
    <div class="talkBar">
      <span class="tbWho">正在问 <b>${esc(p.name)}</b></span>
      <button class="tbEnd" id="tbEnd">结束</button>
    </div>
    <div class="askRow">
      <input id="askIn" placeholder="想问他什么？（随便问，不用挑）" autocomplete="off">
      <button class="primary" id="askGo">问</button>
      <button id="askAuto" class="hintBtn">🎭 替我问</button>
    </div>
    ${ev.length ? `<div class="showRow">
      <span class="srHd">或者把证物拍在他面前：</span>
      ${ev.map((e) => `<button class="srItem" data-id="${esc(e.id)}">${esc(e.name)}</button>`).join('')}
    </div>` : ''}`;

  // 网不好的时候 act 会失败并重绘整个面板。输入框是重绘出来的新元素，
  // 玩家刚敲的那句问话就没了——问得越长越气人。所以发之前先存一份，
  // 失败了原样填回去。
  if (DRAFT) { $('askIn').value = DRAFT; DRAFT = ''; }
  const go = () => {
    const t = $('askIn').value.trim();
    if (t) { DRAFT = t; act({ type: 'talk', person_id: TALKING, text: t }); }
  };
  $('askGo').onclick = go;
  // 「替我问」：依据只有你手里的物证 + 这个人已经说过的话
  $('askAuto').onclick = async () => {
    const b = $('askAuto');
    b.disabled = true;
    const old = b.textContent;
    b.textContent = '想…';
    try {
      const r = await API.api(`/api/g/${ST.game_id}/autoask`, {
        method: 'POST', body: JSON.stringify({ person_id: TALKING }),
      });
      if (r.text) { $('askIn').value = r.text; $('askIn').focus(); }
      else API.showErr('一时没想出该问什么。');
    } catch (e) {
      API.showErr('代提问失败：' + e.message);
    } finally {
      b.disabled = false;
      b.textContent = old;
    }
  };
  $('askIn').onkeydown = (e) => { if (e.key === 'Enter') go(); };
  $('askIn').focus();
  $('tbEnd').onclick = () => { TALKING = null; renderScene(); renderAct(); };
  for (const b of document.querySelectorAll('.srItem')) {
    b.onclick = () => act({ type: 'present', person_id: TALKING, evidence_id: b.dataset.id });
  }
}

// ── 指认 ──────────────────────────────────────────────
function showAccuse() {
  const a = ST.accuse;
  let culprit = null;
  const box = $('caseAct');
  TALKING = null;
  renderScene();

  box.innerHTML = `
    <div class="accusePanel">
      <div class="apWarn">只有一次机会。手法和动机要你自己写出来，会逐条打分。</div>

      <div class="acGroup">
        <div class="acHd">你认为是谁干的</div>
        <div class="acPeople">
          ${a.people.map((x) => `<button class="acOpt" data-v="${esc(x.id)}">
            <img class="av av-28" alt="" src="/api/avatar/case/${ST.case_id}/${esc(x.id)}.svg">
            <span>${esc(x.name)}<i>${esc(x.role)}</i></span></button>`).join('')}
        </div>
        ${a.people.length < 2 ? '<p class="muted">你见过的人还太少。先去别的地方转转。</p>' : ''}
      </div>

      <div class="acGroup">
        <div class="acHd">他是怎么下手的</div>
        <p class="muted">用什么、怎么伪装、时间上怎么错开的——把你查到的串起来说。</p>
        <textarea id="acHow" rows="4" placeholder="例如：先用……把人打倒，再把……搬过来，做成失足的样子。"></textarea>
      </div>

      <div class="acGroup">
        <div class="acHd">他为什么要这么做</div>
        <p class="muted">动机。说清楚死者手里有什么、或者挡了他什么路。</p>
        <textarea id="acWhy" rows="3" placeholder="例如：死者掌握了他……，一旦公开他就完了。"></textarea>
      </div>

      <div class="row">
        <button class="primary" id="acGo" disabled>就是他</button>
        <button id="acCancel">再想想</button>
      </div>
    </div>`;

  const sync = () => {
    $('acGo').disabled = !(culprit && $('acHow').value.trim() && $('acWhy').value.trim());
  };
  for (const b of document.querySelectorAll('.acOpt')) {
    b.onclick = () => {
      for (const x of document.querySelectorAll('.acOpt')) x.classList.toggle('on', x === b);
      culprit = b.dataset.v;
      sync();
    };
  }
  $('acHow').oninput = sync;
  $('acWhy').oninput = sync;
  $('acGo').onclick = () => act({
    type: 'accuse', culprit,
    method: $('acHow').value.trim(), motive: $('acWhy').value.trim(),
  });
  $('acCancel').onclick = () => renderAct();
}



function showVerdict() {
  const r = ST.result;
  // 满分 5：人 1 分 + 手法 0-2 + 动机 0-2
  const tier = !r.culprit_ok ? 'lose'
    : r.points >= 5 ? 'win' : r.points >= 4 ? 'win' : 'partial';
  const dots = (n) => '●'.repeat(n) + '○'.repeat(2 - n);
  const cls = (n) => n === 2 ? 'sFull' : n === 1 ? 'sHalf' : 'sNone';

  // 手法和动机是玩家自己写的，没有「选项对错」，只有判分和点评。
  // 所以这里印的是「你写的 / 真相 / 裁判怎么说」三段，而不是打勾打叉。
  const graded = (label, score, comment, mine, truth) => `
    <div class="gRow ${cls(score)}">
      <div class="gHd"><b>${label}</b>
        <span class="gDots">${dots(score)}</span>
        <span class="gScore">${score} / 2</span></div>
      <div class="gMine"><i>你写的</i>${esc(mine)}</div>
      ${score === 2 ? '' : `<div class="gTruth"><i>真相</i>${esc(truth)}</div>`}
      ${comment ? `<div class="gCmt">${esc(comment)}</div>` : ''}
    </div>`;

  $('caseAct').innerHTML = `
    <div class="verdict ${tier}">
      <h2>${esc(r.verdict)}</h2><p>${esc(r.note)}</p>
      <div class="vScore">${r.points} / ${r.max_points}</div>
    </div>
    <div class="gRow ${r.culprit_ok ? 'sFull' : 'sNone'}">
      <div class="gHd"><b>凶手</b>
        <span class="gDots">${r.culprit_ok ? '●' : '○'}</span>
        <span class="gScore">${r.culprit_ok ? 1 : 0} / 1</span></div>
      <div class="gMine"><i>你指认</i>${esc(r.your.culprit)}</div>
      ${r.culprit_ok ? '' : `<div class="gTruth"><i>真凶</i>${esc(r.truth.culprit)}</div>`}
    </div>
    ${graded('手法', r.method_score, r.method_comment, r.your.method, r.truth.method)}
    ${graded('动机', r.motive_score, r.motive_comment, r.your.motive, r.truth.motive)}

    <div class="vStats">
      你一共找到 ${r.evidence_found} 件证物（全案共 ${r.evidence_total} 件）　·
      ${Object.values(r.tiers).map((t) => `${esc(t.name)} ${t.tier}/${t.max}`).join('　')}
    </div>
    <div class="vExp"><div class="ceHd">那一晚到底发生了什么</div>
      ${esc(r.explanation).replace(/\n/g, '<br>')}</div>
    <div class="row" style="justify-content:center;margin:18px 0">
      <button class="primary" id="vHome">回大厅</button>
    </div>`;
  $('vHome').onclick = () => location.reload();
  $('caseAct').scrollIntoView({ block: 'start' });
}

