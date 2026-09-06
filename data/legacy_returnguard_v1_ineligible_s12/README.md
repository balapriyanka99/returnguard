# Legacy V1 S12 provenance archive

This archive preserves the original V1 records for `RTN-S12-001` and
`RTN-S12-005` exactly as recorded in Git commit `2b125d8`.

They are **LEGACY_V1**, not ACTIVE demonstration cases: current point-in-time
source verification found no qualifying historical returned item for products
`24767` and `19603`, respectively. Their active rows and exclusive child rows
are therefore excluded from `data/generated_returnguard`.

The archive JSON retains the exact stable identities and request-level V1
provenance, while `source_commit` points to the immutable Git artifact that
contains the complete original child rows. It is provenance only and must not
be loaded as the active application dataset.
