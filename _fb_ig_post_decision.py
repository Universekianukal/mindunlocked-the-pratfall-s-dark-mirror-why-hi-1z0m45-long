"""Posts (or skips) a single video to Facebook or Instagram on-demand, in
response to a Telegram Approve/Reject button tap -- mirrors shadow_gasp's
crosspost_decision.yml pattern (see shadow-gasp-fb-ig-approve-reject).

MindUnlocked's per-video build repos self-delete right after the YouTube
upload, so this workflow (and this script) live permanently in
mindunlocked-template-long instead, dispatched by the Worker whenever a
button is tapped -- regardless of which repo/format originally built the
video. VIDEO_URL is the permanent Cloudinary link _youtube_upload.py already
uploads (see upload_to_cloudinary_permanent), so no local video file or
GitHub Actions artifact is needed here at all.

Unlike the old auto-posting _fb_ig_upload.py, this posts immediately by
default (published=true / no scheduling) -- the whole point of gating on a
manual tap is that "immediately" is exactly when the user wants it to go out.
The Reels buttons added 2026-09-18 (ported from shadow_gasp) are opt-in on
top of that, via env vars that are blank for every old path:
  FB_AS_REEL=true    "🎞 FB: Reel" -- publish as a Facebook Reel
  IG_REELS_ONLY=true "🎞 IG: Reels only" -- Reels tab, not the profile grid
  SCHEDULE_AT=<unix> "⏰ FB: Schedule Reel" -- Facebook's own scheduler (fb only)

Reports its outcome back to the Worker's /video/crosspost-decided endpoint
so the Telegram thread gets a confirmation and KV state is updated.
"""
import json
import os
import sys
import time
import urllib.request

import requests

FB_PAGE_ID = "1166881726518997"
IG_USER_ID = "17841440164198548"
GRAPH = "https://graph.facebook.com/v19.0"
WORKER_URL = "https://mindunlocked-bot.everydayhypehq.workers.dev/video/crosspost-decided"

POLL_INTERVAL_S = 10
POLL_TIMEOUT_S = 600  # IG container processing can take a few minutes for longer videos


def post_to_facebook(token, caption, video_url, schedule_at=None):
    data = {
        "description": caption,
        "access_token": token,
        "published": "true",
        "file_url": video_url,
    }
    if schedule_at:
        # Facebook's own scheduler: the video uploads now, sits in Meta Business
        # Suite's Planner (movable/editable there) and Facebook publishes it at
        # this unix time. Meta requires it to be at least 10 minutes ahead.
        data.update({"published": "false", "scheduled_publish_time": str(schedule_at)})
    resp = requests.post(
        f"{GRAPH}/{FB_PAGE_ID}/videos",
        data=data,
        timeout=600,
    )
    if not resp.ok:
        print(f"Facebook refused: {resp.status_code} {resp.text[:500]}", file=sys.stderr)
    resp.raise_for_status()
    post_id = resp.json()["id"]
    if schedule_at:
        print(f"Facebook scheduled for unix {schedule_at}: https://facebook.com/{post_id}")
    else:
        print(f"Facebook posted: https://facebook.com/{post_id}")
    return post_id


def _fb_video_status(token, video_id):
    r = requests.get(f"{GRAPH}/{video_id}", params={"fields": "status", "access_token": token}, timeout=30)
    r.raise_for_status()
    return r.json().get("status") or {}


def post_fb_reel(token, caption, video_url, schedule_at=None):
    """"🎞 FB: Reel" button: publish as a Facebook REEL (Page Reels section, recommended to
    non-followers too) via the Reels Publishing API -- start -> hosted-file upload -> finish.
    Reels must be 3-90 s, 9:16; max 30 API reels per Page per 24 h. Ported from shadow_gasp's
    _fb_ig_upload.py (2026-09-18)."""
    start = requests.post(f"{GRAPH}/{FB_PAGE_ID}/video_reels",
                          data={"upload_phase": "start", "access_token": token}, timeout=60)
    if not start.ok:
        print(f"Facebook refused the reel start: {start.status_code} {start.text[:500]}", file=sys.stderr)
    start.raise_for_status()
    video_id = start.json()["video_id"]

    up = requests.post(f"https://rupload.facebook.com/video-upload/{GRAPH.rsplit('/', 1)[-1]}/{video_id}",
                       headers={"Authorization": f"OAuth {token}", "file_url": video_url}, timeout=600)
    if not up.ok:
        print(f"Facebook refused the reel upload: {up.status_code} {up.text[:500]}", file=sys.stderr)
    up.raise_for_status()

    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        st = _fb_video_status(token, video_id)
        phase = (st.get("uploading_phase") or {}).get("status")
        if st.get("video_status") == "error" or phase == "error":
            raise RuntimeError(f"Facebook reel {video_id} upload failed: {st}")
        if phase == "complete" or st.get("video_status") in ("ready", "upload_complete"):
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        raise TimeoutError(f"Facebook reel {video_id} upload did not finish within {POLL_TIMEOUT_S}s")

    fin_data = {"upload_phase": "finish", "video_id": video_id, "video_state": "PUBLISHED",
                "description": caption, "access_token": token}
    if schedule_at:
        # "⏰ FB: Schedule Reel": Facebook holds it in the Planner (10 min - 29 days ahead).
        fin_data.update({"video_state": "SCHEDULED", "scheduled_publish_time": str(schedule_at)})
    fin = requests.post(f"{GRAPH}/{FB_PAGE_ID}/video_reels", data=fin_data, timeout=120)
    if not fin.ok:
        print(f"Facebook refused to publish the reel: {fin.status_code} {fin.text[:500]}", file=sys.stderr)
    fin.raise_for_status()
    if not fin.json().get("success"):
        raise RuntimeError(f"Facebook reel {video_id} finish returned {fin.text[:300]}")

    # PAST THE POINT OF NO RETURN: `finish` succeeded, so the reel is committed
    # to the Page. Nothing below may raise -- on 2026-09-18 the very first real
    # Reel published fine and then the run exited 1 from somewhere in here, so
    # the post was live while Telegram was told it had failed (the same trap as
    # shadow-gasp-publish-action-reported-failure-while-publishing). Everything
    # from here is best-effort: warn, never fail.
    try:
        deadline = time.time() + (0 if schedule_at else POLL_TIMEOUT_S)  # scheduled: nothing to wait for
        while time.time() < deadline:
            st = _fb_video_status(token, video_id)
            pub = (st.get("publishing_phase") or {}).get("status")
            if st.get("video_status") == "error" or pub == "error" or (st.get("processing_phase") or {}).get("status") == "error":
                # Report it, but don't fail: the reel may still appear. The Page is the truth.
                print(f"WARNING: reel {video_id} reported an error phase: {st} -- check the Page",
                      file=sys.stderr)
                break
            if pub == "complete":
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            if not schedule_at:
                print(f"WARNING: reel {video_id} not reported published within {POLL_TIMEOUT_S}s -- check the Page",
                      file=sys.stderr)
    except Exception as e:  # noqa: BLE001 - a published reel must never report failure
        print(f"WARNING: couldn't poll reel {video_id} after publishing ({type(e).__name__}: {e}) -- "
              f"it is most likely live; check the Page", file=sys.stderr)

    ref = video_id
    try:  # the post id is what FB comments carry; fall back to the video id
        r = requests.get(f"{GRAPH}/{video_id}", params={"fields": "post_id", "access_token": token}, timeout=30)
        pid = r.json().get("post_id") if r.ok else None
        if pid:
            ref = str(pid) if "_" in str(pid) else f"{FB_PAGE_ID}_{pid}"
    except Exception as e:  # noqa: BLE001 - incl. ValueError from a non-JSON body, which used to escape
        print(f"WARNING: post_id lookup failed ({type(e).__name__}: {e}); using the video id", file=sys.stderr)
    if schedule_at:
        print(f"Facebook reel scheduled for unix {schedule_at}: video_id={video_id} https://facebook.com/{ref}")
    else:
        print(f"Facebook reel posted: video_id={video_id} https://facebook.com/{ref}")
    return ref


def post_to_instagram(token, caption, video_url, reels_only=False):
    data = {
        "media_type": "REELS",
        "video_url": video_url,
        "caption": caption,
        "access_token": token,
    }
    if reels_only:
        # "🎞 IG: Reels only" button: the Reel appears ONLY in the Reels tab -- not the
        # profile grid / followers' feed. Can only be set at creation (the API can't move it later).
        data["share_to_feed"] = "false"
    resp = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media",
        data=data,
        timeout=60,
    )
    resp.raise_for_status()
    container_id = resp.json()["id"]

    deadline = time.time() + POLL_TIMEOUT_S
    while time.time() < deadline:
        status_resp = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code", "access_token": token},
            timeout=30,
        )
        status_resp.raise_for_status()
        status_code = status_resp.json().get("status_code")
        if status_code == "FINISHED":
            break
        if status_code == "ERROR":
            raise RuntimeError(f"Instagram container {container_id} failed processing")
        time.sleep(POLL_INTERVAL_S)
    else:
        raise TimeoutError(f"Instagram container {container_id} did not finish within {POLL_TIMEOUT_S}s")

    publish_resp = requests.post(
        f"{GRAPH}/{IG_USER_ID}/media_publish",
        data={"creation_id": container_id, "access_token": token},
        timeout=60,
    )
    publish_resp.raise_for_status()
    media_id = publish_resp.json()["id"]
    print(f"Instagram posted: media_id={media_id}")
    return media_id


def notify_worker(result):
    secret = os.environ.get("CLIP_LEDGER_SECRET")
    if not secret:
        print("CLIP_LEDGER_SECRET missing, can't notify worker", file=sys.stderr)
        return
    data = json.dumps(result).encode()
    req = urllib.request.Request(
        WORKER_URL,
        data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Clip-Ledger-Secret": secret,
            # Cloudflare refuses urllib's default User-Agent at the edge with
            # error 1010 (a 403 that never reaches the Worker), so EVERY post
            # here went out unconfirmed from 2026-08-18 until this was found on
            # 2026-09-18. _youtube_upload.py and _post_image.py already do this.
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"worker notify failed: {e}", file=sys.stderr)


def main():
    video_id = os.environ["MU_VIDEO_ID"]
    platform = os.environ["PLATFORM"]  # "fb" or "ig"
    decision = os.environ["DECISION"]  # "approve" or "reject"
    video_url = os.environ.get("VIDEO_URL", "")
    caption = os.environ.get("CAPTION", "")
    notify_chat_id = os.environ.get("NOTIFY_CHAT_ID", "")

    result = {
        "video_id": video_id,
        "platform": platform,
        "decision": decision,
        "notify_chat_id": notify_chat_id,
    }

    if decision == "reject":
        result["ok"] = True
        print(f"{platform} rejected for {video_id} -- not posting")
        notify_worker(result)
        return

    if not video_url:
        result["ok"] = False
        result["error"] = "no video_url supplied (missing CLOUDINARY_LINK for this video)"
        notify_worker(result)
        sys.exit(1)

    token = os.environ["FB_PAGE_ACCESS_TOKEN"]

    # All three are blank on every pre-2026-09-18 path, so an ordinary Approve
    # behaves exactly as before.
    schedule_at = os.environ.get("SCHEDULE_AT", "").strip()
    as_reel = os.environ.get("FB_AS_REEL", "").strip().lower() == "true"
    reels_only = os.environ.get("IG_REELS_ONLY", "").strip().lower() == "true"

    try:
        if schedule_at and platform != "fb":
            raise ValueError("SCHEDULE_AT is Facebook-only (Instagram's API cannot schedule)")
        if as_reel and platform != "fb":
            raise ValueError("FB_AS_REEL is Facebook-only")
        if reels_only and platform != "ig":
            raise ValueError("IG_REELS_ONLY is Instagram-only")
        if platform == "fb" and as_reel:
            post_id = post_fb_reel(token, caption, video_url, schedule_at=schedule_at or None)
        elif platform == "fb":
            post_id = post_to_facebook(token, caption, video_url, schedule_at=schedule_at or None)
        elif platform == "ig":
            post_id = post_to_instagram(token, caption, video_url, reels_only=reels_only)
        else:
            raise ValueError(f"unknown platform {platform}")
        result["ok"] = True
        result["post_id"] = post_id
        if schedule_at:
            # The bot says "📅 SCHEDULED on Facebook for ..." instead of "Posted".
            result["scheduled_at"] = schedule_at
    except Exception as e:
        result["ok"] = False
        result["error"] = f"{type(e).__name__}: {e}"
        # Also to the Actions log: when the notify itself fails, this traceback
        # is the ONLY record of why a post didn't happen. The 2026-09-18 Facebook
        # Reel run failed with no explanation anywhere because of this.
        import traceback
        traceback.print_exc()

    notify_worker(result)
    if not result["ok"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
