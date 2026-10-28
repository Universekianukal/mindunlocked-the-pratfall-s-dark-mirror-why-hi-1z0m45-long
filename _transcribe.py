import json
from faster_whisper import WhisperModel
m = WhisperModel("small.en", device="cpu", compute_type="int8")
segs,_ = m.transcribe("assets/vo.wav", word_timestamps=True, beam_size=5)
words=[]
for s in segs:
    for w in (s.words or []):
        t=w.word.strip()
        if t: words.append({"text":t,"start":round(w.start,3),"end":round(w.end,3)})
for i,w in enumerate(words): w["id"]=f"w{i}"
json.dump(words, open("transcript.json","w"), indent=0)
print(f"words: {len(words)}  span: {words[0]['start']}s - {words[-1]['end']}s")
print(" ".join(w["text"] for w in words[:40]))
