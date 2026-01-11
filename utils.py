# utils.py
import json
import re
from typing import Any


def content_to_text(content) -> str:
    """mistralai SDK kann content als str oder Liste von Parts liefern."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            t = getattr(p, "text", None)
            if t:
                parts.append(t)
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(content)


def parse_json_maybe(text: str) -> Any:
    """Parst JSON auch dann, wenn ```json ... ``` oder Text drumherum vorkommt."""
    s = (text or "").strip()
    if not s:
        raise ValueError("Leere Antwort vom Modell (kein JSON erhalten).")

    # Codefences entfernen
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\s*```$", "", s)

    # Direkt versuchen
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    # Erstes JSON-Objekt/Array extrahieren
    m = re.search(r"(\{.*\}|\[.*\])", s, flags=re.DOTALL)
    if not m:
        raise ValueError(f"Kein JSON gefunden. Anfang der Antwort: {s[:200]!r}")
    return json.loads(m.group(1))


def clamp_int(x: int, lo: int = 0, hi: int = 100) -> int:
    return max(lo, min(hi, int(x)))
