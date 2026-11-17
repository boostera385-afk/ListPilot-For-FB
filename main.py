import customtkinter as ctk
from gui.app import App
import sys

def main():
    # Set default appearance mode
    ctk.set_appearance_mode("System")  
    ctk.set_default_color_theme("blue")
    
    app = App()
    app.mainloop()

if __name__ == "__main__":
    main()
