import os
import re
import time
import html
import sqlite3
from datetime import datetime

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

MODEL_NAME = "gpt-5.2"
FIRST_RUN_SKIP_OLD = True
MAX_SUMMARY_LENGTH = 1800
SEND_DELAY = 2

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
# 文本清洗
# =========================

def clean_html(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = re.sub(r"<br\\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"</p\\s*>", "\n", text, flags=re.I)
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
    return text[:max_len].rstrip() + "..."


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
# AI 编译
# =========================

SYSTEM_PROMPT = """
你是“势界行情深读”的中文财经编辑，擅长把英文财经、加密市场、宏观新闻整理成适合中文频道发布的市场解读内容。

你的任务不是机械翻译，而是做“中文编译 + 市场解读”。

你输出内容时，必须使用“势界观点”的口吻，风格要求如下：

1. 语言自然、清晰，像中文财经编辑写的，不要逐句直译
2. 不要空话、套话、废话，不要机械复述新闻
3. 不要喊单，不要夸张，不要使用“必涨”“必跌”“马上起飞”之类表达
4. 重点分析这条消息对市场意味着什么，而不是只说新闻发生了什么
5. 优先从以下角度中选择2到4个展开：
   - 市场情绪
   - 资金预期
   - 短线影响
   - 后续持续性
   - 风险提示
6. 风格偏交易视角，但表达要稳健
7. 不能编造原文没有的信息
8. 不要输出英文
9. 输出必须适合 Telegram 频道阅读，结构清楚，长度适中
10. 最终必须严格按照指定模板输出
""".strip()


def build_user_prompt(title_en: str, summary_en: str) -> str:
    return f"""
请根据下面这条英文财经新闻，生成适合“势界行情深读”频道发布的中文内容。

请严格按以下格式输出：

【势界行情深读】

新闻：
用一句中文概括这条新闻，不要逐字直译，要像中文财经媒体的一句话新闻导语。

势界观点：
写3到4句分析，解释这条消息对市场意味着什么。
重点不是复述新闻，而是分析市场会如何理解这条消息。
优先从以下角度中选择2到4个展开：
- 市场情绪
- 资金预期
- 短线影响
- 后续持续性
- 风险提示

要求：
1. 不要逐句翻译原文
2. 不要只是复述新闻
3. 要像频道编辑在做市场拆解
4. 语气稳健，有交易视角，但不要喊单
5. 不要加入原文没有的信息
6. 每句尽量有信息量，不要空泛

市场倾向：
只能从以下四个中选一个：
偏多 / 偏空 / 中性 / 观望

补充要求：
- “新闻”部分只能写1句
- “势界观点”部分写3到4句
- “市场倾向”只能输出一个词，不要解释
- 不要使用项目符号
- 不要输出多余说明
- 直接输出最终成品
- 不要出现“根据新闻来看”“综合来看”这类空泛开头
- “势界观点”要尽量像有市场经验的人在拆解消息

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

    text = (response.output_text or "").strip()
    return text


def is_valid_ai_output(text: str) -> bool:
    if not text:
        return False
    required = ["【势界行情深读】", "新闻：", "势界观点：", "市场倾向："]
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


# =========================
# 主流程
# =========================

def process_feed(feed_url: str):
    print(f"[{datetime.now()}] 检查 RSS: {feed_url}")
    feed = feedparser.parse(feed_url)

    if not feed.entries:
        print("没有抓到内容")
        return

    entries = list(feed.entries[:10])
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

        try:
            final_text = ai_compile_news(title_en, summary_en)

            if not is_valid_ai_output(final_text):
                print("AI 输出格式不合格，跳过:", title_en)
                mark_sent(link)
                continue

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

    print("势界行情深读机器人启动成功")
    print("频道:", CHAT_ID)

    while True:
        for rss in RSS_URLS:
            try:
                process_feed(rss)
            except Exception as e:
                print(f"处理 RSS 失败 {rss}: {e}")

        print(f"休眠 {CHECK_INTERVAL} 秒...\\n")
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
