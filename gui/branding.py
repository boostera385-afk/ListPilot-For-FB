"""Windows shell branding, independent of the launcher's working directory."""
import ctypes
import logging
from pathlib import Path
import sys
from tkinter import TclError


APP_ID = "ListPilot.Desktop"
ICON_PATH = Path(__file__).resolve().parent / "assets" / "listpilot.ico"
logger = logging.getLogger(__name__)


def set_windows_app_id():
    """Give ListPilot its own taskbar group instead of Python's; call before Tk."""
    if sys.platform != "win32":
        return
    try:
        set_app_id = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
        set_app_id.argtypes = [ctypes.c_wchar_p]
        set_app_id.restype = ctypes.c_long
        result = set_app_id(APP_ID)
        if result < 0:
            logger.warning("Could not set ListPilot taskbar identity: HRESULT %s", result)
    except (AttributeError, OSError) as exc:
        logger.warning("Could not set ListPilot taskbar identity: %s", exc)


def set_window_icon(window):
    """Set both title-bar/taskbar icons and the default for child windows."""
    if sys.platform != "win32":
        return
    try:
        # Use CTk's public method so its delayed default icon cannot override ours.
        window.iconbitmap(default=str(ICON_PATH))
    except (TclError, OSError) as exc:
        logger.warning("Could not load ListPilot icon: %s", exc)
