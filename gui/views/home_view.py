import customtkinter as ctk
from datetime import datetime

from core.automation.browser_manager import provider_label
from gui import theme
from gui.connection_monitor import STATUS_COLORS
from gui.widgets import Card, Chip

class HomeView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        
        self.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(self, text="ListPilot", font=theme.font(26, "bold"),
                                       text_color=theme.TEXT)
        self.title_label.grid(row=0, column=0, padx=theme.PAD, pady=(12, 10), sticky="w")
        self.desc_label = ctk.CTkLabel(
            self, text="Post, track and verify every Marketplace listing. Pick a tool from the sidebar.",
            text_color=theme.TEXT_MUTED, font=theme.font(13), anchor="w", justify="left",
            wraplength=650,
        )
        self.desc_label.grid(row=1, column=0, padx=theme.PAD, pady=(0, 20), sticky="ew")
        self.welcome_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.welcome_frame.grid(row=2, column=0, padx=theme.PAD, pady=(0, theme.GAP), sticky="ew")
        self.welcome_frame.grid_columnconfigure((0, 1), weight=1, uniform="connections")
        self.connection_widgets = {}
        for index, (key, title) in enumerate((("browser", provider_label()), ("sheets", "Google Sheets"))):
            card = Card(self.welcome_frame)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else theme.GAP, 0))
            card.grid_columnconfigure(0, weight=1)
            name = ctk.CTkLabel(card, text=title, font=theme.font(17, "bold"),
                                text_color=theme.TEXT, anchor="w")
            name.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 8))
            chip = Chip(card, "Checking...", theme.AMBER)
            chip.grid(row=1, column=0, sticky="w", padx=16)
            detail = ctk.CTkLabel(card, text="Checking connection...", font=theme.font(13),
                                  text_color=theme.TEXT_MUTED, anchor="nw", justify="left", wraplength=280)
            detail.grid(row=2, column=0, sticky="nsew", padx=16, pady=(10, 8))
            card.grid_rowconfigure(2, weight=1)
            checked = ctk.CTkLabel(card, text="Not checked yet", font=theme.font(11),
                                   text_color=theme.TEXT_MUTED, anchor="w")
            checked.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 16))
            card.bind("<Configure>", lambda event, label=detail: label.configure(
                wraplength=max(160, self._reverse_widget_scaling(event.width) - 32)))
            self.connection_widgets[key] = (name, chip, detail, checked)

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=3, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.GAP))
        self.check_connections_btn = ctk.CTkButton(
            actions, text="Check now", command=lambda: self.winfo_toplevel().refresh_connections(),
            width=130, height=36, fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
            font=theme.font(13, "bold"),
        )
        self.check_connections_btn.grid(row=0, column=0, sticky="w")
        self.settings_btn = ctk.CTkButton(
            actions, text="Connection settings", command=lambda: self.winfo_toplevel().show("settings"),
            width=160, height=36, fg_color=theme.NEUTRAL_BTN, hover_color=theme.NEUTRAL_BTN_HOVER,
            text_color=theme.TEXT, font=theme.font(13),
        )
        self.settings_btn.grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.info_label = ctk.CTkLabel(
            self, text="Connections are checked automatically. No profiles are opened and no sheet rows are changed.",
            font=theme.font(12), text_color=theme.TEXT_MUTED, wraplength=650, justify="left", anchor="w",
        )
        self.info_label.grid(row=4, column=0, sticky="ew", padx=theme.PAD, pady=(0, theme.PAD))

    def update_connections(self, settings, statuses, checking):
        self.connection_widgets["browser"][0].configure(text=settings.provider_name)
        for key, status in statuses.items():
            _, chip, detail, checked = self.connection_widgets[key]
            chip.set(status.label, STATUS_COLORS[status.state])
            detail.configure(text=status.detail)
            text = "Last checked: " + datetime.fromtimestamp(status.checked_at).strftime("%H:%M:%S") if status.checked_at else "Not checked yet"
            checked.configure(text=text)
        self.check_connections_btn.configure(
            state="disabled" if checking else "normal", text="Checking..." if checking else "Check now")
