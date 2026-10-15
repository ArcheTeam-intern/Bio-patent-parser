/**
 * Typed API client for the /api/v1 surface.
 *
 * Every error carries the backend's structured {code,message,details} so
 * the UI can explain failures instead of guessing from status codes.
 */

export const API_PREFIX = "/api/v1";

export class ApiError extends Error {
  code: string;
  details: Record<string, unknown>;
  status: number;

  constructor(status: number, code: string, message: string, details: Record<string, unknown>) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_PREFIX}${path}`, init);
  const text = await res.text();
  const body = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const err = body?.error ?? {};
    throw new ApiError(
      res.status,
      String(err.code ?? "HTTP_ERROR"),
      String(err.message ?? res.statusText),
      err.details ?? {},
    );
  }
  return body as T;
}

// ---- types -------------------------------------------------------------

export interface EngineCapability {
  name: string;
  available: boolean;
  supports_tables: boolean;
  supports_ocr: boolean;
  supports_page_range: boolean;
  sends_bytes_offsite: boolean;
}

export interface Capabilities {
  api_version: string;
  engines: EngineCapability[];
  schema_version: string;
  rule_version: string;
  max_upload_bytes: number;
  samples_available: boolean;
}

export interface SampleInfo {
  sample_id: string;
  file_name: string;
  category: "paper" | "patent" | "scan";
  bytes: number;
  available: boolean;
}

export interface DocumentInfo {
  document_id: string;
  sha256: string;
  bytes: number;
  page_count: number;
  file_name: string;
  is_encrypted: boolean;
}

export interface StageRun {
  stage_name: string;
  attempt: number;
  status: string;
  started_at: string | null;
  ended_at: string | null;
  error: Record<string, string>;
}

export interface Job {
  api_version: string;
  job_id: string;
  document_id: string;
  status: string;
  current_stage: string | null;
  result_revision: number;
  open_reviews: number;
  created_at: string;
  updated_at: string;
  stages: StageRun[];
  export_artifact_id: string | null;
  config_hash: string;
  engine: string;
}

export interface RecordsResponse {
  api_version: string;
  job_id: string;
  revision: number;
  sequences: SequenceRecordData[];
  measurements: MeasurementData[];
  record_pages: Record<string, number[]>;
}

export interface ActivityValue {
  kind: "scalar" | "range" | "plus_minus" | "categorical" | "unparsed";
  operator: string | null;
  number: string | null;
  lower: string | null;
  upper: string | null;
  uncertainty: string | null;
  qualifier: string | null;
  raw: string;
}

export interface SequenceRecordData {
  sequence_id: string;
  document_id: string;
  seq_id_no: string | null;
  molecule_type: string;
  representation: string;
  declared_length: number | null;
  observed_length: number | null;
  raw_sequence: string;
  normalized_sequence: string;
  residues: { position: string; token: string; one_letter: string; resolved: string }[];
  is_complete: boolean;
  validation_state: string;
  acceptance_state: string;
  evidence_ids: string[];
  checks: CheckResultData[];
  review_reasons?: string[];
}

export interface MeasurementData {
  measurement_id: string;
  document_id: string;
  entity_id: string | null;
  metric: string;
  value: ActivityValue;
  unit_raw: string | null;
  unit_normalized: string | null;
  target_raw: string | null;
  evidence_kind: string;
  field_evidence: Record<string, string[]>;
  evidence_ids: string[];
  validation_state: string;
  acceptance_state: string;
  checks: CheckResultData[];
  review_reasons?: string[];
}

export interface CheckResultData {
  check_id: string;
  outcome: string;
  details: Record<string, string>;
  evidence_ids: string[];
}

export interface PageData {
  api_version: string;
  job_id: string;
  page_number: number;
  width_pt: number;
  height_pt: number;
  route: string;
  blocks: { block_id: string; kind: string; bbox: number[] | null; text_raw: string }[];
  tables: unknown[];
}

export interface ReviewItem {
  api_version?: string;
  review_id: string;
  job_id: string;
  record_id: string;
  record_kind: "sequence" | "measurement" | "unknown";
  record_revision: number;
  reason_codes: string[];
  status: string;
  evidence_ids: string[];
}

// ---- endpoints ---------------------------------------------------------

export function getCapabilities(): Promise<Capabilities> {
  return request("/capabilities");
}

export function getSamples(): Promise<{ samples: SampleInfo[] }> {
  return request("/documents");
}

export async function uploadDocument(file: File): Promise<DocumentInfo> {
  const form = new FormData();
  form.append("file", file);
  return request("/documents", { method: "POST", body: form });
}

export function importSample(sampleId: string): Promise<DocumentInfo> {
  return request(`/documents/import-sample?sample_id=${encodeURIComponent(sampleId)}`, {
    method: "POST",
  });
}

export function createJob(body: {
  document_id: string;
  parser: string;
  page_numbers?: number[];
  targets?: string[];
  ocr_policy?: string;
  document_type_hint?: string;
}): Promise<Job> {
  return request("/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listJobs(): Promise<Job[]> {
  return request("/jobs");
}

export function getJob(jobId: string): Promise<Job> {
  return request(`/jobs/${encodeURIComponent(jobId)}`);
}

export function getRecords(jobId: string, kind?: string): Promise<RecordsResponse> {
  const suffix = kind ? `?kind=${kind}` : "";
  return request(`/jobs/${encodeURIComponent(jobId)}/records${suffix}`);
}

export function getPage(jobId: string, pageNumber: number): Promise<PageData> {
  return request(`/jobs/${encodeURIComponent(jobId)}/pages/${pageNumber}`);
}

export function getReviews(jobId?: string): Promise<{ reviews: ReviewItem[]; total: number }> {
  const suffix = jobId ? `?job_id=${encodeURIComponent(jobId)}` : "";
  return request(`/reviews${suffix}`);
}

export function documentSourceUrl(documentId: string): string {
  return `${API_PREFIX}/documents/${encodeURIComponent(documentId)}/source`;
}

export function resumeJob(jobId: string): Promise<Job> {
  return request(`/jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST" });
}

export function cancelJob(jobId: string): Promise<Job> {
  return request(`/jobs/${encodeURIComponent(jobId)}/cancel`, { method: "POST" });
}

export function artifactContentUrl(jobId: string, artifactId: string): string {
  return `${API_PREFIX}/jobs/${encodeURIComponent(jobId)}/artifacts/${encodeURIComponent(artifactId)}/content`;
}

export interface DecisionResponse {
  api_version: string;
  review_id: string;
  action: string;
  record_revision: number;
  accepted: boolean | null;
  validation_state: string | null;
}

export function submitDecision(
  reviewId: string,
  body: {
    action: "confirm" | "correct" | "reject";
    base_revision: number;
    actor: string;
    reason?: string;
    patch?: Record<string, unknown>;
    evidence_ids?: string[];
  },
): Promise<DecisionResponse> {
  return request(`/reviews/${encodeURIComponent(reviewId)}/decisions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export interface ExportInfo {
  api_version: string;
  export_id: string;
  job_id: string;
  format: string;
  status: string;
  result_revision: number;
  record_count: number;
  error: string | null;
}

export function createExport(
  jobId: string,
  body: { format: "json" | "csv" | "fasta"; accepted_only?: boolean },
): Promise<{ export_id: string; status: string }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/exports`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function getExport(exportId: string): Promise<ExportInfo> {
  return request(`/exports/${encodeURIComponent(exportId)}`);
}

export function exportDownloadUrl(exportId: string): string {
  return `${API_PREFIX}/exports/${encodeURIComponent(exportId)}/download`;
}

// ---- evidence (B01: typed, no ID guessing) ------------------------------

export interface EvidenceFragmentData {
  page_number: number;
  bbox: number[] | null;
  block_id: string | null;
  table_id: string | null;
  cell_id: string | null;
  char_start: number | null;
  char_end: number | null;
  raw_text: string | null;
  source_artifact_id: string | null;
}

export interface EvidenceData {
  api_version: string;
  evidence_id: string;
  document_id: string;
  job_id?: string;
  baseline_id: string | null;
  result_revision?: number;
  page_number: number | null;
  bbox: number[] | null;
  char_start: number | null;
  char_end: number | null;
  raw_text: string | null;
  source_parser: string | null;
  source_locator: string | null;
  fragments: EvidenceFragmentData[];
  /** Text precision: offset | block | none (B02 split). */
  text_precision: "offset" | "block" | "none";
  /** Geometry precision: character | cell | line | block | page | none. */
  geometry_precision: "character" | "cell" | "line" | "block" | "page" | "none";
  /** Legacy field kept for older payloads. */
  location_precision?: string;
}

export function getEvidence(jobId: string, evidenceId: string): Promise<EvidenceData> {
  return request(`/jobs/${encodeURIComponent(jobId)}/evidence/${encodeURIComponent(evidenceId)}`);
}

// ---- issues (B12) ---------------------------------------------------------

export interface ParseIssueData {
  issue_id: string;
  job_id: string;
  scope: string;
  reason_code: string;
  source_ids: string[];
  detail: string;
  state: string;
  baseline_id: string | null;
  created_at: string;
}

export function getIssues(jobId: string): Promise<{ issues: ParseIssueData[]; total: number }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/issues`);
}

// ---- queue status (live-ops) ---------------------------------------------

export interface WorkerPresence {
  worker_id: string;
  pid: number | null;
  seconds_since_heartbeat: number;
  online: boolean;
}

export interface QueueStatus {
  api_version: string;
  data_dir: string;
  queued_jobs: number;
  running_jobs: number;
  workers: WorkerPresence[];
  online_worker_count: number;
  stale_after_seconds: number;
}

export function getQueueStatus(): Promise<QueueStatus> {
  return request("/queue/status");
}

// ---- artifacts (run details, design §7.7) ---------------------------------

export interface ArtifactInfo {
  artifact_id: string;
  relative_path: string;
  media_type: string;
  bytes: number | null;
}

export function listArtifacts(
  jobId: string,
): Promise<{ api_version: string; job_id: string; artifacts: ArtifactInfo[] }> {
  return request(`/jobs/${encodeURIComponent(jobId)}/artifacts`);
}
