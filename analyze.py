"""单产品分析流水线（写 → 质检 → 修订一次）和每日总览。"""
import json

import qa
from common import ROOT, fill, log, redact

METHODS = ["对照比较与假设检验", "转化漏斗与分群", "留存/流失预测", "时间序列预测", "文本分类", "聚类分群",
           "回归与因素解释", "异常检测", "推荐或检索排序", "网络/关系分析", "耗时与流程分析"]
FACT_KEYS = ("name", "tagline", "description", "topics", "website_resolved")


def material_of(p):
    return {"product_json": json.dumps({k: p[k] for k in FACT_KEYS}, ensure_ascii=False, indent=1),
            "maker_comments": "\n---\n".join(p["maker_comments"]) or "（没有）",
            "website_text": p["website_text"] or "（未能抓取官网，资料较少，更要多标 [推测]）"}


def load_datasets():
    rows = []
    for line in (ROOT / "datasets.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            url, _, fields = line.partition("|")
            rows.append((url.strip(), fields.strip()))
    return rows


def plan_projects(llm, products, used, key):
    """先统一分配“数据集 + 方法”，避免各产品各写各的、最后雷同。返回 {post_id: plan}"""
    if not products:
        return {}
    earlier = sorted((k for k in used if k != key), reverse=True)
    avoid = set(used[earlier[0]]) if earlier else set()      # 与上一期不重复
    datasets = {u: f for u, f in load_datasets() if u not in avoid}
    items = [{k: p[k] for k in ("rank", "name", "tagline", "description", "topics")} for p in products]
    ranks = {p["rank"] for p in products}

    def check(o):
        plans = o["plans"]
        urls, methods = [x["dataset_url"] for x in plans], [x["method"] for x in plans]
        if {x["rank"] for x in plans} != ranks or len(plans) != len(ranks):
            raise ValueError("产品没有一一对应")
        if not set(urls) <= set(datasets) or len(set(urls)) != len(urls):
            raise ValueError("数据集重复或不在清单里")
        if not set(methods) <= set(METHODS) or max(methods.count(m) for m in methods) > 2:
            raise ValueError("方法不合法或重复过多")
        if not all(x["question"] and x["relevance"] for x in plans):
            raise ValueError("缺少 question 或 relevance")

    prompt = fill("plan.md", methods="、".join(METHODS), items=json.dumps(items, ensure_ascii=False),
                  datasets="\n".join(f"- {u} ：{f}" for u, f in datasets.items()))
    try:
        o = llm.call(prompt, role="review", as_json=True, check=check)
    except Exception as e:
        log.warning("选题分配失败，各产品自行选题: %s", redact(e)[:200])
        return {}
    by_rank = {p["rank"]: p["id"] for p in products}
    return {by_rank[x["rank"]]: {**x, "fields": datasets[x["dataset_url"]]} for x in o["plans"]}


def plan_text(plan):
    if not plan:
        return "（未指定，请自行选一个确定存在、且与产品问题结构相近的具体公开数据集）"
    return (f"- 数据集：{plan['dataset_url']}\n- 该数据真实内容：{plan['fields']}\n- 主要方法：{plan['method']}\n"
            f"- 要回答的问题：{plan['question']}\n- 与产品的联系：{plan['relevance']}")


def _inspect(llm, text, product, material, cfg, checker, skip_review, plan=None):
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
    out.update(qa.program_check(text, product, cfg["qa"], checker, plan))
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


def analyze_product(llm, product, cfg, checker, skip_review=False, plan=None):
    material = {**material_of(product), "plan": plan_text(plan)}
    glossary = "\n".join(f"- {t}：{m}" for t, m in qa.load_glossary())
    task = fill("product_analysis.md", glossary=glossary, **material)
    text = qa.normalize(llm.call(task))
    text, q = _inspect(llm, text, product, material, cfg, checker, skip_review, plan)
    first, revisions = None, 0
    low = q["score"] is not None and q["score"] < cfg["qa"]["review_threshold"]
    if q["issues"] or low:
        first = {k: q[k] for k in ("score", "issues", "must_fix", "unclear", "relabel")}
        try:
            new = qa.normalize(llm.call(fill("revise.md", task=task, draft=text,
                                             issues="\n".join(f"- {x}" for x in issue_list(q)))))
            text, q = _inspect(llm, new, product, material, cfg, checker, skip_review, plan)
            revisions = 1
        except Exception as e:
            log.warning("修订失败，保留初稿 %s: %s", product["name"], redact(e)[:200])
    q.update(passed=is_passed(q, cfg, skip_review), revisions=revisions, first_draft=first, plan=plan,
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
