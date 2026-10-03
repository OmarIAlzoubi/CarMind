# Local manufacturer source template

Keep vehicle-specific PDFs, page splits, manifests, and populated metadata under
`manufacturer_knowledge/<manufacturer>/<model>/<source>/`. That directory is
ignored by Git. The files in this template contain placeholders only.

1. Copy `manual_source.example.json` to `manual_source.json` in the private
   source directory. Replace every `REPLACE_WITH_...` value with verified source
   metadata. Keep `model_year` or `market` as `null` if the source does not
   establish them; do not infer them from a filename.
2. Put the canonical PDF and a section manifest in the private directory.
   `source_path` and `manifest_path` are paths relative to the CarMind project
   root. The manifest must give `pdf_path` (the canonical PDF filename),
   `page_count`, and contiguous `sections` with `index`, `title`, `page_start`,
   `page_end`, and `file`. Each section `file` is relative to the canonical
   PDF's directory. The section PDF must contain exactly the stated pages.
   For one section, the manifest shape is:

   ```json
   {"pdf_path":"manual.pdf","page_count":1,"sections":[
     {"index":1,"title":"REPLACE_WITH_SECTION_TITLE","page_start":1,
      "page_end":1,"file":"section.pdf"}]}
   ```

   Replace the page numbers and files with the actual document structure.
3. Copy `manual_facts.example.json` to `manual_facts.json` beside the source
   registry. Replace its source ID and add only facts checked against specific
   physical pages. These facts do not automatically become maintenance rules.
4. Build a local index with
   `python -m carmind.manufacturer_manual build --source <private-path>/manual_source.json`.
   Use the Python interpreter required by `AGENTS.md` and `PYTHONPATH=src`.
5. Start the local conversation with `--manual-source <private-path>/manual_source.json`.
   Alternatively set `CARMIND_MANUAL_SOURCE` to that registry path. If exactly
   one local `manual_source.json` exists, CarMind discovers it automatically.
   When switching sources, rebuild the index; a stale index is rejected.

Legacy structured maintenance packs use `sources.json` and
`maintenance_schedule.json` in a private directory. Their loader discovers a
single local pack or uses `CARMIND_KNOWLEDGE_DIRECTORY` / an explicit directory.
Manual facts alone never activate scheduled maintenance.

The selected source is matched to the stored vehicle make and model. A known
model-year or market mismatch prevents retrieval. Unknown applicability is
marked unverified. The template does not establish guidance for any vehicle.
