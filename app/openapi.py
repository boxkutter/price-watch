"""The OpenAPI document: FastAPI's generated spec, completed with what clients need to know.

Adds the two ways of authenticating, one tag per area, and the real error envelope on every
operation (the service answers errors in its own format, not FastAPI's default). Served at
/openapi.json and committed as docs/openapi.json so tools and AI agents can read it offline:

    python -m app.openapi > docs/openapi.json
"""
import json

from fastapi.openapi.utils import get_openapi

DESCRIPTION = """\
Retail Monitor tracks prices and promotions for the specific products you choose, across stores.

**Start with the guide:** `/api-guide` (also `docs/api.md`), which covers authentication, the
asynchronous job pattern, limits, workflows, events and webhooks. A summary for AI agents is at
`/llms.txt`.

**Authentication.** Every `/v1` call except login and accepting an invitation needs
`Authorization: Bearer <user token>`. A client app also sends `X-Client-Token: <client token>`;
its permissions are then the intersection of the user's, the client's and the grant's scopes.

**Conventions.** Money is a decimal string (`"24.90"`), times are ISO 8601 UTC, unknown values are
`null` (never zero). Anything that contacts a store is asynchronous: the call returns `202` with a
`job_id` and `status_url`; poll the job. Errors always use the `Error` envelope.
"""

TAGS = [
    ("Health", "Liveness and readiness."),
    ("Authentication", "Accept an invitation, sign in, who am I."),
    ("API clients", "Credentials for apps, and the grants users give them."),
    ("My stores", "The stores you compare prices across."),
    ("Catalogue", "Retailers, connectors, stores, products, listings and offers the service knows."),
    ("Search", "Live searches by name or barcode (asynchronous)."),
    ("Compare", "Scan a barcode in a shop and compare the same item across your stores."),
    ("Monitors", "What you watch: add by link, by search or by barcode; check, remove, link a barcode."),
    ("Events", "What happened to what you watch (price changes, alerts, offers)."),
    ("Webhooks", "Get events pushed to your server, signed."),
    ("Jobs", "Poll the result of anything asynchronous."),
    ("Administration", "Administrator-only: users, connectors, stores, the example client."),
]
ERROR_CODES = ["unauthorized", "forbidden", "not_found", "method_not_allowed", "conflict",
               "invalid_request", "rate_limited", "unavailable", "request_failed"]
PUBLIC = ("/v1/auth/login", "/v1/auth/accept-invitation", "/images/{image_id}", "/logos/{retailer_id}")


def tag_for(path: str) -> str:
    if path.startswith("/health"):
        return "Health"
    if path.startswith("/v1/admin"):
        return "Administration"
    if path in ("/v1/me",) or path.startswith("/v1/auth"):
        return "Authentication"
    if path.startswith("/v1/me/stores"):
        return "My stores"
    if path.startswith(("/v1/clients", "/v1/me/client-grants")):
        return "API clients"
    if path.startswith("/v1/search"):
        return "Search"
    if path.startswith("/v1/compare"):
        return "Compare"
    if path.startswith("/v1/monitors"):
        return "Monitors"
    if path.startswith("/v1/events"):
        return "Events"
    if path.startswith("/v1/webhooks"):
        return "Webhooks"
    if path.startswith("/v1/jobs"):
        return "Jobs"
    return "Catalogue"


def build_schema(app) -> dict:
    schema = get_openapi(title=app.title, version=app.version, description=DESCRIPTION,
                         routes=app.routes, tags=[{"name": n, "description": d} for n, d in TAGS])
    components = schema.setdefault("components", {})
    components["securitySchemes"] = {
        "UserToken": {"type": "http", "scheme": "bearer",
                      "description": "The user's token from POST /v1/auth/login."},
        "ClientToken": {"type": "apiKey", "in": "header", "name": "X-Client-Token",
                        "description": "An API client's token. Optional: when sent, permissions "
                                       "are limited to what the user granted this client."},
    }
    components.setdefault("schemas", {})["Error"] = {
        "type": "object", "required": ["code", "message", "request_id"],
        "properties": {
            "code": {"type": "string", "enum": ERROR_CODES},
            "message": {"type": "string", "description": "Safe to show to a person."},
            "details": {"type": "array", "items": {"type": "object"},
                        "description": "Field errors for invalid_request; extra context otherwise."},
            "request_id": {"type": "string", "description": "Quote this when reporting a problem."},
            "retry_after": {"type": "integer", "description": "Seconds, on rate_limited."}}}
    error = {"$ref": "#/components/schemas/Error"}

    def response(description: str) -> dict:
        return {"description": description, "content": {"application/json": {"schema": error}}}

    for path, operations in schema["paths"].items():
        for operation in operations.values():
            operation["tags"] = [tag_for(path)]
            if path.startswith("/health"):
                continue
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)
            responses["422"] = response("The request is invalid (see `details`).")
            responses["429"] = response("Too many requests or a quota was reached; see `Retry-After`.")
            if path not in PUBLIC:
                responses["401"] = response("Missing, invalid, expired or revoked token.")
                responses["403"] = response("The token lacks the scope this needs.")
                operation["security"] = [{"UserToken": [], "ClientToken": []}, {"UserToken": []}]
            if "{" in path:
                responses["404"] = response("Not found, or not yours.")
            if path.startswith("/v1/admin"):
                operation["x-admin-only"] = True
    return schema


def install(app) -> None:
    def openapi() -> dict:
        if app.openapi_schema is None:
            app.openapi_schema = build_schema(app)
        return app.openapi_schema

    app.openapi = openapi


def main() -> None:
    from app.main import app
    print(json.dumps(build_schema(app), indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
