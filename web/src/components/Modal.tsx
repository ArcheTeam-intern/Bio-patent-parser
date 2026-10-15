import { ReactNode, useEffect, useRef } from "react";

/**
 * 原生模态对话框：``showModal()`` 将其置于顶层层叠，PDF 画布、
 * 证据高亮或任何页内元素都不可能遮挡它（此前用 ``<dialog open>``
 * 内联在页面布局里，会被 PDF 列盖住）。Esc 与点击遮罩都会关闭，
 * 并通过原生 close 事件同步父组件状态。
 */
export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  width?: number;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;

  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    if (open && !dlg.open) {
      dlg.showModal();
    } else if (!open && dlg.open) {
      dlg.close();
    }
  }, [open]);

  // Esc / native close keeps the parent's open state in sync.
  useEffect(() => {
    const dlg = ref.current;
    if (!dlg) return;
    const handler = () => onCloseRef.current();
    dlg.addEventListener("close", handler);
    return () => dlg.removeEventListener("close", handler);
  }, []);

  if (!open) return null;

  return (
    <dialog
      ref={ref}
      style={width ? { width } : undefined}
      onClick={(e) => {
        if (e.target === ref.current) onCloseRef.current();
      }}
    >
      <div className="dialog-title">{title}</div>
      <div className="dialog-body">{children}</div>
      {footer && <div className="dialog-actions">{footer}</div>}
    </dialog>
  );
}