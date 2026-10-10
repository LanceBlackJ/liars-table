/* 角色朗读。用浏览器内置的 speechSynthesis，不联网、不要密钥。
 *
 * 能做到什么，先说清楚：
 *   ✓ 每个角色一套固定的 音色/音调/语速，彼此明显不同，整局稳定
 *   ✓ 参数从角色已有的 speech_style / personality 推出来——
 *     「话少」的语速快，「慢条斯理」的慢，女性角色音调高一档
 *   ✗ 真正的情感语调（愤怒、心虚、颤抖）做不到。
 *     Web Speech 是平读的，没有情感参数。要那个得接云端 TTS（付费）。
 *
 * 中文语音包在 Windows 上通常只有一到三个（Huihui / Yaoyao / Kangkang），
 * 所以「音色」本身的可选项有限，真正拉开差别的是 pitch + rate 的组合。
 */

let VOICES = [];
let ENABLED = false;
let RATE_SCALE = 1;
const ASSIGNED = new Map();   // 角色 id → 参数，整局不变

function loadVoices() {
  VOICES = (window.speechSynthesis?.getVoices() || [])
    .filter((v) => /^zh/i.test(v.lang));
  return VOICES;
}

if (window.speechSynthesis) {
  loadVoices();
  // Chrome 是异步加载语音包的，首次 getVoices() 常常是空的
  window.speechSynthesis.onvoiceschanged = loadVoices;
}

export function available() {
  return !!window.speechSynthesis;
}

export function voiceCount() {
  return VOICES.length;
}

export function enabled() { return ENABLED; }

export function setEnabled(on) {
  ENABLED = !!on;
  if (!ENABLED) stop();
}

export function setRate(scale) { RATE_SCALE = scale; }

export function stop() {
  try { window.speechSynthesis?.cancel(); } catch { /* 忽略 */ }
}

/** 从角色的说话风格 + 性格里推一套嗓音参数。
 *
 * 这些关键词直接取自剧本里已经写好的 speech_style / personality，
 * 所以「话少的人语速快」这种对应关系是有依据的，不是随机分配。
 */
function deriveParams(seedIdx, style = '', personality = '', hint = '') {
  const t = `${style} ${personality}`;
  let rate = 1.0;
  let pitch = 1.0;

  // 语速
  if (/话少|简洁|短句|干脆|急|语速快|冲/.test(t)) rate += 0.18;
  if (/慢|絮叨|拖|谨慎|斟酌|沉稳|条理/.test(t)) rate -= 0.15;
  // 音调
  if (/轻声|温和|柔|安静|怯/.test(t)) pitch += 0.12;
  if (/嗓门|粗声|大气|嗓子|洪亮|冲/.test(t)) pitch -= 0.14;
  if (/女|小姐|太太|阿姨|大姐/.test(t)) pitch += 0.1;

  // 同一本子里的人再按序号错开一点，避免两个风格相近的人听起来一样
  rate += ((seedIdx % 5) - 2) * 0.045;
  pitch += ((seedIdx % 7) - 3) * 0.035;

  return {
    rate: Math.min(1.6, Math.max(0.6, rate)),
    pitch: Math.min(1.8, Math.max(0.4, pitch)),
    voice: pickVoice(seedIdx, hint),
  };
}

/** 按性别挑音色。Windows 上通常是 Huihui(女) / Yaoyao(女) / Kangkang(男)。
 *  纯按序号轮的话会出现男角色配女声，听着很出戏。 */
function pickVoice(seedIdx, hint) {
  if (!VOICES.length) return null;
  const male = VOICES.filter((v) => /kangkang|yunxi|yunyang|male/i.test(v.name));
  const female = VOICES.filter((v) => !male.includes(v));
  const pool = (hint === 'm' ? male : female);
  const use = pool.length ? pool : VOICES;
  return use[seedIdx % use.length];
}

/** 给一个角色分配嗓音。同一个 id 永远拿到同一套参数。 */
export function assign(id, idx, style, personality, hint) {
  if (!ASSIGNED.has(id)) {
    ASSIGNED.set(id, deriveParams(idx, style, personality, hint));
  }
  return ASSIGNED.get(id);
}

export function reset() {
  ASSIGNED.clear();
  stop();
}

/** 念一句。id 决定嗓音，没登记过就现场按 idx 分配。 */
export function speak(text, id, idx = 0, style = '', personality = '', hint = '') {
  if (!ENABLED || !text || !window.speechSynthesis) return;
  const p = assign(id, idx, style, personality, hint);
  const u = new SpeechSynthesisUtterance(
    // 念出来的是台词，不是排版。去掉星号和括注，否则会把「**」也念出来
    String(text).replace(/\*\*/g, '').replace(/[（(][^）)]*[）)]/g, '').slice(0, 600),
  );
  if (p.voice) u.voice = p.voice;
  u.lang = p.voice?.lang || 'zh-CN';
  u.rate = p.rate * RATE_SCALE;
  u.pitch = p.pitch;
  u.volume = 1;
  window.speechSynthesis.speak(u);
}
