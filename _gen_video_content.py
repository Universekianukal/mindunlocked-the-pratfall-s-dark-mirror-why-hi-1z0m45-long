"""Generate vo.txt, beats.json, highlight_words.json, cta.json, canvas.json,
and meta.json from a single TOPIC string, via one LLM call. Long-form variant:
~1000-1100 word narration, landscape canvas, chunk count scaled to length.

Only runs when vo.txt isn't already committed (mirrors the TTS-skip pattern),
so a hand-authored video in this repo is never overwritten.
"""
import json
import os
import re
import datetime

import _llm

WPM = 165  # calibrated from existing MindUnlocked long-form scripts
MODEL = "claude-sonnet-5"  # used only when LLM_PROVIDER resolves to anthropic

SYS = """You are the writer for MindUnlocked, a YouTube channel that explains
psychology phenomena in a hook-driven, conversational, second-person style —
built to feel like a sharp friend explaining something that just clicked, not
a lecture.

Write the narration through these beats, IN ORDER. Do NOT include literal
section labels (no "[HOOK]", no "Segment 1") in the output — this is pacing
guidance only; the vo must read as one continuous piece of narration with a
blank-line paragraph break between beats:

1. Hook (one line): a punchy, counterintuitive claim in caps-adjacent tone
   (not literal all-caps) that states the phenomenon's *effect* without
   naming it yet.
2. Open loop: 2-3 short, concrete "you've felt this" examples the viewer has
   personally lived — real situations, not abstractions. Then name the
   specific psychological phenomenon and, briefly, who identified it and when.
3. TWO distinct segments, each built around ONE real, specific, named study or
   historical anecdote — real researcher name(s), approximate year, and a
   concrete number or experimental detail (e.g. "in 1975, researcher Stephen
   Worchel gave one group a jar of ten cookies and another group a jar of
   two — same cookies, same bakery"). The two studies must demonstrate the
   phenomenon from two different angles (e.g. one behavioral/experimental,
   one relational/social), not the same angle twice. After each study,
   translate the finding back into an everyday relatable moment and land it
   with a direct question to the viewer ("Have you ever...?").
4. Mechanism: explain the evolutionary or psychological "why" in plain
   language — what mental shortcut is running, and why it made sense for
   most of human history even though it can misfire today.
5. Identity close: a practical, specific reframe the viewer can actually use
   — not generic advice like "be mindful," but a concrete two-second gut-check
   or question they can ask themselves in the moment.
6. A natural comment-prompt CTA line growing out of the topic (not bolted on).

Style rules, calibrated from the channel's existing scripts:
- Second person ("you", "your brain"), short punchy sentences mixed with
  longer explanatory ones. Paragraph breaks between beats (blank line).
- Concrete numbers and named people beat vague generalities everywhere.
- Target length: 1000-1100 words.

Also produce:
- beats: a list of ~80-95 short visual search phrases (3-6 words each, like
  stock-footage search queries: "person shocked surprised face close up",
  "two men shaking hands meeting friendly", "brain neurons synapse science
  animation") that sequentially match the narration's emotional/narrative beats
  in order, each with a "count" of 2 (use 3 for especially important recurring
  concepts). These will be used to fetch real stock footage, so keep them
  concrete and visual, not abstract.
- highlight_words: 30-50 lowercase single words from the script (no phrases)
  that are the emotionally/conceptually key words worth visually emphasizing
  in captions (names, the phenomenon's name, striking verbs/adjectives).
- cta_q: a short, punchy discussion question related to the topic, for an
  end-card (e.g. "What favor could you ask this week?").
- cta_chip: a short comment-prompt like "Comment WORD \U0001F447" using one
  striking word from the topic.
- thumbnail_headline: ONE punchy phrase or short clause for a YouTube
  thumbnail, mixed case (not ALL CAPS), editorial/documentary tone (think Vox
  or The Atlantic explainer covers) — e.g. "The One Missing Piece Your Brain
  Won't Let Go Of". This is a SEPARATE hook from the video title, generated
  later by a different process — pull a specific, vivid detail straight from
  the script you just wrote (a stat, a phrase from the study, the mechanism,
  a direct question) rather than a generic summary, since it needs to stand
  alone as its own reason to click. Vary structure per topic (question,
  statement, a number, etc.) — don't reuse the same template every time.
- thumbnail_subline: ONE short supporting sentence underneath the headline,
  plain/lighter tone, adding one more concrete detail from the script.
- thumbnail_category: a topic-specific tag, 1-3 words, ALL CAPS (e.g.
  "COGNITIVE BIAS", "MEMORY", "SOCIAL PSYCHOLOGY") — new information for the
  viewer, never the channel name.
- thumbnail_image_prompt: a photorealistic image for a FULL-BLEED 1280x720
  background — either a symbolic/conceptual object or scene directly related
  to the topic (preferred when it captures the idea well — e.g. a single
  missing puzzle piece for an "unfinished tasks" topic) OR a portrait if the
  topic is more personal/emotional, composed with darker, emptier negative
  space toward the BOTTOM of the frame for a text scrim. Cinematic, moody,
  documentary-photography lighting. If a portrait: prefer face/shoulders-up
  framing; AVOID prompts requiring detailed close-up hands or hands
  interacting with objects — free image models reliably render hands wrong
  (extra/missing fingers). Do NOT mention any text, words, letters, numbers,
  logos, or UI elements — describe only the photo itself. Also AVOID any
  object that inherently implies visible writing even if you never ask for
  text explicitly — newspapers, books, signs, screens/monitors, letters,
  documents, horoscope columns, handwritten notes — free image models always
  render fake garbled text on these, which fails QA. Pick a symbolic object
  or scene with no legible surfaces at all.

Return JSON: {"vo": "...", "beats": [{"query":"","count":2}, ...],
"highlight_words": ["", ...], "cta_q": "", "cta_chip": "",
"thumbnail_headline": "", "thumbnail_subline": "", "thumbnail_category": "",
"thumbnail_image_prompt": ""}
Respond with ONLY the JSON object — no markdown code fences, no other text."""

MIN_WORDS, MAX_WORDS = 850, 1300  # target 1000-1100; LLMs don't reliably hit
# prose length instructions on the first try (observed as low as ~40% of
# target in testing on the short-form variant with gpt-4o-mini), so this is
# enforced with a retry loop rather than trusted, regardless of model.


def extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return json.loads(t, strict=False)


def generate(client, topic, hook_feedback=""):
    user = f"Topic: {topic}"
    if hook_feedback:
        user += (
            "\n\nPERFORMANCE FEEDBACK from this channel's recent videos (use it to "
            "write a stronger HOOK/opening line -- this is guidance, not content to "
            f"restate):\n{hook_feedback}"
        )
    messages = [{"role": "user", "content": user}]
    d = None
    for attempt in range(4):
        resp = client.messages.create(
            model=MODEL,
            # 16384 (was 8192): glm-5p2 on Fireworks spends part of the budget
            # on a reasoning pass before the JSON, so a smaller cap truncates
            # the script. Harmless on the Anthropic path -- it stops at end_turn.
            max_tokens=16384,
            system=SYS,
            messages=messages,
        )
        raw = _llm.only_text(resp)
        try:
            d = extract_json(raw)
        except json.JSONDecodeError as e:
            print(f"attempt {attempt + 1}: malformed JSON ({e}), retrying")
            messages.append({"role": "assistant", "content": raw})
            messages.append({
                "role": "user",
                "content": (
                    "That response was not valid JSON (" + str(e) + "). "
                    "A likely cause is an unescaped quote or control character inside a "
                    "string value. Return the SAME content as strict, valid JSON — "
                    "escape every double-quote and newline inside string values properly."
                ),
            })
            continue
        words = len(d["vo"].split())
        if MIN_WORDS <= words <= MAX_WORDS:
            return d
        print(f"attempt {attempt + 1}: {words} words, outside [{MIN_WORDS},{MAX_WORDS}], retrying")
        messages.append({"role": "assistant", "content": raw})
        messages.append({
            "role": "user",
            "content": (
                f"Your script's \"vo\" field was {words} words. It MUST be between "
                f"{MIN_WORDS} and {MAX_WORDS} words. "
                + ("Expand it with more concrete detail/example, same story." if words < MIN_WORDS
                   else "Tighten it, same story.")
                + " Return the full corrected JSON."
            ),
        })
    if d is None:
        raise RuntimeError("Model never returned valid JSON after 4 attempts")
    return d  # last attempt, even if still out of range


def main():
    if os.path.exists("vo.txt"):
        print("vo.txt already present, skipping content generation")
        return

    topic = os.environ["TOPIC"]
    client = _llm.client()
    d = generate(client, topic, os.environ.get("HOOK_FEEDBACK", "").strip())

    open("vo.txt", "w", encoding="utf-8").write(d["vo"].strip() + "\n")
    json.dump(d["beats"], open("beats.json", "w"), indent=1)
    json.dump(d["highlight_words"], open("highlight_words.json", "w"))

    cta = {
        "q": d["cta_q"],
        "chip": d["cta_chip"],
        "follow": "Subscribe now \U0001F447",
        "tail_after_vo": 2.0,
        "at_offset_end": 10.5,
    }
    json.dump(cta, open("cta.json", "w"), indent=2)

    json.dump({
        "headline": d["thumbnail_headline"],
        "subline": d["thumbnail_subline"],
        "category": d["thumbnail_category"],
        "image_prompt": d["thumbnail_image_prompt"],
    }, open("thumbnail_spec.json", "w"), indent=2)

    words = len(d["vo"].split())
    est_dur = words / WPM * 60
    chunks = max(1, round(est_dur / 95))
    json.dump({"width": 1920, "height": 1080, "chunks": chunks}, open("canvas.json", "w"))

    slug = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")[:50]
    json.dump(
        {"id": slug, "name": slug, "createdAt": datetime.datetime.utcnow().isoformat() + "Z"},
        open("meta.json", "w"), indent=2,
    )

    print(f"Generated: {words} words (~{est_dur:.0f}s est), {len(d['beats'])} beats, {chunks} chunks")


if __name__ == "__main__":
    main()
