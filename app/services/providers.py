"""统一模型供应商适配层。

Qwen、DeepSeek 等厂商都提供 OpenAI-compatible 接口，因此这里用官方
openai SDK 封装一个统一客户端。业务代码只依赖本模块，不直接与某一家
厂商绑定，从而实现"可切换模型"的设计目标。

调用关系：
- 依赖 app/settings.py 的 ProviderConfig
- 被 ingestion.py 调用 embed()（文档入库向量化）
- 被 rag.py 调用 embed()（问题向量化）与 chat()（生成回答）
- 内部使用官方 openai SDK，通过 base_url 指向 Qwen / DeepSeek 等兼容服务
"""

from openai import OpenAI

from app.core.config import ProviderConfig


class OpenAICompatibleProvider:
    """面向任意 OpenAI-compatible 服务的 Provider 封装。

    同时支持两种能力：
    - Embedding：把文本批量转换为向量；
    - Chat：调用聊天模型生成回答。
    """

    def __init__(self, config: ProviderConfig):
        """根据配置创建 OpenAI 客户端。

        参数:
            config: 由 settings.get_provider() 生成的供应商配置。
        """
        self.config = config
        # 传入 base_url 即可指向 Qwen / DeepSeek 等兼容服务
        self.client = OpenAI(api_key=config.api_key, base_url=config.base_url)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """将一批文本批量转换为 Embedding 向量。

        参数:
            texts: 待向量化的文本列表（一次可传入多个文本块）。
        返回:
            与 texts 一一对应的向量列表。
        """
        # 供应商未配置 Embedding 模型时（如 DeepSeek）直接报错，
        # 防止请求打到不存在的模型
        if not self.config.embedding_model:
            raise RuntimeError(f"Provider '{self.config.name}' has no embedding model configured.")
        response = self.client.embeddings.create(model=self.config.embedding_model, input=texts)
        # 响应 data 中的顺序与请求输入顺序一致
        return [item.embedding for item in response.data]

    def chat(self, system: str, user: str) -> str:
        """调用聊天模型，返回模型生成的回答文本。

        参数:
            system: 系统提示词（约束回答行为，如"仅依据资料回答"）。
            user:   用户输入（通常包含检索到的资料上下文与问题）。
        返回:
            模型回答文本；为空时返回空字符串。
        """
        response = self.client.chat.completions.create(
            model=self.config.chat_model,
            temperature=0.1,  # 低温采样，保证回答稳定、贴近资料
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        return response.choices[0].message.content or ""

    def chat_stream(self, system: str, user: str):
        """流式调用聊天模型，逐块产出回答增量文本（生成器）。

        与 chat() 参数一致、prompt 一致，区别在于开启 stream=True 并逐个
        yield 模型返回的 content 增量，供前端 SSE 流式渲染。
        """
        response = self.client.chat.completions.create(
            model=self.config.chat_model,
            temperature=0.1,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            stream=True,
        )
        for chunk in response:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            content = getattr(delta, "content", None)
            if content:
                yield content
