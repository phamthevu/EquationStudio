from __future__ import annotations
import io
import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

# Worker mode must run before importing any GUI/WebEngine code.
if len(sys.argv) > 1 and sys.argv[1] in ('--ocr', '--prepare'):
    from ocr_worker import main
    sys.exit(main(sys.argv[2] if sys.argv[1] == '--ocr' else None,
                  prepare=sys.argv[1] == '--prepare'))

from PyQt6.QtCore import Qt, QRectF, QProcess, QTimer, QMimeData, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QImage, QKeySequence, QPen, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QPushButton,
    QLabel, QComboBox, QCheckBox, QListWidget, QListWidgetItem, QPlainTextEdit,
    QSplitter, QFileDialog, QMessageBox, QGraphicsView, QGraphicsScene,
    QGraphicsRectItem, QAbstractItemView, QProgressBar, QInputDialog)
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEngineUrlRequestInterceptor
from core import image_png, pil_png, clean_latex, to_mathml, export_docx, save_session, load_session


class OfflineRequests(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        if info.requestUrl().scheme() not in ('data', 'about', 'qrc'):
            info.block(True)


class ImageView(QGraphicsView):
    cropped = pyqtSignal(list)

    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setMinimumWidth(320)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor('#e9edf4'))
        self.selection_mode = False
        self.start = None
        self.rubber = None
        self.bounds = QRectF()
        self.highlights = []

    def show_png(self, data):
        self.scene().clear()
        self.highlights = []
        self.rubber = None
        self.start = None
        pix = QPixmap()
        if not pix.loadFromData(data, 'PNG'):
            raise ValueError('Không đọc được ảnh.')
        self.bounds = QRectF(0, 0, pix.width(), pix.height())
        self.scene().addPixmap(pix)
        self.scene().setSceneRect(self.bounds)
        self.fit_image()

    def fit_image(self):
        if not self.bounds.isEmpty():
            self.fitInView(self.bounds, Qt.AspectRatioMode.KeepAspectRatio)

    def set_boxes(self, formulas, selected_id=None):
        for rect in self.highlights:
            self.scene().removeItem(rect)
        self.highlights = []
        for f in formulas:
            x1, y1, x2, y2 = f['box']
            rect = QGraphicsRectItem(QRectF(x1, y1, x2-x1, y2-y1))
            selected = f['id'] == selected_id
            rect.setPen(QPen(QColor('#ff8a00' if selected else '#2473d9'), 3 if selected else 2))
            rect.setBrush(QColor(255, 138, 0, 28) if selected else QColor(36, 115, 217, 12))
            self.scene().addItem(rect)
            self.highlights.append(rect)

    def crop_mode(self, active):
        self.selection_mode = active
        self.setDragMode(QGraphicsView.DragMode.NoDrag if active else QGraphicsView.DragMode.ScrollHandDrag)
        self.setCursor(Qt.CursorShape.CrossCursor if active else Qt.CursorShape.ArrowCursor)

    def wheelEvent(self, event):
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            factor = 1.2 if event.angleDelta().y() > 0 else 1/1.2
            current = self.transform().m11()
            if 0.02 < current * factor < 12:
                self.scale(factor, factor)
            event.accept()
        else:
            super().wheelEvent(event)

    def mousePressEvent(self, event):
        if self.selection_mode and event.button() == Qt.MouseButton.LeftButton and not self.bounds.isEmpty():
            point = self.mapToScene(event.pos())
            if self.bounds.contains(point):
                self.start = point
                self.rubber = self.scene().addRect(QRectF(point, point), QPen(QColor('#df5830'), 2))
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.start is not None:
            rect = QRectF(self.start, self.mapToScene(event.pos())).normalized().intersected(self.bounds)
            self.rubber.setRect(rect)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.start is not None:
            rect = self.rubber.rect()
            self.scene().removeItem(self.rubber)
            self.rubber = None
            self.start = None
            if rect.width() >= 5 and rect.height() >= 5:
                self.cropped.emit([int(rect.left()), int(rect.top()),
                                   int(rect.right()), int(rect.bottom())])
            event.accept()
            return
        super().mouseReleaseEvent(event)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle('Equation Studio • Offline')
        self.resize(1450, 880)
        self.images, self.formulas = [], []
        self.selected_id = None
        self.dirty = False
        self.project_path = None
        self.process = None
        self.temp = None
        self.buffer = b''
        self.errors = []
        self.started = 0
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.preview)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self.elapsed)
        self.build_ui()

    def button(self, label, callback, layout):
        b = QPushButton(label)
        b.clicked.connect(callback)
        layout.addWidget(b)
        return b

    def build_ui(self):
        root = QWidget()
        vertical = QVBoxLayout(root)
        toolbar = QHBoxLayout()
        self.import_button = self.button('Import ảnh', self.import_images, toolbar)
        self.paste_button = self.button('Dán screenshot', self.paste, toolbar)
        self.scan_button = self.button('Trích công thức', lambda: self.scan(), toolbar)
        self.crop_button = self.button('Khoanh vùng', self.toggle_crop, toolbar)
        self.crop_button.setCheckable(True)
        self.cancel_button = self.button('Hủy OCR', self.cancel, toolbar)
        self.cancel_button.setEnabled(False)
        toolbar.addStretch()
        self.button('Lưu project', self.save, toolbar)
        self.open_button = self.button('Mở project', self.open_project, toolbar)
        self.button('Xuất Word (đã tick)', self.export, toolbar)
        vertical.addLayout(toolbar)
        options = QHBoxLayout()
        self.image_combo = QComboBox()
        self.image_combo.setMinimumWidth(200)
        self.image_combo.currentIndexChanged.connect(self.image_changed)
        options.addWidget(QLabel('Ảnh:'))
        options.addWidget(self.image_combo, 1)
        self.all_images = QCheckBox('Quét mọi ảnh')
        self.two_pages = QCheckBox('Hai trang cạnh nhau')
        self.isolated = QCheckBox('Chỉ công thức đứng riêng')
        self.dedup = QCheckBox('Ẩn công thức trùng')
        self.dedup.toggled.connect(self.refresh_list)
        for control in (self.all_images, self.two_pages, self.isolated, self.dedup):
            options.addWidget(control)
        vertical.addLayout(options)
        splitter = QSplitter()
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.addWidget(QLabel('Ảnh gốc • Ctrl + cuộn để zoom • kéo ảnh để di chuyển'))
        self.view = ImageView()
        self.view.cropped.connect(self.crop_scan)
        left_layout.addWidget(self.view)
        self.button('Vừa khung', self.view.fit_image, left_layout)
        splitter.addWidget(left)
        middle = QWidget()
        mid = QVBoxLayout(middle)
        self.count = QLabel('0 công thức')
        mid.addWidget(self.count)
        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list.currentItemChanged.connect(self.select_formula)
        self.list.itemChanged.connect(self.check_changed)
        mid.addWidget(self.list)
        row = QHBoxLayout()
        self.button('Tick tất cả', lambda: self.check_all(True), row)
        self.button('Bỏ tick', lambda: self.check_all(False), row)
        mid.addLayout(row)
        self.button('Thêm công thức bằng LaTeX', self.manual_latex, mid)
        self.button('Xóa công thức đang chọn', self.delete_formula, mid)
        splitter.addWidget(middle)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        self.details = QLabel('Chọn một công thức để xem/sửa.')
        self.details.setWordWrap(True)
        right_layout.addWidget(self.details)
        self.crop_preview = QLabel()
        self.crop_preview.setMinimumHeight(80)
        self.crop_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        right_layout.addWidget(self.crop_preview)
        right_layout.addWidget(QLabel('LaTeX • sửa trực tiếp, kết quả được lưu tự động vào project'))
        self.editor = QPlainTextEdit()
        self.editor.setPlaceholderText(r'Ví dụ: \mu=\frac{k_1}{\sqrt{1+k_1^2+k_2^2}}')
        self.editor.textChanged.connect(self.edited)
        right_layout.addWidget(self.editor, 1)
        self.preview_message = QLabel('Xem trước công thức')
        self.preview_message.setWordWrap(True)
        right_layout.addWidget(self.preview_message)
        self.web = QWebEngineView()
        self.interceptor = OfflineRequests(self.web)
        self.web.page().profile().setUrlRequestInterceptor(self.interceptor)
        self.web.setMinimumHeight(180)
        right_layout.addWidget(self.web, 1)
        self.button('Copy Equation → Word', self.copy_equation, right_layout)
        self.button('Copy LaTeX', self.copy_latex, right_layout)
        splitter.addWidget(right)
        splitter.setSizes([620, 310, 450])
        vertical.addWidget(splitter, 1)
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        vertical.addWidget(self.progress)
        self.status = QLabel('Import ảnh hoặc chụp Win + Shift + S rồi dán vào app. Chuẩn bị mô hình trước lần OCR đầu.')
        self.status.setWordWrap(True)
        vertical.addWidget(self.status)
        self.setCentralWidget(root)
        for text, shortcut, callback in [('Import', 'Ctrl+O', self.import_images),
                                         ('Paste', 'Ctrl+V', self.paste),
                                         ('Save', 'Ctrl+S', self.save)]:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)
        tools = self.menuBar().addMenu('Công cụ')
        prepare = tools.addAction('Tải/chuẩn bị mô hình (cần Internet)')
        prepare.triggered.connect(self.prepare)
        tools.addAction('Project mới').triggered.connect(self.new_project)
        tools.addAction('Hướng dẫn').triggered.connect(self.help)
        self.setStyleSheet('QMainWindow {background:#f5f7fb;} QPushButton {padding:7px 10px;} '
                           'QPlainTextEdit,QListWidget {background:white;border:1px solid #d8deeb;}')

    def notify_error(self, error):
        QMessageBox.warning(self, 'Equation Studio', str(error))

    def mark_dirty(self):
        self.dirty = True
        self.setWindowTitle('Equation Studio • Offline • Chưa lưu')

    def current_image(self):
        i = self.image_combo.currentIndex()
        return self.images[i] if 0 <= i < len(self.images) else None

    def current_formula(self):
        return next((x for x in self.formulas if x['id'] == self.selected_id), None)

    def add_image(self, data, name):
        image = {'id': uuid.uuid4().hex, 'name': name, 'png': data}
        self.images.append(image)
        self.image_combo.addItem(name, image['id'])
        self.image_combo.setCurrentIndex(len(self.images)-1)
        self.mark_dirty()

    def import_images(self):
        files, _ = QFileDialog.getOpenFileNames(self, 'Import ảnh', '', 'Ảnh (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp)')
        for file in files:
            try:
                self.add_image(image_png(file), Path(file).name)
            except Exception as exc:
                self.notify_error(f'{Path(file).name}: {exc}')

    def paste(self):
        clipboard = QApplication.clipboard()
        if clipboard.mimeData().hasImage():
            image = clipboard.image()
            if image.isNull():
                self.notify_error('Clipboard không có ảnh hợp lệ.')
                return
            from PyQt6.QtCore import QBuffer, QIODevice
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            image.save(buffer, 'PNG')
            self.add_image(bytes(buffer.data()), f'Screenshot {len(self.images)+1}')
        elif self.editor.hasFocus():
            self.editor.paste()
        else:
            self.notify_error('Hãy chụp Win + Shift + S hoặc copy một ảnh trước.')

    def image_changed(self, *args):
        image = self.current_image()
        if image:
            self.view.show_png(image['png'])
            self.highlight()

    def highlight(self):
        image = self.current_image()
        if image:
            self.view.set_boxes([x for x in self.formulas if x['image_id'] == image['id']], self.selected_id)

    def toggle_crop(self):
        self.view.crop_mode(self.crop_button.isChecked())
        if self.crop_button.isChecked():
            self.status.setText('Kéo một hình chữ nhật quanh công thức; app sẽ nhận dạng vùng đó.')

    def crop_scan(self, box):
        self.crop_button.setChecked(False)
        self.view.crop_mode(False)
        self.scan(box)

    def scan(self, box=None):
        if self.process:
            return
        if not self.images:
            self.notify_error('Import/dán ảnh trước.')
            return
        images = self.images if self.all_images.isChecked() and box is None else [self.current_image()]
        self.start_process(images, box)

    def prepare(self):
        if not self.process:
            self.start_process([], prepare=True)

    def start_process(self, images, box=None, prepare=False):
        self.temp = tempfile.TemporaryDirectory(prefix='equation-studio-')
        jobs = []
        for image in images:
            path = Path(self.temp.name) / (image['id'] + '.png')
            path.write_bytes(image['png'])
            jobs.append({'id': image['id'], 'path': str(path), 'name': image['name'], 'box': box})
        request = Path(self.temp.name) / 'request.json'
        request.write_text(json.dumps({'jobs': jobs, 'two_pages': self.two_pages.isChecked(),
                                       'isolated_only': self.isolated.isChecked()}), encoding='utf-8')
        process = QProcess(self)
        self.process = process
        self.buffer, self.errors, self.stderr_tail = b'', [], ''
        self.last_status = 'Đang khởi động…'
        self.cancelled = False
        self.failed = False
        self.received_done = False
        process.readyReadStandardOutput.connect(self.read_output)
        process.readyReadStandardError.connect(self.read_stderr)
        process.finished.connect(self.finished)
        process.errorOccurred.connect(self.process_error)
        args = ['--prepare'] if prepare else ['--ocr', str(request)]
        if not getattr(sys, 'frozen', False):
            args = [str(Path(__file__).resolve())] + args
        process.setWorkingDirectory(str(Path(__file__).resolve().parent))
        self.set_busy(True)
        self.progress.setRange(0, 0)
        self.started = time.monotonic()
        self.clock.start(1000)
        self.status.setText(self.last_status)
        process.start(sys.executable, args)

    def set_busy(self, busy):
        for button in (self.scan_button, self.crop_button, self.import_button, self.paste_button, self.open_button):
            button.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)

    def elapsed(self):
        self.status.setText(f'{self.last_status} | {int(time.monotonic()-self.started)} giây')

    def read_stderr(self):
        if self.process:
            self.stderr_tail = (self.stderr_tail + bytes(self.process.readAllStandardError()).decode('utf-8', 'replace'))[-6000:]

    def read_output(self):
        if not self.process:
            return
        self.buffer += bytes(self.process.readAllStandardOutput())
        while b'\n' in self.buffer:
            line, self.buffer = self.buffer.split(b'\n', 1)
            try:
                event = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            kind = event.get('event')
            if kind == 'formula':
                event.pop('event')
                # Do not add the exact same source box/formula twice on re-scans.
                if not any(f['image_id'] == event['image_id'] and f['box'] == event['box'] and
                           f['latex'] == event['latex'] for f in self.formulas):
                    event.update(id=uuid.uuid4().hex, checked=True)
                    self.formulas.append(event)
                    self.mark_dirty()
                    self.refresh_list()
                    self.highlight()
            elif kind in ('error', 'job_error'):
                self.errors.append(event['message'])
                if kind == 'error':
                    self.failed = True
            elif kind == 'progress':
                self.progress.setRange(0, event['total'])
                self.progress.setValue(event['current']-1)
                self.last_status = f"Ảnh {event['current']}/{event['total']}: {event['message']}"
            elif kind in ('status', 'done'):
                self.last_status = event['message']
                self.received_done |= kind == 'done'
        self.elapsed()

    def process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.errors.append('Không khởi động được Python/OCR worker.')
            self.failed = True
            self.finished(-1, QProcess.ExitStatus.CrashExit)

    def finished(self, exit_code, exit_status):
        if self.process is None:
            return
        self.read_output()
        self.read_stderr()
        process = self.process
        self.process = None
        self.clock.stop()
        self.set_busy(False)
        self.progress.setRange(0, 1)
        self.progress.setValue(0 if self.cancelled or self.failed or self.errors else 1)
        if self.cancelled:
            self.status.setText('Đã hủy. Kết quả đã nhận dạng vẫn được giữ.')
        elif self.errors or exit_code != 0 or not self.received_done:
            self.status.setText('Có lỗi OCR. Kết quả đã nhận dạng vẫn được giữ; có thể sửa và xuất Word.')
            message = '\n'.join(self.errors) or self.stderr_tail or f'Worker dừng với mã {exit_code}.'
            self.notify_error(message + '\n\nNếu thiếu mô hình, chạy prepare_models.bat khi có Internet.')
        else:
            self.status.setText(self.last_status)
        process.deleteLater()
        self.temp.cleanup()
        self.temp = None

    def cancel(self):
        if self.process:
            self.cancelled = True
            self.cancel_button.setEnabled(False)
            self.process.kill()

    def refresh_list(self, *args):
        self.list.blockSignals(True)
        self.list.clear()
        seen = set()
        selected_item = None
        hidden = 0
        for number, f in enumerate(self.formulas, 1):
            try:
                key = clean_latex(f['latex'])
            except ValueError:
                key = f['latex']
            if self.dedup.isChecked() and key in seen:
                hidden += 1
                continue
            seen.add(key)
            item = QListWidgetItem(f"{number}. {f['source_name']}\n{f['latex'][:90]}")
            item.setData(Qt.ItemDataRole.UserRole, f['id'])
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if f.get('checked', True) else Qt.CheckState.Unchecked)
            self.list.addItem(item)
            if f['id'] == self.selected_id:
                selected_item = item
        if selected_item:
            self.list.setCurrentItem(selected_item)
        self.list.blockSignals(False)
        self.count.setText(f'{len(self.formulas)} công thức • {hidden} trùng bị ẩn')
        if selected_item is None:
            self.selected_id = None
            self.select_formula(None, None)
            if self.list.count():
                self.list.setCurrentRow(0)

    def select_formula(self, item, previous):
        self.selected_id = item.data(Qt.ItemDataRole.UserRole) if item else None
        f = self.current_formula()
        self.editor.blockSignals(True)
        self.editor.setPlainText(f['latex'] if f else '')
        self.editor.blockSignals(False)
        if not f:
            self.details.setText('Chọn một công thức để xem/sửa.')
            self.crop_preview.clear()
            self.web.setHtml('<html><body></body></html>')
            self.highlight()
            return
        image = next(x for x in self.images if x['id'] == f['image_id'])
        index = self.images.index(image)
        if self.image_combo.currentIndex() != index:
            self.image_combo.setCurrentIndex(index)
        score = f.get('score')
        self.details.setText(f"{f['source_name']} • {f['kind']}" +
                             (f" • điểm OCR: {score:.2f} (cần đối chiếu)" if score is not None else ''))
        pix = QPixmap()
        pix.loadFromData(image['png'])
        x1, y1, x2, y2 = [int(n) for n in f['box']]
        self.crop_preview.setPixmap(pix.copy(x1, y1, x2-x1, y2-y1).scaled(
            420, 150, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.highlight()
        self.preview()

    def edited(self):
        f = self.current_formula()
        if f:
            f['latex'] = self.editor.toPlainText()
            self.mark_dirty()
            item = self.list.currentItem()
            if item:
                index = self.formulas.index(f)+1
                self.list.blockSignals(True)
                item.setText(f"{index}. {f['source_name']}\n{f['latex'][:90]}")
                self.list.blockSignals(False)
            self.preview_timer.start(350)

    def preview(self):
        f = self.current_formula()
        if not f:
            return
        try:
            mml = to_mathml(f['latex'])
            self.web.setHtml('<!DOCTYPE html><html><head><meta charset="utf-8">'
                             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
                             '<style>body{padding:20px;color:#14223b}math{font-size:25px}</style></head>'
                             f'<body>{mml}</body></html>', QUrl('about:blank'))
            self.preview_message.setText('Xem trước • đối chiếu chỉ số, dấu căn và ký hiệu với ảnh gốc')
        except Exception as exc:
            self.web.setHtml('<html><body></body></html>')
            self.preview_message.setText(f'Chưa chuyển được: {exc}')

    def check_changed(self, item):
        f = next(x for x in self.formulas if x['id'] == item.data(Qt.ItemDataRole.UserRole))
        f['checked'] = item.checkState() == Qt.CheckState.Checked
        self.mark_dirty()

    def check_all(self, checked):
        # Only visible rows; hidden duplicates remain stored but excluded from export.
        for i in range(self.list.count()):
            self.list.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def chosen(self):
        ids = {self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())}
        return [f for f in self.formulas if f['id'] in ids and f.get('checked', True)]

    def manual_latex(self):
        image = self.current_image()
        if not image:
            self.notify_error('Import một ảnh để gắn công thức với nguồn trước.')
            return
        latex, ok = QInputDialog.getMultiLineText(self, 'Thêm công thức', 'Nhập LaTeX:')
        if ok and latex.strip():
            from PIL import Image
            with Image.open(io.BytesIO(image['png'])) as im:
                box = [0, 0, im.width, im.height]
            f = dict(id=uuid.uuid4().hex, image_id=image['id'], source_name=image['name'],
                     latex=latex, box=box, kind='manual', score=None, checked=True)
            self.formulas.append(f)
            self.selected_id = f['id']
            self.mark_dirty()
            self.refresh_list()
            self.select_formula(self.list.currentItem(), None)

    def delete_formula(self):
        f = self.current_formula()
        if f:
            self.formulas.remove(f)
            self.selected_id = None
            self.mark_dirty()
            self.refresh_list()
            self.highlight()

    def copy_equation(self):
        f = self.current_formula()
        if not f:
            self.notify_error('Chọn một công thức trước.')
            return
        try:
            mml = to_mathml(f['latex'])
            mime = QMimeData()
            mime.setText(mml)
            mime.setHtml(f'<html><body><!--StartFragment-->{mml}<!--EndFragment--></body></html>')
            mime.setData('application/mathml+xml', mml.encode('utf-8'))
            mime.setData('application/x-qt-windows-mime;value="MathML"', mml.encode('utf-8') + b'\0')
            QApplication.clipboard().setMimeData(mime)
            self.status.setText('Đã copy Equation. Trong Word desktop nhấn Ctrl + V. Nếu dán ra text, dùng Xuất Word.')
        except Exception as exc:
            self.notify_error(exc)

    def copy_latex(self):
        f = self.current_formula()
        if f:
            QApplication.clipboard().setText(f['latex'])
            self.status.setText('Đã copy LaTeX. Word: Alt + = → LaTeX → dán → Convert / Professional.')

    def export(self):
        formulas = self.chosen()
        if not formulas:
            self.notify_error('Tick ít nhất một công thức.')
            return
        path, _ = QFileDialog.getSaveFileName(self, 'Xuất Equation Word', 'Equations.docx', 'Word (*.docx)')
        if path:
            if not path.lower().endswith('.docx'):
                path += '.docx'
            try:
                export_docx(path, formulas)
                self.status.setText(f'Đã xuất {len(formulas)} Equation chỉnh sửa được: {path}')
            except Exception as exc:
                self.notify_error(exc)

    def save(self):
        path = self.project_path
        if not path:
            path, _ = QFileDialog.getSaveFileName(self, 'Lưu project', 'Equations.eqstudio', 'Equation Studio (*.eqstudio)')
        if not path:
            return False
        if not path.lower().endswith('.eqstudio'):
            path += '.eqstudio'
        try:
            save_session(path, self.images, self.formulas)
            self.project_path = path
            self.dirty = False
            self.setWindowTitle('Equation Studio • Offline')
            self.status.setText(f'Đã lưu: {path}')
            return True
        except Exception as exc:
            self.notify_error(exc)
            return False

    def maybe_save(self):
        if not self.dirty:
            return True
        reply = QMessageBox.question(self, 'Project chưa lưu', 'Lưu thay đổi trước khi tiếp tục?',
                                      QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard |
                                      QMessageBox.StandardButton.Cancel)
        if reply == QMessageBox.StandardButton.Save:
            return self.save()
        return reply == QMessageBox.StandardButton.Discard

    def open_project(self):
        if self.process or not self.maybe_save():
            return
        path, _ = QFileDialog.getOpenFileName(self, 'Mở project', '', 'Equation Studio (*.eqstudio)')
        if path:
            try:
                images, formulas = load_session(path)
                self.images, self.formulas = images, formulas
                self.selected_id = None
                self.image_combo.blockSignals(True)
                self.image_combo.clear()
                for image in self.images:
                    self.image_combo.addItem(image['name'], image['id'])
                self.image_combo.blockSignals(False)
                self.project_path, self.dirty = path, False
                self.setWindowTitle('Equation Studio • Offline')
                self.view.scene().clear()
                self.view.highlights = []
                self.view.bounds = QRectF()
                self.view.start = self.view.rubber = None
                self.image_changed()
                self.refresh_list()
            except Exception as exc:
                self.notify_error(exc)

    def new_project(self):
        if self.process or not self.maybe_save():
            return
        self.images, self.formulas = [], []
        self.selected_id = None
        self.project_path, self.dirty = None, False
        self.image_combo.clear()
        self.view.scene().clear()
        self.view.highlights = []
        self.view.bounds = QRectF()
        self.view.start = self.view.rubber = None
        self.refresh_list()
        self.setWindowTitle('Equation Studio • Offline')

    def help(self):
        QMessageBox.information(self, 'Hướng dẫn',
            '1. Chạy setup_windows.bat (Python 3.11 x64).\n'
            '2. Chạy prepare_models.bat với Internet một lần.\n'
            '3. Import/dán ảnh → chọn chế độ → Trích công thức.\n'
            '4. Khoanh vùng thủ công nếu bị bỏ sót.\n'
            '5. Sửa LaTeX, tick công thức → Copy Equation hoặc Xuất Word.\n'
            'OCR chạy CPU offline; ảnh không gửi tới API.\n'
            'Hủy OCR giữ các kết quả đã có. Lưu project để giữ ảnh và công thức.\n'
            'Hai trang cạnh nhau: quét hết trang trái rồi trang phải.\n'
            'Bản đầu không tự diễn giải/kiểm chứng nội dung toán học.')

    def closeEvent(self, event):
        if self.process:
            self.notify_error('Hãy hủy OCR và chờ worker dừng trước khi đóng app.')
            event.ignore()
        elif self.maybe_save():
            event.accept()
        else:
            event.ignore()


if __name__ == '__main__':
    app = QApplication(sys.argv)
    app.setApplicationName('EquationStudio')
    window = Window()
    window.show()
    sys.exit(app.exec())
