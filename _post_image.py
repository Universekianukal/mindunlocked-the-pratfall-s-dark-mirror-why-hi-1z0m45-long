"""Post ONE image to a Facebook Page or an Instagram account, right now.

Dispatched by post_image.yml, which the Telegram bot dispatches when you tap
"Post" on a photo post -- or, for a "Schedule"d one, when the bot's scheduler
reaches the chosen time. Scheduling lives entirely in the Worker (Instagram's
API has no scheduling at all), so this script only ever posts immediately.

The same file lives in shadow-gasp-pipeline and mindunlocked-template-long;
everything account-specific comes from the workflow's env, never from here.

Image sources (SOURCE):
  day    shadow_gasp batch day's thumbnail still (DAY_DIR/shot1.jpeg)
  photo  a photo you sent the bot -- fetched back from the Worker by PHOTO_TOKEN
  yt     a YouTube video's public thumbnail (VIDEO_ID) -- only exists once public

Env: PLATFORM (fb|ig), SOURCE, CAPTION, FB_PAGE_ID, IG_USER_ID,
FB_PAGE_ACCESS_TOKEN, CLOUDINARY_CLOUD_NAME/API_KEY/API_SECRET, WORKER_URL,
WORKER_SECRET_HEADER, WORKER_SECRET, NOTIFY_CHAT_ID, LABEL, RUN_URL,
plus DAY_DIR / PHOTO_TOKEN / VIDEO_ID for the matching source.
Always reports the outcome to WORKER_URL/post/decided; exits 1 on failure.
"""
import hashlib
import io
import json
import os
import sys
import time

import requests
from PIL import Image, ImageFilter, ImageOps

GRAPH = "https://graph.facebook.com/v19.0"
# Cloudflare refuses python-requests' default User-Agent at the edge (error 1010),
# so the Worker would never even see the call. Same fix as every other notify step.
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
IG_MIN_RATIO, IG_MAX_RATIO = 0.8, 1.91  # Instagram feed images: 4:5 .. 1.91:1


def env(key, default=""):
    return os.environ.get(key, default).strip()


def worker_headers():
    return {env("WORKER_SECRET_HEADER"): env("WORKER_SECRET"), "User-Agent": UA}


def load_image():
    source = env("SOURCE")
    if source == "day":
        day_dir = env("DAY_DIR")
        for name in ("shot1.jpeg", "shot1.jpg", "images/seq/01.jpeg"):
            path = os.path.join(day_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    return f.read()
        raise RuntimeError(f"no thumbnail still found in {day_dir}")
    if source == "photo":
        r = requests.post(env("WORKER_URL").rstrip("/") + "/photo/get",
                          json={"token": env("PHOTO_TOKEN")}, headers=worker_headers(), timeout=60)
        if r.status_code == 404:
            raise RuntimeError("that photo is no longer stored (photos are kept 30 days) -- send it to the bot again")
        r.raise_for_status()
        return r.content
    if source == "yt":
        video_id = env("VIDEO_ID")
        for quality in ("maxresdefault", "sddefault", "hqdefault"):
            r = requests.get(f"https://i.ytimg.com/vi/{video_id}/{quality}.jpg", timeout=30)
            # A missing thumbnail comes back as a ~1 KB grey placeholder, not always a 404.
            if r.ok and len(r.content) > 5000:
                return r.content
        raise RuntimeError("YouTube has no public thumbnail for this video yet -- it is probably still "
                           "private/scheduled. Schedule the photo post for after the video goes public.")
    raise RuntimeError(f"unknown SOURCE {source!r}")


def fit_for_instagram(im):
    """Pad (never crop) into Instagram's allowed aspect range.

    A 9:16 short's still would lose ~30% of its height to a centre crop -- the
    kind of crop that cuts a person's head off. Instead the whole image is kept
    and centred on a blurred, enlarged copy of itself.
    """
    w, h = im.size
    ratio = w / h
    if IG_MIN_RATIO <= ratio <= IG_MAX_RATIO:
        return im
    if ratio < IG_MIN_RATIO:
        cw, ch = round(h * IG_MIN_RATIO), h
    else:
        cw, ch = w, round(w / IG_MAX_RATIO)
    scale = max(cw / w, ch / h)
    bg = im.resize((round(w * scale) + 1, round(h * scale) + 1), Image.LANCZOS)
    left, top = (bg.width - cw) // 2, (bg.height - ch) // 2
    bg = bg.crop((left, top, left + cw, top + ch)).filter(ImageFilter.GaussianBlur(40))
    bg.paste(im, ((cw - w) // 2, (ch - h) // 2))
    return bg


def prepare(raw, platform):
    im = ImageOps.exif_transpose(Image.open(io.BytesIO(raw))).convert("RGB")
    if platform == "ig":
        im = fit_for_instagram(im)
        if im.width > 1440:
            im = im.resize((1440, round(im.height * 1440 / im.width)), Image.LANCZOS)
        if im.width < 320:
            raise RuntimeError(f"image is {im.width}px wide -- Instagram needs at least 320px")
    elif max(im.size) > 2048:
        im.thumbnail((2048, 2048), Image.LANCZOS)
    out = io.BytesIO()
    im.save(out, "JPEG", quality=92)
    return out.getvalue()


def cloudinary_upload(jpeg):
    """Signed upload to a FRESH public_id every time.

    Instagram's fetcher caches a refusal against the URL, so reusing a public_id
    after one failure makes every retry fail the same way.
    """
    cloud, key, secret = env("CLOUDINARY_CLOUD_NAME"), env("CLOUDINARY_API_KEY"), env("CLOUDINARY_API_SECRET")
    ts = str(int(time.time()))
    public_id = f"social_posts/{ts}_{os.urandom(4).hex()}"
    signature = hashlib.sha1(f"public_id={public_id}&timestamp={ts}{secret}".encode()).hexdigest()
    r = requests.post(f"https://api.cloudinary.com/v1_1/{cloud}/image/upload",
                      data={"api_key": key, "timestamp": ts, "public_id": public_id, "signature": signature},
                      files={"file": ("post.jpg", jpeg, "image/jpeg")}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Cloudinary upload failed: {r.status_code} {r.text[:300]}")
    return r.json()["secure_url"]


def post_facebook(image_url, caption, token):
    r = requests.post(f"{GRAPH}/{env('FB_PAGE_ID')}/photos",
                      data={"url": image_url, "message": caption, "access_token": token}, timeout=120)
    if not r.ok:
        raise RuntimeError(f"Facebook refused the photo: {r.status_code} {r.text[:300]}")
    body = r.json()
    return body.get("post_id") or body["id"]


def post_instagram(image_url, caption, token):
    ig = env("IG_USER_ID")
    r = requests.post(f"{GRAPH}/{ig}/media",
                      data={"image_url": image_url, "caption": caption, "access_token": token}, timeout=60)
    if not r.ok:
        raise RuntimeError(f"Instagram refused the image: {r.status_code} {r.text[:300]}")
    container = r.json()["id"]
    for _ in range(36):  # images are usually FINISHED at once; allow 3 min
        status = requests.get(f"{GRAPH}/{container}", params={"fields": "status_code", "access_token": token},
                              timeout=30).json().get("status_code")
        if status == "FINISHED":
            break
        if status == "ERROR":
            raise RuntimeError(f"Instagram could not process the image (container {container})")
        time.sleep(5)
    else:
        raise RuntimeError(f"Instagram container {container} never finished processing")
    p = requests.post(f"{GRAPH}/{ig}/media_publish", data={"creation_id": container, "access_token": token},
                      timeout=60)
    if not p.ok:
        raise RuntimeError(f"Instagram publish failed: {p.status_code} {p.text[:300]}")
    return p.json()["id"]


def day_caption():
    """shadow_gasp day posts reuse the video's own caption builder (title +
    first paragraph + hashtags), so the photo reads the same as the reel."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from _fb_ig_upload import build_caption  # noqa: E402 -- only exists in shadow-gasp-pipeline
    with open(os.path.join(env("DAY_DIR"), "youtube.json"), encoding="utf-8") as f:
        return build_caption(json.load(f))


def report(result):
    body = {**result, "platform": env("PLATFORM"), "label": env("LABEL") or "Photo post",
            "chat_id": env("NOTIFY_CHAT_ID"), "run_url": env("RUN_URL")}
    try:
        r = requests.post(env("WORKER_URL").rstrip("/") + "/post/decided", json=body,
                          headers=worker_headers(), timeout=30)
        print(f"reported to the bot: HTTP {r.status_code}")
    except Exception as e:  # the post itself already happened (or not); never mask that
        print(f"could not report to the bot: {e}", file=sys.stderr)


def main():
    platform = env("PLATFORM").lower()
    result = {"ok": False}
    marker = None
    try:
        if platform not in ("fb", "ig"):
            raise RuntimeError(f"PLATFORM must be fb or ig, got {platform!r}")
        if env("SOURCE") == "day":
            marker = os.path.join(env("DAY_DIR"), "FB_IMG_POSTED" if platform == "fb" else "IG_IMG_POSTED")
            if os.path.exists(marker):
                raise RuntimeError("this day's photo was already posted there -- not posting a duplicate")
        caption = env("CAPTION")
        if not caption and env("SOURCE") == "day":
            caption = day_caption()
        caption = caption[:2200]  # Instagram's hard cap
        image_url = cloudinary_upload(prepare(load_image(), platform))
        token = env("FB_PAGE_ACCESS_TOKEN")
        ref = post_facebook(image_url, caption, token) if platform == "fb" else post_instagram(image_url, caption, token)
        result = {"ok": True, "ref_id": ref}
        print(f"posted: {platform} ref_id={ref}")
        if marker:
            open(marker, "w").close()
    except Exception as e:
        result = {"ok": False, "error": str(e)[:500]}
        print(f"FAILED: {e}", file=sys.stderr)
    report(result)
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
