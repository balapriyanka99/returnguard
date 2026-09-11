CREATE TABLE IF NOT EXISTS `return-guard-506407.returnguard.return_audit_log` (
  audit_event_id STRING NOT NULL,
  return_id STRING NOT NULL,
  assessment_id STRING,
  event_type STRING NOT NULL,
  event_at TIMESTAMP NOT NULL,
  actor_type STRING NOT NULL,
  actor_name STRING,
  status_from STRING,
  status_to STRING,
  summary STRING,
  details_json JSON,
  trace_id STRING,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(event_at)
CLUSTER BY return_id, assessment_id;
