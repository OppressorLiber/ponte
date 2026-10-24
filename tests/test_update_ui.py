"""Local Qt checks for updater feedback and session-safe install actions."""
import os
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PONTE_REDUCED_MOTION", "1")

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from pontelan.ui import MainWindow


class FakeController(QObject):
    event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.checks = 0
        self.installs = 0

    def check_updates(self):
        self.checks += 1

    def install_update(self):
        self.installs += 1

    def stop(self):
        pass

    def scan(self):
        pass


class UpdateUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        # The Windows offscreen plugin starts with no system font database.
        # Load real Windows UI fonts so compact-layout checks measure real text.
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
            if (fonts / name).is_file():
                QFontDatabase.addApplicationFont(str(fonts / name))

    def setUp(self):
        self.controller = FakeController()
        self.window = MainWindow(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def update(self, state, **data):
        self.controller.event.emit({"type": "update", "state": state, **data})

    def test_manual_check_has_inline_feedback(self):
        self.assertTrue(self.window.update_status.isHidden())
        self.window.updates_button.click()
        self.assertEqual(self.controller.checks, 1)
        self.assertFalse(self.window.update_status.isHidden())
        self.assertIn("Buscando", self.window.update_text.text())
        self.assertFalse(self.window.updates_button.isEnabled())
        self.update("current")
        self.assertTrue(self.window.updates_button.isEnabled())
        self.assertIn("atualizada", self.window.update_text.text())

    def test_ready_waits_for_active_session_to_end(self):
        self.controller.event.emit({"type": "status", "state": "hosting", "role": "host"})
        self.update("ready", version="0.2.0")
        self.assertTrue(self.window._session)
        self.assertTrue(self.window.install_update_button.isHidden())
        self.assertFalse(self.window.install_update_button.isEnabled())
        self.assertIn("ao encerrar a sessão", self.window.update_text.text())
        self.window._install_update()
        self.assertEqual(self.controller.installs, 0)
        self.controller.event.emit({"type": "status", "state": "stopped"})
        self.assertFalse(self.window.install_update_button.isHidden())
        self.assertTrue(self.window.install_update_button.isEnabled())

    def test_update_error_preserves_session_and_connection_data(self):
        self.controller.event.emit({"type": "room", "code": "actual-invite", "name": "Mundo", "port": 25565})
        self.update("error", message="O GitHub não respondeu. Tente novamente.")
        self.assertTrue(self.window._session)
        self.assertEqual(self.window._code, "actual-invite")
        self.assertEqual(self.window.pages.currentWidget(), self.window.session_page)
        self.assertIn("GitHub", self.window.update_text.text())
        self.assertTrue(self.window.updates_button.isEnabled())

    def test_ready_installs_only_while_idle_and_ignores_repeat_click(self):
        self.update("ready", version="0.2.0")
        self.window._set_busy(True)
        self.window._install_update()
        self.assertEqual(self.controller.installs, 0)
        self.window._set_busy(False)
        self.window.install_update_button.click()
        self.window._install_update()
        self.assertEqual(self.controller.installs, 1)
        self.assertFalse(self.window.install_update_button.isEnabled())

    def test_update_operation_error_also_preserves_session(self):
        self.controller.event.emit({"type": "status", "state": "connected", "role": "guest"})
        self.controller.event.emit({"type": "error", "operation": "update", "message": "Sem rede."})
        self.assertTrue(self.window._session)
        self.assertEqual(self.window._state, "connected")

    def test_installation_quits_only_after_controller_installing_event(self):
        self.update("ready", version="0.2.0")
        with patch("pontelan.ui.QTimer.singleShot") as schedule:
            self.window.install_update_button.click()
            schedule.assert_not_called()
            self.update("installing")
            schedule.assert_called_once_with(150, self.window._quit_for_update)
        self.assertFalse(self.window.updates_button.isEnabled())

    def test_footer_controls_fit_compact_window(self):
        for width, height in ((820, 600), (960, 690)):
            self.window.resize(width, height)
            self.update("ready", version="0.2.0")
            self.app.processEvents()
            self.assertEqual(self.window.width(), width)
            controls = [self.window.motion_toggle, self.window.diagnostics_button, self.window.updates_button]
            for left, right in zip(controls, controls[1:]):
                self.assertLessEqual(left.geometry().right(), right.geometry().left())
            self.assertLess(self.window.updates_button.geometry().right(), self.window.centralWidget().width())


if __name__ == "__main__":
    unittest.main()
