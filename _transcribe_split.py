"""Transcribe assets/vo.wav in segments to stay under the hyperframes CLI's
hardcoded 5-minute internal whisper.cpp subprocess timeout. Splits with ffmpeg,
transcribes each segment independently, merges word lists with time offsets.
"""
import json, os, subprocess, math

SEG_LEN = 120.0  # seconds; safely under the CLI's 300s internal timeout
SAFE_DUR = 180.0  # below this, skip the split machinery entirely (avoids a bug in the ffmpeg-reencoded intermediate)
VO = 'assets/vo.wav'

dur = float(subprocess.run(
    ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', VO],
    capture_output=True, text=True).stdout.strip())

if dur <= SAFE_DUR:
    print(f"vo duration {dur:.1f}s <= {SAFE_DUR}s safe threshold, transcribing directly (no split)")
    r = subprocess.run(
        ['npx', '--yes', 'hyperframes@0.6.80', 'transcribe', VO, '--model', 'small.en', '--dir', '.', '--json'],
        capture_output=True, text=True)
    print('  cli stdout:', r.stdout.strip()[-2000:])
    print('  cli stderr:', r.stderr.strip()[-2000:])
    if r.returncode != 0:
        raise RuntimeError(f"transcribe failed (exit {r.returncode})")
    print(f"done -> transcript.json")
    raise SystemExit(0)

n_segs = max(1, math.ceil(dur / SEG_LEN))
print(f"vo duration {dur:.1f}s -> {n_segs} segment(s)")

words = []
os.makedirs('_tsplit', exist_ok=True)
for i in range(n_segs):
    start = i * SEG_LEN
    seg_path = f'_tsplit/seg{i}.wav'
    subprocess.run(
        ['ffmpeg', '-y', '-loglevel', 'error', '-i', VO, '-ss', str(start), '-t', str(SEG_LEN), seg_path],
        check=True)
    seg_dir = f'_tsplit/out{i}'
    os.makedirs(seg_dir, exist_ok=True)
    r = subprocess.run(
        ['npx', '--yes', 'hyperframes@0.6.80', 'transcribe', seg_path, '--model', 'small.en', '--dir', seg_dir, '--json'],
        capture_output=True, text=True)
    print('  cli stdout:', r.stdout.strip()[-2000:])
    print('  cli stderr:', r.stderr.strip()[-2000:])
    if r.returncode != 0:
        raise RuntimeError(f"transcribe failed on segment {i} (exit {r.returncode})")
    seg_words = json.load(open(f'{seg_dir}/transcript.json'))
    for w in seg_words:
        w['start'] = round(w['start'] + start, 3)
        w['end'] = round(w['end'] + start, 3)
        words.append(w)
    print(f"  segment {i}: {len(seg_words)} words")

for i, w in enumerate(words):
    w['id'] = f'w{i}'
json.dump(words, open('transcript.json', 'w'), indent=0)
print(f"merged {len(words)} words across {n_segs} segment(s) -> transcript.json")
