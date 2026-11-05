"use client";

/** The paper-detail drawer's abstract body. */

/** A structured-abstract section label like "IMPORTANCE:" or "RESULTS:" — an
 *  all-caps run (≥6 letters, so in-text acronyms don't match) at a sentence
 *  boundary, immediately followed by a colon and a space. */
const SECTION_LABEL = /(?:^|(?<=[.\s]))([A-Z][A-Z][A-Z][A-Z][A-Z][A-Z &,/()-]*):(?=\s)/g;

type Segment = { label: string | null; body: string };

/** Split a structured abstract into labelled sections; a plain abstract comes
 *  back as a single unlabelled segment. */
function splitAbstract(text: string): Segment[] {
  const segments: Segment[] = [];
  let lastIndex = 0;
  let label: string | null = null;
  SECTION_LABEL.lastIndex = 0;
  for (let m = SECTION_LABEL.exec(text); m; m = SECTION_LABEL.exec(text)) {
    const body = text.slice(lastIndex, m.index).trim();
    if (body) segments.push({ label, body });
    label = m[1];
    lastIndex = SECTION_LABEL.lastIndex;
  }
  const tail = text.slice(lastIndex).trim();
  if (tail) segments.push({ label, body: tail });
  return segments;
}

/** The abstract body: labelled sections when the source is structured
 *  ("IMPORTANCE: … METHODS: …"), else a flat line-clamped paragraph. Shows the
 *  first two segments collapsed, with a Read-more toggle. */
export function Abstract({
  text,
  expanded,
  onToggle,
}: {
  text: string;
  expanded: boolean;
  onToggle: () => void;
}) {
  const segments = splitAbstract(text);
  const structured = segments.length >= 2 && segments.some((s) => s.label);
  const canToggle = text.length > 400 || (structured && segments.length > 2);

  return (
    <div className="text-xs leading-relaxed text-foreground/80">
      {structured ? (
        (expanded ? segments : segments.slice(0, 2)).map((s, i) => (
          <p key={i} className={i === 0 ? "" : "mt-2"}>
            {s.label && (
              <span className="font-semibold text-foreground">{s.label}: </span>
            )}
            {s.body}
          </p>
        ))
      ) : (
        <p className={expanded ? "" : "line-clamp-6"}>{text}</p>
      )}
      {canToggle && (
        <button
          type="button"
          onClick={onToggle}
          className="mt-1 text-xs font-medium text-foreground hover:underline"
        >
          {expanded ? "Show less" : "Read more"}
        </button>
      )}
    </div>
  );
}
