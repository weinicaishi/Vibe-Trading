from __future__ import annotations

from pathlib import Path

import yaml


RULES_PATH = Path(
    "deploy/market-morning/monitoring/market-morning.rules.yml"
)
SCRAPE_PATH = Path(
    "deploy/market-morning/monitoring/prometheus-scrape.example.yml"
)
ALERTMANAGER_PATH = Path(
    "deploy/market-morning/monitoring/alertmanager.example.yml"
)


def _load(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_market_morning_alert_rules_cover_conditions_and_pipeline_health() -> None:
    payload = _load(RULES_PATH)
    groups = payload["groups"]
    assert len(groups) == 1
    assert groups[0]["name"] == "market-morning-operations"
    assert groups[0]["interval"] == "30s"
    rules = {rule["alert"]: rule for rule in groups[0]["rules"]}
    assert set(rules) == {
        "MarketMorningCriticalCondition",
        "MarketMorningWarningCondition",
        "MarketMorningMetricsScrapeDown",
        "MarketMorningMetricsSnapshotMissing",
        "MarketMorningMetricsSnapshotStale",
    }
    assert rules["MarketMorningCriticalCondition"]["expr"] == (
        'market_morning_operations_alert_count{severity="critical"} > 0'
    )
    assert rules["MarketMorningWarningCondition"]["expr"] == (
        'market_morning_operations_alert_count{severity="warning"} > 0'
    )
    assert rules["MarketMorningMetricsScrapeDown"]["expr"] == (
        'up{job="market-morning-operations"} == 0'
    )
    assert rules["MarketMorningMetricsSnapshotMissing"]["expr"] == (
        "absent(market_morning_operations_snapshot_timestamp_seconds)"
    )
    assert rules["MarketMorningMetricsSnapshotStale"]["expr"] == (
        "time() - market_morning_operations_snapshot_timestamp_seconds > 600"
    )


def test_alert_rules_are_immediate_for_business_conditions_and_bounded_for_transport() -> None:
    rules = {
        rule["alert"]: rule
        for rule in _load(RULES_PATH)["groups"][0]["rules"]
    }
    assert rules["MarketMorningCriticalCondition"]["for"] == "0m"
    assert rules["MarketMorningWarningCondition"]["for"] == "0m"
    for alert_name in (
        "MarketMorningMetricsScrapeDown",
        "MarketMorningMetricsSnapshotMissing",
        "MarketMorningMetricsSnapshotStale",
    ):
        assert rules[alert_name]["for"] == "2m"


def test_alert_rules_only_use_privacy_safe_labels_and_annotations() -> None:
    raw = RULES_PATH.read_text(encoding="utf-8")
    lowered = raw.lower()
    for forbidden in (
        "user_id",
        "email",
        "source_url",
        "provider_request_id",
        "payload",
        "authorization:",
        "bearer ",
    ):
        assert forbidden not in lowered
    for rule in _load(RULES_PATH)["groups"][0]["rules"]:
        labels = rule["labels"]
        assert labels["service"] == "market-morning"
        assert labels["owner"] == "market-morning-operations"
        assert labels["severity"] in {"warning", "critical"}
        assert set(labels) <= {"severity", "service", "owner", "code"}


def test_scrape_template_uses_https_bearer_file_and_protected_metrics_path() -> None:
    payload = _load(SCRAPE_PATH)
    assert payload["rule_files"] == [
        "/etc/prometheus/rules/market-morning.rules.yml"
    ]
    jobs = payload["scrape_configs"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_name"] == "market-morning-operations"
    assert job["scheme"] == "https"
    assert job["metrics_path"] == (
        "/market-morning/_internal/operations/metrics"
    )
    assert job["params"] == {"hours": ["24"]}
    assert job["authorization"] == {
        "type": "Bearer",
        "credentials_file": (
            "/run/secrets/market_morning_metrics_bearer_token"
        ),
    }
    assert job["static_configs"][0]["labels"]["environment"] == "staging"


def test_scrape_template_contains_no_inline_secret_or_query_token() -> None:
    raw = SCRAPE_PATH.read_text(encoding="utf-8")
    lowered = raw.lower()
    assert "credentials:" not in lowered
    assert "access_token" not in lowered
    assert "api_key" not in lowered
    assert "token=" not in lowered


def test_alertmanager_template_routes_warning_and_critical_separately() -> None:
    payload = _load(ALERTMANAGER_PATH)
    route = payload["route"]
    assert route["receiver"] == "market-morning-unmatched"
    assert route["group_by"] == [
        "alertname",
        "service",
        "severity",
        "code",
    ]
    routes = {item["receiver"]: item for item in route["routes"]}
    assert set(routes) == {
        "market-morning-critical-webhook",
        "market-morning-warning-webhook",
    }
    assert routes["market-morning-critical-webhook"]["matchers"] == [
        'service = "market-morning"',
        'severity = "critical"',
    ]
    assert routes["market-morning-critical-webhook"]["group_wait"] == "0s"
    assert routes["market-morning-critical-webhook"]["repeat_interval"] == "15m"
    assert routes["market-morning-warning-webhook"]["group_wait"] == "30s"
    assert routes["market-morning-warning-webhook"]["repeat_interval"] == "1h"


def test_alertmanager_template_uses_secret_files_and_sends_resolved() -> None:
    payload = _load(ALERTMANAGER_PATH)
    receivers = {item["name"]: item for item in payload["receivers"]}
    assert receivers["market-morning-unmatched"] == {
        "name": "market-morning-unmatched"
    }
    for name, expected_file in {
        "market-morning-critical-webhook": (
            "/run/secrets/market_morning_critical_webhook_url"
        ),
        "market-morning-warning-webhook": (
            "/run/secrets/market_morning_warning_webhook_url"
        ),
    }.items():
        configs = receivers[name]["webhook_configs"]
        assert configs == [
            {
                "url_file": expected_file,
                "send_resolved": True,
                "max_alerts": 20,
                "timeout": "10s",
            }
        ]

    raw = ALERTMANAGER_PATH.read_text(encoding="utf-8").lower()
    assert "url:" not in raw
    assert "authorization:" not in raw
    assert "bearer " not in raw
    assert "token=" not in raw
