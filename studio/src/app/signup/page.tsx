"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { FormEvent, useEffect, useState } from "react";
import { toast } from "@/lib/toast";
import { register as apiRegister, ApiError } from "@/lib/api";
import { setToken, useAuth } from "@/lib/auth";
import { TopBar } from "@/components/TopBar";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";

export default function SignupPage() {
  const router = useRouter();
  const { isAuthenticated, ready } = useAuth();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [busy, setBusy] = useState(false);

  // Already signed in? Skip the form and go straight to the reviews list —
  // same behaviour as /login.
  useEffect(() => {
    if (ready && isAuthenticated) router.replace("/dashboard");
  }, [ready, isAuthenticated, router]);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    try {
      // Registering logs you in: /auth/register returns the token, so there is
      // no second login round-trip to fail and strand someone who now has an
      // account but no session.
      const { access_token } = await apiRegister(email, password, inviteCode);
      setToken(access_token);
      toast.success("Account created");
      // replace(), not push(): Back should not return to the signup form for an
      // account that now exists.
      router.replace("/dashboard");
      return; // stay disabled through the navigation
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Sign up failed";
      toast.error(msg);
      setBusy(false);
    }
  }

  return (
    <div>
      <TopBar />
      <main className="mx-auto max-w-sm px-4 py-16">
        <h1 className="text-2xl font-bold">Create account</h1>
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
              minLength={8}
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="mt-1"
            />
          </div>
          <div>
            <label htmlFor="invite-code" className="block text-sm font-medium">
              Access code
            </label>
            {/* Deliberately NOT `required`: whether a code is needed is the
                server's call (SIGNUP_CODE), so flipping it needs no client
                redeploy, and we don't advertise whether this instance is
                closed. A wrong/missing code comes back as a 403 whose message
                the catch block already surfaces. */}
            <Input
              id="invite-code"
              type="text"
              autoComplete="off"
              spellCheck={false}
              value={inviteCode}
              onChange={(e) => setInviteCode(e.target.value)}
              className="mt-1"
            />
            <p className="mt-1 text-xs text-muted-foreground">
              Required if you were sent one.
            </p>
          </div>
          <Button type="submit" disabled={busy} className="w-full">
            {busy ? "Creating…" : "Create account"}
          </Button>
        </form>
        <p className="mt-4 text-sm text-muted-foreground">
          Already have an account?{" "}
          <Link href="/login" className="text-primary hover:underline">
            Log in
          </Link>
        </p>
      </main>
    </div>
  );
}
