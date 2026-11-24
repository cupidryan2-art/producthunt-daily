"""单产品分析流水线（写 → 质检 → 修订一次）和每日总览。"""
import json

import qa
from common import fill, log, redact

FACT_KEYS = ("name", "tagline", "description", "topics", "website_resolved")


def material_of(p):
    return {"product_json": json.dumps({k: p[k] for k in FACT_KEYS}, ensure_ascii=False, indent=1),
            "maker_comments": "\n---\n".join(p["maker_comments"]) or "（没有）",
            "website_text": p["website_text"] or "（未能抓取官网，资料较少，更要多标 [推测]）"}


def _inspect(llm, text, product, material, cfg, checker, skip_review):
    """跑一遍 4.1 + 4.2。relabel 由程序直接替换。"""
    text, fixed = qa.explain_terms(text, product["name"], cfg["qa"]["term_window"])
    out = {"terms_autofixed": fixed, "score": None, "must_fix": [], "unclear": [], "relabel": [], "relabel_applied": 0,
           "relabel_missed": [], "review_error": ""}
    if not skip_review:
        try:
            r = qa.review(llm, text, material)
            out.update({k: r[k] for k in ("score", "must_fix", "unclear", "relabel")})
            text, out["relabel_applied"], out["relabel_missed"] = qa.apply_relabel(text, r["relabel"])
        except Exception as e:
            out["review_error"] = redact(e)[:200]
            log.warning("审稿失败 %s: %s", product["name"], out["review_error"])
    out.update(qa.program_check(text, product, cfg["qa"], checker))
    return text, out


def is_passed(q, cfg, skip_review):
    if q["issues"]:
        return False
    return skip_review or (q["score"] is not None and q["score"] >= cfg["qa"]["review_threshold"])


def issue_list(q):
    lines = list(q["issues"])
    lines += [f"必须修改：{x}" for x in q["must_fix"]]
    lines += [f"零基础读者看不懂：{x}" for x in q["unclear"]]
    lines += [f"应改标为 [推测]：{x}" for x in q["relabel_missed"]]
    return lines


def analyze_product(llm, product, cfg, checker, skip_review=False):
    material = material_of(product)
    glossary = "\n".join(f"- {t}：{m}" for t, m in qa.load_glossary())
    task = fill("product_analysis.md", glossary=glossary, **material)
    text = qa.normalize(llm.call(task))
    text, q = _inspect(llm, text, product, material, cfg, checker, skip_review)
    first, revisions = None, 0
    low = q["score"] is not None and q["score"] < cfg["qa"]["review_threshold"]
    if q["issues"] or low:
        first = {k: q[k] for k in ("score", "issues", "must_fix", "unclear", "relabel")}
        try:
            new = qa.normalize(llm.call(fill("revise.md", task=task, draft=text,
                                             issues="\n".join(f"- {x}" for x in issue_list(q)))))
            text, q = _inspect(llm, new, product, material, cfg, checker, skip_review)
            revisions = 1
        except Exception as e:
            log.warning("修订失败，保留初稿 %s: %s", product["name"], redact(e)[:200])
    q.update(passed=is_passed(q, cfg, skip_review), revisions=revisions, first_draft=first,
             fields=qa.parse_fields(text))
    log.info("%s：得分 %s，修订 %d 次，%s", product["name"], q["score"], revisions,
             "通过" if q["passed"] else "未通过")
    return {"text": text, "qa": q}


def overview(llm, day, results):
    """LLM 只负责归类和理由，数量由程序数。返回 {"trends": [...], "pick": {...}|None}"""
    items, passed = [], []
    for p in day["products"]:
        r = results[p["id"]]
        item = {"rank": p["rank"], "name": p["name"], "tagline": p["tagline"], "topics": p["topics"],
                "description": p["description"], "passed": False}
        if r.get("text"):
            item.update(passed=r["qa"]["passed"], fields=r["qa"]["fields"],
                        project=qa.section(r["text"], "⑥")[:1200])
            if item["passed"]:
                passed.append(p["rank"])
        items.append(item)
    ranks = {p["rank"] for p in day["products"]}

    def check(o):
        for t in o["trends"]:
            if not t["label"] or not t["insight"] or not set(t["ranks"]) <= ranks:
                raise ValueError("趋势不合法")
        if passed and (o.get("pick") or {}).get("rank") not in passed:
            raise ValueError("pick 不在质检通过的产品里")
        if passed and not o["pick"].get("reason"):
            raise ValueError("pick 缺少理由")

    try:
        o = llm.call(fill("overview.md", items=json.dumps(items, ensure_ascii=False)),
                     as_json=True, check=check)
    except Exception as e:
        log.warning("总览生成失败: %s", redact(e)[:200])
        o = {"trends": [], "pick": None}
        best = max((i for i in items if i["passed"]),
                   key=lambda i: i["fields"]["feasibility"], default=None)
        if best:
            o["pick"] = {"rank": best["rank"], "reason": "总览模型调用失败，按项目可行性分数最高自动选出。"}
    if not passed:
        o["pick"] = None
    for t in o["trends"]:
        t["ranks"] = sorted(set(t["ranks"]))
    o["trends"] = [t for t in o["trends"] if len(t["ranks"]) >= 2][:3]
    return o
