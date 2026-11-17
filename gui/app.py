import customtkinter as ctk
from gui.views.home_view import HomeView
from gui.views.settings_view import SettingsView
from gui.views.fb_listing_view import FBListingView
from gui.views.dominancy_checker_view import DominancyCheckerView
from gui.views.response_checker_view import ResponseCheckerView
from gui.views.live_checker_view import LiveCheckerView
from gui.views.script_view import ScriptView
from gui.ui_helpers import start_ui_pump

class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("KitKat")
        self.geometry("800x500")
        
        # Grid layout 1x2 (Sidebar, Main content)
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(1, weight=1)
        
        # --- Sidebar ---
        self.sidebar_frame = ctk.CTkFrame(self, width=200, corner_radius=0)
        self.sidebar_frame.grid(row=0, column=0, sticky="nsew")
        self.sidebar_frame.grid_rowconfigure(10, weight=1)
        
        self.logo_label = ctk.CTkLabel(self.sidebar_frame, text="KitKat", font=ctk.CTkFont(size=20, weight="bold"))
        self.logo_label.grid(row=0, column=0, padx=20, pady=(20, 10))
        
        self.btn_home = ctk.CTkButton(self.sidebar_frame, text="Home", command=self.show_home_view)
        self.btn_home.grid(row=1, column=0, padx=20, pady=10)
        
        self.btn_fb_listing = ctk.CTkButton(self.sidebar_frame, text="FB Listings", command=self.show_fb_listing_view)
        self.btn_fb_listing.grid(row=2, column=0, padx=20, pady=10)

        self.btn_dominancy = ctk.CTkButton(self.sidebar_frame, text="Dominancy Checker", command=self.show_dominancy_view)
        self.btn_dominancy.grid(row=3, column=0, padx=20, pady=10)
        self.btn_response = ctk.CTkButton(self.sidebar_frame, text="Response Checker", command=self.show_response_view)
        self.btn_response.grid(row=4, column=0, padx=20, pady=10)

        self.btn_live = ctk.CTkButton(self.sidebar_frame, text="Live Checker", command=self.show_live_view)
        self.btn_live.grid(row=5, column=0, padx=20, pady=10)
        
        self.btn_script = ctk.CTkButton(self.sidebar_frame, text="Script Builder", command=self.show_script_view)
        self.btn_script.grid(row=6, column=0, padx=20, pady=10)

        self.btn_settings = ctk.CTkButton(self.sidebar_frame, text="Settings", command=self.show_settings_view)
        self.btn_settings.grid(row=10, column=0, padx=20, pady=20, sticky="s")
        
        # --- Main Content Area ---
        self.main_container = ctk.CTkFrame(self, corner_radius=0, fg_color="transparent")
        self.main_container.grid(row=0, column=1, sticky="nsew")
        self.main_container.grid_rowconfigure(0, weight=1)
        self.main_container.grid_columnconfigure(0, weight=1)
        
        # Initialize all views
        self.home_view = HomeView(self.main_container, fg_color="transparent")
        self.fb_listing_view = FBListingView(self.main_container, fg_color="transparent")
        self.dominancy_view = DominancyCheckerView(self.main_container, fg_color="transparent")
        self.response_view = ResponseCheckerView(self.main_container, fg_color="transparent")
        self.live_view = LiveCheckerView(self.main_container, fg_color="transparent")
        self.script_view = ScriptView(self.main_container, fg_color="transparent")
        self.settings_view = SettingsView(self.main_container, fg_color="transparent")
        
        # Default view
        self.show_home_view()

        # Deliver UI updates queued by automation threads.
        start_ui_pump(self)

    def hide_all_views(self):
        self.home_view.grid_forget()
        self.fb_listing_view.grid_forget()
        self.dominancy_view.grid_forget()
        self.response_view.grid_forget()
        self.live_view.grid_forget()
        self.script_view.grid_forget()
        self.settings_view.grid_forget()

    def show_home_view(self):
        self.hide_all_views()
        self.home_view.grid(row=0, column=0, sticky="nsew")
        
    def show_fb_listing_view(self):
        self.hide_all_views()
        self.fb_listing_view.grid(row=0, column=0, sticky="nsew")

    def show_response_view(self):
        self.hide_all_views()
        self.response_view.grid(row=0, column=0, sticky="nsew")

    def show_live_view(self):
        self.hide_all_views()
        self.live_view.grid(row=0, column=0, sticky="nsew")

    def show_dominancy_view(self):
        self.hide_all_views()
        self.dominancy_view.grid(row=0, column=0, sticky="nsew")
        
    def show_script_view(self):
        self.hide_all_views()
        self.script_view.grid(row=0, column=0, sticky="nsew")
        
    def show_settings_view(self):
        self.hide_all_views()
        self.settings_view.grid(row=0, column=0, sticky="nsew")

    def change_appearance_mode_event(self, new_appearance_mode: str):
        ctk.set_appearance_mode(new_appearance_mode)
