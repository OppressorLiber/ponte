"""Qt action and rendering contracts for explicit transports and local services."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PONTE_REDUCED_MOTION", "1")

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog

from pontelan.ui import AdditionalPortsDialog, MainWindow


class FakeController(QObject):
    event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.calls = []

    def host(self, port, name, ports=None):
        self.calls.append(("host", port, name, ports))

    def join(self, code, port=0, name="Amigo", transport="quic"):
        self.calls.append(("join", code, port, name, transport))

    def scan(self):
        pass

    def stop(self):
        pass


class FakeClipboard:
    def __init__(self):
        self.value = ""

    def setText(self, value):
        self.value = value


class TransportUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
            if (fonts / name).is_file():
                QFontDatabase.addApplicationFont(str(fonts / name))

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.settings_patch = patch("pontelan.ui.QSettings", return_value=self.settings)
        self.settings_patch.start()
        self.clipboard = FakeClipboard()
        self.clipboard_patch = patch("pontelan.ui.QApplication.clipboard", return_value=self.clipboard)
        self.clipboard_patch.start()
        self.controller = FakeController()
        self.window = MainWindow(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self.clipboard_patch.stop()
        self.settings_patch.stop()
        self.settings.sync()
        self.directory.cleanup()

    @staticmethod
    def mapping(**changes):
        return {"id": "p1", "name": "Voz", "protocol": "udp", "host_port": 24454, "local_port": 24454, **changes}

    def emit(self, kind, **data):
        self.controller.event.emit({"type": kind, **data})

    def guest(self):
        self.window._set_tab("guest", animate=False)
        self.emit("status", state="connecting", role="guest")

    def test_new_join_defaults_to_explicit_quic(self):
        self.window._set_tab("guest", animate=False)
        self.assertEqual(self.window.transport_combo.currentData(), "quic")
        self.window.invite_input.setPlainText("valid-invite")
        self.window.guest_name.setText("Maria")
        self.window.join_button.click()
        self.assertEqual(self.controller.calls, [("join", "valid-invite", 0, "Maria", "quic")])
        self.assertFalse(self.window.transport_combo.isEnabled())

    def test_compatibility_choice_is_passed_without_fallback(self):
        self.window._set_tab("guest", animate=False)
        self.window.invite_input.setPlainText("older-host")
        self.window.transport_combo.setCurrentIndex(self.window.transport_combo.findData("webrtc"))
        self.window.join_button.click()
        self.assertEqual(self.controller.calls, [("join", "older-host", 0, "Amigo", "webrtc")])
        self.emit("error", message="O anfitrião não respondeu.")
        self.assertEqual(len(self.controller.calls), 1)
        self.assertEqual(self.window.transport_combo.currentData(), "webrtc")

    def test_host_sends_validated_service_configuration(self):
        self.window._configured_ports = [self.mapping()]
        self.window.world_name.setText("Mundo do grupo")
        self.window.port_input.setValue(50000)
        self.window.host_button.click()
        self.assertEqual(self.controller.calls, [("host", 50000, "Mundo do grupo", [self.mapping()])])
        self.assertFalse(self.window.extra_ports_button.isEnabled())

    def test_invalid_saved_mapping_cannot_start_host(self):
        self.window._configured_ports = [self.mapping(protocol="tcp", host_port=25565)]
        self.window.host_button.click()
        self.assertEqual(self.controller.calls, [])
        self.assertFalse(self.window._busy)
        self.assertFalse(self.window.notice.isHidden())
        self.assertIn("porta TCP do jogo", self.window.notice_text.text())
        self.assertTrue(self.window.extra_ports_button.isEnabled())

    def test_save_remembers_only_canonical_nonsecret_service_fields(self):
        self.window.extra_ports_button.click()
        dialog = self.window._ports_dialog
        dialog.voice_button.click()
        dialog.save_button.click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(self.window._configured_ports, [self.mapping()])
        self.assertEqual(json.loads(self.settings.value("extra_ports")), [self.mapping()])
        self.assertEqual(set(self.settings.allKeys()), {"extra_ports"})
        self.assertIn("1", self.window.extra_ports_button.text())

    def test_cancel_discards_changes_and_host_session_blocks_configuration(self):
        self.window._configured_ports = [self.mapping()]
        self.window.extra_ports_button.click()
        dialog = self.window._ports_dialog
        dialog.rows[0].host_port.setValue(30000)
        dialog.reject()
        self.assertEqual(self.window._configured_ports[0]["host_port"], 24454)
        self.emit("status", state="hosting", role="host")
        self.assertFalse(self.window.extra_ports_button.isEnabled())
        self.window._open_ports_configuration()
        self.assertIs(self.window._ports_dialog, dialog)
        self.assertTrue(dialog.isHidden())

    def test_dialog_maximum_rows_duplicate_validation_and_id_reuse(self):
        dialog = AdditionalPortsDialog([], 25565, self.window)
        dialog.show()
        for _ in range(9):
            dialog.add_button.click()
        self.assertEqual(len(dialog.rows), 8)
        self.assertFalse(dialog.add_button.isEnabled())
        self.assertFalse(dialog.voice_button.isEnabled())
        dialog.save_button.click()
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertIn("repetidas", dialog.notice.text())
        removed_id = dialog.rows[0].port_id
        dialog.rows[0].remove_button.click()
        dialog.add_button.click()
        self.assertEqual(dialog.rows[-1].port_id, removed_id)
        self.assertEqual(len({row.port_id for row in dialog.rows}), 8)
        dialog.close()

    def test_guest_port_follows_host_default_but_preserves_manual_override(self):
        dialog = AdditionalPortsDialog([], 25565, self.window)
        dialog.add_port()
        row = dialog.rows[0]
        row.host_port.setValue(31000)
        self.assertEqual(row.local_port.value(), 31000)
        row.local_port.setValue(0)
        row.host_port.setValue(32000)
        self.assertEqual(row.local_port.value(), 0)
        dialog.close()

    def test_guest_shows_only_actual_bound_ports_and_copies_exact_address(self):
        self.guest()
        self.emit("room", code="fixture", name="Mundo", ports=[self.mapping(local_port=0)])
        self.assertTrue(self.window.session_ports_button.isHidden())
        self.emit("listening", address="127.0.0.1:54123", ports=[self.mapping(local_port=0, local_address="127.0.0.1:54124")])
        self.assertFalse(self.window.session_ports_button.isHidden())
        self.window.session_ports_button.click()
        self.assertEqual(self.window._address_rows[0].address_label.text(), "UDP · 127.0.0.1:54124")
        self.window._address_rows[0].copy_button.click()
        self.assertEqual(self.clipboard.value, "127.0.0.1:54124")
        self.assertNotIn("24454", self.window._address_rows[0].address_label.text())

    def test_live_ports_update_and_stop_clear_guest_addresses(self):
        self.guest()
        self.emit("listening", address="127.0.0.1:54123")
        self.emit("ports", ports=[self.mapping(local_address="127.0.0.1:54125")])
        self.window.session_ports_button.click()
        self.emit("ports", ports=[self.mapping(local_address="127.0.0.1:54126")])
        self.assertIn("54126", self.window._address_rows[0].address_label.text())
        self.emit("status", state="stopped")
        self.assertEqual(self.window._active_ports, [])
        self.assertTrue(self.window._addresses_dialog.isHidden())
        self.assertTrue(self.window.session_ports_button.isHidden())
        self.emit("ports", ports=[self.mapping(local_address="127.0.0.1:54126")])
        self.assertEqual(self.window._active_ports, [])

    def test_host_service_addresses_use_local_destination(self):
        self.emit("status", state="hosting", role="host")
        self.emit("room", code="fixture", name="Mundo", ports=[self.mapping(local_port=0)])
        self.window.session_ports_button.click()
        self.assertEqual(self.window._address_rows[0].address_label.text(), "UDP · 127.0.0.1:24454")
        self.assertIn("Destinos", self.window.services_hint.text())

    def test_peer_tooltip_names_actual_transport_route_and_measured_rtt(self):
        self.emit("status", state="hosting", role="host")
        self.emit("peer", peer_id="fixture", name="Amigo", state="connected", transport="quic",
                  transport_name="QUIC / TLS 1.3", route="host ↔ srflx", rtt_ms=28.7,
                  rtt_p50_ms=27.3, rtt_p95_ms=31.9)
        detail = self.window._peer_rows["fixture"].detail_label
        self.assertIn("QUIC / TLS 1.3", detail.toolTip())
        self.assertIn("host ↔ srflx", detail.toolTip())
        self.assertIn("RTT do aplicativo", detail.toolTip())
        self.assertIn("29 ms", detail.text())

    def test_compact_forms_and_dialog_keep_controls_in_bounds(self):
        for width, height in ((820, 600), (960, 690)):
            self.window.resize(width, height)
            self.window._set_tab("guest", animate=False)
            self.app.processEvents()
            self.assertLess(self.window.transport_combo.geometry().bottom(), self.window.invite_input.geometry().top())
            self.assertLess(self.window.guest_name.geometry().right(), self.window.transport_combo.geometry().left())
            scroll = self.window.guest_page.findChild(type(self.window.peers_scroll))
            self.assertLessEqual(self.window.transport_combo.geometry().right(), scroll.viewport().width())
            self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
            self.window._set_tab("host", animate=False)
            self.app.processEvents()
            self.assertLess(self.window.scan_hint.geometry().bottom(), self.window.extra_ports_button.geometry().top())
        dialog = AdditionalPortsDialog([self.mapping(name="Serviço com nome comprido")], 25565, self.window)
        dialog.resize(620, 380)
        dialog.show()
        self.app.processEvents()
        row = dialog.rows[0]
        controls = [row.name_input, row.protocol_combo, row.host_port, row.local_port, row.remove_button]
        for left, right in zip(controls, controls[1:]):
            self.assertLess(left.geometry().right(), right.geometry().left())
        self.assertLessEqual(row.remove_button.geometry().right(), row.width())
        self.assertGreaterEqual(row.name_input.width(), 90)
        dialog.close()


if __name__ == "__main__":
    unittest.main()
