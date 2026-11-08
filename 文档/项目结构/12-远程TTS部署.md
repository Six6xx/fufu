# 远程 CosyVoice TTS 服务部署

> 公网地址：`https://u834247-a589-7d0d0f30.bjb1.seetacloud.com:8443`

## 一、概述

Mac (Apple Silicon / MPS) 上 CosyVoice3 模型权重必须转为 Float32，导致语音含混不清。
为解决此问题，将 CosyVoice3 部署在远程 GPU 服务器上，通过 HTTP API 提供 TTS 合成服务。

本地项目通过 `TTSEngineRouter` 路由层调用远程服务，失败时自动回退到本地引擎。

```
本地 (Mac)                          远程 (AutoDL RTX 4090)
┌─────────────────┐    HTTPS POST    ┌──────────────────────────┐
│ 芙芙             │ ──────────────→ │ FastAPI (port 6006)      │
│ TTSEngineRouter  │ ← WAV bytes ──  │ CosyVoice3 (CUDA, fp16)  │
│ RemoteTTSEngine  │                  │ 芙芙微调模型              │
└─────────────────┘                  └──────────────────────────┘
                                         VRAM: ~5GB / 49GB
                                         合成速度: ~2.2s/句
```

## 二、远程服务器信息

| 项目 | 详情 |
|------|------|
| 平台 | AutoDL 云服务器 |
| GPU | NVIDIA RTX 4090 (49GB) |
| SSH | `ssh -p 11802 root@connect.bjb1.seetacloud.com`，密码 `pXQK+2QAaJld` |
| Python | `/root/miniconda3/bin/python` (3.10.8, base 环境) |
| PyTorch | 2.3.1+cu121, CUDA 可用 |
| CosyVoice 源码 | `/root/CosyVoice/` |
| 模型目录 | `/root/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B/` |
| 参考音频 | `/tmp/ref_audio.wav` (WAV, 16-bit mono, 48kHz) |
| 服务端口 | 6006 (AutoDL 自定义服务端口) |

## 三、公网 API 端点

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/tts/health` | 健康检查，返回模型加载状态 |
| `GET` | `/api/tts/info` | GPU 型号、采样率等详细信息 |
| `POST` | `/api/tts/synthesize` | 合成语音，返回 WAV 字节 |

### 健康检查

```bash
curl https://u834247-a589-7d0d0f30.bjb1.seetacloud.com:8443/api/tts/health
```

响应：
```json
{
  "status": "ok",
  "model_loaded": true,
  "device": "cuda",
  "sample_rate": 24000,
  "model_dir": "/root/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B",
  "load_time_seconds": 11.7
}
```

### 合成语音

```bash
curl -X POST https://u834247-a589-7d0d0f30.bjb1.seetacloud.com:8443/api/tts/synthesize \
  -H 'Content-Type: application/json' \
  -d '{"text": "你好，我是芙芙。"}' \
  -o output.wav
```

请求体：
```json
{
  "text": "要合成的文本",
  "prompt_text": "参考音频对应的文本 (可选, 有默认值)",
  "prompt_audio": "base64 编码的参考音频 (可选)",
  "speed": 1.0
}
```

响应：`audio/wav` 二进制数据 (16-bit PCM, mono, 24000 Hz)

## 四、模型信息

使用的是 **芙芙微调** 后的 Fun-CosyVoice3-0.5B 模型：

| 组件 | 文件 | 训练状态 |
|------|------|----------|
| LLM | `llm.pt` | 微调 epoch 28 (acc 99.71%) |
| Flow | `flow.pt` | 微调 epoch 20 (CV loss 0.589) |
| HiFi-GAN | `hift.pt` | 微调完成 |

原始预训练权重备份为 `*.pt.bak`。

### 参考音频

默认参考音频 `/tmp/ref_audio.wav` 对应文本：
> 我不是俱乐部的会员。只是一大早被克洛琳德敲门叫醒，说有个不错的剧本可以体验，这才决定加入的。

`prompt_text` 格式为 CosyVoice3 instruct 模式：
```
You are a helpful assistant.<|endofprompt|>参考音频对应的文本
```

## 五、服务启动与重启

### 启动脚本

路径：`/root/run_tts_server.sh`

```bash
#!/bin/bash
export OMP_NUM_THREADS=1
export COSYVOICE_SRC=/root/CosyVoice

cd /root/CosyVoice

/root/miniconda3/bin/python /root/cosyvoice_server.py \
  --model /root/CosyVoice/pretrained_models/Fun-CosyVoice3-0.5B \
  --prompt-wav /tmp/ref_audio.wav \
  --prompt-text "You are a helpful assistant.<|endofprompt|>我不是俱乐部的会员。只是一大早被克洛琳德敲门叫醒，说有个不错的剧本可以体验，这才决定加入的。" \
  --host 0.0.0.0 \
  --port 6006 \
  --device cuda
```

### 操作命令

```bash
# SSH 连接
ssh -p 11802 root@connect.bjb1.seetacloud.com
# 密码: pXQK+2QAaJld

# 启动服务 (screen 后台运行)
screen -dmS tts bash /root/run_tts_server.sh

# 查看日志
tail -f /tmp/tts_server.log

# 停止服务
screen -X -S tts quit

# 重启服务
screen -X -S tts quit && screen -dmS tts bash /root/run_tts_server.sh
```

## 六、本地项目集成

### 文件关系

```
code_demo/
├── asr_tts/
│   ├── cosyvoice_server.py      # 远程服务端 (部署在 GPU 机器上)
│   ├── remote_tts_client.py     # HTTP 客户端 (本地调用)
│   └── tts_router.py            # 路由层 (远程优先, 本地回退)
├── core/
│   ├── agent.py                  # 调用 TTSEngineRouter
│   ├── server.py                 # WebSocket + HTTP 端点
│   └── utils/
│       └── voice_provider_manager.py  # 语音供应商 CRUD
└── ai_configuration/
    └── voice_providers.json      # 远程 TTS 配置 (持久化)
```

### voice_providers.json

```json
{
  "tts_providers": [
    {
      "id": "tts_1e0b9e5e",
      "name": "autoDL_tts",
      "url": "https://u834247-a589-7d0d0f30.bjb1.seetacloud.com:8443",
      "api_key": "",
      "provider_type": "remote",
      "config": {}
    }
  ],
  "active_tts_provider_id": "tts_1e0b9e5e",
  "tts_fallback_to_local": true
}
```

### 调用链路

```
用户发送消息
    │
    ▼
server.py → agent.agent_loop() → LLM 流式生成
    │
    ▼ (检测到句子边界: 。！？.!?)
server.py → 12 步 markdown 清洗 → 过滤 (≥4 有效字符)
    │
    ▼
agent.synthesize_speech(clean_text)
    │
    ▼
TTSEngineRouter.synthesize()
    │
    ├── 有活跃远程供应商 → RemoteTTSEngine
    │   POST https://u834247...seetacloud.com:8443/api/tts/synthesize
    │   ├── 成功 → return (wav, "remote")
    │   └── 失败 + fallback=True → 回退本地
    │
    └── 无远程供应商 → 本地 TTSEngine → return (wav, "local")
    │
    ▼
server.py → WebSocket stream_audio → 前端播放
```

### 回退机制

```python
# tts_router.py
def synthesize(text, ...) -> tuple[bytes|None, str]:
    active = vpm.get_active_tts()
    if active:
        try:
            wav = remote_client.synthesize(text)  # HTTPS 调用
            return (wav, "remote")
        except Exception:
            if vpm.get_tts_fallback():
                wav = local_engine.synthesize(text)  # 回退本地
                return (wav, "fallback")
    return (None, "none")
```

回退时通过 WebSocket 推送 `status` 消息通知前端：
> ⚠ 远程 TTS 不可用，已回退到本地引擎

## 七、AutoDL 注意事项

### 端口映射

AutoDL 实例没有独立公网 IP。平台为每个实例的 **6006** 端口映射了 HTTPS 公网访问地址（通过"自定义服务"功能查看）。

### 实例关机

- AutoDL 实例关机后 TTS 服务会停止
- 重新开机后需要 SSH 进去手动启动：`screen -dmS tts bash /root/run_tts_server.sh`
- 公网 URL **不会变化**（同一实例）

### 磁盘空间

```bash
# 清理 conda 缓存 (释放 ~5GB)
conda clean -y --all

# 清理 pip 缓存
rm -rf /root/.cache/pip/ /tmp/pip-*

# 查看磁盘使用
df -h /
```

### 环境说明

| 环境 | 路径 | 用途 |
|------|------|------|
| base | `/root/miniconda3/bin/python` | **TTS 服务** (306 包, CosyVoice 完整适配) |
| cosy | `/root/miniconda3/envs/cosy/bin/python` | 备用 (依赖不全, 需要 LD_PRELOAD) |

TTS 服务使用 **base 环境**，无需 `conda activate`，无需 `LD_PRELOAD`。

## 八、故障排查

| 现象 | 原因 | 解决 |
|------|------|------|
| 健康检查超时 | AutoDL 实例已关机 | 登录 AutoDL 控制台开机 |
| 健康检查返回 503 | 模型未加载 | `screen -X -S tts quit && screen -dmS tts bash /root/run_tts_server.sh` |
| 合成超时 | GPU 被其他任务占用 | `nvidia-smi` 检查 VRAM |
| `ModuleNotFoundError` | 依赖缺失 | 在 base 环境安装缺失包 |
| 公网 URL 变化 | 实例重建 | 更新 `voice_providers.json` 中的 URL |

### 日志查看

```bash
# 服务运行日志
tail -100 /tmp/tts_server.log

# 实时日志
screen -r tts

# GPU 状态
watch -n 1 nvidia-smi
```

---

**最后更新**: 2026-06-11
