"""One pollable transcript embedding job and foreground admission priority."""
from contextlib import contextmanager
from threading import Condition, Lock, Thread


class EmbeddingAdmission:
    def __init__(self):
        self._condition = Condition()
        self._foreground = 0
        self._busy = False
        self._background = False

    @contextmanager
    def foreground(self):
        with self._condition:
            self._foreground += 1
        try:
            yield
        finally:
            with self._condition:
                self._foreground -= 1

    @contextmanager
    def encode(self, timeout=None):
        with self.foreground():
            with self._condition:
                admitted = self._condition.wait_for(lambda: not self._busy, timeout)
                if admitted:
                    self._busy = True
            try:
                yield admitted
            finally:
                if admitted:
                    self.release()

    def try_background(self):
        with self._condition:
            if self._busy or self._foreground:
                return False
            self._busy = self._background = True
            return True

    def wait_for_background(self, timeout):
        """Called with a foreground request registered against new admissions."""
        with self._condition:
            return self._condition.wait_for(lambda: not self._background, timeout)

    def release(self):
        with self._condition:
            self._busy = self._background = False
            self._condition.notify_all()


class EmbedJobSlot:
    def __init__(self, admission):
        self._admission = admission
        self._lock = Lock()
        self._id = None
        self._result = {"status": "missing"}

    def poll(self, job_id):
        with self._lock:
            return self._result if job_id == self._id else {"status": "missing"}

    def submit(self, job_id, encode, texts):
        with self._lock:
            if job_id == self._id:
                return self._result
            if self._result["status"] == "running":
                return {"status": "busy", "job_id": self._id}
            if not self._admission.try_background():
                return {"status": "busy"}
            self._id = job_id
            self._result = {"status": "running"}
            try:
                Thread(target=self._run, args=(encode, texts), daemon=True,
                       name="transcript-embed").start()
            except RuntimeError:
                self._admission.release()
                self._result = {"status": "failed"}
            return self._result

    def _run(self, encode, texts):
        result = {"status": "failed"}
        try:
            result = {"status": "done", **encode(texts)}
        except Exception:
            pass  # The backend reports and backs off terminal failures.
        finally:
            self._admission.release()
            with self._lock:
                self._result = result
