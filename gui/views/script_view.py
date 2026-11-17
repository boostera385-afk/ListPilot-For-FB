import os
import customtkinter as ctk
import pyperclip

class ScriptView(ctk.CTkFrame):
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)

        self.grid_rowconfigure(2, weight=1)
        self.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(self, text="Google Sheets Setup", font=ctk.CTkFont(size=20, weight="bold"))
        self.title_label.grid(row=0, column=0, padx=20, pady=(20, 10), sticky="nw")

        instructions = (
            "To connect this app to Google Sheets, you need to deploy the Apps Script below.\n"
            "1. Copy the code below.\n"
            "2. Open your Google Sheet, go to Extensions > Apps Script.\n"
            "3. Paste the code. For an existing setup, choose Deploy > Manage deployments > Edit > New version.\n"
            "4. Run setupSheets once; it will add the Live column without removing existing data.\n"
            "5. For a new setup, copy the Web App URL into Settings."
        )
        self.instructions_label = ctk.CTkLabel(self, text=instructions, justify="left")
        self.instructions_label.grid(row=1, column=0, padx=20, pady=10, sticky="nw")

        self.code_box = ctk.CTkTextbox(self)
        self.code_box.grid(row=2, column=0, padx=20, pady=10, sticky="nsew")

        self.btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        self.btn_frame.grid(row=3, column=0, padx=20, pady=(0, 20), sticky="ew")
        self.btn_frame.grid_columnconfigure(1, weight=1)

        self.copy_btn = ctk.CTkButton(self.btn_frame, text="Copy Code", command=self.copy_code)
        self.copy_btn.grid(row=0, column=0, sticky="nw")

        self.status_label = ctk.CTkLabel(self.btn_frame, text="", text_color="green")
        self.status_label.grid(row=0, column=1, padx=10, sticky="w")

        self.load_script_content()

    def load_script_content(self):
        script_path = os.path.join(os.getcwd(), "core", "assets", "apps_script.js")
        try:
            if os.path.exists(script_path):
                with open(script_path, "r", encoding="utf-8") as f:
                    code = f.read()
                self.code_box.insert("1.0", code)
            else:
                self.code_box.insert("1.0", "// Error: apps_script.js not found in core/assets/")
        except Exception as e:
            self.code_box.insert("1.0", f"// Error loading script: {e}")
        
        self.code_box.configure(state="disabled")

    def copy_code(self):
        code = self.code_box.get("1.0", "end-1c")
        pyperclip.copy(code)
        self.status_label.configure(text="Copied!", text_color="green")
        self.after(2000, lambda: self.status_label.configure(text=""))
