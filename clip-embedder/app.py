"""
clip-embedder — isolated CLIP image-embedding microservice.

Deliberately its own process/venv (torch + open_clip live only here) so no
other phixtra-app service takes on this dependency weight. Loads the model
once at startup; exposes a single embedding endpoint. Internal-only — no
auth, bind to 127.0.0.1.
"""

import io
import os

import numpy as np
import open_clip
import torch
from fastapi import FastAPI, File, HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

MODEL_NAME = os.getenv("CLIP_MODEL_NAME", "ViT-B-32-quickgelu")
PRETRAINED = os.getenv("CLIP_PRETRAINED", "openai")
MIN_DIMENSION = int(os.getenv("CLIP_MIN_DIMENSION", "150"))

app = FastAPI(title="PhiXtra CLIP Embedder")

_model = None
_preprocess = None
_device = "cpu"


@app.on_event("startup")
def _load_model():
    global _model, _preprocess
    torch.set_num_threads(max(1, os.cpu_count() or 1))
    model, _, preprocess = open_clip.create_model_and_transforms(
        MODEL_NAME, pretrained=PRETRAINED
    )
    model.eval()
    _model = model
    _preprocess = preprocess
    print(f"✅ [CLIP] loaded {MODEL_NAME}/{PRETRAINED} on {_device}")


@app.get("/health")
def health():
    return {"status": "ok", "model": MODEL_NAME, "pretrained": PRETRAINED, "loaded": _model is not None}


def _embed(image: Image.Image) -> list[float]:
    tensor = _preprocess(image).unsqueeze(0)
    with torch.no_grad():
        features = _model.encode_image(tensor)
        features = features / features.norm(dim=-1, keepdim=True)
    return features.squeeze(0).cpu().numpy().astype(np.float32).tolist()


@app.post("/embed-image")
def embed_image(file: UploadFile = File(...)):
    """
    Sync def, not async — _embed() below is blocking CPU-bound torch
    inference with no internal await points. An async route would block
    this service's event loop for the full inference duration, delaying
    concurrent /health checks. FastAPI runs sync routes in a thread pool.
    """
    raw = file.file.read()

    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except UnidentifiedImageError:
        raise HTTPException(status_code=422, detail="undecodable_image")
    except Exception:
        raise HTTPException(status_code=422, detail="undecodable_image")

    width, height = image.size
    if width < MIN_DIMENSION or height < MIN_DIMENSION:
        raise HTTPException(status_code=422, detail="image_too_small")

    image = image.convert("RGB")

    try:
        embedding = _embed(image)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"embedding_failed: {exc}")

    return {"embedding": embedding, "dim": len(embedding), "width": width, "height": height}
