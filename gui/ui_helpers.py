import customtkinter as ctk
import queue
import threading
import traceback

# Worker threads must never touch Tk directly: Tk can raise "main thread is
# not in main loop", which used to escape log() and kill automation runs.
# Threads only enqueue; the main loop drains the queue.
_ui_queue = queue.Queue()


def run_on_ui(func, *args):
    """Thread-safe replacement for widget.after(0, func, *args)."""
    _ui_queue.put((func, args))


def start_ui_pump(root, interval_ms=50):
    """Run queued UI callbacks on the Tk main thread. Call once at startup."""
    def pump():
        for _ in range(500):
            try:
                func, args = _ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                func(*args)
            except Exception:
                traceback.print_exc()
        root.after(interval_ms, pump)

    root.after(interval_ms, pump)


def resolve_duplicates_ui(master, identifier, matches):
    selected_id = [None]
    event = threading.Event()
    
    def show():
        dialog = ctk.CTkToplevel(master)
        dialog.title("Duplicate Profiles")
        dialog.geometry("450x300")
        dialog.attributes("-topmost", True)
        dialog.focus_force()
        
        lbl = ctk.CTkLabel(dialog, text=f"Multiple profiles found for '{identifier}'.\nPlease select the correct one based on Group:", font=ctk.CTkFont(weight="bold"))
        lbl.pack(pady=10)
        
        scroll = ctk.CTkScrollableFrame(dialog)
        scroll.pack(fill="both", expand=True, padx=10, pady=5)
        
        def on_select(b_id):
            selected_id[0] = b_id
            dialog.destroy()
            event.set()
            
        for m in matches:
            group_name = m.get('group', 'Unassigned')
            if not group_name: group_name = 'Unassigned'
            
            btn_text = f"Group: {group_name}"
            btn = ctk.CTkButton(scroll, text=btn_text, command=lambda b=m['browser_id']: on_select(b))
            btn.pack(pady=5, fill="x", padx=10)
            
        dialog.protocol("WM_DELETE_WINDOW", lambda: [dialog.destroy(), event.set()])
        
    run_on_ui(show)
    event.wait()
    return selected_id[0]

