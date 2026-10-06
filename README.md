# Care MCP

A [Model Context Protocol](https://modelcontextprotocol.io) server for
[CARE](https://github.com/ohcnetwork/care), shipped as a Care backend plugin.

It lets an AI assistant (Claude, Cursor, VS Code, or any MCP client) work with Care
through a Care **service account**: look up facilities, find admitted patients,
read diagnoses, medications, observations, notes and reports, and (if an operator
allows it) make changes.

The server runs **inside Care**, and its tools are **generated from Care's own
OpenAPI (Swagger) schema**. Every tool call goes through Care's API views as the
service account, so an assistant sees exactly what that account would see through
Care's REST API: the same querysets, the same `AuthorizationController` checks and
the same errors. Nothing here re-implements Care's permission logic, and the plugin
adds no models and no migrations.

## How it works

```
MCP client ──POST /api/care_mcp/mcp/──▶ MCPView (auth, origin check, rate limit)
                                            │ JSON-RPC: initialize, tools/*, prompts/*
                                            ▼
                     tool generated from an OpenAPI operation, e.g.
                     patient_allergy_intolerance_list → GET /api/v1/patient/{id}/allergy_intolerance/
                                            │
                                            ▼
                         Care's URL resolver → Care's viewset, run as the service account
```

- **Tools from Swagger:** on first use the plugin builds Care's OpenAPI schema with
  the same generator that serves `/api/schema/` (about 550 operations, other
  plugins' endpoints included) and turns operations into MCP tools: an
  operation's path and query parameters, and its request body, become the tool's
  input schema. A new Care endpoint is available to assistants as soon as it ships.
- **Transport:** Streamable HTTP (protocol versions `2025-11-25`, `2025-06-18` and
  `2025-03-26`), answered with plain JSON. The server is stateless, so it works
  behind any number of gunicorn workers with no session store.
- **Authentication:** a Care service-account token, sent as
  `Authorization: Token <token>` (as for the rest of Care's API) or
  `Authorization: Bearer <token>` (for clients that only offer a bearer field).
  A regular Care JWT also works, for clients built into Care's web app.
- **Read-only by default.** Operations that change data are hidden unless the
  operator sets `CARE_MCP_ALLOW_WRITES`.

## Install

Add the plugin to Care's `plug_config.py`:

```python
care_mcp = Plug(
    name="care_mcp",
    package_name="git+https://github.com/ohcnetwork/care_mcp.git",
    version="@develop",
    configs={},
)

plugs = [care_mcp]
```

or, without editing `plug_config.py`, at build time:

```bash
ADDITIONAL_PLUGS='[{"name":"care_mcp","package_name":"git+https://github.com/ohcnetwork/care_mcp.git","version":"@develop"}]'
```

Then rebuild the image (`make down && make build && make up`). There is nothing
to migrate.

## Set up a service account

The assistant works as a Care service account, Care's existing kind of user for
integrations. Give it only the access the assistant needs.

1. **Create the account** as an administrator, with `is_service_account` set:

   ```bash
   curl -X POST https://care.example.org/api/v1/users/ \
     -H "Authorization: Bearer $ADMIN_JWT" -H "Content-Type: application/json" \
     -d '{"username": "mcp-assistant", "email": "mcp-assistant@example.org",
          "first_name": "MCP", "last_name": "Assistant", "user_type": "doctor",
          "gender": "non_binary", "phone_number": "+910000000000",
          "is_service_account": true, "role_orgs": []}'
   ```

2. **Give it roles** the way you would a staff member, for example by adding it to
   a facility organization:
   `POST /api/v1/facility/<facility_id>/organizations/<organization_id>/users/`
   with `{"user": "<service account id>", "role": "<role id>"}`. Its roles decide
   everything the assistant can see and, if writes are enabled, change.

3. **Generate its token** (as a superuser, or as the admin who created the account):

   ```bash
   curl -X POST https://care.example.org/api/v1/users/mcp-assistant/generate_service_account_token/ \
     -H "Authorization: Bearer $ADMIN_JWT"
   ```

   The response contains `token`. Generating again replaces the old token, and
   `DELETE /api/v1/users/mcp-assistant/revoke_service_account_token/` revokes it.
   Either change applies from the next MCP request.

## Connect a client

The endpoint is `https://<care-host>/api/care_mcp/mcp/`
(`GET /api/care_mcp/config/` returns it).

**Claude Code**

```bash
claude mcp add --transport http care https://care.example.org/api/care_mcp/mcp/ \
  --header "Authorization: Token <service-account-token>"
```

**Cursor, VS Code, Windsurf and other clients with remote-server support**

```json
{
  "mcpServers": {
    "care": {
      "url": "https://care.example.org/api/care_mcp/mcp/",
      "headers": { "Authorization": "Token <service-account-token>" }
    }
  }
}
```

**Claude Desktop** (through the `mcp-remote` bridge)

```json
{
  "mcpServers": {
    "care": {
      "command": "npx",
      "args": ["mcp-remote", "https://care.example.org/api/care_mcp/mcp/",
               "--header", "Authorization:${CARE_AUTH}"],
      "env": { "CARE_AUTH": "Token <service-account-token>" }
    }
  }
}
```

## Tools

Tools are named after Care's Swagger operation ids, without the `api_v1_` prefix.
By default these operations get a tool of their own:

| Tool | Care API |
| --- | --- |
| `users_getcurrentuser_retrieve` | The account itself, with its facilities, organizations and permissions |
| `facility_list`, `facility_retrieve` | Facilities |
| `patient_list`, `patient_retrieve`, `patient_search_create` | Patients, and search by phone number |
| `encounter_list`, `encounter_retrieve` | Visits and admissions (`facility=<id>`, `live=false` for patients currently under care) |
| `patient_diagnosis_list`, `patient_symptom_list` | Conditions |
| `patient_allergy_intolerance_list` | Allergies and intolerances |
| `patient_medication_request_list`, `patient_medication_statement_list`, `patient_medication_administration_list` | Prescriptions, reported medications, administered doses |
| `patient_observation_list` | Vitals and results |
| `patient_questionnaire_response_list` | Filled forms and assessments |
| `patient_diagnostic_report_list` | Lab and imaging reports |
| `facility_service_request_list` | Orders |
| `patient_thread_list`, `patient_thread_note_list` | Discussion threads and notes |

Three more tools reach every other operation in Care's schema:

- `search_operations` finds operations by keyword (e.g. "location", "schedule",
  "inventory").
- `get_operation` returns an operation's parameters, request body schema and
  response fields.
- `call_operation` calls any operation by id, with its path and query parameters
  and its body.

Set `CARE_MCP_TOOLS` to change which operations get a tool of their own.
Operations that only read data but use `POST` (patient search, value set lookups
and a few others) are listed in `CARE_MCP_READ_ONLY_OPERATIONS`, which keeps them
available when writes are off.

Arguments are checked against the schema, and mistakes come back as tool results
with `isError: true` and a hint (a 403 on clinical data, for example, tells the
model to pass the `encounter` that grants access), so the model can recover on its
own. Some Care views read query parameters their schema does not declare
(`encounter` on thread notes, `resource_type` on schedules); those are passed on to
Care, with a note telling the model that Care may have ignored them.

Because the tools come from Care's schema, a better schema makes better tools:
docstrings, `extend_schema` descriptions, declared filters and enum choices in
Care all show up in the tools' descriptions and input schemas.

Prompts: `patient_summary` (clinical summary of a patient) and `shift_handover`
(handover notes for a facility's admitted patients).

## Settings

Resolution order: `PLUGIN_CONFIGS["care_mcp"][key]` → environment variable → default.

| Setting | Default | Meaning |
| --- | --- | --- |
| `CARE_MCP_ENABLED` | `True` | Master switch for the endpoint |
| `CARE_MCP_ALLOW_WRITES` | `False` | Offer operations that create, update or delete data |
| `CARE_MCP_TOOLS` | the operations above | Comma-separated operation ids that get a tool of their own |
| `CARE_MCP_READ_ONLY_OPERATIONS` | patient search, value set lookups… | Comma-separated non-GET operation ids that only read data |
| `CARE_MCP_MAX_RESPONSE_CHARS` | `50000` | Truncate longer tool results |
| `CARE_MCP_RATE_LIMIT` | `120/m` | Per-user limit on MCP messages (each message in a batch counts); `""` disables it |
| `CARE_MCP_ALLOWED_ORIGINS` | `""` | Comma-separated browser origins allowed to call the endpoint |

## Security notes

- The assistant has exactly the access of its service account. Give the account
  only the roles the assistant needs, and keep writes off unless you need them.
- Login, token, password, MFA, OTP and batch endpoints, including the
  service-account token endpoints, are left out of the tool index and cannot be
  called through MCP.
- Requests that carry an `Origin` header not in `CARE_MCP_ALLOWED_ORIGINS` are
  rejected, which stops a malicious web page from driving a local client's
  connection (DNS rebinding). Desktop and CLI clients send no `Origin`.
- Every tool call is logged (`care_mcp` logger: tool, user, outcome; arguments are
  not logged). Changes made through MCP run through Care's own views as the
  service account, so Care records them as that account's.
- When a write fails, its database changes are rolled back. Effects outside the
  database, such as files already stored or notifications already sent, are not.
- Patient data leaves Care when an assistant reads it. Only connect clients and
  model providers your deployment's data-protection rules allow.

## Not yet supported

- OAuth 2.1 authorization (needed for clients such as claude.ai custom connectors
  that only support OAuth). Header-based tokens cover Claude Code, Claude Desktop,
  Cursor and VS Code today.
- Patient-portal (OTP) users: the server only serves Care user accounts.

## Development

Install the plugin into Care's virtualenv in editable mode and register it with
`package_name="care_mcp"` and `version=""`, in `plug_config.py` or through
`ADDITIONAL_PLUGS`:

```bash
pip install -e /path/to/care_mcp
export ADDITIONAL_PLUGS='[{"name":"care_mcp","package_name":"care_mcp","version":""}]'
```

For Docker-based development, clone this repo inside the Care checkout instead (a
real directory, not a symlink, so the image build can see it) and rebuild the
image.

```bash
make lint                          # in this repo: ruff check + ruff format --check
python manage.py test care_mcp     # in the Care checkout
```

If the repo is cloned inside the Care checkout, name the test modules instead
(`python manage.py test care_mcp.tests.test_protocol care_mcp.tests.test_tools`),
because `care_mcp` then also names the repo directory.
