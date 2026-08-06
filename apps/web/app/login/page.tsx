"use client";

import { useRouter } from "next/navigation";
import { type FormEvent, useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setPending(true);
    setError(null);

    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email, password }),
      });

      if (response.ok) {
        router.push("/traces");
        return;
      }

      if (response.status === 423) {
        const retryAfter = response.headers.get("Retry-After");
        const minutes = retryAfter ? Math.ceil(Number(retryAfter) / 60) : null;
        setError(
          minutes
            ? `locked, try again in ${minutes} minute${minutes === 1 ? "" : "s"}`
            : "account locked, try again later",
        );
        return;
      }

      const body = await response.json().catch(() => null);
      setError(body?.detail ?? "invalid email or password");
    } finally {
      setPending(false);
    }
  }

  return (
    <main className="mx-auto max-w-sm space-y-6 p-8">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold">Log in</h1>
      </header>

      <form onSubmit={submit} className="space-y-3">
        <Input
          type="email"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
          placeholder="Email"
          autoComplete="email"
        />
        <Input
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          placeholder="Password"
          autoComplete="current-password"
        />
        <Button type="submit" disabled={pending || !email || !password}>
          {pending ? "Logging in" : "Log in"}
        </Button>
      </form>

      {error && <p className="text-sm text-red-500">{error}</p>}
    </main>
  );
}
