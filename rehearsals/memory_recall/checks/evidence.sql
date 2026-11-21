-- Read-only checks for the cross-task recall rehearsal.
--
-- Every statement here is a SELECT. Nothing in this file writes, locks for update,
-- migrates, or closes a Run. Run it against the rehearsal database only:
--
--   psql "<dsn>" -v task_id='<source task uuid>' -v run_id='<source run uuid>' \
--                -v target_task_id='<target task uuid>' -v target_run_id='<target run uuid>' \
--                -f checks/evidence.sql
--
-- An empty result is an answer too: "no rows" often means the expected evidence is
-- missing, not that the check passed.

\echo '== 1. source Task: status, repository and whether admission evidence marks a pack =='
SELECT t.id,
       t.status,
       t.repo,
       t.base_commit,
       (t.admission_record -> 'evidence') AS admission_evidence,
       EXISTS (
           SELECT 1 FROM jsonb_array_elements_text(
                      COALESCE(t.admission_record -> 'evidence', '[]'::jsonb)) AS item
           WHERE item LIKE 'pack:%') AS has_pack_marker
FROM tasks t
WHERE t.id = :'task_id';

\echo '== 2. source Run and its formal Verification (ingest needs PASSED + trusted_v1 + suite_version) =='
SELECT r.id AS run_id,
       r.status AS run_status,
       v.id AS verification_id,
       v.result AS verification_result,
       v.evidence ->> 'scope' AS evidence_scope,
       v.evidence ->> 'suite_version' AS suite_version,
       v.evidence ->> 'draft_artifact_id' AS draft_artifact_id,
       v.evidence ->> 'independent_artifact_id' AS independent_artifact_id
FROM runs r
LEFT JOIN verifications v ON v.run_id = r.id
WHERE r.id = :'run_id'
ORDER BY v.created_at;

\echo '== 3. memory candidate: source state, content status, scope and applicability =='
SELECT m.id,
       m.identity_key,
       m.status,
       m.source_kind,
       m.source_state,
       m.content_status,
       m.scope_kind,
       m.scope_value,
       m.topic,
       m.valid_until,
       m.applicable_base_commit,
       m.applicable_environment,
       m.task_id AS source_task_id,
       m.run_id AS source_run_id,
       m.verification_id AS source_verification_id,
       m.task_review_id AS source_task_review_id,
       m.created_by,
       m.created_at
FROM memory_items m
WHERE m.id = :'memory_id';

\echo '== 4. candidate review (use needs accepted + matching candidate digest) =='
SELECT rv.id,
       rv.memory_id,
       rv.conclusion,
       rv.candidate_sha256,
       rv.reviewer,
       rv.reason,
       rv.created_at
FROM memory_reviews rv
WHERE rv.memory_id = :'memory_id';

\echo '== 5. operator declarations made against the target Task =='
SELECT u.id,
       u.idempotency_key,
       u.memory_id,
       u.review_id,
       u.run_id,
       u.candidate_sha256,
       u.operator,
       u.purpose,
       u.target_base_commit,
       u.target_environment,
       u.created_at
FROM memory_uses u
WHERE u.task_id = :'target_task_id'
ORDER BY u.created_at;

\echo '== 6. what the Worker actually put into an Agent input (this is the recall payload) =='
SELECT ae.id AS agent_execution_id,
       ae.agent_role,
       ae.status,
       ae.created_at,
       (ae.input ? 'memory_recall_v1') AS has_recall_key,
       ae.input -> 'memory_recall_v1' ->> 'candidate_id' AS candidate_id,
       ae.input -> 'memory_recall_v1' ->> 'candidate_sha256' AS candidate_sha256,
       ae.input -> 'memory_recall_v1' ->> 'content_status' AS content_status,
       ae.input -> 'memory_recall_v1' ->> 'source_task_id' AS source_task_id,
       ae.input -> 'memory_recall_v1' ->> 'use_id' AS use_id
FROM agent_executions ae
WHERE ae.run_id = :'target_run_id'
ORDER BY ae.created_at;

\echo '== 7. the rehearsal must leave no formal Verification on the target Run =='
SELECT v.id AS verification_id, v.result, v.evidence ->> 'scope' AS evidence_scope
FROM verifications v
WHERE v.run_id = :'target_run_id';

\echo '== 8. target Task and Run state after the rehearsal (RUNNING is the expected outcome) =='
SELECT t.id AS task_id, t.status AS task_status, r.id AS run_id, r.status AS run_status
FROM tasks t
LEFT JOIN runs r ON r.task_id = t.id
WHERE t.id = :'target_task_id';
