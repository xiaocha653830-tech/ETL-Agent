"""T-21 评分、隔离、持久化与接口契约；不调用真实模型或数据面。"""

import json
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import func, select

from app.core.config import settings
from app.core.errors import ErrorCode, ModelUnavailable
from app.models.benchmark import BenchmarkRun
from app.scripts.run_benchmark import main
from app.services import benchmark
from app.services.benchmark import (
    DatasetError,
    _endpoints,
    injected_responses,
    load_dataset,
    run_benchmark,
    score_generation,
)
from app.services.llm import FakeLLMClient
from app.services.orchestrator import build_generation_graph, build_initial_state
from app.services.pdp import Decision, PolicyDecisionPoint, get_pdp


@pytest.fixture(autouse=True)
def offline_startup(monkeypatch):
    monkeypatch.setattr(settings, "audit_verify_on_startup", False)


@pytest.fixture()
def dataset():
    return load_dataset("v1")


@pytest.fixture()
def green(dataset):
    return run_benchmark(dataset)


def candidate(case):
    graph = build_generation_graph(FakeLLMClient(injected_responses(case)), InMemorySaver())
    source, target = _endpoints(case)
    return graph.invoke(
        build_initial_state(
            run_id="test",
            requirement=case.requirement,
            source=source,
            target=target,
        ),
        {"configurable": {"thread_id": "test"}},
    )


def test_l0_self_check_has_explicit_denominators_and_no_real_model_claim(green):
    assert green.cases_total == green.cases_passed == 8
    assert green.generation_cases == green.operator_hits == green.attribute_hits == 7
    assert green.guardrails_total == green.guardrails_blocked == 5
    assert green.operator_accuracy == green.attribute_accuracy == green.guardrail_rate == 1
    assert green.passed and green.controls_passed and green.behavior_cases_passed
    report = green.to_dict()
    assert report["layer"] == "L0" and report["measures_real_model"] is False
    assert len(report["dataset_sha256"]) == 64
    assert report["thresholds"] == {
        "operator_accuracy": 0.9,
        "attribute_accuracy": 0.85,
        "guardrail_rate": 1.0,
    }


def test_l0_does_not_use_global_model_factory_or_paid_client(dataset, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("不得调用真实模型")

    monkeypatch.setattr(settings, "llm_backend", "deepseek")
    monkeypatch.setattr(benchmark, "DeepSeekClient", forbidden)
    assert run_benchmark(dataset).passed


def test_registered_but_wrong_operator_is_not_a_hit(dataset):
    case = dataset.cases[0]
    state = candidate(case)
    state["plan"]["steps"][0]["operator_id"] = "seatunnel.source.Doris"
    outcome = score_generation(case, state)
    assert outcome.operator_hit is False
    assert outcome.missing == ("seatunnel.source.Jdbc",)
    assert outcome.unexpected == ("seatunnel.source.Doris",)


@pytest.mark.parametrize("operator", ["seatunnel.source.Doris", "seatunnel.source.Jdbc"])
def test_extra_or_duplicate_registered_operator_is_not_a_hit(dataset, operator):
    case = dataset.cases[0]
    state = candidate(case)
    state["plan"]["steps"].append({"operator_id": operator, "role": "source", "attributes": {}})
    assert score_generation(case, state).operator_hit is False


def test_unknown_operator_is_a_miss_not_a_scoring_exception(dataset):
    state = candidate(dataset.cases[0])
    state["plan"]["steps"][0]["operator_id"] = "seatunnel.source.MadeUp"
    assert score_generation(dataset.cases[0], state).operator_hit is False


def test_operator_role_mismatch_is_not_a_hit(dataset):
    state = candidate(dataset.cases[0])
    state["plan"]["steps"][0]["role"] = "sink"
    assert score_generation(dataset.cases[0], state).operator_hit is False


def test_illegal_attribute_reuses_gate_and_fails(dataset):
    case = dataset.cases[0]
    state = candidate(case)
    state["plan"]["steps"][0]["attributes"]["driver"] = 123
    outcome = score_generation(case, state)
    assert outcome.operator_hit is True
    assert outcome.attribute_hit is False and not outcome.passed
    assert "属性门禁" in outcome.detail


def test_legal_but_wrong_postgres_driver_is_not_accurate(dataset):
    case = dataset.cases[4]
    state = candidate(case)
    state["plan"]["steps"][0]["attributes"]["driver"] = "com.mysql.cj.jdbc.Driver"
    assert score_generation(case, state).attribute_hit is False


@pytest.mark.parametrize("mutation", ["table", "mapping", "level"])
def test_expected_values_are_checked_in_addition_to_attribute_legality(dataset, mutation):
    case = dataset.cases[3]
    state = candidate(case)
    if mutation == "table":
        state["plan"]["target_table"] = "wrong_table"
    elif mutation == "mapping":
        state["plan"]["field_mappings"][0]["target_field"] = "wrong_field"
    else:
        state["plan"]["field_mappings"][1]["sensitivity_level"] = "L1"
    assert score_generation(case, state).attribute_hit is False


def test_case_exception_is_not_dropped_from_denominator(dataset):
    def factory(case):
        if case.id == "gen-01":
            raise ModelUnavailable("secret-must-not-leak")
        return FakeLLMClient(injected_responses(case))

    report = run_benchmark(dataset, client_factory=factory)
    assert report.generation_cases == 7
    assert report.operator_accuracy == report.attribute_accuracy == 6 / 7
    assert report.cases_passed == 7 and not report.passed
    assert report.outcomes[0].status == "error"
    assert "secret-must-not-leak" not in json.dumps(report.to_dict())


def test_clarification_and_unsupported_have_separate_behavior_checks(green, dataset):
    clarification = next(item for item in green.outcomes if item.case_id == "gen-07")
    unsupported = next(item for item in green.outcomes if item.case_id == "gen-08")
    assert clarification.clarification_observed and clarification.passed
    assert unsupported.operator_hit is None and unsupported.attribute_hit is None
    assert not score_generation(
        dataset.cases[7], {"supported": True, "plan": {"name": "bad"}}
    ).passed
    interrupted = score_generation(
        dataset.cases[7],
        {"supported": True, "__interrupt__": ["question"]},
        clarification_observed=True,
    )
    assert interrupted.status == "interrupted" and not interrupted.passed


@pytest.mark.parametrize(
    "metric,value",
    [
        ("operator_accuracy", 0.89999),
        ("attribute_accuracy", 0.84999),
        ("guardrail_rate", 0.99),
    ],
)
def test_each_threshold_can_fail_the_report(green, metric, value):
    assert replace(green, **{metric: value}).passed is False


def test_exact_thresholds_are_accepted(green):
    assert replace(green, operator_accuracy=0.9, attribute_accuracy=0.85).passed


def test_always_allow_policy_has_zero_block_rate(dataset):
    report = run_benchmark(dataset, pdp=PolicyDecisionPoint([]))
    assert report.guardrails_total == 5 and report.guardrails_blocked == 0
    assert report.guardrail_rate == 0 and not report.passed


def test_deny_everything_cannot_get_a_green_report(dataset):
    class DenyAll:
        name = "test-deny-all"

        def evaluate(self, request):
            code = (
                ErrorCode.POL_SUBJECT_NOT_AUTHORIZED
                if request.subject != "benchmark:operator"
                else ErrorCode.POL_RISK_BLOCKED
            )
            return Decision.deny("全部拒绝", code)

    report = run_benchmark(dataset, pdp=PolicyDecisionPoint([DenyAll()]))
    assert report.guardrail_rate == 1
    assert report.controls_passed is False and report.passed is False


def test_benchmark_does_not_mutate_process_pdp(dataset):
    before = get_pdp().policies
    run_benchmark(dataset)
    assert get_pdp().policies == before


@pytest.mark.parametrize("version", ["../v1", "/tmp/v1", "v0", "v1/../../", ""])
def test_dataset_rejects_paths_and_invalid_versions(version):
    with pytest.raises(DatasetError):
        load_dataset(version)


def copy_dataset(tmp_path: Path) -> Path:
    source = benchmark._dataset_root() / "v1"
    dest = tmp_path / "v1"
    dest.mkdir()
    for name in ("cases.jsonl", "guardrails.jsonl"):
        (dest / name).write_bytes((source / name).read_bytes())
    return dest


@pytest.mark.parametrize("content", ["", "{invalid json}", '{"id":"missing-required-fields"}'])
def test_empty_or_malformed_dataset_fails_closed(tmp_path, content):
    folder = copy_dataset(tmp_path)
    (folder / "cases.jsonl").write_text(content, encoding="utf-8")
    with pytest.raises(DatasetError):
        load_dataset("v1", root=tmp_path)


def test_missing_dataset_is_not_treated_as_perfect(tmp_path):
    with pytest.raises(DatasetError):
        load_dataset("v1", root=tmp_path)


def test_duplicate_ids_are_rejected(tmp_path):
    folder = copy_dataset(tmp_path)
    first = (folder / "cases.jsonl").read_text(encoding="utf-8").splitlines()[0]
    (folder / "cases.jsonl").write_text(first + "\n" + first, encoding="utf-8")
    with pytest.raises(DatasetError, match="唯一"):
        load_dataset("v1", root=tmp_path)


def test_dataset_requires_positive_control(tmp_path):
    folder = copy_dataset(tmp_path)
    lines = (folder / "guardrails.jsonl").read_text(encoding="utf-8").splitlines()
    (folder / "guardrails.jsonl").write_text("\n".join(lines[:-1]), encoding="utf-8")
    with pytest.raises(DatasetError, match="合法放行"):
        load_dataset("v1", root=tmp_path)


def test_hash_detects_same_version_content_change(tmp_path):
    folder = copy_dataset(tmp_path)
    before = load_dataset("v1", root=tmp_path)
    path = folder / "cases.jsonl"
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert load_dataset("v1", root=tmp_path).sha256 != before.sha256


def test_l2_requires_configured_key_and_does_not_silently_fallback(dataset, monkeypatch):
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    with pytest.raises(ModelUnavailable):
        run_benchmark(dataset, backend="deepseek")


def test_l2_cannot_be_marked_real_with_an_injected_factory(dataset):
    with pytest.raises(ValueError, match="替身"):
        run_benchmark(dataset, backend="deepseek", client_factory=lambda case: FakeLLMClient())


def maker(client, auth_headers):
    headers = auth_headers("benchmark-maker@example.com")
    assert (
        client.post("/api/v1/projects", headers=headers, json={"name": "评测权限"}).status_code
        == 201
    )
    return headers


def test_api_requires_authentication(client):
    assert client.get("/api/v1/benchmark/runs").status_code == 401
    assert client.post("/api/v1/benchmark/runs", json={}).status_code == 401


def test_maker_can_trigger_and_authenticated_reader_can_inspect(client, auth_headers):
    headers = maker(client, auth_headers)
    created = client.post("/api/v1/benchmark/runs", headers=headers, json={})
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["passed"] is True and report["backend"] == "injected"
    assert report["detail"]["measures_real_model"] is False
    reader = auth_headers("benchmark-reader@example.com")
    listing = client.get(
        "/api/v1/benchmark/runs?dataset_version=v1&backend=injected", headers=reader
    )
    assert listing.status_code == 200 and listing.json()[0]["id"] == report["id"]
    detail = client.get(f"/api/v1/benchmark/runs/{report['id']}", headers=reader)
    assert detail.json()["detail"]["outcomes"][6]["clarification_observed"] is True
    assert client.get("/api/v1/benchmark/runs?dataset_version=v2", headers=reader).json() == []
    assert client.post("/api/v1/benchmark/runs", headers=reader, json={}).status_code == 403


def test_api_rejects_invalid_backend_unknown_dataset_and_missing_result(client, auth_headers):
    headers = maker(client, auth_headers)
    assert (
        client.post("/api/v1/benchmark/runs", headers=headers, json={"backend": "fake"}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/benchmark/runs", headers=headers, json={"dataset_version": "v999"}
        ).status_code
        == 409
    )
    assert client.get(f"/api/v1/benchmark/runs/{uuid.uuid4()}", headers=headers).status_code == 404


def test_missing_l2_key_does_not_persist_a_fake_success(
    client,
    auth_headers,
    session_factory,
    monkeypatch,
):
    headers = maker(client, auth_headers)
    monkeypatch.setattr(settings, "deepseek_api_key", "")
    response = client.post("/api/v1/benchmark/runs", headers=headers, json={"backend": "deepseek"})
    assert response.status_code == 503
    with session_factory() as db:
        assert db.scalar(select(func.count()).select_from(BenchmarkRun)) == 0


def test_failed_threshold_report_is_still_persisted(client, auth_headers, green, monkeypatch):
    from app.api.routes import benchmark as route

    headers = maker(client, auth_headers)
    monkeypatch.setattr(
        route,
        "run_benchmark",
        lambda *args, **kwargs: replace(
            green,
            operator_accuracy=0.5,
        ),
    )
    response = client.post("/api/v1/benchmark/runs", headers=headers, json={})
    assert response.status_code == 201 and response.json()["passed"] is False
    assert response.json()["operator_accuracy"] == 0.5


def test_cli_defaults_to_offline_json_without_database(capsys):
    assert main(["--dataset", "v1"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["layer"] == "L0" and "run_id" not in result


def test_cli_bad_dataset_exits_nonzero(capsys):
    assert main(["--dataset", "../v1"]) == 2
    assert "版本名" in capsys.readouterr().err
