import { Auth0Provider } from "@auth0/auth0-react";
import type { ReactNode } from "react";
import { auth0Settings } from "./auth0";

/** Auth0 only when the build has its settings; Auth0 sends the person back to /login (an allowed callback URL). */
export default function WithAuth0({ children }: { children: ReactNode }) {
  if (!auth0Settings) return <>{children}</>;
  return (
    <Auth0Provider
      domain={auth0Settings.domain}
      clientId={auth0Settings.clientId}
      authorizationParams={{ audience: auth0Settings.audience, redirect_uri: `${window.location.origin}/login` }}
    >
      {children}
    </Auth0Provider>
  );
}
