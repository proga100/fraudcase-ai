# Context Grounding evidence — `fraudcase-ai-evidence`

Source documents for the UiPath Context Grounding index used by FraudCase AI.
Generated from `demo_dataset/` by `uipath/build_evidence.py` (re-run to regenerate).

## Files

- **`evidence.jsonl`** — one JSON object per invoice: `invoice_id`, `title`,
  `content` (rich natural-language description for semantic search), plus
  `vendor_name`, `department`, `category`, `amount`, `invoice_date`, `fraud_label`.
- **`evidence.csv`** — same data, flat columns (use whichever ingestion path the
  index connector prefers).

Every document carries `invoice_id` in its `id`, `title`, **and** `content`, so a
Context Grounding search result maps straight back to an invoice/finding regardless
of how the connector exposes metadata.

## Upload steps

1. In the tenant, create an index named exactly **`fraudcase-ai-evidence`**
   (matches `UIPATH_CONTEXT_GROUNDING_INDEX_NAME`).
2. Upload `evidence.jsonl` (or `evidence.csv`) to the index's storage bucket /
   data source and ingest. Context Grounding owns the embeddings + vector index.
3. The coded agent's `investigate` step calls `context_grounding.search(name=
   "fraudcase-ai-evidence", query=<objective>, ...)`; the `invoice_id` + `score`
   from each hit feed the detector suite.

To produce one document file per invoice instead (some connectors prefer discrete
docs): `python uipath/build_evidence.py --per-file` → writes `docs/<invoice_id>.md`.

1,558 invoices (290 fraud-labelled), 60 vendors, 4 policies.
