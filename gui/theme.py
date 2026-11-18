"""ListPilot design tokens: colors, fonts and spacing shared by every screen.

Colors are (light, dark) tuples, the form CustomTkinter switches on by itself
when the appearance mode changes. Plain strings are used only where Tk needs a
single color (text tags), via ``pick()``.
"""
import customtkinter as ctk

APP_NAME = "ListPilot"
TAGLINE = "Post, track and verify every Marketplace listing."

# --- Surfaces -----------------------------------------------------------------
BG = ("#F4F6FB", "#0F1320")          # window background
SURFACE = ("#FFFFFF", "#171C2C")     # cards
SURFACE_ALT = ("#EEF1F8", "#1E2438") # inputs, table header, hovered rows
SIDEBAR = ("#FFFFFF", "#131829")
BORDER = ("#E1E5EF", "#262D45")

TEXT = ("#141A2E", "#E8ECF7")
TEXT_MUTED = ("#5E6680", "#8E97B3")

# --- Brand / actions ------------------------------------------------------------
ACCENT = ("#3D5AFE", "#5C77FF")
ACCENT_HOVER = ("#2F49D9", "#4A64E8")
ACCENT_SOFT = ("#E3E8FF", "#222B52")
DANGER = ("#E5484D", "#E5484D")
DANGER_HOVER = ("#C93A3F", "#C93A3F")
NEUTRAL_BTN = ("#E6E9F2", "#262D45")
NEUTRAL_BTN_HOVER = ("#D7DBE7", "#30385A")

# --- Status colors (listing states and check results) ---------------------------
GREEN = ("#12A150", "#2BD47A")
AMBER = ("#C77700", "#F5A524")
RED = ("#D93036", "#F2555A")
PURPLE = ("#7A3FD1", "#A87BFF")
BLUE = ("#2563EB", "#5B8CFF")
GRAY = ("#6B7280", "#8E97B3")

STATUS_COLORS = {
    "LIVE": GREEN,
    "OK": GREEN,
    "SEARCH": GREEN,
    "PENDING": AMBER,
    "PARTIAL": AMBER,
    "LINK_ONLY": AMBER,
    "NOT_IN_SEARCH": AMBER,
    "REJECTED": RED,
    "REMOVED": RED,
    "HIDDEN": PURPLE,
    "EXPIRED": GRAY,
    "SOLD": BLUE,
    "DRAFT": GRAY,
    "LOGGED_OUT": RED,
    "CHECKPOINT": RED,
    "ERROR": RED,
    "UNKNOWN": AMBER,
    "ACTION_REQUIRED": AMBER,
    "PARTIAL": AMBER,
    "LAYOUT_UNKNOWN": RED,
    "LOGGED_OUT": RED,
    "CHECKPOINT": RED,
    "NO_MARKETPLACE": RED,
    "TIMEOUT": RED,
}

RADIUS = 14
RADIUS_SM = 8
PAD = 20
GAP = 12


def status_color(code):
    """Color for a status or check-result code such as 'PARTIAL: ...'."""
    key = str(code or "").split(":")[0].strip().upper()
    return STATUS_COLORS.get(key, GRAY)


def is_dark():
    return ctk.get_appearance_mode().lower() == "dark"


def pick(color):
    """The single color of a (light, dark) tuple for the current mode."""
    if isinstance(color, (tuple, list)):
        return color[1] if is_dark() else color[0]
    return color


def font(size=13, weight="normal", family="Segoe UI"):
    return ctk.CTkFont(family=family, size=size, weight=weight)


def mono(size=12):
    return ctk.CTkFont(family="Consolas", size=size)
