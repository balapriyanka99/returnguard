CREATE TABLE IF NOT EXISTS `return-guard-506407.returnguard.economics_assessments` (
  economics_event_id STRING NOT NULL,
  return_id STRING NOT NULL,
  assessment_id STRING,
  assessment_at TIMESTAMP NOT NULL,
  economics_version STRING NOT NULL,
  current_item_value NUMERIC,
  product_cost NUMERIC,
  reverse_logistics_cost NUMERIC,
  inspection_cost NUMERIC,
  recovery_value NUMERIC,
  total_operational_cost NUMERIC,
  estimated_net_return_cost NUMERIC,
  estimated_loss_exposure NUMERIC,
  confidence STRING NOT NULL,
  data_origin STRING NOT NULL,
  created_at TIMESTAMP NOT NULL
)
PARTITION BY DATE(assessment_at)
CLUSTER BY return_id, assessment_id;
