"""
LLM 调用封装。

设计原则：
1. 只依赖 OpenAI 兼容接口（DeepSeek / 通义 / 智谱 / OpenAI / 本地 Ollama 都能用）；
2. 没配 key 不报错，返回 available=False，让上层走规则模式；
3. 结构化输出用 JSON 模式 + 解析重试，不指望模型一次就吐对。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")


class LLM:
    def __init__(self, override: dict[str, Any] | None = None) -> None:
        cfg: dict[str, Any] = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = {}
        # 传入 override 时可临时试配置（不落盘），用于「测试连接」
        if override:
            cfg = {**cfg, **{k: v for k, v in override.items() if v}}

        # 配置优先级：config.json > 环境变量
        self.base_url = (cfg.get("base_url") or os.getenv("LLM_BASE_URL") or "").rstrip("/")
        self.api_key = cfg.get("api_key") or os.getenv("LLM_API_KEY") or ""
        self.model = cfg.get("model") or os.getenv("LLM_MODEL") or ""

        # 常见厂商的默认值，少让用户填几个字段
        if self.base_url and not self.base_url.endswith("/chat/completions"):
            self.endpoint = self.base_url + "/chat/completions"
        elif self.base_url:
            self.endpoint = self.base_url
        else:
            self.endpoint = "https://api.deepseek.com/v1/chat/completions"
            self.model = self.model or "deepseek-chat"

        self.available = bool(self.api_key and self.model)
        self.last_error = "" if self.available else "未配置 api_key"

    # ---------- 基础调用 ----------

    async def chat(self, system: str, user: str, temperature: float = 0.3,
                   json_mode: bool = False, timeout: float = 120.0) -> str:
        if not self.available:
            raise RuntimeError("LLM 不可用：" + self.last_error)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(self.endpoint, json=payload, headers=headers)
            if resp.status_code >= 400:
                # 部分厂商不支持 response_format，去掉重试一次
                if json_mode and resp.status_code in (400, 422):
                    payload.pop("response_format", None)
                    resp = await client.post(self.endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()

        return data["choices"][0]["message"]["content"] or ""

    # ---------- 结构化输出 ----------

    async def json_call(self, system: str, user: str, retries: int = 2) -> dict:
        """要求模型返回 JSON；解析失败就把错误喂回去重试。"""
        last_err = ""
        for attempt in range(retries + 1):
            hint = ""
            if last_err:
                hint = f"\n\n【上一次输出无法解析，错误：{last_err}。请只返回合法 JSON，不要任何解释文字。】"
            raw = await self.chat(system, user + hint, temperature=0.2, json_mode=True)
            obj = _loads_loose(raw)
            if obj is not None:
                return obj
            last_err = "返回内容不是合法 JSON"
        raise ValueError("模型多次未能返回合法 JSON: " + last_err)


def _loads_loose(raw: str) -> dict | None:
    """容错解析：去掉 ```json 围栏，截取第一个 { 到最后一个 }。"""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


llm = LLM()
