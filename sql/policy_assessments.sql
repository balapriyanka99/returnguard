CREATE TABLE IF NOT EXISTS `return-guard-506407.returnguard.policy_assessments` (
  policy_event_id STRING NOT NULL,
  return_id STRING NOT NULL,
  assessment_id STRING,
  assessment_at TIMESTAMP NOT NULL,
  policy_version STRING NOT NULL,
  matched_rule STRING NOT NULL,
  action STRING NOT NULL,
  normalized_reason STRING,
  return_fee NUMERIC,
  fee_reason STRING,
  economics_json JSON NOT NULL,
  pricing_json JSON,
  rationale ARRAY<STRING>,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(assessment_at)
CLUSTER BY return_id, assessment_id;
