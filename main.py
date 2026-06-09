from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib.util
import io
import json
import os
import re
import sys
import traceback
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import astrbot.api.message_components as Comp
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_temp_path

try:
    # Prefer package-relative import when loaded as a plugin
    from .tools.build import ChipBuildTool
except Exception:
    # Fallback for direct execution contexts
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str((_Path(__file__).parent / "tools").resolve()))
    from build import ChipBuildTool


PLUGIN_DIR = Path(__file__).parent.resolve()
VENDOR_DIR = PLUGIN_DIR / "vendor_pipeline"


@register(
    "astrbot_plugin_chip_melsave_builder",
    "heh469051",
    "生成芯片 .melsave（支持 DSL 输入，LLM 自动命名）",
    "1.1.0",
)
class ChipMelsaveBuilder(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.cfg = self.context.get_config() or {}
        self._pipeline_lock = asyncio.Lock()
        # Register LLM tool here to ensure it always runs
        try:
            # Preferred API (>= v4.5.1)
            self.context.add_llm_tools(ChipBuildTool(plugin=self))
            print("[chip-plugin] tool 'chip_build' registered (add_llm_tools)")
        except Exception as e:
            # Legacy fallback (< v4.5.1)
            try:
                tool_mgr = self.context.provider_manager.llm_tools
                tool_mgr.func_list.append(ChipBuildTool(plugin=self))
                print("[chip-plugin] tool 'chip_build' registered (legacy)")
            except Exception as ee:
                print(
                    f"[chip-plugin] tool registration failed: add_llm_tools={e}; legacy={ee}"
                )

    def init(self, context: Context):
        super().init(context)
        # Registration already handled in __init__. Keep init lightweight.
        return
        # 注册单一工具：构建并发布 .melsave，同时向 LLM 返回日志 JSON
        try:
            self.context.add_llm_tools(ChipBuildTool(plugin=self))
            print("[chip-plugin] 已注册工具 chip_build")
        except Exception as e:
            print(f"[chip-plugin] 注册工具失败: {e}")

    # ---------- 配置读取 ----------
    def _cfg_str(self, key: str, default: str) -> str:
        v = self.cfg.get(key, default)
        if not isinstance(v, str):
            return default
        return v

    def _cfg_list(self, key: str, default: list[str]) -> list[str]:
        v = self.cfg.get(key, default)
        if not isinstance(v, list):
            return default
        return v

    # ---------- 时间命名 ----------
    async def _propose_name_via_llm(self, event: AstrMessageEvent) -> str:
        """使用当前时间生成文件名，格式：chip_YYYYMMDD_HHMMSS"""
        try:
            from datetime import datetime

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            return f"chip_{timestamp}"
        except Exception as e:
            print(f"[chip-plugin] 时间命名失败: {e}")
            # 如果出错，使用默认时间格式
            return "chip_default"

    def _slugify(self, base: str) -> str:
        if not base:
            name = "chip_" + datetime.now().strftime("%Y%m%d_%H%M%S")
        else:
            # 移除可能导致路径问题的字符
            name = re.sub(r"[^0-9A-Za-z._-]+", "_", base).strip("._-")
            # 避免以点开头（隐藏文件）
            if name.startswith("."):
                name = "_" + name[1:]
            # 避免以连字符结尾
            name = name.rstrip("-")
            if not name:
                name = "chip_" + datetime.now().strftime("%Y%m%d_%H%M%S")

        # 确保长度限制
        return name[:40]

    # ---------- DSL 下发 ----------
    def _write_dsl(self, dsl: str) -> Path:
        """
        根据配置写入 DSL。
        dsl_target_path: 写入路径，默认 vendor_pipeline/input.py
        dsl_wrapper:     包裹模板，使 {dsl} 占位；为空则原样写入
        """
        if not dsl or not dsl.strip():
            raise ValueError("DSL 内容不能为空")

        target = self._cfg_str("dsl_target_path", "vendor_pipeline/input.py")
        wrapper = self._cfg_str("dsl_wrapper", "")  # 设为空字符串，原样写入 DSL
        target_path = (PLUGIN_DIR / target).resolve()

        # 安全检查：确保目标路径在插件目录内
        try:
            target_path.relative_to(PLUGIN_DIR)
        except ValueError:
            raise ValueError(f"目标路径 {target_path} 超出插件目录范围")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        content = wrapper.replace("{dsl}", dsl) if wrapper else dsl
        target_path.write_text(content, encoding="utf-8")
        return target_path

    # ---------- 执行流水线 ----------
    def _summarize_pipeline_logs(self, output_text: str, error_text: str = "") -> str:
        keywords = (
            "error",
            "failed",
            "warning",
            "traceback",
            "exception",
            "错误",
            "失败",
            "警告",
        )
        lines: list[str] = []

        for raw_text in (error_text, output_text):
            for line in raw_text.splitlines():
                cleaned = line.strip()
                if cleaned:
                    lines.append(cleaned)

        if not lines:
            return "流水线执行失败，未捕获到可用日志。"

        prioritized = [
            line
            for line in lines
            if any(keyword in line.lower() for keyword in keywords)
        ]
        selected = prioritized[-8:] if prioritized else lines[-8:]
        summary = "\n".join(selected)
        if len(summary) > 1200:
            summary = summary[:1200].rstrip() + "..."
        return summary

    def _run_pipeline_in_process(self, entry_path: Path) -> None:
        module_name = "_chip_vendor_pipeline_main"
        stdout_buffer = io.StringIO()
        stderr_buffer = io.StringIO()
        original_cwd = Path.cwd()
        original_sys_path = list(sys.path)

        try:
            os.chdir(entry_path.parent)
            # Ensure the vendor_pipeline directory is in sys.path for imports
            vendor_path = str(entry_path.parent)
            if vendor_path not in sys.path:
                sys.path.insert(0, vendor_path)
            
            # Clean up cached 'src' modules to avoid stale imports from previous runs
            modules_to_remove = [name for name in sys.modules if name == 'src' or name.startswith('src.')]
            for name in modules_to_remove:
                del sys.modules[name]

            spec = importlib.util.spec_from_file_location(module_name, entry_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"无法加载流水线入口：{entry_path}")

            module = importlib.util.module_from_spec(spec)
            with (
                contextlib.redirect_stdout(stdout_buffer),
                contextlib.redirect_stderr(stderr_buffer),
            ):
                spec.loader.exec_module(module)
                if not hasattr(module, "main"):
                    raise RuntimeError(f"流水线入口缺少 main()：{entry_path}")
                module.main()
        except Exception as exc:
            traceback.print_exc(file=stderr_buffer)
            summary = self._summarize_pipeline_logs(
                output_text=stdout_buffer.getvalue(),
                error_text=stderr_buffer.getvalue(),
            )
            raise RuntimeError(summary) from exc
        finally:
            os.chdir(original_cwd)
            sys.path = original_sys_path
            sys.modules.pop(module_name, None)

    def _find_latest_output(
        self, workdir: Path, start_ts: float | None = None
    ) -> Path | None:
        globs = self._cfg_list(
            "output_search_globs",
            [
                "vendor_pipeline/output/*.melsave",
                "vendor_pipeline/**/*.melsave",
                "*.melsave",
            ],
        )
        candidates: list[Path] = []
        for glob_pattern in globs:
            try:
                candidates.extend(PLUGIN_DIR.glob(glob_pattern))
                candidates.extend(workdir.glob(glob_pattern))
            except Exception:
                pass

        valid_candidates: list[Path] = []
        for path in candidates:
            if not path.is_file():
                continue
            if start_ts is not None and path.stat().st_mtime < start_ts:
                continue
            valid_candidates.append(path)

        if not valid_candidates:
            return None

        return max(valid_candidates, key=lambda path: path.stat().st_mtime)

    async def _run_pipeline_subprocess(self) -> Path | None:
        entry_cfg = self._cfg_str("vendor_entry", "vendor_pipeline/main.py")
        entry_path = (PLUGIN_DIR / entry_cfg).resolve()

        # 安全检查：确保入口脚本在插件目录内
        try:
            entry_path.relative_to(PLUGIN_DIR)
        except ValueError:
            raise ValueError(f"入口脚本路径 {entry_path} 超出插件目录范围")

        if not entry_path.exists():
            raise FileNotFoundError(f"找不到入口脚本：{entry_path}")

        workdir = entry_path.parent  # 将工作目录设置为入口脚本所在目录
        async with self._pipeline_lock:
            start_ts = datetime.now().timestamp()
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._run_pipeline_in_process, entry_path),
                    timeout=300,
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError("流水线执行超时（5分钟），已强制终止。") from exc
            except Exception as exc:
                if not isinstance(exc, (FileNotFoundError, ValueError, RuntimeError)):
                    print(f"[chip-plugin] 执行流水线时发生未预期错误: {exc}")
                raise

        latest_file = self._find_latest_output(workdir, start_ts=start_ts)
        if latest_file is None:
            latest_file = self._find_latest_output(workdir)
        return latest_file

    # ---------- LLM Tool：根据 DSL 生成并发布 ----------
    # @llm_tool(name="chip_build")
    async def chip_build(
        self, event: AstrMessageEvent, dsl: str, base_name: str = ""
    ) -> MessageEventResult:
        """
        根据 DSL 生成并发送芯片（.melsave）。
        Args:
            dsl (str): 芯片 DSL 文本（按 converter 约定格式）。
            base_name (str): 可选，文件基名（不含扩展名）；留空则由 LLM 自动命名。
        """
        # 1) 落盘 DSL
        try:
            self._write_dsl(dsl)
        except ValueError as e:
            yield event.plain_result(f"DSL 参数错误：{e}")
            return
        except Exception as e:
            yield event.plain_result(f"写入 DSL 失败：{e}")
            return

        # 2) 需要时由 LLM 起名
        if not base_name:
            try:
                base_name = await self._propose_name_via_llm(event)
            except Exception:
                base_name = ""
        safe_stem = self._slugify(base_name)

        # 3) 跑一遍流水线
        try:
            produced = await self._run_pipeline_subprocess()
        except Exception as e:
            yield event.plain_result(f"生成失败：{e}")
            return

        if not produced or not produced.exists():
            yield event.plain_result(
                "没找到生成的 .melsave 文件（请检查流水线输出与路径）"
            )
            return

        # 4) 重命名并发送
        target = produced.with_name(f"{safe_stem}.melsave")

        # 安全检查：确保目标文件在合理范围内
        try:
            target.relative_to(PLUGIN_DIR)
        except ValueError:
            # 如果目标路径超出插件目录，使用插件目录下的 vendor_pipeline
            target = PLUGIN_DIR / "vendor_pipeline" / f"{safe_stem}.melsave"

        try:
            if produced != target:
                # 检查目标是否已存在
                if target.exists():
                    backup_name = target.with_name(
                        f"{safe_stem}_{datetime.now().strftime('%H%M%S')}.melsave"
                    )
                    target.replace(backup_name)

                produced.replace(target)
        except PermissionError:
            yield event.plain_result(
                "⚠️ 文件被占用，无法重命名。请关闭可能正在使用该文件的程序。"
            )
            target = produced
        except Exception as e:
            yield event.plain_result(f"⚠️ 已生成文件，但重命名失败：{e}\n将以原名发送。")
            target = produced

        # 最终检查文件是否存在
        if not target.exists():
            yield event.plain_result("文件在发送前丢失，请检查磁盘空间和权限。")
            return

        # 优先尝试 OneBot v11 直接上传，失败时回退到 Comp.File
        if not await self._upload_file_via_onebot_v11(event, target):
            yield event.chain_result(
                [Comp.File(file=str(target), name=target.name)]
            )

    # （保留旧工具，向后兼容，仅命名）
    # @llm_tool(name="build_chip_melsave")
    async def build_chip_melsave(
        self, event: AstrMessageEvent, base_name: str = ""
    ) -> MessageEventResult:
        """
        直接执行流水线并发布 .melsave，不写入 DSL。
        Args:
            base_name (str): 可选，文件基名（不含扩展名）；留空则由 LLM 自动命名。
        """
        # 1) 需要时由 LLM 起名
        if not base_name:
            try:
                base_name = await self._propose_name_via_llm(event)
            except Exception:
                base_name = ""
        safe_stem = self._slugify(base_name)

        # 2) 跑一遍流水线
        try:
            produced = await self._run_pipeline_subprocess()
        except Exception as e:
            yield event.plain_result(f"生成失败：{e}")
            return

        if not produced or not produced.exists():
            yield event.plain_result("没找到生成的 .melsave 文件")
            return

        # 3) 重命名并发送
        target = produced.with_name(f"{safe_stem}.melsave")

        # 安全检查：确保目标文件在合理范围内
        try:
            target.relative_to(PLUGIN_DIR)
        except ValueError:
            # 如果目标路径超出插件目录，使用插件目录下的 vendor_pipeline
            target = PLUGIN_DIR / "vendor_pipeline" / f"{safe_stem}.melsave"

        try:
            if produced != target:
                # 检查目标是否已存在
                if target.exists():
                    backup_name = target.with_name(
                        f"{safe_stem}_{datetime.now().strftime('%H%M%S')}.melsave"
                    )
                    target.replace(backup_name)

                produced.replace(target)
        except PermissionError:
            yield event.plain_result(
                "⚠️ 文件被占用，无法重命名。请关闭可能正在使用该文件的程序。"
            )
            target = produced
        except Exception as e:
            yield event.plain_result(f"⚠️ 已生成文件，但重命名失败：{e}\n将以原名发送。")
            target = produced

        # 最终检查文件是否存在
        if not target.exists():
            yield event.plain_result("文件在发送前丢失，请检查磁盘空间和权限。")
            return

        # 优先尝试 OneBot v11 直接上传，失败时回退到 Comp.File
        if not await self._upload_file_via_onebot_v11(event, target):
            yield event.chain_result(
                [Comp.File(file=str(target), name=target.name)]
            )

    # ---------- OneBot v11 直接文件上传 ----------
    async def _upload_file_via_onebot_v11(
        self, event: AstrMessageEvent, file_path: Path
    ) -> bool:
        """尝试使用 OneBot v11 API 直接上传文件。

        优先使用 OneBot v11 的 upload_group_file 或 upload_private_file API。
        如果成功返回 True，失败返回 False（由调用方回退到标准 Comp.File 方式）。
        """
        # 仅对 OneBot v11 (aiocqhttp) 平台生效
        if event.get_platform_name() != "aiocqhttp":
            return False

        bot = getattr(event, "bot", None)
        if bot is None:
            return False

        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        is_group = bool(group_id)

        try:
            file_str = str(file_path.resolve())
            if is_group and group_id:
                await bot.call_action(
                    "upload_group_file",
                    group_id=int(group_id),
                    file=file_str,
                    name=file_path.name,
                )
                print(f"[chip-plugin] 通过 OneBot v11 上传文件到群 {group_id}")
                return True
            elif user_id:
                await bot.call_action(
                    "upload_private_file",
                    user_id=int(user_id),
                    file=file_str,
                    name=file_path.name,
                )
                print(f"[chip-plugin] 通过 OneBot v11 上传文件给用户 {user_id}")
                return True
            else:
                return False
        except Exception as e:
            print(
                f"[chip-plugin] OneBot v11 上传失败: {e}，将回退到标准文件发送方式"
            )
            return False

    # ---------- 图片转DSL功能 ----------
    async def _extract_image_from_message(
        self, event: AstrMessageEvent
    ) -> bytes | None:
        """从消息中提取图片数据，支持引用消息中的图片"""
        # 首先检查当前消息中的图片
        for component in event.message_obj.message:
            if (
                hasattr(component, "__class__")
                and "Image" in component.__class__.__name__
            ):
                return await self._process_image_component(component)

        # 如果当前消息没有图片，检查是否有引用的消息（Reply）
        for component in event.message_obj.message:
            if (
                hasattr(component, "__class__")
                and "Reply" in component.__class__.__name__
            ):
                # 这是一个引用消息，尝试通过API获取原始消息
                try:
                    # 使用aiocqhttp API获取引用的消息
                    if event.get_platform_name() == "aiocqhttp":
                        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                            AiocqhttpMessageEvent,
                        )

                        if isinstance(event, AiocqhttpMessageEvent):
                            client = event.bot
                            # Reply组件可能有id属性
                            reply_id = getattr(component, "id", None) or getattr(
                                component, "message_id", None
                            )
                            if reply_id:
                                # 获取引用的消息
                                msg = await client.get_msg(message_id=int(reply_id))
                                if msg and isinstance(msg, dict):
                                    # 直接从msg中获取message字段
                                    if "message" in msg:
                                        for msg_comp in msg["message"]:
                                            if (
                                                isinstance(msg_comp, dict)
                                                and msg_comp.get("type") == "image"
                                            ):
                                                # 处理图片数据
                                                image_data = msg_comp.get("data", {})
                                                if "file" in image_data:
                                                    file_path = image_data["file"]
                                                    # 尝试从URL下载
                                                    if "url" in image_data:
                                                        return (
                                                            await self._download_image(
                                                                image_data["url"]
                                                            )
                                                        )
                                                    # 或者尝试本地文件
                                                    if Path(file_path).exists():
                                                        return Path(
                                                            file_path
                                                        ).read_bytes()
                                                elif "url" in image_data:
                                                    return await self._download_image(
                                                        image_data["url"]
                                                    )
                                                elif "base64" in image_data:
                                                    return base64.b64decode(
                                                        image_data["base64"]
                                                    )
                except Exception:
                    pass

        return None

    async def _process_image_component(self, component) -> bytes | None:
        """处理单个图片组件"""
        # 处理不同类型的图片消息
        if hasattr(component, "file") and component.file:
            # 本地文件路径
            file_path = component.file
            if Path(file_path).exists():
                return Path(file_path).read_bytes()
        elif hasattr(component, "url") and component.url:
            # URL图片 - 需要下载
            return await self._download_image(component.url)
        elif hasattr(component, "base64") and component.base64:
            # Base64编码的图片
            try:
                return base64.b64decode(component.base64)
            except Exception:
                pass
        return None

    async def _download_image(self, url: str) -> bytes | None:
        """下载网络图片"""
        import aiohttp

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception:
            pass
        return None

    def _iter_file_components(self, components: list[Any] | None) -> list[Comp.File]:
        if not components:
            return []

        files: list[Comp.File] = []
        for component in components:
            if isinstance(component, Comp.File):
                files.append(component)
                continue

            if isinstance(component, Comp.Reply) and getattr(component, "chain", None):
                files.extend(self._iter_file_components(component.chain))

        return files

    async def _resolve_melsave_component(
        self, component: Comp.File
    ) -> tuple[Path, str] | None:
        try:
            local_path = await component.get_file()
        except Exception:
            return None

        if not local_path:
            return None

        file_path = Path(local_path)
        file_name = component.name or file_path.name
        if not self._looks_like_melsave_archive(file_path, file_name):
            return None

        return file_path, file_name

    def _looks_like_melsave_archive(self, file_path: Path, file_name: str = "") -> bool:
        if not file_path.is_file() or not zipfile.is_zipfile(file_path):
            return False

        try:
            with zipfile.ZipFile(file_path, "r") as archive:
                names = {
                    Path(member).name
                    for member in archive.namelist()
                    if member and not member.endswith("/")
                }
        except (OSError, zipfile.BadZipFile):
            return False

        if "data" not in names:
            return False

        lowered_name = (file_name or file_path.name).lower()
        return (
            lowered_name.endswith(".melsave") or file_path.suffix.lower() == ".melsave"
        )

    async def _extract_melsave_from_aiocqhttp_reply(
        self, event: AstrMessageEvent
    ) -> tuple[Path, str] | None:
        if event.get_platform_name() != "aiocqhttp":
            return None

        bot = getattr(event, "bot", None)
        if bot is None:
            return None

        for component in event.get_messages():
            if not isinstance(component, Comp.Reply):
                continue

            reply_id = getattr(component, "id", None) or getattr(
                component, "message_id", None
            )
            if not reply_id:
                continue

            try:
                msg = await bot.call_action("get_msg", message_id=int(reply_id))
            except Exception:
                continue

            for raw_component in msg.get("message", []):
                if not isinstance(raw_component, dict):
                    continue
                if raw_component.get("type") != "file":
                    continue

                data = raw_component.get("data", {})
                file_name = (
                    data.get("name")
                    or data.get("file_name")
                    or data.get("file")
                    or "save.melsave"
                )
                file_url = data.get("url", "")
                file_id = data.get("file_id")

                if not file_url and file_id:
                    try:
                        if event.get_group_id():
                            ret = await bot.call_action(
                                action="get_group_file_url",
                                file_id=file_id,
                                group_id=event.get_group_id(),
                            )
                        else:
                            ret = await bot.call_action(
                                action="get_private_file_url",
                                file_id=file_id,
                            )
                    except Exception:
                        ret = {}

                    if isinstance(ret, dict):
                        file_url = ret.get("url", "")
                        file_name = ret.get("file_name") or ret.get("name") or file_name

                if not file_url:
                    continue

                resolved = await self._resolve_melsave_component(
                    Comp.File(name=file_name, url=file_url)
                )
                if resolved is not None:
                    return resolved

        return None

    async def _extract_melsave_from_message(
        self, event: AstrMessageEvent
    ) -> tuple[Path, str] | None:
        for component in self._iter_file_components(event.get_messages()):
            resolved = await self._resolve_melsave_component(component)
            if resolved is not None:
                return resolved

        return await self._extract_melsave_from_aiocqhttp_reply(event)

    def _force_freezed_true(self, node: Any) -> int:
        updated = 0

        if isinstance(node, dict):
            if "freezed" in node and node["freezed"] is not True:
                node["freezed"] = True
                updated += 1

            for value in node.values():
                updated += self._force_freezed_true(value)
            return updated

        if isinstance(node, list):
            for item in node:
                updated += self._force_freezed_true(item)

        return updated

    def _build_freezed_melsave(
        self, source_path: Path, source_name: str
    ) -> tuple[Path, int]:
        temp_root = Path(get_astrbot_temp_path()) / "chip_melsave_builder"
        temp_root.mkdir(parents=True, exist_ok=True)

        output_stem = self._slugify(Path(source_name or source_path.name).stem)
        output_path = (
            temp_root / f"{output_stem}_freezed_{uuid.uuid4().hex[:8]}.melsave"
        )

        try:
            with (
                zipfile.ZipFile(source_path, "r") as source_archive,
                zipfile.ZipFile(output_path, "w") as output_archive,
            ):
                data_member = None
                for member in source_archive.namelist():
                    if member.rstrip("/") == "data":
                        data_member = member
                        break

                if data_member is None:
                    raise RuntimeError(
                        "The .melsave archive does not contain a root data file."
                    )

                data_bytes = source_archive.read(data_member)
                payload = json.loads(data_bytes.decode("utf-8"))
                updated_count = self._force_freezed_true(payload)
                updated_bytes = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")

                for info in source_archive.infolist():
                    if info.is_dir():
                        output_archive.writestr(info, b"")
                        continue

                    if info.filename == data_member:
                        output_archive.writestr(info, updated_bytes)
                        continue

                    output_archive.writestr(info, source_archive.read(info.filename))
        except UnicodeDecodeError as exc:
            raise RuntimeError(
                "Failed to decode the data file inside .melsave."
            ) from exc
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Failed to parse the data file inside .melsave."
            ) from exc
        except (OSError, zipfile.BadZipFile) as exc:
            raise RuntimeError("Quoted file is not a valid .melsave archive.") from exc

        return output_path, updated_count

    def _image_to_dsl(
        self, image_data: bytes, width: int = 32, height: int = 18
    ) -> str:
        """将图片数据转换为DSL格式"""
        try:
            from PIL import Image

            # 打开图片
            img = Image.open(io.BytesIO(image_data))

            # 转换为RGBA模式
            if img.mode != "RGBA":
                img = img.convert("RGBA")

            # 调整大小 - 使用NEAREST算法以保持像素风格
            img_resized = img.resize((width, height), Image.NEAREST)

            # 获取像素数据
            pixels = list(img_resized.getdata())

            # 生成向量数组
            vectors = []
            for r, g, b, a in pixels:
                vectors.append(
                    [
                        round(r / 255, 4),
                        round(g / 255, 4),
                        round(b / 255, 4),
                        round(a / 255, 4),
                    ]
                )

            # 生成DSL字符串 - 完全按照参考代码的格式
            dsl_lines = []
            dsl_lines.append("# 自动生成的甜瓜游乐场图像芯片")
            dsl_lines.append(f"# 分辨率: {width} x {height}")
            dsl_lines.append("")
            dsl_lines.append("# --- Image Data (Vector Array) ---")
            dsl_lines.append("image_data = Constant(attrs={")
            dsl_lines.append('    "value": [')

            # 添加像素数据
            for v in vectors:
                dsl_lines.append(f"        {v},")

            dsl_lines.append("    ]")
            dsl_lines.append("})")
            dsl_lines.append("")
            dsl_lines.append("# --- Output ---")
            dsl_lines.append("image_output = OUTPUT(")
            dsl_lines.append("    INPUT=image_data,")
            dsl_lines.append('    attrs={"name":"ImageBuffer"}')
            dsl_lines.append(")")

            return "\n".join(dsl_lines)

        except ImportError:
            raise RuntimeError("需要安装Pillow库来处理图片。请运行: pip install Pillow")
        except Exception as e:
            raise RuntimeError(f"图片处理失败: {e}")

    @filter.command("转化图片")
    async def convert_image_to_chip(
        self, event: AstrMessageEvent, width: int = 32, height: int = 18
    ):
        """
        将引用的图片转换为芯片DSL并生成.melsave文件。
        用法: 引用图片并发送 "转化图片 [宽度] [高度]"
        默认尺寸: 32x18
        """
        yield event.plain_result("正在处理图片...")

        # 1. 从消息中提取图片
        image_data = await self._extract_image_from_message(event)
        if not image_data:
            yield event.plain_result("❌ 未找到图片，请引用图片后发送指令")
            return

        # 2. 验证尺寸参数
        if width <= 0 or height <= 0:
            yield event.plain_result(f"❌ 尺寸参数无效: {width}x{height}")
            return

        if width > 256 or height > 256:
            yield event.plain_result(
                "⚠️ 警告: 尺寸过大可能导致性能问题，建议使用较小的尺寸"
            )

        # 3. 转换图片为DSL
        try:
            dsl = self._image_to_dsl(image_data, width, height)
        except Exception as e:
            yield event.plain_result(f"❌ 图片转换失败: {e}")
            return

        # 4. 写入DSL
        try:
            self._write_dsl(dsl)
        except Exception as e:
            yield event.plain_result(f"❌ 写入DSL失败: {e}")
            return

        # 5. 生成文件名
        try:
            base_name = await self._propose_name_via_llm(event)
        except Exception:
            base_name = f"image_{width}x{height}"

        safe_stem = self._slugify(base_name)

        # 6. 执行流水线
        yield event.plain_result("正在生成芯片文件...")
        try:
            produced = await self._run_pipeline_subprocess()
        except Exception as e:
            yield event.plain_result(f"❌ 生成失败: {e}")
            return

        if not produced or not produced.exists():
            yield event.plain_result("❌ 未找到生成的.melsave文件")
            return

        # 7. 重命名并发送文件
        target = produced.with_name(f"{safe_stem}.melsave")

        try:
            if produced != target:
                if target.exists():
                    backup_name = target.with_name(
                        f"{safe_stem}_{datetime.now().strftime('%H%M%S')}.melsave"
                    )
                    target.replace(backup_name)
                produced.replace(target)
        except Exception as e:
            yield event.plain_result(f"⚠️ 重命名失败: {e}，使用原文件名")
            target = produced

        # 8. 发送文件
        if not target.exists():
            yield event.plain_result("❌ 文件在发送前丢失")
            return

        yield event.plain_result(f"✅ 图片芯片生成成功！尺寸: {width}x{height}")
        # 优先尝试 OneBot v11 直接上传，失败时回退到 Comp.File
        if not await self._upload_file_via_onebot_v11(event, target):
            yield event.chain_result(
                [Comp.File(file=str(target), name=target.name)]
            )

    # 便捷指令：让 LLM 主导对话并可调用上面两个工具
    @filter.command("freeze", alias={"冻结存档", "freeze存档"})
    async def freeze_melsave(self, event: AstrMessageEvent):
        yield event.plain_result("正在处理 .melsave 文件...")

        quoted_file = await self._extract_melsave_from_message(event)
        if quoted_file is None:
            yield event.plain_result(
                "未找到 .melsave 文件，请直接发送或引用一个群文件后再发送 freeze。"
            )
            return

        source_path, source_name = quoted_file

        try:
            output_path, updated_count = self._build_freezed_melsave(
                source_path, source_name
            )
        except Exception as exc:
            yield event.plain_result(f"处理失败: {exc}")
            return

        event.track_temporary_local_file(str(output_path))

        if updated_count > 0:
            yield event.plain_result(
                f"处理完成，已将 {updated_count} 个 freezed 字段改为 true。"
            )
        else:
            yield event.plain_result(
                "处理完成，没有发现需要从 false 改为 true 的 freezed 字段。"
            )

        # 优先尝试 OneBot v11 直接上传，失败时回退到 Comp.File
        if not await self._upload_file_via_onebot_v11(event, output_path):
            yield event.chain_result(
                [Comp.File(file=str(output_path), name=output_path.name)]
            )

    @filter.command("chip")
    async def chip(self, event: AstrMessageEvent):
        # 移除通过 request_llm 再次触发工具调用的逻辑，改为直接对话
        # 如果用户想通过指令触发构建，可以直接发送 DSL 或描述
        # 核心在于：AstrBot 默认会处理包含唤醒词的消息，如果插件手动再次 request_llm 且带上原工具，极易循环
        prompt = (event.message_str or "").replace("/chip", "").strip()
        if not prompt:
            yield event.plain_result("请在指令后输入芯片描述或 DSL。")
            return

        # 让 LLM 处理，但不额外传入 func_tool_manager，或者传入一个受限的 manager
        # 这里选择停止使用 request_llm 包装工具，因为 chip_build 已经注册为全局 LLM 工具了
        # 用户直接输入 DSL 触发全局工具即可，不需要这个 /chip 指令来中转
        yield event.plain_result(
            "请直接发送您的需求或 DSL（不需要带 /chip 前缀），我会为您构建芯片。"
        )
