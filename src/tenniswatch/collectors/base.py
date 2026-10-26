"""Collector contracts; adapters return normalized observations."""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date
from typing import Iterable
from ..domain import SlotObservation


def safe_failure_details(error: Exception) -> dict:
    """예외 메시지·인자·로컬 변수·경로 없이 종류와 코드 위치만 공개한다."""
    names = {"KeyError", "TypeError", "AttributeError", "ValueError", "IndexError",
             "RuntimeError", "Error", "TimeoutError", "CollectionError"}
    kind = type(error).__name__
    result = {"exception_type": kind if kind in names else "OtherError"}
    frame = error.__traceback__
    while frame is not None:
        filename = frame.tb_frame.f_code.co_filename.replace("\\", "/")
        for module in ("collectors/gyeyang.py", "gyeyang.py", "cli.py"):
            if filename.endswith("/tenniswatch/" + module):
                result["code_location"] = f"{module}:{frame.tb_lineno}"
                break
        frame = frame.tb_next
    return result


class CollectionError(ValueError):
    """A safe, classified failure; messages must not contain page/user data."""

    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CollectorCapabilities:
    slots: bool = True
    facility_discovery: bool = False
    court_discovery: bool = False
    policies: bool = False
    requires_browser: bool = False


@dataclass(frozen=True)
class FacilityRef:
    id: int
    name: str
    external_id: str
    booking_url: str


class BaseCollector(ABC):
    site_key: str
    capabilities = CollectorCapabilities()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        """Release resources; HTTP-only adapters need no implementation."""

    @abstractmethod
    def collect_slots(self, facility: FacilityRef, play_date: date) -> Iterable[SlotObservation]:
        """Collect public availability; raise on incomplete/failed collection."""


class CollectorRegistry:
    def __init__(self) -> None:
        self._collectors: dict[str, BaseCollector] = {}

    def register(self, collector: BaseCollector) -> None:
        if collector.site_key in self._collectors:
            raise ValueError(f"collector already registered: {collector.site_key}")
        self._collectors[collector.site_key] = collector

    def get(self, site_key: str) -> BaseCollector:
        try:
            return self._collectors[site_key]
        except KeyError as exc:
            raise LookupError(f"no collector registered for {site_key}") from exc
