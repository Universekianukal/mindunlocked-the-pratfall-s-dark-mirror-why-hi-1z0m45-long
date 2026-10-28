"""Upload renders/final.mp4 to YouTube: title, description, tags, category,
a scheduled publish time, and (if the channel is phone-verified) a custom
thumbnail.

Reads metadata from youtube.json in the repo root. Credentials come from
env vars (GitHub Actions secrets): YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET,
YOUTUBE_REFRESH_TOKEN.

Publish schedule (daily cadence, set by the user 2026-07-03): shorts go
public the same day at 6:30 PM IST; long-form goes public the next day at
9:00 AM IST. Uploaded as privacyStatus=private with status.publishAt set --
YouTube itself flips it to public at that exact instant, no cron/Worker-side
scheduler needed. VIDEO_FORMAT ("long"/"short") comes from the workflow env.
"""
import datetime
import json
import os
import sys
import urllib.request

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError

VIDEO_PATH = "renders/final.mp4"
META_PATH = "youtube.json"
THUMBNAIL_CANDIDATES = ["thumbnail.jpg", "thumbnail.png"]
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def compute_publish_at():
    fmt = os.environ.get("VIDEO_FORMAT", "long")
    now_ist = datetime.datetime.now(IST)
    if fmt == "short":
        target = now_ist.replace(hour=18, minute=30, second=0, microsecond=0)
        if target <= now_ist:  # build finished after today's slot -> next day's
            target += datetime.timedelta(days=1)
    else:
        target = (now_ist + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    return target.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def notify_telegram(video_id, title, publish_at_utc, fmt, hashtags="", download_link=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    dt = datetime.datetime.strptime(publish_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    ist_str = dt.astimezone(IST).strftime("%Y-%m-%d %I:%M %p IST")
    lines = [
        "✅ " + fmt + ' uploaded: "' + title + '"',
        "https://youtu.be/" + video_id,
        "Scheduled to go public: " + ist_str,
    ]
    if hashtags:
        lines.append(hashtags)
    if download_link:
        lines.append("⬇️ Download: " + download_link)
    lines.append("")
    lines.append("Reschedule? 👇")
    text = "\n".join(lines)
    # One-tap reschedule buttons -- the callbacks (sch:/scc:) are handled by the
    # orchestrator Worker, which reschedules the (private + publishAt) video via
    # the YouTube API. video_id (11 chars) rides directly in callback_data, well
    # under Telegram's 64-byte cap, so no server-side token is needed.
    keyboard = {"inline_keyboard": [
        [
            {"text": "Today 6:30pm", "callback_data": "sch:" + video_id + ":t630"},
            {"text": "Tonight 9pm", "callback_data": "sch:" + video_id + ":t21"},
        ],
        [
            {"text": "Tomorrow 9am", "callback_data": "sch:" + video_id + ":n9"},
            {"text": "Tomorrow 6:30pm", "callback_data": "sch:" + video_id + ":n630"},
        ],
        [
            {"text": "+1 day", "callback_data": "sch:" + video_id + ":p1"},
            {"text": "Custom…", "callback_data": "scc:" + video_id},
        ],
        # Thumbnail replacement must go through the YouTube API (thm:), NOT the
        # build repo -- the repo self-deletes the moment this upload finishes,
        # so the old commit-to-repo flow 404s for anyone replying even slightly
        # late. Keyed on video_id, so this button still works days from now.
        [
            {"text": "🖼 Use my own thumbnail", "callback_data": "thm:" + video_id},
        ],
    ]}
    data = json.dumps({"chat_id": chat_id, "text": text, "reply_markup": keyboard}).encode()
    req = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage",
        data=data, headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        print(f"Telegram notify failed (non-fatal): {e}", file=sys.stderr)


def upload_to_cloudinary_permanent(video_id):
    """Best-effort permanent direct-download link (overwrite=True, never
    deleted) -- distinct from _fb_ig_upload.py's Cloudinary usage, which
    stages the video temporarily for Instagram's Graph API and deletes it
    right after. Never fails the upload itself if this doesn't work."""
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME")
    api_key = os.environ.get("CLOUDINARY_API_KEY")
    api_secret = os.environ.get("CLOUDINARY_API_SECRET")
    if not (cloud_name and api_key and api_secret):
        return None
    try:
        import cloudinary
        import cloudinary.uploader
        cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret)
        result = cloudinary.uploader.upload(
            VIDEO_PATH, resource_type="video",
            public_id=f"mindunlocked_{video_id}", overwrite=True,
        )
        return result.get("secure_url")
    except Exception as e:
        print(f"Cloudinary upload failed (non-fatal): {e}", file=sys.stderr)
        return None


def extract_hashtags(description):
    """The description's own final line is the 5-7 hashtag line _gen_youtube_meta.py
    writes -- just reuse it verbatim rather than re-deriving from tags."""
    lines = [l.strip() for l in (description or "").strip().splitlines() if l.strip()]
    return lines[-1] if lines and lines[-1].startswith("#") else ""


def get_service():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
        client_id=os.environ["YOUTUBE_CLIENT_ID"],
        client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    return build("youtube", "v3", credentials=creds)


def log_to_sheet(meta, video_id, publish_at_utc, fmt):
    """Best-effort: append one row to the MindUnlocked Video Catalog sheet
    (topic/description tracker spanning past+present, long+short). Reuses the
    same OAuth client as YouTube -- YOUTUBE_REFRESH_TOKEN was re-minted with
    the spreadsheets scope added alongside youtube.upload/readonly. Never
    fails the upload itself if this doesn't work."""
    sheet_id = os.environ.get("GOOGLE_SHEET_ID")
    if not sheet_id:
        return
    try:
        creds = Credentials(
            token=None,
            refresh_token=os.environ["YOUTUBE_REFRESH_TOKEN"],
            client_id=os.environ["YOUTUBE_CLIENT_ID"],
            client_secret=os.environ["YOUTUBE_CLIENT_SECRET"],
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        sheets = build("sheets", "v4", credentials=creds)
        dt = datetime.datetime.strptime(publish_at_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        ist_str = dt.astimezone(IST).strftime("%Y-%m-%d %I:%M %p IST")
        row = [[
            fmt, meta["title"], meta.get("description", "").replace("\n", " ")[:500],
            f"https://youtu.be/{video_id}", "scheduled", ist_str,
        ]]
        sheets.spreadsheets().values().append(
            spreadsheetId=sheet_id, range="Videos!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": row},
        ).execute()
        print("Logged to Google Sheet catalog")
    except Exception as e:
        print(f"Sheet logging failed (non-fatal): {e}", file=sys.stderr)


def notify_worker(video_id, fmt):
    # Best-effort ping so the Worker's Short->Long funnel can pair the two
    # formats and link them. Never fails the upload.
    secret = os.environ.get("CLIP_LEDGER_SECRET")
    repo = os.environ.get("GITHUB_REPOSITORY", "").split("/")[-1]
    if not secret or not repo:
        return
    try:
        import urllib.request
        data = json.dumps({"repo": repo, "video_id": video_id, "format": fmt}).encode()
        req = urllib.request.Request(
            "https://mindunlocked-bot.everydayhypehq.workers.dev/video/uploaded",
            data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Clip-Ledger-Secret": secret,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            },
        )
        urllib.request.urlopen(req, timeout=20).read()
        print("notified worker of upload (funnel)")
    except Exception as e:
        print(f"worker notify failed (non-fatal): {e}", file=sys.stderr)


def notify_worker_crosspost_ready(video_id, fmt, title, description, download_link):
    """Best-effort ping so the Worker can offer Facebook/Instagram
    Approve/Reject buttons for this video -- FB/IG never post automatically
    anymore (see crosspost_decision.yml). Skipped entirely if the permanent
    Cloudinary link failed to upload, since there'd be nothing to post."""
    if not download_link:
        return
    secret = os.environ.get("CLIP_LEDGER_SECRET")
    if not secret:
        return
    try:
        import urllib.request
        first_para = (description or "").split("\n\n")[0].strip()
        caption = f"{title}\n\n{first_para}"
        if len(caption) > 2000:
            caption = caption[:1997] + "..."
        data = json.dumps({
            "video_id": video_id,
            "format": fmt,
            "title": title,
            "caption": caption,
            "download_link": download_link,
        }).encode()
        req = urllib.request.Request(
            "https://mindunlocked-bot.everydayhypehq.workers.dev/video/fb-ig-ready",
            data=data, method="POST",
            headers={
                "Content-Type": "application/json",
                "X-Clip-Ledger-Secret": secret,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
            },
        )
        urllib.request.urlopen(req, timeout=20).read()
        print("notified worker of fb/ig readiness")
    except Exception as e:
        print(f"fb/ig-ready notify failed (non-fatal): {e}", file=sys.stderr)


def main():
    if not os.path.isfile(VIDEO_PATH):
        print(f"{VIDEO_PATH} not found", file=sys.stderr)
        sys.exit(1)

    meta = json.load(open(META_PATH))
    publish_at = compute_publish_at()
    body = {
        "snippet": {
            "title": meta["title"],
            "description": meta["description"],
            "tags": meta.get("tags", []),
            "categoryId": meta.get("categoryId", "27"),  # 27 = Education
        },
        "status": {
            # YouTube requires privacyStatus=private whenever publishAt is set;
            # it flips the video public itself at that instant.
            "privacyStatus": "private",
            "publishAt": publish_at,
            "selfDeclaredMadeForKids": False,
        },
    }
    print(f"Scheduling publish for {publish_at} (format={os.environ.get('VIDEO_FORMAT', 'long')})")

    yt = get_service()
    media = MediaFileUpload(VIDEO_PATH, chunksize=-1, resumable=True, mimetype="video/mp4")
    request = yt.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    while response is None:
        status, response = request.next_chunk()
        if status:
            print(f"Uploaded {int(status.progress() * 100)}%")

    video_id = response["id"]
    print(f"Uploaded: https://youtu.be/{video_id}")

    thumb = next((p for p in THUMBNAIL_CANDIDATES if os.path.isfile(p)), None)
    if thumb:
        try:
            yt.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb)).execute()
            print(f"Thumbnail set from {thumb}")
        except HttpError as e:
            # Custom thumbnails require the channel to be phone-verified
            # (youtube.com/verify) -- fails with 403 otherwise. Don't fail
            # the whole upload over a missing verification.
            print(f"Thumbnail upload failed (channel verified? {thumb}): {e}", file=sys.stderr)

    print(f"video_id={video_id}")
    notify_worker(video_id, os.environ.get("VIDEO_FORMAT", "long"))
    fmt = os.environ.get("VIDEO_FORMAT", "long")
    download_link = upload_to_cloudinary_permanent(video_id)
    hashtags = extract_hashtags(meta.get("description", ""))
    notify_telegram(video_id, meta["title"], publish_at, fmt, hashtags=hashtags, download_link=download_link)
    notify_worker_crosspost_ready(video_id, fmt, meta["title"], meta.get("description", ""), download_link)
    log_to_sheet(meta, video_id, publish_at, fmt)


if __name__ == "__main__":
    main()
