"""把结构化结果渲染成 Markdown。"""
import re

import qa
from common import ROOT, write_text

STARS = {n: "★" * n + "☆" * (5 - n) for n in range(1, 6)}


def cell(s):
    return re.sub(r"\s+", " ", str(s)).replace("|", "\\|")


def _votes(p):
    return "—" if p["votes"] is None else p["votes"]


def _links(p):
    site = p["website_resolved"] or p["website"]
    return f"[Product Hunt]({p['url']}) · [官网]({site})"


def _row(p, r):
    one, diff, feas, score = cell(p["tagline"]), "—", "—", "—"
    if r.get("again"):
        one = f"再次上榜，见 {r['again']['date']} 报告"
    elif r.get("error"):
        one = "（分析失败）" + one
    else:
        f, q = r["qa"]["fields"], r["qa"]
        one = cell(f["one_liner"]) or one
        diff = STARS[f["difficulty"]] if f["difficulty"] else "—"
        feas = f"{f['feasibility']}/5" if f["feasibility"] else "—"
        score = f"{'未审' if q['score'] is None else q['score']}{'' if q['passed'] else ' ⚠️'}"
    return f"| {p['rank']} | [{cell(p['name'])}](#p-{p['id']}) | {one} | {_votes(p)} | {diff} | {feas} | {score} |"


def _product(p, r):
    head = [f'<a id="p-{p["id"]}"></a>', "", f"## {p['rank']}. {p['name']}", "",
            f"▲ {_votes(p)} 票 · 💬 {'—' if p['comments'] is None else p['comments']} 条评论 · {_links(p)}", ""]
    if p["website_status"] != "ok":
        head += ["> 官网正文未能抓取，以下分析只依据 Product Hunt 上的资料。", ""]
    if r.get("again"):
        a = r["again"]
        return head + [f"**再次上榜**：本次第 {p['rank']} 名，{_votes(p)} 票。完整分析见 "
                       f"[{a['date']} 的报告]({a['date']}.md#p-{p['id']})。", ""]
    if r.get("error"):
        return head + ["⚠️ 本条分析失败，已跳过。", ""]
    q = r["qa"]
    if not q["passed"]:
        score = "未审" if q["score"] is None else q["score"]
        todo = q["issues"] + q["must_fix"] + ([f"审稿未完成：{q['review_error']}"] if q["review_error"] else [])
        head += [f"> **⚠️ 本条未通过质检（得分 {score}），以下问题请自行核实：**"]
        head += [f"> - {cell(x)}" for x in todo or q["unclear"] or ["审稿总分低于阈值"]] + [""]
    body = qa.annotate_links(r["text"], q["links"])
    # 产品标题是二级，正文小节降为三级，目录层次才正确
    return head + [re.sub(r"^## ", "### ", body, flags=re.M), ""]


def render(day, results, over, stats):
    by_rank = {p["rank"]: p for p in day["products"]}
    n = len(by_rank)
    out = [f"# Product Hunt 每日 Top {n} 解读 · {day['date']}", "",
           f"> 日期按洛杉矶时区；票数抓取于 {day['fetched_at']}（UTC）。"
           "`[已知]` 表示资料里有依据，`[推测]` 表示模型推断。"]
    if day["note"]:
        out += [f">\n> **⚠️ {day['note']}**"]
    if stats.get("incomplete"):
        out += [f">\n> **⚠️ 运行中 DeepSeek {stats['incomplete']}，本报告不完整，请重跑。**"]
    out += ["", "## 今日一览", "",
            "| 排名 | 产品名 | 一句话 | 票数 | 复现难度 | 项目可行性 | 质检分 |",
            "|---|---|---|---|---|---|---|"]
    out += [_row(p, results[p["id"]]) for p in day["products"]]
    out += ["", "## 今日趋势", ""]
    out += [f"- **{t['label']}**：{n} 个里有 {len(t['ranks'])} 个（"
            + "、".join(by_rank[r]["name"] for r in t["ranks"]) + f"）。{t['insight']}"
            for t in over["trends"]] or ["- 今日未能归纳出趋势。"]
    out += ["", "## 今日最值得我动手的 1 个项目", ""]
    if over["pick"]:
        p = by_rank[over["pick"]["rank"]]
        out += [f"**来自 [{p['name']}](#p-{p['id']})**：{over['pick']['reason']}"]
    else:
        out += ["今天没有通过质检的产品，不做推荐。"]
    out += ["", "---", ""]
    for p in day["products"]:
        out += _product(p, results[p["id"]]) + ["---", ""]
    out += ["## 质检摘要", "",
            "| 产品 | 审稿分 | 修订次数 | 失效链接 | 未解释术语 | 自动补解释 | 自动改标 | 结果 |", "|---|---|---|---|---|---|---|---|"]
    remarks = []
    for p in day["products"]:
        r = results[p["id"]]
        if not r.get("text"):
            out.append(f"| {cell(p['name'])} | — | — | — | — | — | — | {'再次上榜' if r.get('again') else '分析失败'} |")
            continue
        q = r["qa"]
        out.append(f"| {cell(p['name'])} | {'未审' if q['score'] is None else q['score']} | {q['revisions']} "
                   f"| {len(q['dead_links'])} | {len(q['unexplained_terms'])} | {len(q['terms_autofixed'])} | {q['relabel_applied']} "
                   f"| {'通过' if q['passed'] else '⚠️ 未通过'} |")
        # 分数够了但审稿仍有意见的，也公开给读者
        left = q["must_fix"] + [f"应视为 [推测]：{x}" for x in q["relabel_missed"]] if q["passed"] else []
        remarks += [f"- {p['name']}：{cell(x)}" for x in left]
    if remarks:
        out += ["", "**通过质检但审稿仍有保留意见：**", ""] + remarks
    out += ["", f"质检通过率 {stats['passed']}/{stats['analyzed']} · token {stats['tokens']:,} · "
            f"预估费用 ${stats['usd']:.4f} · 耗时 {stats['seconds']:.0f} 秒", ""]
    return "\n".join(out)


def write_index():
    files = sorted((f for f in (ROOT / "reports").glob("*.md") if re.fullmatch(r"\d{4}-\d\d-\d\d", f.stem)),
                   reverse=True)
    write_text(ROOT / "reports" / "index.md",
               "# 全部报告\n\n" + "\n".join(f"- [{f.stem}]({f.name})" for f in files) + "\n")
