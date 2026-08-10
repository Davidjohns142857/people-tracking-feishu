CREATE TABLE IF NOT EXISTS source_document_versions (
    source_version_id TEXT PRIMARY KEY,
    source_uri TEXT NOT NULL,
    source_type TEXT NOT NULL,
    media_type TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    raw_object_ref TEXT NOT NULL,
    normalized_markdown_ref TEXT,
    published_at TIMESTAMPTZ,
    retrieved_at TIMESTAMPTZ NOT NULL,
    source_identity TEXT,
    fetch_method TEXT NOT NULL,
    rights_scope TEXT NOT NULL,
    previous_version_id TEXT REFERENCES source_document_versions(source_version_id),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (source_uri, content_hash)
);

CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    episode_type TEXT NOT NULL,
    source_version_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    entity_candidate_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    scan_run_id TEXT,
    occurred_at JSONB,
    observed_at TIMESTAMPTZ NOT NULL,
    actor_id TEXT,
    context JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS evidence_spans (
    evidence_span_id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL REFERENCES source_document_versions(source_version_id),
    locator_type TEXT NOT NULL,
    locator JSONB NOT NULL DEFAULT '{}'::jsonb,
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    normalized_version_ref TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS entities (
    entity_id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS predicate_definitions (
    ontology_version TEXT NOT NULL,
    predicate_id TEXT NOT NULL,
    definition JSONB NOT NULL,
    PRIMARY KEY (ontology_version, predicate_id)
);

CREATE TABLE IF NOT EXISTS extraction_runs (
    extraction_run_id TEXT PRIMARY KEY,
    source_version_id TEXT NOT NULL REFERENCES source_document_versions(source_version_id),
    extractor TEXT NOT NULL,
    extractor_version TEXT NOT NULL,
    ontology_version TEXT NOT NULL,
    prompt_hash TEXT,
    model_name TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS assertions (
    assertion_id TEXT PRIMARY KEY,
    subject_entity_id TEXT NOT NULL REFERENCES entities(entity_id),
    predicate_id TEXT NOT NULL,
    object_value JSONB NOT NULL,
    polarity TEXT NOT NULL,
    epistemic_type TEXT NOT NULL,
    initial_status TEXT NOT NULL,
    valid_time JSONB NOT NULL,
    transaction_time JSONB NOT NULL,
    evidence_span_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    confidence DOUBLE PRECISION NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    extraction_run_id TEXT REFERENCES extraction_runs(extraction_run_id),
    ontology_version TEXT NOT NULL,
    review_required BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS assertion_relations (
    assertion_relation_id TEXT PRIMARY KEY,
    from_assertion_id TEXT NOT NULL REFERENCES assertions(assertion_id),
    relation_type TEXT NOT NULL,
    to_assertion_id TEXT NOT NULL REFERENCES assertions(assertion_id),
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS annotations (
    annotation_id TEXT PRIMARY KEY,
    target_assertion_id TEXT NOT NULL REFERENCES assertions(assertion_id),
    action TEXT NOT NULL,
    replacement_assertion_id TEXT REFERENCES assertions(assertion_id),
    reason TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS initial_review_batches (
    review_batch_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS initial_review_items (
    review_item_id TEXT PRIMARY KEY,
    review_batch_id TEXT NOT NULL REFERENCES initial_review_batches(review_batch_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS initial_review_decisions (
    review_decision_id TEXT PRIMARY KEY,
    review_item_id TEXT NOT NULL REFERENCES initial_review_items(review_item_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS derived_assertion_inputs (
    derived_assertion_id TEXT PRIMARY KEY REFERENCES assertions(assertion_id),
    input_assertion_ids JSONB NOT NULL,
    rule_id TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    model_name TEXT,
    prompt_hash TEXT,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS signals (
    signal_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS candidate_people (
    candidate_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS command_receipts (
    command_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_runs (
    scan_run_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_attempts (
    connector_attempt_id TEXT PRIMARY KEY,
    scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    discovery_candidate_id TEXT PRIMARY KEY,
    scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS change_sets (
    change_set_id TEXT PRIMARY KEY,
    scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_artifacts (
    artifact_id TEXT PRIMARY KEY,
    scan_run_id TEXT NOT NULL REFERENCES scan_runs(scan_run_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_jobs (
    connector_job_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    max_attempts INTEGER NOT NULL CHECK (max_attempts BETWEEN 1 AND 8),
    not_before TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS connector_job_events (
    event_sequence BIGSERIAL PRIMARY KEY,
    connector_job_event_id TEXT NOT NULL UNIQUE,
    connector_job_id TEXT NOT NULL REFERENCES connector_jobs(connector_job_id),
    event_type TEXT NOT NULL CHECK (event_type IN ('queued','claimed','deferred','retry_scheduled','succeeded','dead_lettered')),
    worker_id TEXT,
    attempt INTEGER NOT NULL DEFAULT 0,
    lease_expires_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    payload JSONB NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS person_keys (
    person_key_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL UNIQUE,
    entity_id TEXT NOT NULL UNIQUE REFERENCES entities(entity_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS person_source_slices (
    person_source_slice_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL,
    source_version_id TEXT NOT NULL REFERENCES source_document_versions(source_version_id),
    evidence_span_id TEXT NOT NULL REFERENCES evidence_spans(evidence_span_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (person_key, evidence_span_id)
);

CREATE TABLE IF NOT EXISTS person_profile_revisions (
    profile_revision_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (person_key, revision_number)
);

CREATE TABLE IF NOT EXISTS person_search_plan_revisions (
    search_plan_revision_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (person_key, revision_number)
);

CREATE TABLE IF NOT EXISTS person_profile_import_runs (
    import_run_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS person_profile_scan_runs (
    person_profile_scan_run_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    person_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS person_update_bundles (
    bundle_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL,
    scan_run_id TEXT NOT NULL REFERENCES person_profile_scan_runs(person_profile_scan_run_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS person_digest_batches (
    digest_batch_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_patch_review_batches (
    review_batch_id TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_patch_review_items (
    review_item_id TEXT PRIMARY KEY,
    review_batch_id TEXT NOT NULL REFERENCES profile_patch_review_batches(review_batch_id),
    person_key TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS profile_patch_review_decisions (
    review_decision_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    review_item_id TEXT NOT NULL REFERENCES profile_patch_review_items(review_item_id),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS person_cognee_projections (
    projection_id TEXT PRIMARY KEY,
    person_key TEXT NOT NULL,
    dataset_name TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

ALTER TABLE connector_job_events DROP CONSTRAINT IF EXISTS connector_job_events_event_type_check;
ALTER TABLE connector_job_events ADD CONSTRAINT connector_job_events_event_type_check
CHECK (event_type IN ('queued','claimed','deferred','retry_scheduled','succeeded','dead_lettered'));

CREATE TABLE IF NOT EXISTS projection_outbox (
    outbox_id BIGSERIAL PRIMARY KEY,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'upsert',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projected_at TIMESTAMPTZ,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_source_uri_time ON source_document_versions(source_uri, retrieved_at);
CREATE INDEX IF NOT EXISTS idx_spans_source ON evidence_spans(source_version_id);
CREATE INDEX IF NOT EXISTS idx_entities_name_type ON entities(canonical_name, entity_type);
CREATE INDEX IF NOT EXISTS idx_assertions_subject_predicate ON assertions(subject_entity_id, predicate_id);
CREATE INDEX IF NOT EXISTS idx_assertions_transaction_time ON assertions USING GIN (transaction_time);
CREATE INDEX IF NOT EXISTS idx_assertions_valid_time ON assertions USING GIN (valid_time);
CREATE INDEX IF NOT EXISTS idx_annotations_target_time ON annotations(target_assertion_id, created_at);
CREATE INDEX IF NOT EXISTS idx_relations_from ON assertion_relations(from_assertion_id);
CREATE INDEX IF NOT EXISTS idx_relations_to ON assertion_relations(to_assertion_id);
CREATE INDEX IF NOT EXISTS idx_outbox_pending ON projection_outbox(projected_at) WHERE projected_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_connector_attempts_run ON connector_attempts(scan_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_run ON discovery_candidates(scan_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_change_sets_run ON change_sets(scan_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workflow_artifacts_run ON workflow_artifacts(scan_run_id, created_at);
CREATE INDEX IF NOT EXISTS idx_connector_jobs_ready ON connector_jobs(not_before, created_at);
CREATE INDEX IF NOT EXISTS idx_connector_job_events_latest ON connector_job_events(connector_job_id, event_sequence DESC);
CREATE INDEX IF NOT EXISTS idx_person_slices_key_source ON person_source_slices(person_key, source_version_id);
CREATE INDEX IF NOT EXISTS idx_person_profile_latest ON person_profile_revisions(person_key, revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_person_search_plan_latest ON person_search_plan_revisions(person_key, revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_person_scan_runs_key ON person_profile_scan_runs(person_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_person_bundles_key ON person_update_bundles(person_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_profile_review_items_batch ON profile_patch_review_items(review_batch_id, created_at);
CREATE INDEX IF NOT EXISTS idx_person_cognee_key ON person_cognee_projections(person_key, created_at DESC);

CREATE OR REPLACE FUNCTION enqueue_people_intel_projection()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO projection_outbox(object_type, object_id)
    VALUES (
        TG_ARGV[0],
        to_jsonb(NEW) ->> CASE WHEN TG_ARGV[0] = 'entity' THEN 'entity_id' ELSE 'assertion_id' END
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS entity_projection_outbox ON entities;
CREATE TRIGGER entity_projection_outbox
AFTER INSERT ON entities
FOR EACH ROW EXECUTE FUNCTION enqueue_people_intel_projection('entity');

DROP TRIGGER IF EXISTS assertion_projection_outbox ON assertions;
CREATE TRIGGER assertion_projection_outbox
AFTER INSERT ON assertions
FOR EACH ROW EXECUTE FUNCTION enqueue_people_intel_projection('assertion');

CREATE OR REPLACE FUNCTION reject_append_only_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION '% is append-only; insert a replacement record instead', TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS source_versions_append_only ON source_document_versions;
CREATE TRIGGER source_versions_append_only
BEFORE UPDATE OR DELETE ON source_document_versions
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS episodes_append_only ON episodes;
CREATE TRIGGER episodes_append_only
BEFORE UPDATE OR DELETE ON episodes
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS evidence_spans_append_only ON evidence_spans;
CREATE TRIGGER evidence_spans_append_only
BEFORE UPDATE OR DELETE ON evidence_spans
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS assertions_append_only ON assertions;
CREATE TRIGGER assertions_append_only
BEFORE UPDATE OR DELETE ON assertions
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS annotations_append_only ON annotations;
CREATE TRIGGER annotations_append_only
BEFORE UPDATE OR DELETE ON annotations
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS assertion_relations_append_only ON assertion_relations;
CREATE TRIGGER assertion_relations_append_only
BEFORE UPDATE OR DELETE ON assertion_relations
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS entities_append_only ON entities;
CREATE TRIGGER entities_append_only
BEFORE UPDATE OR DELETE ON entities
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS predicate_definitions_append_only ON predicate_definitions;
CREATE TRIGGER predicate_definitions_append_only
BEFORE UPDATE OR DELETE ON predicate_definitions
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS derived_inputs_append_only ON derived_assertion_inputs;
CREATE TRIGGER derived_inputs_append_only
BEFORE UPDATE OR DELETE ON derived_assertion_inputs
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS signals_append_only ON signals;
CREATE TRIGGER signals_append_only
BEFORE UPDATE OR DELETE ON signals
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS candidates_append_only ON candidate_people;
CREATE TRIGGER candidates_append_only
BEFORE UPDATE OR DELETE ON candidate_people
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS command_receipts_append_only ON command_receipts;
CREATE TRIGGER command_receipts_append_only
BEFORE UPDATE OR DELETE ON command_receipts
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DROP TRIGGER IF EXISTS scan_runs_append_only ON scan_runs;
CREATE TRIGGER scan_runs_append_only BEFORE UPDATE OR DELETE ON scan_runs
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DROP TRIGGER IF EXISTS connector_attempts_append_only ON connector_attempts;
CREATE TRIGGER connector_attempts_append_only BEFORE UPDATE OR DELETE ON connector_attempts
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DROP TRIGGER IF EXISTS discovery_candidates_append_only ON discovery_candidates;
CREATE TRIGGER discovery_candidates_append_only BEFORE UPDATE OR DELETE ON discovery_candidates
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DROP TRIGGER IF EXISTS change_sets_append_only ON change_sets;
CREATE TRIGGER change_sets_append_only BEFORE UPDATE OR DELETE ON change_sets
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DROP TRIGGER IF EXISTS workflow_artifacts_append_only ON workflow_artifacts;
CREATE TRIGGER workflow_artifacts_append_only BEFORE UPDATE OR DELETE ON workflow_artifacts
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DROP TRIGGER IF EXISTS connector_jobs_append_only ON connector_jobs;
CREATE TRIGGER connector_jobs_append_only BEFORE UPDATE OR DELETE ON connector_jobs
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();
DROP TRIGGER IF EXISTS connector_job_events_append_only ON connector_job_events;
CREATE TRIGGER connector_job_events_append_only BEFORE UPDATE OR DELETE ON connector_job_events
FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation();

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'person_keys',
        'person_source_slices',
        'person_profile_revisions',
        'person_search_plan_revisions',
        'person_profile_import_runs',
        'person_profile_scan_runs',
        'person_update_bundles',
        'person_digest_batches',
        'profile_patch_review_batches',
        'profile_patch_review_items',
        'profile_patch_review_decisions',
        'person_cognee_projections'
    ]
    LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON %I', table_name || '_append_only', table_name);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON %I FOR EACH ROW EXECUTE FUNCTION reject_append_only_mutation()',
            table_name || '_append_only',
            table_name
        );
    END LOOP;
END
$$;
