#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from datetime import datetime, timezone
from pathlib import Path
from google.cloud import bigquery

CONTROLLED = [
    "return_requests","synthetic_scenarios","return_evidence",
    "return_inspections","return_logistics","return_operational_costs",
    "synthetic_network_links"
]
SOURCE = [
    "source_order_items_snapshot","source_orders_snapshot",
    "source_users_snapshot","source_products_snapshot","source_events_snapshot"
]

def clean(row):
    if row is None: return {}
    out={}
    for k,v in dict(row.items()).items():
        out[k]=v.isoformat() if hasattr(v,"isoformat") else v
    return out

def rows(client, sql, params=None):
    cfg=bigquery.QueryJobConfig(query_parameters=params or [])
    return [clean(r) for r in client.query(sql, job_config=cfg).result()]

def cols(client, table):
    try: return {f.name for f in client.get_table(table).schema}
    except Exception: return set()

def exists(client, table):
    try: client.get_table(table); return True
    except Exception: return False

def print_block(title, data, limit=5):
    print("\n"+"="*90); print(title); print("="*90)
    if not data:
        print("(no rows)"); return
    for i,row in enumerate(data[:limit],1):
        print(f"\nRow {i}")
        for k,v in row.items(): print(f"  {k}: {v}")
    if len(data)>limit: print(f"... {len(data)-limit} more row(s) omitted")

def first(row, names):
    for n in names:
        if row.get(n) not in (None,""): return row[n]
    return None

def main():
    p=argparse.ArgumentParser()
    p.add_argument("--return-id", required=True)
    p.add_argument("--project", default="return-guard-506407")
    p.add_argument("--dataset", default="returnguard")
    p.add_argument("--output")
    a=p.parse_args()
    c=bigquery.Client(project=a.project)
    ds=f"{a.project}.{a.dataset}"

    report={"generated_at":datetime.now(timezone.utc).isoformat(),
            "mode":"read_only","return_id":a.return_id,"sections":{}}
    print("ReturnGuard case inspection — READ ONLY")
    print("return_id:", a.return_id)

    inv=rows(c,f"""SELECT table_id,row_count,size_bytes,
    TIMESTAMP_MILLIS(last_modified_time) last_modified
    FROM `{ds}.__TABLES__` ORDER BY table_id""")
    report["sections"]["table_inventory"]=inv
    print_block("1. TABLE INVENTORY", inv, 100)

    controlled={}
    for t in CONTROLLED:
        full=f"{ds}.{t}"
        if exists(c,full) and "return_id" in cols(c,full):
            controlled[t]=rows(c,f"SELECT * FROM `{full}` WHERE return_id=@rid",
                [bigquery.ScalarQueryParameter("rid","STRING",a.return_id)])
        else: controlled[t]=[]
    report["sections"]["controlled"]=controlled

    for t in CONTROLLED:
        print_block(f"CONTROLLED: {t} ({len(controlled[t])} row(s))", controlled[t], 10)

    if not controlled["return_requests"]:
        raise SystemExit("return_id not found in return_requests")

    rr=controlled["return_requests"][0]
    user_id=first(rr,["user_id","customer_id"])
    order_id=first(rr,["order_id"])
    item_id=first(rr,["order_item_id","source_order_item_id","item_id"])
    product_id=first(rr,["product_id"])
    ids={"user_id":user_id,"order_id":order_id,"order_item_id":item_id,"product_id":product_id}
    report["sections"]["resolved_ids"]=ids
    print("\nResolved IDs:", ids)

    src={}
    mapping={
        "source_order_items_snapshot":item_id,
        "source_orders_snapshot":order_id,
        "source_users_snapshot":user_id,
        "source_products_snapshot":product_id,
    }
    for t,val in mapping.items():
        full=f"{ds}.{t}"
        if val is not None and exists(c,full) and "id" in cols(c,full):
            src[t]=rows(c,f"SELECT * FROM `{full}` WHERE id=@id",
                        [bigquery.ScalarQueryParameter("id","INT64",int(val))])
        else: src[t]=[]

    # bounded source events for this user
    ev=f"{ds}.source_events_snapshot"
    if user_id is not None and exists(c,ev) and "user_id" in cols(c,ev):
        order_by="created_at" if "created_at" in cols(c,ev) else "id"
        src["source_events_snapshot"]=rows(c,f"""SELECT * FROM `{ev}`
            WHERE user_id=@uid ORDER BY {order_by} DESC LIMIT 20""",
            [bigquery.ScalarQueryParameter("uid","INT64",int(user_id))])
    else: src["source_events_snapshot"]=[]

    report["sections"]["source"]=src
    for t in SOURCE:
        print_block(f"SOURCE: {t} ({len(src.get(t,[]))} row(s))", src.get(t,[]), 10)

    # product attributes
    pa=f"{ds}.product_attributes"
    if product_id is not None and exists(c,pa) and "product_id" in cols(c,pa):
        pa_rows=rows(c,f"SELECT * FROM `{pa}` WHERE product_id=@pid",
                     [bigquery.ScalarQueryParameter("pid","INT64",int(product_id))])
    else: pa_rows=[]
    report["sections"]["product_attributes"]=pa_rows
    print_block("RETURNGUARD PRODUCT ATTRIBUTES", pa_rows, 10)

    # customer frozen history
    oi=f"{ds}.source_order_items_snapshot"
    if user_id is not None and exists(c,oi) and "user_id" in cols(c,oi):
        order_by="created_at" if "created_at" in cols(c,oi) else "id"
        hist=rows(c,f"""SELECT * FROM `{oi}` WHERE user_id=@uid
            ORDER BY {order_by}""",
            [bigquery.ScalarQueryParameter("uid","INT64",int(user_id))])
    else: hist=[]
    report["sections"]["customer_history"]=hist
    print_block(f"FROZEN CUSTOMER HISTORY ({len(hist)} row(s))", hist, 15)

    summary={
        "scenario_rows":len(controlled["synthetic_scenarios"]),
        "evidence_rows":len(controlled["return_evidence"]),
        "inspection_rows":len(controlled["return_inspections"]),
        "logistics_rows":len(controlled["return_logistics"]),
        "cost_rows":len(controlled["return_operational_costs"]),
        "network_rows":len(controlled["synthetic_network_links"]),
        "source_order_item_found":bool(src["source_order_items_snapshot"]),
        "source_order_found":bool(src["source_orders_snapshot"]),
        "source_user_found":bool(src["source_users_snapshot"]),
        "source_product_found":bool(src["source_products_snapshot"]),
        "source_event_rows":len(src["source_events_snapshot"]),
        "source_customer_history_rows":len(hist),
    }
    report["sections"]["summary"]=summary
    print("\n"+"="*90+"\nSUMMARY\n"+"="*90)
    for k,v in summary.items(): print(f"{k}: {v}")

    out=Path(a.output or f"data/generated_returnguard/case_inspection_{a.return_id}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report,indent=2,default=str),encoding="utf-8")
    print("\nJSON report:", out)

if __name__=="__main__":
    main()
