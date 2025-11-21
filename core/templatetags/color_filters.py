from django import template
import re

register = template.Library()

def _clamp(x): 
    return max(0, min(255, int(round(x))))

def _parse_color(s):
    s = (s or "").strip()
    # #RRGGBB
    if s.startswith("#") and len(s) == 7:
        r = int(s[1:3], 16); g = int(s[3:5], 16); b = int(s[5:7], 16)
        return r, g, b, 1.0
    # rgb/rgba
    m = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", s, re.I)
    if m:
        r = float(m.group(1)); g = float(m.group(2)); b = float(m.group(3))
        a = float(m.group(4)) if m.group(4) is not None else 1.0
        return _clamp(r), _clamp(g), _clamp(b), a
    # fallback: intenta #RGB expandido o devuelve None
    if s.startswith("#") and len(s) == 4:
        r = int(s[1]*2, 16); g = int(s[2]*2, 16); b = int(s[3]*2, 16)
        return r, g, b, 1.0
    return None

def _to_hex(r,g,b):
    return f"#{_clamp(r):02x}{_clamp(g):02x}{_clamp(b):02x}"

@register.filter
def darken(color_str, pct=20):
    """Devuelve el mismo color mezclado con negro en pct% (0–100)."""
    try:
        pct = float(pct)
    except:
        pct = 20.0
    rgba = _parse_color(color_str)
    if not rgba:
        # devuelve original para no romper el template
        return color_str
    r,g,b,a = rgba
    factor = 1 - (pct/100.0)  # 20% => 80% del valor original
    dr = r * factor
    dg = g * factor
    db = b * factor
    return _to_hex(dr, dg, db)
