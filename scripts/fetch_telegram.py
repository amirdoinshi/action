"""
fetch_telegram.py — run by telegram_fetcher.yml on GitHub Actions.

Reads INPUT_CHANNELS, INPUT_RUN_ID, INPUT_HISTORY from environment,
scrapes each Telegram channel, downloads media, and writes results to
downloads/run_<RUN_ID>/ for the bot to download.
"""

import copy
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

# ============================================================================
# Configuration
# ============================================================================

TELEGRAM_BASE_URL = os.environ.get("TELEGRAM_BASE_URL", "https://telegram.me").rstrip("/")

CHANNELS         = [u.strip() for u in os.environ.get("INPUT_CHANNELS", "").split(",") if u.strip()]
RUN_ID           = os.environ.get("INPUT_RUN_ID", "unknown")
HISTORY_JSON     = os.environ.get("INPUT_HISTORY", "{}")
OUTPUT_DIR       = Path(f"downloads/run_{RUN_ID}")
AVATAR_CHANNELS  = set(u.strip().rstrip("/") for u in os.environ.get("INPUT_AVATAR_CHANNELS", "").split(",") if u.strip())

# Tehran timezone — UTC+3:30, no DST (Iran stopped DST permanently in 2022).
# Must be timedelta, NOT timezone(), so we can add it to a datetime.
# Rewrite t.me URLs to configured base domain (t.me has been unreliable)
CHANNELS = [
    url.replace("https://t.me", TELEGRAM_BASE_URL) if url.startswith("https://t.me") else url
    for url in CHANNELS
]

TEHRAN_OFFSET = timedelta(hours=3, minutes=30)

MAX_WORKERS         = 2
REQUEST_DELAY       = 1.0        # seconds between sequential page fetches per worker
MAX_FILE_SIZE_MB    = int(os.environ.get("INPUT_MAX_MEDIA_SIZE_MB", "50"))
MAX_FILE_SIZE_BYTES = MAX_FILE_SIZE_MB * 1024 * 1024
MAX_PHOTOS_PER_MSG  = 5

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/134.0.0.0 Safari/537.36"
    )
}

# ============================================================================
# Setup output directories
# ============================================================================

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MEDIA_DIR = OUTPUT_DIR / "media"
MEDIA_DIR.mkdir(exist_ok=True)

try:
    raw_history: dict = json.loads(HISTORY_JSON) if HISTORY_JSON else {}
except json.JSONDecodeError:
    print("WARNING: Could not parse INPUT_HISTORY — starting with empty history.")
    raw_history = {}

# Convert lists → sets for O(1) membership checks
sent_history: dict[str, set[str]] = {
    url: set(ids) for url, ids in raw_history.items()
}

# ============================================================================
# Regex (compiled once at module load — same patterns as tele2matrix app.py)
# ============================================================================

RE_MSG_ID       = re.compile(r'/(\d+)$')
RE_BG_IMAGE_URL = re.compile(r"background-image:url\('([^']+)'\)")
RE_TIME         = re.compile(r'(\d{1,2}:\d{2})')
RE_DATA_POST_ID = re.compile(r'(\d+)$')

# ============================================================================
# HTML → Markdown  (exact mirror of app.py _process_message_element)
# ============================================================================

INLINE_FORMAT: dict[str, tuple[str, str]] = {
    'b':      ('**', '**'),
    'strong': ('**', '**'),
    'i':      ('*',  '*'),
    'em':     ('*',  '*'),
    'code':   ('`',  '`'),
}


def _process_element(node) -> str:
    """
    Recursively convert one BeautifulSoup node to Markdown.
    Mirrors app.py _process_message_element exactly so output is identical.
    """
    # Plain text node — return as-is (preserves spaces and line structure)
    if isinstance(node, NavigableString):
        return str(node)

    if not isinstance(node, Tag):
        return ''

    if node.name == 'a':
        return f'[{node.get_text(strip=True)}]({node.get("href", "")})'

    if node.name == 'br':
        return '  \n'

    if node.name == 'pre':
        return f'```\n{node.get_text(separator=chr(10), strip=True)}\n```'

    # Telegram emoji sprite: <i class="emoji"><b>🛢</b></i>
    if node.name == 'i' and 'emoji' in (node.get('class') or []):
        return node.get_text(strip=True)

    if node.name in INLINE_FORMAT:
        open_md, close_md = INLINE_FORMAT[node.name]
        return f'{open_md}{node.get_text(strip=True)}{close_md}'

    if node.name == 'blockquote':
        inner = ''.join(_process_element(c) for c in node.children)
        lines  = inner.split('  \n')
        quoted = '\n'.join(f'> {line}' if line.strip() else '>' for line in lines)
        return '\n' + quoted + '\n'

    # Any other tag — recurse into children so content is not lost
    return ''.join(_process_element(c) for c in node.children)


def _element_to_markdown(element: Tag) -> str:
    """Iterate the direct children of a parsed element and join their Markdown."""
    return ''.join(_process_element(child) for child in element.children)


# ============================================================================
# Message ID extraction  (same as app.py)
# ============================================================================

def extract_message_id(wrap: Tag) -> str | None:
    try:
        link = wrap.find('a', class_='tgme_widget_message_date')
        if link and link.get('href'):
            m = RE_MSG_ID.search(link['href'])
            if m:
                return m.group(1)
        data_post = wrap.get('data-post', '')
        if data_post:
            m = RE_DATA_POST_ID.search(data_post)
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


# ============================================================================
# Timestamp extraction  (same as app.py, TEHRAN_OFFSET fix applied)
# ============================================================================

def extract_timestamp(bubble: Tag) -> str | None:
    try:
        time_el = bubble.find('time', class_='time')
        if time_el and time_el.get('datetime'):
            dt = datetime.fromisoformat(time_el['datetime'].replace('Z', '+00:00'))
            tehran_dt = dt.astimezone(timezone.utc) + TEHRAN_OFFSET
            return tehran_dt.strftime('%H:%M')
        footer = bubble.find('div', class_='tgme_widget_message_footer')
        if footer:
            m = RE_TIME.search(footer.get_text())
            if m:
                return m.group(1)
    except Exception:
        pass
    return None


# ============================================================================
# Content extraction  (exact mirror of app.py scrape_telegram_channel)
# ============================================================================

def extract_content(bubble: Tag) -> str:
    """
    Deep-copies the bubble, strips UI chrome, then converts to Markdown
    using the same logic as app.py so output is byte-for-byte identical.
    """
    bubble = copy.deepcopy(bubble)

    for cls in ('tgme_widget_message_reactions', 'tgme_widget_message_footer'):
        el = bubble.find('div', class_=cls)
        if el:
            el.decompose()

    reply_content = ''
    reply_el = bubble.find('a', class_='tgme_widget_message_reply')
    if reply_el:
        reply_text = reply_el.get_text(separator='\n', strip=True)
        reply_content = '> ' + reply_text.replace('\n', '\n> ') + '\n\n'
        reply_el.decompose()

    grouped = bubble.find('div', class_='tgme_widget_message_grouped_wrap')
    if grouped:
        text_el = None
        for sibling in grouped.find_next_siblings('div'):
            if 'tgme_widget_message_text' in sibling.get('class', []):
                text_el = sibling
                break
    else:
        text_el = bubble.find('div', class_='tgme_widget_message_text')

    if text_el:
        inner = text_el.find('div', class_='tgme_widget_message_text')
        if inner:
            text_el = inner
        message_content = _element_to_markdown(text_el)
    else:
        message_content = '' if grouped else bubble.get_text(separator='\n', strip=True)

    return reply_content + message_content


# ============================================================================
# Media download
# ============================================================================

def download_media(url: str, filepath: Path, label: str) -> tuple[bool, int]:
    try:
        head = requests.head(url, timeout=30, headers=HEADERS, allow_redirects=True)
        content_length = int(head.headers.get('content-length', 0))
        if content_length > MAX_FILE_SIZE_BYTES:
            print(f"      SKIP {label}: {content_length / 1024 / 1024:.1f} MB > {MAX_FILE_SIZE_MB} MB limit")
            return False, 0

        resp = requests.get(url, timeout=120, stream=True, headers=HEADERS)
        resp.raise_for_status()

        written = 0
        with open(filepath, 'wb') as f:
            for chunk in resp.iter_content(chunk_size=65536):
                f.write(chunk)
                written += len(chunk)
                if written > MAX_FILE_SIZE_BYTES:
                    print(f"      SKIP {label}: exceeded size limit during stream")
                    filepath.unlink(missing_ok=True)
                    return False, 0

        return True, written
    except Exception as exc:
        print(f"      FAIL {label}: {exc}")
        filepath.unlink(missing_ok=True)
        return False, 0


# ============================================================================
# Fetch single channel
# ============================================================================

def fetch_channel(channel_url: str) -> list[dict]:
    channel_url  = channel_url.strip()
    if not channel_url:
        return []

    channel_name = channel_url.rstrip('/').split('/')[-1]
    print(f"\nFetching: {channel_name}")

    sent_ids: set[str] = sent_history.get(channel_url, set())

    try:
        resp = requests.get(channel_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        print(f"  FAIL HTTP: {exc}")
        return []

    soup = BeautifulSoup(resp.text, 'lxml')

    # Extract channel metadata + download avatar image
    channel_display_name = channel_name
    el = soup.find("div", class_="tgme_channel_info_header_title")
    if el:
        channel_display_name = el.get_text(strip=True) or channel_name

    channel_username = ""
    el = soup.find("div", class_="tgme_channel_info_header_username")
    if el:
        channel_username = el.get_text(strip=True)

    channel_description = ""
    el = soup.find("div", class_="tgme_channel_info_description")
    if el:
        channel_description = el.get_text(strip=True)

    channel_subscribers = ""
    counters = soup.find("div", class_="tgme_channel_info_counters")
    if counters:
        channel_subscribers = counters.get_text(strip=True)

    # Download avatar image (Telegram CDN is blocked in Iran, so we need the bytes in the artifact)
    channel_avatar_file = ""
    if AVATAR_CHANNELS and channel_url.rstrip("/") not in AVATAR_CHANNELS:
        print(f"  Avatar: skipped (not in avatar_channels list)")
    else:
        m = re.search(r'<meta[^>]*og:image[^>]*content="([^"]*)"', resp.text)
        if m:
            avatar_url = m.group(1)
            try:
                av_resp = requests.get(avatar_url, headers=HEADERS, timeout=15)
                if av_resp.status_code == 200:
                    ext = avatar_url.rstrip("/").rsplit(".", 1)[-1] if "." in avatar_url else "jpg"
                    channel_avatar_file = f"{channel_name}_avatar.{ext}"
                    av_path = OUTPUT_DIR / channel_avatar_file
                    with open(av_path, "wb") as f:
                        f.write(av_resp.content)
                    print(f"  Avatar: {len(av_resp.content)} bytes -> {channel_avatar_file}")
            except Exception as exc:
                print(f"  Avatar download failed: {exc}")

    # Save channel info file alongside messages
    channel_info = {
        "url": channel_url,
        "name": channel_display_name,
        "username": channel_username,
        "description": channel_description,
        "subscribers": channel_subscribers,
        "avatar_file": channel_avatar_file,
    }
    with open(OUTPUT_DIR / f"{channel_name}_info.json", "w", encoding="utf-8") as f:
        json.dump(channel_info, f, indent=2, ensure_ascii=False)
    section = soup.find('section', class_='tgme_channel_history')
    if not section:
        print("  WARN: no channel history section found")
        return []

    wraps = section.find_all('div', class_='tgme_widget_message_wrap')
    print(f"  Page contains {len(wraps)} messages")

    channel_messages: list[dict] = []
    new_count = 0

    # Iterate newest-to-oldest so we can stop at the first already-seen ID.
    # Reversed list is then reversed again before return → oldest-first for sending.
    for wrap in reversed(wraps):
        bubble = wrap.find('div', class_='tgme_widget_message_bubble')
        if not bubble:
            continue

        msg_id = extract_message_id(wrap)
        if not msg_id:
            continue

        if msg_id in sent_ids:
            print(f"  STOP at known message {msg_id} ({new_count} new found)")
            break

        timestamp       = extract_timestamp(bubble)
        message_content = extract_content(bubble)

        # --- Photos ---
        photo_wraps = bubble.find_all('a', class_='tgme_widget_message_photo_wrap')
        photo_urls: list[str] = []
        telegram_post_link: str | None = None
        for pw in photo_wraps:
            m = RE_BG_IMAGE_URL.search(pw.get('style', ''))
            if m:
                photo_urls.append(m.group(1))
            if telegram_post_link is None:
                telegram_post_link = pw.get('href')

        # --- Video ---
        video_url: str | None = None
        video_thumb_url: str | None = None
        video_el = bubble.find('video', class_='tgme_widget_message_video') or bubble.find('video')
        if video_el and video_el.get('src'):
            video_url = video_el['src']
            vp = bubble.find('a', class_='tgme_widget_message_video_player')
            if vp:
                telegram_post_link = vp.get('href')
            thumb_el = bubble.find('i', class_='tgme_widget_message_video_thumb')
            if thumb_el:
                m = RE_BG_IMAGE_URL.search(thumb_el.get('style', ''))
                if m:
                    video_thumb_url = m.group(1)

        # --- Audio / Voice ---
        audio_url: str | None = None
        if not video_url:
            audio_el = bubble.find("audio")
            if audio_el and audio_el.get("src"):
                audio_url = audio_el["src"]
            # Also check for document-type audio (no direct download URL)
            if not audio_url:
                doc = bubble.find("a", class_="tgme_widget_message_document_wrap")
                if doc and doc.find("div", class_=lambda c: c and "audio" in c.split()):
                    telegram_post_link = doc.get("href") or telegram_post_link

        # --- Document (PDF, APK, etc.) ---
        document_url: str | None = None
        document_filename: str | None = None
        document_description: str | None = None
        doc_wrap = bubble.find("a", class_="tgme_widget_message_document_wrap")
        if doc_wrap and not audio_url:
            href = doc_wrap.get("href")
            if href:
                document_url = href
                telegram_post_link = href
            title_el = doc_wrap.find("div", class_="tgme_widget_message_document_title")
            if title_el:
                document_filename = title_el.get_text(strip=True)
            desc_el = doc_wrap.find("div", class_="tgme_widget_message_document_description")
            if desc_el:
                document_description = desc_el.get_text(strip=True)

        # --- Download media ---
        downloaded_media: list[dict] = []

        for i, url in enumerate(photo_urls[:MAX_PHOTOS_PER_MSG]):
            fname    = f"{channel_name}_{msg_id}_photo_{i}.jpg"
            filepath = MEDIA_DIR / fname
            ok, size = download_media(url, filepath, f"photo {i+1}")
            if ok:
                downloaded_media.append({'type': 'photo', 'filename': fname, 'size': size})
                print(f"    Photo {i+1}: {size // 1024} KB")

        if video_url:
            fname    = f"{channel_name}_{msg_id}_video.mp4"
            filepath = MEDIA_DIR / fname
            ok, size = download_media(video_url, filepath, "video")
            if ok:
                downloaded_media.append({'type': 'video', 'filename': fname, 'size': size})
                print(f"    Video: {size / 1024 / 1024:.1f} MB")
            elif video_thumb_url:
                # Workflow downloads thumbnail as fallback so the bot can still
                # send something visual even when the video is too large.
                fname    = f"{channel_name}_{msg_id}_thumb.jpg"
                filepath = MEDIA_DIR / fname
                ok, size = download_media(video_thumb_url, filepath, "video_thumb")
                if ok:
                    downloaded_media.append({'type': 'video_thumb', 'filename': fname, 'size': size})
                    print(f"    Video thumbnail (fallback): {size // 1024} KB")

        if audio_url:
            fname = f"{channel_name}_{msg_id}_audio.ogg"
            filepath = MEDIA_DIR / fname
            ok, size = download_media(audio_url, filepath, "audio")
            if ok:
                downloaded_media.append({'type': 'audio', 'filename': fname, 'size': size})
                print(f"    Audio: {size // 1024} KB")

        msg_data = {
            'message_id':         msg_id,
            'channel':            channel_name,
            'channel_url':        channel_url,
            'content':            message_content,
            'timestamp':          timestamp,
            'photo_url':          photo_urls[0] if photo_urls else None,
            'photo_count':        len(photo_urls),
            'video_url':          video_url,
            'video_thumb_url':    video_thumb_url,
            'audio_url':          audio_url,
            'document_url':       document_url,
            'document_filename':  document_filename,
            'document_description': document_description,
            'telegram_post_link': telegram_post_link,
            'downloaded_media':   downloaded_media,
            'fetched_at':         datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        }

        msg_file = OUTPUT_DIR / f"{channel_name}_{msg_id}.json"
        with open(msg_file, 'w', encoding='utf-8') as f:
            json.dump(msg_data, f, indent=2, ensure_ascii=False)

        channel_messages.append(msg_data)
        new_count += 1
        preview = message_content[:60].replace('\n', ' ')
        print(f"  OK {msg_id}: {len(message_content)} chars, {len(downloaded_media)} media — {preview!r}")

        time.sleep(REQUEST_DELAY)

    # Reverse so the bot receives messages in chronological (oldest-first) order
    channel_messages.reverse()
    print(f"  -> {new_count} new message(s) from {channel_name}")
    return channel_messages


# ============================================================================
# Parallel fetch across all channels
# ============================================================================

all_messages: list[dict] = []

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
    futures = {executor.submit(fetch_channel, url): url for url in CHANNELS}
    for future in as_completed(futures):
        url = futures[future]
        try:
            all_messages.extend(future.result())
        except Exception as exc:
            import traceback
            print(f"FAIL {url}: {exc}")
            traceback.print_exc()

# Write summary
summary = {
    'run_id':         RUN_ID,
    'fetched_at':     datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    'total_messages': len(all_messages),
    'channels':       sorted({m['channel'] for m in all_messages}),
}
with open(OUTPUT_DIR / 'summary.json', 'w', encoding='utf-8') as f:
    json.dump(summary, f, indent=2)

print(f"\nSummary: {len(all_messages)} new message(s) across {len(summary['channels'])} channel(s)")

# Write to GITHUB_OUTPUT so downstream steps can branch on message_count
gh_output = os.environ.get('GITHUB_OUTPUT', '')
if gh_output:
    with open(gh_output, 'a') as f:
        f.write(f"message_count={len(all_messages)}\n")

sys.exit(0)
