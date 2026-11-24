"""入口：python main.py [--date YYYY-MM-DD] [--dry-run] [--skip-review]"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import analyze
import fetch
import qa
import render
from common import ROOT, load_config, log, read_json, redact, setup_logging, write_json, write_text
from llm import LLM


def main():
    ap = argparse.ArgumentParser(description="Product Hunt 每日 Top 10 解读器")
    ap.add_argument("--date", type=date.fromisoformat, help="补跑某天（洛杉矶日期）")
    ap.add_argument("--dry-run", action="store_true", help="只抓数据，不调 LLM")
    ap.add_argument("--skip-review", action="store_true", help="跳过 LLM 审稿（调试用）")
    args = ap.parse_args()
    load_dotenv(ROOT / ".env")
    setup_logging()
    cfg, t0 = load_config(), time.time()
    today = datetime.now(ZoneInfo(cfg["fetch"]["timezone"])).date()
    target = args.date or today - timedelta(days=1)
    if target >= today:
        ap.error(f"{target} 在洛杉矶时区还没结束，排名不是最终结果")

    log.info("开始处理 %s", target)
    day = fetch.get_day(target, cfg["fetch"])
    for p in day["products"]:
        log.info("#%s %s（%s 票，maker 评论 %d 条，官网 %s）", p["rank"], p["name"], p["votes"],
                 len(p["maker_comments"]), p["website_status"])
    if args.dry_run:
        return 0

    llm, checker = LLM(cfg["llm"], cfg["pricing"]), qa.LinkChecker(cfg["qa"])
    seen_path = ROOT / "data" / "seen.json"
    seen = read_json(seen_path, {})

    def work(p):
        old = seen.get(p["id"])
        # 同一天重跑不算“再次上榜”
        if old and old["date"] != str(target):
            return p["id"], {"again": old}
        try:
            return p["id"], analyze.analyze_product(llm, p, cfg, checker, args.skip_review)
        except Exception as e:
            log.error("%s 分析失败: %s", p["name"], redact(e)[:300])
            return p["id"], {"error": redact(e)[:300]}

    with ThreadPoolExecutor(min(3, cfg["llm"]["concurrency"])) as pool:
        results = dict(pool.map(work, day["products"]))

    over = analyze.overview(llm, day, results)
    done = [r for r in results.values() if r.get("text")]
    scores = [r["qa"]["score"] for r in done if r["qa"]["score"] is not None]
    u = llm.usage
    stats = {"date": str(target), "analyzed": len(done), "passed": sum(r["qa"]["passed"] for r in done),
             "failed": sum("error" in r for r in results.values()),
             "revised": sum(r["qa"]["revisions"] for r in done),
             "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
             "tokens": sum(s["cache_hit"] + s["cache_miss"] + s["output"] for s in u.values()),
             "usd": round(sum(s["usd"] for s in u.values()), 5),
             "seconds": round(time.time() - t0, 1), "models": dict(u), "incomplete": llm.fatal}

    qa_out = {p["name"]: results[p["id"]].get("qa") or results[p["id"]] for p in day["products"]}
    report = render.render(day, results, over, stats)
    mismatch = qa.check_report(report, day)
    for m in mismatch:
        log.error(m)
    write_json(ROOT / "data" / "qa" / f"{target}.json",
               {"stats": stats, "report_check": mismatch, "products": qa_out})
    write_text(ROOT / "reports" / f"{target}.md", report)
    render.write_index()
    for p in day["products"]:
        if results[p["id"]].get("text"):
            seen[p["id"]] = {"date": str(target), "name": p["name"]}
    write_json(seen_path, seen)
    log.info("完成：%s", stats)
    # 全军覆没时让 GitHub Actions 显示红叉，避免悄悄产出空报告
    if llm.fatal:
        log.error("DeepSeek %s，本次报告不完整，处理后请用 --date %s 重跑", llm.fatal, target)
    return 1 if llm.fatal or mismatch or (stats["failed"] and not done) else 0


if __name__ == "__main__":
    sys.exit(main())
