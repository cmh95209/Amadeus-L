"""Tiny stand-in for the 'eunjeon' package (no Windows build exists).
GPT-SoVITS's Korean reader (g2pk2) only needs Mecab().pos(text); the
python-mecab-ko wheel provides the same analyser with its own dictionary."""
from mecab import MeCab as _MeCab


class Mecab:
    def __init__(self, dicpath=None, *args, **kwargs):
        self._m = _MeCab()

    def pos(self, text):
        return self._m.pos(text)
