import os
import re
import time
import html
import sqlite3
import random
import tempfile
from datetime import datetime
from urllib.parse import urlparse, urljoin

import feedparser
import requests
from openai import OpenAI


# =========================
# 基础配置
# =========================

RSS_URLS = [
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://cointelegraph.com/rss",
    "https://decrypt.co/feed",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.reuters.com/Reuters/worldNews",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))

MODEL_NAME = "gpt-5.4-nano"
FIRST_RUN_SKIP_OLD = True
MAX_SUMMARY_LENGTH = 420
SEND_DELAY = 2
COVERS_DIR = "covers"
MAX_FEED_ITEMS_PER_CHECK = 10

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# 数据库
# =========================

def init_db():
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sent_items (
            link TEXT PRIMARY KEY,
            created_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def has_sent(link: str) -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM sent_items WHERE link = ?", (link,))
    row = cur.fetchone()
    conn.close()
    return row is not None


def mark_sent(link: str):
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute(
        "INSERT OR IGNORE INTO sent_items(link, created_at) VALUES (?, ?)",
        (link, datetime.now().isoformat())
    )
    conn.commit()
    conn.close()


def has_any_sent_items() -> bool:
    conn = sqlite3.connect("data.db")
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sent_items")
    count = cur.fetchone()[0]
    conn.close()
    return count > 0


# =========================
# 文本处理
# =========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\s*>", "\n", text, flags=re.I)
    text = re.sub(r"<.*?>", "", text, flags=re.S)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def shorten_text(text: str, max_len: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= max_len:
        return text

    cut = text[:max_len].rstrip()

    split_chars = ["。", "！", "？", "；", "，", ".", "!", "?", ";", ","]
    last_pos = -1
    for ch in split_chars:
        pos = cut.rfind(ch)
        if pos > last_pos:
            last_pos = pos

    if last_pos >= max_len // 2:
        cut = cut[:last_pos + 1].rstrip()

    return cut


def extract_summary(entry) -> str:
    raw_summary = (
        getattr(entry, "summary", "")
        or getattr(entry, "description", "")
    )

    content_list = getattr(entry, "content", None)
    if content_list and isinstance(content_list, list):
        for item in content_list:
            value = item.get("value", "")
            if value and len(value) > len(raw_summary):
                raw_summary = value

    summary_clean = clean_html(raw_summary)
    summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

    if len(summary_clean) < 40:
        return ""

    return shorten_text(summary_clean, MAX_SUMMARY_LENGTH)


# =========================
# 图片处理
# =========================

def is_valid_http_url(url: str) -> bool:
    if not url:
        return False
    try:
        p = urlparse(url)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def normalize_image_url(img_url: str, base_url: str) -> str:
    if not img_url:
        return ""
    if img_url.startswith("//"):
        return "https:" + img_url
    if img_url.startswith("/"):
        return urljoin(base_url, img_url)
    return img_url


def get_image_url_from_rss(entry) -> str:
    media_content = getattr(entry, "media_content", None)
    if media_content and isinstance(media_content, list):
        for item in media_content:
            url = item.get("url")
            if url:
                return url

    media_thumbnail = getattr(entry, "media_thumbnail", None)
    if media_thumbnail and isinstance(media_thumbnail, list):
        for item in media_thumbnail:
            url = item.get("url")
            if url:
                return url

    links = getattr(entry, "links", [])
    if links:
        for item in links:
            href = item.get("href", "")
            type_ = item.get("type", "")
            rel = item.get("rel", "")
            if href and (rel == "enclosure" or str(type_).startswith("image/")):
                return href

    raw_summary = getattr(entry, "summary", "") or getattr(entry, "description", "")
    if raw_summary:
        m = re.search(r'<img[^>]+src="([^"]+)"', raw_summary, re.I)
        if m:
            return m.group(1)

    return ""


def get_image_url_from_page(article_url: str) -> str:
    if not is_valid_http_url(article_url):
        return ""

    try:
        resp = requests.get(article_url, headers=REQUEST_HEADERS, timeout=15)
        if resp.status_code != 200 or not resp.text:
            return ""

        html_text = resp.text

        patterns = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
        ]

        for pattern in patterns:
            m = re.search(pattern, html_text, re.I)
            if m:
                img = normalize_image_url(m.group(1).strip(), article_url)
                if is_valid_http_url(img):
                    return img

        imgs = re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', html_text, re.I)
        for img in imgs:
            img = normalize_image_url(img.strip(), article_url)
            if not is_valid_http_url(img):
                continue

            lower_img = img.lower()
            if any(x in lower_img for x in ["logo", "icon", "avatar", "sprite", ".svg"]):
                continue

            return img

    except Exception as e:
        print(f"网页抓图失败: {article_url} -> {e}")

    return ""


def get_local_cover_list():
    if not os.path.isdir(COVERS_DIR):
        return []

    files = []
    for name in os.listdir(COVERS_DIR):
        lower = name.lower()
        if lower.endswith(".jpg") or lower.endswith(".jpeg") or lower.endswith(".png"):
            files.append(os.path.join(COVERS_DIR, name))

    return sorted(files)


def get_random_local_cover():
    covers = get_local_cover_list()
    if not covers:
        return ""
    return random.choice(covers)


def get_best_remote_image_url(entry, article_url: str) -> str:
    rss_img = get_image_url_from_rss(entry)
    if is_valid_http_url(rss_img):
        return rss_img

    page_img = get_image_url_from_page(article_url)
    if is_valid_http_url(page_img):
        return page_img

    return ""


def guess_extension_from_response(resp, url: str) -> str:
    content_type = (resp.headers.get("Content-Type") or "").lower()

    if "jpeg" in content_type or "jpg" in content_type:
        return ".jpg"
    if "png" in content_type:
        return ".png"
    if "webp" in content_type:
        return ".webp"

    parsed = urlparse(url)
    path = parsed.path.lower()
    if path.endswith(".jpg") or path.endswith(".jpeg"):
        return ".jpg"
    if path.endswith(".png"):
        return ".png"
    if path.endswith(".webp"):
        return ".webp"

    return ".jpg"


def download_remote_image(url: str) -> str:
    if not is_valid_http_url(url):
        return ""

    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=20, stream=True)
        if resp.status_code != 200:
            print(f"下载图片失败，状态码: {resp.status_code} -> {url}")
            return ""

        content_type = (resp.headers.get("Content-Type") or "").lower()
        if "image/" not in content_type and not any(x in content_type for x in ["jpeg", "jpg", "png", "webp"]):
            print(f"下载内容不是图片: {content_type} -> {url}")
            return ""

        ext = guess_extension_from_response(resp, url)
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp:
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    tmp.write(chunk)
            return tmp.name

    except Exception as e:
        print(f"下载远程图片异常: {url} -> {e}")
        return ""


# =========================
# AI 提示词
# =========================

SYSTEM_PROMPT = """
你是“势界行情深读”的中文财经编辑。你的任务不是机械翻译，而是做中文编译 + 市场解读。

写作要求：
1. 语言自然、简洁、有判断力，不要逐句直译
2. 不要空话，不要喊单，不要夸张
3. 重点分析市场如何理解这条消息，而不是重复新闻本身
4. 每条内容只从2到3个角度展开，角度可包括：情绪、资金预期、短线影响、后续观察、风险、供需、交易逻辑
5. 不要加入原文没有的信息
6. 不要输出英文
7. 输出必须严格按照指定模板
8. 【市场倾向】必须和结果写在同一行，不能换行单独写
9. 不要反复使用这些句式：
   - 这条消息的核心在于……
   - 真正需要观察的是……
   - 市场会把……视为……
   - 短线更容易……
   - 本质上会先被交易层面当作……
10. 每次尽量更换开头表达方式
11. 可以灵活使用但不要反复重复：
   - 先看结果，这条消息……
   - 对市场来说，更重要的是……
   - 真正有影响的不是……而是……
   - 从交易层面看……
   - 这类变化通常先影响……
   - 表面看是……，但盘面更在意……
   - 这件事释放出的信号是……
   - 如果市场继续沿着这个逻辑交易……
12. 不要写成公文腔，也不要每句都像结论句
13. 不要使用“...”或“……”或任何省略式表达
14. 句子必须完整，宁可更短也不要半句话
15. 【势界行情深读】部分总字数尽量控制在70到110字之间
""".strip()


def build_user_prompt(title_en: str, summary_en: str) -> str:
    return f"""
请根据下面这条英文财经新闻，生成适合“势界行情深读”频道发布的中文内容。

严格按这个格式输出：

【新闻】
用一句中文概括这条新闻

【势界行情深读】
写2到3句，分析市场如何理解这条消息，语气稳健，偏交易视角，但不要写成固定模板。
每次尽量换一种表达方式，不要总是同一种句式起笔。

【市场倾向】 偏多 / 偏空 / 中性 / 观望
注意：
1. 必须只输出其中一个结果
2. 必须和【市场倾向】写在同一行
3. 不能拆成两行

额外要求：
1. 不要输出英文标题
2. 不要输出英文摘要
3. 不要输出来源
4. 不要输出链接
5. 不要添加多余栏目
6. 最终只输出中文成品
7. 【势界行情深读】部分避免模板化、套话化、公文腔
8. 不要使用“……”或“...”结尾
9. 句子必须完整，不要半句话

英文标题：
{title_en}

英文摘要：
{summary_en if summary_en else "（无摘要）"}
""".strip()


def ai_compile_news(title_en: str, summary_en: str) -> str:
    prompt = build_user_prompt(title_en, summary_en)

    response = client.responses.create(
        model=MODEL_NAME,
        instructions=SYSTEM_PROMPT,
        input=prompt,
    )
    return (response.output_text or "").strip()


def is_valid_ai_output(text: str) -> bool:
    if not text:
        return False
    required = ["【新闻】", "【势界行情深读】", "【市场倾向】"]
    return all(x in text for x in required)


# =========================
# Telegram 发送
# =========================

def send_telegram_message(text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": text,
            "disable_web_page_preview": True
        },
        timeout=30
    )
    print("sendMessage 结果:", resp.status_code, resp.text)
    return resp


def send_telegram_photo_by_file(photo_path: str, caption: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    with open(photo_path, "rb") as f:
        resp = requests.post(
            url,
            data={
                "chat_id": CHAT_ID,
                "caption": caption
            },
            files={"photo": f},
            timeout=30
        )
    print("sendPhoto(file) 结果:", resp.status_code, resp.text)
    return resp


# =========================
# 主流程
# =========================

def process_feed(feed_url: str):
    print(f"[{datetime.now()}] 检查 RSS: {feed_url}")
    feed = feedparser.parse(feed_url)

    if not feed.entries:
        print("没有抓到内容")
        return

    entries = list(feed.entries[:MAX_FEED_ITEMS_PER_CHECK])
    entries.reverse()

    first_run = not has_any_sent_items()

    for entry in entries:
        link = getattr(entry, "link", "").strip()
        title_en = clean_html(getattr(entry, "title", "").strip())

        if not link or not title_en:
            continue

        if has_sent(link):
            continue

        if first_run and FIRST_RUN_SKIP_OLD:
            print("首次运行，跳过旧新闻:", title_en)
            mark_sent(link)
            continue

        summary_en = extract_summary(entry)
        temp_remote_file = ""

        try:
            final_text = ai_compile_news(title_en, summary_en)

            if not is_valid_ai_output(final_text):
                print("AI 输出格式不合格，跳过:", title_en)
                mark_sent(link)
                continue

            resp = None

            remote_img_url = get_best_remote_image_url(entry, link)
            if remote_img_url:
                temp_remote_file = download_remote_image(remote_img_url)

            if temp_remote_file and os.path.isfile(temp_remote_file):
                resp = send_telegram_photo_by_file(temp_remote_file, final_text)
                if resp.status_code != 200:
                    print("远程图上传失败，尝试公图")

            if resp is None or resp.status_code != 200:
                local_cover = get_random_local_cover()
                if local_cover and os.path.isfile(local_cover):
                    resp = send_telegram_photo_by_file(local_cover, final_text)
                    if resp.status_code != 200:
                        print("公图发送失败，改为纯文字")
                        resp = send_telegram_message(final_text)
                else:
                    resp = send_telegram_message(final_text)

            if resp.status_code == 200:
                mark_sent(link)
                print("已发送:", title_en)
            else:
                print("发送失败，未记录:", title_en)

        except Exception as e:
            print("处理失败:", title_en, "->", e)

        finally:
            if temp_remote_file and os.path.isfile(temp_remote_file):
                try:
                    os.remove(temp_remote_file)
                except Exception:
                    pass

        time.sleep(SEND_DELAY)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少环境变量 BOT_TOKEN")
    if not CHAT_ID:
        raise ValueError("缺少环境变量 CHAT_ID")
    if not OPENAI_API_KEY:
        raise ValueError("缺少环境变量 OPENAI_API_KEY")

    init_db()

    print("势界行情深读机器人启动成功")
    print("频道:", CHAT_ID)

    while True:
        for rss in RSS_URLS:
            try:
                process_feed(rss)
            except Exception as e:
                print(f"处理 RSS 失败 {rss}: {e}")

        print(f"休眠 {CHECK_INTERVAL} 秒...\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
