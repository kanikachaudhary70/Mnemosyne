# Static Assets for Mnemosyne Landing Page & Fallbacks
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
_pub = _root / "public"

try:
    HTML_CONTENT = (_pub / "index.html").read_text(encoding="utf-8") if (_pub / "index.html").exists() else ""
except Exception:
    HTML_CONTENT = "<html><body><h1>Mnemosyne</h1></body></html>"

try:
    CSS_CONTENT = (_pub / "styles.css").read_text(encoding="utf-8") if (_pub / "styles.css").exists() else ""
except Exception:
    CSS_CONTENT = ""

try:
    JS_CONTENT = (_pub / "main.js").read_text(encoding="utf-8") if (_pub / "main.js").exists() else ""
except Exception:
    JS_CONTENT = ""
