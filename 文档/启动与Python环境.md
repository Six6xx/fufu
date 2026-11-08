# Windows 启动与 Python 环境说明

## 系统要求

- Windows 10/11，64 位。
- Python 3.12（推荐）或兼容的 Python 3.11+。
- 首次启动需联网安装依赖并连接模型服务。

安装 Python 时启用 Python Launcher（`py`）和 `Add Python to PATH`。在 PowerShell 验证：`py -3.12 --version`。若没有 `py`，启动器尝试用 `python`。

## 一键启动

双击封装目录根部的 `启动.bat`。它会创建独立 `.venv`、安装 `requirements.txt`、设置 `PYTHONPATH` 并启动 Web 服务。默认地址 `http://127.0.0.1:8000`。关闭控制台窗口即可停止服务。

依赖清单更新后，删除 `.venv/.requirements-installed` 再启动以重新安装；重建环境则删除封装目录中的 `.venv` 后再启动。

## 手动创建环境

在封装目录打开 PowerShell：

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install --upgrade pip
.venv\Scripts\python.exe -m pip install -r requirements.txt
$env:PYTHONPATH = (Get-Location).Path
.venv\Scripts\python.exe -m code_demo --server --host 127.0.0.1 --port 8000
```

项目使用 `code_demo.*` 导入，因此 `PYTHONPATH` 必须指向封装目录；启动器会自动设置。

## 模型配置

`code_demo/ai_configuration/model_providers.json` 是空白模板。可通过 Web 配置页面添加供应商，或手动填写项目格式：供应商包含 `id`、`name`、`url`、`api_key`、`models`；模型项包含 `type`（如 `llm`）和 `name`；`active_provider_id` 指向选中的供应商。

封装时没有复制开发机的 API Key 和个人运行数据。真实密钥只放在本机配置中。

## 陪伴者首次设置

封装版提供始终注入的内部 Skill `__companion_onboarding`。首次使用时，模型应询问用户姓名/称呼、陪伴者名称与用户希望的称呼、兴趣爱好、偏好人格；有任何项目未确认时保留引导，并在每个新会话继续询问缺项。四项复述并经用户确认后，模型通过 `complete_companion_setup` 保存至 `ai_configuration/companion_profile.json` 并自动移除引导。封装版的人格基线不预置原项目作者、旧用户姓名或偏好。

## 封装边界

`code_demo/core`、`rag`、`asr_tts` 为 Python 源码；`web_html`、`live2d` 是服务引用的静态资源；`skills` 包含活跃、内部和主动学习技能，不含 `__retired`；`ai_configuration` 是空白模板和运行时目录；`文档/项目结构` 是架构/面试材料。

已排除 `.venv`、Python 缓存、`claude-code`、`llm_harness`、`games`、IDE 配置及无关目录。

## 常见问题

- **找不到 Python**：安装 64 位 Python，并启用 Python Launcher/PATH。
- **依赖安装失败**：检查网络和 Python 版本，再运行 `.venv\Scripts\python.exe -m pip install -r requirements.txt`。
- **模型请求失败**：检查服务商 URL、API Key、模型名称和网络。服务启动成功不代表已配置模型凭据。
- **端口占用**：编辑 `启动.bat` 最后一条命令的 `--port 8000`。
