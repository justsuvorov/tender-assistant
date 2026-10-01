"""Защита консоли сервиса в Windows от «зависания» по щелчку мыши.

В консоли Windows по умолчанию включён режим быстрого выделения (QuickEdit):
щелчок мышью в окне начинает выделение текста, и пока оно не снято, консоль
не принимает вывод. Процесс, который пишет в неё журнал, блокируется на
первой же записи — для пользователя сервис «завис», хотя он просто ждёт,
пока кто-нибудь нажмёт Esc или Enter. Для сервиса, который пишет в консоль
на каждом шаге, это остановка всей обработки.

Режим отключается только для окна этого процесса и только пока он работает.
"""

import logging
import sys

logger = logging.getLogger(__name__)

_STD_INPUT_HANDLE = -10
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_INSERT_MODE = 0x0020
# Без этого флага SetConsoleMode игнорирует изменения QuickEdit/Insert.
_ENABLE_EXTENDED_FLAGS = 0x0080


def disable_quick_edit() -> bool:
    """Отключает QuickEdit у консоли текущего процесса. True — если отключён.

    Вне Windows и без консоли (служба, Docker, вывод в файл) ничего не делает.
    """
    if sys.platform != "win32":
        return False

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False  # stdin — не консоль

        new_mode = (
            mode.value & ~(_ENABLE_QUICK_EDIT_MODE | _ENABLE_INSERT_MODE)
        ) | _ENABLE_EXTENDED_FLAGS
        if not kernel32.SetConsoleMode(handle, new_mode):
            logger.warning(
                "Не удалось отключить выделение мышью в консоли (код %s)",
                ctypes.get_last_error(),
            )
            return False
    except (OSError, AttributeError) as exc:
        logger.warning("Не удалось отключить выделение мышью в консоли: %s", exc)
        return False

    logger.info("Выделение мышью в консоли отключено: щелчок по окну не остановит сервис")
    return True
