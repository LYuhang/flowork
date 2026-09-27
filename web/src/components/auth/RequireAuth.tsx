/**
 * Route guard for the authenticated portion of the application.
 *
 * Resolves the HttpOnly cookie once before deciding whether to redirect.
 * Missing/expired Session → `/login` without leaking the attempted URL.
 * (no consumer reads it; embedding it would just stash the deep-link in
 * `history.state` of the public login page).
 *
 * The router wires this as a parent route over `/workspace`, `/workflow/*`,
 * and `/settings`. We don't gate on `bootstrapped` here: if the token
 * exists locally but `/auth/me` ultimately 401s, the api client middleware
 * fires `handle401` which clears the token and triggers this guard to
 * redirect on the very next render.
 */
import { Navigate, Outlet } from 'react-router';
import { useEffect } from 'react';
import { useAuthStore } from '@/stores/auth';
import { loadAppLayout, preloadRoute } from '@/app/route-loaders';

export function RequireAuth() {
  const authenticated = useAuthStore((s) => s.authenticated);
  const bootstrapped = useAuthStore((s) => s.bootstrapped);
  const bootstrap = useAuthStore((s) => s.bootstrap);
  useEffect(() => {
    if (!bootstrapped) {
      // Authentication and route chunks are independent. Fetch both at once
      // so a hard refresh does not wait for /auth/me before it even starts
      // downloading/parsing the Chat shell and transcript renderer.
      void loadAppLayout();
      void preloadRoute(window.location.pathname);
      void bootstrap();
    }
  }, [bootstrap, bootstrapped]);
  if (!bootstrapped) return null;
  if (!authenticated) return <Navigate to="/login" replace />;
  return <Outlet />;
}
