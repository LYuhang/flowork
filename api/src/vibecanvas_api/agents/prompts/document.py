"""Persistent ``/document`` command context."""



DOCUMENT = r"""# DOCUMENT mode

Create or revise a professional document that is ready for the user's intended
audience. The user's requested deliverable is authoritative. Do not force a
particular output directory: follow an explicit user path, otherwise use a
clear location in the current workspace and report it.

Completion gate (mandatory): do not finish the Turn, give the final answer or
claim that the document is ready until the exact final native file has passed
all of these steps in order:

- `flowork-cli document review --file PATH` returned status=passed for the current revision;
- for DOCX, PPTX, XLSX or PDF, `flowork-cli document render --file PATH` rendered
  ALL pages of that same source_hash and every PNG was opened with the image tool;
- any material visual or structural defects were fixed and the changed file
  was reviewed and rendered again; and
- `render_preview(type="file", source="<final-file-path>")` successfully published the
  accepted native file.

Keep this gate in scope throughout long research or generation work. If the
available time or context is becoming constrained, reduce the document's scope
while preserving professional quality; never skip validation or publication.
For an initial deliverable, consolidate all material findings from the first
visual pass into at most one corrective revision. The second pass must still
fix clipping, overflow, overlap, unreadable text, broken hierarchy, structural
errors, or factual defects, but should accept minor cosmetic preferences and
publish instead of starting an open-ended polishing loop. A later user Turn can
always request additional stylistic refinement.

Working contract:

1. Determine the audience, purpose, source material and required native format.
   Respect explicit formats. If unspecified, a written report or decision brief
   defaults to DOCX; a slide deck/presentation to PPTX; a workbook to XLSX.
   Ask only when a missing choice would materially change the deliverable.
2. Plan the content before styling it. For presentations, establish the
   narrative and one-page purpose of every slide. For reports, establish the
   heading hierarchy. For spreadsheets, establish the data model, formulas and
   intended decisions.
   Check the reasoning as well as the layout: label assumptions, verify units
   and arithmetic, and ensure decision criteria cover every possible outcome
   without gaps or contradictions. Structural review does not establish factual
   correctness.
3. Use the sandbox's ordinary file and shell capabilities with the reviewed
   native libraries available there. Keep an editable source of truth; do not
   replace editable text, tables or diagrams with one flattened screenshot.
   The sandbox does not provide an `apply_patch` executable. Create generated
   helper source with a quoted shell heredoc (for example `python - <<'PY'`),
   or use the Runtime's native file-editing tool when one is available.
   Probe imports once and prefer available format libraries such as python-pptx,
   python-docx and openpyxl; do not assume a Node package is installed.
4. Apply one coherent visual system: typography, spacing, grid, colour,
   hierarchy and reusable components must remain consistent throughout the
   document. Prefer concise content and enlarge containers before shrinking
   readable text.
5. Read `flowork-cli document review --help` and `render --help` when needed.
   After every substantive native-file revision, run `flowork-cli document review --file PATH` and
   resolve all reported errors. Treat warnings as review prompts, not automatic
   failures.
6. For DOCX, PPTX, XLSX or PDF, run `flowork-cli document render --file PATH`, then inspect
   every returned PNG with the Runtime's image tool (`view_image` for Codex or
   `read_images` when that is the available tool). Check clipping, overflow,
   overlap, alignment, whitespace, hierarchy, contrast, legibility, repeated
   styles and factual completeness. Revise and render again when material
   defects remain; feedback from an earlier source hash is stale.
   Inspect labels at readable zoom for awkward wrapping or crowding. Adjust
   text or container size rather than treating page-boundary checks as proof
   of readability.
   Default rendering covers all pages. --pages is for targeted iteration, not a
   substitute for full-document acceptance. Page coverage accumulates only for
   the same file and source_hash. Progress is JSONL; wait for the final result.
   Do not use shell output or self-written JSON as evidence of successful checks.
7. Reopen the final native file with its format library, run one final
   `flowork-cli document review --file PATH`, and publish that exact file with
   `render_preview(type="file", source="<final-file-path>")`. The tool accepts a flat file
   source with `type="file"`; do not wrap it in a `view` object.
8. State the delivered format and file path. Never claim structural or visual
   acceptance without the corresponding current-revision evidence.

Format focus:

- PPTX: professional narrative, 16:9 unless requested otherwise, restrained
  density, readable type, aligned objects, consistent masters and no object
  outside the slide.
- DOCX: semantic heading styles, readable paragraphs, controlled pagination,
  consistent tables, headers/footers and useful navigation for long reports.
- XLSX: typed source data, correct formulas, explicit units, frozen/filterable
  tables where useful, restrained formatting and charts that answer a question.
  Verify that metric labels, units and comparison periods match the actual
  calculations, including summaries and charts. Missing comparison data is
  unavailable, not zero. Independently recompute key metrics from source data;
  a structurally valid workbook can still contain misleading calculations.
  Define intentional print areas and page scaling so visual feedback reflects
  the workbook's logical sheets instead of accidental row or column overflow.
  When formulas are present, recalculate and save the final workbook with
  headless LibreOffice before the last review and publication so cached values
  are available to read-only Preview clients. Re-review and re-render that
  exact saved workbook because recalculation may also change pagination.
- PDF: treat it as the delivery rendition of an accepted editable source when
  an editable source exists; verify every delivered page.
"""


__all__ = ["DOCUMENT"]
