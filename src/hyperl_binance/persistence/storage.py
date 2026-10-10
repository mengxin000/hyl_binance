from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from queue import Empty, Full, Queue
from typing import Any


class Storage:
    def __init__(self, root: Path, max_log_bytes: int) -> None:
        self.root, self.max_log_bytes = root, max_log_bytes
        self.queue: Queue[tuple[str, dict[str, Any]]] = Queue(maxsize=20_000)
        self.dropped = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._writer, name="diagnostic-writer", daemon=True)

    def start(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)

    def emit(self, relative: str, data: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait((relative, data))
        except Full:
            self.dropped += 1

    def save_state(self, relative: str, data: dict[str, Any]) -> None:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )
        os.replace(temp, path)

    def _writer(self) -> None:
        while not self._stop.is_set() or not self.queue.empty():
            try:
                relative, payload = self.queue.get(timeout=0.25)
            except Empty:
                continue
            try:
                path = self.root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if (
                    relative.endswith("network_pipeline.jsonl")
                    and path.exists()
                    and path.stat().st_size >= self.max_log_bytes
                ):
                    rotated = path.with_suffix(".1.jsonl")
                    if rotated.exists():
                        rotated.unlink()
                    os.replace(path, rotated)
                with path.open("a", encoding="utf-8") as stream:
                    stream.write(
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
                    )
            except OSError:
                pass
            finally:
                self.queue.task_done()
