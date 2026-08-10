from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from people_intel.ledger import LedgerNotFound
from people_intel.person_profile_schemas import (
    PROFILE_FIELDS,
    PersonCogneeProjection,
    PersonCogneeRecallRequest,
    PersonCogneeRecallResponse,
    PersonDigestBatch,
    PersonGraphComparison,
    PersonKeyRecord,
    PersonProfileImportRequest,
    PersonProfileImportRun,
    PersonProfileRevision,
    PersonProfileScanRequest,
    PersonProfileScanResponse,
    PersonProfileScanRun,
    PersonProfileSummary,
    PersonProfileView,
    PersonPlatformAccount,
    PersonScanAction,
    PersonSearchPlanRevision,
    PersonSearchTarget,
    PersonSourceSlice,
    PersonUpdateBundle,
    ProfileEvidenceRef,
    ProfileFieldItem,
    ProfilePatchReviewBatch,
    ProfilePatchReviewBatchView,
    ProfilePatchReviewDecision,
    ProfilePatchReviewDecisionRequest,
    ProfilePatchReviewItem,
)
from people_intel.schemas import (
    CognifyRequest,
    Entity,
    EntityType,
    FetchMethod,
    RightsScope,
    SourceDocumentVersion,
    SourceType,
    new_id,
    utc_now,
)
from people_intel.service import MemoryValidationError, TemporalMemoryService


FIELD_LABELS = {
    "identity": "身份",
    "urls": "主页与账号",
    "source_documents": "原始文档来源",
    "affiliations": "学校、公司与团队",
    "education": "教育经历",
    "awards": "奖项",
    "papers": "论文与作者关系",
    "projects": "项目、代码与产品",
    "research_topics": "研究主题",
    "career_and_funding": "创业、融资与职业事件",
    "relationships": "人物关系",
    "supplementary_information": "补充信息",
}


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def normalize_person_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().casefold()
    normalized = re.sub(r"[\s_/]+", "-", normalized)
    normalized = re.sub(r"[^\w\u4e00-\u9fff-]+", "", normalized, flags=re.UNICODE)
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or stable_id("person", value)[7:23]


def person_dataset_name(person_key: str) -> str:
    return f"people-intel-person-{hashlib.sha256(person_key.encode()).hexdigest()[:16]}"


class PersonProfileService:
    """Exact person-scoped projection over the immutable source/assertion ledger."""

    def __init__(self, memory: TemporalMemoryService):
        self.memory = memory
        self.ledger = memory.ledger

    def backfill(self, request: PersonProfileImportRequest) -> PersonProfileImportRun:
        for existing in self.ledger.list_person_profile_import_runs():
            if existing.command_id == request.command_id:
                return existing

        source_filter = set(request.source_version_ids)
        if request.include_all_existing:
            source_filter.update(item.source_version_id for item in self.ledger.list_source_versions())
        people = sorted(
            (item for item in self.ledger.list_entities() if item.entity_type == EntityType.PERSON),
            key=lambda item: (item.canonical_name.casefold(), item.entity_id),
        )

        created_slices: list[str] = []
        created_profiles: list[str] = []
        created_plans: list[str] = []
        person_keys: list[str] = []
        review_candidates: list[tuple[str, ProfileFieldItem]] = []

        for person in people:
            key_record = self._ensure_person_key(person)
            person_keys.append(key_record.person_key)
            slices = self._ensure_person_slices(key_record, source_filter)
            created_slices.extend(item.person_source_slice_id for item in slices)
            profile, created = self.materialize_profile(key_record.person_key)
            if created:
                created_profiles.append(profile.profile_revision_id)
            plan, plan_created = self.ensure_search_plan(key_record.person_key, created_by=request.created_by)
            if plan_created:
                created_plans.append(plan.search_plan_revision_id)
            for item in profile.fields.get("relationships", []):
                if item.review_status == "pending":
                    review_candidates.append((key_record.person_key, item))

        review_batch_ids: list[str] = []
        if review_candidates:
            all_review_person_keys = sorted({person_key for person_key, _ in review_candidates})
            unique_review_candidates: dict[str, tuple[str, ProfileFieldItem]] = {}
            for person_key, item in review_candidates:
                key = item.assertion_ids[0] if item.assertion_ids else item.field_item_id
                unique_review_candidates.setdefault(key, (person_key, item))
            review_candidates = list(unique_review_candidates.values())
            batch_id = stable_id("pprb", request.command_id, "derived-relationships")
            existing_batches = {item.review_batch_id for item in self.ledger.list_profile_patch_review_batches()}
            item_models = [
                ProfilePatchReviewItem(
                    review_item_id=stable_id("ppri", batch_id, person_key, item.field_item_id),
                    review_batch_id=batch_id,
                    person_key=person_key,
                    field_name=item.field_name,
                    field_item_id=item.field_item_id,
                    target_assertion_id=item.assertion_ids[0] if item.assertion_ids else None,
                    title_zh=f"{person_key}：{item.label_zh}",
                    question_zh="是否确认这条由共同论文等规则推导的人物关系？",
                    reason_zh="逻辑推定必须单独审核，不因依赖事实存在而自动成为已确认事实。",
                    evidence_span_ids=[ref.evidence_span_id for ref in item.evidence],
                )
                for person_key, item in review_candidates
            ]
            if batch_id not in existing_batches:
                self.ledger.append_profile_patch_review_batch(ProfilePatchReviewBatch(
                    review_batch_id=batch_id,
                    title_zh="逐人精确档案：逻辑推定集中审核",
                    source_ref=f"person-profile-import:{request.command_id}",
                    person_keys=all_review_person_keys,
                    review_item_ids=[item.review_item_id for item in item_models],
                ))
                existing_item_ids = {item.review_item_id for item in self.ledger.list_profile_patch_review_items()}
                for item in item_models:
                    if item.review_item_id not in existing_item_ids:
                        self.ledger.append_profile_patch_review_item(item)
            review_batch_ids.append(batch_id)

        run = PersonProfileImportRun(
            import_run_id=stable_id("pimport", request.command_id),
            command_id=request.command_id,
            source_version_ids=sorted(source_filter),
            person_keys=person_keys,
            created_profile_revision_ids=created_profiles,
            created_search_plan_revision_ids=created_plans,
            created_source_slice_ids=created_slices,
            review_batch_ids=review_batch_ids,
            status="completed",
            counts={
                "people": len(person_keys),
                "source_slices": len(self.ledger.list_person_source_slices()),
                "profile_revisions_created": len(created_profiles),
                "search_plans_created": len(created_plans),
                "derived_review_items": len(review_candidates),
            },
            created_by=request.created_by,
            metadata={
                "schema_version": "person-profile-v1",
                "extraction_mode": "deterministic_person_first",
                "cognee_required": False,
            },
        )
        self.ledger.append_person_profile_import_run(run)
        return run

    def list_profiles(self) -> list[PersonProfileSummary]:
        summaries: list[PersonProfileSummary] = []
        for key in sorted(self.ledger.list_person_keys(), key=lambda item: item.canonical_name.casefold()):
            revisions = self.ledger.list_person_profile_revisions(key.person_key)
            if revisions:
                summaries.append(self._summary(key.person_key))
        return summaries

    def profile_view(self, person_key: str) -> PersonProfileView:
        profile = self.latest_profile(person_key)
        plans = self.ledger.list_person_search_plan_revisions(person_key)
        bundles = self.ledger.list_person_update_bundles(person_key)
        review_batches = [
            item for item in self.ledger.list_profile_patch_review_batches()
            if person_key in item.person_keys
        ]
        return PersonProfileView(
            summary=self._summary(person_key),
            profile=profile,
            search_plan=plans[-1] if plans else None,
            latest_bundle=bundles[-1] if bundles else None,
            latest_review_batch_id=review_batches[-1].review_batch_id if review_batches else None,
        )

    def latest_profile(self, person_key: str) -> PersonProfileRevision:
        revisions = self.ledger.list_person_profile_revisions(person_key)
        if not revisions:
            raise LedgerNotFound(person_key)
        return max(revisions, key=lambda item: (item.revision_number, item.created_at))

    def latest_search_plan(self, person_key: str) -> PersonSearchPlanRevision:
        plans = self.ledger.list_person_search_plan_revisions(person_key)
        if not plans:
            raise LedgerNotFound(person_key)
        return max(plans, key=lambda item: (item.revision_number, item.created_at))

    def materialize_profile(self, person_key: str) -> tuple[PersonProfileRevision, bool]:
        key = self._key(person_key)
        person = self.ledger.get_entity(key.entity_id)
        assertions = self._person_assertions(person.entity_id)
        slices = self.ledger.list_person_source_slices(person_key)
        fields: dict[str, list[ProfileFieldItem]] = {name: [] for name in PROFILE_FIELDS}

        if slices:
            first_evidence = self._evidence_for_span(slices[0].evidence_span_id)
            fields["identity"].append(ProfileFieldItem(
                field_item_id=stable_id("pfi", person.entity_id, "identity"),
                field_name="identity",
                label_zh=FIELD_LABELS["identity"],
                value={"canonical_name": person.canonical_name, "aliases": person.aliases},
                evidence=[first_evidence],
                confidence=1.0,
                review_status="pending" if person.metadata.get("strict_initial_review") else "accepted",
                extraction_method="deterministic_assertion_projection",
                known_at=first_evidence.retrieved_at,
                metadata={"entity_id": person.entity_id},
            ))

        seen_sources: set[str] = set()
        for source_slice in slices:
            if source_slice.source_version_id in seen_sources:
                continue
            seen_sources.add(source_slice.source_version_id)
            evidence = self._evidence_for_span(source_slice.evidence_span_id)
            fields["source_documents"].append(ProfileFieldItem(
                field_item_id=stable_id("pfi", person_key, "source", source_slice.source_version_id),
                field_name="source_documents",
                label_zh=FIELD_LABELS["source_documents"],
                value={
                    "source_uri": evidence.source_uri,
                    "source_type": evidence.source_type,
                    "source_version_id": evidence.source_version_id,
                    "content_hash": evidence.content_hash,
                },
                evidence=[evidence],
                confidence=source_slice.confidence,
                review_status="accepted",
                extraction_method="deterministic_assertion_projection",
                known_at=evidence.retrieved_at,
            ))

        for assertion, direction in assertions:
            field_name = self._field_for_assertion(assertion, direction)
            if field_name is None:
                continue
            evidence = [
                self._evidence_for_span(span_id)
                for span_id in self._evidence_ids_for_person(assertion, person.entity_id)
                if self._span_exists(span_id)
            ]
            if not evidence:
                continue
            value = self._assertion_value(assertion, direction)
            status, corrected_value = self._review_state(assertion.assertion_id, assertion)
            if corrected_value is not None:
                value = corrected_value
            extraction = (
                "human_correction"
                if corrected_value is not None
                else "derived_rule"
                if assertion.epistemic_type == "derived"
                else "deterministic_claim_classifier"
                if assertion.predicate_id == "profile_claim"
                else "deterministic_assertion_projection"
            )
            fields[field_name].append(ProfileFieldItem(
                field_item_id=stable_id("pfi", assertion.assertion_id, field_name),
                field_name=field_name,
                label_zh=FIELD_LABELS[field_name],
                value=value,
                assertion_ids=[assertion.assertion_id],
                evidence=evidence,
                confidence=assertion.confidence,
                review_status=status,
                extraction_method=extraction,
                valid_time=assertion.valid_time.model_dump(mode="json"),
                known_at=assertion.transaction_time.ingested_at,
                metadata={
                    "predicate_id": assertion.predicate_id,
                    "direction": direction,
                    "epistemic_type": assertion.epistemic_type,
                },
            ))

        for name in fields:
            deduped = {item.field_item_id: item for item in fields[name]}
            fields[name] = sorted(deduped.values(), key=lambda item: (str(item.value), item.field_item_id))
        missing = {
            name: "当前归属于该人物的原文与 Assertion 中没有可审计信息"
            for name, items in fields.items() if not items
        }
        populated = sum(bool(items) for items in fields.values())
        all_items = [item for items in fields.values() for item in items]
        assertion_ids = sorted({assertion_id for item in all_items for assertion_id in item.assertion_ids})
        signature = hashlib.sha256(
            repr([
                (name, [(item.field_item_id, item.review_status, item.value) for item in items])
                for name, items in fields.items()
            ]).encode("utf-8")
        ).hexdigest()
        revisions = self.ledger.list_person_profile_revisions(person_key)
        latest = max(revisions, key=lambda item: item.revision_number) if revisions else None
        if latest and latest.metadata.get("materialization_hash") == signature:
            return latest, False

        revision = PersonProfileRevision(
            person_key=person_key,
            entity_id=person.entity_id,
            canonical_name=person.canonical_name,
            aliases=person.aliases,
            revision_number=(latest.revision_number + 1) if latest else 1,
            supersedes_revision_id=latest.profile_revision_id if latest else None,
            fields=fields,
            missing_fields=missing,
            source_slice_ids=[item.person_source_slice_id for item in slices],
            assertion_ids=assertion_ids,
            coverage_percent=round(populated / len(PROFILE_FIELDS) * 100, 1),
            accepted_item_count=sum(item.review_status == "accepted" for item in all_items),
            pending_item_count=sum(item.review_status == "pending" for item in all_items),
            conflicted_item_count=sum(item.review_status == "conflicted" for item in all_items),
            metadata={
                "materialization_hash": signature,
                "authority": "rebuildable_projection",
                "person_first": True,
                "cognee_required": False,
            },
        )
        self.ledger.append_person_profile_revision(revision)
        return revision, True

    def ensure_search_plan(self, person_key: str, *, created_by: str = "system") -> tuple[PersonSearchPlanRevision, bool]:
        key = self._key(person_key)
        person = self.ledger.get_entity(key.entity_id)
        profile = self.latest_profile(person_key)
        platform_accounts = self._platform_accounts(profile)
        targets: list[PersonSearchTarget] = []
        urls = [
            str(item.value)
            for item in profile.fields.get("urls", [])
            if isinstance(item.value, str) and item.review_status != "rejected"
        ]
        for index, url in enumerate(sorted(set(urls)), 1):
            channel, route = self._route_for_url(url)
            account = next((item for item in platform_accounts if item.profile_url == url), None)
            fallback_hint = (
                self._first_profile_text(profile, "projects")
                or self._first_profile_text(profile, "papers")
                or self._first_profile_text(profile, "research_topics")
                or self._first_profile_text(profile, "affiliations")
            )
            targets.append(PersonSearchTarget(
                target_id=stable_id("ptarget", person_key, "url", url),
                target_type="known_url",
                channel=channel,
                value=url,
                route=route,
                cadence="daily" if channel in {"github", "arxiv", "x"} else "weekly",
                priority=index,
                reason_zh="档案中已有的直接网址优先刷新，避免开放搜索噪声。",
                fallback_route="qiaomu_generic" if channel == "homepage" else None,
                query_kind="known_account_feed" if account else "known_url_refresh",
                platform_account_key=account.platform_account_key if account else None,
                capture_fields=self._capture_fields(channel),
                checkpoint_fields=self._checkpoint_fields(channel),
                identity_gate_zh=(
                    "账号内容只归属于已登记账号；用户名相似不能替代 account_id 或来源链接核验。"
                    if account else "页面变化只形成 SourceVersion/ChangeSet，不直接推断人物状态。"
                ),
                metadata={
                    "fallback_query": " ".join(
                        part for part in [person.canonical_name, fallback_hint] if part
                    )
                    if channel == "xhs" and account
                    else None,
                },
            ))
        aliases = " OR ".join(f'"{item}"' for item in [person.canonical_name, *person.aliases])
        base_query = aliases or f'"{person.canonical_name}"'
        compact_names = " ".join([person.canonical_name, *person.aliases])
        affiliation_hint = self._first_profile_text(profile, "affiliations") or self._first_profile_text(profile, "education")
        x_identity_query = f"{base_query} {affiliation_hint or ''}".strip()
        xhs_identity_query = f"{compact_names} {affiliation_hint or ''}".strip()
        query_specs: list[tuple[str, str, str, str, str, str]] = []
        if profile.fields.get("papers") or profile.fields.get("education") or profile.fields.get("research_topics"):
            query_specs.append(("arxiv", "arxiv_api", base_query, "weekly", "publication_discovery", "按作者名发现论文候选，仍需机构或主页交叉消歧。"))
        if profile.fields.get("papers") or profile.fields.get("projects"):
            query_specs.append(("github", "github_api", f"{base_query} AI research", "weekly", "repository_discovery", "仓库命中只表示候选；owner、commit author 和主页回链共同决定账号归属。"))
        query_specs.extend([
            ("x", "agent_reach_x", x_identity_query, "weekly", "account_discovery", "display name、handle、bio 机构与主页回链至少两项一致才进入账号审核。"),
            ("x", "agent_reach_x", f"{base_query} (joined OR founded OR paper OR launch OR funding)", "weekly", "person_mentions", "本人帖子与他人提及分开；搜索摘要不能直接写事实。"),
            ("xhs", "xiaohongshu_cli_readonly", xhs_identity_query, "weekly", "account_discovery", "nickname 可变，必须优先保存稳定 user_id；搜索作者只作为候选账号。"),
            ("xhs", "xiaohongshu_cli_readonly", f"{compact_names} 创业 融资 论文 项目", "weekly", "person_mentions", "笔记作者 user_id 与已确认一级账号一致时才视为本人发布。"),
            ("wechat", "qiaomu_wechat_search", f"{base_query} 人物 动态", "weekly", "event_discovery", "公众号只作为报道来源，不参与一级账号判定。"),
            ("news", "exa_news_search", f"{base_query} startup funding research", "daily", "event_discovery", "新闻候选需读取全文并保留来源差异。"),
        ])
        offset = len(targets)
        for index, (channel, route, query, cadence, query_kind, identity_gate) in enumerate(query_specs, 1):
            targets.append(PersonSearchTarget(
                target_id=stable_id("ptarget", person_key, channel, query),
                target_type="channel_query",
                channel=channel,
                value=query,
                route=route,
                cadence=cadence,
                priority=offset + index,
                reason_zh="只在 person_key 已确定后执行；命中结果保持候选，不能反推人物身份。",
                fallback_route="extension_bridge_gui" if channel == "xhs" else None,
                query_kind=query_kind,
                capture_fields=self._capture_fields(channel),
                checkpoint_fields=self._checkpoint_fields(channel),
                identity_gate_zh=identity_gate,
            ))
        signature = hashlib.sha256(
            repr([
                (item.channel, item.value, item.route, item.cadence, item.query_kind, item.platform_account_key)
                for item in targets
            ]).encode()
        ).hexdigest()
        plans = self.ledger.list_person_search_plan_revisions(person_key)
        latest = max(plans, key=lambda item: item.revision_number) if plans else None
        if latest and latest.metadata.get("plan_hash") == signature:
            return latest, False
        plan = PersonSearchPlanRevision(
            person_key=person_key,
            entity_id=key.entity_id,
            revision_number=(latest.revision_number + 1) if latest else 1,
            supersedes_revision_id=latest.search_plan_revision_id if latest else None,
            platform_accounts=platform_accounts,
            targets=targets,
            created_by=created_by,
            metadata={
                "plan_hash": signature,
                "known_url_first": True,
                "primary_account_first": True,
                "xhs_circuit_breaker": "signature_error -> extension_bridge_gui; verification/ip block -> human",
            },
        )
        self.ledger.append_person_search_plan_revision(plan)
        return plan, True

    def replace_search_plan(self, person_key: str, proposed: PersonSearchPlanRevision) -> PersonSearchPlanRevision:
        current = self.latest_search_plan(person_key)
        if proposed.person_key != person_key or proposed.entity_id != current.entity_id:
            raise MemoryValidationError("search plan identity cannot be changed")
        replacement = proposed.model_copy(update={
            "search_plan_revision_id": new_id("splan"),
            "revision_number": current.revision_number + 1,
            "supersedes_revision_id": current.search_plan_revision_id,
            "created_at": utc_now(),
        })
        self.ledger.append_person_search_plan_revision(replacement)
        return replacement

    def record_platform_observations(
        self,
        person_key: str,
        *,
        platform: str,
        results: list[dict[str, Any]],
        source_version_by_uri: dict[str, str],
        query_kind: str | None,
        requested_account_key: str | None,
        observed_at: datetime,
    ) -> PersonSearchPlanRevision:
        """Append account identifiers observed by a person-scoped connector run.

        Search results create candidates only. A direct known-account run may
        enrich an existing record with a stable platform ID, but never upgrades
        ownership to confirmed without the existing human/evidence status.
        """
        if platform not in {"x", "xhs", "github"}:
            return self.latest_search_plan(person_key)
        current = self.latest_search_plan(person_key)
        accounts = list(current.platform_accounts)
        changed = False
        requested = next(
            (item for item in accounts if item.platform_account_key == requested_account_key),
            None,
        )
        observations = results
        if requested is not None:
            # A known-account timeline can contain retweets, quoted posts and
            # organization content. Account identity must come from the
            # separately fetched profile/tracked-account fields, never from the
            # author of an arbitrary timeline item.
            observations = [
                item
                for item in results
                if (
                    item.get("tracked_account_id")
                    or item.get("tracked_account_handle")
                    or item.get("result_kind") == "account_profile"
                    or (
                        platform == "xhs"
                        and str(item.get("author_id") or "") == str(requested.platform_user_id or "")
                    )
                    or (
                        platform == "github"
                        and str(item.get("account_login") or "").casefold()
                        == str(requested.username or "").casefold()
                    )
                )
            ][:1]
        for result in observations:
            username, platform_user_id, profile_url = self._account_identifiers(
                platform,
                result,
                prefer_tracked_identity=requested is not None,
            )
            if not username and not platform_user_id:
                continue
            matched = requested or next(
                (
                    item for item in accounts
                    if item.platform == platform
                    and (
                        (
                            platform_user_id
                            and item.platform_user_id == platform_user_id
                        )
                        or (
                            username
                            and item.username
                            and item.username.casefold() == username.casefold()
                        )
                    )
                ),
                None,
            )
            source_version_id = source_version_by_uri.get(str(result.get("uri") or ""))
            if matched:
                source_ids = sorted({
                    *matched.source_version_ids,
                    *([source_version_id] if source_version_id else []),
                })
                proposed = matched.model_copy(update={
                    "username": username or matched.username,
                    "platform_user_id": platform_user_id or matched.platform_user_id,
                    "profile_url": profile_url or matched.profile_url,
                    "display_name": (
                        result.get("tracked_account_display_name")
                        if requested is not None
                        else result.get("author") or result.get("display_name")
                    ) or matched.display_name,
                    "source_version_ids": source_ids,
                    "last_observed_at": observed_at,
                    "discovery_method": "connector_profile" if requested else matched.discovery_method,
                    "confidence": max(matched.confidence, 0.92 if requested else 0.55),
                    "metadata": {
                        **matched.metadata,
                        "last_query_kind": query_kind,
                        "stable_identifier": "platform_user_id" if platform_user_id else matched.metadata.get("stable_identifier"),
                    },
                })
                if self._account_signature(proposed) != self._account_signature(matched):
                    accounts[accounts.index(matched)] = proposed
                    changed = True
                continue
            identifier = platform_user_id or username or profile_url
            accounts.append(PersonPlatformAccount(
                platform_account_key=stable_id("paccount", person_key, platform, str(identifier).casefold()),
                person_key=person_key,
                entity_id=current.entity_id,
                platform=platform,
                username=username,
                platform_user_id=platform_user_id,
                profile_url=profile_url,
                display_name=result.get("author") or result.get("display_name"),
                identity_status="candidate",
                discovery_method="search_candidate",
                confidence=0.45,
                source_version_ids=[source_version_id] if source_version_id else [],
                first_seen_at=observed_at,
                last_observed_at=observed_at,
                metadata={
                    "last_query_kind": query_kind,
                    "requires_human_link": True,
                    "stable_identifier": "platform_user_id" if platform_user_id else "pending_platform_id",
                },
            ))
            changed = True
        if not changed:
            return current
        replacement = current.model_copy(update={
            "search_plan_revision_id": new_id("splan"),
            "revision_number": current.revision_number + 1,
            "supersedes_revision_id": current.search_plan_revision_id,
            "platform_accounts": accounts,
            "created_at": utc_now(),
            "created_by": "connector-observation",
            "metadata": {
                **current.metadata,
                "account_observation_platform": platform,
                "account_observation_query_kind": query_kind,
            },
        })
        self.ledger.append_person_search_plan_revision(replacement)
        return replacement

    @staticmethod
    def _account_identifiers(
        platform: str,
        result: dict[str, Any],
        *,
        prefer_tracked_identity: bool = False,
    ) -> tuple[str | None, str | None, str]:
        if platform == "x":
            username = (
                result.get("tracked_account_handle")
                if prefer_tracked_identity
                else result.get("author_handle")
            )
            account_id = (
                result.get("tracked_account_id")
                if prefer_tracked_identity
                else result.get("author_id")
            )
            return (
                str(username) if username else None,
                str(account_id) if account_id else None,
                f"https://x.com/{username}" if username else str(result.get("uri") or ""),
            )
        if platform == "xhs":
            account_id = result.get("author_id")
            username = result.get("author")
            return (
                str(username) if username else None,
                str(account_id) if account_id else None,
                f"https://www.xiaohongshu.com/user/profile/{account_id}" if account_id else str(result.get("uri") or ""),
            )
        username = result.get("account_login") or result.get("owner_login")
        account_id = result.get("account_id") or result.get("owner_id")
        return (
            str(username) if username else None,
            str(account_id) if account_id else None,
            f"https://github.com/{username}" if username else str(result.get("uri") or ""),
        )

    @staticmethod
    def _account_signature(account: PersonPlatformAccount) -> tuple[Any, ...]:
        return (
            account.username,
            account.platform_user_id,
            account.profile_url,
            account.display_name,
            tuple(account.source_version_ids),
            account.identity_status,
            account.discovery_method,
            account.confidence,
            json.dumps(account.metadata, ensure_ascii=False, sort_keys=True, default=str),
        )

    def _enqueue_live_target(
        self,
        *,
        person_key: str,
        canonical_name: str,
        target: PersonSearchTarget,
        request: PersonProfileScanRequest,
    ) -> str | None:
        """Append an allowlisted source intent without performing network I/O.

        The connector worker owns execution, throttling and circuit breaking.
        A stable target-level command id makes repeated HTTP/Cron/Feishu
        delivery return the same job instead of revisiting the source.
        """
        from people_intel.connector_jobs import ConnectorJobQueue
        from people_intel.schemas import ConnectorJobEnqueueRequest, ConnectorRunRequest

        if target.channel == "other":
            return None
        command_id = stable_id(
            "profile-source",
            request.command_id,
            person_key,
            target.target_id,
        )
        view = ConnectorJobQueue(self.memory).enqueue(ConnectorJobEnqueueRequest(
            request=ConnectorRunRequest(
                command_id=command_id,
                channel=target.channel,
                query=(
                    str(target.metadata.get("fallback_query") or canonical_name)
                    if target.query_kind == "known_account_feed"
                    else target.value
                ),
                source_uri=target.value if target.target_type == "known_url" else None,
                subject_name=canonical_name,
                trigger=request.trigger,
                max_results=5,
                tracking_key=stable_id("ppsub", person_key, target.target_id),
                person_key=person_key,
                query_kind=target.query_kind,
                platform_account_key=target.platform_account_key,
            ),
            max_attempts=3,
        ))
        return view.job.connector_job_id

    def scan(self, person_key: str, request: PersonProfileScanRequest) -> PersonProfileScanResponse:
        for existing in self.ledger.list_person_profile_scan_runs():
            if existing.command_id == request.command_id:
                bundle = next(
                    (item for item in self.ledger.list_person_update_bundles(person_key) if item.scan_run_id == existing.person_profile_scan_run_id),
                    None,
                )
                return PersonProfileScanResponse(scan_run=existing, bundle=bundle, reused_bundle=True)

        started = utc_now()
        key = self._key(person_key)
        profile = self.latest_profile(person_key)
        plan = self.latest_search_plan(person_key)
        actions = [PersonScanAction(
            sequence=1,
            action_type="read_profile",
            title_zh="读取当前人物档案与搜索设计",
            status="completed",
            input_zh=f"{profile.profile_revision_id} / {plan.search_plan_revision_id}",
            result_zh=f"得到 {len(plan.targets)} 个有顺序的刷新或搜索目标。",
            object_ids=[profile.profile_revision_id, plan.search_plan_revision_id],
        )]
        sequence = 2
        source_by_uri = defaultdict(list)
        for source in self.ledger.list_source_versions():
            source_by_uri[source.source_uri].append(source)
        for target in sorted(plan.targets, key=lambda item: item.priority):
            queued_job_id: str | None = None
            if request.mode == "live":
                queued_job_id = self._enqueue_live_target(
                    person_key=person_key,
                    canonical_name=profile.canonical_name,
                    target=target,
                    request=request,
                )
            if target.target_type == "known_url":
                matches = source_by_uri.get(target.value, [])
                actions.append(PersonScanAction(
                    sequence=sequence,
                    action_type="refresh_known_url",
                    title_zh=f"刷新已知网址 · {target.channel}",
                    status="queued" if queued_job_id else ("completed" if matches else "unconfigured"),
                    input_zh=target.value,
                    result_zh=(
                        f"已将直接网址加入幂等连接器队列；抓取后与账本已有 {len(matches)} 个版本比较 hash。"
                        if queued_job_id
                        else (
                            f"账本已有 {len(matches)} 个版本；本轮以 hash 判断是否变化。"
                            if matches
                            else "当前没有可回放版本；未伪造抓取结果。"
                        )
                    ),
                    object_ids=(
                        [queued_job_id, *[item.source_version_id for item in matches]]
                        if queued_job_id
                        else [item.source_version_id for item in matches]
                    ),
                ))
            else:
                status = "queued" if queued_job_id else "completed"
                actions.append(PersonScanAction(
                    sequence=sequence,
                    action_type="search_channel",
                    title_zh=f"执行人物专属搜索 · {target.channel}",
                    status=status,
                    input_zh=target.value,
                    result_zh=(
                        "回放模式只读取已经归属于该人物的证据，不调用外网。"
                        if request.mode == "replay"
                        else "已写入不可变 ConnectorJob；worker 可重试，单渠道失败不阻断其他来源。"
                    ),
                    object_ids=[queued_job_id] if queued_job_id else [],
                ))
            sequence += 1

        all_slices = self.ledger.list_person_source_slices(person_key)
        previous_bundles = self.ledger.list_person_update_bundles(person_key)
        consumed = {slice_id for item in previous_bundles for slice_id in item.source_slice_ids}
        requested_sources = set(request.changed_source_version_ids)
        new_slices = [
            item for item in all_slices
            if item.person_source_slice_id not in consumed
            and (not requested_sources or item.source_version_id in requested_sources)
        ]
        if not new_slices:
            run = PersonProfileScanRun(
                command_id=request.command_id,
                person_key=person_key,
                entity_id=key.entity_id,
                trigger=request.trigger,
                mode=request.mode,
                status="unchanged",
                actions=actions + [PersonScanAction(
                    sequence=sequence,
                    action_type="merge_person_delta",
                    title_zh="检查逐人增量",
                    status="skipped",
                    input_zh="当前人物自上次 checkpoint 后的 SourceSlice",
                    result_zh="没有新增 slice；不重复抽取、不生成新 bundle。",
                )],
                profile_revision_id=profile.profile_revision_id,
                started_at=started,
                completed_at=utc_now(),
            )
            self.ledger.append_person_profile_scan_run(run)
            return PersonProfileScanResponse(scan_run=run)

        merged = self._bundle_markdown(person_key, new_slices)
        stored = self.memory.object_store.put_text(merged)
        updated_profile, _ = self.materialize_profile(person_key)
        old_items = {item.field_item_id for values in profile.fields.values() for item in values}
        new_items = [item for values in updated_profile.fields.values() for item in values if item.field_item_id not in old_items]
        scan_id = new_id("pscan")
        status = "needs_review" if any(item.review_status in {"pending", "conflicted"} for item in new_items) else "new_information"
        bundle = PersonUpdateBundle(
            person_key=person_key,
            entity_id=key.entity_id,
            scan_run_id=scan_id,
            previous_profile_revision_id=profile.profile_revision_id,
            current_profile_revision_id=updated_profile.profile_revision_id,
            source_slice_ids=[item.person_source_slice_id for item in new_slices],
            source_version_ids=sorted({item.source_version_id for item in new_slices}),
            merged_markdown_ref=stored.object_ref,
            merged_markdown_hash=stored.digest,
            field_deltas=[
                {
                    "operation": "add",
                    "field_name": item.field_name,
                    "field_item_id": item.field_item_id,
                    "review_status": item.review_status,
                    "value": item.value,
                }
                for item in new_items
            ],
            status=status,
            metadata={"cross_person_extraction": False, "source_slice_count": len(new_slices)},
        )
        actions.extend([
            PersonScanAction(
                sequence=sequence,
                action_type="merge_person_delta",
                title_zh="合并该人物的增量原文",
                status="completed",
                input_zh=f"{len(new_slices)} 个仅属于 {person_key} 的 SourceSlice",
                result_zh=f"生成内容寻址 Markdown：{stored.digest[:16]}…",
                object_ids=[item.person_source_slice_id for item in new_slices],
            ),
            PersonScanAction(
                sequence=sequence + 1,
                action_type="extract_field_patch",
                title_zh="提取字段补丁",
                status="completed",
                input_zh="仅使用该人物的增量 Markdown",
                result_zh=f"产生 {len(new_items)} 个新增字段项；高风险项保持 pending。",
                object_ids=[item.field_item_id for item in new_items],
            ),
            PersonScanAction(
                sequence=sequence + 2,
                action_type="materialize_profile",
                title_zh="生成不可变档案版本",
                status="completed",
                input_zh=profile.profile_revision_id,
                result_zh=f"当前投影为 {updated_profile.profile_revision_id}，旧版本未修改。",
                object_ids=[updated_profile.profile_revision_id],
            ),
        ])
        digest = self._create_digest([bundle])
        run = PersonProfileScanRun(
            person_profile_scan_run_id=scan_id,
            command_id=request.command_id,
            person_key=person_key,
            entity_id=key.entity_id,
            trigger=request.trigger,
            mode=request.mode,
            status="partial" if status == "needs_review" else "completed",
            actions=actions,
            bundle_id=bundle.bundle_id,
            profile_revision_id=updated_profile.profile_revision_id,
            digest_batch_id=digest.digest_batch_id,
            started_at=started,
            completed_at=utc_now(),
        )
        self.ledger.append_person_profile_scan_run(run)
        self.ledger.append_person_update_bundle(bundle)
        return PersonProfileScanResponse(scan_run=run, bundle=bundle)

    def bundle_markdown(self, bundle_id: str) -> str:
        bundle = self._bundle(bundle_id)
        return self.memory.object_store.read_text(bundle.merged_markdown_ref)

    def latest_digest(self) -> PersonDigestBatch:
        batches = self.ledger.list_person_digest_batches()
        if not batches:
            raise LedgerNotFound("person digest")
        return max(batches, key=lambda item: item.created_at)

    def digest_markdown(self, digest_batch_id: str) -> str:
        batch = next(
            (item for item in self.ledger.list_person_digest_batches() if item.digest_batch_id == digest_batch_id),
            None,
        )
        if batch is None:
            raise LedgerNotFound(digest_batch_id)
        return self.memory.object_store.read_text(batch.markdown_ref)

    def review_batch(self, review_batch_id: str) -> ProfilePatchReviewBatchView:
        batch = next(
            (item for item in self.ledger.list_profile_patch_review_batches() if item.review_batch_id == review_batch_id),
            None,
        )
        if batch is None:
            raise LedgerNotFound(review_batch_id)
        items = self.ledger.list_profile_patch_review_items(review_batch_id)
        decisions = [
            item for item in self.ledger.list_profile_patch_review_decisions()
            if item.review_item_id in {candidate.review_item_id for candidate in items}
        ]
        latest = {item.review_item_id: item for item in decisions}
        counts = defaultdict(int)
        for item in items:
            counts[latest[item.review_item_id].action if item.review_item_id in latest else "pending"] += 1
        return ProfilePatchReviewBatchView(batch=batch, items=items, decisions=decisions, counts=dict(counts))

    def review_markdown(self, review_batch_id: str) -> str:
        view = self.review_batch(review_batch_id)
        decisions = {item.review_item_id: item for item in view.decisions}
        lines = [
            f"# {view.batch.title_zh}",
            "",
            "逐条事实仍有独立 ID，但可以在同一文档内一次通读、批量确认或输入修正。",
            "",
        ]
        for index, item in enumerate(view.items, 1):
            decision = decisions.get(item.review_item_id)
            lines.extend([
                f"## {index}. {item.title_zh}",
                "",
                f"- 状态：{decision.action if decision else 'pending'}",
                f"- 字段：`{item.field_name}`",
                f"- Assertion：`{item.target_assertion_id or 'profile-only'}`",
                f"- 原因：{item.reason_zh}",
                f"- 问题：{item.question_zh}",
                "",
                f"可回复：`确认第{index}条`、`驳回第{index}条`，或 `第{index}条改为：...`。",
                "",
            ])
        return "\n".join(lines)

    def decide_review(
        self,
        review_batch_id: str,
        request: ProfilePatchReviewDecisionRequest,
    ) -> ProfilePatchReviewBatchView:
        for existing in self.ledger.list_profile_patch_review_decisions():
            if existing.command_id == request.command_id:
                return self.review_batch(review_batch_id)
        item = next(
            (
                candidate for candidate in self.ledger.list_profile_patch_review_items(review_batch_id)
                if candidate.review_item_id == request.review_item_id
            ),
            None,
        )
        if item is None:
            raise LedgerNotFound(request.review_item_id)
        if request.action == "correct" and not request.correction_text:
            raise MemoryValidationError("correct requires correction_text")
        self.ledger.append_profile_patch_review_decision(ProfilePatchReviewDecision(
            command_id=request.command_id,
            review_item_id=request.review_item_id,
            action=request.action,
            actor_id=request.actor_id,
            reason=request.reason,
            correction_text=request.correction_text,
        ))
        self.materialize_profile(item.person_key)
        return self.review_batch(review_batch_id)

    def graph_comparison(self, person_key: str) -> PersonGraphComparison:
        profile = self.latest_profile(person_key)
        key = self._key(person_key)
        graph_ids = {item.assertion_id for item, _ in self._person_assertions(key.entity_id)}
        profile_ids = {
            assertion_id for values in profile.fields.values()
            for item in values for assertion_id in item.assertion_ids
        }
        profile_only = [
            item.field_item_id for values in profile.fields.values()
            for item in values if not item.assertion_ids
        ]
        conflicted = [
            item.field_item_id for values in profile.fields.values()
            for item in values if item.review_status == "conflicted"
        ]
        return PersonGraphComparison(
            person_key=person_key,
            profile_revision_id=profile.profile_revision_id,
            matched_assertion_ids=sorted(graph_ids & profile_ids),
            profile_only_field_item_ids=sorted(profile_only),
            graph_only_assertion_ids=sorted(graph_ids - profile_ids),
            conflicted_field_item_ids=sorted(conflicted),
            explanation_zh=[
                "matched 表示档案字段仍能直接回到图账本 Assertion。",
                "profile_only 仅允许身份标题或人工 correction 等明确标记的投影信息。",
                "graph_only 通常是当前字段 schema 尚未映射的关系，不会因此删除。",
            ],
        )

    def project_cognee(self, person_key: str) -> PersonCogneeProjection:
        slices = self.ledger.list_person_source_slices(person_key)
        content = self._bundle_markdown(person_key, slices)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        dataset = person_dataset_name(person_key)
        for existing in reversed(self.ledger.list_person_cognee_projections(person_key)):
            if existing.dataset_name == dataset and existing.content_hash == digest and existing.status == "completed":
                return existing
        adapter = self.memory.cognify_adapter
        if adapter is None:
            projection = PersonCogneeProjection(
                person_key=person_key,
                dataset_name=dataset,
                source_slice_ids=[item.person_source_slice_id for item in slices],
                content_hash=digest,
                status="unavailable",
                error="Cognee adapter is disabled; the exact profile remains fully operational.",
                metadata={"authority": "projection_only", "person_scoped": True},
            )
            self.ledger.append_person_cognee_projection(projection)
            return projection
        status = adapter.runtime_status()
        if not status.get("ready"):
            projection = PersonCogneeProjection(
                person_key=person_key,
                dataset_name=dataset,
                source_slice_ids=[item.person_source_slice_id for item in slices],
                content_hash=digest,
                status="unconfigured",
                error=status.get("detail_zh"),
                metadata={"authority": "projection_only", "person_scoped": True},
            )
            self.ledger.append_person_cognee_projection(projection)
            return projection
        started = time.perf_counter()
        synthetic = SourceDocumentVersion(
            source_version_id=stable_id("profile_projection", person_key, digest),
            source_uri=f"person-profile-projection://{person_key}/{digest}",
            source_type=SourceType.OTHER,
            media_type="text/markdown",
            content_hash=digest,
            raw_object_ref=f"derived:{digest}",
            fetch_method=FetchMethod.MANUAL,
            rights_scope=RightsScope.RESTRICTED,
            metadata={
                "authority": "projection_input_only",
                "person_key": person_key,
                "source_slice_ids": [item.person_source_slice_id for item in slices],
            },
        )
        try:
            result = adapter.cognify(
                source=synthetic,
                content=content,
                request=CognifyRequest(
                    extractor_version="person-profile-v1",
                    dataset_name=dataset,
                    temporal=True,
                ),
            )
            projection_status = "completed" if result.status == "completed" else "failed"
            error = None
            metadata = result.metadata
        except Exception as exc:
            projection_status = "failed"
            error = f"{type(exc).__name__}: {exc}"
            metadata = {}
        projection = PersonCogneeProjection(
            person_key=person_key,
            dataset_name=dataset,
            source_slice_ids=[item.person_source_slice_id for item in slices],
            content_hash=digest,
            status=projection_status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            error=error,
            metadata={**metadata, "authority": "projection_only", "person_scoped": True},
        )
        self.ledger.append_person_cognee_projection(projection)
        return projection

    def recall_cognee(self, person_key: str, request: PersonCogneeRecallRequest) -> PersonCogneeRecallResponse:
        dataset = person_dataset_name(person_key)
        completed = [
            item for item in self.ledger.list_person_cognee_projections(person_key)
            if item.dataset_name == dataset and item.status == "completed"
        ]
        if not completed:
            raise MemoryValidationError("该人物尚无 completed Cognee 投影；请先运行逐人投影")
        if self.memory.cognify_adapter is None:
            raise MemoryValidationError("Cognee adapter is disabled")
        raw = self.memory.cognify_adapter.recall(
            query_text=request.query_text,
            dataset_name=dataset,
            top_k=request.top_k,
            query_type=request.query_type,
        )
        return PersonCogneeRecallResponse(
            person_key=person_key,
            dataset_name=dataset,
            query_text=request.query_text,
            query_type=raw["query_type"],
            result_count=raw["result_count"],
            results=raw["results"],
            duration_ms=raw["duration_ms"],
        )

    def _ensure_person_key(self, person: Entity) -> PersonKeyRecord:
        for existing in self.ledger.list_person_keys():
            if existing.entity_id == person.entity_id:
                return existing
        base = normalize_person_key(person.canonical_name)
        used = {item.person_key for item in self.ledger.list_person_keys()}
        discriminator = None
        candidate = base
        if candidate in used:
            discriminator = normalize_person_key(str(person.metadata.get("cohort") or person.entity_id[-8:]))
            candidate = f"{base}@{discriminator}"
            counter = 2
            while candidate in used:
                candidate = f"{base}@{discriminator}-{counter}"
                counter += 1
        record = PersonKeyRecord(
            person_key_id=stable_id("pkey", person.entity_id),
            person_key=candidate,
            entity_id=person.entity_id,
            canonical_name=person.canonical_name,
            collision_discriminator=discriminator,
        )
        self.ledger.append_person_key(record)
        return record

    def _ensure_person_slices(self, key: PersonKeyRecord, source_filter: set[str]) -> list[PersonSourceSlice]:
        existing = {
            item.evidence_span_id: item
            for item in self.ledger.list_person_source_slices(key.person_key)
        }
        assertions = self._person_assertions(key.entity_id)
        result = list(existing.values())
        for assertion, _ in assertions:
            for span_id in self._evidence_ids_for_person(assertion, key.entity_id):
                if span_id in existing:
                    continue
                try:
                    span = self.ledger.get_evidence_span(span_id)
                except LedgerNotFound:
                    continue
                if source_filter and span.source_version_id not in source_filter:
                    continue
                item = PersonSourceSlice(
                    person_source_slice_id=stable_id("pslice", key.person_key, span_id),
                    person_key=key.person_key,
                    entity_id=key.entity_id,
                    source_version_id=span.source_version_id,
                    evidence_span_id=span_id,
                    assignment_method="assertion_evidence",
                    confidence=min(1.0, max(0.5, assertion.confidence)),
                    quote_hash=span.quote_hash,
                )
                self.ledger.append_person_source_slice(item)
                existing[span_id] = item
                result.append(item)
        return sorted(result, key=lambda item: (item.source_version_id, item.evidence_span_id))

    def _evidence_ids_for_person(self, assertion: Any, entity_id: str) -> list[str]:
        """Keep a person's retrieval corpus free of coauthor profile leakage.

        A derived coauthor Assertion references both authorship premises. Those
        premises remain available in the review graph, but each person's exact
        profile namespace receives only the premise whose subject is that
        person.
        """
        if assertion.epistemic_type != "derived":
            return assertion.evidence_span_ids
        inputs = self.ledger.get_derived_inputs(assertion.assertion_id)
        if inputs is None:
            return assertion.evidence_span_ids
        selected: list[str] = []
        for assertion_id in inputs.input_assertion_ids:
            premise = self.ledger.get_assertion(assertion_id)
            if premise.subject_entity_id == entity_id:
                selected.extend(premise.evidence_span_ids)
        return list(dict.fromkeys(selected))

    def _person_assertions(self, entity_id: str) -> list[tuple[Any, str]]:
        result: list[tuple[Any, str]] = []
        for assertion in self.ledger.list_assertions():
            if assertion.subject_entity_id == entity_id:
                result.append((assertion, "outgoing"))
            elif assertion.object.entity_id == entity_id and assertion.predicate_id in {
                "account_owned_by", "coauthored_with", "same_as", "possibly_same_as",
            }:
                result.append((assertion, "incoming"))
        return result

    def _field_for_assertion(self, assertion: Any, direction: str) -> str | None:
        predicate = assertion.predicate_id
        if predicate == "account_owned_by" and direction == "incoming":
            return "urls"
        if predicate in {"authored", "published"}:
            return "papers"
        if predicate == "awarded":
            return "awards"
        if predicate in {"studied_at", "advised_by"}:
            return "education"
        if predicate in {"holds_role", "affiliated_with", "member_of", "worked_at", "joined", "left"}:
            return "affiliations"
        if predicate in {"maintains", "contributed_to", "launched"}:
            return "projects"
        if predicate in {"founded", "cofounded", "raised_funding", "invested_in", "spun_out_from"}:
            return "career_and_funding"
        if predicate == "interested_in":
            return "research_topics"
        if predicate in {"coauthored_with", "collaborated_with"}:
            return "relationships"
        if predicate == "profile_claim":
            return self._classify_claim(str(assertion.object.value))
        return None

    @staticmethod
    def _classify_claim(value: str) -> str:
        lowered = value.casefold()
        if re.search(r"博士|硕士|本科|学位|phd|student|毕业|导师|supervis", lowered):
            return "education"
        if re.search(r"award|奖|fellow|scholar|荣誉", lowered):
            return "awards"
        if re.search(r"论文|paper|publication|arxiv|发表", lowered):
            return "papers"
        if re.search(r"github|代码|开源|项目|project|repository|repo|产品", lowered):
            return "projects"
        if re.search(r"研究方向|研究兴趣|research|focus|方向", lowered):
            return "research_topics"
        if re.search(r"融资|创业|创办|founded|startup|funding|任职|加入|离开", lowered):
            return "career_and_funding"
        if re.search(r"大学|学院|公司|实验室|研究院|university|institute|lab|inc\\.?|corp", lowered):
            return "affiliations"
        return "supplementary_information"

    def _assertion_value(self, assertion: Any, direction: str) -> Any:
        if assertion.object.entity_id:
            entity = self.ledger.get_entity(
                assertion.subject_entity_id if direction == "incoming" else assertion.object.entity_id
            )
            return entity.metadata.get("url") or entity.canonical_name
        return assertion.object.value

    def _review_state(self, assertion_id: str, assertion: Any) -> tuple[str, Any | None]:
        review_items = {
            item.target_assertion_id: item
            for item in self.ledger.list_profile_patch_review_items()
            if item.target_assertion_id
        }
        if assertion_id in review_items:
            decisions = self.ledger.list_profile_patch_review_decisions(review_items[assertion_id].review_item_id)
            if decisions:
                latest = max(decisions, key=lambda item: item.created_at)
                status = {
                    "confirm": "accepted",
                    "reject": "rejected",
                    "correct": "accepted",
                    "mark_ambiguous": "conflicted",
                }[latest.action]
                return status, latest.correction_text if latest.action == "correct" else None
        initial_items = {
            item.target_assertion_id: item
            for item in self.ledger.list_initial_review_items()
        }
        if assertion_id in initial_items:
            decisions = self.ledger.list_initial_review_decisions(initial_items[assertion_id].review_item_id)
            if decisions:
                latest = max(decisions, key=lambda item: item.created_at)
                return {
                    "confirm": "accepted",
                    "reject": "rejected",
                    "correct": "accepted",
                    "mark_ambiguous": "conflicted",
                }[latest.action], None
            return "pending", None
        annotations = self.ledger.list_annotations(assertion_id)
        if annotations:
            latest = max(annotations, key=lambda item: item.created_at)
            return {
                "confirm": "accepted",
                "reject": "rejected",
                "correct": "accepted",
                "supplement": "accepted",
                "mark_ambiguous": "conflicted",
                "split_identity": "conflicted",
                "link_identity": "accepted",
            }[latest.action], None
        if assertion.status == "conflicted":
            return "conflicted", None
        if assertion.epistemic_type == "derived" or assertion.review_required:
            return "pending", None
        if assertion.confidence >= 0.90 and assertion.evidence_span_ids:
            return "accepted", None
        return "pending", None

    def _evidence_for_span(self, span_id: str) -> ProfileEvidenceRef:
        span = self.ledger.get_evidence_span(span_id)
        source = self.ledger.get_source_version(span.source_version_id)
        return ProfileEvidenceRef(
            source_version_id=source.source_version_id,
            evidence_span_id=span.evidence_span_id,
            source_uri=source.source_uri,
            source_type=source.source_type,
            quote=span.quote,
            content_hash=source.content_hash,
            retrieved_at=source.retrieved_at,
            published_at=source.published_at,
        )

    def _span_exists(self, span_id: str) -> bool:
        try:
            self.ledger.get_evidence_span(span_id)
            return True
        except LedgerNotFound:
            return False

    def _key(self, person_key: str) -> PersonKeyRecord:
        record = next((item for item in self.ledger.list_person_keys() if item.person_key == person_key), None)
        if record is None:
            raise LedgerNotFound(person_key)
        return record

    def _summary(self, person_key: str) -> PersonProfileSummary:
        key = self._key(person_key)
        profile = self.latest_profile(person_key)
        plans = self.ledger.list_person_search_plan_revisions(person_key)
        bundles = self.ledger.list_person_update_bundles(person_key)
        cadence = "daily" if any(item.metadata.get("tier") == "A" for item in [profile]) else "weekly"
        next_scan = profile.created_at + (timedelta(days=1) if cadence == "daily" else timedelta(days=7))
        return PersonProfileSummary(
            person_key=person_key,
            entity_id=key.entity_id,
            canonical_name=profile.canonical_name,
            aliases=profile.aliases,
            profile_revision_id=profile.profile_revision_id,
            search_plan_revision_id=plans[-1].search_plan_revision_id if plans else None,
            coverage_percent=profile.coverage_percent,
            source_count=len({item.source_version_id for item in self.ledger.list_person_source_slices(person_key)}),
            accepted_item_count=profile.accepted_item_count,
            pending_item_count=profile.pending_item_count,
            conflicted_item_count=profile.conflicted_item_count,
            next_scan_at=next_scan,
            last_bundle_id=bundles[-1].bundle_id if bundles else None,
            cognee_namespace=person_dataset_name(person_key),
        )

    def _platform_accounts(self, profile: PersonProfileRevision) -> list[PersonPlatformAccount]:
        accounts: list[PersonPlatformAccount] = []
        for item in profile.fields.get("urls", []):
            if not isinstance(item.value, str) or item.review_status == "rejected":
                continue
            parsed = urlparse(item.value)
            host = parsed.netloc.casefold()
            parts = [part for part in parsed.path.split("/") if part]
            platform: str | None = None
            username: str | None = None
            platform_user_id: str | None = None
            if host in {"github.com", "www.github.com"} and len(parts) == 1:
                platform, username = "github", parts[0]
            elif host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"} and parts:
                if parts[0] not in {"home", "search", "i"}:
                    platform, username = "x", parts[0]
            elif host.endswith("xiaohongshu.com") and len(parts) >= 3 and parts[:2] == ["user", "profile"]:
                platform, platform_user_id = "xhs", parts[2]
            if platform is None:
                continue
            identifier = platform_user_id or username or item.value
            evidence = [reference for reference in item.evidence]
            accounts.append(PersonPlatformAccount(
                platform_account_key=stable_id("paccount", profile.person_key, platform, identifier.casefold()),
                person_key=profile.person_key,
                entity_id=profile.entity_id,
                platform=platform,
                username=username,
                platform_user_id=platform_user_id,
                profile_url=item.value,
                display_name=profile.canonical_name,
                identity_status={
                    "accepted": "confirmed",
                    "pending": "pending_review",
                    "conflicted": "conflicted",
                }.get(item.review_status, "candidate"),
                discovery_method="source_url",
                confidence=item.confidence,
                source_version_ids=sorted({reference.source_version_id for reference in evidence}),
                evidence_span_ids=sorted({reference.evidence_span_id for reference in evidence}),
                first_seen_at=min(
                    (reference.retrieved_at for reference in evidence),
                    default=profile.created_at,
                ),
                last_observed_at=max(
                    (reference.retrieved_at for reference in evidence),
                    default=profile.created_at,
                ),
                metadata={
                    "username_mutable": platform in {"x", "github"},
                    "stable_identifier": "platform_user_id" if platform_user_id else "pending_platform_id",
                    "source_field_item_id": item.field_item_id,
                },
            ))
        return accounts

    @staticmethod
    def _first_profile_text(profile: PersonProfileRevision, field_name: str) -> str | None:
        for item in profile.fields.get(field_name, []):
            if isinstance(item.value, str) and item.value.strip():
                return item.value.strip()[:120]
        return None

    @staticmethod
    def _capture_fields(channel: str) -> list[str]:
        return {
            "x": ["platform_user_id", "username", "display_name", "bio", "post_id", "created_at", "metrics"],
            "xhs": ["user_id", "nickname", "note_id", "published_at", "last_update_time", "metrics"],
            "github": ["database_id", "node_id", "login", "repo_node_id", "full_name", "pushed_at", "default_branch_sha"],
            "arxiv": ["paper_id", "authors", "published_at", "updated_at"],
            "homepage": ["source_hash", "structured_diff"],
        }.get(channel, ["uri", "published_at", "author", "content_hash"])

    @staticmethod
    def _checkpoint_fields(channel: str) -> list[str]:
        return {
            "x": ["newest_post_id", "newest_created_at"],
            "xhs": ["newest_note_id", "newest_last_update_time", "pagination_cursor"],
            "github": ["account_database_id", "repo_node_id", "pushed_at", "default_branch_sha"],
            "arxiv": ["newest_paper_id", "newest_updated_at"],
            "homepage": ["content_hash"],
        }.get(channel, ["latest_published_at", "content_hash"])

    @staticmethod
    def _route_for_url(url: str) -> tuple[str, str]:
        host = urlparse(url).netloc.casefold()
        if "github.com" in host:
            return "github", "github_api"
        if "arxiv.org" in host:
            return "arxiv", "arxiv_api"
        if host.endswith("x.com") or "twitter.com" in host:
            return "x", "agent_reach_x"
        if "xiaohongshu.com" in host:
            return "xhs", "xiaohongshu_cli_readonly"
        return "homepage", "qiaomu_generic"

    def _bundle_markdown(self, person_key: str, slices: list[PersonSourceSlice]) -> str:
        key = self._key(person_key)
        lines = [
            f"# {key.canonical_name} · 逐人增量原文包",
            "",
            f"- person_key: `{person_key}`",
            f"- entity_id: `{key.entity_id}`",
            "- extraction_scope: `this_person_only`",
            "",
        ]
        for index, item in enumerate(sorted(slices, key=lambda value: (value.source_version_id, value.evidence_span_id)), 1):
            span = self.ledger.get_evidence_span(item.evidence_span_id)
            source = self.ledger.get_source_version(item.source_version_id)
            lines.extend([
                f"## 来源 {index}",
                "",
                f"- source_version_id: `{source.source_version_id}`",
                f"- evidence_span_id: `{span.evidence_span_id}`",
                f"- channel: `{source.source_type}`",
                f"- URL: {source.source_uri}",
                f"- retrieved_at: `{source.retrieved_at.isoformat()}`",
                f"- published_at: `{source.published_at.isoformat() if source.published_at else 'unknown'}`",
                f"- content_hash: `{source.content_hash}`",
                f"- rights_scope: `{source.rights_scope}`",
                "",
                "### 原文",
                "",
                span.quote,
                "",
            ])
        return "\n".join(lines)

    def _create_digest(self, bundles: list[PersonUpdateBundle]) -> PersonDigestBatch:
        lines = [
            "# 人物动态逐人增量汇总",
            "",
            "本汇总在每个人独立完成抽取后拼接；禁止把本文件重新用于跨人物事实抽取。",
            "",
        ]
        for bundle in bundles:
            profile = self.latest_profile(bundle.person_key)
            lines.extend([
                f"## {profile.canonical_name}",
                "",
                f"- person_key: `{bundle.person_key}`",
                f"- bundle_id: `{bundle.bundle_id}`",
                f"- 状态：`{bundle.status}`",
                f"- 新增来源版本：{len(bundle.source_version_ids)}",
                f"- 字段变化：{len(bundle.field_deltas)}",
                "",
            ])
        stored = self.memory.object_store.put_text("\n".join(lines))
        batch = PersonDigestBatch(
            bundle_ids=[item.bundle_id for item in bundles],
            person_keys=[item.person_key for item in bundles],
            markdown_ref=stored.object_ref,
            markdown_hash=stored.digest,
        )
        self.ledger.append_person_digest_batch(batch)
        return batch

    def _bundle(self, bundle_id: str) -> PersonUpdateBundle:
        bundle = next((item for item in self.ledger.list_person_update_bundles() if item.bundle_id == bundle_id), None)
        if bundle is None:
            raise LedgerNotFound(bundle_id)
        return bundle
