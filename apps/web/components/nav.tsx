import Link from "next/link";

export function Nav() {
  return (
    <nav className="border-b">
      <div className="mx-auto flex max-w-5xl gap-6 p-4 text-sm">
        <Link href="/">Ask</Link>
        <Link href="/evals">Evals</Link>
        <Link href="/traces">Traces</Link>
      </div>
    </nav>
  );
}
