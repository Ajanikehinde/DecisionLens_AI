# Salesforce + HubSpot OAuth — Setup Guide

DecisionLens AI ships with OAuth scaffolding for **Salesforce** and **HubSpot**.
Until you add real credentials, the **Sources** page will show these as
**`CONNECTED · MOCK`** when you click "Connect" — clearly flagging that no real
data is flowing yet. Follow these steps to graduate from MOCK to production.

---

## 1) Salesforce Connected App

1. Log into **https://login.salesforce.com** (or your sandbox)
2. **Setup → App Manager → New Connected App**
3. Fill in:
   - **Connected App Name**: `DecisionLens AI`
   - **API Name**: `DecisionLens_AI`
   - **Contact Email**: your email
4. Enable **OAuth Settings**
   - **Callback URL**: `https://decision-lens-9.preview.emergentagent.com/api/oauth/salesforce/callback`
   - **Selected OAuth Scopes**: `api`, `refresh_token`, `offline_access`
5. Save → wait ~10 minutes for propagation
6. Open the Connected App → **Manage Consumer Details**
7. Copy **Consumer Key** and **Consumer Secret**
8. Paste them into `/app/backend/.env`:
   ```
   SALESFORCE_CLIENT_ID="<your consumer key>"
   SALESFORCE_CLIENT_SECRET="<your consumer secret>"
   ```
9. Restart backend: `sudo supervisorctl restart backend`
10. Visit **Sources** → click **Connect Salesforce** → real OAuth screen opens

---

## 2) HubSpot OAuth App

1. Log into **https://developers.hubspot.com**
2. **Apps → Create a public app** (or "Create app" if you have a developer account)
3. Under **Auth tab**:
   - **Redirect URL**: `https://decision-lens-9.preview.emergentagent.com/api/oauth/hubspot/callback`
   - **Scopes**: `crm.objects.contacts.read` (add more as needed)
4. Copy **Client ID** and **Client secret** from the Auth tab
5. Paste into `/app/backend/.env`:
   ```
   HUBSPOT_CLIENT_ID="<your client id>"
   HUBSPOT_CLIENT_SECRET="<your client secret>"
   ```
6. Restart backend: `sudo supervisorctl restart backend`
7. Visit **Sources** → click **Connect HubSpot**

---

## Verifying it worked

After connecting:
- The source card status pill should read **`CONNECTED`** (without the orange `MOCK` tag)
- `GET /api/connections` returns `{"salesforce": {"status":"connected","mock":false,...}}`
- The OAuth state record was created in MongoDB collection `oauth_states` and consumed on callback

If you see `MOCK` after adding credentials, double-check:
- `.env` was saved with no surrounding quotes mismatched
- `sudo supervisorctl restart backend` was run
- Redirect URI in the OAuth app **exactly matches** the value in `.env`

---

## Behind the scenes

The OAuth implementation in `/app/backend/server.py` follows the standard
authorization-code flow:

1. `GET /api/oauth/{provider}/start` — generates a state token, stores it in
   `db.oauth_states`, returns a `redirect_url` pointing at the provider's
   authorize endpoint
2. User authenticates with the provider, which redirects back to
   `GET /api/oauth/{provider}/callback?code=...&state=...`
3. Backend validates the state, exchanges the code for an access token
   (best-effort POST to the provider's token URL), upserts the connection
   record, redirects to `/sources?connected={provider}`
4. `POST /api/oauth/{provider}/disconnect` flips the status to `disconnected`

The token-exchange call is currently best-effort — it does not persist the
returned access/refresh tokens. Before pulling real data from Salesforce or
HubSpot you'll want to:
- Capture `access_token`, `refresh_token`, `instance_url` (Salesforce) /
  `expires_in` (HubSpot)
- Store them encrypted in `db.connections`
- Implement a refresh-token rotation worker

This is the natural next step once real CRM data integration begins.
