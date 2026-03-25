import sys
from PyQt6.QtWidgets import QApplication
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QIcon

from main_window import MainWindow


def main():
    # Enable high-DPI scaling
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)

    app = QApplication(sys.argv)
    app.setApplicationName("NaiDunia PDF Cropper")
    app.setApplicationVersion("1.0.0")
    app.setOrganizationName("PDF Tools")

    win = MainWindow()
    win.show()

    # If a PDF path was passed as a CLI argument, open it immediately
    if len(sys.argv) > 1:
        pdf_path = sys.argv[1]
        win._open_file_directly(pdf_path)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
