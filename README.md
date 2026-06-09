
# 芯片构建与发送（LLM命名）

- 指令：`/buildchip <可选命名提示>` —— 调用 `vendor_pipeline/main.py` 运行工程，自动查找最新 `.melsave`，由 LLM 以函数调用生成文件名后重命名并发送。
- 指令：`/chipname <提示>` —— 仅生成一个建议的 `.melsave` 文件名。

## 安装
将本目录放入 `AstrBot/data/plugins/astrbot_plugin_chip_melsave_builder`，在 WebUI 插件管理中启用即可。

## 目录说明
- `main.py`：插件入口（必须命名为 main.py）。
- `vendor_pipeline/`：请将你的工程放在这里，需能通过 `python vendor_pipeline/main.py` 独立运行并产出 `.melsave`。
- `_conf_schema.json`：插件配置 Schema（可在管理面板里可视化配置）。
- `metadata.yaml`：插件市场展示信息。
- `requirements.txt`：插件依赖（如有）。

## 注意
- 某些平台可能不支持直接发送文件，届时消息里会仅显示文本或路径。
- 如果未启用 LLM 提供商，插件会使用时间戳兜底文件名。
