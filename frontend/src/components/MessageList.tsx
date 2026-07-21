// The scrolling chat transcript (message-list half of the chat UI).
//
// Renders user and system messages; clarification prompts (Req 1.5) and error
// messages (Req 1.6, 1.7) are visually distinguished via the message tone. The
// list auto-scrolls to the newest message as the transcript grows.

import { useEffect, useRef } from "react";

import type { ChatMessage } from "../conversation/messages";

export interface MessageListProps {
  messages: ChatMessage[];
}

export function MessageList({ messages }: MessageListProps) {
  const endRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages.length]);

  return (
    <ol className="message-list" aria-label="Conversation" aria-live="polite">
      {messages.length === 0 && (
        <li className="message-list__empty">
          Describe the plugin you want to build to get started.
        </li>
      )}
      {messages.map((message) => (
        <li
          key={message.id}
          className={`message message--${message.role} message--${message.tone}`}
          data-role={message.role}
          data-tone={message.tone}
        >
          {message.tone === "clarification" && (
            <span className="message__tag">Clarification needed</span>
          )}
          <span className="message__text">{message.text}</span>
        </li>
      ))}
      <div ref={endRef} />
    </ol>
  );
}
