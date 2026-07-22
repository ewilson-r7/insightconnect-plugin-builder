// The message input with file attachment support.
//
// Enforces length bounds (Req 1.1) for immediate feedback, shows a live
// character count as the limit is approached, supports attaching API spec files
// (JSON, YAML) that get sent alongside the message for the LLM to digest, and
// clears on a successful submit.

import { useRef, useState, type FormEvent, type KeyboardEvent } from "react";

import type { MessageAttachment } from "../types";
import { MAX_MESSAGE_LENGTH } from "../validation";

/** Accepted file extensions for API spec attachments. */
const ACCEPTED_EXTENSIONS = ".json,.yaml,.yml,.txt,.md";

export interface MessageInputProps {
  /** Submit the raw text + attachments; returns whether the parent accepted it. */
  onSubmit: (
    text: string,
    attachments?: MessageAttachment[],
  ) => { accepted: boolean; reason?: string };
  /** Disable input while the connection is not open. */
  disabled?: boolean;
}

export function MessageInput({ onSubmit, disabled = false }: MessageInputProps) {
  const [text, setText] = useState("");
  const [attachments, setAttachments] = useState<MessageAttachment[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const trimmedEmpty = text.trim().length === 0 && attachments.length === 0;
  const overLimit = text.length > MAX_MESSAGE_LENGTH;
  const canSubmit = !disabled && !trimmedEmpty && !overLimit;

  const doSubmit = () => {
    if (!canSubmit) {
      return;
    }
    const result = onSubmit(text, attachments.length > 0 ? attachments : undefined);
    if (result.accepted) {
      setText("");
      setAttachments([]);
    }
  };

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    doSubmit();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      doSubmit();
    }
  };

  const handleFileSelect = async (event: React.ChangeEvent<HTMLInputElement>) => {
    const files = event.target.files;
    if (!files) return;

    const newAttachments: MessageAttachment[] = [];
    for (const file of Array.from(files)) {
      try {
        const content = await file.text();
        newAttachments.push({ name: file.name, content });
      } catch {
        // Skip unreadable files silently.
      }
    }
    setAttachments((prev) => [...prev, ...newAttachments]);
    // Reset the file input so the same file can be re-selected.
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  };

  const removeAttachment = (index: number) => {
    setAttachments((prev) => prev.filter((_, i) => i !== index));
  };

  const remaining = MAX_MESSAGE_LENGTH - text.length;
  const showCount = text.length > MAX_MESSAGE_LENGTH - 2000;

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

      {attachments.length > 0 && (
        <div className="message-input__attachments" data-testid="attachments">
          {attachments.map((att, index) => (
            <span key={`${att.name}-${index}`} className="message-input__chip">
              <span className="message-input__chip-name">{att.name}</span>
              <button
                type="button"
                className="message-input__chip-remove"
                onClick={() => removeAttachment(index)}
                aria-label={`Remove ${att.name}`}
              >
                &times;
              </button>
            </span>
          ))}
        </div>
      )}

      <div className="message-input__footer">
        <button
          type="button"
          className="message-input__attach"
          onClick={() => fileInputRef.current?.click()}
          disabled={disabled}
          title="Attach an API spec file (JSON, YAML)"
          aria-label="Attach file"
          data-testid="attach-button"
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none" aria-hidden="true">
            <path
              d="M14 9.5V13a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V3a2 2 0 0 1 2-2h4.5M14 9.5L9 4.5M14 9.5H10a1 1 0 0 1-1-1V4.5M9 4.5V1"
              stroke="currentColor"
              strokeWidth="1.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            />
          </svg>
        </button>
        <input
          ref={fileInputRef}
          type="file"
          accept={ACCEPTED_EXTENSIONS}
          multiple
          onChange={handleFileSelect}
          className="message-input__file-input"
          tabIndex={-1}
          aria-hidden="true"
        />

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
