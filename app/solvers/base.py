from __future__ import annotations

from abc import ABC, abstractmethod

from app.domain.models import PlanRequest, PlanResult


class Solver(ABC):
    @abstractmethod
    async def solve(self, request: PlanRequest) -> PlanResult:
        raise NotImplementedError
