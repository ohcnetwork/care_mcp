from unittest import mock

from django.test import override_settings
from django.urls import reverse
from rest_framework_simplejwt.tokens import RefreshToken

from care_mcp.protocol import SUPPORTED_PROTOCOL_VERSIONS
from care_mcp.tests.base import MCPTestBase, plugin_config
from care_mcp.views import MAX_BATCH_MESSAGES


class MCPAuthenticationTests(MCPTestBase):
    def setUp(self):
        super().setUp()
        self.account = self.create_service_account()
        self.token = self.token_for(self.account)

    def test_requires_authentication(self):
        response = self.rpc("tools/list")
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers["WWW-Authenticate"])

    def test_service_account_token(self):
        response = self.rpc("ping", token=self.token)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["result"], {})

    def test_service_account_token_as_bearer(self):
        response = self.rpc("ping", token=self.token, scheme="Bearer")
        self.assertEqual(response.status_code, 200)

    def test_care_jwt(self):
        jwt = str(RefreshToken.for_user(self.create_user()).access_token)
        response = self.rpc("ping", token=jwt, scheme="Bearer")
        self.assertEqual(response.status_code, 200)

    def test_unknown_token(self):
        for scheme in ("Token", "Bearer"):
            response = self.rpc("ping", token="0" * 40, scheme=scheme)
            self.assertEqual(response.status_code, 401, scheme)

    def test_garbage_bearer(self):
        response = self.rpc("ping", token="not-a-token", scheme="Bearer")
        self.assertEqual(response.status_code, 401)

    def test_inactive_account(self):
        self.account.is_active = False
        self.account.save()
        self.assertEqual(self.rpc("ping", token=self.token).status_code, 401)

    def test_rejects_foreign_origin(self):
        response = self.rpc(
            "ping", token=self.token, HTTP_ORIGIN="https://evil.example"
        )
        self.assertEqual(response.status_code, 403)

    def test_allows_configured_origin(self):
        with plugin_config(CARE_MCP_ALLOWED_ORIGINS="https://care.example"):
            response = self.rpc(
                "ping", token=self.token, HTTP_ORIGIN="https://care.example"
            )
        self.assertEqual(response.status_code, 200)

    def test_disabled(self):
        with plugin_config(CARE_MCP_ENABLED=False):
            self.assertEqual(self.rpc("ping", token=self.token).status_code, 404)

    def test_config_accepts_service_account_tokens(self):
        url = reverse("care-mcp-config")
        for scheme in ("Token", "Bearer"):
            response = self.client.get(url, HTTP_AUTHORIZATION=f"{scheme} {self.token}")
            self.assertEqual(response.status_code, 200, scheme)
            self.assertTrue(response.json()["endpoint"].endswith(self.url))
        self.assertEqual(self.client.get(url).status_code, 401)


class MCPProtocolTests(MCPTestBase):
    def setUp(self):
        super().setUp()
        self.token = self.token_for(self.create_service_account())

    def test_initialize(self):
        response = self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
            token=self.token,
        )
        result = response.json()["result"]
        self.assertEqual(result["protocolVersion"], "2025-06-18")
        self.assertEqual(result["serverInfo"]["name"], "care")
        self.assertIn("tools", result["capabilities"])
        self.assertTrue(result["instructions"])

    def test_initialize_unknown_version_gets_latest(self):
        response = self.rpc(
            "initialize", {"protocolVersion": "1999-01-01"}, token=self.token
        )
        self.assertEqual(
            response.json()["result"]["protocolVersion"],
            SUPPORTED_PROTOCOL_VERSIONS[0],
        )

    def test_notification_is_accepted(self):
        response = self.post(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            token=self.token,
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.content, b"")

    def test_unsupported_protocol_header(self):
        response = self.rpc(
            "ping", token=self.token, HTTP_MCP_PROTOCOL_VERSION="1999-01-01"
        )
        self.assertEqual(response.status_code, 400)

    def test_parse_error(self):
        response = self.client.post(
            self.url,
            "{not json",
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Token {self.token}",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32700)

    def test_method_not_found(self):
        response = self.rpc("resources/subscribe", token=self.token)
        self.assertEqual(response.json()["error"]["code"], -32601)

    def test_invalid_message(self):
        response = self.post({"id": 1, "method": "ping"}, token=self.token)
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_batch(self):
        response = self.post(
            [
                {"jsonrpc": "2.0", "id": 1, "method": "ping"},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "ping"},
            ],
            token=self.token,
        )
        self.assertEqual([r["id"] for r in response.json()], [1, 2])

    def test_oversized_batch_is_rejected(self):
        batch = [
            {"jsonrpc": "2.0", "id": i, "method": "ping"}
            for i in range(MAX_BATCH_MESSAGES + 1)
        ]
        response = self.post(batch, token=self.token)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_each_batched_message_counts_against_the_rate_limit(self):
        batch = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(3)]
        with (
            override_settings(DISABLE_RATELIMIT=False),
            mock.patch("care_mcp.views.is_ratelimited", return_value=False) as limited,
        ):
            self.assertEqual(self.post(batch, token=self.token).status_code, 200)
        self.assertEqual(limited.call_count, 3)
        with (
            override_settings(DISABLE_RATELIMIT=False),
            mock.patch(
                "care_mcp.views.is_ratelimited", side_effect=[False, False, True]
            ),
        ):
            self.assertEqual(self.post(batch, token=self.token).status_code, 429)

    def test_null_method_is_invalid(self):
        response = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": None}, token=self.token
        )
        self.assertEqual(response.json()["error"]["code"], -32600)

    def test_request_id_must_be_a_string_or_an_integer(self):
        for id_ in (None, {}, [], True, 1.5):
            body = self.post(
                {"jsonrpc": "2.0", "id": id_, "method": "ping"}, token=self.token
            ).json()
            self.assertEqual(body["error"]["code"], -32600, id_)
            self.assertIsNone(body["id"])
        for id_ in ("abc", 0):
            body = self.post(
                {"jsonrpc": "2.0", "id": id_, "method": "ping"}, token=self.token
            ).json()
            self.assertEqual(body, {"jsonrpc": "2.0", "id": id_, "result": {}})

    def test_get_not_allowed(self):
        response = self.client.get(self.url, HTTP_AUTHORIZATION=f"Token {self.token}")
        self.assertEqual(response.status_code, 405)

    def test_unknown_tool(self):
        response = self.rpc(
            "tools/call", {"name": "nope", "arguments": {}}, token=self.token
        )
        self.assertEqual(response.json()["error"]["code"], -32602)

    def test_params_and_arguments_must_be_objects(self):
        for value in ([], "", 0, False):
            response = self.rpc("ping", value, token=self.token)
            self.assertEqual(response.json()["error"]["code"], -32602, value)
            response = self.rpc(
                "tools/call",
                {"name": "search_operations", "arguments": value},
                token=self.token,
            )
            self.assertEqual(response.json()["error"]["code"], -32602, value)

    def test_null_params_and_arguments_mean_none_given(self):
        response = self.post(
            {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": None},
            token=self.token,
        )
        self.assertEqual(response.json()["result"], {})
        response = self.rpc(
            "tools/call",
            {"name": "users_getcurrentuser_retrieve", "arguments": None},
            token=self.token,
        )
        self.assertFalse(response.json()["result"]["isError"])

    def test_prompts(self):
        prompts = self.rpc("prompts/list", token=self.token).json()["result"]
        self.assertIn("patient_summary", {p["name"] for p in prompts["prompts"]})
        result = self.rpc(
            "prompts/get",
            {"name": "patient_summary", "arguments": {"patient_id": "abc"}},
            token=self.token,
        ).json()["result"]
        self.assertIn("abc", result["messages"][0]["content"]["text"])
        response = self.rpc(
            "prompts/get",
            {"name": "patient_summary", "arguments": ["abc"]},
            token=self.token,
        )
        self.assertEqual(response.json()["error"]["code"], -32602)
        response = self.rpc("prompts/get", {"name": ["abc"]}, token=self.token)
        self.assertEqual(response.json()["error"]["code"], -32602)

    def test_every_tool_call_is_logged_without_its_arguments(self):
        account = self.create_service_account()
        token = self.token_for(account)
        with self.assertLogs("care_mcp.protocol", "INFO") as logs:
            self.call_tool("search_operations", {"query": 5}, token=token)
            self.rpc(
                "tools/call",
                {"name": "search_operations", "arguments": []},
                token=token,
            )
            self.call_tool("search_operations", {"query": "allergy"}, token=token)
        messages = [record.getMessage() for record in logs.records]
        self.assertEqual(
            [m.rsplit("outcome=", 1)[1] for m in messages], ["error", "error", "ok"]
        )
        for message in messages:
            self.assertIn(f"user={account.external_id}", message)
            self.assertNotIn("allergy", message)

    def test_truncates_long_results(self):
        with plugin_config(CARE_MCP_MAX_RESPONSE_CHARS=100):
            result = self.call_tool(
                "search_operations", {"query": "patient"}, token=self.token
            )
        self.assertIn("truncated", result["content"][0]["text"])
