"""获取 Product Hunt 某天（洛杉矶时区）的 Top N，并抓取官网正文。"""
import os
import re
import time
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from readability import Document

from common import ROOT, log, read_json, redact, write_json

API = "https://api.producthunt.com/v2/api/graphql"
FEED = "https://www.producthunt.com/feed"
UA = {"User-Agent": "ph-daily-digest/1.0 (personal study project)"}

POSTS_QUERY = """query($a:DateTime!,$b:DateTime!,$n:Int!){
 posts(first:$n, order:RANKING, featured:true, postedAfter:$a, postedBefore:$b){nodes{
  id name slug tagline description votesCount commentsCount dailyRank featuredAt
  website url userId makers{id} topics(first:8){nodes{name}}
  comments(first:20, order:VOTES_COUNT){nodes{userId body} pageInfo{endCursor hasNextPage}}}}}"""
COMMENTS_QUERY = """query($id:ID!,$c:String){post(id:$id){
 comments(first:20, order:VOTES_COUNT, after:$c){nodes{userId body} pageInfo{endCursor hasNextPage}}}}"""
DOMAIN_RE = re.compile(
    r"(?<![\w@./-])((?:[a-z0-9-]+\.)+(?:com|ai|io|app|dev|co|so|tech|net|org|xyz|me))\b", re.I)


def clean_url(url):
    """去掉 utm_* 等追踪参数（里面含有 API 应用名）。"""
    if not url:
        return ""
    s = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(s.query) if not k.startswith("utm_")]
    return urlunsplit((s.scheme, s.netloc, s.path, urlencode(query), ""))


def html_text(html):
    return re.sub(r"\s+", " ", BeautifulSoup(html or "", "html.parser").get_text(" ")).strip()


def graphql(query, variables, retries=2):
    token = os.environ.get("PH_TOKEN", "").strip()
    if not token:
        raise RuntimeError("环境变量 PH_TOKEN 未设置")
    for attempt in range(retries + 1):
        try:
            r = requests.post(API, json={"query": query, "variables": variables}, timeout=30,
                              headers={**UA, "Authorization": f"Bearer {token}"})
            r.raise_for_status()
            body = r.json()
            if body.get("errors"):
                raise RuntimeError(f"GraphQL 错误: {body['errors']}")
            return body["data"]
        except Exception:
            if attempt == retries:
                raise
            time.sleep(2 * 2 ** attempt)


def day_window(date, tz):
    start = datetime.combine(date, datetime.min.time(), ZoneInfo(tz))
    # 用“次日零点”而不是 +24h，换夏令时那天也正确
    end = datetime.combine(date + timedelta(days=1), datetime.min.time(), ZoneInfo(tz))
    return start, end


def maker_comments(post, cfg):
    makers = {post["userId"], *(m["id"] for m in post["makers"])}
    conn, found, html = post["comments"], [], []
    for page in range(cfg["max_comment_pages"]):
        for c in conn["nodes"]:
            if c["userId"] in makers and len(found) < cfg["max_maker_comments"]:
                html.append(c["body"])
                found.append(html_text(c["body"])[:3000])
        # 已找到就不再翻页，省 API 额度
        if found or not conn["pageInfo"]["hasNextPage"] or page + 1 == cfg["max_comment_pages"]:
            break
        try:
            conn = graphql(COMMENTS_QUERY, {"id": post["id"], "c": conn["pageInfo"]["endCursor"]}
                           )["post"]["comments"]
        except Exception as e:
            log.warning("%s 评论翻页失败: %s", post["name"], redact(e))
            break
    return found, html


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def candidate_sites(name, slug, html_list, text):
    """从 API 返回的 maker 评论/描述里找官网链接，域名必须与产品名吻合，不瞎猜域名。"""
    urls = re.findall(r'href="(https?://[^"]+)"', " ".join(html_list))
    blob = " ".join([text, *map(html_text, html_list)])
    urls += re.findall(r"https?://[^\s<>\"'）)\]]+", blob)
    urls += ["https://" + d for d in DOMAIN_RE.findall(blob)]
    keys = {_norm(name), _norm(slug)} | {_norm(w) for w in name.split() if len(_norm(w)) >= 5}
    out = []
    for u in urls:
        host = (urlsplit(u).hostname or "").removeprefix("www.")
        if not host or host.endswith("producthunt.com"):
            continue
        label = _norm(host.rsplit(".", 1)[0])
        if any(len(k) >= 4 and (k in _norm(host) or (len(label) >= 5 and label in k)) for k in keys):
            root = f"https://{host}/"
            if root not in out:
                out.append(root)
    return out[:2]


def fetch_site(url, cfg):
    r = requests.get(url, timeout=cfg["website_timeout"], headers=UA)
    if r.headers.get("cf-mitigated") or r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}")
    if "html" not in r.headers.get("content-type", ""):
        raise RuntimeError("不是 HTML 页面")
    r.encoding = r.apparent_encoding or r.encoding
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript", "svg"]):
        t.decompose()
    full = html_text(str(soup))
    article = html_text(Document(r.text).summary())
    # 落地页不是文章，readability 常只抓到一小块，此时用全文
    text = article if len(article) > 1500 else full
    if len(text) < 100:
        raise RuntimeError("正文过短（可能靠 JavaScript 渲染）")
    return r.url, text[:cfg["website_max_chars"]]


def add_website(p, cfg, html):
    p.update(website_resolved="", website_text="", website_status="")
    errors = []
    for u in [p["website"], *candidate_sites(p["name"], p["slug"], html, p["description"])]:
        try:
            final, p["website_text"] = fetch_site(u, cfg)
            p["website_resolved"], p["website_status"] = clean_url(final), "ok"
            return
        except Exception as e:
            errors.append(f"{urlsplit(u).hostname}: {redact(e)[:120]}")
    p["website_status"] = "跳过：" + "；".join(errors)
    log.info("官网抓取跳过 %s: %s", p["name"], p["website_status"])


def fetch_api(date, cfg):
    start, end = day_window(date, cfg["timezone"])
    # postedBefore 是闭区间：若传次日零点，会混进次日 00:00 发布的产品，所以减 1 秒
    data = graphql(POSTS_QUERY, {"a": start.isoformat(), "n": 20,
                                 "b": (end - timedelta(seconds=1)).isoformat()})
    nodes = [n for n in data["posts"]["nodes"] if n["featuredAt"] and
             start <= datetime.fromisoformat(n["featuredAt"].replace("Z", "+00:00")) < end]
    if not nodes:
        raise RuntimeError("API 没有返回该日的产品")
    nodes.sort(key=lambda n: n["dailyRank"] or 10 ** 6)
    products = []
    for i, n in enumerate(nodes[:cfg["top_n"]], 1):
        comments, html = maker_comments(n, cfg)
        p = {"rank": i, "daily_rank": n["dailyRank"], "id": n["id"], "name": n["name"],
             "slug": n["slug"], "tagline": n["tagline"], "description": n["description"] or "",
             "votes": n["votesCount"], "comments": n["commentsCount"],
             "topics": [t["name"] for t in n["topics"]["nodes"]],
             "website": clean_url(n["website"]), "url": clean_url(n["url"]),
             "maker_comments": comments}
        add_website(p, cfg, html)
        products.append(p)
    return products


def fetch_rss(date, cfg):
    r = requests.get(FEED, timeout=20, headers=UA)
    r.raise_for_status()
    start, end = day_window(date, cfg["timezone"])
    products = []
    for e in BeautifulSoup(r.content, "xml").find_all("entry"):
        if not start <= datetime.fromisoformat(e.published.text) < end:
            continue
        content = BeautifulSoup(e.find("content").text, "html.parser")
        first = content.find("p")
        link = next((a["href"] for a in content.find_all("a") if a.text.strip() == "Link"), "")
        url = e.link["href"]
        p = {"rank": len(products) + 1, "daily_rank": None, "id": e.id.text.rsplit("/", 1)[-1],
             "name": e.title.text.strip(), "slug": url.rsplit("/", 1)[-1],
             "tagline": html_text(str(first)) if first else "", "description": "", "votes": None,
             "comments": None, "topics": [], "website": clean_url(link), "url": clean_url(url),
             "maker_comments": []}
        add_website(p, cfg, [])
        products.append(p)
        if len(products) == cfg["top_n"]:
            break
    if not products:
        raise RuntimeError("RSS 里没有该日的产品（RSS 只含最近的产品）")
    return products


def get_day(date, cfg):
    path = ROOT / "data" / "raw" / f"{date}.json"
    cached = read_json(path)
    if cached and cached["source"] == "api":   # RSS 的不完整数据不算缓存命中
        log.info("使用缓存 %s", path.name)
        return cached
    note = ""
    try:
        source, products = "api", fetch_api(date, cfg)
    except Exception as e:
        log.warning("API 不可用，改用 RSS: %s", redact(e))
        source, products = "rss", fetch_rss(date, cfg)
        note = ("Product Hunt API 不可用，本报告改用官方 RSS：没有票数、评论和最终排名，"
                "顺序不代表名次，数据不完整。")
    if len(products) < cfg["top_n"]:
        note += f"当天只取到 {len(products)} 个产品。"
    day = {"date": str(date), "source": source, "note": note,
           "fetched_at": datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds"),
           "products": products}
    write_json(path, day)
    return day
