import logging
import json
import os
import sys
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from .controller import Controller
from .ui import MainWindow
from . import __version__


def main():
    if sys.platform == "win32":
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Ponte.Minecraft.0.1")
    folder = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "Ponte"
    folder.mkdir(parents=True, exist_ok=True)
    from logging.handlers import RotatingFileHandler
    handler = RotatingFileHandler(folder / "Ponte.log", maxBytes=1000000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = QApplication(sys.argv)
    app.setApplicationName("Ponte")
    app.setOrganizationName("Ponte")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI Variable", 10))
    assets = Path(getattr(sys, "_MEIPASS", str(Path(__file__).resolve().parent.parent))) / "assets"
    if (assets / "ponte.ico").exists():
        app.setWindowIcon(QIcon(str(assets / "ponte.ico")))
    elif (assets / "ponte.svg").exists():
        app.setWindowIcon(QIcon(str(assets / "ponte.svg")))
    controller = Controller()
    window = MainWindow(controller)
    app.aboutToQuit.connect(controller.shutdown)
    window.show()
    if "--update-ack" in sys.argv:
        from .update_helper import confirm_ready
        job_path = sys.argv[sys.argv.index("--update-ack") + 1]
        def acknowledge_update():
            try:
                confirm_ready(job_path)
            except Exception:
                logging.getLogger(__name__).exception("Não foi possível confirmar a atualização")
                app.quit()
        QTimer.singleShot(200, acknowledge_update)
    if "--update-error" in sys.argv:
        QTimer.singleShot(400, lambda: controller.event.emit({"type": "update", "state": "error", "message":
            "A atualização não iniciou corretamente. A versão anterior foi restaurada."}))
    if "--qa-check" not in sys.argv:
        QTimer.singleShot(8000, controller.check_updates)
        update_timer = QTimer(app)
        update_timer.setInterval(6 * 60 * 60 * 1000)
        update_timer.timeout.connect(controller.check_updates)
        update_timer.start()
    if "--qa-update" in sys.argv:
        update_report = Path(sys.argv[sys.argv.index("--qa-update") + 1])
        install_requested = False
        def check_update_flow(event):
            nonlocal install_requested
            if event.get("type") != "update":
                return
            state = event.get("state")
            if state == "ready" and not install_requested:
                install_requested = True
                controller.install_update()
            elif state == "installing":
                update_report.write_text(json.dumps({"stage": "helper_started", "from_version": __version__,
                    "job": str(controller.updater.job_path), "old_target": sys.executable}), encoding="utf-8")
            elif state in ("error", "current"):
                update_report.write_text(json.dumps({"stage": state, "message": event.get("message", "")}), encoding="utf-8")
                app.quit()
        controller.event.connect(check_update_flow)
        QTimer.singleShot(800, controller.check_updates)
    # An explicit developer QA entry point exercises this exact frozen binary.
    # It captures only our own window; the normal launch has no QA behavior.
    if "--qa-check" in sys.argv:
        qa_path = Path(sys.argv[sys.argv.index("--qa-check") + 1])
        def check_ui():
            try:
                window.guest_tab.click()
                guest_ok = window._role == "guest"
                window.host_tab.click()
                result = {"passed": guest_ok and window._role == "host" and window.isVisible(),
                          "frozen": bool(getattr(sys, "frozen", False)),
                          "size": [window.width(), window.height()],
                          "guest_tab": guest_ok, "host_tab": window._role == "host",
                          "controller_thread": controller.thread.is_alive(),
                          "scope": "actual application window; no simulated connection data"}
                qa_path.parent.mkdir(parents=True, exist_ok=True)
                window.pages.setGraphicsEffect(None)
                window.grab().save(str(qa_path.with_suffix(".png")))
                qa_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
            finally:
                app.quit()
        QTimer.singleShot(1200, check_ui)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
