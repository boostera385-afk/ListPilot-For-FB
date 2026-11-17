import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog
from core.config import get_setting, set_setting

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

        # --- Save Button ---
        self.save_btn = ctk.CTkButton(self.settings_frame, text="Save All Settings", command=self.on_save)
        self.save_btn.grid(row=18, column=0, sticky="w", pady=(20, 0))

        self.status_label = ctk.CTkLabel(self.settings_frame, text="", text_color="gray")
        self.status_label.grid(row=19, column=0, sticky="w", pady=(5, 0))

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

        self.status_label.configure(text="Settings saved successfully!", text_color="green")
        self.after(3000, lambda: self.status_label.configure(text=""))
