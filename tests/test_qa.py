import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import analyze
import common
import fetch
import qa
import render

CFG = {"min_chars": 1200, "max_chars": 4000, "one_liner_max": 30, "link_timeout": 8,
       "link_concurrency": 5, "stale_days": 730, "term_window": 50, "review_threshold": 75}
PRODUCT = {"id": "1", "name": "Foo", "votes": 100, "rank": 2}
PAD = "这是一段普通的说明文字。" * 130

GOOD = f"""## ① 一句话说清楚
帮你自动整理周报的小工具。

## ② 它解决了什么痛点
- Foo 给要写周报的产品经理用 [已知]
{PAD}

## ③ 它是怎么做到的
- 产品形态：网页 [已知]
- 推测的技术栈：
  - 前端：网页 [推测]
  - 后端：资料未提及 [推测]
- 真正的壁垒是什么：先做出来 [推测]

## ④ 对我的启发
- 思路

## ⑤ 可复现性评估
- 复现难度：★★★☆☆ (3/5)，理由

## ⑥ 基于这个灵感的个人项目（STAR 法则）
- **项目名 + 一句话描述**：周报分析
- **S (Situation)**：背景
- **T (Task)**：任务
- **A (Action)**：做法 https://example.com/data
- **R (Result)**：预期目标：准确率提升到 80%
- **可行性打分**
  - 项目可行性：4/5
- **两周执行计划**：第一周
- **面试可能被追问的 2 个问题**：问题
"""


class FakeChecker:
    def __init__(self, status="ok"):
        self.status = status

    def check(self, urls):
        return {u: {"status": self.status, "code": 404 if self.status == "dead" else 200} for u in urls}


def run(text, status="ok"):
    return qa.program_check(text, PRODUCT, CFG, FakeChecker(status))


def test_good_passes():
    assert run(GOOD)["issues"] == []


# ---- 任务书要求的四种情况 ----
@pytest.mark.parametrize("title", qa.SECTIONS)
def test_missing_heading(title):
    assert f"缺少标题：## {title}" in run(GOOD.replace(f"## {title}\n", ""))["checks"]["structure"]


@pytest.mark.parametrize("verb", ["实现了", "达到了", "提升了"])
def test_result_written_as_done(verb):
    bad = GOOD.replace("预期目标：准确率提升到 80%", f"准确率{verb} 80%")
    assert run(bad)["checks"]["result_tense"]


def test_dead_link():
    q = run(GOOD, "dead")
    assert q["dead_links"] == ["https://example.com/data"] and q["checks"]["links"]
    out = qa.annotate_links(GOOD, q["links"])
    assert "~~https://example.com/data~~ ⚠️ 链接失效，已自动标记" in out


def test_unexplained_term():
    assert "RAG" in run(GOOD.replace("- 思路", "- 用 RAG 查资料"))["unexplained_terms"]


# ---- 其他 ----
@pytest.mark.parametrize("s", ["RAG (检索增强生成) 很常见", "RAG：先查资料再回答", "检索增强生成（RAG）很常见"])
def test_explained_term(s):
    assert "RAG" not in qa.unexplained_terms(s)


def test_term_only_first_occurrence_counts():
    assert "RAG" in qa.unexplained_terms("用 RAG 做\n后面才说 RAG（检索增强生成）")


def test_term_ignores_urls_and_product_name():
    assert qa.unexplained_terms("见 https://a.com/api/API 和 Foo MCP", "Foo MCP") == []


def test_wrong_order_and_missing_star():
    a, b = "## ④ 对我的启发", "## ⑤ 可复现性评估"
    swapped = GOOD.replace(a, "X").replace(b, a).replace("X", b)
    assert run(swapped)["checks"]["structure"]
    assert run(GOOD.replace("- **T (Task)**：任务\n", ""))["checks"]["structure"]


def test_length_and_one_liner():
    assert run(GOOD.replace(PAD, ""))["checks"]["length"]
    assert run(GOOD.replace("帮你自动整理", "帮你自动整理" * 6))["checks"]["length"]


def test_stack_label_missing():
    assert run(GOOD.replace("前端：网页 [推测]", "前端：网页"))["checks"]["labels"]


def test_fields():
    f = qa.parse_fields(GOOD)
    assert (f["difficulty"], f["feasibility"]) == (3, 4)
    assert run(GOOD.replace("(3/5)", "(4/5)"))["checks"]["fields"]   # 星数和数字矛盾


def test_wrong_votes_and_rank():
    assert run(GOOD.replace("- 思路", "- 它有 999 票"))["checks"]["data"]
    assert run(GOOD.replace("- 思路", "- 它有 100 票"))["checks"]["data"] == []
    assert run(GOOD.replace("- 思路", "- 它是日榜第 5"))["checks"]["data"]


def test_extract_urls():
    t = "看[这](https://a.com/x)，https://b.com/y。`https://c.com`（https://d.com/z）**https://e.com/**"
    assert qa.extract_urls(t) == ["https://a.com/x", "https://b.com/y", "https://c.com",
                                  "https://d.com/z", "https://e.com/"]


def test_annotate_does_not_touch_longer_url():
    links = {"https://a.com/x": {"status": "dead", "code": 404}, "https://a.com/xy": {"status": "ok", "code": 200}}
    out = qa.annotate_links("https://a.com/xy 和 [点我](https://a.com/x)", links)
    assert out.startswith("https://a.com/xy 和 ~~[点我](https://a.com/x)~~ ⚠️")


def test_annotate_blocked_is_not_called_dead():
    out = qa.annotate_links("https://a.com/x", {"https://a.com/x": {"status": "blocked", "code": 403}})
    assert "~~" not in out and "403" in out


@pytest.mark.parametrize("code,status", [(200, "ok"), (404, "dead"), (500, "dead"), (403, "blocked")])
def test_link_status(monkeypatch, code, status):
    class R:
        status_code = code
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(qa.requests, "request", lambda *a, **k: R())
    assert qa.LinkChecker(CFG).check(["https://a.com"])["https://a.com"]["status"] == status


def test_link_timeout(monkeypatch):
    def boom(*a, **k):
        raise qa.requests.Timeout()
    monkeypatch.setattr(qa.requests, "request", boom)
    assert qa.LinkChecker(CFG).check(["https://a.com"])["https://a.com"]["status"] == "dead"


def test_github_repo(monkeypatch):
    body = {"full_name": "a/b", "stargazers_count": 7, "pushed_at": "2020-01-01T00:00:00Z"}
    monkeypatch.setattr(qa.requests, "get", lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: body))
    r = qa.LinkChecker(CFG).check(["https://github.com/a/b"])
    assert r["https://github.com/a/b"]["stale"]
    assert "可能已停止维护" in qa.annotate_links("https://github.com/a/b", r)
    monkeypatch.setattr(qa.requests, "get", lambda *a, **k: SimpleNamespace(status_code=404))
    assert qa.LinkChecker(CFG).check(["https://github.com/a/x"])["https://github.com/a/x"]["status"] == "dead"


def test_relabel():
    t = "- 甲用 **网页** [已知]\n- 乙 [已知]"
    new, n, missed = qa.apply_relabel(t, [{"quote": "甲用 网页 [已知]"}, {"quote": "根本不存在的话"}])
    assert new == "- 甲用 **网页** [推测]\n- 乙 [已知]" and n == 1 and missed == ["根本不存在的话"]


def test_relabel_label_after_quote():
    new, n, _ = qa.apply_relabel("前端用网页实现。[已知] 后端未知。[已知]", [{"quote": "前端用网页实现"}])
    assert new == "前端用网页实现。[推测] 后端未知。[已知]" and n == 1


def test_review_json_validation():
    qa.validate_review({"score": 80, "must_fix": [], "relabel": [], "unclear": []})
    for bad in ({"score": 101, "must_fix": [], "relabel": [], "unclear": []}, {"score": 80}, []):
        with pytest.raises(ValueError):
            qa.validate_review(bad)


# ---- 修订流程 ----
class FakeLLM:
    def __init__(self, drafts, scores):
        self.drafts, self.scores = iter(drafts), iter(scores)

    def call(self, prompt, role="analysis", **k):
        if role == "review":
            return {"score": next(self.scores), "must_fix": [], "relabel": [], "unclear": []}
        return next(self.drafts)


FULL = {**PRODUCT, "tagline": "", "description": "", "topics": [], "website_resolved": "",
        "maker_comments": [], "website_text": ""}


@pytest.mark.parametrize("drafts,scores,passed,revs", [
    ([GOOD], [90], True, 0), ([GOOD, GOOD], [50, 80], True, 1),
    ([GOOD, GOOD], [50, 60], False, 1), (["坏", GOOD], [90, 90], True, 1)])
def test_revision_flow(drafts, scores, passed, revs):
    q = analyze.analyze_product(FakeLLM(drafts, scores), FULL, {"qa": CFG}, FakeChecker())["qa"]
    assert (q["passed"], q["revisions"]) == (passed, revs)


def test_review_failure_is_not_pass():
    q = analyze.analyze_product(FakeLLM([GOOD], []), FULL, {"qa": CFG}, FakeChecker())["qa"]
    assert not q["passed"] and q["review_error"] is not None


# ---- 其他模块 ----
def test_day_window_handles_dst():
    start, end = fetch.day_window(datetime(2026, 11, 1).date(), "America/Los_Angeles")
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == 25 * 3600


def test_candidate_site_must_match_name():
    html = ['<a href="https://hydrilla.ai/x">a</a> <a href="https://ollama.com">b</a>']
    assert fetch.candidate_sites("Hydrilla", "hydrilla", html, "") == ["https://hydrilla.ai/"]


def test_redact(monkeypatch):
    monkeypatch.setenv("PH_TOKEN", "abc-secret")
    assert "abc-secret" not in common.redact("x abc-secret y")


def test_fill_does_not_expand_data():
    out = common.fill("review.md", product_json="{analysis}", maker_comments="", website_text="", analysis="ZZ")
    assert out.count("ZZ") == 1


def test_render_matches_raw():
    p = {**FULL, "name": "A|B", "comments": 3, "url": "https://p", "website": "https://w", "website_status": "ok"}
    day = {"date": "2026-01-01", "fetched_at": "t", "note": "", "products": [p]}
    report = render.render(day, {"1": {"again": {"date": "2025-12-01"}}}, {"trends": [], "pick": None},
                           {"passed": 0, "analyzed": 0, "tokens": 0, "usd": 0, "seconds": 0})
    assert qa.check_report(report, day) == []
    assert "2025-12-01.md" in report
    p["votes"] = 101
    assert qa.check_report(report, day)


def test_explain_terms():
    new, fixed = qa.explain_terms(GOOD.replace("- 思路", "- 用 RAG 查资料，见 https://a.com/RAG"))
    assert fixed == ["RAG"] and "用 RAG（检索增强生成" in new and qa.unexplained_terms(new) == []


def test_no_retry_when_out_of_balance():
    from llm import LLM
    class E(Exception):
        status_code = 402
    calls = []
    def create(**k):
        calls.append(k)
        raise E()
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    cfg = common.load_config()
    llm = LLM(cfg["llm"], cfg["pricing"], client)
    with pytest.raises(E):
        llm.call("x", role="review")
    assert len(calls) == 1 and llm.fatal and calls[0]["temperature"] <= 0.5


def test_budget_cap_stops_calls():
    from llm import LLM, BudgetExceeded
    cfg = common.load_config()
    llm = LLM(cfg["llm"], cfg["pricing"], client=object())
    llm.usage["x"]["usd"] = 99
    with pytest.raises(BudgetExceeded):
        llm.call("x")
    assert llm.fatal


def test_normalize_labels():
    t = qa.normalize("```\n## ①\na [已知：官网] b [已知能实现，实现方式推测] c 【推测】\n```")
    assert "（官网）[已知]" in t and t.count("[推测]") == 2


def test_star_without_bold_is_accepted():
    assert qa.check_structure(GOOD.replace("**", "")) == []


def test_relabel_skips_already_guess():
    assert qa.apply_relabel("甲乙丙丁 [推测]", [{"quote": "甲乙丙丁 [推测]"}]) == ("甲乙丙丁 [推测]", 0, [])


def test_logistic_regression_is_its_own_term():
    assert qa.unexplained_terms("用逻辑回归（估计概率的方法）") == []
    assert qa.unexplained_terms("用逻辑回归做") == ["逻辑回归"]


def test_catalog_homepage_is_not_a_data_source():
    assert run(GOOD.replace("https://example.com/data", "https://huggingface.co/datasets"))["checks"]["data_source"]
    assert run(GOOD)["checks"]["data_source"] == []


def test_week_window_is_seven_days():
    start, end = fetch.day_window(datetime(2026, 11, 16).date(), "America/Los_Angeles", 7)
    assert (end.date() - start.date()).days == 7


PLAN = {"rank": 2, "dataset_url": "https://grouplens.org/datasets/movielens/", "method": "聚类分群",
        "question": "q", "relevance": "r"}


class PlanLLM:
    def __init__(self, plans):
        self.plans = plans

    def call(self, prompt, check=None, **k):
        o = {"plans": self.plans}
        check(o)
        return o


def test_plan_assigns_distinct_datasets():
    prods = [{**FULL, "id": "1", "rank": 2}, {**FULL, "id": "2", "rank": 3}]
    other = {**PLAN, "rank": 3, "dataset_url": "https://www.gharchive.org/"}
    ok = analyze.plan_projects(PlanLLM([PLAN, other]), prods, {}, "k")
    assert set(ok) == {"1", "2"} and ok["1"]["fields"]
    assert analyze.plan_projects(PlanLLM([PLAN, {**PLAN, "rank": 3}]), prods, {}, "k") == {}


def test_plan_avoids_previous_issue():
    prods = [{**FULL, "id": "1", "rank": 2}]
    assert analyze.plan_projects(PlanLLM([PLAN]), prods, {"2026-W46": [PLAN["dataset_url"]]}, "2026-W47") == {}


def test_assigned_dataset_must_be_used():
    assert qa.check_data_source(GOOD, PLAN)
    assert qa.check_data_source(GOOD.replace("https://example.com/data", PLAN["dataset_url"]), PLAN) == []
