CREATE TABLE IF NOT EXISTS `return-guard-506407.returnguard.return_decisions` (
  decision_event_id STRING NOT NULL,
  return_id STRING NOT NULL,
  assessment_id STRING,
  assessment_at TIMESTAMP NOT NULL,
  decision_version STRING NOT NULL,
  recommended_action STRING NOT NULL,
  matched_policy_rule STRING NOT NULL,
  risk_score INT64,
  risk_band STRING NOT NULL,
  risk_coverage STRING NOT NULL,
  decision_summary STRING NOT NULL,
  strongest_evidence ARRAY<STRING>,
  mitigating_context ARRAY<STRING>,
  network_context_json JSON,
  economics_summary_json JSON NOT NULL,
  pricing_json JSON,
  policy_reasoning ARRAY<STRING>,
  limitations ARRAY<STRING>,
  specialists_used ARRAY<STRING>,
  data_origin STRING,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(assessment_at)
CLUSTER BY return_id, assessment_id;
