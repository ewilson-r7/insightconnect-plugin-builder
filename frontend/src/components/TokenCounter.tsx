// Live cumulative session token counter (Req 3.6).
//
// Displays the cumulative session token total as a non-negative integer that
// reflects the sum of recorded LLM invocations. The value is driven by the
// `tokens`/`turn` WebSocket frames folded in `useConversation`.

export interface TokenCounterProps {
  total: number;
}

export function TokenCounter({ total }: TokenCounterProps) {
  // Guard against any transient negative/NaN value so the display always shows
  // a non-negative integer (Req 3.6).
  const safeTotal = Number.isFinite(total) && total > 0 ? Math.floor(total) : 0;
  return (
    <div className="token-counter" aria-label="Cumulative session token usage">
      <span className="token-counter__label">Tokens used</span>
      <span className="token-counter__value" data-testid="token-total">
        {safeTotal.toLocaleString()}
      </span>
    </div>
  );
}
