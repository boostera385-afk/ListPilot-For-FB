import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
from core.config import get_setting, set_setting
from gui import theme

class SettingsView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self.grid_columnconfigure(0, weight=1)
        # Let the settings container take the remaining height so it can scroll.
        self.grid_rowconfigure(1, weight=1)

        # Title
        self.title_label = ctk.CTkLabel(self, text="Settings", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="nw")

        # Settings Container (scrollable so nothing gets clipped on small windows)
        self.settings_frame = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.settings_frame.grid(row=1, column=0, padx=20, pady=10, sticky="nsew")
        self.settings_frame.grid_columnconfigure(0, weight=1)

        # --- Browser Provider Setting ---
        self.provider_label = ctk.CTkLabel(self.settings_frame, text="Browser:", font=ctk.CTkFont(weight="bold"))
        self.provider_label.grid(row=0, column=0, sticky="w", pady=(0, 5))

        self.provider_desc = ctk.CTkLabel(self.settings_frame, text="Which anti-detect browser to use for all automations.", text_color="gray")
        self.provider_desc.grid(row=1, column=0, sticky="w", pady=(0, 10))

        # Map between stored keys and display labels.
        self._provider_labels = {"incogniton": "Incogniton", "adspower": "AdsPower"}
        self._provider_keys = {v: k for k, v in self._provider_labels.items()}

        current_provider = str(get_setting("browser_provider", "incogniton") or "incogniton").lower()
        current_provider_label = self._provider_labels.get(current_provider, "Incogniton")

        self.provider_var = tk.StringVar(value=current_provider_label)
        self.provider_menu = ctk.CTkOptionMenu(
            self.settings_frame,
            values=list(self._provider_labels.values()),
            variable=self.provider_var,
        )
        self.provider_menu.set(current_provider_label)
        self.provider_menu.grid(row=2, column=0, sticky="w", pady=(0, 15))

        # --- Incogniton Path Setting ---
        self.path_label = ctk.CTkLabel(self.settings_frame, text="Incogniton Executable Path:", font=ctk.CTkFont(weight="bold"))
        self.path_label.grid(row=3, column=0, sticky="w", pady=(0, 5))

        self.path_desc = ctk.CTkLabel(self.settings_frame, text="Set this if the app cannot automatically find Incogniton.", text_color="gray")
        self.path_desc.grid(row=4, column=0, sticky="w", pady=(0, 10))

        self.path_input_frame = ctk.CTkFrame(self.settings_frame, fg_color="transparent")
        self.path_input_frame.grid(row=5, column=0, sticky="ew")
        self.path_input_frame.grid_columnconfigure(0, weight=1)

        self.path_entry = ctk.CTkEntry(self.path_input_frame, placeholder_text="C:\\Program Files\\Incogniton\\Incogniton.exe")
        self.path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        current_path = get_setting("incogniton_path", "")
        if current_path:
            self.path_entry.insert(0, current_path)

        self.browse_btn = ctk.CTkButton(self.path_input_frame, text="Browse", width=80, command=self.on_browse)
        self.browse_btn.grid(row=0, column=1)

        # --- AdsPower Path Setting ---
        self.ads_path_label = ctk.CTkLabel(self.settings_frame, text="AdsPower Executable Path:", font=ctk.CTkFont(weight="bold"))
        self.ads_path_label.grid(row=6, column=0, sticky="w", pady=(20, 5))

        self.ads_path_desc = ctk.CTkLabel(self.settings_frame, text="Set this if the app cannot automatically find AdsPower.", text_color="gray")
        self.ads_path_desc.grid(row=7, column=0, sticky="w", pady=(0, 10))

        self.ads_path_input_frame = ctk.CTkFrame(self.settings_frame, fg_color="transparent")
        self.ads_path_input_frame.grid(row=8, column=0, sticky="ew")
        self.ads_path_input_frame.grid_columnconfigure(0, weight=1)

        self.ads_path_entry = ctk.CTkEntry(self.ads_path_input_frame, placeholder_text="C:\\Program Files\\AdsPower Global\\AdsPower Global.exe")
        self.ads_path_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))

        current_ads_path = get_setting("adspower_path", "")
        if current_ads_path:
            self.ads_path_entry.insert(0, current_ads_path)

        self.ads_browse_btn = ctk.CTkButton(self.ads_path_input_frame, text="Browse", width=80, command=self.on_browse_adspower)
        self.ads_browse_btn.grid(row=0, column=1)

        # --- AdsPower API Key Setting ---
        self.ads_key_label = ctk.CTkLabel(self.settings_frame, text="AdsPower API Key:", font=ctk.CTkFont(weight="bold"))
        self.ads_key_label.grid(row=9, column=0, sticky="w", pady=(20, 5))

        self.ads_key_desc = ctk.CTkLabel(self.settings_frame, text="Required by AdsPower. Get it from AdsPower -> Settings -> API -> API Key.", text_color="gray")
        self.ads_key_desc.grid(row=10, column=0, sticky="w", pady=(0, 10))

        self.ads_key_entry = ctk.CTkEntry(self.settings_frame, placeholder_text="Paste your AdsPower API key", show="*")
        self.ads_key_entry.grid(row=11, column=0, sticky="ew", padx=(0, 0))

        current_ads_key = get_setting("adspower_api_key", "")
        if current_ads_key:
            self.ads_key_entry.insert(0, current_ads_key)

        # --- AdsPower Profile OS Setting ---
        self.ads_os_label = ctk.CTkLabel(self.settings_frame, text="AdsPower Profile OS:", font=ctk.CTkFont(weight="bold"))
        self.ads_os_label.grid(row=12, column=0, sticky="w", pady=(20, 5))

        self.ads_os_desc = ctk.CTkLabel(self.settings_frame, text="Operating system used when creating new AdsPower profiles.", text_color="gray")
        self.ads_os_desc.grid(row=13, column=0, sticky="w", pady=(0, 10))

        self._os_labels = {"android": "Android", "windows": "Windows"}
        self._os_keys = {v: k for k, v in self._os_labels.items()}

        current_os = str(get_setting("adspower_os", "android") or "android").lower()
        current_os_label = self._os_labels.get(current_os, "Android")

        self.ads_os_var = tk.StringVar(value=current_os_label)
        self.ads_os_menu = ctk.CTkOptionMenu(
            self.settings_frame,
            values=list(self._os_labels.values()),
            variable=self.ads_os_var,
        )
        self.ads_os_menu.grid(row=14, column=0, sticky="w", pady=(0, 15))

        # --- Apps Script URL Setting ---
        self.url_label = ctk.CTkLabel(self.settings_frame, text="Google Apps Script Web App URL:", font=ctk.CTkFont(weight="bold"))
        self.url_label.grid(row=15, column=0, sticky="w", pady=(20, 5))

        self.url_desc = ctk.CTkLabel(self.settings_frame, text="Used to connect to Google Sheets for all automations.", text_color="gray")
        self.url_desc.grid(row=16, column=0, sticky="w", pady=(0, 10))

        self.url_entry = ctk.CTkEntry(self.settings_frame, placeholder_text="https://script.google.com/macros/s/...")
        self.url_entry.grid(row=17, column=0, sticky="ew", padx=(0, 0))

        current_url = get_setting("apps_script_url", "")
        if current_url:
            self.url_entry.insert(0, current_url)

        # --- Live Checker visibility accounts ---
        self.checker_label = ctk.CTkLabel(self.settings_frame, text="Live Checker – Buyer checks", font=theme.font(17, "bold"))
        self.checker_label.grid(row=18, column=0, sticky="w", pady=(20, 5))

        self.checker_desc = ctk.CTkLabel(
            self.settings_frame,
            text="Owner listing counts are always scanned. Buyer checks are optional samples.\n"
                 "Switches apply to the next run; turning a checker off keeps its saved profile ID.",
            text_color=theme.TEXT_MUTED, justify="left", font=theme.font(12),
        )
        self.checker_desc.grid(row=19, column=0, sticky="w", pady=(0, 10))

        buyer_box = ctk.CTkFrame(self.settings_frame, fg_color=theme.SURFACE, corner_radius=theme.RADIUS)
        buyer_box.grid(row=20, column=0, sticky="ew", pady=(0, 12))
        buyer_box.grid_columnconfigure(0, weight=1)
        self.buyers_enabled_var = tk.BooleanVar(value=get_setting("live_check_buyers_enabled", True))
        self.buyers_switch = ctk.CTkSwitch(
            buyer_box, text="Run buyer visibility checks", variable=self.buyers_enabled_var,
            command=self._update_buyer_controls, progress_color=theme.ACCENT, font=theme.font(14, "bold"),
        )
        self.buyers_switch.grid(row=0, column=0, sticky="w", padx=16, pady=(16, 12))
        self.pk_enabled_var = tk.BooleanVar(value=get_setting("live_check_pk_enabled", True))
        self.pk_switch = ctk.CTkSwitch(
            buyer_box, text="PK buyer", variable=self.pk_enabled_var, command=self._update_buyer_controls,
            progress_color=theme.ACCENT, font=theme.font(13),
        )
        self.pk_switch.grid(row=1, column=0, sticky="w", padx=16, pady=(0, 5))
        self.pk_checker_entry = ctk.CTkEntry(buyer_box, placeholder_text="PK checker: exact profile name or ID")
        self.pk_checker_entry.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 12))
        self.pk_checker_entry.insert(0, get_setting("live_check_pk_profile", "") or "")

        self.us_enabled_var = tk.BooleanVar(value=get_setting("live_check_us_enabled", True))
        self.us_switch = ctk.CTkSwitch(
            buyer_box, text="US buyer", variable=self.us_enabled_var, command=self._update_buyer_controls,
            progress_color=theme.ACCENT, font=theme.font(13),
        )
        self.us_switch.grid(row=3, column=0, sticky="w", padx=16, pady=(0, 5))
        self.us_checker_entry = ctk.CTkEntry(buyer_box, placeholder_text="US checker: exact profile name or ID")
        self.us_checker_entry.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 16))
        self.us_checker_entry.insert(0, get_setting("live_check_us_profile", "") or "")

        self.sample_sizes = {"Quick (3 listings)": 3, "Standard (5 listings)": 5, "Extended (15 listings)": 15}
        sample = get_setting("live_check_sample_size", 3)
        self.sample_var = tk.StringVar(value=next((key for key, value in self.sample_sizes.items() if value == sample), "Quick (3 listings)"))
        sample_box = ctk.CTkFrame(self.settings_frame, fg_color="transparent")
        sample_box.grid(row=21, column=0, sticky="ew")
        ctk.CTkLabel(sample_box, text="Live listings per seller, per enabled buyer",
                     font=theme.font(12), text_color=theme.TEXT_MUTED).grid(row=0, column=0, sticky="w")
        self.sample_menu = ctk.CTkOptionMenu(sample_box, values=list(self.sample_sizes), variable=self.sample_var, width=230)
        self.sample_menu.grid(row=1, column=0, sticky="w", pady=(4, 4))
        ctk.CTkLabel(sample_box, text="Quick is recommended. Exact listing IDs are checked first; title-only matches are unverified.",
                     font=theme.font(11), text_color=theme.TEXT_MUTED, wraplength=620, justify="left").grid(
                         row=2, column=0, sticky="w")
        self.open_details_var = tk.BooleanVar(value=get_setting("live_check_open_details", False))
        self.open_details_checkbox = ctk.CTkCheckBox(
            self.settings_frame, text="Read extra dates from listing dialogs (slower)",
            variable=self.open_details_var, font=theme.font(12),
        )
        self.open_details_checkbox.grid(row=22, column=0, sticky="w", pady=(16, 0))
        self._update_buyer_controls()

        # --- Save Button ---
        self.save_btn = ctk.CTkButton(self.settings_frame, text="Save All Settings", command=self.on_save)
        self.save_btn.grid(row=23, column=0, sticky="w", pady=(20, 0))

        self.status_label = ctk.CTkLabel(self.settings_frame, text="", text_color="gray")
        self.status_label.grid(row=24, column=0, sticky="w", pady=(5, 0))

    def _update_buyer_controls(self):
        enabled = bool(self.buyers_enabled_var.get())
        for switch in (self.pk_switch, self.us_switch):
            switch.configure(state="normal" if enabled else "disabled")
        self.pk_checker_entry.configure(state="normal" if enabled and self.pk_enabled_var.get() else "disabled")
        self.us_checker_entry.configure(state="normal" if enabled and self.us_enabled_var.get() else "disabled")
        self.sample_menu.configure(state="normal" if enabled and (self.pk_enabled_var.get() or self.us_enabled_var.get()) else "disabled")

    def on_browse(self):
        file_path = filedialog.askopenfilename(
            title="Select Incogniton.exe",
            filetypes=[("Executable Files", "*.exe")]
        )
        if file_path:
            self.path_entry.delete(0, tk.END)
            self.path_entry.insert(0, file_path)

    def on_browse_adspower(self):
        file_path = filedialog.askopenfilename(
            title="Select AdsPower executable",
            filetypes=[("Executable Files", "*.exe")]
        )
        if file_path:
            self.ads_path_entry.delete(0, tk.END)
            self.ads_path_entry.insert(0, file_path)

    def on_save(self):
        new_path = self.path_entry.get().strip()
        new_url = self.url_entry.get().strip()
        new_ads_path = self.ads_path_entry.get().strip()
        new_ads_key = self.ads_key_entry.get().strip()
        provider_key = self._provider_keys.get(self.provider_var.get(), "incogniton")
        os_key = self._os_keys.get(self.ads_os_var.get(), "android")

        set_setting("browser_provider", provider_key)
        set_setting("incogniton_path", new_path)
        set_setting("adspower_path", new_ads_path)
        set_setting("adspower_api_key", new_ads_key)
        set_setting("adspower_os", os_key)
        set_setting("apps_script_url", new_url)
        set_setting("live_check_pk_profile", self.pk_checker_entry.get().strip())
        set_setting("live_check_us_profile", self.us_checker_entry.get().strip())
        set_setting("live_check_buyers_enabled", bool(self.buyers_enabled_var.get()))
        set_setting("live_check_pk_enabled", bool(self.pk_enabled_var.get()))
        set_setting("live_check_us_enabled", bool(self.us_enabled_var.get()))
        set_setting("live_check_sample_size", self.sample_sizes[self.sample_var.get()])
        set_setting("live_check_open_details", bool(self.open_details_var.get()))

        self.status_label.configure(text="Saved. Live Checker changes apply to the next run.", text_color=theme.GREEN)
        self.after(3000, lambda: self.status_label.configure(text=""))
