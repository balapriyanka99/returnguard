#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path
import pandas as pd
from google.cloud import bigquery

CONTROLLED_TABLES = [
    "return_requests","fraud_labels","return_logistics",
    "return_operational_costs","synthetic_scenarios","product_attributes",
    "return_evidence","return_inspections","synthetic_network_links",
]
SOURCE_TABLES = [
    "source_order_items_snapshot","source_products_snapshot",
    "source_users_snapshot","source_orders_snapshot","source_events_snapshot",
]

def args():
    p=argparse.ArgumentParser()
    p.add_argument("--project-id", default="return-guard-506407")
    p.add_argument("--production-dataset", default="returnguard")
    p.add_argument("--staging-dataset", default="returnguard_rebuilt_stage")
    p.add_argument("--controlled-dir", default="data/generated_returnguard_rebuilt")
    p.add_argument("--snapshot-dir", default="data/generated_returnguard_rebuilt_snapshot/source_snapshot")
    return p.parse_args()

def is_null(v):
    try:
        return pd.isna(v)
    except Exception:
        return False

def normalize(df, schema):
    out=df.copy()
    for f in schema:
        n=f.name
        if n not in out.columns: continue
        vals=out[n].tolist()
        t=f.field_type.upper()

        if f.mode == "REPEATED":
            def rep(v):
                if is_null(v): return []
                if isinstance(v,list): return v
                s=str(v).strip()
                if not s: return []
                try:
                    x=json.loads(s)
                    return x if isinstance(x,list) else [x]
                except Exception:
                    return [s]
            out[n]=pd.Series([rep(v) for v in vals],dtype="object")
        elif t=="STRING":
            out[n]=pd.Series([None if is_null(v) else str(v) for v in vals],dtype="object")
        elif t in {"INTEGER","INT64"}:
            out[n]=pd.Series([None if is_null(v) else int(v) for v in vals],dtype="object")
        elif t in {"FLOAT","FLOAT64","NUMERIC","BIGNUMERIC"}:
            out[n]=pd.Series([None if is_null(v) else float(v) for v in vals],dtype="object")
        elif t in {"BOOLEAN","BOOL"}:
            def b(v):
                if is_null(v): return None
                if isinstance(v,str):
                    s=v.strip().lower()
                    if s in {"true","1","yes"}: return True
                    if s in {"false","0","no"}: return False
                return bool(v)
            out[n]=pd.Series([b(v) for v in vals],dtype="object")
        elif t in {"TIMESTAMP","DATETIME","DATE"}:
            parsed=pd.to_datetime(out[n],errors="raise",utc=(t=="TIMESTAMP"),format="mixed")
            if t=="DATE":
                out[n]=pd.Series([None if pd.isna(v) else v.date() for v in parsed],dtype="object")
            else:
                out[n]=pd.Series([None if pd.isna(v) else v.to_pydatetime() for v in parsed],dtype="object")
    return out

def ensure_dataset(client, project, dataset):
    ref=bigquery.Dataset(f"{project}.{dataset}")
    try:
        client.get_dataset(ref)
        print(f"Staging dataset exists: {project}.{dataset}")
    except Exception:
        ref.location="US"
        client.create_dataset(ref)
        print(f"Created staging dataset: {project}.{dataset}")

def load_one(client, a, table, csv_path):
    if not csv_path.exists():
        raise FileNotFoundError(f"Missing CSV: {csv_path}")
    prod_ref=f"{a.project_id}.{a.production_dataset}.{table}"
    stage_ref=f"{a.project_id}.{a.staging_dataset}.{table}"
    prod=client.get_table(prod_ref)
    schema=prod.schema
    df=pd.read_csv(csv_path)
    clean=normalize(df,schema)
    cols=[f.name for f in schema]
    missing=[c for c in cols if c not in clean.columns]
    if missing:
        raise RuntimeError(f"{table}: missing columns {missing}")
    clean=clean[cols]
    cfg=bigquery.LoadJobConfig(schema=schema,write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE)
    print(f"Loading {table}: {len(clean)} rows")
    client.load_table_from_dataframe(clean,stage_ref,job_config=cfg).result()
    loaded=client.get_table(stage_ref).num_rows
    if loaded!=len(clean):
        raise RuntimeError(f"{table}: local={len(clean)} BigQuery={loaded}")
    print(f"  PASS {loaded}")
    return loaded

def main():
    a=args()
    c=bigquery.Client(project=a.project_id)
    ensure_dataset(c,a.project_id,a.staging_dataset)
    counts={}
    for t in CONTROLLED_TABLES:
        counts[t]=load_one(c,a,t,Path(a.controlled_dir)/f"{t}.csv")
    for t in SOURCE_TABLES:
        counts[t]=load_one(c,a,t,Path(a.snapshot_dir)/f"{t}.csv")
    print("\nSTAGING LOAD COMPLETE")
    for t in CONTROLLED_TABLES+SOURCE_TABLES:
        print(f"{t:34s} {counts[t]:>10}")
    print(f"\nProduction {a.project_id}.{a.production_dataset} was not modified.")

if __name__=="__main__":
    main()
