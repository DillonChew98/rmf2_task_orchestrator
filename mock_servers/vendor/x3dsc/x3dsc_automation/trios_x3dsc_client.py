"""
HTTP client for the TRIOS X3 DSC instrument REST API.

The OpenAPI spec defines:

- Instrument-scoped base URL: ``http://{trios-api-base}/api/v1/instruments/{instrumentId}``
  for X3 DSC endpoints (status, queue, reference-pans, autosampler, sensors).

- Common host base: ``http://{trios-api-base}/api`` for discovery, JWT authentication,
  instrument info, and run control.

This client accepts either an API host/root plus ``instrument_id``, or an already
instrument-scoped base URL. The common ``/api`` base is derived from the same scheme
and host/port as ``api_base_url``.

Transport (URL building, error decoding, TriosClientError) lives in
``trios_common.http.TriosHttpClient``; this module only adds the X3 DSC-specific
endpoint methods.
"""

from __future__ import annotations

from urllib.parse import quote

from trios_common.http import (
    DEFAULT_CONTROL_START_TIMEOUT_SECONDS,
    DEFAULT_WAIT_FOR_TEST_STARTED_SECONDS,
    DEFAULT_WAIT_UNTIL_IDLE_SECONDS,
    JsonMapping,
    JsonValue,
    TriosClientError,
    TriosHttpClient,
)

#: Historically a distinct class; kept as an alias so ``except TriosX3dscClientError``
#: (here and in ``x3dsc_automation`` consumers) still catches transport errors raised
#: by the shared ``TriosHttpClient``.
TriosX3dscClientError = TriosClientError


class TriosX3dscClient(TriosHttpClient):
    """Synchronous client covering every endpoint in the TRIOS X3 DSC OpenAPI spec."""

    api_error_fallback = "TRIOS X3 DSC API request failed"

    # -------------------------------------------------------------------------
    # Instrument status
    # -------------------------------------------------------------------------

    def get_instrument_status(self) -> JsonValue | None:
        return self._request("GET", "/status")

    def wait_until_idle(self, timeout_seconds: int | None = None) -> JsonValue | None:
        return self._request(
            "POST",
            "/status/wait-idle",
            params={"timeout": timeout_seconds},
            request_timeout=self._wait_request_timeout(
                timeout_seconds,
                DEFAULT_WAIT_UNTIL_IDLE_SECONDS,
            ),
        )

    def wait_for_test_started(self, timeout_seconds: int | None = None) -> JsonValue | None:
        return self._request(
            "POST",
            "/status/wait-test-started",
            params={"timeout": timeout_seconds},
            request_timeout=self._wait_request_timeout(
                timeout_seconds,
                DEFAULT_WAIT_FOR_TEST_STARTED_SECONDS,
            ),
        )

    def get_instrument_errors(self) -> JsonValue | None:
        return self._request("GET", "/errors")

    # -------------------------------------------------------------------------
    # Queue management
    # -------------------------------------------------------------------------

    def list_queue_experiments(
        self,
        *,
        index: int | None = None,
        queue_type: str | None = None,
    ) -> JsonValue | None:
        return self._request(
            "GET",
            "/queue/experiments",
            params={"index": index, "queueType": queue_type},
        )

    def get_queue_status(self, *, queue_type: str | None = None) -> JsonValue | None:
        return self._request("GET", "/queue/status", params={"queueType": queue_type})

    def validate_queue(self, *, queue_type: str | None = None) -> JsonValue | None:
        return self._request("GET", "/queue/validate", params={"queueType": queue_type})

    def add_experiments_from_sequence_to_queue(
        self,
        request_body: JsonMapping,
        *,
        queue_type: str | None = None,
    ) -> JsonValue | None:
        return self._request(
            "POST",
            "/queue/experiments/from-sequence",
            json_body=request_body,
            params={"queueType": queue_type},
        )

    def validate_experiment(
        self,
        request_body: JsonMapping,
        *,
        queue_type: str | None = None,
    ) -> JsonValue | None:
        return self._request(
            "POST",
            "/queue/experiments/validate",
            json_body=request_body,
            params={"queueType": queue_type},
        )

    def add_new_experiments_to_queue(
        self,
        request_body: JsonMapping,
        *,
        queue_type: str | None = None,
    ) -> JsonValue | None:
        return self._request(
            "POST",
            "/queue/experiments/new",
            json_body=request_body,
            params={"queueType": queue_type},
        )

    def copy_experiment_to_queue(
        self,
        request_body: JsonMapping,
        *,
        queue_type: str | None = None,
    ) -> JsonValue | None:
        return self._request(
            "POST",
            "/queue/experiments/copy",
            json_body=request_body,
            params={"queueType": queue_type},
        )

    def get_experiment_details(self, experiment_id: str) -> JsonValue | None:
        return self._request("GET", f"/queue/experiments/{quote(experiment_id, safe='')}")

    def modify_experiment_in_queue(
        self,
        experiment_id: str,
        request_body: JsonMapping,
    ) -> JsonValue | None:
        return self._request(
            "PATCH",
            f"/queue/experiments/{quote(experiment_id, safe='')}",
            json_body=request_body,
        )

    def remove_experiment_from_queue(self, experiment_id: str) -> JsonValue | None:
        return self._request("DELETE", f"/queue/experiments/{quote(experiment_id, safe='')}")

    def move_experiment_in_queue(
        self,
        experiment_id: str,
        request_body: JsonMapping,
    ) -> JsonValue | None:
        return self._request(
            "PUT",
            f"/queue/experiments/{quote(experiment_id, safe='')}/position",
            json_body=request_body,
        )

    def validate_queued_experiment(self, experiment_id: str) -> JsonValue | None:
        return self._request(
            "GET",
            f"/queue/experiments/{quote(experiment_id, safe='')}/validate",
        )

    # -------------------------------------------------------------------------
    # Reference pans
    # -------------------------------------------------------------------------

    def get_reference_pans(self) -> JsonValue | None:
        return self._request("GET", "/reference-pans")

    def put_reference_pans(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/reference-pans", json_body=request_body)

    def delete_reference_pan(self, pan_number: int | str) -> JsonValue | None:
        return self._request("DELETE", f"/reference-pans/{quote(str(pan_number), safe='')}")

    # -------------------------------------------------------------------------
    # Autosampler
    # -------------------------------------------------------------------------

    def get_autosampler_status(self) -> JsonValue | None:
        return self._request("GET", "/autosampler/status")

    def get_autosampler_settings(self) -> JsonValue | None:
        return self._request("GET", "/autosampler/settings")

    def update_autosampler_settings(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/autosampler/settings", json_body=request_body)

    def load_sample_pan(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/autosampler/load-sample", json_body=request_body)

    def unload_sample_pan(self, request_body: JsonMapping | None = None) -> JsonValue | None:
        return self._request("POST", "/autosampler/unload-sample", json_body=request_body)

    def load_reference_pan(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/autosampler/load-reference", json_body=request_body)

    def unload_reference_pan(self) -> JsonValue | None:
        return self._request("POST", "/autosampler/unload-reference")

    def park_autosampler(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/autosampler/park", json_body=request_body)

    def open_autosampler_lid(self) -> JsonValue | None:
        return self._request("POST", "/autosampler/open-lid")

    def close_autosampler_lid(self) -> JsonValue | None:
        return self._request("POST", "/autosampler/close-lid")

    # -------------------------------------------------------------------------
    # Sensors (X3-specific)
    # -------------------------------------------------------------------------

    def get_sensors(self) -> JsonValue | None:
        return self._request("GET", "/sensors")

    def set_sensors(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/sensors", json_body=request_body)

    def export_results_json(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/results/export-json", json_body=request_body)

    def get_results_export_json_job(self, job_id: str) -> JsonValue | None:
        return self._request("GET", f"/results/export-json/jobs/{quote(job_id, safe='')}")

    # -------------------------------------------------------------------------
    # Common API routes (shared /api base)
    # -------------------------------------------------------------------------

    def login(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request(
            "POST",
            "/auth/login",
            json_body=request_body,
            route="common",
            send_bearer=False,
        )

    def refresh_auth_token(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/auth/refresh-token", json_body=request_body, route="common")

    def verify_auth_token(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/auth/verify-token", json_body=request_body, route="common")

    def get_health(self) -> JsonValue | None:
        return self._request("GET", "/health", route="common")

    def list_instances(self) -> JsonValue | None:
        return self._request("GET", "/instances", route="common")

    def get_instrument_info(self) -> JsonValue | None:
        return self._request("GET", "/info", route="common")

    def control_start(
        self,
        *,
        wait_for_ready: bool | None = None,
        timeout_seconds: int | None = None,
        force: bool | None = None,
    ) -> JsonValue | None:
        request_timeout = None
        if wait_for_ready is not False:
            request_timeout = self._wait_request_timeout(
                timeout_seconds,
                DEFAULT_CONTROL_START_TIMEOUT_SECONDS,
            )
        return self._request(
            "POST",
            "/control/start",
            route="common",
            params={
                "waitForReady": wait_for_ready,
                "timeout": timeout_seconds,
                "force": force,
            },
            request_timeout=request_timeout,
        )

    def control_stop(self, *, force: bool | None = None) -> JsonValue | None:
        return self._request("POST", "/control/stop", route="common", params={"force": force})

    def show_trios(self) -> JsonValue | None:
        return self._request("POST", "/app/show", route="common")

    def hide_trios(self) -> JsonValue | None:
        return self._request("POST", "/app/hide", route="common")

    def get_ui_enabled(self) -> JsonValue | None:
        return self._request("GET", "/app/ui-enabled", route="common")

    def set_ui_enabled(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/app/ui-enabled", json_body=request_body, route="common")

    def get_analysis_settings(self) -> JsonValue | None:
        return self._request("GET", "/app/settings/analysis", route="common")

    def set_analysis_settings(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/app/settings/analysis", json_body=request_body, route="common")

    def get_json_settings(self) -> JsonValue | None:
        return self._request("GET", "/app/settings/json", route="common")

    def set_json_settings(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/app/settings/json", json_body=request_body, route="common")

    def get_lims_settings(self) -> JsonValue | None:
        return self._request("GET", "/app/settings/lims", route="common")

    def set_lims_settings(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/app/settings/lims", json_body=request_body, route="common")

    def get_user_settings(self) -> JsonValue | None:
        return self._request("GET", "/app/settings/user", route="common")

    def set_user_settings(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("PUT", "/app/settings/user", json_body=request_body, route="common")

    def show_message(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/dialog/show-message", json_body=request_body, route="common")

    def show_notification(self, request_body: JsonMapping) -> JsonValue | None:
        return self._request("POST", "/dialog/notification", json_body=request_body, route="common")
