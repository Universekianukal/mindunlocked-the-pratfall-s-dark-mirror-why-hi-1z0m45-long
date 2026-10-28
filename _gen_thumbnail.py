"""Generate thumbnail.png: a FLUX.1-schnell portrait (Kaggle GPU, no text baked in --
free/open models render text badly, confirmed by the user) composited with a real
HTML/CSS headline overlay (guaranteed-crisp text, screenshotted via chrome-headless-shell).

Best-effort and non-fatal by design: main() catches everything and exits 0 without
producing thumbnail.png on any failure (Kaggle queue congestion, GPU unavailable, etc.)
so a flaky Kaggle run never blocks the video from uploading -- YouTube just falls back
to an auto-selected frame, same as if this script never ran.

Layout mirrors a reference the user approved: subject in the left third (dark gradient
bleeding right), 2-3 stacked bold headline lines on the right, one line in accent yellow.

Auth: ANTHROPIC_API_KEY (headline copy) + KAGGLE_ACCESS_TOKEN (image gen, written to
~/.kaggle/access_token by the workflow before this runs, matching the account used for
everydayhype's proven FLUX pipeline) + HF_TOKEN (required -- FLUX.1-schnell is a GATED
repo on HuggingFace despite the Apache-2.0 license; confirmed live via a 401
GatedRepoError without it. Must also click "Agree" on
huggingface.co/black-forest-labs/FLUX.1-schnell once per HF account).
"""
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import _llm

MODEL = "claude-sonnet-5"  # used only when LLM_PROVIDER resolves to anthropic
W, H = 1280, 720
WORK = Path("_thumb_work")
KDIR = WORK / "kernel"
# Unique per run -- a fixed shared slug would let two videos' thumbnail jobs
# running concurrently (e.g. a long+short pair) overwrite each other's kernel
# push mid-poll, since `kernels output`/`status` operate on the latest version
# of the slug, not the version a specific caller pushed.
#
# GITHUB_REPOSITORY alone is NOT enough: "Clean up & retry" deletes and
# recreates the repo under the identical name, so a retry reuses the same slug
# as the failed build's orphaned kernel version -- the poll then locks onto
# that stale version and `kernels output` pulls no fresh portrait, silently
# dropping the thumbnail (confirmed as the "no thumbnail after retry" bug).
# GITHUB_RUN_ID is unique per workflow run, so folding it in gives every
# attempt (first build AND each retry) its own kernel slug.
#
# Hashed rather than a truncated literal repo name: a truncated name (a) can
# land right after a hyphen, which Kaggle 400s on ("kernel title does not
# resolve to the specified id" -- its own slugification strips the trailing
# hyphen the literal id string doesn't), and (b) combined with the
# "mindunlocked-thumb-" prefix plus the repo name's own "mindunlocked-"
# prefix, produced a 58-char slug that also 400'd (Kaggle's own length
# limit) -- both confirmed live. A hash sidesteps both: fixed short length,
# no hyphens at all.
_slug_source = "{}:{}".format(
    os.environ.get("GITHUB_REPOSITORY", ""), os.environ.get("GITHUB_RUN_ID", "")
).strip(":") or uuid.uuid4().hex
_slug = hashlib.sha1(_slug_source.encode()).hexdigest()[:16]
KERNEL_ID = f"anuragmishra108/mindunlocked-thumb-{_slug}"

KERNEL_TMPL = r'''import sys, subprocess, traceback

def log(msg):
    with open("/kaggle/working/status.txt", "a") as f:
        f.write(msg + "\n")

def pip(*a):
    r = subprocess.run([sys.executable,"-m","pip","install","-q",*a], capture_output=True, text=True)
    log(f"pip {a} rc={r.returncode}")
    if r.returncode != 0:
        log("PIP STDERR:\n" + r.stderr[-3000:])

try:
    pip("torch==2.4.1","torchvision==0.19.1","--index-url","https://download.pytorch.org/whl/cu121")
    pip("diffusers==0.32.2","transformers==4.46.3","accelerate","sentencepiece","protobuf","bitsandbytes")
    log("installs done")
    import torch
    from huggingface_hub import login; login(token=%(hf)r)
    from diffusers import FluxPipeline, FluxTransformer2DModel, BitsAndBytesConfig as DBnb
    from transformers import T5EncoderModel, BitsAndBytesConfig as TBnb
    log("imports + hf login done")
    repo="black-forest-labs/FLUX.1-schnell"; nf4=dict(load_in_4bit=True,bnb_4bit_quant_type="nf4",bnb_4bit_compute_dtype=torch.float16)
    tf=FluxTransformer2DModel.from_pretrained(repo,subfolder="transformer",quantization_config=DBnb(**nf4),torch_dtype=torch.float16)
    log("transformer loaded")
    te=T5EncoderModel.from_pretrained(repo,subfolder="text_encoder_2",quantization_config=TBnb(**nf4),torch_dtype=torch.float16)
    log("text encoder loaded")
    pipe=FluxPipeline.from_pretrained(repo,transformer=tf,text_encoder_2=te,torch_dtype=torch.float16); pipe.enable_model_cpu_offload()
    log("pipeline ready")
    img=pipe(%(prompt)r,num_inference_steps=4,guidance_scale=0.0,height=768,width=1280,max_sequence_length=256,
             generator=torch.Generator("cpu").manual_seed(1000)).images[0]
    log("image generated")
    img.save("/kaggle/working/portrait.png")
    log("DONE")
    print("DONE", flush=True)
except Exception:
    log("EXCEPTION:\n" + traceback.format_exc())
    raise
'''


def extract_json(text):
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\n", "", t)
        t = re.sub(r"\n```$", "", t)
    return json.loads(t)


def gen_copy(client, topic, context, regen_hint=None):
    resp = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=(
            'You write YouTube thumbnail copy for MindUnlocked, a psychology channel, in an '
            'editorial/documentary style (think Vox or The Atlantic explainer covers, not a '
            'bold-caps influencer template). Given a topic and its title+description, produce:\n'
            '- headline: ONE punchy phrase or short clause, mixed case (not ALL CAPS), readable as '
            'a real sentence fragment -- e.g. "The One Missing Piece Your Brain Won\'t Let Go Of". '
            'CRITICAL: this must NOT be a paraphrase or shortened restatement of the video title -- '
            'the viewer reads title and thumbnail together, so repeating the same claim in both wastes '
            'the second impression. Pull a different angle: a surprising detail from the description '
            'that ISN\'T in the title, a specific number/stat, a direct question to the viewer, or the '
            'raw emotional stakes -- something that makes someone who already read the title still want '
            'to click. Vary structure topic to topic (question, statement, a number, etc.).\n'
            '- subline: ONE short supporting sentence underneath the headline, plain/lighter tone, '
            'adding one more concrete detail (e.g. "Why unfinished tasks stick in memory twice as hard '
            'as finished ones.").\n'
            '- category: a topic-specific tag, 1-3 words, ALL CAPS (e.g. "COGNITIVE BIAS", "MEMORY", '
            '"SOCIAL PSYCHOLOGY") -- new information for the viewer, never the channel name (YouTube '
            'already shows that under the thumbnail in every feed view, so repeating it wastes space).\n'
            '- image_prompt: a photorealistic image for a FULL-BLEED 1280x720 background -- can be '
            'either a symbolic/conceptual object or scene directly related to the topic (preferred when '
            'it captures the idea well -- e.g. a single missing puzzle piece for an "unfinished tasks" '
            'topic) OR a portrait if the topic is more personal/emotional, composed with darker, emptier '
            'negative space toward the BOTTOM of the frame for a text scrim. Cinematic, moody, '
            'documentary-photography lighting. If a portrait: prefer face/shoulders-up framing; AVOID '
            'prompts requiring detailed close-up hands or hands interacting with objects -- free image '
            'models reliably render hands wrong (extra/missing fingers). Do NOT mention any text, '
            'words, letters, numbers, logos, or UI elements -- describe only the photo itself.\n'
            'Respond with ONLY the JSON object {"headline":"","subline":"","category":"",'
            '"image_prompt":""} -- no markdown code fences, no other text.'
        ),
        messages=[{"role": "user", "content": f"Topic: {topic}\n\n{context[:1500]}\n{regen_hint or ''}"}],
    )
    raw = _llm.only_text(resp)
    return extract_json(raw)


def plain_copy(topic, meta):
    """No-LLM headline: the topic itself, so the image step can still run."""
    t = (meta.get("title") or topic or "").strip()
    return {
        "headline": t.split(":")[0].strip()[:60] or "Mind Unlocked",
        "subline": "",
        "category": "PSYCHOLOGY",
        "image_prompt": (
            f"cinematic portrait of a thoughtful person, dramatic rim lighting, "
            f"dark moody background, symbolising the idea of {topic or t}, "
            "shallow depth of field, ultra detailed"
        ),
    }


def kaggle(*args):
    env = dict(os.environ, PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    r = subprocess.run(["kaggle", *args], capture_output=True, text=True, env=env)
    if r.returncode != 0:
        raise RuntimeError(f"kaggle {' '.join(args)} failed (rc={r.returncode}): {r.stderr.strip()}")
    return r.stdout


def gen_portrait(image_prompt):
    KDIR.mkdir(parents=True, exist_ok=True)
    style = (
        ", dark cinematic, absolutely no text, no letters, no numbers, no logos, "
        "no watermark, no UI, no signage"
    )
    (KDIR / "gen.py").write_text(KERNEL_TMPL % {"prompt": image_prompt + style, "hf": os.environ["HF_TOKEN"]})
    (KDIR / "kernel-metadata.json").write_text(json.dumps({
        "id": KERNEL_ID, "title": KERNEL_ID.split("/")[1], "code_file": "gen.py",
        "language": "python", "kernel_type": "script", "is_private": True,
        "enable_gpu": True, "enable_internet": True,
        "dataset_sources": [], "competition_sources": [], "kernel_sources": [], "model_sources": [],
    }))
    print("[thumb] pushing FLUX kernel...", flush=True)
    kaggle("kernels", "push", "-p", str(KDIR))
    # Cold start (pip installs + weight download) can run long on the free GPU queue.
    # This job runs in parallel with the render-chunk matrix, so a generous budget here
    # doesn't slow down the video itself -- only the (optional) thumbnail.
    consecutive_status_errors = 0
    for _ in range(40):
        time.sleep(60)
        try:
            st = kaggle("kernels", "status", KERNEL_ID)
        except RuntimeError as e:
            # Kaggle can 404 briefly right after a push while it finishes
            # indexing the new kernel/version -- transient, not a real error.
            # Only give up if it keeps happening (a genuinely broken kernel
            # id/auth would fail every time, not just once or twice).
            consecutive_status_errors += 1
            print(f"[thumb] status check failed (attempt {consecutive_status_errors}), retrying: {e}", flush=True)
            if consecutive_status_errors >= 5:
                raise
            continue
        consecutive_status_errors = 0
        print("[thumb]", st.strip(), flush=True)
        if "COMPLETE" in st:
            break
        if "ERROR" in st:
            _dump_status()
            raise RuntimeError("FLUX kernel ERROR:\n" + st)
    else:
        _dump_status()
        raise TimeoutError("FLUX kernel timed out after 40 min")
    kaggle("kernels", "output", KERNEL_ID, "-p", str(WORK))
    portrait = WORK / "portrait.png"
    if not portrait.exists():
        raise RuntimeError("kernel completed but portrait.png missing from output")
    return portrait


def _dump_status():
    """Best-effort: pull whatever partial output exists (incl. status.txt with the
    kernel's own progress log / traceback) so a failure is diagnosable from the
    Actions log instead of a bare 'ERROR' with no detail."""
    try:
        kaggle("kernels", "output", KERNEL_ID, "-p", str(WORK))
        status_file = WORK / "status.txt"
        if status_file.exists():
            print("[thumb] kernel status.txt:\n" + status_file.read_text(errors="replace"), flush=True)
    except Exception as e:
        print(f"[thumb] couldn't pull diagnostic output: {e}", flush=True)


def b64(p):
    return "data:image/png;base64," + base64.b64encode(Path(p).read_bytes()).decode()


def chrome_bin():
    for c in [os.environ.get("CHROME_BIN"), "/usr/bin/google-chrome",
              "/usr/bin/chromium-browser", "chrome-headless-shell"]:
        if c and (shutil.which(c) or Path(c).exists()):
            return c
    raise RuntimeError("no chrome found; set CHROME_BIN")


# Editorial/documentary layout (Vox/Atlantic explainer style), chosen over
# the original bold-caps-influencer template after the user reviewed 3
# concept options side by side -- credible, doesn't depend on a face, and
# distinctive against the wall of near-identical templates in this niche.
# Category tag is topic-specific (e.g. "COGNITIVE BIAS"), never the channel
# name -- YouTube already shows that under the thumbnail in every feed view.
def build_html(image_path, headline, subline, category):
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<link href="https://fonts.googleapis.com/css2?family=Libre+Franklin:wght@300;600;800&display=swap" rel="stylesheet">
<style>
  *{{margin:0;padding:0;box-sizing:border-box}}
  body{{width:{W}px;height:{H}px;overflow:hidden;background:#0a0e0e;font-family:'Libre Franklin',sans-serif}}
  .frame{{position:relative;width:{W}px;height:{H}px;overflow:hidden}}
  .bg{{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;filter:saturate(0.75) contrast(1.05)}}
  .scrim{{position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,14,14,0.15) 0%,rgba(10,14,14,0.35) 55%,rgba(10,14,14,0.96) 100%)}}
  .tag{{position:absolute;left:64px;top:56px;font-size:20px;font-weight:800;letter-spacing:0.18em;color:#E8A857;text-transform:uppercase}}
  .content{{position:absolute;left:64px;right:64px;bottom:64px}}
  .headline{{font-size:64px;font-weight:800;line-height:1.05;color:#fff;letter-spacing:-0.01em;max-width:920px;
    text-shadow:0 4px 20px rgba(0,0,0,0.6)}}
  .sub{{margin-top:18px;font-size:26px;font-weight:300;color:#c9d1cf;max-width:820px}}
</style></head><body>
  <div class="frame">
    <img class="bg" src="{b64(image_path)}">
    <div class="scrim"></div>
    <div class="tag">{html_escape(category)}</div>
    <div class="content">
      <div class="headline">{html_escape(headline)}</div>
      <div class="sub">{html_escape(subline)}</div>
    </div>
  </div>
</body></html>"""
    out = WORK / "thumb.html"
    out.write_text(doc, encoding="utf-8")
    return out


def html_escape(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render(html_path):
    full = WORK / "_full.png"
    subprocess.run(
        [chrome_bin(), "--headless", "--disable-gpu", "--hide-scrollbars",
         "--force-device-scale-factor=1", f"--window-size={W},{H}",
         "--virtual-time-budget=8000", f"--screenshot={full}", html_path.resolve().as_uri()],
        capture_output=True, check=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(full),
         "-vf", f"crop={W}:{H}:0:0", "thumbnail.png"],
        check=True,
    )


def qa_check(client, thumbnail_path):
    """Vision QA pass: catch what the earlier pipeline can't self-detect --
    AI-generation artifacts (malformed hands/limbs, distorted faces, warped
    objects) that only show up by actually looking at the finished image.
    Never lets a flawed thumbnail ship; failure/uncertainty both reject.

    Vision-only: glm-5p2 (the Fireworks fallback) is text-only, so when the
    provider isn't Anthropic this pass is skipped and the FLUX portrait ships
    unchecked -- the same risk everydayhype's proven FLUX pipeline already runs
    with. Skipping loudly beats raising (which would silently drop the
    thumbnail on every video while on Fireworks)."""
    if _llm.provider() != "anthropic":
        print("[thumb] vision QA skipped: LLM provider has no vision; shipping portrait unchecked")
        return {"ok": True}
    img_b64 = base64.b64encode(thumbnail_path.read_bytes()).decode()
    resp = client.messages.create(
        model=MODEL,
        max_tokens=300,
        system=(
            'You are a strict QA reviewer for YouTube thumbnails. Look at the attached image '
            'and check ONLY for AI-image-generation artifacts on the PHOTO portion (ignore the '
            'text overlay, which is real HTML text, not AI-generated): malformed or wrong-count '
            'fingers/hands, extra or missing limbs, distorted/asymmetric face, warped or '
            'nonsensical objects, or other obvious generation glitches. Be strict -- reject '
            'anything a viewer would notice as "off" within a second glance. '
            'Respond with ONLY JSON {"ok": true|false, "issue": "..."} -- issue is empty if ok.'
        ),
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
                {"type": "text", "text": "Check this thumbnail for AI-generation artifacts."},
            ],
        }],
    )
    raw = _llm.only_text(resp)
    return extract_json(raw)


REGEN_ANGLES = [
    "Lead with a direct QUESTION to the viewer.",
    "Lead with a specific NUMBER or statistic from the script.",
    "Lead with the psychological MECHANISM/why, not the effect.",
    "Lead with a provocative STATEMENT that sounds wrong at first.",
    "Lead with a relatable everyday SCENARIO from the script.",
]


def send_telegram_photo(thumbnail_path, caption):
    """Best-effort: post the finished thumbnail to Telegram with a Regenerate
    button, so the user sees it as soon as it's ready instead of only
    discovering it (or not) once the video finally uploads. Silently no-ops
    if the bot/chat/token env vars aren't set (e.g. local manual runs)."""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    thumb_token = os.environ.get("THUMB_TOKEN")
    if not (bot_token and chat_id):
        print("[thumb] TELEGRAM_BOT_TOKEN/CHAT_ID not set, skipping Telegram notify")
        return
    import requests
    reply_markup = {"inline_keyboard": [[
        {"text": "\U0001F504 Regenerate", "callback_data": f"rt:{thumb_token}"},
        {"text": "\U0001F4E4 Upload my own", "callback_data": f"ut:{thumb_token}"},
    ]]} if thumb_token else None
    try:
        with open(thumbnail_path, "rb") as f:
            data = {"chat_id": chat_id, "caption": caption}
            if reply_markup:
                data["reply_markup"] = json.dumps(reply_markup)
            r = requests.post(
                f"https://api.telegram.org/bot{bot_token}/sendPhoto",
                data=data, files={"photo": f}, timeout=30,
            )
        if not r.ok:
            print(f"[thumb] Telegram sendPhoto failed: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[thumb] Telegram sendPhoto failed: {e}")


def main():
    topic = os.environ.get("TOPIC", "")
    client = _llm.client()
    is_regen = os.environ.get("REGEN_THUMBNAIL") == "true"

    if is_regen:
        # Always a fresh Claude call, never thumbnail_spec.json -- the whole
        # point of the Regenerate button is a genuinely different result. No
        # temperature control available (rejected by claude-sonnet-5), so
        # variety is forced by requiring a specific different opening angle
        # each time rather than hoping a repeat call drifts on its own.
        meta = json.loads(Path("youtube.json").read_text(encoding="utf-8")) if Path("youtube.json").exists() else {}
        context = (
            f"VIDEO TITLE (do not repeat/paraphrase this in the headline): {meta.get('title', '')}\n\n"
            f"DESCRIPTION: {meta.get('description', '')}"
        ) if meta else topic
        import random
        hint = f"IMPORTANT for this regeneration: {random.choice(REGEN_ANGLES)}"
        copy = gen_copy(client, topic, context, regen_hint=hint)
        print(f"[thumb] regenerating with hint: {hint}")
    elif Path("thumbnail_spec.json").exists():
        # Preferred path: _gen_video_content.py wrote thumbnail_spec.json in
        # the SAME call that wrote the full script, so the headline is
        # grounded in actual script details (a real stat, the mechanism, a
        # specific line) instead of a compressed title+description summary --
        # also sidesteps the title-echo problem by construction, since that
        # call never sees the eventual video title (generated separately,
        # later, by _gen_youtube_meta.py).
        copy = json.loads(Path("thumbnail_spec.json").read_text(encoding="utf-8"))
        print(f"[thumb] using thumbnail_spec.json from the script-gen call")
    else:
        # Fallback: hand-authored repos (vo.txt pre-committed, e.g.
        # ben-franklin) never run that script, so there's no
        # thumbnail_spec.json -- derive from youtube.json instead.
        meta = json.loads(Path("youtube.json").read_text(encoding="utf-8")) if Path("youtube.json").exists() else {}
        context = (
            f"VIDEO TITLE (do not repeat/paraphrase this in the headline): {meta.get('title', '')}\n\n"
            f"DESCRIPTION: {meta.get('description', '')}"
        ) if meta else topic
        try:
            copy = gen_copy(client, topic, context)
            print(f"[thumb] no thumbnail_spec.json, generated from youtube.json fallback")
        except Exception as e:  # LLM down (billing, outage) must not cost the thumbnail
            print(f"[thumb] copy LLM failed ({e}); using plain title copy", file=sys.stderr)
            copy = plain_copy(topic, meta)
    print(f"[thumb] headline: {copy['headline']!r} category={copy['category']!r}")

    image = gen_portrait(copy["image_prompt"])
    page = build_html(image, copy["headline"], copy["subline"], copy["category"])
    render(page)

    qa = qa_check(client, Path("thumbnail.png"))
    if not qa.get("ok"):
        Path("thumbnail.png").unlink(missing_ok=True)
        print(f"[thumb] QA rejected thumbnail, shipping without one: {qa.get('issue')}")
        return
    print("[thumb] thumbnail.png written, passed QA")

    caption = "New thumbnail:" if is_regen else "Generated thumbnail for this video:"
    send_telegram_photo(Path("thumbnail.png"), caption)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"[thumb] generation failed, continuing without a custom thumbnail: {e}", file=sys.stderr)
        sys.exit(0)
