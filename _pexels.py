import json, os, sys, urllib.request, urllib.parse, time, subprocess

KEY = os.environ.get('PEXELS_API_KEY') or open('D:/Hyperframe/pexels_key.txt').read().strip()
WORKER_URL = 'https://mindunlocked-bot.everydayhypehq.workers.dev'
LEDGER_SECRET = os.environ.get('CLIP_LEDGER_SECRET')
DST = 'assets/clips'
os.makedirs(DST, exist_ok=True)

canvas = json.load(open('canvas.json')) if os.path.exists('canvas.json') else {'width': 1920, 'height': 1080}
PORTRAIT = canvas['height'] > canvas['width']
ORIENTATION = 'portrait' if PORTRAIT else 'landscape'

beats = json.load(open('beats.json'))
QUERIES = [(b['query'], b.get('count', 2)) for b in beats]

def api(url):
    req = urllib.request.Request(url, headers={'Authorization': KEY, 'User-Agent': 'Mozilla/5.0 HyperFrames'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())

# Cross-channel/cross-video no-repeat check, backed by the Worker's KV store
# (durable, reachable from any repo/channel). Previously this read a local
# file at a Windows-only path that never existed on GitHub's cloud runners,
# so no-repeat dedup silently never actually ran here.
def reserve(ids):
    if not ids:
        return set()
    if not LEDGER_SECRET:
        return set(ids)  # secret not configured: fail open rather than block the build
    req = urllib.request.Request(
        f'{WORKER_URL}/clips/reserve',
        data=json.dumps({'ids': ids}).encode(),
        headers={
            'Content-Type': 'application/json',
            'X-Clip-Ledger-Secret': LEDGER_SECRET,
            # Cloudflare's bot-fight mode blocks urllib's default UA (Python-urllib/x.y)
            # with error 1010 before the request even reaches the Worker.
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return set(json.loads(r.read())['reserved'])
    except Exception as e:
        print('  ! ledger reserve failed, allowing clip through:', e)
        return set(ids)  # ledger outage shouldn't break the whole build

def best_file(vfiles):
    if PORTRAIT:
        cand = [f for f in vfiles if f.get('height', 0) >= f.get('width', 0) and f.get('height', 0) >= 1200]
        if not cand: cand = [f for f in vfiles if f.get('height', 0) >= f.get('width', 0)]
        if not cand: cand = vfiles
        cand.sort(key=lambda f: abs(f.get('height', 0) - 1920))
    else:
        cand = [f for f in vfiles if f.get('width', 0) >= 1280 and f.get('width', 0) >= f.get('height', 0)]
        if not cand: cand = vfiles
        cand.sort(key=lambda f: abs(f.get('height', 0) - 1080))
    return cand[0] if cand else None

picked = []
seen = set()  # same-run fast path only; the ledger reserve() call is the real authority
for q, n in QUERIES:
    url = 'https://api.pexels.com/videos/search?' + urllib.parse.urlencode(
        {'query': q, 'orientation': ORIENTATION, 'size': 'medium', 'per_page': 15})
    try:
        data = api(url)
    except Exception as e:
        print('  ! query failed', q, e); continue
    got = 0
    for v in data.get('videos', []):
        vid = str(v['id'])
        if vid in seen: continue
        if v.get('duration', 0) < 4: continue
        bf = best_file(v.get('video_files', []))
        if not bf: continue
        if vid not in reserve([vid]):
            seen.add(vid)  # already used elsewhere (this or another channel), skip for good
            continue
        picked.append({'id': vid, 'q': q, 'link': bf['link'], 'w': bf.get('width'), 'h': bf.get('height'), 'dur': v.get('duration')})
        seen.add(vid); got += 1
        if got >= n: break
    print(f"{q!r}: picked {got}")
    time.sleep(0.4)

print(f"\nTOTAL picked: {len(picked)}")
json.dump(picked, open('_pexels_picked.json', 'w'), indent=1)

def download(url, out):
    # Pexels' CDN 403s requests with the default urllib User-Agent (looks like a bot).
    req = urllib.request.Request(url, headers={
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    })
    # Write to a temp name and rename only after the WHOLE body arrived. The old
    # `open(out,'wb')` before `r.read()` left a 0-byte file when the read raised
    # IncompleteRead; that file passed the exists() check and later crashed the
    # render of whichever chunk used it (ffprobe exit 1).
    tmp = out + '.part'
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        if not data:
            raise IOError('empty response body')
        with open(tmp, 'wb') as f:
            f.write(data)
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

print("downloading...")
for i, p in enumerate(picked):
    out = f"{DST}/p{p['id']}.mp4"
    if os.path.exists(out) and os.path.getsize(out) > 0: continue
    for attempt in range(3):
        try:
            download(p['link'], out)
            print('  +', out, p['w'], 'x', p['h'])
            break
        except Exception as e:
            print('  ! dl failed', p['id'], f'(attempt {attempt + 1}/3)', e)
            time.sleep(2 * (attempt + 1))

clips = []
for p in picked:
    f = f"p{p['id']}.mp4"
    path = f"{DST}/{f}"
    if not os.path.exists(path):
        continue
    if os.path.getsize(path) == 0:
        os.remove(path)
        print('  ! dropped empty clip', f)
        continue
    try:
        dur = float(subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
            capture_output=True, text=True).stdout.strip())
    except Exception:
        # Unreadable file: keeping it with a guessed duration is what let a
        # broken clip reach the renderer. Drop it; other clips cover the beat.
        os.remove(path)
        print('  ! dropped unreadable clip', f)
        continue
    clips.append({'id': p['id'], 'q': p['q'], 'file': f, 'dur': round(dur, 2), 'w': p['w'], 'h': p['h']})
json.dump(clips, open('_clips.json', 'w'), indent=1)
print("done. clips in _clips.json:", len(clips))
