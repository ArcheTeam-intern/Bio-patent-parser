# Biological PDF parser research and source acquisition

## Goal
Compare open-source PDF parsers with Tencent WeKnora for biological papers and patents, acquire a suitable upstream repository locally, and propose an accuracy-oriented workflow.

## Phases
- [complete] Inspect official documentation and parser source; compare architecture, provenance, OCR, tables, and license constraints.
- [complete] Select and clone the preferred upstream into a separate directory in this workspace.
- [complete] Record the comparison, proposed workflow, and limits of available evidence.
- [complete] Verify checkout identity and report paths and next steps.

## Scope
Research, source acquisition, and design only. Implementation and performance benchmarking are later work.

## Detailed plan handoff requested by user
- [complete] Verify workflow framework choices and current parser API contracts.
- [complete] Write a self-contained architecture specification covering routing, evidence, sequences, activities, review, and evaluation.
- [complete] Write phased implementation tasks with file boundaries, concrete acceptance fixtures, and migration rules.
- [complete] Review the documents for consistent contracts and complete handoff; open the main plan.

## Errors
- Initial planning skill path used the wrong root; resolved to C:/Users/SWQ/.agents/skills/planning-with-files/SKILL.md.
- GitHub API tree request hit an unauthenticated shared-IP rate limit; use raw files and normal Git transport instead.
- Guessed WeKnora requirements.txt and docling_parser.py paths returned 404. Official parsing documentation and existing source files supplied the necessary architecture evidence.
- A guessed Docling custom conversion URL returned 404; resolved via repository examples and current documentation.
