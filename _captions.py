import json, re

words = json.load(open('transcript.json'))
clean = []
for w in words:
    parts = [p for p in w['text'].split('�') if p != '']
    if len(parts) <= 1:
        clean.append({'text': w['text'].replace('�', ''), 'start': w['start'], 'end': w['end']})
    else:
        n = len(parts); s = w['start']; e = w['end']; st = (e - s) / n
        for i, p in enumerate(parts):
            clean.append({'text': p, 'start': round(s + i * st, 3), 'end': round(s + (i + 1) * st, 3)})

HL = set(w.strip().lower() for w in json.load(open('highlight_words.json')) if w.strip())

def norm(t):
    return re.sub(r'[^a-z-]', '', t.lower())

groups = []; GS = 3; i = 0; N = len(clean)
while i < N:
    ch = clean[i:i + GS]
    groups.append({'start': round(ch[0]['start'], 2), 'end': round(ch[-1]['end'], 2),
                    'words': [{'t': c['text'], 'hl': norm(c['text']) in HL} for c in ch]})
    i += GS
for j, g in enumerate(groups):
    if j + 1 < len(groups):
        de = min(g['end'] + 0.5, groups[j + 1]['start']); g['disp_end'] = round(max(de, g['end']), 2)
    else:
        g['disp_end'] = round(g['end'] + 0.4, 2)
json.dump(groups, open('captions.json', 'w')); print(len(groups), 'groups; last', groups[-1]['disp_end'])
