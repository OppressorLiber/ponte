import os
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
from pathlib import Path
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PIL import Image

app = QGuiApplication([])
assets = Path(__file__).parent / 'assets'
image = QImage(256, 256, QImage.Format.Format_ARGB32)
image.fill(0)
painter = QPainter(image)
QSvgRenderer(str(assets / 'ponte.svg')).render(painter)
painter.end()
image.save(str(assets / 'ponte.png'))
Image.open(assets / 'ponte.png').save(assets / 'ponte.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
print('Icon ready')
