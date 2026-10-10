"""Curated per-user color palettes.

The palette catalog is embedded directly as Python data so the runtime has
no dependency on the docs/ directory (which is not copied into the Docker
image). docs/theme-palettes.json remains as the human-readable design record.
"""

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
        "warn_contrast": "#081020",
        "error": "#ff5a4d",
        "teal_highlight": "#35d4c0",
    },
}

CHARIZARD = {
    "id": "charizard",
    "name": "Charizard",
    "mood": "Ember orange, charred night, teal highlights",
    "colors": {
        "background": "#170c08",
        "surface": "#24130d",
        "surface_soft": "#30190f",
        "nav_background": "#0e0604",
        "accent": "#ff8a3d",
        "accent_dark": "#d96524",
        "accent_soft": "#3b1d11",
        "heading": "#ffb347",
        "text": "#f2e6d8",
        "muted": "#a67a5c",
        "border": "#5c2e1a",
        "success": "#7ddd7d",
        "warning": "#ffcf5f",
        "warn_contrast": "#0e0604",
        "error": "#ff5a4d",
        "teal_highlight": "#5fd7c9",
    },
}

FOREST_SAGE = {
    "id": "forest-sage",
    "name": "Forest & sage",
    "mood": "Evergreen, fresh sage, warm gold status accents",
    "colors": {
        "background": "#0d1712",
        "surface": "#14231b",
        "surface_soft": "#1b2d22",
        "nav_background": "#09100c",
        "accent": "#81c784",
        "accent_dark": "#559b60",
        "accent_soft": "#20372a",
        "heading": "#d3dfb4",
        "text": "#e2eadf",
        "muted": "#91a491",
        "border": "#304638",
        "success": "#8bd49a",
        "warning": "#e8c66a",
        "warn_contrast": "#09100c",
        "error": "#ef766c",
        "teal_highlight": "#89cdb8",
    },
}

MIDNIGHT_LAVENDER = {
    "id": "midnight-lavender",
    "name": "Midnight lavender",
    "mood": "Ink violet, soft lilac, mint status accents",
    "colors": {
        "background": "#11101a",
        "surface": "#1a1927",
        "surface_soft": "#232235",
        "nav_background": "#0c0b13",
        "accent": "#a78bfa",
        "accent_dark": "#8064d8",
        "accent_soft": "#29243b",
        "heading": "#d9ccff",
        "text": "#ece9f5",
        "muted": "#a09ab4",
        "border": "#38334e",
        "success": "#70d6a0",
        "warning": "#ffd166",
        "warn_contrast": "#0c0b13",
        "error": "#ff7777",
        "teal_highlight": "#86d6d0",
    },
}

BRIGHT_TEAL = {
    "id": "bright-teal",
    "name": "Bright teal",
    "mood": "Cool white, deep teal, clear high-contrast actions",
    "colors": {
        "background": "#f4f6f8",
        "surface": "#ffffff",
        "surface_soft": "#edf1f5",
        "nav_background": "#ffffff",
        "accent": "#176b87",
        "accent_dark": "#10556d",
        "accent_soft": "#dcecf1",
        "heading": "#173e53",
        "text": "#263744",
        "muted": "#70808c",
        "border": "#d8e0e6",
        "success": "#26845e",
        "warning": "#a86b00",
        "warn_contrast": "#263744",
        "error": "#b33d39",
        "teal_highlight": "#2797a4",
    },
}

PALETTES = (SHIFTWISE, CHARIZARD, FOREST_SAGE, MIDNIGHT_LAVENDER, BRIGHT_TEAL)
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
        "--warn-contrast": colors["warn_contrast"],
        "--navy": colors["nav_background"],
        "--teal": colors["teal_highlight"],
        "--accent-contrast": colors["nav_background"],
    }
    return ";".join(f"{name}:{value}" for name, value in variables.items())
