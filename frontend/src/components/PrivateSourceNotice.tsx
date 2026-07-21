// Private-source usage-restriction notice (Req 25.6).
//
// When the active draft was forked from the private production repository the
// backend supplies a usage-restriction notice on the session state; the chat
// surfaces it prominently. Rendering nothing when there is no notice keeps the
// banner out of net-new / public-source sessions.

export interface PrivateSourceNoticeProps {
  notice: string | null;
}

export function PrivateSourceNotice({ notice }: PrivateSourceNoticeProps) {
  if (!notice) {
    return null;
  }
  return (
    <div className="private-source-notice" role="note" data-testid="private-source-notice">
      <span className="private-source-notice__badge">Private source</span>
      <span className="private-source-notice__text">{notice}</span>
    </div>
  );
}
