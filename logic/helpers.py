from typing import Dict, Any, List


def summarize_recent_actions(rows) -> str:
    if not rows:
        return "Keine."
    items = []
    for r in rows[:6]:
        items.append(f"R{r[0]}: {r[1]}")
    return " | ".join(items)


def format_external_events(events: List[Dict[str, Any]]) -> str:
    if not events:
        return "Keine."
    lines = []
    for e in events:
        c = int(e.get("craziness", 0) or 0)
        q = (e.get("quote") or "").strip()
        if q:
            lines.append(f"- {e.get('actor')} (crazy={c}/100): {e.get('headline')} — {q}")
        else:
            lines.append(f"- {e.get('actor')} (crazy={c}/100): {e.get('headline')}")
    return "\n".join(lines)


def _arrow(delta: int) -> str:
    if delta >= 3:
        return "⬆️"
    if delta <= -3:
        return "⬇️"
    return "➖"


def impact_preview_text(folgen: Dict[str, Any]) -> str:
    land = (folgen or {}).get("land", {}) or {}
    eu = (folgen or {}).get("eu", {}) or {}

    dm = int(land.get("militär", 0))
    ds = int(land.get("stabilität", 0))
    de = int(land.get("wirtschaft", 0))
    dd = int(land.get("diplomatie", 0))
    dp = int(land.get("öffentliche_zustimmung", 0))
    dcoh = int(eu.get("kohäsion", 0))

    max_abs = max(abs(dm), abs(ds), abs(de), abs(dd), abs(dp), abs(dcoh))
    if max_abs >= 9:
        risk = "Risiko: 🔥 hoch"
    elif max_abs >= 6:
        risk = "Risiko: ⚠️ mittel"
    else:
        risk = "Risiko: ✅ niedrig"

    return (
        f"Mil {_arrow(dm)}  Sta {_arrow(ds)}  Wir {_arrow(de)}  "
        f"Dip {_arrow(dd)}  Zust {_arrow(dp)}  EU {_arrow(dcoh)}  •  {risk}"
    )
