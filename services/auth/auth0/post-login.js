/**
 * Auth0 post-login Action: puts the person's role and customers into the access token for our API.
 *
 * The Orchestrator and Card read them under CLAIMS (services/orchestrator/app/auth.py,
 * services/card/app/api/auth.py). Setup: services/auth/README.md.
 *
 * - Only a login that asks for our API (AUDIENCE) is touched, so other apps on the same tenant are not.
 * - role: the one Auth0 Role named analyst, manager or admin, or, on a plan without Roles,
 *   app_metadata.role. Login is refused unless there is exactly one.
 * - customers: app_metadata.customers, the customer ids this person reviews. Managers and admins need it too.
 * - email: the person's email, which the Orchestrator stamps on a decision as by_name for showing who decided.
 */
const AUDIENCE = "https://api.credit-review.demo"; // the API identifier, as AUTH0_AUDIENCE
const CLAIMS = "https://credit-review.demo/";
const PEOPLE = ["analyst", "manager", "admin"];

exports.onExecutePostLogin = async (event, api) => {
  if (!event.resource_server || event.resource_server.identifier !== AUDIENCE) {
    return;
  }
  const metadata = event.user.app_metadata || {};
  const assigned = ((event.authorization && event.authorization.roles) || []).filter((name) => PEOPLE.includes(name));
  const roles = assigned.length ? assigned : [metadata.role].filter((name) => PEOPLE.includes(name));
  if (roles.length !== 1) {
    api.access.deny("This account needs exactly one role: analyst, manager or admin.");
    return;
  }
  api.accessToken.setCustomClaim(`${CLAIMS}role`, roles[0]);
  // Only strings: the services refuse a token whose customers are anything else.
  const customers = Array.isArray(metadata.customers) ? metadata.customers.filter((id) => typeof id === "string") : [];
  api.accessToken.setCustomClaim(`${CLAIMS}customers`, customers);
  // Who decided is shown by email instead of the sub; the services keep the sub as who decided.
  if (typeof event.user.email === "string") api.accessToken.setCustomClaim(`${CLAIMS}email`, event.user.email);
};
