from __future__ import annotations

from typing import Any
from pydantic import Field
from pydantic.dataclasses import dataclass
from pathlib import Path
import json

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageEventResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext


def _format_result(
    success: bool,
    message: str,
    stage: str = "",
    logs: list[str] | None = None,
    file_info: dict[str, str] | None = None,
) -> str:
    """
    格式化 Tool 执行结果，使其易于 LLM 理解。

    Args:
        success: 是否成功
        message: 主要消息（成功或错误描述）
        stage: 失败阶段（仅失败时）
        logs: 日志列表
        file_info: 文件信息（仅成功时）
    """
    if success:
        # 成功时返回简洁的确认信息，让AI知道任务已完成
        return f"✅ {message}"

    # 失败时返回详细的错误信息
    result: dict[str, Any] = {
        "status": "❌ 失败",
        "message": message,
    }

    if stage:
        result["失败阶段"] = stage

    if logs:
        result["执行日志"] = logs

    return json.dumps(result, ensure_ascii=False, indent=2)


@dataclass
class ChipBuildTool(FunctionTool[AstrAgentContext]):
    """
    单一工具：构建芯片（.melsave）。
    - 可选输入 DSL；若提供则先落盘再构建，否则直接执行流水线。
    - 成功时：向用户发送文件（不包含任何日志文本）。
      同时将包含详细日志与文件元信息的 JSON 返回给 LLM。
    - 失败时：不向用户发送任何消息，仅将失败阶段与日志 JSON 返回给 LLM。
    """

    plugin: Any | None = None
    name: str = "chip_build"
    description: str = (
        "构建芯片（.melsave）。可选 DSL 输入；成功后发送文件给用户，"
        "并将执行日志以 JSON 返回给 LLM（日志不展示给用户）。"
    )
    parameters: dict = Field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dsl": {
                    "type": "string",
                    "description": "可选：芯片 DSL 文本（按 converter 约定格式）",
                },
                "base_name": {
                    "type": "string",
                    "description": "可选：文件基名（不含扩展名）；留空则由 LLM 自动命名",
                },
            },
            "required": [],
        }
    )

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        """执行芯片构建流程，返回格式化的结果给 LLM。"""
        if self.plugin is None:
            raise ValueError("ChipBuildTool.plugin is not set.")

        agent_ctx = context.context
        event: AstrMessageEvent = agent_ctx.event
        dsl = str(kwargs.get("dsl") or "")
        base_name = str(kwargs.get("base_name") or "")
        logs: list[str] = []

        # 1) 若提供 DSL，先尝试写入
        if dsl and dsl.strip():
            try:
                t = self.plugin._write_dsl(dsl)
                logs.append(f"✓ DSL 已写入: {t}")
            except Exception as e:
                return _format_result(
                    success=False,
                    message=f"DSL 写入失败: {e}",
                    stage="write_dsl",
                    logs=logs,
                )

        # 2) 文件命名（使用时间戳）
        try:
            if not base_name:
                base_name = await self.plugin._propose_name_via_llm(event)
                logs.append(f"✓ 使用时间命名: {base_name}")
        except Exception as e:
            logs.append(f"⚠ 时间命名失败: {e}，将使用默认名称")
            base_name = ""

        safe_stem = self.plugin._slugify(base_name)
        logs.append(f"✓ 最终文件名: {safe_stem}.melsave")

        # 3) 执行流水线
        try:
            produced: Path | None = await self.plugin._run_pipeline_subprocess()
            logs.append("✓ 流水线执行完成")
        except Exception as e:
            return _format_result(
                success=False,
                message=f"流水线执行失败: {e}",
                stage="pipeline_execution",
                logs=logs,
            )

        if not produced or not produced.exists():
            return _format_result(
                success=False,
                message="流水线执行完成，但未找到生成的 .melsave 文件。请检查流水线输出配置。",
                stage="output_collection",
                logs=logs,
            )

        logs.append(f"✓ 找到生成文件: {produced.name}")

        # 4) 重命名到期望文件名（保持在相同目录）
        target = produced.with_name(f"{safe_stem}.melsave")
        try:
            if produced != target:
                if target.exists():
                    backup = target.with_name(target.stem + "_bak.melsave")
                    try:
                        target.replace(backup)
                        logs.append(f"✓ 目标文件已存在，已备份为: {backup.name}")
                    except Exception:
                        logs.append(f"⚠ 备份失败，继续覆盖")
                produced.replace(target)
                logs.append(f"✓ 文件已重命名为: {target.name}")
        except Exception as e:
            logs.append(f"⚠ 重命名失败，使用原文件名: {e}")
            target = produced

        if not target.exists():
            return _format_result(
                success=False,
                message="文件在发送前丢失，请检查磁盘空间和权限",
                stage="file_finalization",
                logs=logs,
            )

        # 5) 成功：向用户发送文件（不包含日志文本）
        # 优先尝试 OneBot v11 直接上传，失败时回退到 Comp.File
        try:
            if not await self.plugin._upload_file_via_onebot_v11(event, target):
                mer: MessageEventResult = event.chain_result(
                    [Comp.File(file=str(target), name=target.name)]
                )
                await event.send(mer)
            logs.append("✓ 文件已发送给用户")
        except Exception as e:
            logs.append(f"⚠ 发送文件失败（但文件已成功构建）: {e}")

        # 返回给 LLM 的简洁成功信息，明确告知任务已结束，不要再调用工具
        return _format_result(
            success=True,
            message=f"芯片文件 {target.name} 已成功构建并直接发送给用户。任务已完成，请回复用户告知结果。",
        )
