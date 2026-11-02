"""把场景函数包装成参数化 pytest 用例。"""

from __future__ import annotations

import pytest

from tests.simulation.scenarios.registry import SCENARIOS


@pytest.mark.simulation
@pytest.mark.parametrize("scenario_id", sorted(SCENARIOS))
def test_scenario(sim, scenario_id: str) -> None:
    SCENARIOS[scenario_id](sim)
