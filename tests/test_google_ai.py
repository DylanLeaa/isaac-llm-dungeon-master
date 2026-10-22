import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import google_ai  # noqa: E402


def test_fetch_models_keeps_only_chat_models(monkeypatch):
    """The names are from a real models.list of a Google AI Studio key."""
    listed = {
        "models/gemini-2.5-flash": ["generateContent"], "models/gemma-4-31b-it": ["generateContent"],
        "models/gemini-3.5-flash": ["generateContent"], "models/gemini-3.1-pro-preview-customtools": ["generateContent"],
        "models/gemini-2.5-flash-preview-tts": ["generateContent"], "models/gemini-2.5-flash-image": ["generateContent"],
        "models/gemini-nano-banana-2.1": ["generateContent"], "models/gemini-3.5-transcribe": ["generateContent"],
        "models/gemini-robotics-er-2-preview": ["generateContent"], "models/gemini-3.8-live": ["bidiGenerateContent"],
        "models/gemini-embedding-2": ["embedContent"], "models/deep-research-preview-04-2026": ["generateContent"],
        "models/veo-3.1-generate-preview": ["predictLongRunning"], "models/aqa": ["generateAnswer"]}

    class Resp:
        def raise_for_status(self): pass
        def json(self): return {"models": [{"name": n, "supportedGenerationMethods": m} for n, m in listed.items()]}

    class Client:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def get(self, url, params=None, headers=None):
            assert headers == {"x-goog-api-key": "k"}
            return Resp()

    import httpx
    monkeypatch.setattr(httpx, "Client", Client)
    assert [m["name"] for m in google_ai.fetch_models("k")] == [
        "models/gemini-2.5-flash", "models/gemma-4-31b-it", "models/gemini-3.5-flash",
        "models/gemini-3.1-pro-preview-customtools"]
