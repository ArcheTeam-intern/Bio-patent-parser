import { useCallback, useEffect, useRef, useState } from "react";
import { EvidenceData, getEvidence } from "../../api/client";

/**
 * Evidence loading with cancellation (plan B01).
 *
 * Clicking A then B must never let A's slow response overwrite B's target:
 * every load carries a generation counter; stale responses are dropped.
 * The hook returns the resolved evidence together with the request state so
 * the UI can show honest errors instead of silently empty highlights.
 *
 * Location comes ONLY from the API payload (fragment bbox/page or span
 * bbox/page) — ids are opaque and are never parsed for a page number.
 */

export interface EvidenceTarget {
  page: number | null;
  bbox: [number, number, number, number] | null;
}

export interface EvidenceState {
  evidence: EvidenceData | null;
  target: EvidenceTarget | null;
  loading: boolean;
  error: string | null;
  /** Switch to one of the record's evidence ids. */
  select: (evidenceId: string) => void;
  clear: () => void;
  next: () => void;
  prev: () => void;
  position: { index: number; total: number };
}

function targetFrom(span: EvidenceData, fragmentIndex: number): EvidenceTarget {
  const fragments = span.fragments ?? [];
  const fragment = fragments[fragmentIndex] ?? fragments[0] ?? null;
  const page = fragment?.page_number ?? span.page_number ?? null;
  const bboxRaw = fragment?.bbox ?? span.bbox;
  return {
    page,
    bbox: bboxRaw ? (bboxRaw as [number, number, number, number]) : null,
  };
}

export function useEvidence(jobId: string, evidenceIds: string[], selectionKey?: string | null): EvidenceState {
  const [evidence, setEvidence] = useState<EvidenceData | null>(null);
  const [target, setTarget] = useState<EvidenceTarget | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  // Monotonic generation: only the newest request may apply.
  const generation = useRef(0);
  const clear = useCallback(() => {
    ++generation.current;
    setEvidence(null);
    setTarget(null);
    setLoading(false);
    setError(null);
    setActiveId(null);
  }, []);

  const load = useCallback(
    async (evidenceId: string, fragmentIndex: number) => {
      const mine = ++generation.current;
      setLoading(true);
      setError(null);
      setEvidence(null);
      setTarget(null);
      setActiveId(evidenceId);
      try {
        const span = await getEvidence(jobId, evidenceId);
        if (mine !== generation.current) return; // a newer click won
        setEvidence(span);
        setTarget(targetFrom(span, fragmentIndex));
      } catch (e) {
        if (mine !== generation.current) return;
        setEvidence(null);
        setTarget(null);
        setError(`证据加载失败：${String(e)}`);
      } finally {
        if (mine === generation.current) setLoading(false);
      }
    },
    [jobId],
  );

  const select = useCallback(
    (evidenceId: string) => {
      void load(evidenceId, 0);
    },
    [load],
  );

  const total = evidenceIds.length;
  const index = activeId ? evidenceIds.indexOf(activeId) : -1;

  const move = useCallback(
    (delta: number) => {
      if (total === 0) return;
      const base = index === -1 ? 0 : index + delta;
      const wrapped = ((base % total) + total) % total;
      void load(evidenceIds[wrapped], 0);
    },
    [evidenceIds, index, load, total],
  );

  // Auto-select the first evidence when the record (its id set) changes.
  const idsKey = evidenceIds.join("|");
  useEffect(() => {
    clear();
    if (evidenceIds.length > 0) {
      void load(evidenceIds[0], 0);
    }
    return () => { ++generation.current; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jobId, idsKey, selectionKey, load, clear]);

  return {
    evidence,
    target,
    loading,
    error,
    select,
    clear,
    next: () => move(1),
    prev: () => move(-1),
    position: { index: index + 1, total },
  };
}
