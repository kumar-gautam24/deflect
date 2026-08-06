"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";

export function Nav() {
  const router = useRouter();

  async function logout() {
    await fetch("/api/auth/logout", { method: "POST" });
    router.push("/login");
  }

  return (
    <nav className="border-b">
      <div className="mx-auto flex max-w-5xl gap-6 p-4 text-sm">
        <Link href="/">Ask</Link>
        <Link href="/evals">Evals</Link>
        <Link href="/traces">Traces</Link>
        <button
          type="button"
          onClick={() => void logout()}
          className="text-muted-foreground hover:text-foreground ml-auto"
        >
          Log out
        </button>
      </div>
    </nav>
  );
}
