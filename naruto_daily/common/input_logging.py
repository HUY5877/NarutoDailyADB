"""Record input commands locally, including their actual dispatch time."""

from contextlib import contextmanager
from datetime import datetime
import logging
import threading
import time


LOGGER = logging.getLogger('naruto_daily.adb')


@contextmanager
def log_adb_input(serial, arguments):
    details = None
    if tuple(arguments[:3]) == ('shell', 'input', 'tap') and len(arguments) == 5:
        details = f'点击 x={arguments[3]} y={arguments[4]}'
    elif tuple(arguments[:3]) == ('shell', 'input', 'swipe') and len(arguments) == 8:
        details = (f'滑动 x1={arguments[3]} y1={arguments[4]} x2={arguments[5]} '
                   f'y2={arguments[6]} duration_ms={arguments[7]}')
    elif tuple(arguments[:3]) == ('shell', 'input', 'keyevent') and len(arguments) == 4:
        details = f'{"返回" if arguments[3] == "4" else "按键"} keycode={arguments[3]}'
    if details is None:
        yield
        return
    context = f'serial={serial or "default"} thread={threading.current_thread().name} {details}'
    started = time.perf_counter()

    def emit(level, outcome, suffix=''):
        LOGGER.log(level, '[ADB操作%s] time=%s %s%s', outcome,
                   datetime.now().astimezone().isoformat(timespec='microseconds'), context, suffix,
                   extra={'backend_only': True, 'file_only': True})

    emit(logging.INFO, '开始')
    try:
        yield
    except BaseException as error:
        emit(logging.WARNING, '失败', f' elapsed_ms={(time.perf_counter() - started) * 1000:.2f}'
             f' error_type={type(error).__name__}')
        raise
    else:
        emit(logging.INFO, '完成', f' elapsed_ms={(time.perf_counter() - started) * 1000:.2f}')
