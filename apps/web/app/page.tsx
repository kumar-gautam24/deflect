"use client";

import { type FormEvent, useState } from "react";

import { AnswerPanel } from "@/components/answer-panel";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { type AskDone, askStream } from "@/lib/api";

const EXAMPLES = [
  "How do I declare a dependency?",
  "How do I containerise a FastAPI application?",
  "How much does a FastAPI support licence cost?",
];

export default function AskPage() {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [done, setDone] = useState<AskDone | null>(null);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function ask(text: string) {
    setAnswer("");
    setDone(null);
    setError(null);
    setPending(true);

    try {
      for await (const event of askStream(text)) {
        if (event.type === "token") setAnswer((current) => current + event.text);
        else setDone(event);
      }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "the request failed");
    } finally {
      setPending(false);
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    void ask(question);
  }

  return (
    <main className="mx-auto max-w-3xl space-y-6 p-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Deflect</h1>
        <p className="text-muted-foreground text-sm">
          Answers FastAPI questions from the documentation, and escalates when it should not guess.
        </p>
      </header>

      <form onSubmit={submit} className="flex gap-2">
        <Input
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask a FastAPI question"
        />
        <Button type="submit" disabled={pending || !question}>
          {pending ? "Asking" : "Ask"}
        </Button>
      </form>

      <div className="flex flex-wrap gap-2">
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => {
              setQuestion(example);
              void ask(example);
            }}
            className="text-muted-foreground hover:text-foreground rounded-full border px-3 py-1 text-xs"
          >
            {example}
          </button>
        ))}
      </div>

      {error && <p className="text-sm text-red-500">{error}</p>}
      {(answer || done) && <AnswerPanel answer={answer} done={done} />}
    </main>
  );
}
