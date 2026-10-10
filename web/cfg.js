/* 模型设置：让用户自己填接口地址 / 密钥 / 模型名。
 *
 * 密钥只会从浏览器发给本机服务，服务端从不把它传回来（只回尾号），
 * 所以这里的密钥框永远是空的，留空 = 不修改。
 */
const $ = (id) => document.getElementById(id);

// 只是帮着填默认值；模型名会变，以服务商文档为准
const PRESETS = {
  deepseek: { base: 'https://api.deepseek.com/v1', chat: 'deepseek-chat', reason: 'deepseek-reasoner', proxy: false },
  openai: { base: 'https://api.openai.com/v1', chat: 'gpt-4o-mini', reason: 'gpt-4o', proxy: false },
  dashscope: { base: 'https://dashscope.aliyuncs.com/compatible-mode/v1', chat: 'qwen-plus', reason: 'qwen-max', proxy: false },
  siliconflow: { base: 'https://api.siliconflow.cn/v1', chat: 'Qwen/Qwen2.5-72B-Instruct', reason: 'deepseek-ai/DeepSeek-R1', proxy: false },
};

let onChange = () => {};

function fill(c) {
  $('cfgBase').value = c.base_url || '';
  $('cfgChat').value = c.model_chat || '';
  $('cfgReason').value = c.model_reason || '';
  $('cfgProxy').checked = !!c.bypass_proxy;
  $('cfgKey').value = '';
  $('cfgKeyHint').textContent = c.has_key ? `已保存 ${c.key_hint}` : '（还没有密钥——现在是离线模式）';
  $('cfgPreset').value = '';
}

function body() {
  return {
    base_url: $('cfgBase').value.trim(),
    api_key: $('cfgKey').value.trim(),
    model_chat: $('cfgChat').value.trim(),
    model_reason: $('cfgReason').value.trim(),
    bypass_proxy: $('cfgProxy').checked,
  };
}

function msg(text, ok = false) {
  const el = $('cfgMsg');
  el.textContent = text;
  el.style.color = ok ? '#8fc98a' : '';
}

export function init({ api, onChange: cb }) {
  onChange = cb;
  const close = () => $('cfgModal').classList.add('hidden');

  $('btnCfg').onclick = async () => {
    msg('');
    try {
      fill(await api('/api/config'));
      $('cfgModal').classList.remove('hidden');
    } catch (e) {
      alert(e.message);
    }
  };
  $('cfgClose').onclick = close;
  $('cfgModal').addEventListener('click', (e) => { if (e.target === $('cfgModal')) close(); });

  $('cfgPreset').onchange = (e) => {
    const p = PRESETS[e.target.value];
    if (!p) return;
    $('cfgBase').value = p.base;
    $('cfgChat').value = p.chat;
    $('cfgReason').value = p.reason;
    $('cfgProxy').checked = p.proxy;
  };

  $('cfgSave').onclick = async () => {
    msg('');
    $('cfgSave').disabled = true;
    try {
      const c = await api('/api/config', { method: 'POST', body: JSON.stringify(body()) });
      fill(c);
      msg('已保存，立即生效。可以点「测试连接」确认。', true);
      onChange();
    } catch (e) {
      msg(e.message);
    } finally {
      $('cfgSave').disabled = false;
    }
  };

  $('cfgTest').onclick = async () => {
    msg('');
    $('cfgTest').disabled = true;
    msg('正在连接…（填错时可能要等一会儿才报错）', true);
    try {
      // 测试用的是已保存的设置：先存再测，免得填了没存、测的还是旧的
      if ($('cfgKey').value.trim() || $('cfgBase').value.trim()) {
        await api('/api/config', { method: 'POST', body: JSON.stringify(body()) });
      }
      const r = await api('/api/config/test', { method: 'POST' });
      if (r.ok) msg(`连接成功（${r.seconds} 秒）：模型回复「${(r.detail || '').slice(0, 20)}」`, true);
      else msg('连接失败：' + (r.detail || '未知错误'));
      fill(await api('/api/config'));
      onChange();
    } catch (e) {
      msg(e.message);
    } finally {
      $('cfgTest').disabled = false;
    }
  };

  $('cfgReset').onclick = async () => {
    if (!confirm('丢掉在这里保存的设置，回到 .env 里的配置？')) return;
    try {
      fill(await api('/api/config', { method: 'DELETE' }));
      msg('已恢复默认。', true);
      onChange();
    } catch (e) {
      msg(e.message);
    }
  };
}
