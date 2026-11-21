# cron/lib/import-exts.sh: the file extensions `gbrain import` takes (src/core/sync.ts walks .md AND .mdx). Sourced
# by jobs/nightly-sync.sh (the "anything new?" freshness gate) and lib/ingest-chunked.sh (the readback's expected
# list), so the gate and the import can never disagree about which files count (Prism 85fcff4bc647, card t_ab2fcba3).
GBRAIN_IMPORT_EXTS="md mdx"
