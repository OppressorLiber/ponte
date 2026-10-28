"""Qt contract checks for admission, invitation rotation and host controls."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PONTE_REDUCED_MOTION", "1")

from PySide6.QtCore import QObject, QSettings, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from pontelan.ui import MainWindow


class FakeController(QObject):
    event = Signal(dict)

    def __init__(self):
        super().__init__()
        self.calls = []
        self.approvals = []
        self.fail = ""

    def _call(self, method, *args):
        self.calls.append((method, *args))
        if method == self.fail:
            raise RuntimeError("Tente novamente.")

    def approve(self, peer_id, remember=False):
        self.approvals.append((peer_id, remember))
        self._call("approve", peer_id, True) if remember else self._call("approve", peer_id)

    def reject(self, peer_id):
        self._call("reject", peer_id)

    def remove_peer(self, peer_id):
        self._call("remove_peer", peer_id)

    def renew_invite(self):
        self._call("renew_invite")

    def set_trusted(self, peer_id, trusted):
        self._call("set_trusted", peer_id, trusted)

    def forget_trusted(self, fingerprint):
        self._call("forget_trusted", fingerprint)

    def join(self, code, port, name, transport="quic"):
        self._call("join", code, port, name, transport)

    def stop(self):
        self._call("stop")

    def scan(self):
        pass


class FakeClipboard:
    def __init__(self):
        self._text = ""

    def text(self):
        return self._text

    def setText(self, text):
        self._text = text


class RoomUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        for name in ("segoeui.ttf", "seguisb.ttf", "segoeuib.ttf"):
            if (fonts / name).is_file():
                QFontDatabase.addApplicationFont(str(fonts / name))

    def setUp(self):
        self._settings_dir = tempfile.TemporaryDirectory()
        self._settings = QSettings(str(Path(self._settings_dir.name) / "settings.ini"), QSettings.Format.IniFormat)
        self._settings_patch = patch("pontelan.ui.QSettings", return_value=self._settings)
        self._settings_patch.start()
        self._clipboard = FakeClipboard()
        self._clipboard_patch = patch("pontelan.ui.QApplication.clipboard", return_value=self._clipboard)
        self._clipboard_patch.start()
        self.controller = FakeController()
        self.window = MainWindow(self.controller)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        self._clipboard_patch.stop()
        self._settings_patch.stop()
        self._settings.sync()
        self._settings_dir.cleanup()

    def emit(self, kind, **data):
        self.controller.event.emit({"type": kind, **data})

    def host(self):
        self.emit("status", state="hosting", role="host")
        self.emit("room", code="old-invite", name="Mundo dos amigos", port=25565)

    def pending(self, peer_id="first1-guest", name="Amigo", **data):
        self.emit("admission", peer_id=peer_id, name=name, state="pending", **data)
        return self.window._admission_rows.get(peer_id)

    def peer(self, peer_id="peer-a", **data):
        self.emit("peer", peer_id=peer_id, name="Amigo", state="connected", **data)
        return self.window._peer_rows[peer_id]

    def test_only_host_sees_or_can_invoke_room_controls(self):
        self.host()
        self.assertFalse(self.window.renew_invite_button.isHidden())
        self.assertFalse(self.pending().isHidden())
        self.assertFalse(self.peer().remove_button.isHidden())
        self.emit("status", state="stopped")
        self.emit("status", state="connecting", role="guest")
        self.assertTrue(self.window.renew_invite_button.isHidden())
        self.assertTrue(self.window.renew_hint.isHidden())
        self.assertTrue(self.peer().remove_button.isHidden())
        self.pending()
        self.assertFalse(self.window._admissions)
        self.assertTrue(self.window.admissions_section.isHidden())
        self.window._renew_invite()
        self.window._remove_peer("peer-a")
        self.window._admission_action("first1-guest", "approve")
        self.assertEqual(self.controller.calls, [])

    def test_duplicate_names_retain_correct_action_ids_and_ignore_repeats(self):
        self.host()
        first = self.pending("first1-guest", "Mesmo nome")
        second = self.pending("second-guest", "Mesmo nome")
        self.assertNotEqual(first.detail_label.text(), second.detail_label.text())
        first.approve_button.click()
        first.approve_button.click()
        first.reject_button.click()
        self.window._admission_action("first1-guest", "reject")
        second.reject_button.click()
        second.reject_button.click()
        self.assertEqual(self.controller.calls, [("approve", "first1-guest"), ("reject", "second-guest")])
        self.assertFalse(first.approve_button.isEnabled())
        self.assertFalse(first.reject_button.isEnabled())
        self.assertIn("Aprovando", first.approve_button.text())
        self.assertFalse(second.approve_button.isEnabled())

    def test_terminal_admission_events_remove_only_their_request(self):
        self.host()
        for state in ("approved", "rejected", "expired", "cancelled"):
            self.pending("first1-guest")
            self.pending("second-guest")
            self.emit("admission", peer_id="first1-guest", state=state)
            self.assertNotIn("first1-guest", self.window._admission_rows)
            self.assertIn("second-guest", self.window._admission_rows)
            self.assertEqual(self.window.admissions_heading.text(), "Pedidos de entrada · 1")
        self.emit("admission", peer_id="second-guest", state="expired")
        self.assertTrue(self.window.admissions_section.isHidden())

    def test_stop_clears_requests_and_prevents_late_reappearance(self):
        self.host()
        self.pending()
        self.window.stop_button.click()
        self.assertFalse(self.window._admissions)
        self.pending("late-guest")
        self.assertFalse(self.window._admissions)
        self.assertFalse(self.window.renew_invite_button.isEnabled())
        self.emit("status", state="stopped")
        self.assertFalse(self.window._admission_rows)
        self.assertFalse(self.window._peer_actions)
        self.assertFalse(self.window._session)

    def test_admission_error_preserves_session_and_allows_retry(self):
        self.host()
        peer = self.peer()
        request = self.pending()
        request.approve_button.click()
        self.emit("error", operation="peer", peer_id="first1-guest", message="Pedido expirou na rede.")
        self.assertTrue(self.window._session)
        self.assertEqual(self.window._state, "hosting")
        self.assertEqual(self.window._code, "old-invite")
        self.assertIs(self.window._peer_rows["peer-a"], peer)
        self.assertTrue(request.approve_button.isEnabled())
        self.assertTrue(request.reject_button.isEnabled())
        self.assertEqual(self.window.pages.currentWidget(), self.window.session_page)
        self.assertIn("expirou", self.window.notice_text.text())
        request.reject_button.click()
        self.assertEqual(self.controller.calls[-1], ("reject", "first1-guest"))

    def test_synchronous_action_errors_reenable_controls(self):
        self.host()
        request = self.pending()
        self.controller.fail = "approve"
        request.approve_button.click()
        self.assertTrue(request.approve_button.isEnabled())
        self.controller.fail = "remove_peer"
        peer = self.peer()
        peer.remove_button.click()
        self.assertTrue(peer.remove_button.isEnabled())
        self.controller.fail = "renew_invite"
        self.window.renew_invite_button.click()
        self.assertTrue(self.window.renew_invite_button.isEnabled())
        self.assertTrue(self.window._session)

    def test_remove_targets_exact_peer_and_waits_for_confirmation(self):
        self.host()
        first = self.peer("peer-a")
        second = self.peer("peer-b")
        second.remove_button.click()
        self.window._remove_peer("peer-b")
        self.assertEqual(self.controller.calls, [("remove_peer", "peer-b")])
        self.assertTrue(first.remove_button.isEnabled())
        self.assertFalse(second.remove_button.isEnabled())
        self.assertIn("nova aprovação", self.window.removal_hint.text())
        self.emit("peer", peer_id="peer-b", state="disconnected")
        self.assertTrue(second.remove_button.isHidden())
        self.assertFalse(first.remove_button.isHidden())
        self.window._remove_peer("peer-b")
        self.assertEqual(len(self.controller.calls), 1)

    def test_remove_error_preserves_peers_and_restores_action(self):
        self.host()
        row = self.peer()
        row.remove_button.click()
        self.emit("error", operation="peer", message="Não foi possível remover.")
        self.assertTrue(row.remove_button.isEnabled())
        self.assertEqual(self.window._peers["peer-a"]["state"], "connected")
        self.assertTrue(self.window._session)

    def test_renew_updates_copy_value_without_losing_peers_or_session(self):
        self.host()
        row = self.peer(rtt_ms=41.7)
        self.window.renew_invite_button.click()
        self.window._renew_invite()
        self.assertEqual(self.controller.calls, [("renew_invite",)])
        self.assertFalse(self.window.renew_invite_button.isEnabled())
        self.assertFalse(self.window.copy_button.isEnabled())
        self.window.copy_button.click()
        self.assertEqual(self._clipboard.text(), "")
        self.assertIn("antigo deixa de valer", self.window.renew_hint.text())
        self.assertEqual(self.window._code, "old-invite")
        self.emit("room", code="new-invite", name="Mundo dos amigos", port=25565, renewed=True)
        self.assertTrue(self.window.renew_invite_button.isEnabled())
        self.assertTrue(self.window.copy_button.isEnabled())
        self.assertEqual(self.window.code_display.toPlainText(), "new-invite")
        self.assertIs(self.window._peer_rows["peer-a"], row)
        self.assertEqual(self.window._peers["peer-a"]["state"], "connected")
        self.assertTrue(self.window._session)
        self.window.copy_button.click()
        self.assertEqual(self.app.clipboard().text(), "new-invite")

    def test_renew_error_and_ready_event_release_pending_button(self):
        self.host()
        self.peer()
        self.window.renew_invite_button.click()
        self.emit("error", operation="room", message="Não foi possível renovar agora.")
        self.assertTrue(self.window.renew_invite_button.isEnabled())
        self.assertTrue(self.window.copy_button.isEnabled())
        self.assertEqual(self.window._code, "old-invite")
        self.assertEqual(self.window._state, "hosting")
        self.assertTrue(self.window._session)
        self.emit("room_action", state="renewing")
        self.assertFalse(self.window.renew_invite_button.isEnabled())
        self.assertFalse(self.window.copy_button.isEnabled())
        self.emit("room_action", state="ready")
        self.assertTrue(self.window.renew_invite_button.isEnabled())
        self.assertTrue(self.window.copy_button.isEnabled())

    def test_renew_error_does_not_release_an_independent_approval(self):
        self.host()
        request = self.pending()
        request.approve_button.click()
        self.window.renew_invite_button.click()
        self.emit("error", operation="room", message="Renovação indisponível.")
        self.assertTrue(self.window.renew_invite_button.isEnabled())
        self.assertFalse(request.approve_button.isEnabled())
        self.window._admission_action("first1-guest", "approve")
        self.assertEqual(self.controller.calls.count(("approve", "first1-guest")), 1)

    def test_guest_waits_for_approval_without_exposing_minecraft_address(self):
        self.emit("status", state="waiting_approval", role="guest", message="Aguarde o anfitrião.")
        self.assertTrue(self.window._session)
        self.assertIn("aprovação", self.window.session_heading.text())
        self.assertIn("Aguardando", self.window.status_badge.text())
        self.assertTrue(self.window.address_display.isHidden())
        self.assertTrue(self.window.copy_button.isHidden())
        self.assertFalse(self.window.copy_button.isEnabled())
        self.assertEqual(self.window._address, "")
        self.emit("status", state="connected", role="guest")
        self.assertTrue(self.window.address_display.isHidden())
        self.emit("listening", address="127.0.0.1:25561")
        self.assertFalse(self.window.address_display.isHidden())
        self.assertFalse(self.window.copy_button.isHidden())
        self.assertTrue(self.window.copy_button.isEnabled())
        self.assertEqual(self.window.address_display.text(), "127.0.0.1:25561")

    def test_guest_name_is_sent_with_join_request(self):
        self.window._set_tab("guest", animate=False)
        self.window.invite_input.setPlainText("received-invite")
        self.window.guest_name.setText("  Nome de teste  ")
        self.window.join_button.click()
        self.assertEqual(self.controller.calls, [("join", "received-invite", 0, "Nome de teste", "quic")])
        self.assertEqual(self.window.settings.value("guest_name"), "Nome de teste")
        self.assertFalse(self.window.guest_name.isEnabled())

    def test_blank_guest_name_defaults_to_amigo(self):
        self.window._set_tab("guest", animate=False)
        self.window.invite_input.setPlainText("received-invite")
        self.window.guest_name.clear()
        self.window.join_button.click()
        self.assertEqual(self.controller.calls, [("join", "received-invite", 0, "Amigo", "quic")])

    def test_compact_layout_keeps_rows_and_controls_readable(self):
        self.host()
        request = self.pending(name="Jogador com nome suficientemente comprido")
        second_request = self.pending("second-guest", "Mesmo nome")
        peer = self.peer(rtt_ms=42)
        for width, height in ((820, 600), (960, 690)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertGreaterEqual(self.window.peers_scroll.height(), 116)
            self.assertGreaterEqual(request.height(), 54)
            self.assertLess(request.geometry().bottom(), second_request.geometry().top())
            self.assertLess(request.name_label.mapTo(request, request.name_label.rect().topRight()).x(),
                            request.approve_button.geometry().left())
            self.assertLess(request.approve_button.geometry().right(), request.reject_button.geometry().left())
            self.assertLessEqual(request.reject_button.geometry().right(), request.width())
            self.assertLess(self.window.copy_button.geometry().right(), self.window.renew_invite_button.geometry().left())
            self.assertLess(peer.state_label.geometry().right(), peer.remove_button.geometry().left())
            self.assertLessEqual(peer.remove_button.geometry().right(), peer.width())
            viewport = self.window.session_page
            self.assertEqual(viewport.horizontalScrollBar().maximum(), 0)

    def test_long_unbroken_names_do_not_push_actions_outside_viewport(self):
        self.host()
        full_name = "W" * 60
        request = self.pending(name=full_name)
        self.emit("peer", peer_id="long-peer", name=full_name, state="connected")
        peer = self.window._peer_rows["long-peer"]
        self.window.resize(820, 600)
        self.app.processEvents()
        for row, button in ((request, request.reject_button), (peer, peer.remove_button)):
            self.assertLessEqual(row.width(), self.window.session_page.viewport().width())
            self.assertLessEqual(button.geometry().right(), row.width())
            self.assertTrue(row.name_label.text().endswith("…"))
            self.assertEqual(row.name_label.accessibleName(), full_name)
            self.assertIn(full_name, row.name_label.toolTip())

    def test_approve_can_remember_verified_computer_but_defaults_to_one_entry(self):
        self.host()
        fingerprint = "a" * 64
        request = self.pending(can_trust=True, fingerprint=fingerprint)
        self.assertFalse(request.trust_checkbox.isHidden())
        self.assertFalse(request.trust_checkbox.isChecked())
        self.assertIn(fingerprint[:12], request.detail_label.text())
        request.trust_checkbox.setChecked(True)
        request.approve_button.click()
        request.approve_button.click()
        self.assertEqual(self.controller.approvals, [("first1-guest", True)])
        self.assertFalse(request.trust_checkbox.isEnabled())
        self.emit("admission", peer_id="first1-guest", state="approved")
        another = self.pending("second-guest", can_trust=True, fingerprint="b" * 64)
        another.approve_button.click()
        self.assertEqual(self.controller.approvals[-1], ("second-guest", False))

    def test_old_guest_has_no_trust_option_and_cannot_be_remembered(self):
        self.host()
        request = self.pending(can_trust=False)
        self.assertTrue(request.trust_checkbox.isHidden())
        self.window._admission_action("first1-guest", "approve", True)
        self.assertEqual(self.controller.approvals, [("first1-guest", False)])
        peer = self.peer(can_trust=False)
        self.assertTrue(peer.trust_button.isHidden())
        self.window._set_peer_trusted("peer-a", True)
        self.assertNotIn(("set_trusted", "peer-a", True), self.controller.calls)

    def test_participant_trust_uses_peer_id_and_waits_for_backend(self):
        self.host()
        fingerprint = "a" * 64
        peer = self.peer(can_trust=True, fingerprint=fingerprint, trusted=False)
        self.assertFalse(peer.trust_button.isHidden())
        self.assertEqual(peer.trust_button.text(), "Confiar")
        peer.trust_button.click()
        self.window._set_peer_trusted("peer-a", True)
        self.assertEqual(self.controller.calls, [("set_trusted", "peer-a", True)])
        self.assertFalse(peer.trust_button.isEnabled())
        self.assertFalse(peer.remove_button.isEnabled())
        self.emit("peer", peer_id="peer-a", trusted=True)
        self.assertTrue(peer.trust_button.isEnabled())
        self.assertEqual(peer.trust_button.text(), "Confiável")
        peer.trust_button.click()
        self.assertEqual(self.controller.calls[-1], ("set_trusted", "peer-a", False))
        self.emit("trusted", computers=[])
        self.assertTrue(peer.trust_button.isEnabled())
        self.assertEqual(peer.trust_button.text(), "Confiar")
        self.assertEqual(self.window._peers["peer-a"]["state"], "connected")

    def test_trusted_list_uses_full_keys_and_survives_room_stop(self):
        self.host()
        first, second = "a" * 64, "a" * 12 + "b" * 52
        self.emit("trusted", computers=[{"fingerprint": first, "name": "Mesmo nome"},
                                         {"fingerprint": second, "name": "Mesmo nome"}])
        self.assertIn("2", self.window.trusted_session_button.text())
        self.emit("status", state="stopped")
        self.assertEqual(len(self.window._trusted_computers), 2)
        self.window.trusted_button.click()
        self.app.processEvents()
        self.assertTrue(self.window._trusted_dialog.isVisible())
        self.window._trusted_rows[second].forget_button.click()
        self.window._forget_trusted(second)
        self.assertEqual(self.controller.calls, [("forget_trusted", second)])
        self.assertFalse(self.window._trusted_rows[second].forget_button.isEnabled())
        self.emit("trusted", computers=[{"fingerprint": first, "name": "Outro apelido"}])
        self.assertNotIn(second, self.window._trusted_rows)
        self.assertIn(first, self.window._trusted_rows)
        self.assertEqual(self.window._trusted_rows[first].name_label.accessibleName(), "Outro apelido")
        self.assertIn("1", self.window.trusted_button.text())

    def test_forgetting_connected_computer_keeps_participant(self):
        self.host()
        fingerprint = "c" * 64
        peer = self.peer(can_trust=True, fingerprint=fingerprint, trusted=True)
        self.emit("trusted", computers=[{"fingerprint": fingerprint, "name": "Amigo"}])
        self.window._forget_trusted(fingerprint)
        self.emit("trusted", computers=[])
        self.assertIs(self.window._peer_rows["peer-a"], peer)
        self.assertEqual(self.window._peers["peer-a"]["state"], "connected")
        self.assertEqual(peer.trust_button.text(), "Confiar")
        self.assertEqual(self.controller.calls, [("forget_trusted", fingerprint)])

    def test_trust_errors_reenable_correct_controls_and_preserve_session(self):
        self.host()
        fingerprint = "d" * 64
        peer = self.peer(can_trust=True, fingerprint=fingerprint, trusted=False)
        peer.trust_button.click()
        self.emit("error", operation="trust", peer_id="peer-a", message="Falha ao salvar confiança.")
        self.assertTrue(peer.trust_button.isEnabled())
        self.assertEqual(self.window._state, "hosting")
        self.assertTrue(self.window._session)
        self.emit("trusted", computers=[{"fingerprint": fingerprint, "name": "Amigo"}])
        self.window._open_trusted_computers()
        self.app.processEvents()
        self.window._trusted_rows[fingerprint].forget_button.click()
        self.emit("error", operation="trust", fingerprint=fingerprint, message="Falha ao esquecer.")
        self.assertTrue(self.window._trusted_rows[fingerprint].forget_button.isEnabled())
        self.assertIn("esquecer", self.window.trust_notice.text())
        self.assertFalse(self.window.trust_notice.isHidden())
        self.assertEqual(self.window._code, "old-invite")

    def test_approval_error_with_room_operation_preserves_renewal_state(self):
        self.host()
        request = self.pending(can_trust=True, fingerprint="e" * 64)
        request.trust_checkbox.setChecked(True)
        request.approve_button.click()
        self.window.renew_invite_button.click()
        self.emit("error", operation="room", peer_id="first1-guest", message="Aprovação não foi concluída.")
        self.assertTrue(request.approve_button.isEnabled())
        self.assertTrue(request.trust_checkbox.isEnabled())
        self.assertTrue(request.trust_checkbox.isChecked())
        self.assertFalse(self.window.renew_invite_button.isEnabled())
        self.assertTrue(self.window._session)

    def test_initial_trusted_read_populates_offline_management(self):
        class SavedComputersController(FakeController):
            def trusted_computers(self):
                self.event.emit({"type": "trusted", "computers": [{"fingerprint": "f" * 64, "name": "Computador salvo"}]})

        self.window.close()
        self.window.deleteLater()
        self.controller = SavedComputersController()
        self.window = MainWindow(self.controller)
        self.window.show()
        self.app.processEvents()
        self.assertFalse(self.window._session)
        self.assertEqual(self.window.trusted_button.text(), "Computadores de confiança (1)")
        self.window.trusted_button.click()
        self.app.processEvents()
        self.assertIn("f" * 64, self.window._trusted_rows)

    def test_trusted_read_failure_stays_idle_and_guest_cannot_manage(self):
        with patch.object(self.controller, "trusted_computers", side_effect=RuntimeError("Lista indisponível."), create=True):
            self.window._load_trusted_computers()
        self.assertFalse(self.window._session)
        self.assertEqual(self.window._state, "idle")
        self.assertIn("indisponível", self.window.notice_text.text())
        self.emit("trusted", computers=[{"fingerprint": "f" * 64, "name": "Amigo"}])
        self.window._set_tab("guest", animate=False)
        self.window._open_trusted_computers()
        self.window._forget_trusted("f" * 64)
        self.assertIsNone(self.window._trusted_dialog)
        self.assertEqual(self.controller.calls, [])

    def test_trust_controls_fit_compact_rows_and_dialog(self):
        self.host()
        fingerprint = "a" * 64
        request = self.pending(name="W" * 60, can_trust=True, fingerprint=fingerprint)
        peer = self.peer(can_trust=True, fingerprint=fingerprint, trusted=True)
        self.emit("trusted", computers=[{"fingerprint": fingerprint, "name": "W" * 60}])
        for width, height in ((820, 600), (960, 690)):
            self.window.resize(width, height)
            self.app.processEvents()
            self.assertLess(request.trust_checkbox.mapTo(request, request.trust_checkbox.rect().topRight()).x(),
                            request.approve_button.geometry().left())
            self.assertLess(peer.state_label.geometry().right(), peer.trust_button.geometry().left())
            self.assertLessEqual(peer.trust_button.geometry().right(), peer.width())
            self.assertLess(peer.trust_button.geometry().bottom(), peer.remove_button.geometry().top())
            self.assertGreaterEqual(self.window.peers_scroll.height(), 116)
        self.window._open_trusted_computers()
        self.app.processEvents()
        row = self.window._trusted_rows[fingerprint]
        self.assertLess(row.name_label.mapTo(row, row.name_label.rect().topRight()).x(), row.forget_button.geometry().left())
        self.assertLessEqual(row.forget_button.geometry().right(), row.width())


if __name__ == "__main__":
    unittest.main()
