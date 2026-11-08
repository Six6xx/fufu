# 兴趣使然

## 简介

本项目开始于2026年4月初, 当时openai推出了 harness这个概念, 并同时关于claude code的开源, 使其能够观摩并模仿其中顶尖的harness内核;

本项目主要是想打造一个私人定制化的ai助手,借鉴了hermess的进化逻辑, 使其能够在与使用者交互当中进化, 同时加入了llm_wiki以及graphify等开源项目,但是发现仍然有许多困难,需要后续的沉淀

总之这个只是通过一份简易的harness的内核外,加上了live2d的元素,由于本人实在不熟悉live2d, 所以无法拥有完成度高的动画模型.

正如标题所说,这只是兴趣使然的一份产物,或许后续不会再更新,又或许在实力上升后,可以把这个打造成一个完美的产品,而不是一个简易的demo

后续为ai写出的启动方式以及项目结构梳理



独立 Windows 汇总目录，包含应用运行代码、Web 静态资源、活跃 Skills、项目结构文档和不含密钥的配置模板。虚拟环境、缓存、实验目录及无关项目未打包。

## 启动

1. 安装 64 位 Python 3.12，并启用 Python Launcher（`py`）和 PATH。
2. 双击 `启动.bat`。首次启动会创建 `.venv`、安装依赖并运行 Web 服务。
3. 浏览器访问 `http://127.0.0.1:8000`。
4. 首次使用前，在 `code_demo/ai_configuration/model_providers.json` 配置模型供应商和 API Key。请勿分享真实密钥。

完整说明见 [文档/启动与Python环境.md](文档/启动与Python环境.md)。

## 内容范围

- `code_demo/core`：Agent、CompanionAgent、AgentTeam、Web、MCP、工具与记忆。
- `code_demo/rag`、`code_demo/asr_tts`：RAG 和语音功能。
- `code_demo/web_html`、`code_demo/live2d`：Web 界面和静态资源。
- `code_demo/skills`：活跃、内部和项目主动学习技能（不含下线归档）。
- 首次使用：内置 `__companion_onboarding` 会询问用户称呼、陪伴者称呼、兴趣和人格偏好；通过 `complete_companion_setup` 保存后自动移除引导。
- `code_demo/ai_configuration`：空白配置模板和运行时配置目录。
- `文档`：项目架构/面试梳理和启动说明。
