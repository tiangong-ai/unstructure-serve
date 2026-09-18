from collections import Counter

from src.scripts.benchmark_vision import build_request_plan


def test_every_case_visits_every_endpoint_and_seed():
    plan = build_request_plan(case_count=5, endpoint_count=2, repetitions=3)
    assert len(plan) == 30
    assert Counter(plan) == Counter(
        (case, endpoint, repeat)
        for case in range(5)
        for endpoint in range(2)
        for repeat in range(3)
    )
    assert plan == build_request_plan(case_count=5, endpoint_count=2, repetitions=3)
    assert plan != sorted(plan)
