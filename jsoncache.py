"""
Cache chave→valor persistido em JSON, seguro entre threads.

Carrega o arquivo uma vez, guarda em memória e grava com escrita atômica
(arquivo temporário + os.replace), então duas requisições simultâneas do
Flask não corrompem o arquivo nem perdem entradas uma da outra.
"""
from __future__ import annotations

import json
import os
import threading
from typing import Any


class JsonCache:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        self._data: dict[str, Any] | None = None

    def _ensure(self) -> dict[str, Any]:
        if self._data is None:
            if os.path.exists(self.path):
                try:
                    with open(self.path, "r", encoding="utf-8") as f:
                        self._data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    self._data = {}   # arquivo corrompido: recomeça do zero
            else:
                self._data = {}
        return self._data

    @property
    def lock(self) -> threading.RLock:
        """Para ler-modificar-gravar uma chave sem corrida com outras threads."""
        return self._lock

    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return self._ensure().get(key, default)

    def __contains__(self, key: str) -> bool:
        with self._lock:
            return key in self._ensure()

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with self._lock:
            self._ensure()[key] = value
            if save:
                self.save()

    def update(self, items: dict[str, Any]) -> None:
        with self._lock:
            self._ensure().update(items)
            self.save()

    def pop(self, key: str) -> Any:
        with self._lock:
            val = self._ensure().pop(key, None)
            self.save()
            return val

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._ensure())

    def save(self) -> None:
        with self._lock:
            tmp = f"{self.path}.{threading.get_ident()}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._ensure(), f, indent=1, ensure_ascii=False)
            os.replace(tmp, self.path)
