"""Small Qt helpers shared by the entry point and UI."""
from PyQt5.QtGui import QFont, QFontDatabase

_UI_CANDIDATES = ("Segoe UI", "Tahoma", "Arial")
_MONO_CANDIDATES = ("Cascadia Mono", "Lucida Console", "Courier New")


def configure_app_font(app, size=10):
    """Use a Windows-native UI font so Qt does not warn about missing OpenType tables."""
    db = QFontDatabase()
    for fam in _UI_CANDIDATES:
        if fam in db.families():
            app.setFont(QFont(fam, size))
            return fam
    return None


def mono_font(size=10):
    """Monospace font with full OpenType support on Windows."""
    db = QFontDatabase()
    for fam in _MONO_CANDIDATES:
        if fam in db.families():
            return QFont(fam, size)
    f = QFont()
    f.setFamily("monospace")
    f.setStyleHint(QFont.Monospace)
    f.setPointSize(size)
    return f


def mono_css_family():
    """CSS font-family list for QTextEdit stylesheets."""
    db = QFontDatabase()
    found = [f"'{fam}'" for fam in _MONO_CANDIDATES if fam in db.families()]
    if found:
        return ", ".join(found + ["monospace"])
    return "monospace"
