import ctypes
import unittest
from unittest import mock
from tkinter import TclError

from PIL import Image

from gui import branding


class BrandingTests(unittest.TestCase):
    def test_icon_has_all_windows_sizes_and_transparency(self):
        self.assertTrue(branding.ICON_PATH.is_absolute())
        with Image.open(branding.ICON_PATH) as icon:
            self.assertEqual(icon.format, "ICO")
            self.assertEqual(icon.ico.sizes(), {(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)})
            for size in icon.ico.sizes():
                frame = icon.ico.getimage(size).convert("RGBA")
                self.assertLessEqual(frame.getpixel((0, 0))[3], 4)
                colors = frame.getcolors(frame.width * frame.height)
                self.assertTrue(any(min(color[:3]) > 180 and color[3] == 255
                                    for _count, color in colors))

    @mock.patch.object(branding.sys, "platform", "win32")
    def test_app_id_uses_unicode_windows_api(self):
        shell = mock.Mock()
        shell.shell32.SetCurrentProcessExplicitAppUserModelID.return_value = 0
        with mock.patch.object(branding.ctypes, "windll", shell, create=True):
            branding.set_windows_app_id()
        setter = shell.shell32.SetCurrentProcessExplicitAppUserModelID
        setter.assert_called_once_with(branding.APP_ID)
        self.assertEqual(setter.argtypes, [ctypes.c_wchar_p])
        self.assertEqual(setter.restype, ctypes.c_long)

    @mock.patch.object(branding.sys, "platform", "win32")
    def test_app_id_failure_does_not_block_startup(self):
        shell = mock.Mock()
        shell.shell32.SetCurrentProcessExplicitAppUserModelID.side_effect = OSError("unavailable")
        with mock.patch.object(branding.ctypes, "windll", shell, create=True):
            with self.assertLogs(branding.logger, level="WARNING"):
                branding.set_windows_app_id()

    @mock.patch.object(branding.sys, "platform", "win32")
    def test_app_id_reports_hresult_failure(self):
        shell = mock.Mock()
        shell.shell32.SetCurrentProcessExplicitAppUserModelID.return_value = -1
        with mock.patch.object(branding.ctypes, "windll", shell, create=True):
            with self.assertLogs(branding.logger, level="WARNING"):
                branding.set_windows_app_id()

    @mock.patch.object(branding.sys, "platform", "win32")
    def test_window_uses_absolute_default_icon(self):
        window = mock.Mock()
        branding.set_window_icon(window)
        window.iconbitmap.assert_called_once_with(default=str(branding.ICON_PATH))

    @mock.patch.object(branding.sys, "platform", "win32")
    def test_missing_icon_does_not_block_startup(self):
        window = mock.Mock()
        window.iconbitmap.side_effect = TclError("missing icon")
        with self.assertLogs(branding.logger, level="WARNING"):
            branding.set_window_icon(window)

    @mock.patch.object(branding.sys, "platform", "linux")
    def test_non_windows_does_not_call_shell_or_iconbitmap(self):
        window = mock.Mock()
        with mock.patch.object(branding.ctypes, "windll", create=True) as shell:
            branding.set_windows_app_id()
            branding.set_window_icon(window)
        self.assertEqual(shell.mock_calls, [])
        window.iconbitmap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
