from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
from pathlib import Path

from people_intel.ledger import LedgerNotFound
from people_intel.schemas import (
    Assertion,
    AssertionObject,
    DeriveRequest,
    Entity,
    EntityType,
    EpistemicType,
    EvidenceSpan,
    FetchMethod,
    InitialReviewBatchView,
    IntervalType,
    LiteralDatatype,
    ObjectKind,
    SourceIngestionRequest,
    SourceType,
    TemporalExtent,
    TransactionTime,
)
from people_intel.service import MemoryValidationError, TemporalMemoryService


URL_RE = re.compile(r"https?://[^\s）)；;，,]+")
EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})(?![\w.-])")


@dataclass
class ImportedPerson:
    entity_id: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    assertion_ids: list[str] = field(default_factory=list)


@dataclass
class ImportReport:
    source_version_id: str
    version_status: str
    review_batch_id: str
    people: list[ImportedPerson]
    paper_entity_ids: list[str]
    identity_entity_ids: list[str]
    assertion_ids: list[str]
    derived_assertion_ids: list[str]
    evidence_span_ids: list[str]
    counts: dict[str, int]


class IcmlPeopleMarkdownImporter:
    """Deterministic, resumable importer for the supplied ICML people dossier.

    The importer deliberately separates three things:

    * every profile paragraph becomes an EvidenceSpan, so no source knowledge is
      lost merely because v1 does not yet have a safe semantic rule for it;
    * explicit authorship, account ownership and paper award claims become
      review-gated direct Assertions;
    * coauthor relations are Derived Assertions whose two authorship inputs and
      inference rule remain inspectable and independently reviewable.

    Stable IDs make an interrupted or repeated import safe to resume. Existing
    immutable objects are verified and reused rather than overwritten.
    """

    IMPORTER_VERSION = "icml-markdown-v2"
    COAUTHOR_RULE_ID = "shared-paper-implies-coauthor"
    COAUTHOR_RULE_VERSION = "1.0"

    def __init__(self, service: TemporalMemoryService):
        self.service = service

    def import_path(self, path: str | Path, *, observed_at: datetime, created_by: str = "local-import") -> ImportReport:
        source_path = Path(path).resolve()
        return self.import_text(
            source_path.read_text(encoding="utf-8"),
            source_uri=source_path.as_uri(),
            observed_at=observed_at,
            created_by=created_by,
        )

    def import_text(
        self,
        text: str,
        *,
        source_uri: str,
        observed_at: datetime,
        created_by: str = "local-import",
        cohort_id: str = "icml-2026-award-contributors",
        media_type: str = "text/markdown",
        normalized_markdown: str | None = None,
    ) -> ImportReport:
        normalized_text = normalized_markdown if normalized_markdown is not None else text
        records = self._parse(normalized_text)
        if not records:
            raise MemoryValidationError("ICML dossier contains no level-three person records")
        if any(item["paper_title"] == "Unknown paper" for item in records):
            raise MemoryValidationError("each person record must be nested below a paper heading")

        response = self.service.ingest_source(
            SourceIngestionRequest(
                source_uri=source_uri,
                source_type=SourceType.FILE if source_uri.startswith("file:") else SourceType.FEISHU,
                media_type=media_type,
                content=text,
                normalized_markdown=normalized_text,
                retrieved_at=observed_at,
                fetch_method=FetchMethod.UPLOAD if source_uri.startswith("file:") else FetchMethod.FEISHU,
                metadata={"cohort": cohort_id, "strict_initial_review": True, "importer": self.IMPORTER_VERSION},
            )
        )
        source = self.service.get_source_version(response.source_version_id)
        paper_entities: dict[str, Entity] = {}
        authored_by_paper: dict[str, list[tuple[Entity, Assertion]]] = {}
        identity_ids: list[str] = []
        direct_assertion_ids: list[str] = []
        evidence_ids: list[str] = []
        imported: list[ImportedPerson] = []

        for record in records:
            paper_title = record["paper_title"]
            paper = paper_entities.get(paper_title)
            if paper is None:
                paper = self._ensure_entity(
                    Entity(
                        entity_id=self._stable_id("ent", "Paper", paper_title),
                        entity_type=EntityType.PAPER,
                        canonical_name=paper_title,
                        metadata={"cohort": "ICML 2026"},
                    )
                )
                paper_entities[paper_title] = paper
                authored_by_paper[paper_title] = []

            person = self._ensure_entity(
                Entity(
                    entity_id=self._stable_id("ent", "Person", record["canonical_name"]),
                    entity_type=EntityType.PERSON,
                    canonical_name=record["canonical_name"],
                    aliases=record["aliases"],
                    metadata={"cohort": cohort_id, "strict_initial_review": True},
                )
            )
            block_span = self._ensure_span(
                EvidenceSpan(
                    evidence_span_id=self._stable_id("span", source.source_version_id, person.entity_id, "profile-block"),
                    source_version_id=source.source_version_id,
                    locator_type="character",
                    locator={"start": record["start"], "end": record["end"], "heading": record["heading"], "fragment_kind": "profile_block"},
                    quote=record["content"],
                    quote_hash=hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
                    normalized_version_ref=source.normalized_markdown_ref,
                )
            )
            evidence_ids.append(block_span.evidence_span_id)
            profile_claim_ids: list[str] = []
            for paragraph_index, fragment in enumerate(self._paragraph_fragments(record["content"], record["start"]), 1):
                fragment_span = self._ensure_span(
                    EvidenceSpan(
                        evidence_span_id=self._stable_id("span", source.source_version_id, person.entity_id, "paragraph", str(paragraph_index)),
                        source_version_id=source.source_version_id,
                        locator_type="paragraph",
                        locator={"paragraph": paragraph_index, "start": fragment["start"], "end": fragment["end"], "heading": record["heading"], "fragment_kind": "profile_paragraph"},
                        quote=fragment["quote"],
                        quote_hash=hashlib.sha256(fragment["quote"].encode("utf-8")).hexdigest(),
                        normalized_version_ref=source.normalized_markdown_ref,
                    )
                )
                evidence_ids.append(fragment_span.evidence_span_id)

            # Every biographical proposition is reviewable even when v1 does
            # not yet have a safe rule for mapping it to a domain relation.
            # This prevents "stored somewhere in a paragraph" from being
            # confused with "split into auditable knowledge".
            for statement_index, statement in enumerate(self._statement_fragments(record["content"], record["start"]), 1):
                statement_span = self._ensure_span(EvidenceSpan(
                    evidence_span_id=self._stable_id("span", source.source_version_id, person.entity_id, "statement", str(statement_index)),
                    source_version_id=source.source_version_id,
                    locator_type="character",
                    locator={
                        "start": statement["start"], "end": statement["end"],
                        "heading": record["heading"], "fragment_kind": "profile_statement",
                        "statement": statement_index,
                    },
                    quote=statement["quote"],
                    quote_hash=hashlib.sha256(statement["quote"].encode("utf-8")).hexdigest(),
                    normalized_version_ref=source.normalized_markdown_ref,
                ))
                evidence_ids.append(statement_span.evidence_span_id)
                claim = self._ensure_assertion(Assertion(
                    assertion_id=self._stable_id("ast", source.source_version_id, person.entity_id, "profile_claim", statement["quote"]),
                    subject_entity_id=person.entity_id,
                    predicate_id="profile_claim",
                    object=AssertionObject(
                        kind=ObjectKind.LITERAL,
                        datatype=LiteralDatatype.STRING,
                        value=statement["quote"],
                    ),
                    epistemic_type=EpistemicType.REPORTED,
                    valid_time=TemporalExtent(interval_type=IntervalType.UNKNOWN),
                    transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                    evidence_span_ids=[statement_span.evidence_span_id],
                    confidence=0.65,
                    review_required=True,
                    metadata={
                        "importer": self.IMPORTER_VERSION,
                        "fact_kind": "direct_fact",
                        "claim_scope": "unmapped_profile_statement",
                        "extraction_rule": "sentence-boundary-v1",
                        "source_version_id": source.source_version_id,
                    },
                ))
                direct_assertion_ids.append(claim.assertion_id)
                profile_claim_ids.append(claim.assertion_id)

            authored = self._ensure_assertion(
                Assertion(
                    assertion_id=self._stable_id("ast", source.source_version_id, person.entity_id, "authored", paper.entity_id),
                    subject_entity_id=person.entity_id,
                    predicate_id="authored",
                    object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=paper.entity_id),
                    epistemic_type=EpistemicType.REPORTED,
                    valid_time=TemporalExtent(interval_type=IntervalType.UNKNOWN),
                    transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                    evidence_span_ids=[block_span.evidence_span_id],
                    confidence=0.90,
                    review_required=True,
                    metadata={"importer": self.IMPORTER_VERSION, "fact_kind": "direct_fact", "source_version_id": source.source_version_id},
                )
            )
            direct_assertion_ids.append(authored.assertion_id)
            authored_by_paper[paper_title].append((person, authored))
            person_result = ImportedPerson(
                entity_id=person.entity_id,
                canonical_name=person.canonical_name,
                aliases=person.aliases,
                assertion_ids=[*profile_claim_ids, authored.assertion_id],
            )

            accounts = sorted(set(record["urls"] + [f"mailto:{value}" for value in record["emails"]]))
            for account_value in accounts:
                account = self._ensure_entity(
                    Entity(
                        entity_id=self._stable_id("ent", "IdentityAccount", account_value.lower()),
                        entity_type=EntityType.IDENTITY_ACCOUNT,
                        canonical_name=account_value,
                        metadata={"url": account_value, "source_person_name": person.canonical_name, "account_kind": "email" if account_value.startswith("mailto:") else "url"},
                    )
                )
                identity_ids.append(account.entity_id)
                owned = self._ensure_assertion(
                    Assertion(
                        assertion_id=self._stable_id("ast", source.source_version_id, account.entity_id, "account_owned_by", person.entity_id),
                        subject_entity_id=account.entity_id,
                        predicate_id="account_owned_by",
                        object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=person.entity_id),
                        epistemic_type=EpistemicType.REPORTED,
                        valid_time=TemporalExtent(interval_type=IntervalType.OPEN_END),
                        transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                        evidence_span_ids=[block_span.evidence_span_id],
                        confidence=0.75,
                        review_required=True,
                        metadata={"importer": self.IMPORTER_VERSION, "fact_kind": "direct_fact", "identity_url": account_value, "source_version_id": source.source_version_id},
                    )
                )
                direct_assertion_ids.append(owned.assertion_id)
                person_result.assertion_ids.append(owned.assertion_id)
            imported.append(person_result)

        # The section label is explicit evidence that the paper received an
        # award or nomination. Keep it review-gated like every initial fact.
        seen_awards: set[str] = set()
        for record in records:
            paper_title = record["paper_title"]
            if paper_title in seen_awards:
                continue
            seen_awards.add(paper_title)
            award_name = self._award_name(record["paper_heading"])
            if not award_name:
                continue
            paper = paper_entities[paper_title]
            award = self._ensure_entity(Entity(
                entity_id=self._stable_id("ent", "Award", award_name),
                entity_type=EntityType.AWARD,
                canonical_name=award_name,
                metadata={"conference": "ICML", "year": 2026},
            ))
            heading_quote = record["paper_heading_line"]
            section_span = self._ensure_span(EvidenceSpan(
                evidence_span_id=self._stable_id("span", source.source_version_id, paper.entity_id, "paper-heading"),
                source_version_id=source.source_version_id,
                locator_type="character",
                locator={"start": record["paper_heading_start"], "end": record["paper_heading_end"], "fragment_kind": "paper_award_heading"},
                quote=heading_quote,
                quote_hash=hashlib.sha256(heading_quote.encode("utf-8")).hexdigest(),
                normalized_version_ref=source.normalized_markdown_ref,
            ))
            evidence_ids.append(section_span.evidence_span_id)
            awarded = self._ensure_assertion(Assertion(
                assertion_id=self._stable_id("ast", source.source_version_id, paper.entity_id, "awarded", award.entity_id),
                subject_entity_id=paper.entity_id,
                predicate_id="awarded",
                object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=award.entity_id),
                epistemic_type=EpistemicType.REPORTED,
                valid_time=TemporalExtent(interval_type=IntervalType.UNKNOWN),
                transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                evidence_span_ids=[section_span.evidence_span_id],
                confidence=0.88,
                review_required=True,
                metadata={"importer": self.IMPORTER_VERSION, "fact_kind": "direct_fact", "source_version_id": source.source_version_id},
            ))
            direct_assertion_ids.append(awarded.assertion_id)

        derived_assertion_ids: list[str] = []
        for paper_title, authors in authored_by_paper.items():
            paper = paper_entities[paper_title]
            for (left_person, left_authored), (right_person, right_authored) in combinations(sorted(authors, key=lambda value: value[0].entity_id), 2):
                assertion_id = self._stable_id("ast", source.source_version_id, "coauthor", left_person.entity_id, right_person.entity_id, paper.entity_id)
                try:
                    derived = self.service.ledger.get_assertion(assertion_id)
                except LedgerNotFound:
                    evidence_span_ids = list(dict.fromkeys([*left_authored.evidence_span_ids, *right_authored.evidence_span_ids]))
                    derived = self.service.derive(DeriveRequest(
                        assertion=Assertion(
                            assertion_id=assertion_id,
                            subject_entity_id=left_person.entity_id,
                            predicate_id="coauthored_with",
                            object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=right_person.entity_id),
                            valid_time=TemporalExtent(interval_type=IntervalType.UNKNOWN),
                            transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                            evidence_span_ids=evidence_span_ids,
                            confidence=min(left_authored.confidence, right_authored.confidence) * 0.95,
                            review_required=True,
                            metadata={
                                "importer": self.IMPORTER_VERSION,
                                "fact_kind": "derived_inference",
                                "paper_entity_id": paper.entity_id,
                                "paper_title": paper_title,
                                "inference_explanation_zh": f"两人都被原文列为《{paper_title}》作者，因此推定为共同作者。",
                                "group_key": f"coauthor:{paper.entity_id}",
                            },
                        ),
                        input_assertion_ids=[left_authored.assertion_id, right_authored.assertion_id],
                        rule_id=self.COAUTHOR_RULE_ID,
                        rule_version=self.COAUTHOR_RULE_VERSION,
                    ))
                derived_assertion_ids.append(derived.assertion_id)

        all_assertion_ids = list(dict.fromkeys([*direct_assertion_ids, *derived_assertion_ids]))
        review = self.service.create_initial_review_batch(
            source_kind="file_import",
            source_ref=f"initial-import:{source.source_version_id}",
            title_zh="ICML 2026 人物知识首次建档审核",
            assertion_ids=all_assertion_ids,
            source_version_ids=[source.source_version_id],
            created_by=created_by,
        )
        unique_evidence = list(dict.fromkeys(evidence_ids))
        unique_identity = list(dict.fromkeys(identity_ids))
        return ImportReport(
            source_version_id=source.source_version_id,
            version_status=response.version_status,
            review_batch_id=review.batch.review_batch_id,
            people=imported,
            paper_entity_ids=[item.entity_id for item in paper_entities.values()],
            identity_entity_ids=unique_identity,
            assertion_ids=all_assertion_ids,
            derived_assertion_ids=derived_assertion_ids,
            evidence_span_ids=unique_evidence,
            counts={
                "people": len(imported),
                "papers": len(paper_entities),
                "identity_accounts": len(unique_identity),
                "direct_assertions": len(direct_assertion_ids),
                "derived_coauthor_assertions": len(derived_assertion_ids),
                "evidence_spans": len(unique_evidence),
                "review_items": len(review.items),
            },
        )

    def _ensure_entity(self, entity: Entity) -> Entity:
        try:
            existing = self.service.ledger.get_entity(entity.entity_id)
        except LedgerNotFound:
            return self.service.create_entity(entity)
        if existing.entity_type != entity.entity_type or existing.canonical_name != entity.canonical_name:
            raise MemoryValidationError(f"stable entity id collision: {entity.entity_id}")
        return existing

    def _ensure_span(self, span: EvidenceSpan) -> EvidenceSpan:
        try:
            existing = self.service.ledger.get_evidence_span(span.evidence_span_id)
        except LedgerNotFound:
            self.service.ledger.append_evidence_span(span)
            return span
        if existing.source_version_id != span.source_version_id or existing.quote_hash != span.quote_hash:
            raise MemoryValidationError(f"stable evidence id collision: {span.evidence_span_id}")
        return existing

    def _ensure_assertion(self, assertion: Assertion) -> Assertion:
        try:
            existing = self.service.ledger.get_assertion(assertion.assertion_id)
        except LedgerNotFound:
            return self.service.create_assertion(assertion)
        if (
            existing.subject_entity_id != assertion.subject_entity_id
            or existing.predicate_id != assertion.predicate_id
            or existing.object != assertion.object
        ):
            raise MemoryValidationError(f"stable assertion id collision: {assertion.assertion_id}")
        return existing

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:32]
        return f"{prefix}_{digest}"

    @staticmethod
    def _parse(text: str) -> list[dict]:
        heading_matches = list(re.finditer(r"^##\s+(.+)$|^###\s+(.+)$", text, flags=re.MULTILINE))
        paper_context = {
            "title": "Unknown paper", "heading": "", "line": "", "start": 0, "end": 0,
        }
        records: list[dict] = []
        for index, match in enumerate(heading_matches):
            level2, level3 = match.groups()
            if level2 is not None:
                heading = IcmlPeopleMarkdownImporter._unescape(level2.strip())
                paper_context = {
                    "title": IcmlPeopleMarkdownImporter._paper_title(heading),
                    "heading": heading,
                    "line": match.group(0),
                    "start": match.start(),
                    "end": match.end(),
                }
                continue
            heading = IcmlPeopleMarkdownImporter._unescape(level3.strip())
            start = match.start()
            end = heading_matches[index + 1].start() if index + 1 < len(heading_matches) else len(text)
            content = text[start:end].strip()
            canonical, aliases = IcmlPeopleMarkdownImporter._person_names(heading)
            unescaped_content = IcmlPeopleMarkdownImporter._unescape(content)
            urls = sorted({IcmlPeopleMarkdownImporter._clean_url(url) for url in URL_RE.findall(content)})
            emails = sorted({value.lower() for value in EMAIL_RE.findall(unescaped_content)})
            records.append({
                "heading": heading,
                "canonical_name": canonical,
                "aliases": aliases,
                "paper_title": paper_context["title"],
                "paper_heading": paper_context["heading"],
                "paper_heading_line": paper_context["line"],
                "paper_heading_start": paper_context["start"],
                "paper_heading_end": paper_context["end"],
                "urls": [url for url in urls if url],
                "emails": emails,
                "start": start,
                "end": end,
                "content": content,
            })
        return records

    @staticmethod
    def _paragraph_fragments(content: str, absolute_start: int) -> list[dict[str, int | str]]:
        fragments: list[dict[str, int | str]] = []
        for match in re.finditer(r"(?ms)(?:^|\n\s*\n)([^\n].*?)(?=\n\s*\n|\Z)", content):
            quote = match.group(1).strip()
            if not quote or quote.startswith("### ") and "\n" not in quote:
                continue
            relative = content.find(quote, match.start())
            fragments.append({"quote": quote, "start": absolute_start + relative, "end": absolute_start + relative + len(quote)})
        return fragments

    @staticmethod
    def _statement_fragments(
        content: str,
        absolute_start: int,
        *,
        exclude_identity_only: bool = False,
    ) -> list[dict[str, int | str]]:
        """Split biography prose into exact, independently reviewable claims.

        Sentence splitting preserves the source punctuation and locator. Some
        source contracts intentionally review contact prose as a profile claim;
        importers that separately normalize every link can opt out explicitly.
        """
        body = re.sub(r"^#{2,3}\s+[^\n]+\n+", "", content).strip()
        body_offset = content.find(body)
        claims: list[dict[str, int | str]] = []
        for paragraph in re.split(r"\n\s*\n", body):
            paragraph = paragraph.strip()
            identity_only = bool(re.match(r"^(邮箱|个人主页|学术档案|GitHub|Google Scholar|实验室主页|主页)\s*[：:\[]", paragraph))
            if not paragraph or (exclude_identity_only and identity_only):
                continue
            paragraph_offset = content.find(paragraph, max(body_offset, 0))
            for match in re.finditer(r"[^。！？\n]+[。！？]?", paragraph):
                quote = match.group(0).strip()
                if (
                    not quote
                    or quote.startswith("![")
                    or quote.startswith("|")
                    or quote == "---"
                    or (
                        exclude_identity_only
                        and re.match(r"^(邮箱|个人主页|学术档案|GitHub|Google Scholar|实验室主页|主页)\s*[：:\[]", quote)
                    )
                    or not re.search(r"[\u4e00-\u9fffA-Za-z]", quote)
                ):
                    continue
                relative = paragraph_offset + match.start() + len(match.group(0)) - len(match.group(0).lstrip())
                claims.append({
                    "quote": quote,
                    "start": absolute_start + relative,
                    "end": absolute_start + relative + len(quote),
                })
        return claims

    @staticmethod
    def _award_name(heading: str) -> str | None:
        if "提名" in heading:
            return "ICML 2026 Honorable Mention"
        if "获奖" in heading:
            return "ICML 2026 Outstanding Paper"
        return None

    @staticmethod
    def _paper_title(heading: str) -> str:
        if "：" in heading:
            return heading.split("：", 1)[1].strip()
        return heading

    @staticmethod
    def _person_names(heading: str) -> tuple[str, list[str]]:
        match = re.match(r"^(.*?)（(.*?)）$", heading)
        if not match:
            return heading.strip(), []
        canonical = match.group(1).strip()
        raw_aliases = re.split(r"[，,；;]", match.group(2))
        aliases = []
        for value in raw_aliases:
            alias = re.sub(r"^(亦用|又名)\s*", "", value).strip()
            if alias:
                aliases.append(alias)
        return canonical, aliases

    @staticmethod
    def _clean_url(url: str) -> str:
        return IcmlPeopleMarkdownImporter._unescape(url).rstrip(".。")

    @staticmethod
    def _unescape(value: str) -> str:
        return re.sub(r"\\([.\-_&()+~])", r"\1", value)


class AppleScholarsMarkdownImporter(IcmlPeopleMarkdownImporter):
    """Deterministic importer for the yearly Apple Scholars people dossier."""

    IMPORTER_VERSION = "apple-scholars-markdown-v1"

    def import_path(self, path: str | Path, *, observed_at: datetime, created_by: str = "local-import") -> ImportReport:
        source_path = Path(path).resolve()
        return self.import_text(
            source_path.read_text(encoding="utf-8"),
            source_uri=source_path.as_uri(),
            observed_at=observed_at,
            created_by=created_by,
        )

    def import_text(
        self,
        text: str,
        *,
        source_uri: str,
        observed_at: datetime,
        created_by: str = "local-import",
        cohort_id: str = "apple-scholars-ai-ml",
        media_type: str = "text/markdown",
        normalized_markdown: str | None = None,
    ) -> ImportReport:
        normalized_text = normalized_markdown if normalized_markdown is not None else text
        records = self._parse_apple(normalized_text)
        if not records:
            raise MemoryValidationError("Apple Scholars dossier contains no year-scoped person records")
        response = self.service.ingest_source(SourceIngestionRequest(
            source_uri=source_uri,
            source_type=SourceType.FILE if source_uri.startswith("file:") else SourceType.FEISHU,
            media_type=media_type,
            content=text,
            normalized_markdown=normalized_text,
            retrieved_at=observed_at,
            fetch_method=FetchMethod.UPLOAD if source_uri.startswith("file:") else FetchMethod.FEISHU,
            metadata={"cohort": cohort_id, "strict_initial_review": True, "importer": self.IMPORTER_VERSION},
        ))
        source = self.service.get_source_version(response.source_version_id)
        people: list[ImportedPerson] = []
        identity_ids: list[str] = []
        assertion_ids: list[str] = []
        evidence_ids: list[str] = []
        award_ids: list[str] = []

        for record in records:
            person = self._ensure_entity(Entity(
                entity_id=self._stable_id("ent", "Person", cohort_id, record["canonical_name"]),
                entity_type=EntityType.PERSON,
                canonical_name=record["canonical_name"],
                aliases=record["aliases"],
                metadata={"cohort": cohort_id, "award_year": record["year"], "strict_initial_review": True},
            ))
            block_span = self._ensure_span(EvidenceSpan(
                evidence_span_id=self._stable_id("span", source.source_version_id, person.entity_id, "profile-block"),
                source_version_id=source.source_version_id,
                locator_type="character",
                locator={"start": record["start"], "end": record["end"], "heading": record["heading"], "year": record["year"], "fragment_kind": "profile_block"},
                quote=record["content"],
                quote_hash=hashlib.sha256(record["content"].encode("utf-8")).hexdigest(),
                normalized_version_ref=source.normalized_markdown_ref,
            ))
            evidence_ids.append(block_span.evidence_span_id)
            person_assertions: list[str] = []
            for statement_index, statement in enumerate(
                self._statement_fragments(record["content"], record["start"], exclude_identity_only=True),
                1,
            ):
                span = self._ensure_span(EvidenceSpan(
                    evidence_span_id=self._stable_id("span", source.source_version_id, person.entity_id, "statement", str(statement_index)),
                    source_version_id=source.source_version_id,
                    locator_type="character",
                    locator={"start": statement["start"], "end": statement["end"], "heading": record["heading"], "year": record["year"], "fragment_kind": "profile_statement", "statement": statement_index},
                    quote=statement["quote"],
                    quote_hash=hashlib.sha256(statement["quote"].encode("utf-8")).hexdigest(),
                    normalized_version_ref=source.normalized_markdown_ref,
                ))
                evidence_ids.append(span.evidence_span_id)
                claim = self._ensure_assertion(Assertion(
                    assertion_id=self._stable_id("ast", source.source_version_id, person.entity_id, "profile_claim", statement["quote"]),
                    subject_entity_id=person.entity_id,
                    predicate_id="profile_claim",
                    object=AssertionObject(kind=ObjectKind.LITERAL, datatype=LiteralDatatype.STRING, value=statement["quote"]),
                    epistemic_type=EpistemicType.REPORTED,
                    valid_time=TemporalExtent(interval_type=IntervalType.UNKNOWN),
                    transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                    evidence_span_ids=[span.evidence_span_id],
                    confidence=0.65,
                    review_required=True,
                    metadata={"importer": self.IMPORTER_VERSION, "fact_kind": "direct_fact", "claim_scope": "unmapped_profile_statement", "award_year": record["year"], "source_version_id": source.source_version_id},
                ))
                assertion_ids.append(claim.assertion_id)
                person_assertions.append(claim.assertion_id)

            award_name = f"Apple Scholars in AI/ML PhD Fellowship {record['year']}"
            award = self._ensure_entity(Entity(
                entity_id=self._stable_id("ent", "Award", award_name),
                entity_type=EntityType.AWARD,
                canonical_name=award_name,
                metadata={"program": "Apple Scholars in AI/ML PhD Fellowship", "year": record["year"]},
            ))
            award_ids.append(award.entity_id)
            awarded = self._ensure_assertion(Assertion(
                assertion_id=self._stable_id("ast", source.source_version_id, person.entity_id, "awarded", award.entity_id),
                subject_entity_id=person.entity_id,
                predicate_id="awarded",
                object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=award.entity_id),
                epistemic_type=EpistemicType.REPORTED,
                valid_time=TemporalExtent(interval_type=IntervalType.UNKNOWN),
                transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                evidence_span_ids=[block_span.evidence_span_id],
                confidence=0.88,
                review_required=True,
                metadata={"importer": self.IMPORTER_VERSION, "fact_kind": "direct_fact", "award_year": record["year"], "source_version_id": source.source_version_id},
            ))
            assertion_ids.append(awarded.assertion_id)
            person_assertions.append(awarded.assertion_id)

            accounts = sorted(set(record["urls"] + [f"mailto:{value}" for value in record["emails"]]))
            for account_value in accounts:
                account = self._ensure_entity(Entity(
                    entity_id=self._stable_id("ent", "IdentityAccount", account_value.lower()),
                    entity_type=EntityType.IDENTITY_ACCOUNT,
                    canonical_name=account_value,
                    metadata={"url": account_value, "source_person_name": person.canonical_name},
                ))
                identity_ids.append(account.entity_id)
                owned = self._ensure_assertion(Assertion(
                    assertion_id=self._stable_id("ast", source.source_version_id, account.entity_id, "account_owned_by", person.entity_id),
                    subject_entity_id=account.entity_id,
                    predicate_id="account_owned_by",
                    object=AssertionObject(kind=ObjectKind.ENTITY, entity_id=person.entity_id),
                    epistemic_type=EpistemicType.REPORTED,
                    valid_time=TemporalExtent(interval_type=IntervalType.OPEN_END),
                    transaction_time=TransactionTime(observed_at=observed_at, ingested_at=observed_at),
                    evidence_span_ids=[block_span.evidence_span_id],
                    confidence=0.75,
                    review_required=True,
                    metadata={"importer": self.IMPORTER_VERSION, "fact_kind": "direct_fact", "identity_url": account_value, "source_version_id": source.source_version_id},
                ))
                assertion_ids.append(owned.assertion_id)
                person_assertions.append(owned.assertion_id)
            people.append(ImportedPerson(person.entity_id, person.canonical_name, person.aliases, person_assertions))

        review = self.service.create_initial_review_batch(
            source_kind="feishu_import" if source.source_type == SourceType.FEISHU else "file_import",
            source_ref=f"initial-import:{source.source_version_id}",
            title_zh="Apple Scholars 人物知识首次建档审核",
            assertion_ids=list(dict.fromkeys(assertion_ids)),
            source_version_ids=[source.source_version_id],
            created_by=created_by,
        )
        unique_evidence = list(dict.fromkeys(evidence_ids))
        unique_identity = list(dict.fromkeys(identity_ids))
        return ImportReport(
            source_version_id=source.source_version_id,
            version_status=response.version_status,
            review_batch_id=review.batch.review_batch_id,
            people=people,
            paper_entity_ids=list(dict.fromkeys(award_ids)),
            identity_entity_ids=unique_identity,
            assertion_ids=list(dict.fromkeys(assertion_ids)),
            derived_assertion_ids=[],
            evidence_span_ids=unique_evidence,
            counts={
                "people": len(people), "award_years": len(set(record["year"] for record in records)),
                "identity_accounts": len(unique_identity),
                "direct_assertions": len(set(assertion_ids)), "derived_assertions": 0,
                "evidence_spans": len(unique_evidence), "review_items": len(review.items),
            },
        )

    @classmethod
    def _parse_apple(cls, text: str) -> list[dict]:
        headings = list(re.finditer(r"^(#)\s+(.+)$|^(##)\s+(.+)$", text, flags=re.MULTILINE))
        year: str | None = None
        records: list[dict] = []
        for index, match in enumerate(headings):
            level1, h1, level2, h2 = match.groups()
            if level1:
                found = re.search(r"(20\d{2})\s*年", cls._unescape(h1))
                if found:
                    year = found.group(1)
                continue
            if not level2 or not year:
                continue
            heading = cls._unescape(h2.strip())
            heading = re.sub(r"^\*\*|\*\*$", "", heading).strip()
            heading = re.sub(r"^\d+[.．]\s*", "", heading).strip()
            heading = re.sub(r"^\*\*|\*\*$", "", heading).strip()
            start = match.start()
            end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
            content = text[start:end].strip()
            table = re.search(r"\n\s*\n\|\s*姓名\s*\|", content)
            if table:
                content = content[:table.start()].rstrip()
                end = start + len(content)
            canonical, aliases = cls._person_names(heading)
            unescaped = cls._unescape(content)
            urls = cls._extract_urls(content)
            emails = sorted({value.lower() for value in EMAIL_RE.findall(unescaped)})
            records.append({
                "year": year, "heading": heading, "canonical_name": canonical, "aliases": aliases,
                "start": start, "end": end, "content": content,
                "urls": [url for url in urls if url], "emails": emails,
            })
        return records

    @classmethod
    def _extract_urls(cls, content: str) -> list[str]:
        """Prefer Markdown link destinations and never join label + target."""
        destinations = re.findall(r"\]\((https?://[^)\s]+)\)", content)
        without_links = re.sub(r"\[[^\]]*\]\((?:https?://|mailto:)[^)]*\)", "", content)
        bare = URL_RE.findall(without_links)
        return sorted({value for value in (cls._clean_url(item) for item in [*destinations, *bare]) if value})


__all__ = ["AppleScholarsMarkdownImporter", "IcmlPeopleMarkdownImporter", "ImportReport", "ImportedPerson"]
