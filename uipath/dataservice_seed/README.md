# Data Service seed CSVs

Demo data for the UiPath Data Service (Data Fabric) entities, generated from
`demo_dataset/` by `uipath/build_dataservice_csv.py`.

- `Vendor.csv` (60 rows)
- `Policy.csv` (4 rows)
- `Transaction.csv` (1,558 rows)

Column headers match the entity field display names, so Data Fabric's import maps
them automatically.

## Import (Data Fabric UI — uses your logged-in permissions)

Client-credential external apps are blocked from writing Data Service records
("unsupported robot type"), so seed through the UI, not the API:

1. Open **Data Fabric** → the entity → **Import data** (or **⋮ → Import**).
2. Upload the matching CSV; confirm the column→field mapping; run the import.
3. **Order matters** (Transaction references vendors/policies): import **Vendor** and
   **Policy** first, then **Transaction**.

The repo's `uipath/seed_data_service.py` is the API equivalent and works only if the
external application is granted Data Service entity access.
