"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { toast } from "@/lib/toast";
import { login as apiLogin, ApiError } from "@/lib/api";
import { nextPathFromLocation, setToken, useAuth } from "@/lib/auth";
import { TopBar } from "@/components/TopBar";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export default function LoginPage() {
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  // Already signed in? Skip the form and go straight to the reviews list.
  useEffect(() => {
    if (ready && isAuthenticated) router.replace(nextPathFromLocation());
  }, [ready, isAuthenticated, router]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      const { access_token } = await apiLogin(email, password);
      setToken(access_token);
      toast.success("Logged in");
      // Back to where an expired session interrupted them, if anywhere.
      router.push(nextPathFromLocation());
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Login failed";
      toast.error(msg, { copy: false });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <TopBar />
      <main className="mx-auto max-w-sm px-4 py-16">
        <h1 className="text-2xl font-bold">Log in</h1>
        <form onSubmit={onSubmit} className="mt-6 space-y-4">
          <div>
            <label htmlFor="email" className="block text-sm font-medium">
              Email
            </label>
            <Input
              id="email"
              type="email"
              required
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              className="mt-1"
            />
          </div>
          <div>
            <label htmlFor="password" className="block text-sm font-medium">
              Password
            </label>
            <Input
              id="password"
              type="password"
              required
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1"
            />
          </div>
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "Logging in…" : "Log in"}
          </Button>
        </form>
        <p className="mt-4 text-sm text-muted-foreground">
          No account?{" "}
          <Link href="/signup" className="text-primary hover:underline">
            Sign up
          </Link>
        </p>
      </main>
    </div>
  );
}
