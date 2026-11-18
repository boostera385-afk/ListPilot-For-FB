import customtkinter as ctk

from core.config import get_setting, set_setting
from core.automation.browser_manager import provider_label
from gui import theme
from gui import animations as anim
from gui.widgets import Chip
from gui.views.home_view import HomeView
from gui.views.settings_view import SettingsView
from gui.views.fb_listing_view import FBListingView
from gui.views.dominancy_checker_view import DominancyCheckerView
from gui.views.response_checker_view import ResponseCheckerView
from gui.views.live_checker_view import LiveCheckerView
from gui.views.script_view import ScriptView
from gui.ui_helpers import start_ui_pump

# (key, icon, label, view class). Settings is pinned to the bottom.
NAV_ITEMS = (
    ("home", "⌂", "Home", HomeView),
    ("fb_listing", "▤", "FB Listings", FBListingView),
    ("dominancy", "◎", "Dominancy Checker", DominancyCheckerView),
    ("response", "✉", "Response Checker", ResponseCheckerView),
    ("live", "◉", "Live Checker", LiveCheckerView),
    ("script", "⟨⟩", "Script Builder", ScriptView),
)
SETTINGS_ITEM = ("settings", "⚙", "Settings", SettingsView)

SIDEBAR_WIDE, SIDEBAR_NARROW = 220, 68
APPEARANCE = {"Light": "light", "Dark": "dark", "System": "system"}


def view_is_busy(view):
    engine = getattr(view, "engine", None)
    return bool(
        getattr(view, "is_processing", False)
        or getattr(view, "visibility_control", None)
        or (engine is not None and getattr(engine, "is_running", False))
    )


class NavButton(ctk.CTkFrame):
    """Sidebar entry: accent bar + icon + label + pulsing 'running' dot."""

    def __init__(self, master, icon, label, command):
        super().__init__(master, fg_color=theme.SIDEBAR, corner_radius=theme.RADIUS_SM, height=42)
        self.grid_columnconfigure(2, weight=1)
        self.command = command
        self.active = False

        self.bar = ctk.CTkFrame(self, width=4, height=22, corner_radius=2, fg_color=theme.SIDEBAR)
        self.bar.grid(row=0, column=0, padx=(4, 6), pady=10)
        self.icon = ctk.CTkLabel(self, text=icon, width=26, font=theme.font(17), text_color=theme.TEXT_MUTED)
        self.icon.grid(row=0, column=1)
        self.label = ctk.CTkLabel(self, text=label, anchor="w", font=theme.font(13), text_color=theme.TEXT_MUTED)
        self.label.grid(row=0, column=2, sticky="w", padx=(8, 4))
        self.dot = ctk.CTkLabel(self, text="", width=14, font=theme.font(11), text_color=theme.GREEN)
        self.dot.grid(row=0, column=3, padx=(0, 8))
        self.pulse = anim.Pulse(self.dot, "text_color", theme.ACCENT_SOFT, theme.GREEN)

        for widget in (self, self.bar, self.icon, self.label, self.dot):
            widget.bind("<Button-1>", lambda _e: self.command())
            widget.bind("<Enter>", lambda _e: self._hover(True))
            widget.bind("<Leave>", lambda _e: self._hover(False))
            try:
                widget.configure(cursor="hand2")
            except Exception:
                pass

    def _hover(self, inside):
        if not self.active:
            target = theme.SURFACE_ALT if inside else theme.SIDEBAR
            self.bar.configure(fg_color=target)
            anim.fade_color(self, "fg_color", theme.SIDEBAR if inside else theme.SURFACE_ALT, target, 140)

    def set_active(self, active):
        self.active = active
        color = theme.ACCENT if active else theme.TEXT_MUTED
        self.configure(fg_color=theme.ACCENT_SOFT if active else theme.SIDEBAR)
        self.bar.configure(fg_color=theme.ACCENT if active else theme.SIDEBAR)
        self.icon.configure(text_color=color)
        self.label.configure(text_color=theme.TEXT if active else theme.TEXT_MUTED,
                             font=theme.font(13, "bold" if active else "normal"))

    def set_busy(self, busy):
        if busy and not self.pulse.active:
            self.dot.configure(text="●")
            self.pulse.start()
        elif not busy and self.pulse.active:
            self.pulse.stop(theme.GREEN)
            self.dot.configure(text="")

    def set_compact(self, compact):
        if compact:
            self.label.grid_remove()
        else:
            self.label.grid()


class App(ctk.CTk):
    def __init__(self):
        super().__init__(fg_color=theme.BG)
        ctk.set_appearance_mode(get_setting("appearance_mode", "system") or "system")

        self.title(f"{theme.APP_NAME} – {theme.TAGLINE}")
        saved_geometry = get_setting("window_geometry", "")
        if saved_geometry:
            self.geometry(saved_geometry)
        else:
            self.geometry("1240x780")
            self.after(250, lambda: self.state("zoomed"))  # fits any screen and Windows scaling
        self.minsize(960, 600)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        self.compact = bool(get_setting("sidebar_compact", False))

        self._build_sidebar()
        self._build_main()

        self.views, self.nav, self.labels = {}, {}, {}
        for key, icon, label, view_cls in NAV_ITEMS + (SETTINGS_ITEM,):
            self.views[key] = view_cls(self.page_host, fg_color=theme.BG)
            self.labels[key] = label
        for row, (key, icon, label, _) in enumerate(NAV_ITEMS, start=2):
            self.nav[key] = NavButton(self.sidebar, icon, label, lambda k=key: self.show(k))
            self.nav[key].grid(row=row, column=0, sticky="ew", padx=10, pady=2)
        key, icon, label, _ = SETTINGS_ITEM
        self.nav[key] = NavButton(self.sidebar, icon, label, lambda: self.show("settings"))
        self.nav[key].grid(row=21, column=0, sticky="ew", padx=10, pady=(2, 6))
        self._apply_compact()

        self.current = None
        self.show("home")

        # Deliver UI updates queued by automation threads.
        start_ui_pump(self)
        self._poll_status()

    # --- Layout ------------------------------------------------------------------
    def _build_sidebar(self):
        self.sidebar = ctk.CTkFrame(self, width=SIDEBAR_WIDE, corner_radius=0, fg_color=theme.SIDEBAR,
                                    border_width=0)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_columnconfigure(0, weight=1)
        self.sidebar.grid_rowconfigure(20, weight=1)

        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.grid(row=0, column=0, sticky="ew", padx=14, pady=(20, 18))
        brand.grid_columnconfigure(1, weight=1)
        self.logo = ctk.CTkLabel(brand, text="✈", width=38, height=38, corner_radius=10,
                                 fg_color=theme.ACCENT, text_color="#FFFFFF", font=theme.font(18, "bold"))
        self.logo.grid(row=0, column=0, rowspan=2)
        self.brand_name = ctk.CTkLabel(brand, text=theme.APP_NAME, font=theme.font(18, "bold"),
                                       text_color=theme.TEXT, anchor="w")
        self.brand_name.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.brand_tag = ctk.CTkLabel(brand, text="Marketplace autopilot", font=theme.font(11),
                                      text_color=theme.TEXT_MUTED, anchor="w")
        self.brand_tag.grid(row=1, column=1, sticky="w", padx=(10, 0))

        self.section_label = ctk.CTkLabel(self.sidebar, text="TOOLS", font=theme.font(10, "bold"),
                                          text_color=theme.TEXT_MUTED, anchor="w")
        self.section_label.grid(row=1, column=0, sticky="w", padx=24, pady=(0, 6))

        self.collapse_btn = ctk.CTkButton(self.sidebar, text="«", height=30, command=self.toggle_sidebar,
                                          fg_color="transparent", hover_color=theme.SURFACE_ALT,
                                          text_color=theme.TEXT_MUTED, font=theme.font(14, "bold"))
        self.collapse_btn.grid(row=22, column=0, sticky="ew", padx=10, pady=(0, 14))

    def _build_main(self):
        main = ctk.CTkFrame(self, corner_radius=0, fg_color=theme.BG)
        main.grid(row=0, column=1, sticky="nsew")
        main.grid_rowconfigure(1, weight=1)
        main.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(main, height=56, corner_radius=0, fg_color=theme.BG)
        header.grid(row=0, column=0, sticky="ew", padx=theme.PAD, pady=(12, 0))
        header.grid_columnconfigure(0, weight=1)
        self.crumb = ctk.CTkLabel(header, text="", font=theme.font(13), text_color=theme.TEXT_MUTED, anchor="w")
        self.crumb.grid(row=0, column=0, sticky="w")
        self.provider_chip = Chip(header, provider_label(), theme.BLUE)
        self.provider_chip.grid(row=0, column=1, padx=(0, 8))
        self.sheets_chip = Chip(header, "Sheets")
        self.sheets_chip.grid(row=0, column=2, padx=(0, 12))
        self.mode_switch = ctk.CTkSegmentedButton(
            header, values=list(APPEARANCE), command=self.set_appearance, font=theme.font(12),
            selected_color=theme.ACCENT, selected_hover_color=theme.ACCENT_HOVER,
        )
        saved = str(get_setting("appearance_mode", "system") or "system").lower()
        self.mode_switch.set(next((k for k, v in APPEARANCE.items() if v == saved), "System"))
        self.mode_switch.grid(row=0, column=3)

        self.page_host = ctk.CTkFrame(main, corner_radius=0, fg_color=theme.BG)
        self.page_host.grid(row=1, column=0, sticky="nsew")

    # --- Navigation ----------------------------------------------------------------
    def show(self, key):
        if key == self.current:
            return
        if self.current:
            self.views[self.current].place_forget()
            self.nav[self.current].set_active(False)
        self.current = key
        self.nav[key].set_active(True)
        self.crumb.configure(text=f"{theme.APP_NAME}  ›  {self.labels[key]}")
        anim.slide_in(self.views[key])

    # Older code may still call these.
    def show_home_view(self): self.show("home")
    def show_fb_listing_view(self): self.show("fb_listing")
    def show_dominancy_view(self): self.show("dominancy")
    def show_response_view(self): self.show("response")
    def show_live_view(self): self.show("live")
    def show_script_view(self): self.show("script")
    def show_settings_view(self): self.show("settings")

    def toggle_sidebar(self):
        self.compact = not self.compact
        set_setting("sidebar_compact", self.compact)
        self._apply_compact()

    def _apply_compact(self):
        start = self.sidebar.winfo_width() if self.sidebar.winfo_ismapped() else None
        end = SIDEBAR_NARROW if self.compact else SIDEBAR_WIDE
        for widget in (self.brand_name, self.brand_tag, self.section_label):
            widget.grid_remove() if self.compact else widget.grid()
        for button in self.nav.values():
            button.set_compact(self.compact)
        self.collapse_btn.configure(text="»" if self.compact else "«  Collapse")
        if start and start > 1:
            anim.animate(self.sidebar, "width", 220,
                         lambda t: self.sidebar.configure(width=round(start + (end - start) * t)))
        else:
            self.sidebar.configure(width=end)

    # --- Appearance and status -------------------------------------------------------
    def set_appearance(self, label):
        mode = APPEARANCE.get(label, "system")
        if self.mode_switch.get() != label:
            self.mode_switch.set(label)
        ctk.set_appearance_mode(mode)
        set_setting("appearance_mode", mode)
        self.after(50, self._refresh_theme)

    def _refresh_theme(self):
        for view in self.views.values():
            if hasattr(view, "refresh_theme"):
                view.refresh_theme()

    def _poll_status(self):
        for key, view in self.views.items():
            self.nav[key].set_busy(view_is_busy(view))
        self._status_ticks = getattr(self, "_status_ticks", 0) + 1
        if self._status_ticks % 5 == 1:  # config is a file read; every ~3 s is plenty
            self.provider_chip.set(provider_label(), theme.BLUE)
            if get_setting("apps_script_url", ""):
                self.sheets_chip.set("Sheets linked", theme.GREEN)
            else:
                self.sheets_chip.set("Sheets not set", theme.RED)
        self.after(600, self._poll_status)

    def _on_close(self):
        try:
            if self.state() == "normal":
                set_setting("window_geometry", self.geometry())
        except Exception:
            pass
        self.destroy()

    def change_appearance_mode_event(self, new_appearance_mode: str):
        ctk.set_appearance_mode(new_appearance_mode)
