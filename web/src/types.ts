export type Role = 'maker' | 'checker_data' | 'checker_security' | 'operator' | 'auditor'
export interface User { id: string; email: string; display_name: string | null; is_active: boolean }
export interface Project { id: string; name: string; tenant_id: string; created_at: string }
export interface Member { id: string; user_id: string; email: string; display_name: string | null; role: Role; created_at: string }
export interface Datasource { id: string; project_id: string; name: string; type: string; config: Record<string, unknown>; status: string; has_credential: boolean; created_at: string }
export interface ProfileColumn { name: string; type: string; nullable: boolean; level: string; samples: string[] }
export interface ProfileTable { name: string; columns: ProfileColumn[]; max_level?: string }
export interface Profile { id: string; datasource_id: string; max_level: string; tables_scanned: number; sampled_at: string; schema_json: { tables: ProfileTable[] }; sensitivity_tags: Record<string, unknown> }
export interface Mapping { source_field: string; target_field: string; transform: string | null; sensitivity_level: string | null }
export interface Step { step_id: string; operator_id: string; role: string; attributes: Record<string, unknown>; note: string | null }
export interface QualityRule { field: string; rule: 'not_null' | 'regex' | 'range' | 'enum'; params: Record<string, unknown>; error_code: string; action: 'reject' }
export interface Plan { name: string; description: string; source_datasource_id: string; target_datasource_id: string; source_table: string; target_table: string; steps: Step[]; field_mappings: Mapping[]; quality_rules: QualityRule[] }
export interface Generation { run_id: string; status: string; plan: Plan | null; config: { content: string; format: string } | null; dag: { nodes: { id: string; label: string; kind: string }[]; edges: { source: string; target: string }[] } | null; questions: { key: string; question: string; hint: string | null; options: string[] }[]; warnings: string[]; unsupported_reason: string | null; error: string | null }
export interface VersionSummary { id: string; project_id: string; version_no: number; status: string; sha256: string | null; created_at: string; frozen_at: string | null }
export interface Version extends VersionSummary { run_id: string | null; etl_plan: Plan; config: string; quality_rules: QualityRule[]; gate_report: { passed: boolean; checks: { name: string; status: string; message: string }[] } | null; source_profile_id: string | null; target_profile_id: string | null }
export interface VersionDiff { left: VersionSummary; right: VersionSummary; fingerprint_equal: boolean; plan_changed: boolean; config_changed: boolean; quality_rules_changed: boolean; mappings_added: Mapping[]; mappings_removed: Mapping[]; mappings_changed: Record<string, unknown>[]; steps_added: string[]; steps_removed: string[]; config_diff: string[] }
export interface Approval { id: string; dimension: 'data' | 'security'; status: string; approver_id: string | null; decision: 'approved' | 'rejected' | null; comment: string | null; decided_at: string | null; expires_at: string }
export interface PreparationSummary { id: string; version_id: string; status: string; risk_level: string; fingerprint: string; expires_at: string; created_at: string }
export interface Preparation extends PreparationSummary { project_id: string; created_by: string; impact_scope: Record<string, unknown>; data_grading: Record<string, unknown>; budget: Record<string, number>; rollback_plan: Record<string, unknown>; risk_reasons: string[]; approvals: Approval[]; missing_dimensions: string[]; committed_at: string | null }
/** 一次运行的质量结论（FR-33 / FR-40）。为 null 表示**这次运行没有质量契约**。 */
export interface QualityReport { run_id: string; source_rows: number; valid_rows: number; rejected_rows: number; rejected_ratio: number; error_dist: Record<string, number>; error_table: string | null }
export interface Run { id: string; project_id: string; version_id: string; preparation_id: string; status: string; environment: string; artifact: string; read_rows: number; written_bytes: number; rejected_rows: number; cancel_requested: boolean; worker_lease_owner: string | null; lease_expires_at: string | null; error: string | null; started_at: string | null; finished_at: string | null; heartbeat_at: string | null; published_at: string | null; rolled_back_at: string | null; quality_report: QualityReport | null; created_at: string }
export interface RollbackResult { run_id: string; executed: boolean; dropped: string[]; reason: string }
export interface Operator { operator_id: string; type: string; description: string; connector_artifact: string }
export interface OperatorDetail extends Operator { attributes: { name: string; required: boolean; default: string | null; type: string; description: string }[]; dialect_constraints: string[]; seatunnel_version: string }
/** 证据账本里的一条记录（M11，FR-39）。`anchored` 表示它同时是一个签名检查点。 */
export interface AuditRecord { seq: number; event_type: string; actor: string; occurred_at: string; project_id: string | null; run_id: string | null; approval_id: string | null; version_id: string | null; preparation_id: string | null; payload: Record<string, unknown> | null; prev_hash: string; entry_hash: string; signature: string | null; anchored: boolean }
export interface AuditChainHead { seq: number; entry_hash: string; integrity_status: string; checked_at: string | null }
export interface AuditRecordPage { items: AuditRecord[]; next_after_seq: number | null; head: AuditChainHead }
export interface AuditFailure { seq: number; code: string; detail: string }
/** 链完整性结论。`empty` / `indeterminate` / `breached` 是三种不同的处境，不可合并显示。 */
export interface AuditVerification { chain_id: string; status: string; trigger: string; checked_at: string; checked: number; from_seq: number | null; to_seq: number | null; head_seq: number; head_hash: string; anchors_checked: number; anchors_verified: number; first_failure_seq: number | null; failures: AuditFailure[]; unanchored_tail_seconds: number | null; duration_ms: number; public_key: { kid: string; algorithm: string; pem: string }; stale: boolean }
