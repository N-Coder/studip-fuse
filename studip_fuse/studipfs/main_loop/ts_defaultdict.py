import threading
from collections.__init__ import defaultdict


class ThreadSafeDefaultDict(defaultdict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.__lock = threading.Lock()

    def __missing__(self, key):
        with self.__lock:
            if key in self:
                return super().__getitem__(key)
            else:
                return super().__missing__(key)
