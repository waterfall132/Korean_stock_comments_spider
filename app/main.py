import os
import re
import json
import time
import random
import sqlite3
import hashlib
from typing import List, Literal, Optional, Tuple
from urllib.parse import urljoin, parse_qs, urlparse

import requests
from bs4 import BeautifulSoup
from cachetools import TTLCache
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# =========================
# 基础配置
# =========================
BASE_URL = "https://finance.naver.com/item/board.nhn"
BASE_READ_URL = "https://finance.naver.com/item/board_read.naver"
BASE_DOMAIN = "https://finance.naver.com"

DB_PATH = os.getenv("DB_PATH", "naver_forum.db")

# 翻译接口（你指定的）
TRANSLATE_API_URL = os.getenv("TRANSLATE_API_URL", "https://60s.viki.moe/v2/fanyi")
TRANSLATE_TIMEOUT = float(os.getenv("TRANSLATE_TIMEOUT", "15"))
TRANSLATE_TARGET_MAP = {
    "zh": "zh-CHS",
    "en": "en",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

# =========================
# Requests Session
# =========================
session = requests.Session()
session.headers.update(HEADERS)
session.trust_env = False  # 不读系统代理，避免脏环境干扰

# 可选 Clash 代理
proxy_url = os.getenv("CLASH_HTTP_PROXY", "").strip()
if proxy_url:
    session.proxies.update({
        "http": proxy_url,
        "https": proxy_url
    })

retry = Retry(
    total=3,
    connect=3,
    read=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET"]
)
adapter = HTTPAdapter(max_retries=retry)
session.mount("http://", adapter)
session.mount("https://", adapter)

# =========================
# 缓存
# =========================
page_cache = TTLCache(maxsize=500, ttl=60)          # 列表页缓存 60s
translate_cache = TTLCache(maxsize=50000, ttl=86400)  # 翻译缓存 24h
detail_cache = TTLCache(maxsize=5000, ttl=600)      # 详情页缓存 10m

# =========================
# Pydantic models
# =========================
class PostItem(BaseModel):
    dedupe_key: str
    date: str
    title_ko: str
    title: str
    author: str
    views: int
    likes: int
    dislikes: int
    comments: int
    nid: Optional[str] = None
    post_url: Optional[str] = None


class PostsResponse(BaseModel):
    code: str
    page: int
    lang: Literal["ko", "zh", "en"]
    available_pages: List[int]
    next_group_page: Optional[int]
    count: int
    posts: List[PostItem]


class RangeResponse(BaseModel):
    code: str
    start_page: int
    end_page: int
    lang: Literal["ko", "zh", "en"]
    count: int
    posts: List[PostItem]


class PostDetailResponse(BaseModel):
    code: str
    nid: Optional[str] = None
    post_url: str
    title_ko: str
    title: str
    content_ko: str
    content: str


# =========================
# FastAPI app
# =========================
app = FastAPI(title="Naver Forum Crawler API", version="3.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境建议改为你的域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# 工具函数
# =========================
def to_int(text: str) -> int:
    if not text:
        return 0
    text = re.sub(r"[^\d-]", "", text)
    return int(text) if text else 0


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u200b", " ").replace("\xa0", " ")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_page_from_href(href: str) -> Optional[int]:
    if not href:
        return None
    m = re.search(r"[?&]page=(\d+)", href)
    return int(m.group(1)) if m else None


def extract_nid_from_href(href: str) -> Optional[str]:
    if not href:
        return None
    qs = parse_qs(urlparse(href).query)
    return qs.get("nid", [None])[0]


def build_dedupe_key(code: str, nid: Optional[str], post_url: Optional[str], title_ko: str, date: str) -> str:
    if nid:
        return f"nid:{nid}"
    if post_url:
        return f"url:{post_url}"
    raw = f"{code}|{title_ko}|{date}"
    return "hash:" + hashlib.md5(raw.encode("utf-8")).hexdigest()


def dedupe_posts(posts: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for p in posts:
        k = p["dedupe_key"]
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out

# =========================
# 翻译（改为 60s.viki.moe）
# =========================
def _split_text_for_get(text: str, max_chars: int = 700) -> List[str]:
    """GET query 参数长度限制，长文本分片"""
    text = text or ""
    if len(text) <= max_chars:
        return [text]

    parts = re.split(r"(\n)", text)  # 保留换行
    chunks, buf, cur = [], [], 0
    for p in parts:
        lp = len(p)
        if cur + lp > max_chars and buf:
            chunks.append("".join(buf))
            buf, cur = [p], lp
        else:
            buf.append(p)
            cur += lp
    if buf:
        chunks.append("".join(buf))
    return chunks


def _translate_viki_once(text: str, to_lang: str, from_lang: str = "auto") -> str:
    params = {
        "text": text,
        "from": from_lang,
        "to": to_lang,
        "encoding": "json",
    }
    r = session.get(TRANSLATE_API_URL, params=params, timeout=TRANSLATE_TIMEOUT)
    r.raise_for_status()
    data = r.json()

    if data.get("code") != 200:
        raise RuntimeError(f"翻译接口业务错误: {data.get('message', '')}")

    target = (data.get("data") or {}).get("target") or {}
    return str(target.get("text", "") or "")


def translate_ko(text: str, lang: Literal["ko", "zh", "en"]) -> str:
    if not text or lang == "ko":
        return text

    to_lang = TRANSLATE_TARGET_MAP.get(lang)
    if not to_lang:
        return text

    cache_key = f"{lang}:{text}"
    if cache_key in translate_cache:
        return translate_cache[cache_key]

    chunks = _split_text_for_get(text, max_chars=700)
    out = []

    for c in chunks:
        if not c:
            out.append(c)
            continue
        try:
            out.append(_translate_viki_once(c, to_lang=to_lang, from_lang="auto") or c)
        except Exception:
            out.append(c)  # 回退原文
        time.sleep(0.05)  # 轻微节流

    translated = "".join(out)
    translate_cache[cache_key] = translated
    return translated

# =========================
# 数据库（自动迁移）
# =========================
def _get_columns(conn: sqlite3.Connection, table_name: str) -> List[str]:
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    return [r[1] for r in rows]


def init_db_and_migrate():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1) forum_posts（先创建最小结构）
    cur.execute("""
    CREATE TABLE IF NOT EXISTS forum_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        nid TEXT,
        post_url TEXT,
        date TEXT,
        title_ko TEXT,
        author TEXT,
        views INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0,
        dislikes INTEGER DEFAULT 0,
        comments INTEGER DEFAULT 0,
        crawled_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # 2) 补列（兼容旧库）
    cols = set(_get_columns(conn, "forum_posts"))
    if "dedupe_key" not in cols:
        cur.execute("ALTER TABLE forum_posts ADD COLUMN dedupe_key TEXT")
    if "post_url" not in cols:
        cur.execute("ALTER TABLE forum_posts ADD COLUMN post_url TEXT")
    if "comments" not in cols:
        cur.execute("ALTER TABLE forum_posts ADD COLUMN comments INTEGER DEFAULT 0")

    # 3) 回填 dedupe_key
    rows = cur.execute("""
        SELECT id, code, nid, post_url, title_ko, date
        FROM forum_posts
        WHERE dedupe_key IS NULL OR dedupe_key=''
    """).fetchall()

    for _id, code, nid, post_url, title_ko, date in rows:
        key = build_dedupe_key(
            code=code or "",
            nid=nid,
            post_url=post_url,
            title_ko=title_ko or "",
            date=date or ""
        )
        cur.execute("UPDATE forum_posts SET dedupe_key=? WHERE id=?", (key, _id))

    # 4) 去重（同 code + dedupe_key 保留最新 id）
    cur.execute("""
    DELETE FROM forum_posts
    WHERE id NOT IN (
      SELECT MAX(id) FROM forum_posts
      WHERE dedupe_key IS NOT NULL AND dedupe_key <> ''
      GROUP BY code, dedupe_key
    )
    """)

    # 5) 索引
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_forum_posts_code_dedupe
    ON forum_posts(code, dedupe_key)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_posts_code_crawled
    ON forum_posts(code, crawled_at DESC)
    """)

    # 6) 详情表
    cur.execute("""
    CREATE TABLE IF NOT EXISTS forum_post_details (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT NOT NULL,
        dedupe_key TEXT NOT NULL,
        nid TEXT,
        post_url TEXT NOT NULL,
        title_ko TEXT,
        content_ko TEXT,
        fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)
    cur.execute("""
    CREATE UNIQUE INDEX IF NOT EXISTS uq_forum_post_details_code_dedupe
    ON forum_post_details(code, dedupe_key)
    """)
    cur.execute("""
    CREATE INDEX IF NOT EXISTS idx_detail_code_nid
    ON forum_post_details(code, nid)
    """)

    conn.commit()
    conn.close()


init_db_and_migrate()


def db_upsert_posts(code: str, posts: List[dict]):
    if not posts:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    sql = """
    INSERT INTO forum_posts
    (code, dedupe_key, nid, post_url, date, title_ko, author, views, likes, dislikes, comments)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(code, dedupe_key) DO UPDATE SET
      nid=excluded.nid,
      post_url=excluded.post_url,
      date=excluded.date,
      title_ko=excluded.title_ko,
      author=excluded.author,
      views=excluded.views,
      likes=excluded.likes,
      dislikes=excluded.dislikes,
      comments=excluded.comments,
      crawled_at=CURRENT_TIMESTAMP
    """
    for p in posts:
        cur.execute(sql, (
            code, p["dedupe_key"], p.get("nid"), p.get("post_url"),
            p.get("date"), p.get("title_ko"), p.get("author"),
            p.get("views"), p.get("likes"), p.get("dislikes"), p.get("comments")
        ))
    conn.commit()
    conn.close()


def db_get_detail(code: str, dedupe_key: str) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("""
        SELECT code, dedupe_key, nid, post_url, title_ko, content_ko
        FROM forum_post_details
        WHERE code=? AND dedupe_key=?
        LIMIT 1
    """, (code, dedupe_key))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def db_upsert_detail(code: str, dedupe_key: str, nid: Optional[str], post_url: str, title_ko: str, content_ko: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO forum_post_details
    (code, dedupe_key, nid, post_url, title_ko, content_ko)
    VALUES (?, ?, ?, ?, ?, ?)
    ON CONFLICT(code, dedupe_key) DO UPDATE SET
      nid=excluded.nid,
      post_url=excluded.post_url,
      title_ko=excluded.title_ko,
      content_ko=excluded.content_ko,
      fetched_at=CURRENT_TIMESTAMP
    """, (code, dedupe_key, nid, post_url, title_ko, content_ko))
    conn.commit()
    conn.close()

# =========================
# 抓列表页
# =========================
def scrape_board_page(code: str, page: int) -> Tuple[List[dict], List[int], Optional[int]]:
    cache_key = f"{code}:{page}"
    if cache_key in page_cache:
        return page_cache[cache_key]

    params = {"code": code, "page": page}
    try:
        resp = session.get(BASE_URL, params=params, timeout=20)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"请求目标站失败: {str(e)}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"目标站返回状态码: {resp.status_code}")

    if not resp.encoding or resp.encoding.lower() in ("iso-8859-1", "ascii"):
        resp.encoding = resp.apparent_encoding

    soup = BeautifulSoup(resp.text, "html.parser")

    posts = []
    rows = soup.select("tr[onmouseover]")
    for row in rows:
        tds = row.find_all("td")
        if len(tds) < 6:
            continue

        date_text = clean_text(tds[0].get_text(" ", strip=True))

        title_a = tds[1].select_one("a[href*='board_read']")
        if not title_a:
            continue

        title_ko = clean_text(title_a.get("title") or title_a.get_text(" ", strip=True))
        href = title_a.get("href", "")
        post_url = urljoin(BASE_DOMAIN, href) if href else ""
        nid = extract_nid_from_href(href)

        cmt_b = tds[1].select_one("span.tah.p9 b")
        comments = to_int(cmt_b.get_text(strip=True) if cmt_b else "0")

        author = clean_text(" ".join(tds[2].stripped_strings))
        views = to_int(tds[3].get_text(" ", strip=True))
        likes = to_int(tds[4].get_text(" ", strip=True))
        dislikes = to_int(tds[5].get_text(" ", strip=True))

        dedupe_key = build_dedupe_key(code, nid, post_url, title_ko, date_text)

        posts.append({
            "dedupe_key": dedupe_key,
            "date": date_text,
            "title_ko": title_ko,
            "author": author,
            "views": views,
            "likes": likes,
            "dislikes": dislikes,
            "comments": comments,
            "nid": nid,
            "post_url": post_url
        })

    posts = dedupe_posts(posts)

    page_links = soup.select("a[href*='/item/board.nhn?'][href*='page=']")
    available_pages = sorted({
        p for p in (extract_page_from_href(a.get("href", "")) for a in page_links) if p
    })

    next_group_page = None
    for a in page_links:
        if "다음" in a.get_text(" ", strip=True):
            next_group_page = extract_page_from_href(a.get("href", ""))
            break

    result = (posts, available_pages, next_group_page)
    page_cache[cache_key] = result
    return result

# =========================
# 抓详情页（含 iframe / NEXT_DATA）
# =========================
def _fetch_iframe_content(iframe_url: str, referer: str) -> str:
    headers = {
        "User-Agent": HEADERS["User-Agent"],
        "Referer": referer,
        "Accept-Language": "ko-KR,ko;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        r = session.get(iframe_url, headers=headers, timeout=20)
    except requests.RequestException:
        return ""

    if r.status_code != 200:
        return ""

    raw = r.content.decode("utf-8", errors="replace")

    # 方案1：__NEXT_DATA__
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            queries = (
                data.get("props", {})
                    .get("pageProps", {})
                    .get("dehydratedState", {})
                    .get("queries", [])
            )
            for q in queries:
                result = q.get("state", {}).get("data", {}).get("result", {})
                content_json_str = result.get("contentJsonSwReplaced", "")
                if not content_json_str:
                    continue
                try:
                    content_obj = json.loads(content_json_str)
                    texts = []
                    components = content_obj.get("document", {}).get("components", [])
                    for comp in components:
                        for paragraph in comp.get("value", []):
                            for node in paragraph.get("nodes", []):
                                val = node.get("value", "")
                                if val and val.strip():
                                    texts.append(val.strip())
                    if texts:
                        return clean_text("\n".join(texts))
                except Exception:
                    pass
        except Exception:
            pass

    # 方案2：se-main-container
    soup = BeautifulSoup(raw, "html.parser")
    node = soup.select_one("div.se-main-container")
    if node:
        paragraphs = []
        for p in node.select("p.se-text-paragraph"):
            txt = clean_text(p.get_text(" ", strip=True))
            if txt:
                paragraphs.append(txt)
        if paragraphs:
            return clean_text("\n".join(paragraphs))
        txt = clean_text(node.get_text("\n", strip=True))
        if len(txt) > 5:
            return txt

    return ""


def scrape_post_detail(code: str, nid: Optional[str], post_url: Optional[str]) -> dict:
    if not post_url:
        if not nid:
            raise HTTPException(status_code=400, detail="nid 和 post_url 不能同时为空")
        post_url = f"{BASE_READ_URL}?code={code}&nid={nid}&page=1"

    cache_key = f"detail:{code}:{nid or post_url}"
    if cache_key in detail_cache:
        return detail_cache[cache_key]

    if not nid:
        nid = extract_nid_from_href(post_url)

    detail_headers = {
        "Referer": f"https://finance.naver.com/item/board.nhn?code={code}",
        "Accept-Language": "ko-KR,ko;q=0.9",
    }

    try:
        resp = session.get(post_url, headers=detail_headers, timeout=20)
    except requests.RequestException as e:
        raise HTTPException(status_code=502, detail=f"详情页请求失败: {str(e)}")

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"详情页状态码异常: {resp.status_code}")

    # 编码兼容
    raw_text = ""
    for enc in (resp.apparent_encoding, "euc-kr", "utf-8"):
        try:
            raw_text = resp.content.decode(enc)
            break
        except Exception:
            continue
    if not raw_text:
        raw_text = resp.text

    soup = BeautifulSoup(raw_text, "html.parser")

    # 标题
    title_ko = ""
    og = soup.select_one("meta[property='og:title']")
    if og:
        title_ko = clean_text(og.get("content", ""))
    if not title_ko:
        for sel in ["strong.c.p15", "div.view_top strong", "td.title strong", "span.tah.p15", "h3", "h4"]:
            node = soup.select_one(sel)
            if node:
                t = clean_text(node.get_text(" ", strip=True))
                if t:
                    title_ko = t
                    break

    # 正文：先 iframe，再主页面
    content_ko = ""

    iframe_src = ""
    iframe_node = soup.select_one("iframe#contents, iframe[name='contents']")
    if iframe_node:
        iframe_src = iframe_node.get("src", "").strip()

    if not iframe_src and nid:
        iframe_src = f"https://m.stock.naver.com/pc/domestic/stock/{code}/discussion/{nid}"

    if iframe_src:
        iframe_url = urljoin(post_url, iframe_src)
        content_ko = _fetch_iframe_content(iframe_url, referer=post_url)

    if not content_ko:
        for sel in ["td.view_se", "div.view_se", "td#body", "div#body", "div.se-main-container", "div.post_ct"]:
            node = soup.select_one(sel)
            if node:
                for tag in node.select("script, style, noscript, iframe"):
                    tag.decompose()
                txt = clean_text(node.get_text("\n", strip=True))
                if len(txt) > 5:
                    content_ko = txt
                    break

    content_ko = clean_text(content_ko)
    if not title_ko:
        title_ko = "（无标题）"
    if not content_ko:
        content_ko = "（正文为空）"

    data = {
        "code": code,
        "nid": nid,
        "post_url": post_url,
        "title_ko": title_ko,
        "content_ko": content_ko
    }
    detail_cache[cache_key] = data
    return data

# =========================
# API
# =========================
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "db": DB_PATH,
        "proxy": proxy_url or "disabled",
        "translator": TRANSLATE_API_URL
    }


@app.get("/api/posts", response_model=PostsResponse)
def get_posts(
    code: str = Query("000660", description="股票代码"),
    page: int = Query(1, ge=1),
    lang: Literal["ko", "zh", "en"] = Query("ko"),
    save_db: bool = Query(True, description="是否落库")
):
    posts_raw, available_pages, next_group_page = scrape_board_page(code, page)

    if save_db:
        db_upsert_posts(code, posts_raw)

    posts = [
        PostItem(**p, title=translate_ko(p["title_ko"], lang))
        for p in posts_raw
    ]

    return PostsResponse(
        code=code,
        page=page,
        lang=lang,
        available_pages=available_pages,
        next_group_page=next_group_page,
        count=len(posts),
        posts=posts
    )


@app.get("/api/posts/range", response_model=RangeResponse)
def get_posts_range(
    code: str = Query("000660"),
    start_page: int = Query(1, ge=1),
    end_page: int = Query(3, ge=1),
    lang: Literal["ko", "zh", "en"] = Query("ko"),
    save_db: bool = Query(True)
):
    if end_page < start_page:
        raise HTTPException(status_code=400, detail="end_page 必须 >= start_page")
    if end_page - start_page > 20:
        raise HTTPException(status_code=400, detail="单次最多抓取 20 页")

    all_posts = []
    for p in range(start_page, end_page + 1):
        posts_raw, _, _ = scrape_board_page(code, p)
        all_posts.extend(posts_raw)
        time.sleep(random.uniform(0.4, 1.0))

    all_posts = dedupe_posts(all_posts)  # 跨页去重

    if save_db:
        db_upsert_posts(code, all_posts)

    ret = [
        PostItem(**row, title=translate_ko(row["title_ko"], lang))
        for row in all_posts
    ]

    return RangeResponse(
        code=code,
        start_page=start_page,
        end_page=end_page,
        lang=lang,
        count=len(ret),
        posts=ret
    )


@app.get("/api/post/detail", response_model=PostDetailResponse)
def get_post_detail(
    code: str = Query(...),
    nid: Optional[str] = Query(None),
    post_url: Optional[str] = Query(None),
    lang: Literal["ko", "zh", "en"] = Query("ko"),
    force_refresh: bool = Query(False),
    save_db: bool = Query(True)
):
    temp_key = build_dedupe_key(code, nid, post_url, "", "")

    # 先查库
    if not force_refresh:
        db_data = db_get_detail(code, temp_key)
        if db_data:
            return PostDetailResponse(
                code=code,
                nid=db_data.get("nid"),
                post_url=db_data.get("post_url"),
                title_ko=db_data.get("title_ko", ""),
                title=translate_ko(db_data.get("title_ko", ""), lang),
                content_ko=db_data.get("content_ko", ""),
                content=translate_ko(db_data.get("content_ko", ""), lang)
            )

    # 实时抓取
    detail = scrape_post_detail(code, nid, post_url)
    final_key = build_dedupe_key(
        code, detail.get("nid"), detail.get("post_url"), detail.get("title_ko", ""), ""
    )

    if save_db:
        db_upsert_detail(
            code=code,
            dedupe_key=final_key,
            nid=detail.get("nid"),
            post_url=detail.get("post_url"),
            title_ko=detail.get("title_ko", ""),
            content_ko=detail.get("content_ko", "")
        )

    return PostDetailResponse(
        code=code,
        nid=detail.get("nid"),
        post_url=detail.get("post_url"),
        title_ko=detail.get("title_ko", ""),
        title=translate_ko(detail.get("title_ko", ""), lang),
        content_ko=detail.get("content_ko", ""),
        content=translate_ko(detail.get("content_ko", ""), lang)
    )


# 挂载前端静态页
if os.path.isdir("frontend"):
    app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
