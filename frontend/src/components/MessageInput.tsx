// The message input (input half of the chat UI).
//
// Enforces the same length bounds as the backend (Req 1.1) for immediate
// feedback, shows a live character count as the limit is approached, and clears
// on a successful submit. Submission is delegated to the parent, which performs
// the authoritative validation + send.

import { useState, type FormEvent, type KeyboardEvent } from "react";

import { MAX_MESSAGE_LENGTH } from "../validation";

export interface MessageInputProps {
  /** Submit the raw text; returns whether the parent accepted it. */
  onSubmit: (text: string) => { accepted: boolean; reason?: string };
  /** Disable input while the connection is not open. */
  disabled?: boolean;
}

export function MessageInput({ onSubmit, disabled = false }: MessageInputProps) {
  const [text, setText] = useState("");

  const trimmedEmpty = text.trim().length === 0;
  const overLimit = text.length > MAX_MESSAGE_LENGTH;
  const canSubmit = !disabled && !trimmedEmpty && !overLimit;

  const doSubmit = () => {
    if (!canSubmit) {
      return;
    }
    const result = onSubmit(text);
    if (result.accepted) {
      setText("");
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    doSubmit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter submits; Shift+Enter inserts a newline.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      doSubmit();
    }
  };

  const remaining = MAX_MESSAGE_LENGTH - text.length;
  const showCount = text.length > MAX_MESSAGE_LENGTH - 500;

  return (
    <form className="message-input" onSubmit={handleSubmit}>
      <label className="message-input__label" htmlFor="message-input-field">
        Describe your plugin or the change you want
      </label>
      <textarea
        id="message-input-field"
        className="message-input__field"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={handleKeyDown}
        placeholder="e.g. Add an action that lists users with pagination"
        rows={3}
        disabled={disabled}
        aria-invalid={overLimit}
      />
      <div className="message-input__footer">
        {showCount && (
          <span
            className={`message-input__count ${overLimit ? "message-input__count--over" : ""}`}
            data-testid="char-remaining"
          >
            {remaining.toLocaleString()} characters left
          </span>
        )}
        <button
          type="submit"
          className="message-input__submit"
          disabled={!canSubmit}
          data-testid="send-button"
        >
          Send
        </button>
      </div>
    </form>
  );
}
