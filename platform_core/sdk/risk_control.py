from __future__ import annotations

from datetime import UTC, datetime
from typing import Callable, Literal
from uuid import uuid4

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from platform_core.db.models import RiskPolicyVersionRecord

from .risk import LiveRiskPolicy


class RiskPolicyVersion(BaseModel):
    policy_id: str
    scope: str
    version: int
    status: Literal["DRAFT", "APPROVED", "ACTIVE", "RETIRED"]
    policy_payload: dict
    proposed_by: str
    approved_by: str | None = None
    activated_by: str | None = None
    created_at: datetime
    approved_at: datetime | None = None
    activated_at: datetime | None = None


class RiskLimitControlSDK:
    """带版本、双人复核和审计激活的风控策略。"""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._session_factory = session_factory

    def propose(
        self,
        *,
        scope: str,
        policy: LiveRiskPolicy,
        actor: str,
    ) -> RiskPolicyVersion:
        normalized_scope = self._scope(scope)
        normalized_actor = self._actor(actor)
        with self._session_factory() as session, session.begin():
            latest = session.scalar(
                select(func.max(RiskPolicyVersionRecord.version)).where(
                    RiskPolicyVersionRecord.scope == normalized_scope
                )
            )
            row = RiskPolicyVersionRecord(
                policy_id=str(uuid4()),
                scope=normalized_scope,
                version=int(latest or 0) + 1,
                status="DRAFT",
                policy_payload=policy.to_payload(),
                proposed_by=normalized_actor,
                created_at=datetime.now(UTC),
            )
            session.add(row)
            session.flush()
            return self._view(row)

    def approve(self, *, policy_id: str, actor: str) -> RiskPolicyVersion:
        normalized_actor = self._actor(actor)
        with self._session_factory() as session, session.begin():
            row = self._locked(session, policy_id)
            if row.status == "APPROVED":
                return self._view(row)
            if row.status != "DRAFT":
                raise ValueError(f"risk policy cannot be approved from {row.status}")
            if row.proposed_by == normalized_actor:
                raise PermissionError("risk policy proposer cannot approve their own change")
            row.status = "APPROVED"
            row.approved_by = normalized_actor
            row.approved_at = datetime.now(UTC)
            return self._view(row)

    def activate(
        self,
        *,
        policy_id: str,
        actor: str,
        confirmation: str,
    ) -> RiskPolicyVersion:
        normalized_actor = self._actor(actor)
        with self._session_factory() as session, session.begin():
            row = self._locked(session, policy_id)
            if confirmation != f"ACTIVATE-RISK-POLICY:{row.scope}:{row.version}":
                raise PermissionError("risk policy activation confirmation does not match")
            if row.status == "ACTIVE":
                return self._view(row)
            if row.status != "APPROVED" or row.approved_by is None:
                raise PermissionError("risk policy requires independent approval before activation")
            self._retire_active(session, scope=row.scope, except_policy_id=row.policy_id)
            session.flush()
            row.status = "ACTIVE"
            row.activated_by = normalized_actor
            row.activated_at = datetime.now(UTC)
            return self._view(row)

    def rollback(
        self,
        *,
        scope: str,
        version: int,
        actor: str,
        confirmation: str,
    ) -> RiskPolicyVersion:
        normalized_scope = self._scope(scope)
        normalized_actor = self._actor(actor)
        with self._session_factory() as session, session.begin():
            row = session.scalar(
                select(RiskPolicyVersionRecord)
                .where(
                    RiskPolicyVersionRecord.scope == normalized_scope,
                    RiskPolicyVersionRecord.version == version,
                )
                .with_for_update()
            )
            if row is None:
                raise LookupError(f"unknown risk policy {normalized_scope} v{version}")
            if confirmation != f"ROLLBACK-RISK-POLICY:{normalized_scope}:{version}":
                raise PermissionError("risk policy rollback confirmation does not match")
            if row.approved_by is None:
                raise PermissionError("cannot roll back to a policy that was never approved")
            self._retire_active(session, scope=normalized_scope, except_policy_id=row.policy_id)
            session.flush()
            row.status = "ACTIVE"
            row.activated_by = normalized_actor
            row.activated_at = datetime.now(UTC)
            return self._view(row)

    def active(self, scope: str) -> RiskPolicyVersion | None:
        normalized_scope = self._scope(scope)
        with self._session_factory() as session:
            row = session.scalar(
                select(RiskPolicyVersionRecord)
                .where(
                    RiskPolicyVersionRecord.scope == normalized_scope,
                    RiskPolicyVersionRecord.status == "ACTIVE",
                )
                .order_by(RiskPolicyVersionRecord.version.desc())
            )
            return self._view(row) if row is not None else None

    def resolve(self, account: str) -> LiveRiskPolicy | None:
        selected = self.active(f"account:{account}") or self.active("global")
        return (
            LiveRiskPolicy.from_payload(selected.policy_payload)
            if selected is not None
            else None
        )

    def versions(self, scope: str) -> list[RiskPolicyVersion]:
        normalized_scope = self._scope(scope)
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(RiskPolicyVersionRecord)
                    .where(RiskPolicyVersionRecord.scope == normalized_scope)
                    .order_by(RiskPolicyVersionRecord.version.desc())
                )
            )
            return [self._view(row) for row in rows]

    @staticmethod
    def _retire_active(
        session: Session, *, scope: str, except_policy_id: str
    ) -> None:
        for active in session.scalars(
            select(RiskPolicyVersionRecord)
            .where(
                RiskPolicyVersionRecord.scope == scope,
                RiskPolicyVersionRecord.status == "ACTIVE",
                RiskPolicyVersionRecord.policy_id != except_policy_id,
            )
            .with_for_update()
        ):
            active.status = "RETIRED"

    @staticmethod
    def _locked(session: Session, policy_id: str) -> RiskPolicyVersionRecord:
        row = session.scalar(
            select(RiskPolicyVersionRecord)
            .where(RiskPolicyVersionRecord.policy_id == policy_id)
            .with_for_update()
        )
        if row is None:
            raise LookupError(f"unknown risk policy {policy_id!r}")
        return row

    @staticmethod
    def _scope(scope: str) -> str:
        normalized = scope.strip()
        if normalized == "global":
            return normalized
        if not normalized.startswith("account:") or not normalized.removeprefix(
            "account:"
        ).strip():
            raise ValueError("risk scope must be 'global' or 'account:<id>'")
        return f"account:{normalized.removeprefix('account:').strip()}"

    @staticmethod
    def _actor(actor: str) -> str:
        normalized = actor.strip()
        if not normalized:
            raise ValueError("risk policy actor is required")
        return normalized

    @staticmethod
    def _view(row: RiskPolicyVersionRecord) -> RiskPolicyVersion:
        return RiskPolicyVersion(
            policy_id=row.policy_id,
            scope=row.scope,
            version=row.version,
            status=row.status,
            policy_payload=row.policy_payload,
            proposed_by=row.proposed_by,
            approved_by=row.approved_by,
            activated_by=row.activated_by,
            created_at=row.created_at,
            approved_at=row.approved_at,
            activated_at=row.activated_at,
        )
