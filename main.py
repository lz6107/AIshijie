import os
import re
import json
import time
import html
import sqlite3
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
    # 美股 / 宏观
    "https://feeds.bloomberg.com/markets/news.rss",
    "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://feeds.reuters.com/news/usmarkets",
    # 加一点港股/中国市场联动信号
    "https://www.scmp.com/rss/91/feed",
    "https://finance.yahoo.com/news/rssindex",
]

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
SEND_DELAY = float(os.getenv("SEND_DELAY", "2"))
MAX_SUMMARY_LENGTH = int(os.getenv("MAX_SUMMARY_LENGTH", "500"))
MAX_FEED_ITEMS_PER_CHECK = int(os.getenv("MAX_FEED_ITEMS_PER_CHECK", "8"))

MODEL_NAME = "gpt-5.4-nano"
FIRST_RUN_SKIP_OLD = True
IMAGES_DIR = "images"

REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Referer": "https://www.google.com/",
}

client = OpenAI(api_key=OPENAI_API_KEY)


# =========================
# RSS 过滤关键词
# =========================

SKIP_KEYWORDS = [
    "podcast",
    "newsletter",
    "video",
    "watch live",
    "live blog",
    "live updates",
    "minute-by-minute",
    "opinion",
    "editorial",
]


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


def should_skip_title(title_en: str) -> bool:
    title_lower = (title_en or "").lower().strip()
    if not title_lower:
        return True
    return any(k in title_lower for k in SKIP_KEYWORDS)


def clean_one_line(text: str) -> str:
    if not text:
        return ""
    text = clean_html(text)
    text = text.replace("...", "").replace("……", "")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" \n\r\t-—:：")


def clean_paragraph(text: str) -> str:
    if not text:
        return ""
    text = clean_html(text)
    text = text.replace("...", "").replace("……", "")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    lines = [x.strip() for x in text.split("\n") if x.strip()]
    return "\n".join(lines).strip()


# =========================
# 图片处理
# =========================

def image_path(filename: str) -> str:
    return os.path.join(IMAGES_DIR, filename)


IMAGE_MAP = {
    ("market", "偏多"): "01_market_bull.png",
    ("market", "偏空"): "02_market_bear.png",
    ("market", "中性"): "03_market_neutral.png",
    ("market", "观望"): "04_market_watch.png",

    ("macro", "偏多"): "05_macro_bull.png",
    ("macro", "偏空"): "06_macro_bear.png",
    ("macro", "中性"): "07_macro_neutral.png",
    ("macro", "观望"): "08_macro_watch.png",

    ("earnings", "偏多"): "09_earnings_bull.png",
    ("earnings", "偏空"): "10_earnings_bear.png",
    ("earnings", "中性"): "11_earnings_neutral.png",
    ("earnings", "观望"): "12_earnings_watch.png",

    ("sector", "偏多"): "13_sector_bull.png",
    ("sector", "偏空"): "14_sector_bear.png",
    ("sector", "中性"): "15_sector_neutral.png",
    ("sector", "观望"): "16_sector_watch.png",

    ("flow", "偏多"): "17_flow_bull.png",
    ("flow", "偏空"): "18_flow_bear.png",
    ("flow", "中性"): "19_flow_neutral.png",
    ("flow", "观望"): "20_flow_watch.png",

    ("tech", "偏多"): "21_tech_bull.png",
    ("tech", "偏空"): "22_tech_bear.png",
    ("tech", "中性"): "23_tech_neutral.png",
    ("tech", "观望"): "24_tech_watch.png",

    ("cyclical", "偏多"): "25_cyclical_bull.png",
    ("cyclical", "偏空"): "26_cyclical_bear.png",
    ("cyclical", "中性"): "27_cyclical_neutral.png",
    ("cyclical", "观望"): "28_cyclical_watch.png",

    ("risk", "偏多"): "29_risk_bull.png",
    ("risk", "偏空"): "30_risk_bear.png",
    ("risk", "中性"): "31_risk_neutral.png",
    ("risk", "观望"): "32_risk_watch.png",
}

MARKET_FALLBACK_MAP = {
    "美股": "41_fallback_us.png",
    "A股": "42_fallback_cn.png",
    "港股": "43_fallback_hk.png",
}


def get_best_local_image(content_type: str, bias: str, market_tag: str) -> str:
    # 先按主图匹配
    filename = IMAGE_MAP.get((content_type, bias))
    if filename:
        path = image_path(filename)
        if os.path.isfile(path):
            return path

    # 主图没匹配到，走市场兜底图
    fallback = MARKET_FALLBACK_MAP.get(market_tag)
    if fallback:
        path = image_path(fallback)
        if os.path.isfile(path):
            return path

    # 最后兜底
    neutral_path = image_path("40_fallback_neutral.png")
    if os.path.isfile(neutral_path):
        return neutral_path

    bull_path = image_path("39_fallback_bull.png")
    if os.path.isfile(bull_path):
        return bull_path

    return ""


# =========================
# AI 提示词
# =========================

SYSTEM_PROMPT = """
你是“观市财经”的中文股市编辑，负责把英文财经新闻加工成适合中文频道发布的内容。

覆盖市场：
A股、港股、美股

你的任务不是机械翻译，而是做中文编译和市场提炼。

要求：
1. 不要逐句直译，不要翻译腔
2. 不要输出英文
3. 不要输出原新闻标题、原新闻摘要、来源、链接
4. main_text 要写成适合频道发布的“观市财经”正文，2到4句
5. takeaway 要写成“观市看点”，只写1句
6. 同时判断 market_tag、content_type、bias
7. 语言自然、简洁、专业，不要喊单，不要夸张
8. 不要保留原新闻痕迹，要像重新加工后的中文内容
9. main_text 不要写成模板化套话，不要总是同一种开头
10. takeaway 要简短、有判断，不要重复正文
11. 只输出 JSON，不要输出 JSON 以外的任何内容

market_tag 只能是：
A股、港股、美股

content_type 只能是：
market、macro、earnings、sector、flow、tech、cyclical、risk

bias 只能是：
偏多、偏空、中性、观望
""".strip()


def build_user_prompt(title_en: str, summary_en: str) -> str:
    return f"""
请根据下面这条英文财经新闻，输出一个 JSON 对象，不要输出 JSON 以外的任何内容。

JSON 格式必须严格如下：
{{
  "market_tag": "A股/港股/美股",
  "content_type": "market/macro/earnings/sector/flow/tech/cyclical/risk",
  "bias": "偏多/偏空/中性/观望",
  "main_text": "2到4句加工后的中文正文",
  "takeaway": "1句简短的观市看点"
}}

字段要求：
1. market_tag 只能是：A股、港股、美股
2. content_type 只能是：market、macro、earnings、sector、flow、tech、cyclical、risk
3. bias 只能是：偏多、偏空、中性、观望
4. main_text 写成“观市财经”风格，2到4句，不要翻译腔，不要来源痕迹
5. takeaway 写成“观市看点”风格，只写1句，简短有判断
6. 不要输出英文
7. 不要输出来源
8. 不要输出链接
9. 不要输出多余字段
10. 不要使用省略号
11. 句子必须完整

英文标题：
{title_en}

英文摘要：
{summary_en if summary_en else "（无摘要）"}
""".strip()


def extract_json_object(text: str) -> str:
    if not text:
        return ""
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0).strip() if m else ""


def ai_compile_news(title_en: str, summary_en: str) -> dict:
    prompt = build_user_prompt(title_en, summary_en)

    response = client.responses.create(
        model=MODEL_NAME,
        instructions=SYSTEM_PROMPT,
        input=prompt,
    )
    raw_text = (response.output_text or "").strip()
    raw_json = extract_json_object(raw_text)
    if not raw_json:
        return {}

    try:
        data = json.loads(raw_json)
    except Exception:
        return {}

    market_tag = clean_one_line(str(data.get("market_tag", "")))
    content_type = clean_one_line(str(data.get("content_type", "")))
    bias = clean_one_line(str(data.get("bias", "")))
    main_text = clean_paragraph(str(data.get("main_text", "")))
    takeaway = clean_one_line(str(data.get("takeaway", "")))

    valid_market = {"A股", "港股", "美股"}
    valid_type = {"market", "macro", "earnings", "sector", "flow", "tech", "cyclical", "risk"}
    valid_bias = {"偏多", "偏空", "中性", "观望"}

    if market_tag not in valid_market:
        return {}
    if content_type not in valid_type:
        return {}
    if bias not in valid_bias:
        return {}
    if not main_text or not takeaway:
        return {}

    return {
        "market_tag": market_tag,
        "content_type": content_type,
        "bias": bias,
        "main_text": main_text,
        "takeaway": takeaway,
    }


# =========================
# 标签映射
# =========================

TYPE_TAG_MAP = {
    "market": "#大盘",
    "macro": "#宏观",
    "earnings": "#财报",
    "sector": "#板块",
    "flow": "#资金",
    "tech": "#科技",
    "cyclical": "#周期",
    "risk": "#风险",
}


def build_final_text(result: dict) -> str:
    market_tag = "#" + result["market_tag"]
    type_tag = TYPE_TAG_MAP[result["content_type"]]
    bias_tag = "#" + result["bias"]

    return f"""【观市财经】
{result["main_text"]}

【观市看点】
{result["takeaway"]}

{market_tag} {type_tag} {bias_tag}""".strip()


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

        if should_skip_title(title_en):
            print("跳过低价值标题:", title_en)
            continue

        if has_sent(link):
            continue

        if first_run and FIRST_RUN_SKIP_OLD:
            print("首次运行，跳过旧新闻:", title_en)
            mark_sent(link)
            continue

        summary_en = extract_summary(entry)

        try:
            result = ai_compile_news(title_en, summary_en)
            if not result:
                print("AI 结果无效，跳过:", title_en)
                mark_sent(link)
                continue

            final_text = build_final_text(result)
            photo_path = get_best_local_image(
                result["content_type"],
                result["bias"],
                result["market_tag"]
            )

            if photo_path and os.path.isfile(photo_path):
                resp = send_telegram_photo_by_file(photo_path, final_text)
                if resp.status_code != 200:
                    print("图片发送失败，改为纯文字")
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

        time.sleep(SEND_DELAY)


def main():
    if not BOT_TOKEN:
        raise ValueError("缺少环境变量 BOT_TOKEN")
    if not CHAT_ID:
        raise ValueError("缺少环境变量 CHAT_ID")
    if not OPENAI_API_KEY:
        raise ValueError("缺少环境变量 OPENAI_API_KEY")

    init_db()

    print("观市财经频道机器人启动成功")
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
