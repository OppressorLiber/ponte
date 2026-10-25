"""The Ponte desktop interface. Transport is injected through ``controller``.

Intent: let a small Minecraft group share one open LAN world, calmly.
The invitation / Minecraft address is the focal point. Porcelain surfaces and
one river-blue action keep the hierarchy quiet; a bridge is the signature.
Segoe UI Variable uses weight before size, a four-point spacing rhythm and one
softly elevated workspace. Native Qt controls provide keyboard accessibility.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSettings, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QDialog, QFrame,
    QGraphicsDropShadowEffect, QGraphicsOpacityEffect, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from pontelan import __version__ as VERSION
from pontelan.latency import format_rtt

COLORS = {
    "porcelain": "#F5F6F8", "paper": "#FFFFFF", "inset": "#F5F6F9",
    "ink": "#20242C", "secondary": "#596170", "muted": "#737D8C",
    "edge": "#E4E7ED", "river": "#2367D5", "river_hover": "#1B58BC",
    "river_wash": "#EAF2FF", "green": "#217A54", "green_wash": "#EAF5EF",
    "error": "#A23D3A", "error_wash": "#FCF0EF",
}


def _asset(name: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / "assets" / name


def _font(points: float, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    font = QFont("Segoe UI Variable", pointSize=round(points))
    font.setPointSizeF(points)
    font.setWeight(weight)
    return font


def _label(text: str, *, role: str = "body", wrap: bool = False, name: str = "") -> QLabel:
    label = QLabel(text)
    label.setProperty("role", role)
    label.setObjectName(name)
    label.setWordWrap(wrap)
    label.setTextFormat(Qt.TextFormat.PlainText)
    roles = {
        "title": (22.0, QFont.Weight.DemiBold),
        "brand": (17.0, QFont.Weight.DemiBold),
        "heading": (12.0, QFont.Weight.DemiBold),
        "body": (11.0, QFont.Weight.Normal),
        "label": (10.0, QFont.Weight.DemiBold),
        "caption": (9.5, QFont.Weight.Normal),
        "meta": (9.0, QFont.Weight.Normal),
    }
    label.setFont(_font(*roles.get(role, roles["body"])))
    return label


def _button(text: str, name: str, kind: str = "secondary") -> QPushButton:
    button = QPushButton(text)
    button.setObjectName(name)
    button.setProperty("kind", kind)
    button.setCursor(Qt.CursorShape.PointingHandCursor)
    button.setMinimumHeight(38 if kind != "quiet" else 30)
    button.setFont(_font(10.5, QFont.Weight.DemiBold))
    button.setAutoDefault(False)
    return button


def _format_bytes(value: int) -> str:
    size = float(max(0, value))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return "0 B"


def _scroll_page(content: QWidget, name: str, footer: QPushButton | None = None) -> QWidget:
    """Permit compact windows and large Windows text without squeezed rows."""
    scroll = QScrollArea()
    scroll.setObjectName(name + "Viewport")
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
    scroll.setMinimumHeight(0)
    if footer is None:
        scroll.setWidget(content)
        return scroll
    # Keep the primary action visible even with compact windows or large text.
    content.layout().removeWidget(footer)
    content.layout().takeAt(content.layout().count() - 1)  # trailing spacing
    content.layout().takeAt(content.layout().count() - 1)  # trailing stretch
    scroll.setWidget(content)
    page = QWidget()
    page.setObjectName(name)
    page.setProperty("role", "page")
    layout = QVBoxLayout(page)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(12)
    layout.addWidget(scroll, 1)
    layout.addWidget(footer)
    return page


class BridgeIllustration(QWidget):
    """Small static product illustration; deliberately not a connection meter."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setAccessibleName("Uma ponte entre o mundo do anfitrião e os amigos")

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        left, right, baseline = 26.0, width - 26.0, 50.0
        center = width / 2
        painter.setPen(QPen(QColor("#DBE5F4"), 2))
        painter.drawLine(int(left), int(baseline), int(right), int(baseline))
        painter.setPen(QPen(QColor("#9CB5D9"), 2.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        path = QPainterPath()
        path.moveTo(left + 9, 24)
        path.cubicTo(center - 32, 24, center - 28, 45, center, 45)
        path.cubicTo(center + 28, 45, center + 32, 24, right - 9, 24)
        painter.drawPath(path)
        painter.drawLine(int(left + 9), 17, int(left + 9), 51)
        painter.drawLine(int(right - 9), 17, int(right - 9), 51)
        painter.setPen(QPen(QColor("#C3D2E7"), 1.5))
        for fraction in (0.31, 0.5, 0.69):
            x = left + (right - left) * fraction
            top = 45 if fraction == 0.5 else 39
            painter.drawLine(int(x), top, int(x), 50)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#EAF2FF"))
        painter.drawEllipse(int(left - 11), 35, 22, 22)
        painter.drawEllipse(int(right - 11), 35, 22, 22)
        painter.setBrush(QColor("#6B8DBB"))
        painter.drawRoundedRect(int(left - 4), 42, 8, 8, 2, 2)
        painter.drawRoundedRect(int(right - 4), 42, 8, 8, 2, 2)


class _NameLabel(QLabel):
    """Keep arbitrary user names from displacing the row's action buttons."""

    def __init__(self):
        super().__init__()
        self._full_text = ""
        self.setProperty("role", "label")
        self.setFont(_font(10, QFont.Weight.DemiBold))
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def setText(self, text: str):
        self._full_text = text
        self.setAccessibleName(text)
        self._elide()

    def _elide(self):
        super().setText(self.fontMetrics().elidedText(self._full_text, Qt.TextElideMode.ElideRight, self.width()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()


class AdmissionRow(QFrame):
    """One entry request; actions always retain the request's stable ID."""

    def __init__(self, admission: dict, on_action, parent=None):
        super().__init__(parent)
        self.peer_id = str(admission["peer_id"])
        self.setObjectName(f"admission_{self.peer_id}")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(8)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name_label = _NameLabel()
        self.detail_label = _label(f"Pedido · {self.peer_id[:6]}", role="caption")
        self.name_label.setToolTip("Nome informado pela pessoa; não é uma identidade verificada do Minecraft.")
        text.addWidget(self.name_label)
        text.addWidget(self.detail_label)
        self.trust_checkbox = QCheckBox("Confiar neste computador")
        self.trust_checkbox.setObjectName("rememberComputerCheckbox")
        self.trust_checkbox.setFont(_font(9))
        self.trust_checkbox.setToolTip("Nas próximas salas, este computador entra com um convite válido sem nova aprovação. Você pode desfazer depois.")
        text.addWidget(self.trust_checkbox)
        layout.addLayout(text, 1)
        self.approve_button = _button("Aprovar", "approveEntryButton", "approval")
        self.reject_button = _button("Recusar", "rejectEntryButton")
        self.approve_button.clicked.connect(lambda: on_action(self.peer_id, "approve", self.trust_checkbox.isChecked()))
        self.reject_button.clicked.connect(lambda: on_action(self.peer_id, "reject"))
        layout.addWidget(self.approve_button)
        layout.addWidget(self.reject_button)
        self.update_admission(admission)

    def update_admission(self, admission: dict):
        name = str(admission.get("name") or "Amigo")
        self.name_label.setText(name)
        self.name_label.setToolTip(f"{name}\nNome informado pela pessoa; não é uma identidade verificada do Minecraft.")
        self._can_trust = bool(admission.get("can_trust") and admission.get("fingerprint"))
        fingerprint = str(admission.get("fingerprint") or "")
        self.detail_label.setText(f"Chave · {fingerprint[:12]}" if self._can_trust else f"Pedido · {self.peer_id[:6]}")
        self.detail_label.setToolTip("Identifica a chave local deste computador." if self._can_trust else "")
        self.trust_checkbox.setVisible(self._can_trust)
        if not self._can_trust:
            self.trust_checkbox.setChecked(False)
        self.approve_button.setAccessibleName(f"Aprovar {name}, pedido {self.peer_id[:6]}")
        self.reject_button.setAccessibleName(f"Recusar {name}, pedido {self.peer_id[:6]}")

    def set_action_state(self, enabled: bool, action: str = ""):
        self.approve_button.setEnabled(enabled)
        self.reject_button.setEnabled(enabled)
        self.trust_checkbox.setEnabled(enabled)
        self.approve_button.setText("Aprovando…" if action == "approve" else "Aprovar")
        self.reject_button.setText("Recusando…" if action == "reject" else "Recusar")


class PeerRow(QFrame):
    def __init__(self, peer: dict, parent=None, *, on_remove=None, on_trust=None):
        super().__init__(parent)
        self.peer_id = str(peer.get("peer_id") or "unknown")
        self._on_remove = on_remove
        self._on_trust = on_trust
        self.setObjectName("peerRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 9, 0, 9)
        layout.setSpacing(10)
        self.avatar = _label("?", role="label")
        self.avatar.setObjectName("peerAvatar")
        self.avatar.setFixedSize(30, 30)
        self.avatar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.avatar)
        text = QVBoxLayout()
        text.setSpacing(1)
        self.name_label = _NameLabel()
        self.detail_label = _label("", role="caption", wrap=True)
        self.detail_label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
        text.addWidget(self.name_label)
        text.addWidget(self.detail_label)
        text.setAlignment(Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(text, 1)
        self.state_label = _label("", role="caption")
        layout.addWidget(self.state_label)
        self.remove_button = _button("Remover", "removePeerButton", "quiet")
        self.remove_button.setMinimumHeight(38)
        self.remove_button.setToolTip("Para voltar, precisa de nova aprovação.")
        self.remove_button.clicked.connect(lambda: self._on_remove(self.peer_id) if self._on_remove else None)
        actions = QVBoxLayout()
        actions.setSpacing(0)
        self.trust_button = _button("Confiar", "trustPeerButton", "quiet")
        self.trust_button.setFont(_font(9.5, QFont.Weight.DemiBold))
        self.trust_button.clicked.connect(lambda: self._on_trust(self.peer_id, not self._trusted) if self._on_trust else None)
        actions.addWidget(self.trust_button)
        actions.addWidget(self.remove_button)
        layout.addLayout(actions)
        self.update_peer(peer)

    def update_peer(self, peer: dict):
        name = str(peer.get("name") or "Outro jogador")
        self.name_label.setText(name)
        self.name_label.setToolTip(name)
        self.avatar.setText(name[:1].upper())
        state = str(peer.get("state", "connecting"))
        self._peer_state = state
        self._can_trust = bool(peer.get("can_trust") and peer.get("fingerprint"))
        self._trusted = bool(peer.get("trusted"))
        self.trust_button.setAccessibleName(f"{'Deixar de confiar no' if self._trusted else 'Confiar no'} computador de {name}")
        self.trust_button.setToolTip("Deixar de confiar: a próxima entrada precisará de aprovação. A partida continua."
                                     if self._trusted else "Entrar automaticamente nas próximas salas com um convite válido.")
        self.remove_button.setAccessibleName(f"Remover {name} da sessão")
        self.set_manageable(False)
        states = {
            "connecting": "Conectando", "connected": "Conectado",
            "disconnected": "Desconectado", "failed": "Falha na conexão",
        }
        self.state_label.setText(states.get(state, state))
        self.state_label.setProperty("connected", state == "connected")
        self.state_label.style().unpolish(self.state_label)
        self.state_label.style().polish(self.state_label)
        latency = peer.get("rtt_ms")
        route = peer.get("route")
        if state == "connected":
            detail = "Conexão direta" if route else "Conexão estabelecida"
            if isinstance(latency, (int, float)):
                detail += " · " + format_rtt(latency)
        else:
            detail = {"connecting": "Negociando a rota entre os computadores…",
                      "disconnected": "O jogador saiu da sessão.",
                      "failed": "Não foi possível estabelecer uma rota."}.get(state, "")
        self.detail_label.setText(detail)
        self.detail_label.setToolTip(f"Rota ICE: {route}" if route else "")
        if state == "connected" and peer.get("rtt_p50_ms") is not None:
            self.detail_label.setToolTip(
                f"RTT do aplicativo · último {format_rtt(latency)}\n"
                f"Mediana {format_rtt(peer.get('rtt_p50_ms'))} · p95 {format_rtt(peer.get('rtt_p95_ms'))}\n"
                f"Rota ICE: {route}")

    def set_manageable(self, allowed: bool, busy: bool = False, action: str = ""):
        self.remove_button.setVisible(allowed and self._peer_state in ("connecting", "connected"))
        self.remove_button.setEnabled(not busy)
        self.remove_button.setText("Removendo…" if action == "remove" else "Remover")
        self.trust_button.setVisible(allowed and self._can_trust and self._peer_state == "connected")
        self.trust_button.setEnabled(not busy)
        self.trust_button.setText("Salvando…" if action in ("trust", "untrust") else "Confiável" if self._trusted else "Confiar")


class TrustedComputerRow(QFrame):
    def __init__(self, computer: dict, on_forget, parent=None):
        super().__init__(parent)
        self.fingerprint = str(computer["fingerprint"])
        self.setObjectName("trustedComputerRow")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(2)
        self.name_label = _NameLabel()
        name = str(computer.get("name") or "Amigo")
        self.name_label.setText(name)
        self.name_label.setToolTip(name)
        text.addWidget(self.name_label)
        text.addWidget(_label(f"Chave · {self.fingerprint[:12]}", role="caption"))
        layout.addLayout(text, 1)
        self.forget_button = _button("Esquecer", "forgetTrustedButton")
        self.forget_button.setAccessibleName(f"Esquecer computador de {name}, chave {self.fingerprint[:12]}")
        self.forget_button.setToolTip("A próxima entrada precisa de aprovação. A partida atual continua.")
        self.forget_button.clicked.connect(lambda: on_forget(self.fingerprint))
        layout.addWidget(self.forget_button)

    def set_pending(self, pending: bool):
        self.forget_button.setEnabled(not pending)
        self.forget_button.setText("Esquecendo…" if pending else "Esquecer")


class MainWindow(QMainWindow):
    """UI contract: controller.event is a Qt Signal(dict); actions are methods."""

    def __init__(self, controller):
        super().__init__()
        self.controller = controller
        self.settings = QSettings("Ponte", "Ponte")
        self._role = "host"
        self._state = "idle"
        self._busy = False
        self._session = False
        self._code = ""
        self._address = ""
        self._peers: dict[str, dict] = {}
        self._peer_rows: dict[str, PeerRow] = {}
        self._admissions: dict[str, dict] = {}
        self._admission_rows: dict[str, AdmissionRow] = {}
        self._peer_actions: dict[str, str] = {}
        self._renewing_invite = False
        self._trusted_computers: dict[str, dict] = {}
        self._trusted_rows: dict[str, TrustedComputerRow] = {}
        self._forgetting: set[str] = set()
        self._trusted_dialog: QDialog | None = None
        self._animation = None
        self._update_state = ""
        self._update_version = ""
        self._update_message = ""
        self._update_progress: int | None = None
        self._update_install_requested = False
        self._update_feedback_visible = False
        self._update_timer = QTimer(self)
        self._update_timer.setSingleShot(True)
        self._update_timer.timeout.connect(self._hide_update_feedback)
        self._notice_timer = QTimer(self)
        self._notice_timer.setSingleShot(True)
        self._notice_timer.timeout.connect(self._hide_notice)
        self.setObjectName("mainWindow")
        self.setWindowTitle("Ponte")
        self.setWindowIcon(QIcon(str(_asset("ponte.svg"))))
        self.setMinimumSize(820, 600)
        self.resize(960, 690)
        self.setFont(_font(11))
        self._build_ui()
        self._apply_styles()
        self.controller.event.connect(self.handle_event)
        self._set_tab("host", animate=False)
        if callable(getattr(self.controller, "trusted_computers", None)):
            QTimer.singleShot(0, self._load_trusted_computers)
        QTimer.singleShot(350, self._scan_worlds)

    def _build_ui(self):
        root = QWidget()
        root.setObjectName("appRoot")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 22, 30, 15)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setSpacing(11)
        logo = QLabel()
        logo.setObjectName("brandIcon")
        logo.setPixmap(QIcon(str(_asset("ponte.svg"))).pixmap(43, 43))
        logo.setFixedSize(43, 43)
        header.addWidget(logo)
        brand = QVBoxLayout()
        brand.setSpacing(0)
        brand.addWidget(_label("Ponte", role="brand"))
        brand.addWidget(_label("Minecraft Java, entre amigos", role="caption"))
        header.addLayout(brand)
        header.addStretch(1)
        self.status_badge = _label("Pronto para jogar", role="caption", name="statusBadge")
        self.status_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(self.status_badge)
        layout.addLayout(header)
        layout.addSpacing(27)

        self.heading = _label("Seu mundo. Seus amigos.", role="title", name="pageTitle")
        layout.addWidget(self.heading)
        self.subheading = _label("Abra um mundo na rede local e convide quem está longe.", role="body", wrap=True)
        self.subheading.setObjectName("pageSubtitle")
        layout.addSpacing(5)
        layout.addWidget(self.subheading)
        layout.addSpacing(22)

        body = QHBoxLayout()
        body.setSpacing(28)
        self.workspace = QFrame()
        self.workspace.setObjectName("workspace")
        self.workspace.setMinimumWidth(455)
        surface_shadow = QGraphicsDropShadowEffect(self.workspace)
        surface_shadow.setColor(QColor(28, 42, 66, 16))
        surface_shadow.setBlurRadius(28)
        surface_shadow.setOffset(0, 6)
        self.workspace.setGraphicsEffect(surface_shadow)
        workspace_layout = QVBoxLayout(self.workspace)
        workspace_layout.setContentsMargins(22, 21, 22, 21)
        workspace_layout.setSpacing(17)

        self.segment = QFrame()
        self.segment.setObjectName("segmentedControl")
        segment_layout = QHBoxLayout(self.segment)
        segment_layout.setContentsMargins(4, 4, 4, 4)
        segment_layout.setSpacing(3)
        self.host_tab = _button("Compartilhar mundo", "hostTab", "segment")
        self.guest_tab = _button("Entrar com convite", "guestTab", "segment")
        self.host_tab.setCheckable(True)
        self.guest_tab.setCheckable(True)
        self.tabs = QButtonGroup(self)
        self.tabs.setExclusive(True)
        self.tabs.addButton(self.host_tab)
        self.tabs.addButton(self.guest_tab)
        segment_layout.addWidget(self.host_tab)
        segment_layout.addWidget(self.guest_tab)
        self.host_tab.clicked.connect(lambda: self._set_tab("host"))
        self.guest_tab.clicked.connect(lambda: self._set_tab("guest"))
        workspace_layout.addWidget(self.segment)

        self.pages = QStackedWidget()
        self.pages.setObjectName("mainPages")
        self.host_page = self._build_host_page()
        self.guest_page = self._build_guest_page()
        self.session_page = self._build_session_page()
        self.pages.addWidget(self.host_page)
        self.pages.addWidget(self.guest_page)
        self.pages.addWidget(self.session_page)
        workspace_layout.addWidget(self.pages, 1)
        body.addWidget(self.workspace, 1)
        body.addWidget(self._build_guide())
        layout.addLayout(body, 1)
        layout.addSpacing(13)

        self.notice = QFrame()
        self.notice.setObjectName("notice")
        notice_layout = QHBoxLayout(self.notice)
        notice_layout.setContentsMargins(12, 9, 8, 9)
        notice_layout.setSpacing(8)
        self.notice_text = _label("", role="caption", wrap=True, name="noticeText")
        self.notice_text.setMaximumHeight(76)
        self.notice_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        notice_layout.addWidget(self.notice_text, 1)
        self.open_diagnostics = _button("Abrir pasta", "openDiagnostics", "quiet")
        self.open_diagnostics.clicked.connect(self._open_diagnostics_folder)
        self.open_diagnostics.hide()
        notice_layout.addWidget(self.open_diagnostics)
        self.dismiss_notice = _button("×", "dismissNotice", "quiet")
        self.dismiss_notice.setAccessibleName("Fechar mensagem")
        self.dismiss_notice.setFixedWidth(28)
        self.dismiss_notice.clicked.connect(self._hide_notice)
        notice_layout.addWidget(self.dismiss_notice)
        self.notice.hide()
        layout.addWidget(self.notice)

        # An update should never compete with the invitation or interrupt play.
        # Match the quiet porcelain workspace, its caption hierarchy and 4 px
        # spacing rhythm; river blue belongs only to the ready-to-install action.
        self.update_status = QFrame()
        self.update_status.setObjectName("updateStatus")
        update_layout = QHBoxLayout(self.update_status)
        update_layout.setContentsMargins(12, 7, 8, 7)
        update_layout.setSpacing(8)
        self.update_mark = _label("↑", role="label", name="updateMark")
        self.update_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.update_mark.setFixedSize(22, 22)
        update_layout.addWidget(self.update_mark)
        self.update_text = _label("", role="caption", wrap=True, name="updateText")
        self.update_text.setAccessibleName("Estado das atualizações")
        update_layout.addWidget(self.update_text, 1)
        self.install_update_button = _button("Reiniciar e atualizar", "installUpdateButton", "update")
        self.install_update_button.setMinimumHeight(30)
        self.install_update_button.setFont(_font(9.5, QFont.Weight.DemiBold))
        self.install_update_button.setToolTip("Fecha a Ponte, instala a atualização e abre a nova versão")
        self.install_update_button.clicked.connect(self._install_update)
        self.install_update_button.hide()
        update_layout.addWidget(self.install_update_button)
        self.update_status.hide()
        layout.addWidget(self.update_status)

        footer = QHBoxLayout()
        footer.setSpacing(10)
        self.footer_label = _label("P2P direto · só a conexão do jogo", role="meta", name="footerConnection")
        footer.addWidget(self.footer_label, 1)
        self.motion_toggle = QCheckBox("Reduzir animações")
        self.motion_toggle.setObjectName("reduceMotion")
        self.motion_toggle.setFont(_font(9))
        self.motion_toggle.setChecked(self.settings.value("reduce_motion", False, type=bool))
        self.motion_toggle.toggled.connect(lambda enabled: self.settings.setValue("reduce_motion", enabled))
        footer.addWidget(self.motion_toggle)
        self.diagnostics_button = _button("Diagnóstico", "diagnosticsButton", "quiet")
        self.diagnostics_button.setToolTip("Salvar um relatório técnico para investigar a conexão")
        self.diagnostics_button.clicked.connect(self._diagnostics)
        footer.addWidget(self.diagnostics_button)
        self.updates_button = _button("Atualizações", "updatesButton", "quiet")
        self.updates_button.setToolTip(f"Buscar atualizações da Ponte · versão {VERSION}")
        self.updates_button.clicked.connect(self._check_updates)
        footer.addWidget(self.updates_button)
        footer.addWidget(_label(VERSION, role="meta"))
        layout.addSpacing(5)
        layout.addLayout(footer)

        QWidget.setTabOrder(self.host_tab, self.guest_tab)
        QWidget.setTabOrder(self.guest_tab, self.world_name)
        QWidget.setTabOrder(self.world_name, self.port_input)
        QWidget.setTabOrder(self.port_input, self.world_combo)
        QWidget.setTabOrder(self.world_combo, self.scan_button)
        QWidget.setTabOrder(self.scan_button, self.host_button)
        QWidget.setTabOrder(self.guest_name, self.invite_input)
        QWidget.setTabOrder(self.invite_input, self.join_button)
        QWidget.setTabOrder(self.copy_button, self.renew_invite_button)

    def _build_host_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("hostPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_label("O mundo fica no seu computador.", role="heading"))
        layout.addSpacing(5)
        layout.addWidget(_label("A Ponte cria o caminho para os seus amigos entrarem.", role="caption", wrap=True))
        layout.addSpacing(20)

        fields = QHBoxLayout()
        fields.setSpacing(12)
        name_column = QVBoxLayout()
        name_column.setSpacing(6)
        name_label = _label("Nome do mundo", role="label")
        self.world_name = QLineEdit()
        self.world_name.setObjectName("worldNameInput")
        self.world_name.setPlaceholderText("Meu mundo")
        self.world_name.setMaxLength(80)
        self.world_name.setAccessibleName("Nome do mundo")
        name_label.setBuddy(self.world_name)
        name_column.addWidget(name_label)
        name_column.addWidget(self.world_name)
        fields.addLayout(name_column, 1)
        port_column = QVBoxLayout()
        port_column.setSpacing(6)
        port_label = _label("Porta LAN", role="label")
        self.port_input = QSpinBox()
        self.port_input.setObjectName("portInput")
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(25565)
        self.port_input.setGroupSeparatorShown(False)
        self.port_input.setAccessibleName("Porta LAN mostrada no chat do Minecraft")
        self.port_input.setFixedWidth(112)
        self.port_input.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
        port_label.setBuddy(self.port_input)
        port_column.addWidget(port_label)
        port_column.addWidget(self.port_input)
        fields.addLayout(port_column)
        layout.addLayout(fields)
        layout.addSpacing(15)

        detection = QHBoxLayout()
        detection.setSpacing(8)
        self.world_combo = QComboBox()
        self.world_combo.setObjectName("worldsCombo")
        self.world_combo.setAccessibleName("Mundos LAN encontrados")
        self.world_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.world_combo.setMinimumWidth(0)
        self.world_combo.addItem("Detectar um mundo aberto…", None)
        self.world_combo.currentIndexChanged.connect(self._select_world)
        self.scan_button = _button("Buscar", "scanWorldsButton")
        self.scan_button.setToolTip("Procurar mundos anunciados na rede local")
        self.scan_button.clicked.connect(self._scan_worlds)
        detection.addWidget(self.world_combo, 1)
        detection.addWidget(self.scan_button)
        layout.addLayout(detection)
        self.scan_hint = _label("Ou digite a porta que aparece no chat do jogo.", role="caption", wrap=True, name="scanHint")
        layout.addSpacing(7)
        layout.addWidget(self.scan_hint)
        layout.addSpacing(8)
        self.trusted_button = _button("Computadores de confiança (0)", "trustedComputersButton", "quiet")
        self.trusted_button.clicked.connect(self._open_trusted_computers)
        layout.addWidget(self.trusted_button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)
        layout.addSpacing(20)
        self.host_button = _button("Criar convite", "createInviteButton", "primary")
        self.host_button.setMinimumHeight(42)
        self.host_button.clicked.connect(self._host)
        self.world_name.returnPressed.connect(self._host)
        layout.addWidget(self.host_button)
        return _scroll_page(page, "hostScroll", self.host_button)

    def _build_guest_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("guestPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(_label("Seu lugar no mundo já está esperando.", role="heading"))
        layout.addSpacing(5)
        layout.addWidget(_label("Peça o convite a quem abriu o mundo.", role="caption", wrap=True))
        layout.addSpacing(20)
        name_label = _label("Seu nome", role="label")
        self.guest_name = QLineEdit()
        self.guest_name.setObjectName("guestNameInput")
        self.guest_name.setMaxLength(60)
        self.guest_name.setText(self.settings.value("guest_name", "Amigo", type=str))
        self.guest_name.setAccessibleName("Seu nome no pedido de entrada")
        self.guest_name.setToolTip("O anfitrião verá este nome ao aprovar sua entrada. Não verifica sua identidade no Minecraft.")
        name_label.setBuddy(self.guest_name)
        layout.addWidget(name_label)
        layout.addSpacing(6)
        layout.addWidget(self.guest_name)
        layout.addSpacing(15)
        label = _label("Convite do anfitrião", role="label")
        layout.addWidget(label)
        layout.addSpacing(7)
        self.invite_input = QPlainTextEdit()
        self.invite_input.setObjectName("inviteInput")
        self.invite_input.setAccessibleName("Convite do anfitrião")
        self.invite_input.setPlaceholderText("Cole o convite aqui…")
        self.invite_input.setTabChangesFocus(True)
        self.invite_input.setFixedHeight(98)
        label.setBuddy(self.invite_input)
        layout.addWidget(self.invite_input, 1)
        layout.addSpacing(8)
        layout.addWidget(_label("O endereço para o Minecraft aparece na próxima etapa.", role="caption", wrap=True))
        layout.addStretch(1)
        layout.addSpacing(20)
        self.join_button = _button("Entrar no mundo", "joinButton", "primary")
        self.join_button.setMinimumHeight(42)
        self.join_button.clicked.connect(self._join)
        self.guest_name.returnPressed.connect(self._join)
        layout.addWidget(self.join_button)
        return _scroll_page(page, "guestScroll", self.join_button)

    def _build_session_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("sessionPage")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        top = QHBoxLayout()
        self.session_heading = _label("Seu mundo está aberto.", role="heading", name="sessionHeading")
        top.addWidget(self.session_heading, 1)
        self.stop_button = _button("Encerrar", "stopSessionButton", "quiet")
        self.stop_button.clicked.connect(self._stop)
        top.addWidget(self.stop_button)
        layout.addLayout(top)
        self.room_meta = _label("", role="caption", wrap=True, name="roomMetadata")
        layout.addWidget(self.room_meta)
        layout.addSpacing(17)

        self.connection_label = _label("Convite do seu mundo", role="label", name="connectionLabel")
        layout.addWidget(self.connection_label)
        layout.addSpacing(7)
        self.code_display = QPlainTextEdit()
        self.code_display.setObjectName("inviteCodeDisplay")
        self.code_display.setReadOnly(True)
        self.code_display.setTabChangesFocus(True)
        self.code_display.setAccessibleName("Convite para compartilhar")
        self.code_display.setFixedHeight(78)
        self.code_display.setFont(_font(9.5))
        layout.addWidget(self.code_display)

        self.address_display = QLineEdit()
        self.address_display.setObjectName("minecraftAddress")
        self.address_display.setReadOnly(True)
        self.address_display.setAccessibleName("Endereço para Conexão direta no Minecraft")
        self.address_display.setFont(_font(16, QFont.Weight.DemiBold))
        self.address_display.setMinimumHeight(51)
        self.address_display.setPlaceholderText("Preparando endereço…")
        layout.addWidget(self.address_display)
        layout.addSpacing(9)
        self.copy_button = _button("Copiar convite", "copyConnectionButton", "primary")
        self.copy_button.setMinimumHeight(40)
        self.copy_button.clicked.connect(self._copy_connection)
        connection_actions = QHBoxLayout()
        connection_actions.setSpacing(8)
        connection_actions.addWidget(self.copy_button, 1)
        self.renew_invite_button = _button("Renovar convite", "renewInviteButton")
        self.renew_invite_button.setMinimumHeight(40)
        self.renew_invite_button.clicked.connect(self._renew_invite)
        self.renew_invite_button.setToolTip("O convite antigo deixa de valer. Quem já entrou continua na sessão.")
        connection_actions.addWidget(self.renew_invite_button)
        layout.addLayout(connection_actions)
        layout.addSpacing(8)
        self.connection_hint = _label("Envie este convite para quem vai jogar com você.", role="caption", wrap=True, name="connectionHint")
        layout.addWidget(self.connection_hint)
        self.renew_hint = _label("Ao renovar, o convite antigo deixa de valer. Quem já entrou continua.",
                                role="caption", wrap=True, name="renewInviteHint")
        layout.addSpacing(4)
        layout.addWidget(self.renew_hint)
        layout.addSpacing(17)

        # Requests use the page's scrolling flow, so each row keeps its natural
        # height even in a compact window. Blue stays reserved for approval.
        self.admissions_section = QWidget()
        self.admissions_section.setObjectName("admissionsSection")
        admissions_layout = QVBoxLayout(self.admissions_section)
        admissions_layout.setContentsMargins(0, 0, 0, 12)
        admissions_layout.setSpacing(0)
        self.admissions_heading = _label("Pedidos de entrada", role="label", name="admissionsHeading")
        admissions_layout.addWidget(self.admissions_heading)
        self.admissions_layout = QVBoxLayout()
        self.admissions_layout.setContentsMargins(0, 0, 0, 0)
        self.admissions_layout.setSpacing(0)
        admissions_layout.addLayout(self.admissions_layout)
        layout.addWidget(self.admissions_section)
        self.admissions_section.hide()
        peers_header = QHBoxLayout()
        self.peers_heading = _label("Na sessão", role="label")
        peers_header.addWidget(self.peers_heading)
        peers_header.addStretch()
        self.latency_label = _label("", role="caption", name="latencyLabel")
        peers_header.addWidget(self.latency_label)
        self.trusted_session_button = _button("Confiança · 0", "sessionTrustedComputersButton", "quiet")
        self.trusted_session_button.setFont(_font(9.5))
        self.trusted_session_button.setToolTip("Gerenciar computadores de confiança, inclusive os que estão fora da sessão")
        self.trusted_session_button.clicked.connect(self._open_trusted_computers)
        peers_header.addWidget(self.trusted_session_button)
        layout.addLayout(peers_header)

        self.peers_scroll = QScrollArea()
        self.peers_scroll.setObjectName("peersScroll")
        self.peers_scroll.setWidgetResizable(True)
        self.peers_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.peers_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.peers_scroll.setMinimumHeight(116)
        self.peers_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.peers_container = QWidget()
        self.peers_container.setObjectName("peersContainer")
        self.peers_layout = QVBoxLayout(self.peers_container)
        self.peers_layout.setContentsMargins(0, 0, 5, 0)
        self.peers_layout.setSpacing(0)
        self.peers_empty = _label("Aguardando quem vai jogar.", role="caption", wrap=True, name="peersEmpty")
        self.peers_layout.addWidget(self.peers_empty)
        self.peers_layout.addStretch(1)
        self.peers_scroll.setWidget(self.peers_container)
        layout.addWidget(self.peers_scroll, 1)
        self.removal_hint = _label("Para voltar, precisa de nova aprovação.", role="caption", wrap=True,
                                   name="removePeerHint")
        layout.addSpacing(4)
        layout.addWidget(self.removal_hint)
        return _scroll_page(page, "sessionScroll")

    def _build_guide(self) -> QWidget:
        self.guide = QWidget()
        self.guide.setObjectName("guide")
        self.guide.setFixedWidth(249)
        layout = QVBoxLayout(self.guide)
        layout.setContentsMargins(0, 2, 1, 0)
        layout.setSpacing(0)
        self.bridge_illustration = BridgeIllustration()
        layout.addWidget(self.bridge_illustration)
        self.diagram_labels = QWidget()
        labels = QHBoxLayout(self.diagram_labels)
        labels.setContentsMargins(0, 0, 0, 0)
        labels.addWidget(_label("Seu mundo", role="caption"))
        labels.addStretch()
        labels.addWidget(_label("Seus amigos", role="caption"))
        layout.addWidget(self.diagram_labels)
        layout.addSpacing(25)
        self.guide_title = _label("Do LAN para o seu grupo.", role="heading", wrap=True)
        layout.addWidget(self.guide_title)
        layout.addSpacing(16)
        self.guide_steps = []
        for number in range(1, 4):
            row = QHBoxLayout()
            row.setSpacing(11)
            marker = _label(str(number), role="caption")
            marker.setObjectName("stepMarker")
            marker.setFixedSize(23, 23)
            marker.setAlignment(Qt.AlignmentFlag.AlignCenter)
            row.addWidget(marker, 0, Qt.AlignmentFlag.AlignTop)
            column = QVBoxLayout()
            column.setSpacing(3)
            title = _label("", role="label", wrap=True)
            detail = _label("", role="caption", wrap=True)
            column.addWidget(title)
            column.addWidget(detail)
            row.addLayout(column, 1)
            self.guide_steps.append((title, detail))
            layout.addLayout(row)
            layout.addSpacing(17)
        layout.addStretch(1)
        note = QFrame()
        note.setObjectName("modpackNote")
        note_layout = QVBoxLayout(note)
        note_layout.setContentsMargins(12, 11, 12, 11)
        note_layout.setSpacing(5)
        note_layout.addWidget(_label("Mesmo modpack, mesma versão.", role="label", wrap=True))
        self.modpack_detail = _label("Cada um usa seus próprios arquivos. A Ponte compartilha só a conexão.", role="caption", wrap=True)
        note_layout.addWidget(self.modpack_detail)
        layout.addWidget(note)
        return self.guide

    def _apply_styles(self):
        c = COLORS
        self.setStyleSheet(f"""
            QMainWindow, QWidget#appRoot {{ background: {c['porcelain']}; color: {c['ink']}; }}
            QDialog#trustedComputersDialog {{ background: {c['porcelain']}; color: {c['ink']}; }}
            QLabel {{ color: {c['ink']}; background: transparent; border: none; }}
            QLabel[role="caption"], QLabel[role="meta"] {{ color: {c['muted']}; }}
            QLabel#pageSubtitle {{ color: {c['secondary']}; }}
            QLabel#trustNotice {{ color: {c['error']}; }}
            QFrame#workspace {{ background: {c['paper']}; border: 1px solid #EBEDF2; border-radius: 16px; }}
            QFrame#segmentedControl {{ background: #F0F2F6; border-radius: 9px; }}
            QPushButton {{ background: {c['paper']}; border: 1px solid {c['edge']};
                border-radius: 7px; padding: 0 13px; color: {c['secondary']}; }}
            QPushButton:hover {{ background: #F3F5F9; border-color: #CFD5E0; }}
            QPushButton:pressed {{ background: #E9EDF4; }}
            QPushButton:focus {{ border: 2px solid #80A9EB; padding: 0 12px; }}
            QPushButton:disabled {{ color: #9199A7; background: #EFF1F5; border-color: #E8EBF0; }}
            QPushButton[kind="primary"] {{ background: {c['river']}; color: white; border-color: {c['river']}; }}
            QPushButton[kind="primary"]:hover {{ background: {c['river_hover']}; border-color: {c['river_hover']}; }}
            QPushButton[kind="primary"]:pressed {{ background: #154A9E; }}
            QPushButton[kind="primary"]:focus {{ border-color: #98BAF1; }}
            QPushButton[kind="primary"]:disabled {{ background: #A8BFE3; border-color: #A8BFE3; color: white; }}
            QPushButton[kind="segment"] {{ background: transparent; border: 1px solid transparent;
                border-radius: 6px; padding: 0 8px; min-height: 32px; }}
            QPushButton[kind="segment"]:checked {{ background: white; color: {c['ink']}; border-color: #E0E4EC; }}
            QPushButton[kind="segment"]:hover:!checked {{ color: {c['ink']}; background: #E8ECF2; }}
            QPushButton[kind="segment"]:focus {{ border-color: #80A9EB; }}
            QPushButton[kind="quiet"] {{ background: transparent; border: 1px solid transparent;
                padding: 0 7px; color: {c['muted']}; }}
            QPushButton[kind="quiet"]:hover {{ background: #E9EDF4; color: {c['ink']}; }}
            QPushButton[kind="quiet"]:focus {{ border-color: #80A9EB; }}
            QPushButton[kind="update"] {{ background: {c['river']}; border: 1px solid {c['river']};
                color: white; padding: 0 10px; border-radius: 6px; }}
            QPushButton[kind="update"]:hover {{ background: {c['river_hover']}; border-color: {c['river_hover']}; }}
            QPushButton[kind="update"]:focus {{ border-color: #98BAF1; }}
            QPushButton[kind="update"]:disabled {{ background: #A8BFE3; border-color: #A8BFE3; color: white; }}
            QPushButton[kind="approval"] {{ background: {c['river_wash']}; border-color: #D6E5FC; color: {c['river']}; padding: 0 10px; }}
            QPushButton[kind="approval"]:hover {{ background: #DCEAFE; border-color: #AFCBF3; }}
            QPushButton[kind="approval"]:focus {{ border-color: #80A9EB; }}
            QPushButton[kind="approval"]:disabled {{ background: #EFF3F8; color: #8997AA; border-color: #E8EBF0; }}
            QPushButton#stopSessionButton {{ color: {c['error']}; }}
            QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{ background: {c['inset']};
                color: {c['ink']}; border: 1px solid {c['edge']}; border-radius: 7px;
                padding: 8px 10px; selection-background-color: #CEE0FC; selection-color: {c['ink']}; }}
            QLineEdit, QSpinBox, QComboBox {{ min-height: 23px; }}
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 1px solid #78A4E9; background: #FBFCFF; }}
            QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled, QPlainTextEdit:disabled {{ color: #9299A5; background: #F2F3F6; }}
            QComboBox {{ padding-right: 24px; color: {c['secondary']}; }}
            QComboBox::drop-down {{ border: none; width: 23px; }}
            QComboBox QAbstractItemView {{ background: white; selection-background-color: {c['river_wash']};
                selection-color: {c['ink']}; border: 1px solid {c['edge']}; padding: 4px; outline: none; }}
            QWidget#hostPage, QWidget#guestPage, QWidget#sessionPage, QStackedWidget,
            QScrollArea, QWidget#peersContainer, QWidget#admissionsSection,
            QWidget#trustedComputersContainer, QWidget[role="page"] {{ background: transparent; border: none; }}
            QLabel#statusBadge {{ padding: 6px 11px; background: #E9EDF3; border-radius: 12px; color: {c['secondary']}; }}
            QLabel#statusBadge[state="active"] {{ background: {c['green_wash']}; color: {c['green']}; }}
            QLabel#statusBadge[state="busy"] {{ background: {c['river_wash']}; color: {c['river']}; }}
            QLabel#statusBadge[state="error"] {{ background: {c['error_wash']}; color: {c['error']}; }}
            QLabel#stepMarker {{ border: 1px solid #DDE3EC; border-radius: 11px; color: {c['secondary']}; }}
            QFrame#modpackNote {{ background: #ECF0F5; border: none; border-radius: 9px; }}
            QFrame#notice {{ background: {c['river_wash']}; border: 1px solid #D6E5FC; border-radius: 8px; }}
            QFrame#notice[error="true"] {{ background: {c['error_wash']}; border-color: #F0D8D6; }}
            QFrame#notice[error="true"] QLabel {{ color: {c['error']}; }}
            QFrame#updateStatus {{ background: #EDF1F7; border: none; border-radius: 8px; }}
            QLabel#updateMark {{ color: {c['river']}; background: #E0EAF8; border-radius: 11px; }}
            QFrame#updateStatus[state="error"] QLabel#updateMark {{ color: {c['error']}; background: #F5E4E2; }}
            QFrame#updateStatus[state="ready"] QLabel#updateMark {{ color: {c['green']}; background: #DEEEE5; }}
            QLabel#peerAvatar {{ background: #EEF2F8; border-radius: 8px; color: #617492; }}
            QLabel[connected="true"] {{ color: {c['green']}; }}
            QScrollBar:vertical {{ background: transparent; width: 6px; margin: 3px 0; }}
            QScrollBar::handle:vertical {{ background: #CED4DF; min-height: 20px; border-radius: 3px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
            QCheckBox {{ color: {c['muted']}; spacing: 5px; }}
            QCheckBox::indicator {{ width: 12px; height: 12px; border: 1px solid #BFC8D7;
                border-radius: 3px; background: transparent; }}
            QCheckBox::indicator:checked {{ background: {c['river']}; border-color: {c['river']}; }}
            QCheckBox:focus {{ color: {c['river']}; }}
            QToolTip {{ background: white; color: {c['ink']}; border: 1px solid {c['edge']}; padding: 6px; }}
        """)

    def _set_tab(self, role: str, *, animate: bool = True):
        if self._busy or self._session:
            return
        self._role = role
        self.host_tab.setChecked(role == "host")
        self.guest_tab.setChecked(role == "guest")
        self._switch_page(self.host_page if role == "host" else self.guest_page, animate)
        self._update_guide()
        self._render_trusted_controls()

    def _switch_page(self, page: QWidget, animate: bool = True):
        if self.pages.currentWidget() is page:
            return
        if self._animation:
            self._animation.stop()
        self.pages.setCurrentWidget(page)
        if not animate or self.motion_toggle.isChecked() or os.environ.get("PONTE_REDUCED_MOTION") == "1":
            self.pages.setGraphicsEffect(None)
            return
        effect = QGraphicsOpacityEffect(self.pages)
        self.pages.setGraphicsEffect(effect)
        self._animation = QPropertyAnimation(effect, b"opacity", self)
        self._animation.setDuration(160)
        self._animation.setStartValue(0.45)
        self._animation.setEndValue(1.0)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._animation.finished.connect(lambda: self.pages.setGraphicsEffect(None))
        self._animation.start()

    def _update_guide(self):
        if self._role == "host":
            steps = [
                ("Abra seu mundo", "Minecraft → Abrir para LAN."),
                ("Informe a porta", "Use o número mostrado no chat."),
                ("Envie o convite", "Mantenha o mundo e a Ponte abertos."),
            ]
            self.guide_title.setText("Do LAN para o seu grupo.")
        else:
            steps = [
                ("Receba o convite", "Quem abriu o mundo envia o código."),
                ("Conecte pela Ponte", "O endereço local aparece aqui."),
                ("Entre pelo Minecraft", "Multijogador → Conexão direta."),
            ]
            self.guide_title.setText("O caminho até o mundo.")
        for (title, detail), (title_widget, detail_widget) in zip(steps, self.guide_steps):
            title_widget.setText(title)
            detail_widget.setText(detail)

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.host_tab.setEnabled(not busy and not self._session)
        self.guest_tab.setEnabled(not busy and not self._session)
        for control in (self.world_name, self.port_input, self.world_combo, self.host_button,
                        self.guest_name, self.invite_input, self.join_button):
            control.setEnabled(not busy)
        self.scan_button.setEnabled(not busy)
        self.host_button.setText("Criando convite…" if busy and self._role == "host" else "Criar convite")
        self.join_button.setText("Conectando…" if busy and self._role == "guest" else "Entrar no mundo")
        self._render_update()

    def _set_badge(self, text: str, state: str = "idle"):
        self.status_badge.setText(text)
        self.status_badge.setProperty("state", state)
        self.status_badge.style().unpolish(self.status_badge)
        self.status_badge.style().polish(self.status_badge)

    def _begin_session(self):
        self._session = True
        self._set_busy(False)
        self.segment.hide()
        self.stop_button.setEnabled(True)
        host = self._role == "host"
        self.code_display.setVisible(host)
        self.address_display.setVisible(not host)
        self.connection_label.setVisible(True)
        self.copy_button.setVisible(True)
        self.connection_label.setText("Convite do seu mundo" if host else "Endereço no Minecraft")
        self.copy_button.setText("Copiar convite" if host else "Copiar endereço")
        self.copy_button.setEnabled(bool(self._code if host else self._address))
        self.connection_hint.setText("Envie o convite para quem vai jogar. Você decide quem entra."
                                     if host else "Minecraft → Multijogador → Conexão direta → cole o endereço.")
        self.session_heading.setText("Seu mundo está aberto." if host else "Preparando sua conexão…")
        self.peers_empty.setText("Aguardando quem vai jogar." if host else "Estabelecendo o caminho até o anfitrião…")
        self._render_room_controls()
        if not host:
            self._render_guest_address()
        self._switch_page(self.session_page)
        self._update_guide()

    def _reset_session(self):
        self._session = False
        self._code = ""
        self._address = ""
        self._renewing_invite = False
        self._peer_actions.clear()
        self._clear_admissions()
        self._peers.clear()
        for row in self._peer_rows.values():
            self.peers_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._peer_rows.clear()
        self.peers_empty.show()
        self.code_display.clear()
        self.address_display.clear()
        self.room_meta.clear()
        self.latency_label.clear()
        self.peers_heading.setText("Na sessão")
        self.footer_label.setText("P2P direto · só a conexão do jogo")
        self.segment.show()
        self._render_room_controls()
        self._set_busy(False)
        self._set_tab(self._role)

    def _clear_admissions(self):
        self._admissions.clear()
        for row in self._admission_rows.values():
            self.admissions_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._admission_rows.clear()
        self.admissions_section.hide()

    def _render_room_controls(self):
        host = self._role == "host" and self._session
        can_act = host and self.stop_button.isEnabled()
        self.renew_invite_button.setVisible(host)
        self.renew_hint.setVisible(host)
        self.renew_invite_button.setEnabled(can_act and not self._renewing_invite)
        self.renew_invite_button.setText("Renovando…" if self._renewing_invite else "Renovar convite")
        if host:
            self.copy_button.setEnabled(can_act and bool(self._code) and not self._renewing_invite)
        self.admissions_section.setVisible(host and bool(self._admissions))
        self.admissions_heading.setText(f"Pedidos de entrada · {len(self._admissions)}")
        for peer_id, row in self._admission_rows.items():
            action = self._peer_actions.get(peer_id, "")
            row.set_action_state(can_act and not action, action)
        for peer_id, row in self._peer_rows.items():
            row.set_manageable(host, peer_id in self._peer_actions or not can_act, self._peer_actions.get(peer_id, ""))
        self.removal_hint.setVisible(host and any(
            peer.get("state") in ("connecting", "connected") for peer in self._peers.values()))
        self._render_trusted_controls()

    def _render_guest_address(self):
        if self._role != "guest":
            return
        ready = bool(self._address)
        self.connection_label.setVisible(ready)
        self.address_display.setVisible(ready)
        self.copy_button.setVisible(ready)
        self.copy_button.setEnabled(ready)
        if ready:
            self.connection_hint.setText("Minecraft → Multijogador → Conexão direta → cole o endereço.")
        elif self._state == "waiting_approval":
            self.connection_hint.setText("O endereço do Minecraft aparece depois que o anfitrião aprovar sua entrada.")
        else:
            self.connection_hint.setText("O endereço do Minecraft aparece quando a conexão estiver pronta.")

    def _load_trusted_computers(self):
        try:
            self.controller.trusted_computers()
        except Exception as exc:
            self._trust_action_error(str(exc))

    def _render_trusted_controls(self):
        count = len(self._trusted_computers)
        self.trusted_button.setText(f"Computadores de confiança ({count})")
        self.trusted_button.setEnabled(self._role == "host")
        self.trusted_session_button.setText(f"Confiança · {count}")
        self.trusted_session_button.setAccessibleName(f"Gerenciar computadores de confiança, {count} salvos")
        self.trusted_session_button.setVisible(self._role == "host" and self._session)

    def _open_trusted_computers(self):
        if self._role != "host":
            return
        if self._trusted_dialog is None:
            dialog = QDialog(self)
            dialog.setObjectName("trustedComputersDialog")
            dialog.setWindowTitle("Computadores de confiança")
            dialog.setMinimumSize(440, 300)
            dialog.resize(510, 350)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(24, 20, 24, 20)
            layout.setSpacing(12)
            layout.addWidget(_label("Computadores de confiança", role="heading"))
            layout.addWidget(_label("Entram nas próximas salas com um convite válido, sem nova aprovação. Esquecer mantém a partida atual.", role="caption", wrap=True))
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.Shape.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.trusted_container = QWidget()
            self.trusted_container.setObjectName("trustedComputersContainer")
            self.trusted_layout = QVBoxLayout(self.trusted_container)
            self.trusted_layout.setContentsMargins(0, 0, 4, 0)
            self.trusted_layout.setSpacing(0)
            self.trusted_empty = _label("Nenhum computador salvo. Marque “Confiar neste computador” ao aprovar uma entrada.", role="caption", wrap=True)
            self.trusted_layout.addWidget(self.trusted_empty)
            self.trusted_layout.addStretch(1)
            scroll.setWidget(self.trusted_container)
            layout.addWidget(scroll, 1)
            self.trust_notice = _label("", role="caption", wrap=True, name="trustNotice")
            self.trust_notice.hide()
            layout.addWidget(self.trust_notice)
            close_button = _button("Fechar", "closeTrustedComputersButton")
            close_button.clicked.connect(dialog.close)
            layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
            self._trusted_dialog = dialog
        self._render_trusted_list()
        self.trust_notice.hide()
        self._trusted_dialog.open()

    def _render_trusted_list(self):
        if self._trusted_dialog is None:
            return
        for row in self._trusted_rows.values():
            self.trusted_layout.removeWidget(row)
            row.hide()
            row.deleteLater()
        self._trusted_rows.clear()
        for fingerprint, computer in self._trusted_computers.items():
            row = TrustedComputerRow(computer, self._forget_trusted)
            row.set_pending(fingerprint in self._forgetting)
            self._trusted_rows[fingerprint] = row
            self.trusted_layout.insertWidget(self.trusted_layout.count() - 1, row)
        self.trusted_empty.setVisible(not self._trusted_computers)

    def _set_peer_trusted(self, peer_id: str, trusted: bool):
        peer = self._peers.get(peer_id, {})
        if (self._role != "host" or not self._session or not self.stop_button.isEnabled()
                or peer_id in self._peer_actions or peer.get("state") != "connected"
                or not peer.get("can_trust") or not peer.get("fingerprint")):
            return
        self._peer_actions[peer_id] = "trust" if trusted else "untrust"
        self._render_room_controls()
        try:
            self.controller.set_trusted(peer_id, bool(trusted))
        except Exception as exc:
            self._trust_action_error(str(exc), peer_id=peer_id)

    def _forget_trusted(self, fingerprint: str):
        if (self._role != "host" or fingerprint not in self._trusted_computers
                or fingerprint in self._forgetting):
            return
        self._forgetting.add(fingerprint)
        if fingerprint in self._trusted_rows:
            self._trusted_rows[fingerprint].set_pending(True)
        if self._trusted_dialog is not None:
            self.trust_notice.hide()
        try:
            self.controller.forget_trusted(fingerprint)
        except Exception as exc:
            self._trust_action_error(str(exc), fingerprint=fingerprint)

    def _trust_action_error(self, message: str, *, peer_id: str = "", fingerprint: str = ""):
        if peer_id:
            if self._peer_actions.get(peer_id) in ("trust", "untrust"):
                self._peer_actions.pop(peer_id, None)
        if fingerprint:
            self._forgetting.discard(fingerprint)
            if fingerprint in self._trusted_rows:
                self._trusted_rows[fingerprint].set_pending(False)
        self._render_room_controls()
        if self._trusted_dialog is not None and self._trusted_dialog.isVisible():
            self.trust_notice.setText(message)
            self.trust_notice.show()
        else:
            self._show_notice(message, error=True)

    def _handle_trusted(self, event: dict):
        computers = event.get("computers") or []
        self._trusted_computers = {
            str(computer["fingerprint"]): dict(computer)
            for computer in computers if isinstance(computer, dict) and computer.get("fingerprint")
        }
        self._forgetting.intersection_update(self._trusted_computers)
        for peer_id, peer in self._peers.items():
            fingerprint = str(peer.get("fingerprint") or "")
            if not fingerprint or not peer.get("can_trust"):
                continue
            peer["trusted"] = fingerprint in self._trusted_computers
            self._peer_rows[peer_id].update_peer(peer)
            action = self._peer_actions.get(peer_id)
            if (action == "trust" and peer["trusted"]) or (action == "untrust" and not peer["trusted"]):
                self._peer_actions.pop(peer_id, None)
        self._render_room_controls()
        self._render_trusted_list()

    def _admission_action(self, peer_id: str, action: str, remember: bool = False):
        if (action not in ("approve", "reject") or self._role != "host"
                or not self._session or not self.stop_button.isEnabled()
                or peer_id not in self._admissions or peer_id in self._peer_actions):
            return
        self._peer_actions[peer_id] = action
        self._render_room_controls()
        try:
            if action == "approve":
                admission = self._admissions[peer_id]
                remember = bool(remember and admission.get("can_trust") and admission.get("fingerprint"))
                self.controller.approve(peer_id, remember)
            else:
                self.controller.reject(peer_id)
        except Exception as exc:
            self._room_action_error(str(exc), peer_id)

    def _remove_peer(self, peer_id: str):
        if (self._role != "host" or not self._session or not self.stop_button.isEnabled()
                or peer_id in self._peer_actions
                or self._peers.get(peer_id, {}).get("state") not in ("connecting", "connected")):
            return
        self._peer_actions[peer_id] = "remove"
        self._render_room_controls()
        try:
            self.controller.remove_peer(peer_id)
        except Exception as exc:
            self._room_action_error(str(exc), peer_id)

    def _renew_invite(self):
        if (self._role != "host" or not self._session or not self.stop_button.isEnabled()
                or self._renewing_invite):
            return
        self._renewing_invite = True
        self._render_room_controls()
        try:
            self.controller.renew_invite()
        except Exception as exc:
            self._renewing_invite = False
            self._room_action_error(str(exc), reset_peers=False)

    def _room_action_error(self, message: str, peer_id: str = "", *, reset_peers: bool = True):
        if reset_peers:
            if peer_id:
                self._peer_actions.pop(peer_id, None)
            else:
                self._peer_actions.clear()
        self._render_room_controls()
        self._show_notice(message or "A operação não foi concluída. Tente novamente.", error=True)

    def _host(self):
        if self._busy or self._session:
            return
        self._hide_notice()
        self._role = "host"
        self._set_busy(True)
        self._set_badge("Criando convite", "busy")
        try:
            self.controller.host(int(self.port_input.value()), self.world_name.text().strip() or "Meu mundo")
        except Exception as exc:
            self._error(str(exc))

    def _join(self):
        if self._busy or self._session:
            return
        code = self.invite_input.toPlainText().strip()
        if not code:
            self._show_notice("Cole o convite recebido para entrar no mundo.", error=True)
            self.invite_input.setFocus()
            return
        self._hide_notice()
        self._role = "guest"
        name = self.guest_name.text().strip() or "Amigo"
        self.settings.setValue("guest_name", name)
        self._set_busy(True)
        self._set_badge("Conectando", "busy")
        try:
            self.controller.join(code, 0, name)
        except Exception as exc:
            self._error(str(exc))

    def _stop(self):
        if not self.stop_button.isEnabled():
            return
        self.stop_button.setEnabled(False)
        self._clear_admissions()
        self._render_room_controls()
        self._set_badge("Encerrando", "busy")
        try:
            self.controller.stop()
        except Exception as exc:
            self._error(str(exc))

    def _scan_worlds(self):
        if self._busy or self._session or not self.scan_button.isEnabled():
            return
        self.scan_button.setEnabled(False)
        self.scan_button.setText("Buscando…")
        self.scan_hint.setText("Procurando anúncios de mundos LAN na sua rede…")
        try:
            self.controller.scan()
        except Exception as exc:
            self.scan_button.setEnabled(True)
            self.scan_button.setText("Buscar")
            self.scan_hint.setText("A busca não terminou. Você pode informar a porta manualmente.")
            self._show_notice(str(exc), error=True)

    def _select_world(self, index: int):
        world = self.world_combo.itemData(index)
        if not isinstance(world, dict):
            return
        port = world.get("port")
        if isinstance(port, int) and 1 <= port <= 65535:
            self.port_input.setValue(port)
        name = str(world.get("name") or "").strip()
        if name:
            self.world_name.setText(name[:80])

    def _copy_connection(self):
        value = self._code if self._role == "host" else self._address
        if not value:
            return
        QApplication.clipboard().setText(value)
        self.copy_button.setText("Convite copiado" if self._role == "host" else "Endereço copiado")
        QTimer.singleShot(1800, lambda: self.copy_button.setText(
            "Copiar convite" if self._role == "host" else "Copiar endereço"))

    def _diagnostics(self):
        if not self.diagnostics_button.isEnabled():
            return
        self.diagnostics_button.setEnabled(False)
        self.diagnostics_button.setText("Salvando…")
        try:
            self.controller.diagnostics()
        except Exception as exc:
            self.diagnostics_button.setEnabled(True)
            self.diagnostics_button.setText("Diagnóstico")
            self._show_notice(str(exc), error=True)

    def _check_updates(self):
        if not self.updates_button.isEnabled():
            return
        self._handle_update({"state": "checking"})
        try:
            self.controller.check_updates()
        except Exception as exc:
            self._handle_update({"state": "error", "message": str(exc)})

    def _install_update(self):
        if (self._update_state != "ready" or self._session or self._busy
                or self._update_install_requested):
            return
        self._update_install_requested = True
        self._render_update()
        try:
            self.controller.install_update()
        except Exception as exc:
            self._handle_update({"state": "error", "message": str(exc)})

    def _hide_update_feedback(self):
        if self._update_state == "current":
            self._update_feedback_visible = False
            self.update_status.hide()

    def _handle_update(self, event: dict):
        state = str(event.get("state") or "")
        if state not in {"checking", "current", "available", "downloading", "ready", "error", "deferred", "installing"}:
            return
        self._update_timer.stop()
        self._update_state = state
        self._update_version = str(event.get("version") or self._update_version)
        self._update_message = str(event.get("message") or "")
        progress = event.get("progress")
        self._update_progress = max(0, min(100, int(progress))) if isinstance(progress, (int, float)) else None
        self._update_install_requested = state == "installing"
        self._update_feedback_visible = True
        self._render_update()
        if state == "current":
            self._update_timer.start(7000)
        elif state == "installing":
            # The controller sends this only after its independent helper starts.
            # Give this final status a frame before releasing the installed EXE.
            QTimer.singleShot(150, self._quit_for_update)

    def _quit_for_update(self):
        if self._update_state == "installing":
            app = QApplication.instance()
            if app is not None:
                app.quit()

    def _render_update(self):
        if not hasattr(self, "update_status"):
            return
        state = self._update_state
        version = f"Ponte {self._update_version}" if self._update_version else "Atualização da Ponte"
        message = self._update_message
        texts = {
            "checking": "Buscando atualizações…",
            "current": "A Ponte está atualizada.",
            "available": f"{version} disponível. Preparando o download…",
            "downloading": f"Baixando {version}…",
            "ready": f"{version} pronta para instalar.",
            "error": "Não foi possível atualizar. Tente novamente em Atualizações.",
            "deferred": "A atualização será preparada ao encerrar a sessão.",
            "installing": "Reiniciando a Ponte para concluir a atualização…",
        }
        text = message or texts.get(state, "")
        if state == "ready":
            if self._session:
                text = "Pronta para instalar ao encerrar a sessão."
            elif self._busy:
                text = "Pronta para instalar quando a conexão terminar de preparar."
            elif self._update_install_requested:
                text = "Preparando a atualização…"
        elif state == "downloading" and self._update_progress is not None:
            text = f"{text.rstrip(' .…')} · {self._update_progress}%"
        self.update_text.setText(text if len(text) <= 220 else text[:217].rstrip() + "…")
        self.update_text.setToolTip(message or text)
        self.update_mark.setText("✓" if state in ("current", "ready") else "!" if state == "error" else "↑")
        self.update_status.setProperty("state", state)
        self.update_status.style().unpolish(self.update_status)
        self.update_status.style().polish(self.update_status)
        can_install = state == "ready" and not self._session and not self._busy
        self.install_update_button.setVisible(can_install)
        self.install_update_button.setEnabled(can_install and not self._update_install_requested)
        self.updates_button.setEnabled(state not in ("checking", "downloading", "installing")
                                       and not self._update_install_requested)
        self.updates_button.setText("Buscando…" if state == "checking" else "Atualizações")
        self.update_status.setVisible(bool(state) and self._update_feedback_visible)

    def _open_diagnostics_folder(self):
        path = getattr(self, "_diagnostics_path", "")
        if path:
            target = Path(path)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(target if target.is_dir() else target.parent)))

    def _hide_notice(self):
        self._notice_timer.stop()
        self.notice.hide()
        self.open_diagnostics.hide()

    def _show_notice(self, text: str, *, error: bool = False, transient: bool = False):
        self._notice_timer.stop()
        self.open_diagnostics.hide()
        self.notice_text.setText(text)
        self.notice_text.setToolTip(text)
        self.notice.setProperty("error", error)
        self.notice.style().unpolish(self.notice)
        self.notice.style().polish(self.notice)
        self.notice.show()
        if transient:
            self._notice_timer.start(7000)

    def _error(self, message: str):
        self._state = "error"
        self._reset_session()
        self._set_badge("Não foi possível conectar", "error")
        self.diagnostics_button.setEnabled(True)
        self.diagnostics_button.setText("Diagnóstico")
        self._show_notice(message or "A conexão não foi concluída. Tente novamente.", error=True)

    @Slot(dict)
    def handle_event(self, event: dict):
        """Render only data supplied by the actual controller."""
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "trusted":
            self._handle_trusted(event)
        elif kind == "update":
            self._handle_update(event)
        elif kind == "status":
            state = str(event.get("state", "idle"))
            self._state = state
            if event.get("role") in ("host", "guest"):
                self._role = event["role"]
            if state in ("idle", "stopped"):
                self._reset_session()
                self._set_badge("Sessão encerrada" if state == "stopped" else "Pronto para jogar")
            elif state == "error":
                self._error(str(event.get("message") or "Não foi possível estabelecer a conexão."))
            elif state == "hosting":
                if not self._session:
                    self._begin_session()
                self._set_badge("Mundo compartilhado", "active")
            elif state == "connecting":
                self._set_badge("Conectando", "busy")
                if self._role == "guest" and not self._session:
                    self._begin_session()
                elif not self._session:
                    self._set_busy(True)
            elif state == "waiting_approval":
                if not self._session:
                    self._begin_session()
                self._set_badge("Aguardando anfitrião", "busy")
                self.session_heading.setText("Aguardando aprovação do anfitrião.")
                self.peers_empty.setText("Seu pedido foi enviado. Aguarde a aprovação para entrar.")
                self._render_guest_address()
            elif state == "connected":
                if not self._session:
                    self._begin_session()
                self._set_badge("Conexão estabelecida", "active")
                if self._role == "guest":
                    self.session_heading.setText("Tudo pronto para entrar.")
            message = str(event.get("message") or "")
            self.status_badge.setToolTip(message)
        elif kind == "room":
            self._code = str(event.get("code") or "")
            name = str(event.get("name") or "Meu mundo")
            port = event.get("port")
            self.room_meta.setText(f"{name}  ·  porta LAN {port}" if port else name)
            self.code_display.setPlainText(self._code)
            if not self._session:
                self._begin_session()
            if event.get("renewed"):
                self._renewing_invite = False
                self._show_notice("Convite renovado. O anterior deixou de valer; quem já entrou continua.", transient=True)
            self._render_room_controls()
            if self._role == "guest":
                self._render_guest_address()
        elif kind == "room_action":
            self._renewing_invite = event.get("state") == "renewing"
            self._render_room_controls()
        elif kind == "listening":
            self._address = str(event.get("address") or "")
            self.address_display.setText(self._address)
            if self._role == "guest":
                if not self._session:
                    self._begin_session()
                self._render_guest_address()
        elif kind == "admission":
            peer_id = str(event.get("peer_id") or "")
            if not peer_id or self._role != "host" or not self._session or not self.stop_button.isEnabled():
                return
            if event.get("state") == "pending":
                self._admissions.setdefault(peer_id, {}).update(event)
                if peer_id not in self._admission_rows:
                    row = AdmissionRow(self._admissions[peer_id], self._admission_action)
                    self._admission_rows[peer_id] = row
                    self.admissions_layout.addWidget(row)
                else:
                    self._admission_rows[peer_id].update_admission(self._admissions[peer_id])
            elif event.get("state") in ("approved", "rejected", "expired", "cancelled"):
                self._admissions.pop(peer_id, None)
                self._peer_actions.pop(peer_id, None)
                row = self._admission_rows.pop(peer_id, None)
                if row is not None:
                    self.admissions_layout.removeWidget(row)
                    row.hide()
                    row.deleteLater()
            self._render_room_controls()
        elif kind == "peer":
            peer_id = str(event.get("peer_id") or "unknown")
            self._peers.setdefault(peer_id, {}).update(event)
            peer = self._peers[peer_id]
            if peer_id not in self._peer_rows:
                row = PeerRow(peer, on_remove=self._remove_peer, on_trust=self._set_peer_trusted)
                row.setObjectName(f"peer_{peer_id}")
                self._peer_rows[peer_id] = row
                self.peers_layout.insertWidget(self.peers_layout.count() - 1, row)
            else:
                self._peer_rows[peer_id].update_peer(peer)
            if peer.get("state") in ("disconnected", "failed"):
                self._peer_actions.pop(peer_id, None)
            elif "trusted" in event:
                action = self._peer_actions.get(peer_id)
                if (action == "trust" and peer.get("trusted")) or (action == "untrust" and not peer.get("trusted")):
                    self._peer_actions.pop(peer_id, None)
            self.peers_empty.hide()
            active = sum(p.get("state") == "connected" for p in self._peers.values())
            self.peers_heading.setText(f"Na sessão · {active}" if active else "Na sessão")
            self._render_room_controls()
        elif kind == "metrics":
            sent = int(event.get("sent") or 0)
            received = int(event.get("received") or 0)
            self.footer_label.setText(f"Enviado {_format_bytes(sent)}  ·  recebido {_format_bytes(received)}")
            latency = event.get("rtt_ms")
            self.latency_label.setText(format_rtt(latency) if isinstance(latency, (int, float)) else "")
        elif kind == "worlds":
            self.world_combo.blockSignals(True)
            self.world_combo.clear()
            worlds = event.get("worlds") or []
            if worlds:
                self.world_combo.addItem("Escolha um mundo encontrado", None)
                for world in worlds:
                    if isinstance(world, dict):
                        name, port = world.get("name") or "Mundo LAN", world.get("port")
                        self.world_combo.addItem(f"{name} · {port}", world)
                self.scan_hint.setText("Selecione o mundo para preencher o nome e a porta.")
            else:
                self.world_combo.addItem("Nenhum mundo encontrado", None)
                self.scan_hint.setText("Abra para LAN ou informe a porta mostrada no chat do jogo.")
            self.world_combo.blockSignals(False)
            self.scan_button.setEnabled(not self._busy)
            self.scan_button.setText("Buscar")
        elif kind == "error":
            message = str(event.get("message") or "A operação não foi concluída.")
            operation = event.get("operation")
            if operation == "diagnostics":
                self.diagnostics_button.setEnabled(True)
                self.diagnostics_button.setText("Diagnóstico")
                self._show_notice(message, error=True)
            elif operation == "update":
                self._handle_update({"state": "error", "message": message})
            elif operation == "scan":
                self.scan_button.setEnabled(not self._busy)
                self.scan_button.setText("Buscar")
                self.scan_hint.setText("Você pode informar a porta mostrada no chat do jogo.")
                self._show_notice(message, error=True)
            elif operation == "trust":
                self._trust_action_error(message, peer_id=str(event.get("peer_id") or ""),
                                         fingerprint=str(event.get("fingerprint") or ""))
            elif operation in ("peer", "room"):
                peer_id = str(event.get("peer_id") or "")
                if operation == "room" and not peer_id:
                    self._renewing_invite = False
                self._room_action_error(message, peer_id, reset_peers=operation == "peer" or bool(peer_id))
            else:
                self._error(message)
        elif kind == "diagnostics":
            self._diagnostics_path = str(event.get("path") or "")
            self.diagnostics_button.setEnabled(True)
            self.diagnostics_button.setText("Diagnóstico")
            self._show_notice("Diagnóstico salvo. O relatório reúne informações desta conexão.")
            self.open_diagnostics.setVisible(bool(self._diagnostics_path))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "guide"):
            self.guide.setFixedWidth(219 if self.width() < 900 else 249)
            compact = self.height() < 650
            self.subheading.setVisible(not compact)
            self.bridge_illustration.setVisible(not compact)
            self.diagram_labels.setVisible(not compact)
            self.modpack_detail.setVisible(not compact)

    def closeEvent(self, event):
        if self._trusted_dialog is not None:
            self._trusted_dialog.close()
        try:
            self.controller.stop()
        except Exception:
            pass
        event.accept()
