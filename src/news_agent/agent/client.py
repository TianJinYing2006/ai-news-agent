"""DeepSeek 客户端工厂（OpenAI 兼容协议）。

两个要点：

1. **key 缺失不在此处报错** —— 只有真正要调用模型时才校验（`build_client` 抛可读错误），
   这样工具层与其余单测无需凭据即可运行（docs 实施计划 §3.3 末尾）；
2. `trust_env=False` —— 本机代理对所有目标站点不通，必须直连，
   与 `tools/netguard.py` 同一实测结论（docs §2）。
"""

from __future__ import annotations

import httpx
from openai import OpenAI

from ..config import LLMConfig

# thinking 模式响应可能很长，读超时放宽；连接超时保持短，尽快暴露网络问题。
LLM_TIMEOUT = httpx.Timeout(120.0, connect=10.0)


def build_client(cfg: LLMConfig) -> OpenAI:
    """构建 DeepSeek 客户端。缺 key 时给出可读错误而不是让 SDK 报 401。"""
    if not cfg.api_key:
        raise RuntimeError(
            "未配置 DEEPSEEK_API_KEY：请在 .env 或环境变量中设置后，再运行需要模型的功能。"
        )
    http_client = httpx.Client(trust_env=False, timeout=LLM_TIMEOUT)
    return OpenAI(api_key=cfg.api_key, base_url=cfg.base_url, http_client=http_client)
