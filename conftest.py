"""Make the in-tree package importable when running ``python -m pytest``."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# -- Qt ----------------------------------------------------------------------
# One QApplication for the whole session, kept alive until every widget has
# been garbage collected; destroying it first crashes at interpreter exit.
_QAPP = None


@pytest.fixture(scope="session")
def qapp_session():
    global _QAPP
    pytest.importorskip("PySide6")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    _QAPP = QApplication.instance() or QApplication([])
    return _QAPP


def pytest_sessionfinish(session, exitstatus):
    if _QAPP is None:
        return
    import gc

    from PySide6.QtCore import QCoreApplication, QEvent

    for widget in _QAPP.topLevelWidgets():
        widget.close()
        widget.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    gc.collect()
