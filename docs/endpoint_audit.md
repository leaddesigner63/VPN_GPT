# Endpoint Coverage Audit

The published OpenAPI description enumerates fourteen management endpoints for this service, covering VPN key issuance, user administration, Morune payment flows, notifications, and maintenance tasks.【F:openapi_gpt.json†L9-L600】 The FastAPI application only exposes a subset of these routes. The table below summarises what is documented versus what is implemented.

| Endpoint | Implementation status |
| --- | --- |
| `POST /vpn/issue_key` | Implemented via `issue_key` in `api/endpoints/vpn.py`.【F:openapi_gpt.json†L9-L61】【F:api/endpoints/vpn.py†L19-L107】 |
| `POST /vpn/renew_key` | Implemented via `renew_key` in `api/endpoints/vpn.py`.【F:openapi_gpt.json†L62-L114】【F:api/endpoints/vpn.py†L119-L152】 |
| `POST /vpn/disable_key` | **Missing** — no corresponding FastAPI route exists in the VPN router.【F:openapi_gpt.json†L115-L167】【F:api/endpoints/vpn.py†L19-L152】 |
| `GET /vpn/my_key` | **Missing** — not defined anywhere in the codebase.【F:openapi_gpt.json†L554-L600】【F:api/endpoints/vpn.py†L19-L152】 |
| `GET /vpn/users/{username}` | **Missing** — there is no read-only VPN user endpoint registered.【F:openapi_gpt.json†L243-L288】【F:api/endpoints/vpn.py†L19-L152】 |
| `GET /users/` | **Missing** — the users router only defines `/register`, `/{username}/keys`, and `/{username}/referrals`.【F:openapi_gpt.json†L290-L338】【F:api/endpoints/users.py†L10-L89】 |
| `GET /users/all` | **Missing** — not exposed by the users router.【F:openapi_gpt.json†L438-L459】【F:api/endpoints/users.py†L10-L89】 |
| `GET /users/expiring` | **Missing** — an endpoint exists in `api/endpoints/expiring.py`, but that router is never included in `api/main.py`.【F:openapi_gpt.json†L339-L387】【F:api/endpoints/expiring.py†L8-L16】【F:api/main.py†L52-L85】 |
| `GET /users/userinfo` | **Missing** — no matching route in the users router.【F:openapi_gpt.json†L388-L437】【F:api/endpoints/users.py†L10-L89】 |
| `POST /notify/notify/send` | Implemented by the `notify` router, which FastAPI mounts under the `/notify` prefix.【F:openapi_gpt.json†L460-L498】【F:api/endpoints/notify.py†L9-L59】【F:api/main.py†L52-L85】 |
| `POST /morune/create_invoice` | Implemented in `api/endpoints/morune.py`.【F:openapi_gpt.json†L168-L205】【F:api/endpoints/morune.py†L20-L185】 |
| `POST /morune/paid` | Implemented in `api/endpoints/morune.py`.【F:openapi_gpt.json†L205-L240】【F:api/endpoints/morune.py†L188-L282】 |
| `POST /admin/backup_db` | Implemented in `api/endpoints/admin.py`.【F:openapi_gpt.json†L499-L534】【F:api/endpoints/admin.py†L16-L36】 |
| `GET /healthz` | Implemented in `api/main.py`.【F:openapi_gpt.json†L535-L553】【F:api/main.py†L97-L103】 |

**Summary:** Eight of the fourteen documented endpoints are absent from the FastAPI routing graph. The most notable gaps are the VPN key management read endpoints and the user-administration utilities. To align behaviour with the published specification, either implement the missing routes or revise `openapi_gpt.json` to reflect the actual capabilities.
