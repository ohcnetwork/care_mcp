from unittest import mock

from django.urls import ResolverMatch
from model_bakery import baker

from care.emr.models.allergy_intolerance import AllergyIntolerance
from care.emr.models.condition import Condition
from care.security.permissions.encounter import EncounterPermissions
from care.security.permissions.patient import PatientPermissions
from care_mcp import openapi, tools
from care_mcp.dispatch import call_api
from care_mcp.openapi import operations
from care_mcp.settings import DEFAULT_READ_ONLY_OPERATIONS, DEFAULT_TOOLS
from care_mcp.tests.base import MCPTestBase, plugin_config


class OperationIndexTests(MCPTestBase):
    def test_default_tools_exist_in_care_schema(self):
        index = operations()
        missing = [op for op in DEFAULT_TOOLS.split(",") if op not in index]
        self.assertEqual(missing, [])

    def test_default_read_only_operations_exist_in_care_schema(self):
        index = operations()
        configured = DEFAULT_READ_ONLY_OPERATIONS.split(",")
        self.assertEqual([op for op in configured if op not in index], [])
        self.assertTrue(all(index[op].method == "POST" for op in configured))

    def test_bodies_are_json_only(self):
        # MCP calls always send JSON, so an operation whose body can only be
        # multipart or form data is left out of the index.
        multipart = {
            "requestBody": {"content": {"multipart/form-data": {"schema": {}}}}
        }
        self.assertFalse(openapi._accepts_json(multipart))  # noqa: SLF001
        self.assertIsNone(openapi._json_body_schema(multipart))  # noqa: SLF001
        self.assertTrue(openapi._accepts_json({}))  # noqa: SLF001

    def test_summaries_follow_the_view_action(self):
        index = operations()
        # drf-spectacular names these after the HTTP method; the view says what
        # they actually do.
        self.assertEqual(index["patient_search_create"].action, "search")
        self.assertTrue(
            index["patient_search_create"].summary.startswith("Patient: search")
        )
        self.assertTrue(
            index["users_getcurrentuser_retrieve"].summary.startswith(
                "Users: getcurrentuser"
            )
        )
        self.assertTrue(index["encounter_partial_update"].summary.startswith("Update "))
        self.assertTrue(index["encounter_update"].summary.startswith("Replace "))
        self.assertTrue(index["encounter_list"].is_paginated)

    def test_sensitive_operations_are_not_indexed(self):
        for op in operations().values():
            self.assertFalse(op.path.startswith("/api/v1/auth/"), op.path)
            self.assertNotIn("password", op.path)
            self.assertNotIn("service_account_token", op.path)
            self.assertFalse(op.path.startswith("/api/v1/batch_requests/"), op.path)
            self.assertFalse(op.path.startswith("/api/care_mcp/"), op.path)

    def test_path_values_cannot_change_the_route(self):
        op = operations()["patient_retrieve"]
        for bad in ("../auth/login", "a/b", "..", "a?b=c", ""):
            with self.assertRaises(ValueError, msg=bad):
                op.build_request({"external_id": bad})
        path, query = op.build_request({"external_id": "abc-123", "x": 1})
        self.assertEqual(path, "/api/v1/patient/abc-123/")
        self.assertEqual(query, {"x": 1})


class GeneratedToolTests(MCPTestBase):
    """Tools must see exactly what Care's REST API would show the same account."""

    def setUp(self):
        super().setUp()
        self.account = self.create_service_account()
        self.facility = self.create_facility(user=self.account)
        self.organization = self.create_facility_organization(facility=self.facility)
        self.patient = self.create_patient()
        self.encounter = self.create_encounter(
            patient=self.patient, facility=self.facility, organization=self.organization
        )
        self.token = self.token_for(self.account)

    def grant(self, *permissions):
        role = self.create_role_with_permissions([p.name for p in permissions])
        self.attach_role_facility_organization_user(
            self.organization, self.account, role
        )

    def test_tools_list(self):
        names = self.tool_names(self.token)
        self.assertEqual(names[:3], DEFAULT_TOOLS.split(",")[:3])
        self.assertIn("patient_allergy_intolerance_list", names)
        self.assertEqual(
            names[-3:], ["search_operations", "get_operation", "call_operation"]
        )
        tools = self.rpc("tools/list", token=self.token).json()["result"]["tools"]
        allergy = next(
            t for t in tools if t["name"] == "patient_allergy_intolerance_list"
        )
        self.assertIn("patient_external_id", allergy["inputSchema"]["required"])
        self.assertIn("encounter", allergy["inputSchema"]["properties"])
        self.assertTrue(all(t["annotations"]["readOnlyHint"] for t in tools))

    def test_configured_tools(self):
        with plugin_config(CARE_MCP_TOOLS="facility_list, not_an_operation"):
            names = self.tool_names(self.token)
        self.assertEqual(
            names,
            ["facility_list", "search_operations", "get_operation", "call_operation"],
        )

    def test_get_current_user(self):
        data = self.tool_json("users_getcurrentuser_retrieve", token=self.token)
        self.assertEqual(data["username"], self.account.username)

    def test_patient_hidden_without_access(self):
        result = self.call_tool(
            "patient_retrieve",
            {"external_id": str(self.patient.external_id)},
            self.token,
        )
        self.assertTrue(result["isError"])
        self.assertIn("404", result["content"][0]["text"])

    def test_patient_visible_with_access(self):
        self.grant(PatientPermissions.can_view_clinical_data)
        data = self.tool_json(
            "patient_retrieve",
            {"external_id": str(self.patient.external_id)},
            self.token,
        )
        self.assertEqual(data["id"], str(self.patient.external_id))

    def test_clinical_data_denied_without_access(self):
        result = self.call_tool(
            "patient_allergy_intolerance_list",
            {"patient_external_id": str(self.patient.external_id)},
            self.token,
        )
        self.assertTrue(result["isError"])
        self.assertIn("403", result["content"][0]["text"])
        self.assertIn("encounter=", result["content"][0]["text"])

    def test_list_allergies(self):
        self.grant(PatientPermissions.can_view_clinical_data)
        for status in ("confirmed", "entered_in_error"):
            baker.make(
                AllergyIntolerance,
                patient=self.patient,
                encounter=self.encounter,
                clinical_status="active",
                verification_status=status,
            )
        data = self.tool_json(
            "patient_allergy_intolerance_list",
            {
                "patient_external_id": str(self.patient.external_id),
                "exclude_verification_status": "entered_in_error",
            },
            self.token,
        )
        self.assertEqual(data["count"], 1)

    def test_list_diagnoses_by_category(self):
        self.grant(PatientPermissions.can_view_clinical_data)
        for category in ("encounter_diagnosis", "problem_list_item"):
            baker.make(
                Condition,
                patient=self.patient,
                encounter=self.encounter,
                category=category,
                clinical_status="active",
                verification_status="confirmed",
            )
        data = self.tool_json(
            "patient_diagnosis_list",
            {
                "patient_external_id": str(self.patient.external_id),
                "category": "encounter_diagnosis,chronic_condition",
            },
            self.token,
        )
        self.assertEqual(data["count"], 1)

    def test_list_encounters_for_facility(self):
        self.grant(EncounterPermissions.can_list_encounter)
        data = self.tool_json(
            "encounter_list",
            {"facility": str(self.facility.external_id), "live": "false"},
            self.token,
        )
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["id"], str(self.encounter.external_id))

    def test_invalid_arguments_are_a_tool_error(self):
        result = self.call_tool("patient_retrieve", {"nope": 1}, self.token)
        self.assertTrue(result["isError"])
        self.assertIn("external_id", result["content"][0]["text"])

    def test_parameters_missing_from_the_schema_are_passed_on(self):
        # Some Care views read query parameters their schema does not list, so
        # tools pass them on and tell the model they may have been ignored.
        with mock.patch.object(tools, "call_api", wraps=tools.call_api) as call:
            result = self.call_tool("facility_list", {"not_in_schema": "x"}, self.token)
        self.assertFalse(result["isError"])
        self.assertEqual(call.call_args.kwargs["query"], {"not_in_schema": "x"})
        self.assertIn("not_in_schema", result["content"][1]["text"])

    def test_body_for_an_operation_without_one_is_flagged(self):
        # A GET sends no body, so filters put there would be silently dropped.
        result = self.call_tool("facility_list", {"body": {"name": "x"}}, self.token)
        self.assertFalse(result["isError"])
        self.assertIn("body", result["content"][1]["text"])

    def test_read_only_post_works_with_writes_off(self):
        data = self.tool_json(
            "patient_search_create",
            {"body": {"phone_number": "+919999999999"}},
            self.token,
        )
        self.assertIn("results", data)


class DiscoveryToolTests(MCPTestBase):
    def setUp(self):
        super().setUp()
        self.account = self.create_service_account()
        self.token = self.token_for(self.account)

    def test_search_operations(self):
        data = self.tool_json(
            "search_operations", {"query": "allergy intolerance"}, self.token
        )
        ids = [o["operation_id"] for o in data["operations"]]
        self.assertIn("patient_allergy_intolerance_list", ids)
        # Writes are off, so only operations that read are offered.
        self.assertNotIn("patient_allergy_intolerance_create", ids)

    def test_search_hides_sensitive_operations(self):
        data = self.tool_json(
            "search_operations", {"query": "token password login"}, self.token
        )
        for found in data["operations"]:
            self.assertNotIn("service_account_token", found["operation_id"])
            self.assertNotIn("password", found["operation_id"])

    def test_get_operation(self):
        data = self.tool_json(
            "get_operation",
            {"operation_id": "patient_allergy_intolerance_create"},
            self.token,
        )
        self.assertEqual(data["method"], "POST")
        self.assertIn("patient_external_id", data["path_params"])
        self.assertIn("properties", data["body"])
        self.assertIn("note", data)  # writes are off

    def test_get_operation_accepts_full_operation_id(self):
        data = self.tool_json(
            "get_operation", {"operation_id": "api_v1_facility_list"}, self.token
        )
        self.assertEqual(data["operation_id"], "facility_list")
        self.assertIn("name", data["query_params"])

    def test_unknown_operation(self):
        result = self.call_tool("call_operation", {"operation_id": "nope"}, self.token)
        self.assertTrue(result["isError"])

    def test_call_operation(self):
        data = self.tool_json(
            "call_operation",
            {"operation_id": "users_getcurrentuser_retrieve"},
            self.token,
        )
        self.assertEqual(data["username"], self.account.username)

    def test_call_operation_validates_params(self):
        result = self.call_tool(
            "call_operation",
            {"operation_id": "patient_retrieve", "params": {}},
            self.token,
        )
        self.assertTrue(result["isError"])
        self.assertIn(
            "'external_id' is a required property", result["content"][0]["text"]
        )
        result = self.call_tool(
            "call_operation",
            {"operation_id": "facility_list", "params": {"limit": "ten"}},
            self.token,
        )
        self.assertTrue(result["isError"])
        self.assertIn("limit", result["content"][0]["text"])

    def test_call_blocked_write(self):
        result = self.call_tool(
            "call_operation",
            {"operation_id": "facility_create", "body": {"name": "x"}},
            self.token,
        )
        self.assertTrue(result["isError"])
        self.assertIn("writes are disabled", result["content"][0]["text"])


class WriteTests(MCPTestBase):
    def setUp(self):
        super().setUp()
        self.account = self.create_super_user(is_service_account=True)
        self.facility = self.create_facility(user=self.account)
        self.organization = self.create_facility_organization(facility=self.facility)
        self.patient = self.create_patient()
        self.encounter = self.create_encounter(
            patient=self.patient, facility=self.facility, organization=self.organization
        )
        self.token = self.token_for(self.account)

    def test_write_through_call_operation(self):
        with plugin_config(CARE_MCP_ALLOW_WRITES=True):
            tools = self.rpc("tools/list", token=self.token).json()["result"]["tools"]
            call = next(t for t in tools if t["name"] == "call_operation")
            self.assertFalse(call["annotations"]["readOnlyHint"])
            data = self.tool_json(
                "call_operation",
                {
                    "operation_id": "patient_thread_create",
                    "params": {"patient_external_id": str(self.patient.external_id)},
                    "body": {
                        "title": "Plan for tomorrow",
                        "encounter": str(self.encounter.external_id),
                    },
                },
                self.token,
            )
        self.assertEqual(data["title"], "Plan for tomorrow")

    def test_failed_write_is_tool_error(self):
        with plugin_config(CARE_MCP_ALLOW_WRITES=True):
            result = self.call_tool(
                "call_operation",
                {
                    "operation_id": "patient_thread_create",
                    "params": {"patient_external_id": str(self.patient.external_id)},
                    "body": {},
                },
                self.token,
            )
        self.assertTrue(result["isError"])


class DispatchTests(MCPTestBase):
    def test_failed_call_logs_the_route_not_the_path(self):
        patient_id = "5a1c2b3d-0000-4000-8000-000000000001"

        def failing_view(request, *args, **kwargs):
            raise RuntimeError

        match = ResolverMatch(
            failing_view, (), {"external_id": patient_id}, url_name="patient-detail"
        )
        with (
            mock.patch("care_mcp.dispatch.resolve", return_value=match),
            self.assertLogs("care_mcp.dispatch", "ERROR") as logs,
        ):
            result = call_api(
                self.create_user(), "get", f"/api/v1/patient/{patient_id}/"
            )
        self.assertEqual(result.status_code, 500)
        message = logs.records[0].getMessage()
        self.assertIn("GET patient-detail", message)
        self.assertNotIn(patient_id, message)
