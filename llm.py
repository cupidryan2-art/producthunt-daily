"""DeepSeek 调用封装：并发限制、重试、token 与费用统计。"""
import json
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from openai import OpenAI

from common import log, redact


class LLM:
    def __init__(self, cfg, pricing, client=None):
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if client is None and not key:
            raise RuntimeError("环境变量 DEEPSEEK_API_KEY 未设置")
        self.cfg, self.pricing = cfg, pricing
        self.client = client or OpenAI(api_key=key, base_url=cfg["base_url"],
                                       timeout=cfg["timeout"], max_retries=0)
        self.fatal = ""          # 余额不足/密钥无效：本次运行结果不完整
        self.sem = threading.Semaphore(min(3, cfg["concurrency"]))
        self.lock = threading.Lock()
        self.usage = defaultdict(lambda: {"calls": 0, "cache_hit": 0, "cache_miss": 0,
                                          "output": 0, "usd": 0.0})

    def _record(self, model, u):
        hit = getattr(u, "prompt_cache_hit_tokens", None)
        if hit is None:
            hit = getattr(getattr(u, "prompt_tokens_details", None), "cached_tokens", 0) or 0
        miss, out = u.prompt_tokens - hit, u.completion_tokens
        now = datetime.now(timezone.utc)
        peak = now.weekday() < 5 and any(a <= now.hour < b for a, b in self.pricing["peak_hours_utc"])
        price = self.pricing["models"].get(model, {}).get("peak" if peak else "offpeak")
        with self.lock:
            s = self.usage[model]
            s["calls"] += 1
            s["cache_hit"] += hit
            s["cache_miss"] += miss
            s["output"] += out
            if price:
                s["usd"] += (hit * price["cache_hit"] + miss * price["cache_miss"]
                             + out * price["output"]) / 1e6
            else:
                log.warning("config.yaml 没有 %s 的价格，费用未计入", model)

    def call(self, prompt, role="analysis", as_json=False, check=None):
        """check(结果) 抛异常即视为失败并重试。role: analysis | review"""
        c = self.cfg
        model = c[f"{role}_model"]
        temp = c["temperature"] if role == "analysis" else c["review_temperature"]
        extra = {"response_format": {"type": "json_object"}} if as_json else {}
        body = {"thinking": {"type": "enabled" if c[f"{role}_thinking"] else "disabled"}}
        if c[f"{role}_thinking"] and c.get(f"{role}_reasoning_effort"):
            body["reasoning_effort"] = c[f"{role}_reasoning_effort"]
        for attempt in range(c["retries"] + 1):
            try:
                with self.sem:
                    r = self.client.chat.completions.create(
                        model=model, temperature=min(0.5, temp), max_tokens=c["max_tokens"],
                        messages=[{"role": "user", "content": prompt}],
                        extra_body=body, **extra)
                if r.usage:
                    self._record(model, r.usage)
                if r.choices[0].finish_reason == "length":
                    raise ValueError("输出被截断")
                text = (r.choices[0].message.content or "").strip()
                if not text:
                    raise ValueError("模型返回空内容")
                result = json.loads(text) if as_json else text
                if check:
                    check(result)
                return result
            except Exception as e:
                if getattr(e, "status_code", None) in (401, 402):
                    self.fatal = "余额不足" if e.status_code == 402 else "密钥无效"
                    raise            # 重试也没用
                if attempt == c["retries"]:
                    raise
                log.warning("LLM 调用失败（%s，第 %d 次）: %s", model, attempt + 1, redact(e)[:200])
                time.sleep(c["backoff_seconds"] * 2 ** attempt)
