# Annotation Guidelines (v1)

The benchmark ground truth is **human-confirmed** annotations only. A model
may propose a draft, but a draft is excluded from the official accuracy
denominator until a person marks it `human_verified` and sets `status:
verified`.

## Process

1. **Render the source page** and read the original text/image. Never
   annotate from a model's output alone.
2. For each object on the page, write one `Annotation` record.
3. Mark unreadable objects `kind: unreadable` — they stay in coverage, they
   are NOT silently deleted from the denominator.

## Sequences

- Record `seq_id_no`, `sequence` (normalised, one-letter), and `molecule_type`.
- Exact match requires the sequence to be **identical**, residue by residue.
  Same length with different residues is `exact match = 0`, but still counts
  as *detected* (recall) — the character error rate is reported separately.
- Unknown residues are annotated explicitly; do not guess.

## Measurements

- Record `metric`, `entity_raw`, `target_raw`, `value_raw`, `unit_raw`.
- A measurement is `correct` only when **entity + target + metric** all line
  up. A value that is right but assigned to the wrong entity is a miss.

## Split

- Divide pages into a **dev** set and a **holdout** set (at least 1/3
  holdout). Same-family documents must not leak between the two sets.

## Honesty rules

- ≈30 pages is indicative, not statistical proof. Do not claim "99% correct".
- `auto_accept` stays off; acceptance needs an external reference or human.
