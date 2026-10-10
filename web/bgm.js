/* 背景音乐 —— 用 Web Audio 程序化生成，不加载任何音频文件。
 *
 * 为什么不用 mp3：项目可能跑在内网 + 本地代理环境下，外链音频会随机挂掉；
 * 打包进仓库的音乐动辄几 MB 还有授权问题。程序化生成零依赖、零体积、
 * 可以无限循环不重复。
 *
 * 设计原则（第一版太吵，重做过）：
 *   · 只用 sine / triangle。square 和 sawtooth 谐波太丰富，当背景音就是噪音
 *   · 默认音量 0.10，且每个音都是慢起慢落，不出现任何瞬态attack
 *   · 低通截止压到 200-700Hz，把刺耳的高频整个切掉
 *   · 混响反馈降到 0.22，避免长时间运行后声音糊成一团
 *   · 音符稀疏——它是垫底的氛围，不是要听的曲子
 */

// 每个本子一首专属曲子；狼人杀 / 谁是卧底各一首
const TRACKS = {
  none: { label: '静音', desc: '' },

  manor: {
    label: '雾锁松风馆',
    desc: '民国宅邸 · 低沉迟缓',
    root: 98,                       // G2
    scale: [0, 3, 5, 7, 10],        // 小调五声
    padWave: 'sine', noteWave: 'triangle',
    tempo: 8400, noteRate: 0.45, filter: 420, decay: 3.0, gain: 1.0,
  },
  polar: {
    label: '极夜之下',
    desc: '南极科考站 · 空旷冰冷',
    root: 73.4,                     // D2
    scale: [0, 2, 7, 9, 14],
    padWave: 'sine', noteWave: 'sine',
    tempo: 11000, noteRate: 0.26, filter: 260, decay: 4.6, gain: 1.05,
  },
  reunion: {
    label: '毕业十年',
    desc: '同学会 · 怀旧温和',
    root: 130.8,                    // C3
    scale: [0, 2, 4, 7, 9],         // 大调五声
    padWave: 'triangle', noteWave: 'sine',
    tempo: 7000, noteRate: 0.5, filter: 680, decay: 2.4, gain: 0.9,
  },
  opera: {
    label: '戏台一梦',
    desc: '民国戏班 · 幽咽婉转',
    root: 110,                      // A2
    scale: [0, 2, 5, 7, 9],         // 中式五声（宫商角徵羽）
    padWave: 'triangle', noteWave: 'triangle',
    tempo: 7800, noteRate: 0.55, filter: 560, decay: 2.8, gain: 0.95,
  },
  rain: {
    label: '雨夜孤岛',
    desc: '潮湿阴郁',
    root: 87.3,                     // F2
    scale: [0, 3, 5, 8, 10],
    padWave: 'sine', noteWave: 'sine',
    tempo: 9600, noteRate: 0.38, filter: 320, decay: 3.6, gain: 1.0,
  },
  neon: {
    label: '末班地铁',
    desc: '都市 · 冷调低回',
    root: 116.5,                    // A#2
    scale: [0, 2, 3, 7, 10],
    padWave: 'triangle', noteWave: 'sine',
    tempo: 6600, noteRate: 0.5, filter: 600, decay: 2.2, gain: 0.9,
  },
  temple: {
    label: '古刹疑云',
    desc: '空灵 · 悠远',
    root: 65.4,                     // C2
    scale: [0, 5, 7, 12, 17],       // 四五度堆叠，空灵
    padWave: 'sine', noteWave: 'sine',
    tempo: 12000, noteRate: 0.3, filter: 300, decay: 5.0, gain: 1.1,
  },

  night: {
    label: '长夜（狼人杀）',
    desc: '低频脉动 · 不安',
    root: 61.7,                     // B1
    scale: [0, 1, 5, 8, 10],        // 含小二度
    padWave: 'sine', noteWave: 'triangle',
    tempo: 6000, noteRate: 0.42, filter: 220, decay: 2.6, gain: 1.15,
  },
  turpentine: {
    label: '颜料未干',
    desc: '深夜画室 · 悬着一口气',
    root: 87.3,                     // F2
    scale: [0, 1, 5, 7, 8],         // 半音起头，始终没落回主音——不安定
    padWave: 'sine', noteWave: 'triangle',
    tempo: 9600, noteRate: 0.32, filter: 340, decay: 3.8, gain: 1.0,
  },
  parlor: {
    label: '茶室（谁是卧底）',
    desc: '轻快 · 明亮',
    root: 146.8,                    // D3
    scale: [0, 2, 4, 7, 9],
    padWave: 'triangle', noteWave: 'sine',
    tempo: 5600, noteRate: 0.6, filter: 780, decay: 1.9, gain: 0.85,
  },
};

/** 剧本 → 专属曲目。加新本子时在这里补一行。 */
// 探案的案子各有各的曲子，规矩和剧本一样：一本一曲，不重样
const CASE_TRACK = {
  case_paint: 'turpentine',
};

const SCRIPT_TRACK = {
  dongmai: 'polar',          // 雪夜、封山——借极夜那首冷色的
  yanxian: 'rain',           // 雾锁长江，借海岛那首雨声的
  kechang: 'temple',         // 北宋贡院锁院夜，借古寺那首肃穆的
};

let ctx = null, master = null, current = 'none', timer = null;
let voices = [];
let volume = 0.10;          // 第一版是 0.28，太吵

function ensureCtx() {
  if (ctx) return ctx;
  ctx = new (window.AudioContext || window.webkitAudioContext)();

  master = ctx.createGain();
  master.gain.value = volume;

  // 总线上再压一层低通 + 软限幅，兜底掐掉任何刺耳成分
  const tone = ctx.createBiquadFilter();
  tone.type = 'lowpass';
  tone.frequency.value = 1400;
  tone.Q.value = 0.5;

  const comp = ctx.createDynamicsCompressor();
  comp.threshold.value = -32;
  comp.knee.value = 30;
  comp.ratio.value = 8;
  comp.attack.value = 0.05;
  comp.release.value = 0.6;

  // 廉价混响：几条不同延迟的反馈支路。反馈量刻意压低，避免长跑后糊成一团
  const wet = ctx.createGain();
  wet.gain.value = 0.22;
  for (const d of [0.23, 0.37, 0.53]) {
    const dl = ctx.createDelay(1.0);
    dl.delayTime.value = d;
    const fb = ctx.createGain();
    fb.gain.value = 0.22;
    const damp = ctx.createBiquadFilter();
    damp.type = 'lowpass';
    damp.frequency.value = 900;     // 阻尼：每次反馈都更闷一点，更像真实空间
    dl.connect(damp); damp.connect(fb); fb.connect(dl);
    master.connect(dl); dl.connect(wet);
  }

  master.connect(tone);
  wet.connect(tone);
  tone.connect(comp);
  comp.connect(ctx.destination);
  return ctx;
}

function stopVoices() {
  const t = ctx ? ctx.currentTime : 0;
  for (const v of voices) {
    try {
      v.gain.gain.cancelScheduledValues(t);
      v.gain.gain.setTargetAtTime(0, t, 0.5);
      v.osc.stop(t + 3);
    } catch { /* 已经停了 */ }
  }
  voices = [];
}

const freq = (root, semi) => root * Math.pow(2, semi / 12);

/** 起一个音。全程慢起慢落，没有任何瞬态。 */
function voice(cfg, hz, dur, peak, wave) {
  const t = ctx.currentTime;
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  const lp = ctx.createBiquadFilter();

  osc.type = wave;
  osc.frequency.value = hz;

  // 极慢的失谐，让长音不死板
  const det = ctx.createOscillator();
  const detGain = ctx.createGain();
  det.frequency.value = 0.05 + Math.random() * 0.09;
  detGain.gain.value = 1.6;
  det.connect(detGain); detGain.connect(osc.detune); det.start(t);

  lp.type = 'lowpass';
  lp.frequency.value = cfg.filter;
  lp.Q.value = 0.4;

  // 慢起 40% / 慢落 60%，避免"啵"的一声
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(peak, t + dur * 0.4);
  gain.gain.setTargetAtTime(0, t + dur * 0.5, dur * 0.4);

  osc.connect(lp); lp.connect(gain); gain.connect(master);
  osc.start(t);
  osc.stop(t + dur + 3);
  osc.onended = () => { try { det.stop(); } catch { /* 已停 */ } };

  voices.push({ osc, gain });
  if (voices.length > 36) voices = voices.slice(-20);
}

function tick(cfg) {
  const g = cfg.gain ?? 1;
  const deg = () => cfg.scale[Math.floor(Math.random() * cfg.scale.length)];

  // 和声垫：根音 + 五度 + 一个音阶音。音量都很低
  voice(cfg, freq(cfg.root, 0), cfg.decay * 3.4, 0.115 * g, cfg.padWave);
  voice(cfg, freq(cfg.root, 7), cfg.decay * 3.0, 0.070 * g, cfg.padWave);
  voice(cfg, freq(cfg.root, 12 + deg()), cfg.decay * 2.4, 0.045 * g, cfg.padWave);

  // 稀疏的高音点缀
  if (Math.random() < cfg.noteRate) {
    const delay = Math.random() * cfg.tempo * 0.55;
    setTimeout(() => {
      if (current === 'none' || !ctx) return;
      voice(cfg, freq(cfg.root, 24 + deg()), 2.6, 0.048 * g, cfg.noteWave);
    }, delay);
  }
}

export function play(name) {
  if (name === current) return;
  stop();
  current = name;
  if (name === 'none' || !TRACKS[name]) return;

  ensureCtx();
  if (ctx.state === 'suspended') ctx.resume();
  const cfg = TRACKS[name];
  tick(cfg);
  timer = setInterval(() => tick(cfg), cfg.tempo);
}

export function stop() {
  if (timer) { clearInterval(timer); timer = null; }
  stopVoices();
  current = 'none';
}

export function setVolume(v) {
  volume = Math.max(0, Math.min(1, v));
  if (master && ctx) master.gain.setTargetAtTime(volume, ctx.currentTime, 0.15);
}

export const getVolume = () => volume;
export const currentTrack = () => current;
export const list = () => Object.entries(TRACKS).map(([k, v]) => ({ key: k, ...v }));

/** 按剧本 / 游戏类型给专属曲目。 */
export function defaultFor(kind, scriptId) {
  if (kind === 'werewolf') return 'night';
  if (kind === 'spy') return 'parlor';
  if (kind === 'detective') return CASE_TRACK[scriptId] || 'turpentine';
  return SCRIPT_TRACK[scriptId] || 'manor';
}
