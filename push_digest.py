#!/usr/bin/env python3
"""
Daily automation that gathers AI/robotics and finance news, weather, server health,
and Google Calendar events, then delivers the digest via Server酱 (ServerChan).
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import textwrap
import warnings
from html import unescape
from time import monotonic, sleep
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import feedparser
import psutil
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from dateutil.parser import _parser

warnings.filterwarnings("ignore", category=_parser.UnknownTimezoneWarning)

SENSITIVE_ENV_KEYS = {
    "SERVERCHAN_KEY",
    "OPENROUTER_KEY",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
    "GOOGLE_CALENDAR_ID",
}


def load_env_file(path: Optional[str]) -> None:
    if not path:
        return
    expanded = os.path.expanduser(path)
    if not os.path.exists(expanded):
        return
    try:
        with open(expanded, "r", encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                if not key or key in os.environ or key in SENSITIVE_ENV_KEYS:
                    continue
                value = value.strip().strip('"').strip("'")
                os.environ[key] = value
    except OSError as exc:  # pragma: no cover - filesystem issue
        logging.warning("无法读取 env 文件 %s：%s", expanded, exc)


ENV_FILE = os.getenv("USEFUL_PUSH_ENV_FILE", ".env")
load_env_file(ENV_FILE)

try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    from backports.zoneinfo import ZoneInfo

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:  # pragma: no cover
    service_account = None
    build = None


TZ = ZoneInfo("Asia/Shanghai")
HTTP_TIMEOUT = 15
OPENROUTER_MODEL = "qwen/qwen3-235b-a22b:free"
OPENROUTER_MAX_CALLS_PER_MIN = 12
OPENROUTER_MIN_INTERVAL = 60.0 / OPENROUTER_MAX_CALLS_PER_MIN
OPENROUTER_MAX_RETRIES = 5
OPENROUTER_BACKOFF_BASE = 3.0
OPENROUTER_FORCE = os.getenv("OPENROUTER_ALWAYS", "0").lower() in {"1", "true", "yes", "on"}
MAX_PROMPT_CHARS = int(os.getenv("OPENROUTER_MAX_CHARS", "6000"))
NEWS_LOOKBACK_HOURS = 24
MAX_NEWS_ITEMS = 20
CJK_RE = re.compile("[\u4e00-\u9fff]")
USER_AGENT = "useful_push/1.0 (+https://github.com/cwj/useful_push)"
NEWS_SOURCES_FILE = os.getenv("NEWS_SOURCES_FILE", "news_sources.json")
_last_openrouter_call = 0.0
def _build_http_session() -> requests.Session:
    session = requests.Session()
    retry = Retry(
        total=3,
        read=3,
        connect=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({"User-Agent": USER_AGENT})
    return session


REQUEST_SESSION = _build_http_session()

WEATHER_LOCATIONS = {
    "厦门市": (24.4798, 118.0894),
    "南平市浦城县": (27.9150, 118.5360),
}

DEFAULT_NEWS_SOURCES: List[Dict[str, str]] = [
    {
        "category": "ai",
        "label": "Google News - AI 热点",
        "url": "https://news.google.com/rss/search?q=AI+OR+%E4%BA%BA%E5%B7%A5%E6%99%BA%E8%83%BD+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    },
    {
        "category": "ai",
        "label": "ars technica | AI",
        "url": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    },
    {
        "category": "ai",
        "label": "MIT Technology Review",
        "url": "https://www.technologyreview.com/feed/",
    },
    {
        "category": "ai",
        "label": "VentureBeat AI",
        "url": "https://venturebeat.com/category/ai/feed/",
    },
    {
        "category": "ai",
        "label": "Google Blog - AI",
        "url": "https://blog.google/technology/ai/rss/",
    },
    {
        "category": "ai",
        "label": "Microsoft AI Blog",
        "url": "https://blogs.microsoft.com/ai/feed/",
    },
    {
        "category": "robotics",
        "label": "Google News - 机器人",
        "url": "https://news.google.com/rss/search?q=%E6%9C%BA%E5%99%A8%E4%BA%BA+OR+robotics+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    },
    {
        "category": "robotics",
        "label": "The Robot Report",
        "url": "https://www.therobotreport.com/feed/",
    },
    {
        "category": "robotics",
        "label": "IEEE Spectrum",
        "url": "https://spectrum.ieee.org/feed",
    },
    {
        "category": "robotics",
        "label": "Robotics Business Review",
        "url": "https://www.roboticsbusinessreview.com/feed/",
    },
    {
        "category": "robotics",
        "label": "Robohub",
        "url": "https://robohub.org/feed/",
    },
    {
        "category": "robotics",
        "label": "ScienceDaily - Robotics",
        "url": "https://rss.sciencedaily.com/computers_math/robotics.xml",
    },
    {
        "category": "finance",
        "label": "Google News - 中国财经",
        "url": "https://news.google.com/rss/search?q=%E4%B8%AD%E5%9B%BD+%E8%B4%A2%E7%BB%8F+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    },
    {
        "category": "finance",
        "label": "MarketWatch - Top Stories",
        "url": "https://www.marketwatch.com/rss/topstories",
    },
    {
        "category": "finance",
        "label": "BBC Business",
        "url": "https://feeds.bbci.co.uk/news/business/rss.xml",
    },
    {
        "category": "finance",
        "label": "The Economist - Finance & Economics",
        "url": "https://www.economist.com/finance-and-economics/rss.xml",
    },
    {
        "category": "finance",
        "label": "Financial Times - World Economy",
        "url": "https://www.ft.com/world-economy?format=rss",
    },
    {
        "category": "finance",
        "label": "Wall Street Journal - Markets",
        "url": "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    },
    {
        "category": "finance",
        "label": "SCMP - Economy",
        "url": "https://www.scmp.com/rss/91/feed",
    },
    {
        "category": "tech",
        "label": "Google News - 科技",
        "url": "https://news.google.com/rss/search?q=%E7%A7%91%E6%8A%80+when:1d&hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
    },
    {
        "category": "tech",
        "label": "TechCrunch",
        "url": "https://techcrunch.com/feed/",
    },
    {
        "category": "tech",
        "label": "The Verge",
        "url": "https://www.theverge.com/rss/index.xml",
    },
    {
        "category": "tech",
        "label": "WIRED",
        "url": "https://www.wired.com/feed/rss",
    },
    {
        "category": "tech",
        "label": "Engadget",
        "url": "https://www.engadget.com/rss.xml",
    },
    {
        "category": "tech",
        "label": "CNBC Technology",
        "url": "https://www.cnbc.com/id/100003114/device/rss/rss.html",
    },
]

NEWS_CATEGORY_META = {
    "ai": {
        "push_title": "AI 新闻速递",
        "section_title": "AI 热点（过去 24 小时）",
        "topic_label": "AI",
        "max_items": 20,
    },
    "robotics": {
        "push_title": "机器人观察",
        "section_title": "机器人行业动态（过去 24 小时）",
        "topic_label": "机器人",
        "max_items": 20,
    },
    "finance": {
        "push_title": "财经要闻",
        "section_title": "财经 / 宏观经济",
        "topic_label": "财经 / 宏观经济",
        "max_items": 20,
    },
    "tech": {
        "push_title": "科技快讯",
        "section_title": "全球科技资讯",
        "topic_label": "科技",
        "max_items": 20,
    },
}

def load_news_sources() -> Dict[str, List[NewsSource]]:
    raw_sources: List[Dict[str, str]] = DEFAULT_NEWS_SOURCES
    try:
        with open(NEWS_SOURCES_FILE, "r", encoding="utf-8") as handle:
            raw_sources = json.load(handle)
    except FileNotFoundError:
        logging.warning("找不到 %s，使用内置默认新闻源。", NEWS_SOURCES_FILE)
    except json.JSONDecodeError as exc:
        logging.error("解析 %s 失败（%s），使用内置默认新闻源。", NEWS_SOURCES_FILE, exc)
    grouped: Dict[str, List[NewsSource]] = {}
    for item in raw_sources:
        category = item.get("category")
        url = item.get("url")
        if not (category and url):
            continue
        grouped.setdefault(category, []).append(
            NewsSource(category=category, label=item.get("label", url), url=url)
        )
    return grouped


@dataclass
class NewsSource:
    category: str
    label: str
    url: str


@dataclass
class NewsEntry:
    title: str
    link: str
    published_at: Optional[datetime]
    source: Optional[str]
    description: str
    translation: Optional[str] = None
    summary: Optional[str] = None
    kept_original: bool = True


@dataclass
class WeatherDay:
    date: datetime
    weather: str
    temp_min: float
    temp_max: float
    apparent_min: Optional[float]
    apparent_max: Optional[float]
    precipitation_chance: Optional[float]
    precipitation_sum: Optional[float]
    windspeed_max: Optional[float]
    sunrise: Optional[datetime]
    sunset: Optional[datetime]


@dataclass
class ServerHealth:
    cpu_percent: float
    load_average: Tuple[float, float, float]
    memory_percent: float
    memory_used_gb: float
    memory_total_gb: float
    disk_percent: float
    disk_used_gb: float
    disk_total_gb: float
    uptime_hours: float


@dataclass
class CalendarEvent:
    start: str
    end: str
    summary: str
    location: Optional[str]
    all_day: bool = False


def trim_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def strip_html(text: str) -> str:
    if not text:
        return ""
    soup = BeautifulSoup(text, "html.parser")
    return trim_whitespace(unescape(soup.get_text(separator=" ", strip=True)))


def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = date_parser.parse(value)
        if not parsed.tzinfo:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(TZ)
    except (ValueError, TypeError):
        return None


def parse_local_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo:
        return dt.astimezone(TZ)
    return dt.replace(tzinfo=TZ)


def safe_get(seq: Sequence[Any], idx: int, default: Any = None) -> Any:
    try:
        return seq[idx]
    except (IndexError, TypeError):
        return default


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def fetch_feed_entries(
    feeds: Sequence[str], lookback_hours: int = NEWS_LOOKBACK_HOURS, max_items: int = MAX_NEWS_ITEMS
) -> List[NewsEntry]:
    entries: List[NewsEntry] = []
    cutoff = datetime.now(tz=TZ) - timedelta(hours=lookback_hours)
    for feed_url in feeds:
        try:
            resp = REQUEST_SESSION.get(feed_url, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.text)
        except Exception as exc:  # pragma: no cover - network issues
            logging.warning("Failed to read feed %s: %s", feed_url, exc)
            continue
        for entry in parsed.entries:
            published = parse_datetime(entry.get("published"))
            if published and published < cutoff:
                continue
            title = trim_whitespace(entry.get("title", ""))
            link = entry.get("link") or ""
            description = strip_html(entry.get("summary", "") or entry.get("description", ""))
            source = trim_whitespace(
                entry.get("source", {}).get("title") if isinstance(entry.get("source"), dict) else entry.get("source")
            )
            entries.append(
                NewsEntry(
                    title=title,
                    link=link,
                    published_at=published,
                    source=source,
                    description=description,
                )
            )
    # De-duplicate by link/title pair while preserving order
    seen = set()
    deduped: List[NewsEntry] = []
    baseline = datetime.min.replace(tzinfo=TZ)
    for entry in sorted(entries, key=lambda item: item.published_at or baseline, reverse=True):
        key = (entry.title, entry.link)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(entry)
        if len(deduped) >= max_items:
            break
    return deduped


def contains_cjk(text: str) -> bool:
    return bool(CJK_RE.search(text or ""))


def call_openrouter(messages: Sequence[Dict[str, str]], temperature: float = 0.2) -> Optional[str]:
    api_key = os.getenv("OPENROUTER_KEY")
    if not api_key:
        logging.warning("OPENROUTER_KEY is not set; skipping translation/summarization.")
        return None
    global _last_openrouter_call
    payload = {
        "model": OPENROUTER_MODEL,
        "temperature": temperature,
        "messages": list(messages),
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "HTTP-Referer": "https://github.com/cwj/useful_push",
        "X-Title": "useful_push",
    }
    for attempt in range(OPENROUTER_MAX_RETRIES):
        elapsed = monotonic() - _last_openrouter_call
        if elapsed < OPENROUTER_MIN_INTERVAL:
            sleep_time = OPENROUTER_MIN_INTERVAL - elapsed
            logging.debug("Respecting OpenRouter rate limit; sleeping %.2fs", sleep_time)
            sleep(sleep_time)
        try:
            resp = REQUEST_SESSION.post(
                "https://openrouter.ai/api/v1/chat/completions", headers=headers, json=payload, timeout=60
            )
            _last_openrouter_call = monotonic()
            if resp.status_code == 429:
                backoff = OPENROUTER_BACKOFF_BASE * (2**attempt) + random.uniform(0, 1)
                logging.warning("OpenRouter 429，等待 %.1f 秒后重试（第 %d 次）", backoff, attempt + 1)
                sleep(backoff)
                continue
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except requests.RequestException as exc:  # pragma: no cover - network issues
            backoff = OPENROUTER_BACKOFF_BASE * (2**attempt) + random.uniform(0, 1)
            logging.warning("OpenRouter 调用异常：%s，%.1f 秒后重试（第 %d 次）", exc, backoff, attempt + 1)
            sleep(backoff)
    logging.error("OpenRouter 连续 %d 次调用失败，跳过本条内容。", OPENROUTER_MAX_RETRIES)
    return None


def extract_json_from_text(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        snippet = text[start : end + 1]
        try:
            return json.loads(snippet)
        except json.JSONDecodeError:
            return None
    return None


def translate_and_summarize(entry: NewsEntry, topic_label: str) -> NewsEntry:
    body = entry.description or "（无摘要）"
    if MAX_PROMPT_CHARS > 0 and len(body) > MAX_PROMPT_CHARS:
        body = body[:MAX_PROMPT_CHARS] + "……"
    text_block = f"标题：{entry.title}\n内容：{body}"
    prompt = textwrap.dedent(
        f"""
        你是资讯助理。请阅读以下关于{topic_label}的新闻，将原文翻译成自然中文（如果已经是中文，请保持原文），
        并用两句话以内给出中文摘要。返回 JSON，格式为：
        {{
          "translation": "<中文译文或原文>",
          "summary": "<中文摘要>",
          "language": "<检测到的原文语言，例如 zh / en>"
        }}
        仅输出 JSON，不要带其它文字。
        新闻原文：
        {text_block}
        """
    ).strip()
    response = call_openrouter([{"role": "user", "content": prompt}])
    translation_used = True
    translation = None
    summary = None
    if response:
        data = extract_json_from_text(response)
        if data:
            translation = trim_whitespace(data.get("translation", ""))
            summary = trim_whitespace(data.get("summary", ""))
    if translation:
        translation = strip_html(translation)
    if not translation:
        translation = entry.description or entry.title
        translation_used = False
    if not summary:
        summary = "无法生成摘要，已保留原文。"
    entry.translation = translation
    entry.summary = summary
    entry.kept_original = not translation_used
    return entry


def enrich_news(entries: Iterable[NewsEntry], topic_label: str) -> List[NewsEntry]:
    enriched: List[NewsEntry] = []
    for entry in entries:
        if should_use_openrouter(entry):
            enriched.append(translate_and_summarize(entry, topic_label))
        else:
            fallback_text = entry.description or entry.title
            entry.translation = fallback_text
            entry.summary = local_summary(fallback_text, entry.title)
            entry.kept_original = True
            enriched.append(entry)
    return enriched


def fetch_weather() -> Dict[str, List[WeatherDay]]:
    weather_data: Dict[str, List[WeatherDay]] = {}
    for city, (lat, lon) in WEATHER_LOCATIONS.items():
        params = {
            "latitude": lat,
            "longitude": lon,
            "daily": ",".join(
                [
                    "weathercode",
                    "temperature_2m_max",
                    "temperature_2m_min",
                    "apparent_temperature_max",
                    "apparent_temperature_min",
                    "precipitation_probability_mean",
                    "precipitation_sum",
                    "windspeed_10m_max",
                    "sunrise",
                    "sunset",
                ]
            ),
            "timezone": "Asia/Shanghai",
        }
        try:
            resp = REQUEST_SESSION.get("https://api.open-meteo.com/v1/forecast", params=params, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            raw = resp.json()
        except Exception as exc:  # pragma: no cover - network issues
            logging.warning("Failed to fetch weather for %s: %s", city, exc)
            continue
        daily = raw.get("daily", {})
        days: List[WeatherDay] = []
        times = daily.get("time", [])[:3]
        codes = daily.get("weathercode", [])
        max_temps = daily.get("temperature_2m_max", [])
        min_temps = daily.get("temperature_2m_min", [])
        precip_list = daily.get("precipitation_probability_mean", [])
        apparent_max = daily.get("apparent_temperature_max", [])
        apparent_min = daily.get("apparent_temperature_min", [])
        precip_sum = daily.get("precipitation_sum", [])
        windspeed = daily.get("windspeed_10m_max", [])
        sunrise = daily.get("sunrise", [])
        sunset = daily.get("sunset", [])
        for idx, date_str in enumerate(times):
            date_obj = parse_local_iso(f"{date_str}T00:00:00") or (datetime.now(tz=TZ) + timedelta(days=idx))
            code = int(safe_get(codes, idx, 0) or 0)
            temp_max = float(safe_get(max_temps, idx, 0.0) or 0.0)
            temp_min = float(safe_get(min_temps, idx, 0.0) or 0.0)
            precip_val = safe_get(precip_list, idx)
            precip = float(precip_val) if precip_val is not None else None
            days.append(
                WeatherDay(
                    date=date_obj,
                    weather=weather_code_to_text(code),
                    temp_min=temp_min,
                    temp_max=temp_max,
                    apparent_min=_to_float(safe_get(apparent_min, idx)),
                    apparent_max=_to_float(safe_get(apparent_max, idx)),
                    precipitation_chance=precip,
                    precipitation_sum=_to_float(safe_get(precip_sum, idx)),
                    windspeed_max=_to_float(safe_get(windspeed, idx)),
                    sunrise=parse_local_iso(safe_get(sunrise, idx)),
                    sunset=parse_local_iso(safe_get(sunset, idx)),
                )
            )
        weather_data[city] = days
    return weather_data


def weather_code_to_text(code: int) -> str:
    table = {
        0: "晴",
        1: "以晴为主",
        2: "多云",
        3: "阴",
        45: "有雾",
        48: "雾凇",
        51: "毛毛雨",
        53: "小雨",
        55: "中雨",
        56: "冻毛毛雨",
        57: "冻雨",
        61: "小雨",
        63: "中雨",
        65: "大雨",
        71: "小雪",
        73: "中雪",
        75: "大雪",
        80: "阵雨",
        81: "强阵雨",
        82: "暴雨",
        95: "雷阵雨",
        96: "雷阵雨伴冰雹",
        99: "强雷阵雨伴冰雹",
    }
    return table.get(code, f"天气代码 {code}")


WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def weekday_cn(dt: datetime) -> str:
    return WEEKDAY_CN[dt.weekday() % 7]


def should_use_openrouter(entry: NewsEntry) -> bool:
    if OPENROUTER_FORCE:
        return True
    text = f"{entry.title} {entry.description}".strip()
    if not text:
        return True
    return not contains_cjk(text)


def local_summary(text: str, title: Optional[str] = None, max_chars: int = 200) -> str:
    clean = (text or "").strip()
    if not clean:
        return f"要点：{title.strip()}" if title else "暂无摘要。"
    # 使用第一句或截断内容
    sentence_end = re.search(r"[。！？!?.]", clean)
    candidate = clean[: sentence_end.end()] if sentence_end else clean
    if len(candidate) > max_chars:
        candidate = candidate[: max_chars - 1].rstrip() + "…"
    summary = f"要点：{candidate}"
    if title and candidate == clean:
        summary = f"要点：{title.strip()}——{candidate}"
    return summary


def log_step(message: str) -> None:
    logging.info("[STEP] %s", message)


def gather_server_health() -> ServerHealth:
    cpu_percent = psutil.cpu_percent(interval=1)
    load_average = os.getloadavg() if hasattr(os, "getloadavg") else (0.0, 0.0, 0.0)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    uptime_hours = (datetime.now(tz=TZ) - datetime.fromtimestamp(psutil.boot_time(), tz=TZ)).total_seconds() / 3600
    gb = 1024 ** 3
    return ServerHealth(
        cpu_percent=cpu_percent,
        load_average=load_average,
        memory_percent=mem.percent,
        memory_used_gb=mem.used / gb,
        memory_total_gb=mem.total / gb,
        disk_percent=disk.percent,
        disk_used_gb=disk.used / gb,
        disk_total_gb=disk.total / gb,
        uptime_hours=uptime_hours,
    )


def fetch_calendar_events() -> List[CalendarEvent]:
    json_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID")
    if not (json_path and calendar_id):
        logging.info("Google Calendar is not configured; skipping schedule section.")
        return []
    if not service_account or not build:
        logging.error("google-api-python-client is missing; run `pip install -r requirements.txt`.")
        return []
    scopes = ["https://www.googleapis.com/auth/calendar.readonly"]
    try:
        creds = service_account.Credentials.from_service_account_file(json_path, scopes=scopes)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        start = datetime.now(tz=TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        events_result = (
            service.events()
            .list(
                calendarId=calendar_id,
                timeMin=start.isoformat(),
                timeMax=end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
            )
            .execute()
        )
    except Exception as exc:  # pragma: no cover - API issues
        logging.warning("Failed to read Google Calendar: %s", exc)
        return []
    events: List[CalendarEvent] = []
    for item in events_result.get("items", []):
        start_info = item.get("start", {})
        end_info = item.get("end", {})
        all_day = "date" in start_info
        if all_day:
            start_str = start_info["date"]
            end_str = end_info.get("date", start_str)
        else:
            start_dt = date_parser.parse(start_info.get("dateTime", start_info.get("date")))
            end_dt = date_parser.parse(end_info.get("dateTime", end_info.get("date")))
            start_dt = start_dt.astimezone(TZ)
            end_dt = end_dt.astimezone(TZ)
            start_str = start_dt.strftime("%H:%M")
            end_str = end_dt.strftime("%H:%M")
        events.append(
            CalendarEvent(
                start=start_str,
                end=end_str,
                summary=item.get("summary", "无标题事件"),
                location=item.get("location"),
                all_day=all_day,
            )
        )
    return events


def format_news_section(title: str, entries: List[NewsEntry]) -> str:
    lines = [f"## {title}"]
    if not entries:
        lines.append("暂无最新内容，稍后再来看看。")
        return "\n".join(lines) + "\n"
    for idx, entry in enumerate(entries, start=1):
        time_str = entry.published_at.strftime("%m/%d %H:%M") if entry.published_at else "时间未知"
        source = entry.source or "来源未知"
        lines.append(f"{idx}. [{entry.title}]({entry.link}) · {source} · {time_str}")
        lines.append(f"   - 摘要：{entry.summary or '暂无'}")
        if entry.translation and entry.translation.strip() and entry.translation.strip() != (entry.description or "").strip():
            lines.append(f"   - 译文：{entry.translation}")
            lines.append(f"   - 原文：{entry.description or '原文缺失'}")
        else:
            lines.append(f"   - 原文：{entry.description or entry.translation or '原文缺失'}")
    lines.append("")
    return "\n".join(lines)


def _format_range(min_value: Optional[float], max_value: Optional[float], unit: str = "°C") -> str:
    if min_value is None and max_value is None:
        return "未知"
    if min_value is None:
        return f"{max_value:.1f}{unit}"
    if max_value is None:
        return f"{min_value:.1f}{unit}"
    return f"{min_value:.1f}~{max_value:.1f}{unit}"


def format_weather_section(weather: Dict[str, List[WeatherDay]]) -> str:
    lines = [f"## 天气预报（未来三天） | 更新时间 {datetime.now(tz=TZ).strftime('%m-%d %H:%M')}"]
    if not weather:
        lines.append("天气数据获取失败。")
        return "\n".join(lines) + "\n"
    for city, days in weather.items():
        lines.append(f"### {city}")
        if not days:
            lines.append("- 暂无数据")
            continue
        for day in days:
            date_label = f"{day.date.strftime('%m/%d')}（{weekday_cn(day.date)}）"
            actual_range = _format_range(day.temp_min, day.temp_max)
            apparent_range = _format_range(day.apparent_min, day.apparent_max)
            precip_prob = f"{day.precipitation_chance:.0f}%" if day.precipitation_chance is not None else "未知"
            precip_sum = f"{day.precipitation_sum:.1f}mm" if day.precipitation_sum is not None else "—"
            wind = f"{day.windspeed_max:.0f} km/h" if day.windspeed_max is not None else "—"
            sunrise = day.sunrise.strftime("%H:%M") if day.sunrise else "--:--"
            sunset = day.sunset.strftime("%H:%M") if day.sunset else "--:--"
            lines.append(f"- {date_label} · {day.weather}")
            lines.append(f"  - 气温：{actual_range}（体感 {apparent_range}）")
            lines.append(f"  - 风速：{wind} · 降水概率 {precip_prob} · 预计降水 {precip_sum}")
            lines.append(f"  - 日出 {sunrise} / 日落 {sunset}")
    lines.append("")
    return "\n".join(lines)


def format_server_section(health: ServerHealth) -> str:
    return "\n".join(
        [
            f"## 系统状态（{datetime.now(tz=TZ).strftime('%m-%d %H:%M')}）",
            f"- CPU：{health.cpu_percent:.1f}% · 负载 {health.load_average[0]:.2f}/{health.load_average[1]:.2f}/{health.load_average[2]:.2f}",
            f"- 内存：{health.memory_percent:.1f}% （{health.memory_used_gb:.1f}GB / {health.memory_total_gb:.1f}GB）",
            f"- 磁盘：{health.disk_percent:.1f}% （{health.disk_used_gb:.1f}GB / {health.disk_total_gb:.1f}GB）",
            f"- 运行时长：约 {health.uptime_hours:.1f} 小时",
            "",
        ]
    )


def format_calendar_section(events: List[CalendarEvent]) -> str:
    today = datetime.now(tz=TZ).strftime("%m-%d")
    lines = [f"## 今日日程（{today}）"]
    if not events:
        lines.append("暂无事件或未配置 Google Calendar。")
        return "\n".join(lines) + "\n"
    for event in events:
        when = "全天" if event.all_day else f"{event.start}-{event.end}"
        location = f" @ {event.location}" if event.location else ""
        lines.append(f"- {when}：{event.summary}{location}")
    lines.append("")
    return "\n".join(lines)


def build_push_payloads() -> List[Tuple[str, str]]:
    payloads: List[Tuple[str, str]] = []
    sources = load_news_sources()
    for category, meta in NEWS_CATEGORY_META.items():
        feeds = [src.url for src in sources.get(category, [])]
        if not feeds:
            logging.info("新闻类别 %s 暂无配置，跳过。", category)
            continue
        log_step(f"抓取 {meta['section_title']}（来源 {len(feeds)} 条）")
        entries = enrich_news(
            fetch_feed_entries(feeds, max_items=meta.get("max_items", MAX_NEWS_ITEMS)),
            meta["topic_label"],
        )
        body = format_news_section(meta["section_title"], entries)
        payloads.append((meta["push_title"], body))
    log_step("获取天气数据")
    weather_body = format_weather_section(fetch_weather())
    payloads.append(("天气速递", weather_body))
    log_step("收集服务器健康状况")
    payloads.append(("系统状态", format_server_section(gather_server_health())))
    log_step("读取 Google Calendar")
    payloads.append(("今日行程", format_calendar_section(fetch_calendar_events())))
    return payloads


def send_serverchan(title: str, body: str) -> None:
    key = os.getenv("SERVERCHAN_KEY")
    if not key:
        raise RuntimeError("SERVERCHAN_KEY 未配置，无法发送推送。")
    url = f"https://sctapi.ftqq.com/{key}.send"
    data = {"title": title, "desp": body}
    resp = REQUEST_SESSION.post(url, data=data, timeout=HTTP_TIMEOUT)
    resp.raise_for_status()
    logging.info("Server酱推送成功：%s", resp.json())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log_step("启动 useful_push 任务")
    payloads = build_push_payloads()
    for idx, (title, body) in enumerate(payloads, start=1):
        if not body.strip():
            logging.info("推送 %s 内容为空，跳过。", title)
            continue
        log_step(f"推送进度 {idx}/{len(payloads)}：发送 {title}")
        try:
            send_serverchan(title, body)
        except Exception as exc:  # pragma: no cover
            logging.exception("推送 %s 失败：%s", title, exc)
            continue


if __name__ == "__main__":
    main()
