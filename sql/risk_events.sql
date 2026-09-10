CREATE TABLE IF NOT EXISTS `return-guard-506407.returnguard.risk_events` (
  risk_event_id STRING NOT NULL,
  return_id STRING NOT NULL,
  assessment_id STRING,
  assessment_at TIMESTAMP NOT NULL,
  engine_version STRING NOT NULL,
  score INT64,
  band STRING NOT NULL,
  coverage STRING NOT NULL,
  evaluable_domains ARRAY<STRING> NOT NULL,
  group_scores_json JSON NOT NULL,
  product_mitigation INT64 NOT NULL,
  reasons_json JSON NOT NULL,
  patterns ARRAY<STRING> NOT NULL,
  limitations ARRAY<STRING> NOT NULL,
  data_origin STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(assessment_at)
CLUSTER BY return_id, assessment_id;
