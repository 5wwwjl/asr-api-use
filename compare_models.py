#!/usr/bin/env python3
"""Compare ASR models on a WAV file. Usage: python3 compare_models.py <wav_file>"""
import sys, time, json
from funasr import AutoModel

def transcribe(model_dir, wav_path, device="cuda:0", label=""):
    t0 = time.time()
    model = AutoModel(model=model_dir, trust_remote_code=True, device=device, disable_update=True)
    result = model.generate(input=wav_path)
    text = result[0].get("text", "").strip() if result else ""
    elapsed = (time.time() - t0) * 1000
    # Clean up model to free memory
    del model
    return text, elapsed

if __name__ == "__main__":
    wav = sys.argv[1] if len(sys.argv) > 1 else "/tmp/test.wav"
    
    # Original model (GPU)
    print("=== 原始模型 (Paraformer-large-contextual, GPU) ===")
    text1, t1 = transcribe(
        "/workspace/models/damo/speech_paraformer-large-contextual_asr_nat-zh-cn-16k-common-vocab8404",
        wav, "cuda:0", "original"
    )
    print(f"  结果: {text1}")
    print(f"  耗时: {t1:.0f}ms")
    
    # Fine-tuned model (CPU to avoid OOM)
    print()
    print("=== 微调模型 (消防场景, CPU) ===")
    text2, t2 = transcribe(
        "/workspace/models/paraformer-fire-rescue-v1",
        wav, "cpu", "finetuned"
    )
    print(f"  结果: {text2}")
    print(f"  耗时: {t2:.0f}ms")
    
    print()
    print("=== 对比 ===")
    print(f"  原始:   {text1}")
    print(f"  微调:   {text2}")
    if text1 != text2:
        print(f"  [不同!]")
    else:
        print(f"  [相同]")
