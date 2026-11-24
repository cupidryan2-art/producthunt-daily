"""配置、日志脱敏、JSON 读写等公共工具。"""
import json
import logging
import os
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
SECRET_VARS = ("PH_TOKEN", "DEEPSEEK_API_KEY", "GITHUB_TOKEN")
log = logging.getLogger("ph_digest")


def load_config():
    return yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))


def redact(text):
    text = str(text)
    for var in SECRET_VARS:
        value = os.environ.get(var, "").strip()
        if value:
            text = text.replace(value, "[REDACTED]")
    # 服务端报错有时会回显半遮盖的 key
    return re.sub(r"sk-[A-Za-z0-9]{6,}|\*{3,}\w+", "[REDACTED]", text)


class _RedactFormatter(logging.Formatter):
    def format(self, record):
        return redact(super().format(record))


def setup_logging():
    (ROOT / "logs").mkdir(exist_ok=True)
    fmt = _RedactFormatter("%(asctime)s %(levelname)s %(message)s")
    log.setLevel(logging.INFO)
    log.handlers.clear()
    for handler in (logging.StreamHandler(),
                    logging.FileHandler(ROOT / "logs" / "run.log", encoding="utf-8")):
        handler.setFormatter(fmt)
        log.addHandler(handler)


def read_json(path, default=None):
    path = Path(path)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    tmp.replace(path)


def fill(template_name, **values):
    """一次性替换占位符：资料里碰巧出现的 {xxx} 不会被二次替换。"""
    text = (ROOT / "prompts" / template_name).read_text(encoding="utf-8")
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m[1], m[0])), text)
