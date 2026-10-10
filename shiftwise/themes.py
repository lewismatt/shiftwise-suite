"""Curated per-user color palettes based on the saved theme examples."""

import json
from pathlib import Path

_PALETTE_FILE = Path(__file__).resolve().parents[1] / "docs" / "theme-palettes.json"
_PALETTE_DATA = json.loads(_PALETTE_FILE.read_text(encoding="utf-8"))

# The original ShiftWise palette remains the default and an explicit choice.
SHIFTWISE = {
    "id": "shiftwise",
    "name": "ShiftWise blue",
    "mood": "The original deep navy and bright blue theme",
    "colors": {
        "background": "#0b1526",
        "surface": "#101d31",
        "surface_soft": "#122036",
        "nav_background": "#081020",
        "accent": "#2e7cf6",
        "accent_dark": "#1c5cd0",
        "accent_soft": "#14294a",
        "heading": "#7fd3e8",
        "text": "#dde7f2",
        "muted": "#6e8299",
        "border": "#24344d",
        "success": "#4dd88a",
        "warning": "#ffcf5f",
        "error": "#ff5a4d",
        "teal_highlight": "#35d4c0",
    },
}
PALETTES = (SHIFTWISE, *_PALETTE_DATA["palettes"])
PALETTES_BY_ID = {palette["id"]: palette for palette in PALETTES}


def css_variables(palette_id):
    """Return CSS variables from the fixed, reviewed palette allowlist."""
    palette = PALETTES_BY_ID.get(palette_id, SHIFTWISE)
    colors = palette["colors"]
    variables = {
        "--bg": colors["background"],
        "--bg-soft": colors["surface_soft"],
        "--card": colors["surface"],
        "--card-hover": colors["surface_soft"],
        "--accent": colors["accent"],
        "--accent-dark": colors["accent_dark"],
        "--accent-soft": colors["accent_soft"],
        "--ink": colors["heading"],
        "--text": colors["text"],
        "--dim": colors["muted"],
        "--line": colors["border"],
        "--ok": colors["success"],
        "--warn": colors["warning"],
        "--navy": colors["nav_background"],
        "--teal": colors["teal_highlight"],
        "--accent-contrast": colors["nav_background"],
    }
    return ";".join(f"{name}:{value}" for name, value in variables.items())
