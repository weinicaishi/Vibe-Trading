/**
 * Market Morning Auth0 Post-Login Action.
 *
 * This Action intentionally avoids Auth0 Enterprise-only session and refresh
 * token metadata. The API keeps a hash-only ledger for each access-token
 * instance. A missing auth-time claim permits ordinary reads but remains
 * fail-closed for high-risk recent-auth operations such as account deletion.
 */

const AUTH_TIME_CLAIM = "https://market-morning.invalid/claims/auth-time";
const ROLES_CLAIM = "https://market-morning.invalid/claims/roles";
const OPERATOR_ROLES_METADATA_KEY = "market_morning_roles";

function latestAuthenticationTime(event) {
  const methods = Array.isArray(event.authentication?.methods)
    ? event.authentication.methods
    : [];
  const timestamps = methods
    .map((method) => Date.parse(method?.timestamp || ""))
    .filter(Number.isFinite);
  if (timestamps.length === 0) return null;
  return Math.floor(Math.max(...timestamps) / 1000);
}

function operatorRoles(event) {
  const metadataRoles = event.user?.app_metadata?.[OPERATOR_ROLES_METADATA_KEY];
  const authorizationRoles = event.authorization?.roles;
  const source = Array.isArray(metadataRoles)
    ? metadataRoles
    : Array.isArray(authorizationRoles)
      ? authorizationRoles
      : [];
  return [...new Set(source)]
    .filter((role) => typeof role === "string")
    .map((role) => role.trim())
    .filter((role) => role.length > 0 && role.length <= 128);
}

exports.onExecutePostLogin = async (event, api) => {
  const authenticatedAt = latestAuthenticationTime(event);
  if (authenticatedAt !== null) {
    api.accessToken.setCustomClaim(AUTH_TIME_CLAIM, authenticatedAt);
  }

  const operatorClientId = event.secrets.MARKET_MORNING_OPERATOR_CLIENT_ID;
  if (operatorClientId && event.client?.client_id === operatorClientId) {
    api.accessToken.setCustomClaim(ROLES_CLAIM, operatorRoles(event));
  }
};
