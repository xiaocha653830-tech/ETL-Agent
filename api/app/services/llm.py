"""大模型适配层（M3，FR-08 ~ FR-13；技术选型见 SPEC §2 与 §11 Q2）。

两种后端（``settings.llm_backend``）：

| 后端 | 用途 | 说明 |
|------|------|------|
| ``fake`` | 开发 / 单元测试 | 确定性替身，**不消费真实额度**（SPEC §4 的硬约定），无需 API Key |
| ``deepseek`` | 演示 / 生产 | OpenAI 兼容协议；模型名与别名风险见 SPEC §11 Q2 |

对外只暴露 :class:`LLMClient` 协议，业务代码不感知具体后端。
"""

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Protocol

from app.core.config import settings
from app.core.errors import ModelOutputInvalid, ModelTimeout, ModelUnavailable

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Message:
    role: str  # system | user | assistant
    content: str


class LLMClient(Protocol):
    """模型客户端协议。业务侧只依赖这两个方法。"""

    def complete(self, messages: Sequence[Message], *, temperature: float = 0.0) -> str:
        """返回模型的自由文本输出。"""
        ...

    def complete_json(
        self, messages: Sequence[Message], *, temperature: float = 0.0
    ) -> dict[str, Any]:
        """返回解析后的 JSON 对象。"""
        ...


# --------------------------------------------------------------------------
# JSON 抽取
# --------------------------------------------------------------------------


def extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里抽取 JSON 对象。

    真实模型常把 JSON 包在 ```json 代码块里，或前后带一段解释文字；
    这里做容错提取。抽不出合法对象时抛 :class:`ModelOutputInvalid`
    —— **绝不猜测或补全内容**，宁可失败也不生成看起来合理实则错误的结构。
    """
    text = (raw or "").strip()
    if not text:
        raise ModelOutputInvalid("模型返回空内容")

    if text.startswith("```"):
        # 仅剥离完整的外层代码围栏，不对 JSON 内容本身做任何“修复”。
        text = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ModelOutputInvalid("模型输出不是合法 JSON") from None
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ModelOutputInvalid("模型输出不是合法 JSON") from exc

    if not isinstance(data, dict):
        raise ModelOutputInvalid("模型输出不是 JSON 对象")
    return data


# --------------------------------------------------------------------------
# DeepSeek（OpenAI 兼容）
# --------------------------------------------------------------------------


class DeepSeekClient:
    """真实模型后端。"""

    def __init__(
        self, api_key: str, base_url: str, model: str, timeout_seconds: int = 60
    ) -> None:
        from openai import OpenAI  # 延迟导入，便于 fake 后端零依赖运行

        self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout_seconds)
        self._model = model

    def _payload(
        self, messages: Sequence[Message], temperature: float, *, json_mode: bool
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
        }
        if json_mode:
            # 服务端仍会再次解析和校验；JSON mode 只是降低模型输出自由文本的概率。
            body["response_format"] = {"type": "json_object"}
        return body

    def _call(
        self, messages: Sequence[Message], temperature: float, *, json_mode: bool
    ) -> str:
        try:
            response = self._client.chat.completions.create(
                **self._payload(messages, temperature, json_mode=json_mode)
            )
        except Exception as exc:  # noqa: BLE001 —— SDK 异常层次多，按类型名分流
            name = exc.__class__.__name__
            logger.warning("模型调用失败：%s", name)
            if "Timeout" in name:
                raise ModelTimeout(f"模型调用超时（{name}）") from exc
            raise ModelUnavailable(f"模型调用失败（{name}）") from exc
        return response.choices[0].message.content or ""

    def complete(self, messages: Sequence[Message], *, temperature: float = 0.0) -> str:
        return self._call(messages, temperature, json_mode=False)

    def complete_json(
        self, messages: Sequence[Message], *, temperature: float = 0.0
    ) -> dict[str, Any]:
        return extract_json(self._call(messages, temperature, json_mode=True))


# --------------------------------------------------------------------------
# Fake（开发 / 测试）
# --------------------------------------------------------------------------


class FakeLLMClient:
    """确定性替身。

    按构造时给定的脚本**顺序回放**响应，使多步状态机的每一步都能被精确断言。
    脚本耗尽即报错，而不是悄悄返回空对象 —— 让「少写了一段脚本」立刻暴露。
    """

    def __init__(self, responses: Sequence[str] | None = None) -> None:
        self._responses = list(responses or [])
        self.calls: list[list[Message]] = []

    def complete(self, messages: Sequence[Message], *, temperature: float = 0.0) -> str:
        self.calls.append(list(messages))
        if not self._responses:
            raise ModelUnavailable("FakeLLMClient 脚本已耗尽：请为每一步提供预置响应")
        return self._responses.pop(0)

    def complete_json(
        self, messages: Sequence[Message], *, temperature: float = 0.0
    ) -> dict[str, Any]:
        return extract_json(self.complete(messages, temperature=temperature))


# --------------------------------------------------------------------------
# 工厂
# --------------------------------------------------------------------------


def _build_client() -> LLMClient:
    backend = settings.llm_backend
    if backend == "fake":
        return FakeLLMClient()
    if backend == "deepseek":
        if not settings.deepseek_api_key:
            raise ModelUnavailable(
                "未配置 DEEPSEEK_API_KEY；如需离线开发请设置 LLM_BACKEND=fake"
            )
        return DeepSeekClient(
            api_key=settings.deepseek_api_key,
            base_url=settings.deepseek_base_url,
            model=settings.deepseek_model,
            timeout_seconds=settings.llm_timeout_seconds,
        )
    raise ModelUnavailable(f"未知的模型后端：{backend}")


@lru_cache
def get_llm_client() -> LLMClient:
    client = _build_client()
    logger.info("模型后端已加载：%s", settings.llm_backend)
    return client
