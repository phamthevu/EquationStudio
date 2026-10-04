"""OCR runs in a killable process. Only model preparation permits network use."""
from __future__ import annotations
import contextlib
import json
import os
import socket
import sys
import traceback
from pathlib import Path


def emit(**data):
    print(json.dumps(data, ensure_ascii=True), flush=True)


def block_network():
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    def deny(*args, **kwargs):
        raise RuntimeError('Offline: thiếu model. Chạy prepare_models.bat khi có Internet trước.')
    socket.socket.connect = deny
    socket.socket.connect_ex = deny
    socket.create_connection = deny


def make_engine():
    # Disable vendor analytics before loading model libraries or sessions.
    os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
    os.environ['DO_NOT_TRACK'] = '1'
    os.environ['YOLO_OFFLINE'] = 'true'
    os.environ['WANDB_DISABLED'] = 'true'
    import onnxruntime
    onnxruntime.disable_telemetry_events()
    from ultralytics import settings
    settings.update({'sync': False, 'wandb': False, 'mlflow': False,
                     'comet': False, 'clearml': False})
    from pix2text.text_formula_ocr import TextFormulaOCR
    # Do not load layout/table engines; formula detection preserves individual boxes.
    return TextFormulaOCR.from_config(
        total_configs={'languages': ['en'],
                       'formula': {'model_backend': 'onnx'}},
        enable_formula=True, enable_spell_checker=False, device='cpu')


def main(request_path=None, prepare=False):
    try:
        if not prepare:
            block_network()
        emit(event='status', message='Đang nạp mô hình CPU…')
        with contextlib.redirect_stdout(sys.stderr):
            engine = make_engine()
        if prepare:
            # Some recognizers defer loading until the first call.
            from PIL import Image, ImageDraw
            image = Image.new('RGB', (240, 90), 'white')
            ImageDraw.Draw(image).text((30, 30), 'x = 1', fill='black')
            with contextlib.redirect_stdout(sys.stderr):
                engine.recognize(image, return_text=False)
                engine.recognize_formula(image)
            emit(event='done', message='Đã chuẩn bị mô hình. Có thể chạy offline.')
            return
        from PIL import Image
        request = json.loads(Path(request_path).read_text(encoding='utf-8'))
        jobs = request['jobs']
        for number, job in enumerate(jobs, 1):
            emit(event='progress', current=number, total=len(jobs), message=job['name'])
            try:
                with Image.open(job['path']) as source:
                    image = source.convert('RGB')
                if job.get('box'):
                    box = job['box']
                    with contextlib.redirect_stdout(sys.stderr):
                        latex = engine.recognize_formula(image.crop(tuple(box)))
                    if isinstance(latex, list):
                        latex = latex[0] if latex else ''
                    if isinstance(latex, dict):
                        latex = latex['text']
                    emit(event='formula', image_id=job['id'], source_name=job['name'],
                         box=box, latex=str(latex), kind='manual', score=None)
                    continue
                # Explicit left-page then right-page ordering for two-page screenshots.
                sections = [(0, image.width)]
                if request.get('two_pages'):
                    sections = [(0, image.width // 2), (image.width // 2, image.width)]
                for left, right in sections:
                    emit(event='status', message=f"Đang quét {job['name']} — vùng {left}:{right}…")
                    with contextlib.redirect_stdout(sys.stderr):
                        result = engine.recognize(image.crop((left, 0, right, image.height)),
                                                  return_text=False, resized_shape=1024)
                    for item in result:
                        if item.get('type') not in ('isolated', 'embedding'):
                            continue
                        if request.get('isolated_only') and item['type'] != 'isolated':
                            continue
                        points = item['position']
                        xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
                        box = [max(0, int(min(xs)) + left), max(0, int(min(ys))),
                               min(image.width, int(max(xs)) + left + 1),
                               min(image.height, int(max(ys)) + 1)]
                        if box[2] <= box[0] or box[3] <= box[1]:
                            continue
                        emit(event='formula', image_id=job['id'], source_name=job['name'],
                             box=box, latex=item['text'], kind=item['type'],
                             score=float(item.get('score', 0)))
            except Exception as exc:
                emit(event='job_error', message=f"{job['name']}: {exc}")
                traceback.print_exc(file=sys.stderr)
        emit(event='done', message='Hoàn tất. Hãy đối chiếu ảnh trước khi copy/xuất.')
    except Exception as exc:
        emit(event='error', message=str(exc))
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main(None if '--prepare' in sys.argv else sys.argv[1], '--prepare' in sys.argv))
