"""In-process rules policy backend.

A :class:`LocalRulesPolicy` evaluates proposed tool calls against a
declarative rule set loaded from a config file (YAML or JSON). It needs
no external service, which suits single-tenant gateways that want to
constrain *which resources* a few federated tools may touch — for
example, limiting an infrastructure MCP to a specific set of resource
ids, or a DNS MCP to a single zone.

The rule schema is deployment-agnostic: tool-name globs plus per-field
predicates. All concrete values (ids, zones, names) live in the config
file, never in this module, so the backend stays portable.

Wire it up with ``POLICY_BACKEND=local`` and ``POLICY_RULES_FILE``.

Config schema
-------------

.. code-block:: yaml

    # Action when NO rule matches the called tool. "reject" is the
    # fail-closed default; use "pass" for "guard a few tools, let the
    # rest through" (typical when the relay also serves benign tools).
    default: reject

    rules:
      - tools: ["example-mcp__*"]   # fnmatch globs on "<server>__<tool>"
        require:                    # ALL predicates must hold, else reject
          region: { eq: "eu-west-1" }
          tenant: { in: ["acme", "beta"] }
          name:   { matches: "^proj-" }
        reason: "example-mcp is scoped to the eu-west-1 acme/beta tenants"

Predicates (per field, combined with AND):

- ``eq`` / ``ne`` — string equality / inequality (values coerced to str)
- ``in``          — membership in a list (values coerced to str)
- ``matches``     — :func:`re.search` against the field value

A required field that is absent from the call fails its predicate and
the call is rejected — a privileged tool invoked without its scoping
argument is denied, not allowed through.
"""

from __future__ import annotations

import json
import re
from fnmatch import fnmatchcase
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .client import PolicyClient, PolicyClientError
from .models import Decision, EvaluateRequest, EvaluateResponse


class FieldPredicate(BaseModel):
    """Constraints on a single argument field. AND-combined.

    ``None`` is the "unset" sentinel for every predicate, so a config
    value of ``null`` means "no constraint" rather than "must equal the
    string 'None'" — an unset predicate is skipped during evaluation.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    eq: Optional[Any] = None
    ne: Optional[Any] = None
    in_: Optional[list[Any]] = Field(default=None, alias="in")
    matches: Optional[str] = None


class Rule(BaseModel):
    """A guard over the tools matching any of ``tools``.

    ``principals`` optionally restricts the rule to specific caller
    identities (multi-tenant mode): the rule applies only when the
    request's principal is in the list. Omitted ⇒ applies to every
    principal (and to 1:1 mode, where there is no principal) — so
    single-tenant rule files are unaffected.
    """

    model_config = ConfigDict(extra="forbid")

    tools: list[str]
    require: dict[str, FieldPredicate] = Field(default_factory=dict)
    principals: Optional[list[str]] = None
    reason: Optional[str] = None


class RuleSet(BaseModel):
    """A complete local policy: a default action plus ordered rules."""

    model_config = ConfigDict(extra="forbid")

    default: Literal["pass", "reject"] = "reject"
    rules: list[Rule] = Field(default_factory=list)


@lru_cache(maxsize=512)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern)


def load_ruleset(path: str) -> RuleSet:
    """Load and validate a rule set from a YAML or JSON file.

    Raises :class:`PolicyClientError` on a missing file, unparseable
    content, a schema violation, or an uncompilable ``matches`` regex.
    """
    try:
        text = Path(path).read_text()
    except OSError as e:
        raise PolicyClientError(f"cannot read POLICY_RULES_FILE {path!r}: {e}") from e

    # PyYAML parses JSON too (JSON is a YAML subset), so one loader covers
    # both .yaml and .json rule files.
    try:
        import yaml

        data = yaml.safe_load(text)
    except yaml.YAMLError as e:  # type: ignore[name-defined]
        raise PolicyClientError(f"rules file {path!r} is not valid YAML/JSON: {e}") from e
    except ImportError as e:  # pragma: no cover - dependency wiring
        # Fall back to JSON if PyYAML is somehow unavailable.
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise PolicyClientError(
                "PyYAML is required to load YAML rules files"
            ) from e

    if data is None:
        data = {}
    try:
        ruleset = RuleSet.model_validate(data)
    except ValueError as e:
        raise PolicyClientError(f"rules file {path!r} failed validation: {e}") from e

    # Fail fast on bad regexes rather than at first matching call.
    for rule in ruleset.rules:
        for field, pred in rule.require.items():
            if pred.matches is not None:
                try:
                    _compiled(pred.matches)
                except re.error as e:
                    raise PolicyClientError(
                        f"invalid 'matches' regex for field {field!r}: {e}"
                    ) from e
    return ruleset


class LocalRulesPolicy(PolicyClient):
    """Evaluate tool calls against an in-process :class:`RuleSet`.

    Stateless after construction and safe to call concurrently.
    """

    def __init__(self, ruleset: RuleSet):
        self._ruleset = ruleset

    @classmethod
    def from_file(cls, path: str) -> "LocalRulesPolicy":
        return cls(load_ruleset(path))

    async def evaluate(self, req: EvaluateRequest) -> EvaluateResponse:
        matched = [
            rule
            for rule in self._ruleset.rules
            if (rule.principals is None or req.principal in rule.principals)
            and any(fnmatchcase(req.tool_name, pat) for pat in rule.tools)
        ]
        if not matched:
            if self._ruleset.default == "pass":
                return EvaluateResponse(
                    decision=Decision.PASS,
                    reasons=["local:no_rule"],
                    policy_matched=False,
                )
            return EvaluateResponse(
                decision=Decision.REJECT,
                reasons=["local:default_reject"],
                policy_matched=False,
            )

        for rule in matched:
            for field, pred in rule.require.items():
                ok, why = self._check(pred, self._lookup(req.fields, field))
                if not ok:
                    reason = rule.reason or f"local:{field}:{why}"
                    return EvaluateResponse(
                        decision=Decision.REJECT,
                        reasons=[reason],
                        policy_matched=True,
                    )
        return EvaluateResponse(
            decision=Decision.PASS,
            reasons=["local:allow"],
            policy_matched=True,
        )

    @staticmethod
    def _lookup(fields: dict[str, str], field: str) -> Optional[str]:
        """Find a field value, tolerating the ``arguments.`` path alias.

        The middleware sends each top-level arg under both its bare key
        and an ``arguments.<key>`` alias; rules may name either form.
        """
        if field in fields:
            return fields[field]
        return fields.get(f"arguments.{field}")

    @staticmethod
    def _check(pred: FieldPredicate, value: Optional[str]) -> tuple[bool, str]:
        if value is None:
            return False, "missing"
        if pred.eq is not None and value != str(pred.eq):
            return False, "eq"
        if pred.ne is not None and value == str(pred.ne):
            return False, "ne"
        if pred.in_ is not None and value not in {str(v) for v in pred.in_}:
            return False, "in"
        if pred.matches is not None and not _compiled(pred.matches).search(value):
            return False, "matches"
        return True, "ok"
