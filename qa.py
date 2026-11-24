"""质量自检：4.1 程序化检查 + 4.2 LLM 审稿 + relabel 替换。"""
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

from common import ROOT, fill

SECTIONS = ["① 一句话说清楚", "② 它解决了什么痛点", "③ 它是怎么做到的", "④ 对我的启发",
            "⑤ 可复现性评估", "⑥ 基于这个灵感的个人项目（STAR 法则）"]
STAR = ["S (Situation)", "T (Task)", "A (Action)", "R (Result)"]
URL_RE = re.compile(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")
DONE_RE = re.compile(r"实现了|达到了|提升了|降低了|减少了|提高了|增加了|缩短了|完成了|取得了|节省了|已(?:经)?(?:实现|达到|完成|上线)")
UA = {"User-Agent": "Mozilla/5.0 (compatible; ph-daily-digest/1.0)"}


# ---------- 解析 ----------
def normalize(text):
    """去掉代码围栏和标题前的前言；固定字段里的全角括号统一成半角。"""
    text = re.sub(r"^```(?:markdown)?\s*\n|\n```\s*$", "", text.strip())
    # 标注统一：[已知：xx] -> xx [已知]；同时含两者的混合标注按“拿不准”算 [推测]
    def label(m):
        kind = "推测" if "推测" in m[0] else "已知"
        extra = re.sub(r"^(已知|推测)[：:，, ]*", "", m[1])
        return f"（{extra}）[{kind}]" if extra else f"[{kind}]"
    text = re.sub(r"[\[【]((?:已知|推测)[^\]】\n]*)[\]】](?!\()", label, text)
    start = text.find("## ①")
    return (text[start:] if start > 0 else text).strip()


def sections(text):
    parts = re.split(r"^## +(.+?)\s*$", text, flags=re.M)
    return list(zip(parts[1::2], (p.strip() for p in parts[2::2])))


def section(text, mark):
    return next((body for title, body in sections(text) if title.startswith(mark)), "")


def star_part(text, letter):
    body = section(text, "⑥")
    m = re.search(rf"^\s*(?:[-*]\s+)?(?:\*\*)?{letter} ?[（(]\w+[）)](.*?)"
                  r"(?=^\s*(?:[-*]\s+)?(?:\*\*)?(?:[STAR] ?[（(]|可行性打分|两周|面试)|\Z)",
                  body, re.S | re.M)
    return m[1].lstrip("*：: ").strip() if m else ""


def parse_fields(text):
    plain = text.replace("*", "").replace("`", "").replace("（", "(").replace("）", ")")
    d = re.search(r"复现难度[：:]\s*([★☆]{5})\s*\(([1-5])/5\)", plain)
    f = re.search(r"项目可行性[：:]\s*([1-5])/5", plain)
    return {"difficulty": int(d[2]) if d and d[1].count("★") == int(d[2]) else None,
            "feasibility": int(f[1]) if f else None,
            "one_liner": re.sub(r"\s+", " ", section(text, "①")).strip("- 。")}


def han_count(text):
    return len(re.findall(r"[一-鿿]", text))


def word_count(text):
    """汉字每个算 1；一个英文单词/数字也算 1。"""
    return len(re.findall(r"[一-鿿]|[A-Za-z0-9][A-Za-z0-9.'&+-]*", text))


# ---------- 单项检查，均返回问题字符串列表 ----------
def check_structure(text):
    titles = [t for t, _ in sections(text)]
    out = [f"缺少标题：## {s}" for s in SECTIONS if s not in titles]
    out += [f"多余标题：## {t}" for t in titles if t not in SECTIONS]
    if not out and titles != SECTIONS:
        out.append("二级标题顺序错误或重复")
    out += [f"⑥ 缺少或没写内容：{s}" for s in STAR if not star_part(text, s[0])]
    return out


def check_length(text, cfg):
    out, n = [], han_count(text)
    if not cfg["min_chars"] <= n <= cfg["max_chars"]:
        out.append(f"全文 {n} 个汉字，应在 {cfg['min_chars']}–{cfg['max_chars']} 之间")
    one = word_count(section(text, "①"))
    if not 0 < one <= cfg["one_liner_max"]:
        out.append(f"① 有 {one} 字，应为 1–{cfg['one_liner_max']} 字")
    return out


def check_labels(text):
    lines = section(text, "③").splitlines()
    start = next((i for i, l in enumerate(lines) if "技术栈" in l), None)
    if start is None:
        return ["③ 没有“推测的技术栈”"]
    indent = lambda l: len(l) - len(l.lstrip())
    items = []
    for l in lines[start + 1:]:
        if not l.strip():
            continue
        if "壁垒" in l and indent(l) <= indent(lines[start]):
            break
        items.append(l)
    if not items and re.search(r"技术栈.*?[：:]\s*\S", lines[start]):
        items = [lines[start]]
    if not items:
        return ["③ 技术栈没有列出条目"]
    # 每一条（含同一行里用分号隔开的情况，至少整行）都要有标注
    return [f"③ 技术栈缺少标注：{l.strip()[:60]}" for l in items if not re.search(r"\[(已知|推测)\]", l)]


def check_fields(text):
    f, out = parse_fields(text), []
    if f["difficulty"] is None:
        out.append("无法解析复现难度，格式须为：复现难度：★★★☆☆ (3/5)")
    if f["feasibility"] is None:
        out.append("无法解析项目可行性，格式须为：项目可行性：4/5")
    return out


def check_data(text, product):
    out = []
    body = text.split("## ⑥")[0]
    for m in re.finditer(r"(\d[\d,]*)\s*(?:张)?(?:票|个?投票|upvotes?|votes?)(?!制)", body, re.I):
        if int(m[1].replace(",", "")) != product["votes"]:
            out.append(f"票数与原始数据不符：“{m[0]}”，应为 {product['votes']}")
    for m in re.finditer(r"(?:今日|当日|日榜|周榜|本周|榜单|Product Hunt|PH)[^。\n]{0,10}第\s*(\d+)", body):
        if int(m[1]) != product["rank"]:
            out.append(f"排名与原始数据不符：“{m[0]}”，应为第 {product['rank']}")
    return out


def check_result_tense(text):
    r = star_part(text, "R")
    hits = DONE_RE.findall(r)
    if hits and not re.search(r"预期|目标", r):
        return [f"R 部分写成了已完成（{'、'.join(sorted(set(hits)))}），须写明预期目标"]
    return []


CATALOG_RE = re.compile(r"https?://(?:www[.])?(?:huggingface[.]co(?:/datasets)?|kaggle[.]com(?:/datasets)?|archive[.]ics[.]uci[.]edu(?:/datasets)?|github[.]com|zenodo[.]org)/?$")


def check_data_source(text, plan=None):
    part = star_part(text, "A")
    if plan and plan["dataset_url"].rstrip("/") not in [
            u.rstrip("/") for u in extract_urls(section(text, "⑥"))]:
        return [f"⑥ 必须以指定数据集为主数据并写出完整 URL：{plan['dataset_url']}"]
    urls = extract_urls(part)
    if not plan and not [u for u in urls if not CATALOG_RE.match(u)]:
        return ["A 部分没有给出具体数据集/API 的 URL（只有目录首页或没有链接），请选定一个确定存在的具体来源"]
    return []


def load_glossary():
    rows = []
    for line in (ROOT / "glossary.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            term, _, meaning = line.partition("|")
            rows.append((term.strip(), meaning.strip()))
    return rows


def _find_term(term, body):
    if re.search(r"[A-Za-z]", term):
        flags = 0 if term.isupper() else re.I
        return re.search(rf"(?<![A-Za-z0-9-]){re.escape(term)}s?(?![A-Za-z0-9])", body, flags)
    # “逻辑回归”是另一个词条，不能套用“回归”的解释
    prefix = "(?<!逻辑)" if term == "回归" else ""
    return re.search(prefix + re.escape(term), body)


def unexplained_terms(text, product_name="", window=50, glossary=None):
    """术语首次出现时，同一行内前后 window 字须有括号解释或冒号解释。"""
    # URL 和产品名里的字样不算术语；等长替换，保持位置不变
    mask = lambda m: " " * len(m[0])
    body = URL_RE.sub(mask, text)
    if product_name:
        body = re.sub(re.escape(product_name), mask, body, flags=re.I)
    out = []
    for term, _ in glossary if glossary is not None else load_glossary():
        m = _find_term(term, body)
        if not m:
            continue
        ls = body.rfind("\n", 0, m.start()) + 1
        le = body.find("\n", m.end())
        le = len(body) if le < 0 else le
        before = body[max(ls, m.start() - window):m.start()]
        after = body[m.end():min(le, m.end() + window)]
        paren = re.search(r"[（(][^）)]*[一-鿿]{2}", after)
        colon = re.match(r"[^。；：:（(]{0,12}[：:][^。]*[一-鿿]{4}", after)
        # 形如“大语言模型（LLM）”“能办事的助手（即 Agent）”
        inside = re.search(r"[一-鿿]{2}[^。]*[（(][^）)]*$", before)
        if not (paren or colon or inside):
            out.append(term)
    return out


def explain_terms(text, product_name="", window=50):
    """未解释的术语由程序在首次出现处补上 glossary 的解释（零成本，省一次修订）。
    ① 有字数限制，不在里面补。返回 (新文本, 补过的术语)。"""
    meanings, fixed = dict(load_glossary()), []
    for term in unexplained_terms(text, product_name, window):
        body = URL_RE.sub(lambda m: " " * len(m[0]), text)
        if product_name:
            body = re.sub(re.escape(product_name), lambda m: " " * len(m[0]), body, flags=re.I)
        m = _find_term(term, body)
        # 避开 ①、标题行、行内代码和链接文字
        if not m or m.start() < text.find("## ②"):
            continue
        ls = text.rfind("\n", 0, m.start()) + 1
        prefix = text[ls:m.start()]
        if prefix.startswith("#") or prefix.count("`") % 2 or prefix.rfind("[") > prefix.rfind("]"):
            continue
        text = text[:m.end()] + f"（{meanings[term]}）" + text[m.end():]
        fixed.append(term)
    return text, fixed


# ---------- 链接 ----------
def extract_urls(text):
    out = []
    for u in URL_RE.findall(text):
        # 去掉句末标点；多出来的右括号属于 Markdown 而不是 URL
        while u and (u[-1] in ".,;:!?'*~&]" or (u[-1] == ")" and u.count("(") < u.count(")"))):
            u = u[:-1]
        if "](" in u:
            u = u.split("](")[0]
        if urlsplit(u).hostname and u not in out:
            out.append(u)
    return out


class LinkChecker:
    """status: ok | dead | blocked（401/403/429：网站在但拒绝程序访问，无法判断真假）"""

    def __init__(self, cfg):
        self.cfg, self.cache, self.lock = cfg, {}, threading.Lock()
        self.pool = ThreadPoolExecutor(min(5, cfg["link_concurrency"]))

    def _http(self, url):
        code, t = None, self.cfg["link_timeout"]
        for method in ("head", "get"):
            try:
                with requests.request(method, url, timeout=t, headers=UA, allow_redirects=True,
                                      stream=True) as r:
                    code = r.status_code
            except requests.RequestException:
                code = None
            if code and code < 400:
                return {"status": "ok", "code": code}
        return {"status": "blocked" if code in (401, 403, 429) else "dead", "code": code or "超时/无法连接"}

    def _github(self, owner, repo):
        headers = {**UA, "Accept": "application/vnd.github+json"}
        if os.environ.get("GITHUB_TOKEN"):
            headers["Authorization"] = f"Bearer {os.environ['GITHUB_TOKEN']}"
        try:
            r = requests.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers,
                             timeout=self.cfg["link_timeout"])
        except requests.RequestException:
            return None
        if r.status_code == 404:
            return {"status": "dead", "code": "GitHub 仓库不存在"}
        if r.status_code != 200:
            return None          # 限流等情况，退回普通 HTTP 检查
        d = r.json()
        pushed = datetime.fromisoformat(d["pushed_at"].replace("Z", "+00:00"))
        return {"status": "ok", "code": 200, "repo": d["full_name"], "stars": d["stargazers_count"],
                "pushed_at": d["pushed_at"][:10],
                "stale": (datetime.now(timezone.utc) - pushed).days > self.cfg["stale_days"]}

    def _check(self, url):
        s = urlsplit(url)
        parts = [p for p in s.path.split("/") if p]
        if s.hostname in ("github.com", "www.github.com") and len(parts) >= 2 and parts[0] not in (
                "topics", "orgs", "search", "features", "marketplace", "collections"):
            gh = self._github(parts[0], parts[1].removesuffix(".git"))
            if gh and (gh["status"] == "dead" or len(parts) == 2):
                return gh
            http = self._http(url)
            return {**gh, **http} if gh else http
        return self._http(url)

    def check(self, urls):
        with self.lock:
            futures = {u: self.cache.setdefault(u, None) or self.pool.submit(self._check, u) for u in urls}
            self.cache.update(futures)
        return {u: f.result() for u, f in futures.items()}


def annotate_links(text, links):
    """渲染前在正文里标记链接状态。"""
    for u in sorted(links, key=len, reverse=True):
        r = links[u]
        if r["status"] == "ok" and not r.get("repo"):
            continue
        if r["status"] == "dead":
            note, strike = " ⚠️ 链接失效，已自动标记", True
        elif r["status"] == "blocked":
            note, strike = f" ⚠️ 网站拒绝程序访问（{r['code']}），请自行确认", False
        else:
            note, strike = f"（⭐ {r['stars']}，最近更新 {r['pushed_at']}" + (
                "，⚠️ 可能已停止维护）" if r["stale"] else "）"), False
        e = re.escape(u)
        # 完整匹配该 URL（后面不能还有 URL 字符），兼容 [文字](url)、`url`、<url> 和裸 url
        pattern = re.compile(rf"\[[^\]\n]*\]\({e}\)|`{e}`|<{e}>|(?<![\w(`<]){e}(?![A-Za-z0-9/_%#?=&-])|{e}(?![A-Za-z0-9/_%#?=&-])")
        text = pattern.sub(lambda m: (f"~~{m[0]}~~" if strike else m[0]) + note, text)
    return text


# ---------- 汇总 ----------
def program_check(text, product, cfg, checker, plan=None):
    links = checker.check(extract_urls(text))
    terms = unexplained_terms(text, product["name"], cfg["term_window"])
    dead = [u for u, r in links.items() if r["status"] == "dead"]
    checks = {
        "structure": check_structure(text), "length": check_length(text, cfg),
        "labels": check_labels(text), "fields": check_fields(text),
        "data": check_data(text, product), "result_tense": check_result_tense(text),
        "data_source": check_data_source(text, plan),
        "links": [f"链接失效（{links[u]['code']}）：{u}" for u in dead],
        "terms": [f"术语首次出现时未解释：{t}" for t in terms]}
    return {"checks": checks, "issues": [i for v in checks.values() for i in v],
            "links": links, "dead_links": dead, "unexplained_terms": terms}


def check_report(report, day):
    """整份报告的一览表必须与 data/raw 完全一致。"""
    out = []
    for p in day["products"]:
        votes = "—" if p["votes"] is None else p["votes"]
        name = p["name"].replace("|", "\\|")
        if not re.search(rf"^\| {p['rank']} \| \[{re.escape(name)}\]\([^)]*\) \|.*\| {votes} \|", report, re.M):
            out.append(f"一览表与原始数据不一致：#{p['rank']} {p['name']}")
    return out


def validate_review(r):
    if not isinstance(r, dict) or type(r.get("score")) is not int or not 0 <= r["score"] <= 100:
        raise ValueError("审稿 JSON 的 score 不合法")
    for k in ("must_fix", "relabel", "unclear"):
        if not isinstance(r.get(k), list):
            raise ValueError(f"审稿 JSON 缺少数组 {k}")


def review(llm, text, material):
    return llm.call(fill("review.md", analysis=text, **material), role="review",
                    as_json=True, check=validate_review)


def apply_relabel(text, items):
    """把审稿指出的 [已知] 换成 [推测]。返回 (新文本, 已替换数, 没找到的引文)。"""
    applied, missed = 0, []
    for it in items:
        quote = (it.get("quote", "") if isinstance(it, dict) else str(it)).strip().strip('“”"`')
        if "[推测]" in quote and "[已知]" not in quote:
            continue             # 本来就是 [推测]，无需改
        core = re.sub(r"`?\[(?:已知|推测)\]`?", "", quote).strip(" -。；;，,")
        # 容忍引文里标注、空白、加粗的差异
        bits = [re.escape(c) for c in core if not c.isspace() and c not in "*`"]
        m = None
        if len(bits) >= 4:
            m = re.search(r"(?:[\s*`]|\[(?:已知|推测)\])*".join(bits), text)
        if not m:
            missed.append(quote)
            continue
        line_end = text.find("\n", m.end())
        line_end = len(text) if line_end < 0 else line_end
        span = text[m.start():m.end()]
        if "[已知]" in span:
            # 引文跨了多句时只换这一段里的标注
            text = text[:m.start()] + span.replace("[已知]", "[推测]") + text[m.end():]
        else:
            nxt = re.search(r"\[(已知|推测)\]", text[m.end():line_end])
            if not nxt or nxt[1] != "已知":
                missed.append(quote)
                continue
            a = m.end() + nxt.start()
            text = text[:a] + "[推测]" + text[a + len(nxt[0]):]
        applied += 1
    return text, applied, missed
