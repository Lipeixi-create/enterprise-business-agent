from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Any, Iterable

from ..analysis.contracts import DimensionFilter
from ..config import Settings


_READ_SCOPES = frozenset(
    {
        "run:read",
        "evidence:read",
        "finding:read",
        "report:read",
        "approval:read",
    }
)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    "analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
    "restricted_analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
    "viewer": frozenset({"metrics:read"}),
    "admin": frozenset(
        {
            "metrics:read",
            "agent:run",
            "admin",
            "resource:read_any",
            "approval:decide",
            "approval:read_any",
        }
    )
    | _READ_SCOPES,
    "business_manager": frozenset(
        {
            "metrics:read",
            "agent:run",
            "resource:read_any",
            "approval:decide",
            "approval:read_any",
        }
    )
    | _READ_SCOPES,
    "approver": frozenset(
        {"approval:decide", "resource:read_any"}
    )
    | _READ_SCOPES,
    "sales_analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
    "marketing_analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
    "finance_analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
    "supply_chain_analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
    "hr_analyst": frozenset({"metrics:read", "agent:run"}) | _READ_SCOPES,
}

RESOURCE_SCOPES: dict[str, str] = {
    "run": "run:read",
    "evidence": "evidence:read",
    "finding": "finding:read",
    "report": "report:read",
}

# Default policies are merged with EBA_AUTHZ_POLICY. Department analysts are
# denied every domain except their own unless configuration explicitly widens
# the rule. ``analyst`` and ``viewer`` keep their previous unrestricted
# behaviour for backward compatibility.
DEFAULT_ROLE_POLICIES: dict[str, dict[str, Any]] = {
    "sales_analyst": {"domains": ["Sales"]},
    "marketing_analyst": {"domains": ["Marketing"]},
    "finance_analyst": {"domains": ["Finance"]},
    "supply_chain_analyst": {"domains": ["Supply Chain"]},
    "hr_analyst": {"domains": ["HR"]},
}


class AuthenticationError(Exception):
    code = "unauthorized"


class AuthorizationError(Exception):
    code = "forbidden"

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None):
        self.message = message
        self.detail = detail or {}
        super().__init__(message)


@dataclass(frozen=True)
class Principal:
    principal_id: str
    role: str
    scopes: frozenset[str]
    authenticated: bool = True

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


def authenticate(settings: Settings, api_key: str | None) -> Principal:
    """Resolve a request principal without ever storing the raw key.

    When API keys are configured, a valid key is mandatory. Without keys the
    service is in local development mode and only permits anonymous access if
    EBA_ALLOW_ANONYMOUS_API is true (the default).
    """

    if settings.api_keys:
        presented = api_key or ""
        if not presented:
            raise AuthenticationError("a valid X-API-Key header is required")
        # Compare against every configured key with a constant-time primitive
        # instead of a dict lookup so a missing header or an early match does
        # not become a timing side channel. Never echo the presented key.
        role: str | None = None
        for configured, candidate_role in settings.api_keys.items():
            if hmac.compare_digest(configured, presented):
                role = candidate_role
        if role is None:
            raise AuthenticationError("a valid X-API-Key header is required")
        key_id = hashlib.sha256(presented.encode("utf-8")).hexdigest()[:12]
        return Principal(
            principal_id=f"key:{key_id}",
            role=role,
            scopes=ROLE_SCOPES.get(role, frozenset()),
            authenticated=True,
        )
    if not settings.allow_anonymous_api:
        raise AuthenticationError("anonymous API access is disabled")
    return Principal(
        principal_id="anonymous",
        role="analyst",
        scopes=ROLE_SCOPES["analyst"],
        authenticated=False,
    )


class AuthorizationPolicy:
    """Role-based data and resource restrictions on top of tool scopes.

    A policy entry looks like:
      {"restricted_analyst": {"metrics": ["sales_amount"],
                              "dimensions": ["region", "product"],
                              "regions": ["EC"],
                              "tools": ["query_metric", "compare_periods"]}}
    Missing keys mean unrestricted. Department roles have default domain rules
    in :data:`DEFAULT_ROLE_POLICIES`; explicit configuration overrides them.
    """

    def __init__(self, settings: Settings):
        self._rules = settings.authz_policy or {}

    def _rule(self, role: str) -> dict[str, Any]:
        merged = dict(DEFAULT_ROLE_POLICIES.get(role, {}))
        merged.update(self._rules.get(role, {}))
        return merged

    def require_scope(self, principal: Principal, scope: str) -> None:
        if not principal.has_scope(scope):
            raise AuthorizationError(
                f"role {principal.role!r} is missing required scope {scope!r}",
                detail={"required_scope": scope, "role": principal.role},
            )

    def require_tool(
        self, principal: Principal, scope: str, tool_name: str | None = None
    ) -> None:
        self.require_scope(principal, scope)
        allowed_tools = self._rule(principal.role).get("tools")
        if (
            tool_name is not None
            and allowed_tools is not None
            and tool_name not in allowed_tools
        ):
            raise AuthorizationError(
                f"tool {tool_name!r} is not allowed for role {principal.role!r}",
                detail={"tool": tool_name, "role": principal.role},
            )

    def tool_allowed(self, principal: Principal, tool_name: str, scope: str) -> bool:
        try:
            self.require_tool(principal, scope, tool_name)
        except AuthorizationError:
            return False
        return True

    def require_resource(
        self, principal: Principal, resource: str, owner: str
    ) -> None:
        """Authorize access to a stored run artifact.

        Ownership is the default boundary. Only principals with
        ``resource:read_any`` (admin and business management by default) may
        cross it. Resource types have separate scopes so a role cannot gain
        report access merely by holding an unrelated run scope.
        """

        scope = RESOURCE_SCOPES.get(resource)
        if scope is None:
            raise AuthorizationError(
                f"unknown protected resource {resource!r}",
                detail={"resource": resource, "role": principal.role},
            )
        self.require_scope(principal, scope)
        if owner == principal.principal_id or principal.has_scope("resource:read_any"):
            return
        raise AuthorizationError(
            f"role {principal.role!r} is not authorized to read this {resource}",
            detail={"resource": resource, "role": principal.role},
        )

    def resource_allowed(
        self, principal: Principal, resource: str, owner: str
    ) -> bool:
        try:
            self.require_resource(principal, resource, owner)
        except AuthorizationError:
            return False
        return True

    def require_metric(
        self,
        principal: Principal,
        metric: str,
        *,
        domain: str | None = None,
        source: str | None = None,
    ) -> None:
        rule = self._rule(principal.role)
        allowed = rule.get("metrics")
        if allowed is not None and metric not in allowed:
            raise AuthorizationError(
                f"metric {metric!r} is not allowed for role {principal.role!r}",
                detail={"metric": metric, "role": principal.role},
            )
        allowed_domains = rule.get("domains")
        if allowed_domains is not None:
            if domain is None:
                raise AuthorizationError(
                    f"business domain for metric {metric!r} could not be verified",
                    detail={"metric": metric, "role": principal.role},
                )
            if domain not in allowed_domains:
                raise AuthorizationError(
                    f"business domain {domain!r} is not allowed for role {principal.role!r}",
                    detail={"domain": domain, "role": principal.role},
                )
        allowed_resources = rule.get("resources")
        if allowed_resources is not None:
            if source is None:
                raise AuthorizationError(
                    f"data resource for metric {metric!r} could not be verified",
                    detail={"metric": metric, "role": principal.role},
                )
            if source not in allowed_resources:
                raise AuthorizationError(
                    f"data resource {source!r} is not allowed for role {principal.role!r}",
                    detail={"resource": source, "role": principal.role},
                )

    def metric_allowed(
        self,
        principal: Principal,
        metric: str,
        *,
        domain: str | None = None,
        source: str | None = None,
    ) -> bool:
        try:
            self.require_metric(principal, metric, domain=domain, source=source)
        except AuthorizationError:
            return False
        return True

    def require_dimensions(self, principal: Principal, dimensions: Iterable[str]) -> None:
        allowed = self._rule(principal.role).get("dimensions")
        if allowed is None:
            return
        denied = sorted(set(dimensions) - set(allowed))
        if denied:
            raise AuthorizationError(
                f"dimensions {denied!r} are not allowed for role {principal.role!r}",
                detail={"dimensions": denied, "role": principal.role},
            )

    def require_filters(self, principal: Principal, filters: Iterable[DimensionFilter]) -> None:
        allowed_dimensions = self._rule(principal.role).get("dimensions")
        allowed_regions = self._rule(principal.role).get("regions")
        for item in filters:
            if allowed_dimensions is not None and item.dimension not in allowed_dimensions:
                raise AuthorizationError(
                    f"filter dimension {item.dimension!r} is not allowed",
                    detail={"dimension": item.dimension, "role": principal.role},
                )
            if item.dimension == "region" and allowed_regions is not None:
                values = item.value if isinstance(item.value, list) else [item.value]
                if item.operator not in {"eq", "in"} or any(
                    str(value) not in allowed_regions for value in values
                ):
                    raise AuthorizationError(
                        f"region filter is outside the allowed regions {sorted(allowed_regions)!r}",
                        detail={"region_filter": item.model_dump(mode="json"), "role": principal.role},
                    )

    def require_region_scope(
        self, principal: Principal, filters: Iterable[DimensionFilter]
    ) -> None:
        """A region-restricted role must constrain every query to its regions.

        ``require_filters`` alone only validates filters that happen to be
        present. Without this check a restricted role could omit the filter and
        read a national aggregate, or group by region and see every region.
        """

        allowed_regions = self._rule(principal.role).get("regions")
        if allowed_regions is None:
            return
        region_filters = [item for item in filters if item.dimension == "region"]
        if not region_filters:
            raise AuthorizationError(
                "region-restricted role must filter the query to its allowed regions",
                detail={"allowed_regions": sorted(allowed_regions), "role": principal.role},
            )

    def require_request(
        self,
        principal: Principal,
        *,
        metric: str,
        dimensions: Iterable[str] = (),
        filters: Iterable[DimensionFilter] = (),
        metric_domain: str | None = None,
        metric_source: str | None = None,
    ) -> None:
        filters = list(filters)
        self.require_metric(
            principal,
            metric,
            domain=metric_domain,
            source=metric_source,
        )
        self.require_dimensions(principal, dimensions)
        self.require_region_scope(principal, filters)
        self.require_filters(principal, filters)

    def allows_sql_fallback(self, principal: Principal) -> bool:
        """Return whether unconstrained guarded SQL is safe for this role.

        The SQL fallback has no metric/domain/region policy context: SQL Guard
        can prove a query is read-only, but it cannot know whether a restricted
        analyst is allowed to see the selected table or region. A role with any
        data restriction therefore cannot use fallback; the semantic path with
        ``require_request`` remains available.
        """

        rule = self._rule(principal.role)
        return not any(
            key in rule
            for key in ("metrics", "domains", "resources", "dimensions", "regions", "tools")
        )
