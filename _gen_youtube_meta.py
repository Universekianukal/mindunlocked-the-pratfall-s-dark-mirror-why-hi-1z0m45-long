"""Generate youtube.json (title/description/tags) from this video's actual
script (vo.txt), so upload metadata is unique per video instead of hand-written.
"""
import json
import os
import re

import _llm

MODEL = "claude-sonnet-5"  # used only when LLM_PROVIDER resolves to anthropic

SYS = """You write YouTube metadata for a psychology/self-improvement channel called
MindUnlocked. Given a video's narration script, produce:
- title: under 100 chars, curiosity-driven but accurate to the content, no clickbait lies
- description: 2-4 short paragraphs summarizing the actual content, end with a
  subscribe call-to-action line "Subscribe now \U0001F447 https://www.youtube.com/@mindunlocked108"
- tags: 5-10 relevant lowercase keyword tags, no hashtags

Return JSON: {"title": "", "description": "", "tags": ["", ...]}
Respond with ONLY the JSON object — no markdown code fences, no other text."""


def extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return json.loads(t)


def main():
    # Mirrors the vo.txt-skip pattern in _gen_video_content.py: a
    # pre-committed youtube.json (hand-authored or pregen-supplied) is
    # never overwritten by a fresh metadata generation call.
    if os.path.exists("youtube.json"):
        print("youtube.json already present, skipping generation")
        return

    script = open("vo.txt").read()
    client = _llm.client()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYS,
        messages=[{"role": "user", "content": script}],
    )
    meta = extract_json(_llm.only_text(resp))

    out = {
        "title": meta["title"],
        "description": meta["description"],
        "tags": meta["tags"],
        "categoryId": "27",
        "privacyStatus": "private",
    }
    json.dump(out, open("youtube.json", "w"), indent=2)
    print(f"Generated youtube.json: {out['title']}")


if __name__ == "__main__":
    main()
