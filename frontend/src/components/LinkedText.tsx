import { isKnownLink, splitLinks } from "../linkText";

// A chat message's text with its web links clickable (opened in a new tab).
// `known` (her messages only): the addresses this reply's searches really
// returned - any other link of hers gets a small "unchecked" mark, since it
// came from her memory and may be wrong or out of date.

export function LinkedText({ text, known }: { text: string; known?: Set<string> }) {
  const parts = splitLinks(text);
  // translate="no": chat text is never swapped by the page translator (i18n.ts).
  if (!parts.some((p) => p.href)) return <span translate="no">{text}</span>;
  return (
    <>
      {parts.map((part, i) =>
        part.href ? (
          <span key={i} className="chat-link-wrap">
            <a className="chat-link" href={part.href} target="_blank" rel="noopener noreferrer"
              title={part.href} translate="no">
              {part.text}
            </a>
            {known && !isKnownLink(part.href, known) && (
              <span className="chat-link-unchecked"
                title="She didn't find this link in a search for this reply - it comes from her memory and may be wrong or out of date">
                unchecked
              </span>
            )}
          </span>
        ) : (
          <span key={i} translate="no">{part.text}</span>
        ),
      )}
    </>
  );
}
