"""Thin Ollama /api/chat wrapper with token accounting."""
import requests


def chat(host, model, messages, *, num_ctx=8192, num_predict=256,
         temperature=0.8, keep_alive="10m", timeout=600, num_thread=None):
    options = {
        "num_ctx": num_ctx,
        "num_predict": num_predict,
        "temperature": temperature,
    }
    if num_thread:
        options["num_thread"] = num_thread
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": keep_alive,
        "options": options,
    }
    r = requests.post(f"{host}/api/chat", json=payload, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    msg = data.get("message") or {}
    return {
        "content": msg.get("content", "") or "",
        "thinking": msg.get("thinking", "") or "",   # reasoning models emit a separate think channel
        "eval_count": int(data.get("eval_count") or 0),
        "prompt_eval_count": int(data.get("prompt_eval_count") or 0),
        "done_reason": data.get("done_reason", "") or "",
    }


def unload(host, model, timeout=30):
    """Best-effort: evict the model from VRAM (keep_alive=0)."""
    try:
        requests.post(f"{host}/api/generate",
                      json={"model": model, "prompt": "", "keep_alive": 0,
                            "stream": False},
                      timeout=timeout)
    except Exception:
        pass
