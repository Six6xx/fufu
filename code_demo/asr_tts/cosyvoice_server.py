"""
code_demo/asr_tts/cosyvoice_server.py — CosyVoice 远程合成服务

独立 FastAPI 服务，部署在 GPU 机器上（远程服务器或本地 CUDA Docker）。
接收 HTTP 请求，调用本地 CosyVoice3 模型合成语音，返回 WAV 音频。

用法:
    # 基础用法（需要每次传参考音频）
    python -m code_demo.asr_tts.cosyvoice_server --port 9876 --model /path/to/model

    # 微调模型用法（预注册说话人，后续推理免参考音频）
    python -m code_demo.asr_tts.cosyvoice_server --model /path/to/model \
        --spk-id fufu --spk-prompt-wav /path/to/ref.wav \
        --spk-prompt-text "You are a helpful assistant.<|endofprompt|>参考音频文本"

    # 带默认参考音频
    python -m code_demo.asr_tts.cosyvoice_server --model /path/to/model --prompt-wav /path/to/ref.wav

部署方式:
    # 远程 GPU 服务器
    python -m code_demo.asr_tts.cosyvoice_server --host 0.0.0.0 --port 6006

    # 本地 Docker (CUDA)
    docker run --gpus all -p 9876:9876 cosyvoice-service
"""

import argparse
import base64
import io
import os
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

import numpy as np
import torch
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel


# ── 请求模型 ──

class SynthesizeRequest(BaseModel):
    text: str  # 需要合成语音的文本
    prompt_text: Optional[str] = None  # 零样本克隆使用的参考文本
    prompt_audio: Optional[str] = None  # base64 编码的 WAV 数据
    speed: float = 1.0  # 合成语音的播放速度倍率
    spk_id: Optional[str] = None  # 指定说话人 ID（使用预注册的说话人）


# ── 全局状态 ──

_model = None
_sample_rate = 22050
_device = "cuda"
_model_dir = ""
_default_prompt_wav = None
_default_prompt_text = None
_load_time = 0.0
_registered_spk_id = None  # 预注册的说话人 ID
_spk_id = None
_spk_prompt_wav = None
_spk_prompt_text = None


# ── FastAPI 应用 ──

app = FastAPI(title="CosyVoice TTS Service", version="1.0.0")

# 允许跨域（方便本地开发调试）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 辅助函数 ──

def _tensor_to_wav(audio_tensor: torch.Tensor) -> bytes:
    """Tensor → WAV bytes（与 tts.py TTSEngine._tensor_to_wav_bytes 相同）。"""
    max_val = audio_tensor.abs().max()
    if max_val > 0:
        audio_tensor = audio_tensor / max_val
    audio_np = audio_tensor.cpu().squeeze().numpy()
    audio_int16 = (audio_np * 32767).astype(np.int16)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(_sample_rate)
        wf.writeframes(audio_int16.tobytes())
    buffer.seek(0)
    return buffer.read()


def _load_model(model_dir: str):
    """加载 CosyVoice3 模型。"""
    global _model, _sample_rate, _device, _load_time

    model_path = Path(model_dir)
    if not model_path.is_dir():
        print(f"[CosyVoiceServer] 错误：模型目录不存在: {model_dir}")
        sys.exit(1)

    # CosyVoice 源码路径
    cosyvoice_src = os.getenv("COSYVOICE_SRC", "")
    if not cosyvoice_src:
        model_parent = model_path.parent
        candidate = model_parent / "cosyvoice3-0.5B" / "CosyVoice"
        if candidate.is_dir():
            cosyvoice_src = str(candidate)
        else:
            candidate = model_parent.parent / "cosyvoice3-0.5B" / "CosyVoice"
            if candidate.is_dir():
                cosyvoice_src = str(candidate)

    if not cosyvoice_src or not Path(cosyvoice_src).is_dir():
        print(f"[CosyVoiceServer] 错误：CosyVoice 源码未找到")
        print(f"[CosyVoiceServer] 请设置 COSYVOICE_SRC 环境变量指向 CosyVoice 源码目录")
        sys.exit(1)

    cosyvoice_src = Path(cosyvoice_src)
    matcha_path = cosyvoice_src / "third_party" / "Matcha-TTS"
    for p in [str(cosyvoice_src), str(matcha_path)]:
        if p and p not in sys.path:
            sys.path.insert(0, p)

    print(f"[CosyVoiceServer] CosyVoice 源码: {cosyvoice_src}")

    from cosyvoice.cli.cosyvoice import AutoModel

    print(f"[CosyVoiceServer] 正在加载模型: {model_dir}")
    print(f"[CosyVoiceServer] 推理设备: {_device}")

    t0 = time.time()
    _model = AutoModel(model_dir=model_dir)
    _load_time = time.time() - t0

    # 获取采样率
    try:
        _sample_rate = _model.sample_rate
    except AttributeError:
        _sample_rate = 22050

    print(f"[CosyVoiceServer] 模型加载完成 ({_load_time:.1f}s, "
          f"采样率: {_sample_rate}, 设备: {_device})")


def _register_speaker(spk_id: str, prompt_wav: str, prompt_text: str) -> bool:
    """一次性注册说话人，提取并缓存特征张量到 spk2info.pt。

    注册后，后续推理只需传 zero_shot_spk_id，无需每次提供参考音频。
    """
    global _registered_spk_id

    # 检查是否已有缓存
    spk2info_path = Path(_model_dir) / "spk2info.pt"
    if spk2info_path.exists():
        print(f"[CosyVoiceServer] 发现已有 spk2info.pt，直接加载说话人 '{spk_id}'")
        _registered_spk_id = spk_id
        return True

    if not prompt_wav or not Path(prompt_wav).is_file():
        print(f"[CosyVoiceServer] 警告: 参考音频不存在 ({prompt_wav})，跳过说话人注册")
        return False

    if not prompt_text:
        print(f"[CosyVoiceServer] 警告: 未提供参考文本，跳过说话人注册")
        return False

    try:
        print(f"[CosyVoiceServer] 正在注册说话人 '{spk_id}'...")
        _model.add_zero_shot_spk(prompt_text, prompt_wav, spk_id)
        _model.save_spkinfo()
        _registered_spk_id = spk_id
        print(f"[CosyVoiceServer] 说话人 '{spk_id}' 注册成功，已保存到 spk2info.pt")
        return True
    except Exception as e:
        print(f"[CosyVoiceServer] 说话人注册失败: {e}")
        return False


# ── 端点 ──

@app.on_event("startup")
async def startup():
    """启动时加载模型并注册说话人。"""
    if _model_dir:
        _load_model(_model_dir)
        # 注册说话人（如果有配置）
        if _spk_id and _spk_prompt_wav:
            _register_speaker(_spk_id, _spk_prompt_wav, _spk_prompt_text or "")
        elif _spk_id:
            # 没有参考音频，尝试从已有 spk2info.pt 加载
            spk2info_path = Path(_model_dir) / "spk2info.pt"
            if spk2info_path.exists():
                _register_speaker(_spk_id, "", "")
            else:
                print(f"[CosyVoiceServer] 警告: 未提供参考音频且无 spk2info.pt 缓存")
    else:
        print("[CosyVoiceServer] 警告：未指定模型目录，服务将在首次请求时加载")


@app.post("/api/tts/synthesize")
async def synthesize(req: SynthesizeRequest):
    """
    合成语音，返回 WAV 字节。

    推理模式优先级:
    1. 请求中传了 prompt_audio → 使用上传的参考音频
    2. 请求中传了 spk_id 或预注册了 _registered_spk_id → 快速路径（无需参考音频）
    3. 服务端配置了 --prompt-wav → 使用默认参考音频
    4. 都没有 → 400 错误
    """
    global _model

    if _model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    # 处理参考音频
    prompt_wav_path = _default_prompt_wav
    prompt_text = req.prompt_text or _default_prompt_text

    temp_wav_path = None
    if req.prompt_audio:
        try:
            audio_bytes = base64.b64decode(req.prompt_audio)
            tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
            tmp.write(audio_bytes)
            tmp.close()
            temp_wav_path = tmp.name
            prompt_wav_path = temp_wav_path
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"参考音频解码失败: {e}")

    # 确定说话人 ID
    spk_id = req.spk_id or _registered_spk_id

    try:
        t0 = time.time()
        audio_chunks = []

        if prompt_wav_path and Path(prompt_wav_path).is_file():
            # ── 传统模式：有参考音频 ──
            print(f"[CosyVoiceServer] 推理模式: zero_shot (参考音频: {prompt_wav_path})")
            for result in _model.inference_zero_shot(
                req.text,
                prompt_text or "",
                prompt_wav_path,
                speed=req.speed,
                stream=False,
            ):
                audio_chunks.append(result["tts_speech"])

        elif spk_id:
            # ── 快速模式：使用预注册说话人 ──
            print(f"[CosyVoiceServer] 推理模式: zero_shot (预注册说话人: {spk_id})")
            for result in _model.inference_zero_shot(
                req.text,
                "",
                "",
                zero_shot_spk_id=spk_id,
                speed=req.speed,
                stream=False,
            ):
                audio_chunks.append(result["tts_speech"])

        else:
            raise HTTPException(
                status_code=400,
                detail="需要参考音频（prompt_audio/prompt_wav）或预注册说话人（--spk-id + --spk-prompt-wav）"
            )

        if not audio_chunks:
            raise HTTPException(status_code=500, detail="合成结果为空")

        # 拼接 + 转 WAV
        full_audio = torch.cat(audio_chunks, dim=-1)
        wav_bytes = _tensor_to_wav(full_audio)

        elapsed = time.time() - t0
        print(f"[CosyVoiceServer] 合成完成: {req.text[:30]}... "
              f"({elapsed:.1f}s, {len(wav_bytes) / 1024:.0f}KB)")

        return Response(content=wav_bytes, media_type="audio/wav")

    except HTTPException:
        raise
    except Exception as e:
        print(f"[CosyVoiceServer] 合成失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # 清理临时文件
        if temp_wav_path and Path(temp_wav_path).exists():
            try:
                Path(temp_wav_path).unlink()
            except OSError:
                pass


@app.get("/api/tts/health")
async def health():
    """健康检查。"""
    return {
        "status": "ok",
        "model_loaded": _model is not None,
        "device": _device,
        "sample_rate": _sample_rate,
        "model_dir": _model_dir,
        "load_time_seconds": round(_load_time, 1),
        "registered_spk_id": _registered_spk_id,
    }


class RegisterSpeakerRequest(BaseModel):
    spk_id: str  # 新说话人的唯一 ID
    prompt_wav: str  # base64 编码的 WAV 数据
    prompt_text: str  # 参考音频对应的文本


@app.post("/api/tts/register-speaker")
async def register_speaker(req: RegisterSpeakerRequest):
    """运行时动态注册新说话人。

    上传参考音频 + 文本 → 提取特征张量 → 保存到 spk2info.pt。
    注册后可通过 spk_id 进行推理，无需再传参考音频。
    """
    if _model is None:
        raise HTTPException(status_code=503, detail="模型未加载")

    # 保存临时 wav
    temp_wav_path = None
    try:
        audio_bytes = base64.b64decode(req.prompt_wav)
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.write(audio_bytes)
        tmp.close()
        temp_wav_path = tmp.name
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"参考音频解码失败: {e}")

    try:
        _model.add_zero_shot_spk(req.prompt_text, temp_wav_path, req.spk_id)
        _model.save_spkinfo()

        global _registered_spk_id
        _registered_spk_id = req.spk_id

        print(f"[CosyVoiceServer] 说话人 '{req.spk_id}' 注册成功")
        return {"ok": True, "spk_id": req.spk_id, "message": "说话人注册成功"}
    except Exception as e:
        print(f"[CosyVoiceServer] 说话人注册失败: {e}")
        raise HTTPException(status_code=500, detail=f"说话人注册失败: {e}")
    finally:
        if temp_wav_path and Path(temp_wav_path).exists():
            try:
                Path(temp_wav_path).unlink()
            except OSError:
                pass


@app.get("/api/tts/info")
async def info():
    """模型详细信息。"""
    gpu_name = "cpu"
    if torch.cuda.is_available():
        gpu_name = torch.cuda.get_device_name(0)

    return {
        "model_dir": _model_dir,
        "sample_rate": _sample_rate,
        "device": _device,
        "gpu_name": gpu_name,
        "cuda_available": torch.cuda.is_available(),
        "model_loaded": _model is not None,
        "supported_formats": ["wav"],
    }


# ── 入口 ──

def main():
    global _model_dir, _device, _default_prompt_wav, _default_prompt_text
    global _spk_id, _spk_prompt_wav, _spk_prompt_text

    parser = argparse.ArgumentParser(description="CosyVoice 远程合成服务")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=9876, help="监听端口")
    parser.add_argument("--model", default="", help="模型目录路径")
    parser.add_argument("--prompt-wav", default="", help="默认参考音频路径")
    parser.add_argument("--prompt-text", default="", help="默认参考音频文本")
    parser.add_argument("--device", default="cuda", choices=["cuda", "cpu"],
                        help="推理设备 (默认 cuda)")
    parser.add_argument("--spk-id", default="", help="预注册说话人 ID")
    parser.add_argument("--spk-prompt-wav", default="", help="说话人注册用参考音频路径")
    parser.add_argument("--spk-prompt-text", default="", help="说话人注册用参考文本")
    args = parser.parse_args()

    _model_dir = args.model
    _device = args.device
    _default_prompt_wav = args.prompt_wav if args.prompt_wav else None
    _default_prompt_text = args.prompt_text if args.prompt_text else None
    _spk_id = args.spk_id if args.spk_id else None
    _spk_prompt_wav = args.spk_prompt_wav if args.spk_prompt_wav else None
    _spk_prompt_text = args.spk_prompt_text if args.spk_prompt_text else None

    # 检查 CUDA
    if _device == "cuda" and not torch.cuda.is_available():
        print("[CosyVoiceServer] 警告：CUDA 不可用，回退到 CPU")
        _device = "cpu"

    if not _model_dir:
        # 尝试从环境变量读取
        _model_dir = os.getenv("TTS_MODEL_PATH", "")
        if not _model_dir:
            print("[CosyVoiceServer] 警告：未指定模型目录 (--model 或 TTS_MODEL_PATH)")

    print(f"[CosyVoiceServer] 配置:")
    print(f"  模型目录: {_model_dir or '(未指定)'}")
    print(f"  设备: {_device}")
    print(f"  参考音频: {_default_prompt_wav or '(无)'}")
    print(f"  参考文本: {_default_prompt_text or '(无)'}")
    print(f"  说话人ID: {_spk_id or '(无)'}")
    print(f"  说话人参考音频: {_spk_prompt_wav or '(无)'}")
    print(f"  监听: {args.host}:{args.port}")

    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
