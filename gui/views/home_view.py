import customtkinter as ctk
from core.automation.browser_manager import launch_incogniton, is_incogniton_running, provider_label

class HomeView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        
        # Title
        self.title_label = ctk.CTkLabel(self, text="KitKat", font=ctk.CTkFont(size=24, weight="bold"))
        self.title_label.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="nw")
        
        # Description
        self.desc_label = ctk.CTkLabel(self, text="Select a process from the sidebar.", text_color="gray")
        self.desc_label.grid(row=1, column=0, padx=20, pady=(0, 20), sticky="nw")
        
        # Welcome Panel
        self.welcome_frame = ctk.CTkFrame(self)
        self.welcome_frame.grid(row=2, column=0, padx=20, pady=10, sticky="nsew")
        
        self.info_label = ctk.CTkLabel(self.welcome_frame, text=f"{provider_label()} will be launched automatically when you start an automation task.", font=ctk.CTkFont(size=14))
        self.info_label.pack(pady=40, padx=20)
