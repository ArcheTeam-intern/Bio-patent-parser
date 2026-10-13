"""Review HTML export.

All raw text is HTML-escaped before it is embedded, so document content can
never be interpreted as markup or script. The page shows, for each review
item, its reason codes and the candidate values with their evidence ids.
"""

from __future__ import annotations

import html
from typing import Any


def _escape(text: Any) -> str:
    return html.escape(str(text or ""))


def render_review_html(
    review_items: list[dict[str, Any]],
    *,
    title: str = "Bio PDF Review",
) -> str:
    """Render a static review page for a list of review-item dicts."""

    rows: list[str] = []
    for item in review_items:
        reason_codes = ", ".join(_escape(r) for r in item.get("reason_codes", []))
        evidence_ids = ", ".join(_escape(e) for e in item.get("evidence_ids", []))
        record_id = _escape(item.get("record_id", ""))
        rows.append(
            f"<tr>"
            f"<td>{record_id}</td>"
            f"<td>{reason_codes}</td>"
            f"<td>{evidence_ids}</td>"
            f"</tr>"
        )

    body = "\n".join(rows)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{_escape(title)}</title>
</head>
<body>
  <h1>{_escape(title)}</h1>
  <table border="1">
    <thead>
      <tr><th>record</th><th>reasons</th><th>evidence</th></tr>
    </thead>
    <tbody>
      {body}
    </tbody>
  </table>
</body>
</html>
"""


__all__ = ["render_review_html"]