/* AI 生成本子：表单 + 进度轮询。
 *
 * 生成在服务端后台跑（几分钟），这里只负责收参数、画进度、结束后让选本页刷新。
 * 关掉弹窗不会中断生成——文件写好后下次进选本页就能看到。
 */
const $ = (id) => document.getElementById(id);

let timer = null;

/** kind：'cases' = 从探案页进来，默认只生成探案案子；其余 = 从剧本杀页进来，默认两样都生成 */
function open(kind) {
  const caseMode = kind === 'cases';
  $('genTitleH').textContent = caseMode ? '✨ AI 生成新案子' : '✨ AI 生成新本';
  $('genWithScript').checked = !caseMode;
  $('genWithCase').checked = true;
  syncFields();
  $('genModal').classList.remove('hidden');
  $('genForm').classList.remove('hidden');
  $('genRun').classList.add('hidden');
  $('genMsg').textContent = '';
  setTimeout(() => $('genTheme').focus(), 0);
}

function close() {
  $('genModal').classList.add('hidden');
  if (timer) { clearInterval(timer); timer = null; }
}

/** 只生成探案时，嫌疑人数 / 证人 / 幕数 与它无关，收起来免得误导 */
function syncFields() {
  const on = $('genWithScript').checked;
  for (const id of ['genSuspects', 'genNpc', 'genActs']) {
    const box = $(id).closest('.fld');
    box.style.opacity = on ? '' : '.35';
    $(id).disabled = !on;
  }
}

export function init({ api, esc, onDone, getKind }) {
  $('btnGen').onclick = () => open(getKind ? getKind() : 'mystery');
  $('genWithScript').onchange = syncFields;
  $('genCancel').onclick = close;
  $('genClose').onclick = close;
  $('genModal').addEventListener('click', (e) => { if (e.target === $('genModal')) close(); });

  $('genGo').onclick = async () => {
    const theme = $('genTheme').value.trim();
    if (!$('genWithScript').checked && !$('genWithCase').checked) {
      $('genMsg').textContent = '至少勾一样：剧本杀本子，或探案案子';
      return;
    }
    if (theme.length < 4) {
      $('genMsg').textContent = '主题至少写几个字，例如「民国戏院后台的一桩命案」';
      return;
    }
    $('genMsg').textContent = '';
    $('genGo').disabled = true;
    try {
      const r = await api('/api/gen/script', {
        method: 'POST',
        body: JSON.stringify({
          theme,
          suspects: +$('genSuspects').value,
          npc: +$('genNpc').value,
          acts: +$('genActs').value,
          difficulty: $('genDiff').value,
          title: $('genTitle').value.trim(),
          with_script: $('genWithScript').checked,
          with_case: $('genWithCase').checked,
        }),
      });
      $('genForm').classList.add('hidden');
      $('genRun').classList.remove('hidden');
      $('genLog').innerHTML = '';
      $('genBar').style.width = '2%';
      $('genRunBtns').innerHTML = '<span class="genNote">关掉这个窗口不会中断生成，写好后会出现在选本页。</span>';
      $('genStage').textContent = r.offline
        ? '离线模式：没有配置模型密钥，只会放出一个模板示例'
        : '正在排队…';
      poll(r.job_id, api, esc, onDone);
    } catch (e) {
      $('genMsg').textContent = e.message;
    } finally {
      $('genGo').disabled = false;
    }
  };
}

function poll(jobId, api, esc, onDone) {
  if (timer) clearInterval(timer);
  const tick = async () => {
    let st;
    try {
      st = await api('/api/gen/script/' + jobId);
    } catch (e) {
      $('genStage').textContent = '读取进度失败：' + e.message;
      return;
    }
    $('genBar').style.width = Math.max(2, Math.round(st.progress * 100)) + '%';
    $('genStage').textContent =
      `${st.stage}　${Math.round(st.progress * 100)}%　·　已用 ${Math.round(st.elapsed)} 秒`;
    $('genLog').innerHTML = st.log.map((l) => `<div>${esc(l)}</div>`).join('');
    $('genLog').scrollTop = $('genLog').scrollHeight;
    if (st.status === 'done') {
      clearInterval(timer); timer = null;
      const r = st.result;
      $('genStage').textContent = `完成　·　《${r.title}》`;
      if (r.case_error) $('genLog').innerHTML += `<div class="genErr">${esc(r.case_error)}</div>`;
      $('genRunBtns').innerHTML =
        (r.script ? '<button id="genGoScript" class="primary">去看剧本杀</button>' : '') +
        (r.case ? '<button id="genGoCase" class="primary">去看探案</button>' : '');
      if (r.script) $('genGoScript').onclick = () => { close(); onDone('scripts'); };
      if (r.case) $('genGoCase').onclick = () => { close(); onDone('cases'); };
    } else if (st.status === 'error') {
      clearInterval(timer); timer = null;
      $('genStage').textContent = '生成失败';
      $('genLog').innerHTML += `<div class="genErr">${esc(st.error)}</div>`;
      $('genRunBtns').innerHTML = '<button id="genRetry">再试一次</button>';
      $('genRetry').onclick = () => open('mystery');
    }
  };
  tick();
  timer = setInterval(tick, 2000);
}
