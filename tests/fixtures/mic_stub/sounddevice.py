"""Subprocess regression fixture. Never imports PortAudio or opens hardware."""
import numpy as np


class InputStream:
    def __init__(self, **kwargs):
        self.device = 42

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, frames):
        return np.zeros((frames, 1), dtype=np.int16), False
