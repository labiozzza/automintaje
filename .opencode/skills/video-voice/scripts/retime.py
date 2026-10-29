# Темп и паузы озвучки + таймлайн для монтажа.
#   python3 retime.py vo.mp3 vo_words.json [--tempo 1.2] [--pause 0.3] [--lead 0.3] [--tail 2]
#                     [--text vo_text.txt] [--size 1080x1920] [--fps 30] [--thr -40]
# Что делает:
#   1) паузы длиннее --pause (по громкости, тише --thr дБ) укорачивает до --pause;
#   2) ускоряет голос ×--tempo без изменения высоты (ffmpeg atempo);
#   3) добавляет --lead секунд тишины в начало;
#   4) пересчитывает время слов и пишет timeline.json + timeline.js (window.TL) для шаблона сцен;
#   5) если дан --text (текст диктора), подставляет в субтитры слова из текста (пунктуация, ё, регистр), а не whisper.
# Выход: vo_fast.wav, timeline.json, timeline.js. Нужны: Python 3, numpy, ffmpeg.
import argparse, difflib, json, re, subprocess, sys
import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument('audio'); ap.add_argument('words')
ap.add_argument('--tempo', type=float, default=1.0); ap.add_argument('--pause', type=float, default=0.3)
ap.add_argument('--lead', type=float, default=0.3); ap.add_argument('--tail', type=float, default=2.0)
ap.add_argument('--text'); ap.add_argument('--size', default='1080x1920'); ap.add_argument('--fps', type=int, default=30)
ap.add_argument('--thr', type=float, default=-40.0)
A = ap.parse_args()
SR = 48000

x = np.frombuffer(subprocess.run(['ffmpeg', '-v', 'error', '-i', A.audio, '-f', 'f32le', '-ac', '1', '-ar', str(SR), '-'],
                                 capture_output=True, check=True).stdout, np.float32)
dur0 = len(x) / SR

# 1. паузы по громкости: окна 10 мс
hop = SR // 100
rms = np.sqrt(np.add.reduceat(x.astype(np.float64) ** 2, np.arange(0, len(x), hop)) / hop + 1e-12)
quiet = 20 * np.log10(rms) < A.thr
keep, a, end, i, n = [], 0.0, dur0, 0, len(quiet)
while i < n:
    if quiet[i]:
        j = i
        while j < n and quiet[j]: j += 1
        s0, s1 = i / 100, j / 100
        if i == 0: a = max(0.0, s1 - 0.05)                # тишина в начале → 50 мс (вступление задаёт --lead)
        elif j == n: end = min(dur0, s0 + 0.1)            # тишина в конце → 100 мс
        elif s1 - s0 > A.pause:                           # длинная пауза → оставить --pause (половина с каждой стороны)
            keep.append((a, s0 + A.pause / 2)); a = s1 - A.pause / 2
        i = j
    else: i += 1
keep.append((a, end))

# склейка кусков с микрофейдами 5 мс (без щелчков)
F = int(.005 * SR); ramp = np.linspace(0, 1, F, dtype=np.float32)
chunks = []
for s0, s1 in keep:
    c = x[int(s0 * SR):int(s1 * SR)].copy()
    if len(c) > 2 * F: c[:F] *= ramp; c[-F:] *= ramp[::-1]
    chunks.append(c)
y = np.concatenate(chunks).astype(np.float32)

# 2–3. темп и вступление
y = np.frombuffer(subprocess.run(['ffmpeg', '-v', 'error', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '-',
                                  '-af', f'atempo={A.tempo}', '-f', 'f32le', '-'], input=y.tobytes(), capture_output=True, check=True).stdout, np.float32)
y = np.concatenate([np.zeros(int(A.lead * SR), np.float32), y])   # нули только float32, иначе вместо голоса шум
subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'f32le', '-ar', str(SR), '-ac', '1', '-i', '-', 'vo_fast.wav'], input=y.tobytes(), check=True)
dur1 = len(y) / SR

# 4. время слов: исходник → результат
starts = np.cumsum([0] + [s1 - s0 for s0, s1 in keep])
def remap(t):
    for k, (s0, s1) in enumerate(keep):
        if t <= s1: return (starts[k] + max(0.0, t - s0)) / A.tempo + A.lead
    return starts[-1] / A.tempo + A.lead
W = json.load(open(A.words))['words']
words = [{'w': w['word'], 't0': round(remap(w['start']), 3), 't1': round(remap(w['end']), 3)} for w in W]

# 5. текст диктора вместо whisper
norm = lambda s: re.sub(r'[^\w]', '', s.lower().replace('ё', 'е'))
if A.text:
    T = open(A.text, encoding='utf-8').read().split()
    sm = difflib.SequenceMatcher(a=[norm(w['w']) for w in words], b=[norm(t) for t in T], autojunk=False)
    for op, i0, i1, j0, j1 in sm.get_opcodes():
        if op == 'equal' or (op == 'replace' and i1 - i0 == j1 - j0):
            for k in range(i1 - i0): words[i0 + k]['w'] = T[j0 + k]
        elif op != 'insert':
            print('сверить вручную:', ' '.join(w['w'] for w in words[i0:i1]), '↔', ' '.join(T[j0:j1]) or '(нет в тексте)')

w_, h_ = map(int, A.size.split('x'))
TL = {'fps': A.fps, 'w': w_, 'h': h_, 'vo_dur': round(dur1, 3), 'end': round(dur1 + A.tail, 2), 'words': words}
json.dump(TL, open('timeline.json', 'w'), ensure_ascii=False, indent=1)
open('timeline.js', 'w', encoding='utf-8').write('window.TL=' + json.dumps(TL, ensure_ascii=False) + ';\n')
print(f'голос {dur0:.1f} с → {dur1:.1f} с (паузы: {len(keep) - 1} укорочено, темп ×{A.tempo}), ролик {TL["end"]} с, слов {len(words)}')
print('vo_fast.wav, timeline.json, timeline.js')
