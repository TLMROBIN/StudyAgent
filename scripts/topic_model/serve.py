"""局域网内的话题决策模型推理服务，供 backend 的 TOPIC_MODEL_URL 调用。

    TOPIC_MODEL_DIR=models/topic-laya-v1 TOPIC_MODEL_DEVICE=cuda:1 \
        uvicorn --app-dir scripts/topic_model serve:app --host 0.0.0.0 --port 8090 --workers 1

接口：POST /classify {"subject","text","context","kind"} -> {"p_academic","model_version","latency_ms"}
只返回概率，不做放行/拒绝决定；分档阈值在 backend 配置（TOPIC_MODEL_*_THRESHOLD）。
只在局域网/容器网络监听，不要暴露到公网。
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

import torch
from fastapi import FastAPI
from pydantic import BaseModel, Field
from topic_model_common import LABELS, LAYA_TOPIC_QUESTIONS, format_input

MODEL_DIR = Path(os.environ.get("TOPIC_MODEL_DIR", "models/topic-model"))
DEVICE = os.environ.get("TOPIC_MODEL_DEVICE", "cuda:1" if torch.cuda.is_available() else "cpu")
TRUST_REMOTE_CODE = os.environ.get("TOPIC_MODEL_TRUST_REMOTE_CODE", "").lower() in {"1", "true", "yes"}

meta = json.loads((MODEL_DIR / "topic_model_meta.json").read_text(encoding="utf-8"))
if meta.get("labels") != list(LABELS):
    raise RuntimeError(f"模型标签 {meta.get('labels')} 与 {LABELS} 不一致")
MODEL_VERSION = meta.get("model_version", MODEL_DIR.name)
MAX_LENGTH = int(meta.get("max_length", 256))
ACADEMIC_INDEX = LABELS.index("academic")

IS_LAYA = meta.get("backend") == "laya"
if IS_LAYA:
    from laya import Agent

    model = Agent(str(MODEL_DIR), device=DEVICE)
else:
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(MODEL_DIR, trust_remote_code=TRUST_REMOTE_CODE)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_DIR, trust_remote_code=TRUST_REMOTE_CODE).to(DEVICE).eval()
_lock = threading.Lock()

class ClassifyRequest(BaseModel):
    subject: str = Field(default="", max_length=32)
    text: str = Field(max_length=4000)
    context: str = Field(default="", max_length=2000)
    kind: str = Field(default="input", pattern="^(input|output)$")


def classify(request: ClassifyRequest) -> dict:
    started = perf_counter()
    text = format_input(request.subject, request.text, request.context or None, request.kind)
    with _lock, torch.inference_mode():
        if IS_LAYA:
            result = model.system_one(text, LAYA_TOPIC_QUESTIONS)
            p_academic = result["answers"]["topic"]["probabilities"]["academic"]
        else:
            encoded = tokenizer(text, truncation=True, max_length=MAX_LENGTH, return_tensors="pt").to(DEVICE)
            logits = model(**encoded).logits.float()
            p_academic = torch.softmax(logits, dim=-1)[0, ACADEMIC_INDEX].item()
    return {
        "p_academic": round(p_academic, 6),
        "model_version": MODEL_VERSION,
        "latency_ms": round((perf_counter() - started) * 1000, 2),
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    classify(ClassifyRequest(subject="数学", text="如何求函数的定义域？"))
    yield


app = FastAPI(title="StudyAgent topic model", docs_url=None, redoc_url=None, lifespan=lifespan)
app.post("/classify")(classify)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "model_version": MODEL_VERSION, "device": DEVICE}
