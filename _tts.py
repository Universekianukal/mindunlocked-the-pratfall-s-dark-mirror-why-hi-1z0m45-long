import re, os, numpy as np, soundfile as sf, kokoro_onnx
HOME = os.path.expanduser('~')
base = HOME + '/.cache/hyperframes/tts'
model = kokoro_onnx.Kokoro(base + '/models/kokoro-v1.0.onnx', base + '/voices/voices-v1.0.bin')
text = open('vo.txt', encoding='utf-8').read()
sents = re.split(r'(?<=[.!?])\s+', text.replace('\n', ' '))
chunks = []; cur = ''
for s in sents:
    s = s.strip()
    if not s: continue
    if len(cur) + len(s) + 1 <= 350: cur = (cur + ' ' + s).strip()
    else:
        if cur: chunks.append(cur)
        cur = s
if cur: chunks.append(cur)
print('chunks:', len(chunks))
sr = 24000; gap = np.zeros(int(0.35 * sr), dtype=np.float32); parts = []
for i, c in enumerate(chunks):
    samples, sr = model.create(c, voice='af_heart', speed=0.92)
    parts.append(np.asarray(samples, dtype=np.float32)); parts.append(gap)
    print(f'  {i+1}/{len(chunks)} ok ({len(samples)/sr:.1f}s)')
os.makedirs('assets', exist_ok=True)
audio = np.concatenate(parts)
sf.write('assets/vo.wav', audio, sr)
print('TOTAL %.1f sec -> assets/vo.wav' % (len(audio) / sr))
