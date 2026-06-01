"""LocalRulesPolicy tests.

Synthetic fixtures only — no deployment-specific patterns.
"""

from __future__ import annotations

import pytest

from mcp_relay.policy import (
    Decision,
    EvaluateRequest,
    LocalRulesPolicy,
    PolicyClientError,
    RuleSet,
    build_policy_client,
    load_ruleset,
)


def _policy(data: dict) -> LocalRulesPolicy:
    return LocalRulesPolicy(RuleSet.model_validate(data))


def _req(tool: str, **fields: str) -> EvaluateRequest:
    # Mirror the middleware's bare + ``arguments.`` aliasing.
    flat: dict[str, str] = {}
    for k, v in fields.items():
        flat[k] = v
        flat[f"arguments.{k}"] = v
    return EvaluateRequest(tool_name=tool, fields=flat)


# --- default action ---------------------------------------------------------


async def test_default_reject_blocks_unmatched_tool():
    pol = _policy({"default": "reject", "rules": []})
    resp = await pol.evaluate(_req("any-mcp__do_thing"))
    assert resp.decision is Decision.REJECT
    assert resp.reasons == ["local:default_reject"]
    assert resp.policy_matched is False


async def test_default_pass_lets_unguarded_tool_through():
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"id": {"eq": "1"}}}],
        }
    )
    # A tool no rule matches passes under default=pass.
    resp = await pol.evaluate(_req("notes-mcp__append", body="hello"))
    assert resp.decision is Decision.PASS
    assert resp.policy_matched is False


async def test_ruleset_default_is_reject_when_unspecified():
    # Fail-closed by default if the operator omits `default`.
    assert RuleSet.model_validate({"rules": []}).default == "reject"


# --- predicates -------------------------------------------------------------


async def test_eq_pass_and_reject():
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"region": {"eq": "eu"}}}],
        }
    )
    assert (await pol.evaluate(_req("infra-mcp__x", region="eu"))).decision is Decision.PASS
    bad = await pol.evaluate(_req("infra-mcp__x", region="us"))
    assert bad.decision is Decision.REJECT
    assert bad.reasons == ["local:region:eq"]
    assert bad.policy_matched is True


async def test_in_coerces_numeric_values_to_str():
    # YAML ints in the config; string field value from the wire.
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"vmid": {"in": [10, 11]}}}],
        }
    )
    assert (await pol.evaluate(_req("infra-mcp__x", vmid="10"))).decision is Decision.PASS
    assert (await pol.evaluate(_req("infra-mcp__x", vmid="99"))).decision is Decision.REJECT


async def test_eq_coerces_int_rule_value():
    # Rule value is a YAML int; wire field value is the string "123".
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"resource_id": {"eq": 123}}}],
        }
    )
    assert (
        await pol.evaluate(_req("infra-mcp__x", resource_id="123"))
    ).decision is Decision.PASS
    assert (
        await pol.evaluate(_req("infra-mcp__x", resource_id="124"))
    ).decision is Decision.REJECT


async def test_matches_regex():
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"name": {"matches": "^proj-"}}}],
        }
    )
    assert (await pol.evaluate(_req("infra-mcp__x", name="proj-a"))).decision is Decision.PASS
    assert (await pol.evaluate(_req("infra-mcp__x", name="other"))).decision is Decision.REJECT


async def test_ne_predicate():
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"env": {"ne": "prod"}}}],
        }
    )
    assert (await pol.evaluate(_req("infra-mcp__x", env="dev"))).decision is Decision.PASS
    assert (await pol.evaluate(_req("infra-mcp__x", env="prod"))).decision is Decision.REJECT


async def test_multiple_predicates_are_anded():
    pol = _policy(
        {
            "default": "pass",
            "rules": [
                {
                    "tools": ["infra-mcp__*"],
                    "require": {"region": {"eq": "eu"}, "tenant": {"in": ["a", "b"]}},
                }
            ],
        }
    )
    assert (
        await pol.evaluate(_req("infra-mcp__x", region="eu", tenant="a"))
    ).decision is Decision.PASS
    # One predicate failing is enough to reject.
    assert (
        await pol.evaluate(_req("infra-mcp__x", region="eu", tenant="z"))
    ).decision is Decision.REJECT


async def test_missing_required_field_rejects():
    # A privileged tool invoked without its scoping argument is denied.
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["infra-mcp__*"], "require": {"vmid": {"in": [1]}}}],
        }
    )
    resp = await pol.evaluate(_req("infra-mcp__list_all"))  # no vmid
    assert resp.decision is Decision.REJECT
    assert resp.reasons == ["local:vmid:missing"]


# --- tool globbing + custom reason -----------------------------------------


async def test_glob_matches_prefix_and_specific_tool():
    pol = _policy(
        {
            "default": "pass",
            "rules": [{"tools": ["a-mcp__*", "b-mcp__exact"], "require": {"k": {"eq": "v"}}}],
        }
    )
    assert (await pol.evaluate(_req("a-mcp__anything", k="v"))).decision is Decision.PASS
    assert (await pol.evaluate(_req("b-mcp__exact", k="v"))).decision is Decision.PASS
    # b-mcp__other is unmatched -> default pass.
    assert (await pol.evaluate(_req("b-mcp__other"))).decision is Decision.PASS


async def test_custom_reason_used_on_reject():
    pol = _policy(
        {
            "default": "pass",
            "rules": [
                {
                    "tools": ["infra-mcp__*"],
                    "require": {"region": {"eq": "eu"}},
                    "reason": "infra-mcp is scoped to eu",
                }
            ],
        }
    )
    resp = await pol.evaluate(_req("infra-mcp__x", region="us"))
    assert resp.reasons == ["infra-mcp is scoped to eu"]


# --- loading + validation ---------------------------------------------------


def test_load_ruleset_yaml(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(
        "default: pass\n"
        "rules:\n"
        "  - tools: ['infra-mcp__*']\n"
        "    require:\n"
        "      region: { eq: eu }\n"
    )
    rs = load_ruleset(str(p))
    assert rs.default == "pass"
    assert rs.rules[0].tools == ["infra-mcp__*"]
    assert rs.rules[0].require["region"].eq == "eu"


def test_load_ruleset_json_subset(tmp_path):
    p = tmp_path / "rules.json"
    p.write_text('{"default": "reject", "rules": []}')
    rs = load_ruleset(str(p))
    assert rs.default == "reject"


def test_load_missing_file_raises():
    with pytest.raises(PolicyClientError, match="cannot read"):
        load_ruleset("/nonexistent/rules.yaml")


def test_load_bad_regex_fails_fast(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text(
        "default: pass\n"
        "rules:\n"
        "  - tools: ['x__*']\n"
        "    require:\n"
        "      name: { matches: '(' }\n"
    )
    with pytest.raises(PolicyClientError, match="invalid 'matches' regex"):
        load_ruleset(str(p))


def test_load_unknown_field_rejected(tmp_path):
    p = tmp_path / "rules.yaml"
    p.write_text("default: pass\nrules:\n  - tools: ['x__*']\n    nope: 1\n")
    with pytest.raises(PolicyClientError, match="validation"):
        load_ruleset(str(p))


# --- build_policy_client wiring --------------------------------------------


def test_build_local_backend(tmp_path, monkeypatch):
    p = tmp_path / "rules.yaml"
    p.write_text("default: reject\nrules: []\n")
    monkeypatch.setenv("POLICY_BACKEND", "local")
    monkeypatch.setenv("POLICY_RULES_FILE", str(p))
    client = build_policy_client()
    assert isinstance(client, LocalRulesPolicy)


def test_build_local_missing_file_env_raises(monkeypatch):
    monkeypatch.setenv("POLICY_BACKEND", "local")
    monkeypatch.delenv("POLICY_RULES_FILE", raising=False)
    with pytest.raises(PolicyClientError, match="POLICY_RULES_FILE"):
        build_policy_client()
