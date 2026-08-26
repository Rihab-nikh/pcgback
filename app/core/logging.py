"""Structured logger. Uses codewords_client's logger when available,
falls back to a stdlib shim (so unit tests run without the runtime).
"""
import logging

try:
    from codewords_client import logger  # noqa: F401
except ImportError:  # pragma: no cover - local dev / tests
    _base = logging.getLogger("pcg_engine")
    if not _base.handlers:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    class _Shim:
        """Mimics the keyword-arg structured logging API."""

        @staticmethod
        def _fmt(msg: str, kw: dict) -> str:
            return msg if not kw else msg + " | " + " ".join(f"{k}={v}" for k, v in kw.items())

        def info(self, msg, **kw):
            _base.info(self._fmt(msg, kw))

        def warning(self, msg, **kw):
            _base.warning(self._fmt(msg, kw))

        def error(self, msg, **kw):
            _base.error(self._fmt(msg, kw))

        def debug(self, msg, **kw):
            _base.debug(self._fmt(msg, kw))

    logger = _Shim()
