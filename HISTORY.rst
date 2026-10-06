=======
History
=======

0.1.0 (2026-10-06)
------------------

* First release: MCP endpoint (Streamable HTTP, stateless JSON) at
  ``/api/care_mcp/mcp/``, authenticated with Care service-account tokens.
  Tools are generated from Care's OpenAPI schema and run through Care's own API
  views: a configurable set of operations as tools of their own, plus search,
  describe and call tools for every other operation. Read-only unless
  ``CARE_MCP_ALLOW_WRITES`` is set. Patient summary and shift handover prompts.
  No models or migrations.
