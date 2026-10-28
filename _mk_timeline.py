"""Build _timeline.json from beats.json + _clips.json + captions.json + cta.json.

Generic across long-form and short-form: no per-video hand-authored ORDER list.
beats.json:  [{"query": "...", "count": N}, ...]   (count optional, unused here — _pexels.py already fetched)
cta.json:    {"q": "...", "chip": "...", "follow": "...", "pre": "..." (optional),
              "tail_after_vo": 2.0 (optional, default below), "at_offset_end": 14.5 (optional, default below)}
"""
import json, subprocess
from collections import defaultdict

allc = json.load(open('_clips.json')); clips = {c['id']: c for c in allc}
pools = defaultdict(list)
for c in allc: pools[c['q']].append(c['id'])

beats = json.load(open('beats.json'))
ORDER = [b['query'] for b in beats]

used = set()
def draw(t):
    for cid in pools.get(t, []):
        if cid not in used: used.add(cid); return cid
    for k in pools:
        for cid in pools[k]:
            if cid not in used: used.add(cid); return cid
    return None

playlist = [draw(t) for t in ORDER]
playlist = [c for c in playlist if c]

vo_dur = float(subprocess.run(
    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', 'assets/vo.wav'],
    capture_output=True, text=True).stdout.strip())

CTA = json.load(open('cta.json'))
TAIL_AFTER_VO = CTA.get('tail_after_vo', 2.0)
AT_OFFSET_END = CTA.get('at_offset_end', 14.5)

COMP = round(vo_dur + TAIL_AFTER_VO, 2)
CTA_AT = round(COMP - AT_OFFSET_END, 2)

OVERLAP = 0.35; MS = 0.6; N = len(playlist)
target = COMP + OVERLAP * (N - 1)
maxd = [max(2.0, min(clips[c]['dur'] - MS, 6.0)) for c in playlist]
lo, hi = 0.0, 6.0
for _ in range(60):
    L = (lo + hi) / 2
    if sum(min(m, L) for m in maxd) < target: lo = L
    else: hi = L
L = (lo + hi) / 2; durs = [round(min(m, L), 2) for m in maxd]
starts = [0.0]
for i in range(1, N): starts.append(round(starts[i - 1] + durs[i - 1] - OVERLAP, 2))

# Auto-place flash-transition beats: evenly spaced through the pre-CTA region.
flash_count = 3 if COMP > 60 else 1
flashes = [round((i + 1) / (flash_count + 1) * CTA_AT, 2) for i in range(flash_count)]

caps = [g for g in json.load(open('captions.json')) if g['start'] < CTA_AT - 0.3]

T = {
    "comp": COMP, "vo_dur": round(vo_dur, 2), "cta_at": CTA_AT,
    "cta": CTA,
    "clips": [{"start": starts[i], "dur": durs[i], "file": clips[c]['file']} for i, c in enumerate(playlist)],
    "caps": caps,
    "flashes": flashes,
}
json.dump(T, open('_timeline.json', 'w'), ensure_ascii=False)
print("timeline:", N, "clips, comp", COMP, "vo_dur", round(vo_dur, 2), "cta_at", CTA_AT, "last end", round(starts[-1] + durs[-1], 1))
