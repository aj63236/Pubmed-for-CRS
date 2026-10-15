"""Offline OpenAI migration regressions: python -m unittest -v tests_openai.

Every test loads configuration in an isolated environment, blocks real HTTP,
and redirects both DATA_DIR and CACHE_FILE to a temporary directory. No real
API key, paid request, or write to the repository's clinical data is needed.
"""

import copy
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

from openai_summary import SUMMARY_SCHEMA, response_request, response_text


ROOT = Path(__file__).resolve().parent
DATE = "2026-10-15"
NOW = "2026-10-15T06:00:00+08:00"
CONFIG_KEYS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LLM_PROVIDER", "MODEL",
    "OPENAI_MODEL", "OPENAI_REASONING_EFFORT", "OPENAI_MAX_OUTPUT_TOKENS",
    "USE_BATCH", "BATCH_WAIT_MIN", "BATCH_CHECKPOINT",
)
GOOD = {
    "title_zh": "中文標題", "abstract_zh": "完整的繁體中文摘要", "evidence_level": "RCT",
    "score": 7, "score_reason": "隨機對照研究", "relevance": "高度相關",
    "relevance_why": "大腸直腸外科患者", "novelty": "再確認已知",
    "one_liner": "兩組的局部復發率不同", "action": "目前不需改變做法",
    "pico": {"P": "200 人", "I": "介入組", "C": "對照組", "O": "局部復發"},
    "effect_data": [], "key_numbers": ["介入組 10/100，對照組 20/100"],
    "cautions": ["單一中心", "信賴區間未報告", "追蹤時間短"],
    "unassessable": ["盲法"], "context_verdict": "無相關前作", "context": "無相關前作",
}


def paper(pmid, title="Paper"):
    return {"pmid": pmid, "title": title, "journal": "Ann Coloproctol",
            "ptype": "Randomized Controlled Trial", "abstract": "Abstract text",
            "related": []}


def response_body(summary=None, **changes):
    body = {
        "id": "resp_test", "object": "response", "status": "completed",
        "model": "gpt-6.1-sol", "error": None,
        "output": [
            {"type": "reasoning", "summary": [], "text": "Do not parse reasoning"},
            {"type": "message", "role": "assistant", "status": "completed",
             "content": [{"type": "output_text", "text": json.dumps(
                 GOOD if summary is None else summary, ensure_ascii=False)}]},
        ],
        "usage": {"input_tokens": 2000, "output_tokens": 3600,
                  "output_tokens_details": {"reasoning_tokens": 1000}},
    }
    body.update(changes)
    return body


def result_row(custom_id, body=None, status_code=200, error=None):
    return {"id": "batch_req_" + custom_id, "custom_id": custom_id,
            "response": None if error else {
                "status_code": status_code, "request_id": "req_test",
                "body": response_body() if body is None else body},
            "error": error}


class FakeResponse:
    def __init__(self, body=None, text="", status_code=200):
        self.body, self.text, self.status_code = body, text, status_code

    def json(self):
        return copy.deepcopy(self.body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


class FakeOpenAI:
    """Emulate files, batches, and independently ordered output/error JSONL."""

    def __init__(self, status="completed", output_rows=None, error_rows=None):
        self.status = status
        self.output_rows = output_rows
        self.error_rows = error_rows or []
        self.calls = []
        self.jobs = []
        self.created = 0
        self.uploaded = 0
        self.create_payload = None
        self.create_error = None

    def __call__(self, method, url, **kw):
        self.calls.append((method, url, kw))
        if method == "POST" and url == "https://api.openai.com/v1/files":
            self.uploaded += 1
            assert kw["data"]["purpose"] == "batch"
            uploaded = kw["files"]["file"]
            content = uploaded[1] if isinstance(uploaded, tuple) else uploaded
            content = content.read() if hasattr(content, "read") else content
            content = content.decode("utf-8") if isinstance(content, bytes) else content
            self.jobs = [json.loads(line) for line in content.splitlines() if line.strip()]
            return FakeResponse({"id": "file_input", "object": "file", "purpose": "batch"})
        if method == "POST" and url == "https://api.openai.com/v1/batches":
            self.created += 1
            self.create_payload = copy.deepcopy(kw["json"])
            if self.create_error:
                raise self.create_error
            return FakeResponse({"id": "batch_test", "status": "validating"})
        if method == "GET" and url == "https://api.openai.com/v1/batches/batch_test":
            return FakeResponse({
                "id": "batch_test", "status": self.status,
                "request_counts": {"total": len(self.jobs), "completed": len(self.jobs),
                                   "failed": len(self.error_rows)},
                "output_file_id": "file_output" if self.status == "completed" else None,
                "error_file_id": "file_errors" if self.error_rows else None,
            })
        if method == "GET" and url == "https://api.openai.com/v1/files/file_output/content":
            rows = self.output_rows
            if rows is None:
                rows = [result_row(job["custom_id"], response_body(
                    {**GOOD, "title_zh": "摘要 " + job["custom_id"]}))
                    for job in reversed(self.jobs)]
            return FakeResponse(text="\n".join(json.dumps(row, ensure_ascii=False) for row in rows))
        if method == "GET" and url == "https://api.openai.com/v1/files/file_errors/content":
            return FakeResponse(text="\n".join(json.dumps(row) for row in self.error_rows))
        raise AssertionError(f"Unexpected fake HTTP request: {method} {url}")


class IsolatedTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="pubmed-openai-test-")
        self.addCleanup(self.tmp.cleanup)
        self.data_dir = Path(self.tmp.name)
        self.environment = patch.dict(os.environ)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for key in CONFIG_KEYS:
            os.environ.pop(key, None)
        # These are explicit fake values, never inherited account credentials.
        os.environ.update({"OPENAI_API_KEY": "test-openai-not-a-real-key",
                           "ANTHROPIC_API_KEY": "test-anthropic-not-a-real-key",
                           "USE_BATCH": "1", "BATCH_WAIT_MIN": "0"})
        self.network = patch.object(requests.sessions.Session, "request",
                                    side_effect=AssertionError("Real HTTP is forbidden in tests"))
        self.network.start()
        self.addCleanup(self.network.stop)
        self.fp = self.load_module()
        self.process = patch.object(self.fp.subprocess, "run", side_effect=AssertionError(
            "Real subprocesses, including Git checkpoints, are forbidden in tests"))
        self.process.start()
        self.addCleanup(self.process.stop)
        self.quiet = patch.object(self.fp, "log")
        self.quiet.start()
        self.addCleanup(self.quiet.stop)
        self.sleep = patch.object(self.fp.time, "sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def load_module(self):
        spec = importlib.util.spec_from_file_location("fp_openai_test", ROOT / "fetch_papers.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.DATA_DIR = self.data_dir
        module.CACHE_FILE = self.data_dir / "_cache_v3.json"
        return module

    def pending(self, **changes):
        value = {"batch_id": "batch_test", "date": DATE, "submitted_at": NOW,
                 "papers": [paper("40111111")], "provider": "openai",
                 "model": "gpt-6.1-sol", "submission_id": "submission-test"}
        value.update(changes)
        self.fp.pending_path().write_text(json.dumps(value), encoding="utf-8")
        return value


class ConfigurationTests(IsolatedTestCase):
    def test_openai_key_takes_precedence_and_legacy_model_cannot_override_sol(self):
        os.environ["MODEL"] = "claude-sonnet-5"
        fp = self.load_module()
        self.assertEqual(fp.PROVIDER, "openai")
        self.assertEqual(fp.API_KEY, "test-openai-not-a-real-key")
        self.assertEqual(fp.MODEL, "gpt-6.1-sol")

    def test_openai_model_is_explicitly_configurable(self):
        os.environ["OPENAI_MODEL"] = "  gpt-6.1-sol-custom  "
        self.assertEqual(self.load_module().MODEL, "gpt-6.1-sol-custom")

    def test_no_keys_defaults_to_sol_without_authorizing_a_request(self):
        os.environ.pop("OPENAI_API_KEY")
        os.environ.pop("ANTHROPIC_API_KEY")
        fp = self.load_module()
        self.assertEqual((fp.PROVIDER, fp.MODEL, fp.API_KEY), ("openai", "gpt-6.1-sol", ""))
        self.assertIsNone(fp.summarize(paper("40111111")))

    def test_missing_openai_key_retains_legacy_fallback(self):
        os.environ["OPENAI_API_KEY"] = "   "
        os.environ["MODEL"] = "claude-sonnet-5"
        fp = self.load_module()
        self.assertEqual((fp.PROVIDER, fp.MODEL), ("anthropic", "claude-sonnet-5"))
        self.assertEqual(fp.API_KEY, "test-anthropic-not-a-real-key")

    def test_explicit_provider_selection_and_invalid_provider(self):
        os.environ["LLM_PROVIDER"] = "anthropic"
        self.assertEqual(self.load_module().PROVIDER, "anthropic")
        os.environ["LLM_PROVIDER"] = "openai"
        os.environ.pop("OPENAI_API_KEY")
        fp = self.load_module()
        self.assertEqual(fp.PROVIDER, "openai")
        self.assertEqual(fp.API_KEY, "")
        os.environ["LLM_PROVIDER"] = "unknown"
        with self.assertRaises(ValueError):
            self.load_module()

    def test_headers_use_provider_credentials_and_allow_multipart_boundary(self):
        headers = self.fp.api_headers()
        self.assertEqual(headers["Authorization"], "Bearer test-openai-not-a-real-key")
        self.assertNotIn("x-api-key", headers)
        multipart = {key.lower(): value for key, value in
                     self.fp.api_headers(multipart=True).items()}
        self.assertNotIn("content-type", multipart)
        legacy = self.fp.api_headers(provider="anthropic")
        self.assertEqual(legacy["x-api-key"], "test-anthropic-not-a-real-key")
        self.assertEqual(legacy["anthropic-version"], "2023-06-01")
        self.assertNotIn("Authorization", legacy)

    def test_request_config_does_not_send_legacy_messages_parameters(self):
        request = self.fp.summary_request("臨床摘要")
        self.assertEqual(request, response_request("臨床摘要", "gpt-6.1-sol"))
        self.assertFalse({"messages", "max_tokens", "temperature", "top_p"} & request.keys())
        self.assertEqual(request["input"][0], {"role": "user", "content": "臨床摘要"})
        self.assertFalse(request["store"])

    def test_reasoning_and_output_limit_configuration(self):
        os.environ.update({"OPENAI_REASONING_EFFORT": "high", "OPENAI_MAX_OUTPUT_TOKENS": "24000"})
        request = self.load_module().summary_request("Prompt")
        self.assertEqual(request["reasoning"], {"effort": "high"})
        self.assertEqual(request["max_output_tokens"], 24000)


class SchemaTests(unittest.TestCase):
    def test_all_nested_objects_are_strict_with_all_fields_required(self):
        def check(node):
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    check(child)
            if node.get("type") == "array":
                check(node["items"])
        check(SUMMARY_SCHEMA)
        self.assertEqual(set(SUMMARY_SCHEMA["properties"]), set(GOOD))
        fmt = response_request("Prompt", "gpt-6.1-sol")["text"]["format"]
        self.assertEqual(fmt["type"], "json_schema")
        self.assertIs(fmt["strict"], True)
        self.assertEqual(fmt["schema"], SUMMARY_SCHEMA)

    def test_schema_preserves_clinical_enums_and_effect_limits(self):
        props = SUMMARY_SCHEMA["properties"]
        self.assertEqual(props["score"], {"type": "integer", "minimum": 0, "maximum": 10})
        self.assertEqual(set(props["novelty"]["enum"]),
                         {"可能改變實務", "再確認已知", "仍屬早期", "結果為陰性"})
        self.assertNotIn("與前作矛盾", props["novelty"]["enum"])
        self.assertIn("與前作矛盾", props["context_verdict"]["enum"])
        self.assertEqual((props["cautions"]["minItems"], props["cautions"]["maxItems"]), (3, 3))
        self.assertEqual(props["effect_data"]["maxItems"], 3)
        arm = props["effect_data"]["items"]["properties"]["intervention"]["properties"]
        for key in ("events", "total", "rate"):
            self.assertEqual(set(arm[key]["type"]), {"number", "null"})

    def test_invalid_reasoning_effort_and_output_budget_fail_before_http(self):
        with self.assertRaises(ValueError):
            response_request("Prompt", "gpt-6.1-sol", effort="unlimited")
        for limit in (0, -1, 128001):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                response_request("Prompt", "gpt-6.1-sol", max_output_tokens=limit)


class ResponseTests(IsolatedTestCase):
    def test_parses_only_assistant_text_and_counts_all_output_tokens(self):
        body = response_body()
        body["output"].insert(0, {"type": "message", "role": "user",
                                  "content": [{"type": "output_text", "text": "not JSON"}]})
        result = self.fp.parse_summary(body)
        self.assertEqual(result["abstract_zh"], GOOD["abstract_zh"])
        self.assertEqual(result["summary_provider"], "openai")
        self.assertEqual(result["summary_model"], "gpt-6.1-sol")
        # output_tokens already includes reasoning; neither subtract nor add it.
        self.assertEqual((result["_usage"]["in"], result["_usage"]["out"]), (2000, 3600))
        self.assertEqual(result["_usage"]["reasoning"], 1000)

    def test_multiple_assistant_text_blocks_are_concatenated(self):
        body = response_body()
        text = json.dumps(GOOD)
        body["output"][1]["content"] = [
            {"type": "output_text", "text": text[:30]},
            {"type": "output_text", "text": text[30:]},
        ]
        self.assertEqual(json.loads(response_text(body)), GOOD)

    def test_incomplete_failed_error_and_refusal_responses_are_rejected(self):
        bad = [response_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"}),
               response_body(status="failed"), response_body(status="in_progress"),
               response_body(status=None), response_body(error={"code": "server_error"}),
               response_body(output=[])]
        refusal = response_body()
        refusal["output"][1]["content"].append({"type": "refusal", "refusal": "Cannot comply"})
        bad.append(refusal)
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.fp.parse_summary(body)

    def test_invalid_json_and_clinical_enum_are_not_guessed(self):
        for summary in ({**GOOD, "novelty": "與前作矛盾"},
                        {**GOOD, "relevance": "超級相關"},
                        {**GOOD, "context_verdict": "可能改變實務"},
                        {**GOOD, "abstract_zh": ""}):
            with self.subTest(summary=summary), self.assertRaises(ValueError):
                self.fp.parse_summary(response_body(summary))
        body = response_body()
        body["output"][1]["content"][0]["text"] = "{invalid JSON"
        with self.assertRaises(ValueError):
            self.fp.parse_summary(body)

    def test_incomplete_assistant_item_is_rejected_even_when_outer_response_completed(self):
        body = response_body()
        body["output"][1]["status"] = "incomplete"
        with self.assertRaises(ValueError):
            self.fp.parse_summary(body)

    def test_empty_required_clinical_fields_are_rejected(self):
        variants = [{field: "   "} for field in
                    ("title_zh", "abstract_zh", "evidence_level", "one_liner", "action")]
        variants += [{"score": None}, {"key_numbers": []}, {"cautions": []},
                     {"pico": {"P": "", "I": "", "C": "", "O": ""}},
                     {"pico": {**GOOD["pico"], "C": "   "}}]
        for changes in variants:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.fp.parse_summary(response_body({**GOOD, **changes}))

    def test_openai_requires_exactly_three_nonempty_cautions(self):
        for cautions in ([], ["一"], ["一", "二"], ["一", "二", "   "],
                         ["一", "二", "三", "四"]):
            with self.subTest(cautions=cautions), self.assertRaises(ValueError):
                self.fp.parse_summary(response_body({**GOOD, "cautions": cautions}))

    def test_effects_still_use_deterministic_absolute_risk_calculation(self):
        arm = lambda events: {"label": "組別", "events": events, "total": 100, "rate": None}
        effects = [
            {"outcome": "復發", "event_is_bad": True, "intervention": arm(10), "control": arm(20)},
            {"outcome": "感染", "event_is_bad": True, "intervention": arm(20), "control": arm(10)},
            {"outcome": "相對風險", "event_is_bad": True,
             "intervention": {"label": "組別", "events": None, "total": None, "rate": None},
             "control": {"label": "對照", "events": None, "total": None, "rate": None}},
        ]
        result = self.fp.parse_summary(response_body({**GOOD, "effect_data": effects}))["effects"]
        self.assertEqual((result[0]["arr"], result[0]["nnt"], result[0]["kind"]), (10, 10, "NNT"))
        self.assertEqual((result[1]["arr"], result[1]["nnt"], result[1]["kind"]), (10, 10, "NNH"))
        self.assertFalse(result[2]["ok"])
        self.assertNotIn("nnt", result[2])

    def test_sync_request_uses_responses_and_same_strict_contract(self):
        with patch.object(self.fp.requests, "post", return_value=FakeResponse(response_body())) as post:
            result = self.fp.summarize(paper("40111111"))
        self.assertEqual(result["summary_provider"], "openai")
        args, kwargs = post.call_args
        self.assertEqual(args, ("https://api.openai.com/v1/responses",))
        self.assertEqual(kwargs["json"], self.fp.summary_request(self.fp.build_summary_prompt(paper("40111111"))))

    def test_sync_model_error_does_not_fall_back_to_anthropic(self):
        with patch.object(self.fp.requests, "post", return_value=FakeResponse(status_code=404)) as post:
            self.assertIsNone(self.fp.summarize(paper("40111111")))
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "https://api.openai.com/v1/responses")


class BatchTests(IsolatedTestCase):
    def test_jsonl_upload_and_batch_create_use_responses_contract(self):
        api = FakeOpenAI()
        self.fp.save_pending(None, DATE, [paper("40111111"), paper("40222222")], "submission-test")
        with patch.object(self.fp.requests, "request", side_effect=api):
            self.assertEqual(self.fp.batch_submit([("40111111", "提示一"), ("40222222", "提示二")],
                                                 submission_id="submission-test"), "batch_test")
        self.assertEqual((api.uploaded, api.created), (1, 1))
        self.assertEqual([job["custom_id"] for job in api.jobs], ["40111111", "40222222"])
        for job, prompt in zip(api.jobs, ("提示一", "提示二")):
            self.assertEqual(job["method"], "POST")
            self.assertEqual(job["url"], "/v1/responses")
            self.assertEqual(job["body"], self.fp.summary_request(prompt))
        self.assertEqual(api.create_payload["input_file_id"], "file_input")
        self.assertEqual(api.create_payload["endpoint"], "/v1/responses")
        self.assertEqual(api.create_payload["completion_window"], "24h")
        self.assertEqual(api.create_payload["metadata"]["submission_id"], "submission-test")
        upload_headers = {key.lower(): value for key, value in api.calls[0][2]["headers"].items()}
        self.assertNotIn("content-type", upload_headers)
        self.assertEqual(upload_headers["authorization"], "Bearer test-openai-not-a-real-key")

    def test_out_of_order_results_match_custom_id_and_keep_reasoning_usage(self):
        api = FakeOpenAI()
        papers = [paper("40111111", "First"), paper("40222222", "Second")]
        cache = {}
        with patch.object(self.fp.requests, "request", side_effect=api):
            result = self.fp.summarize_batch(papers, DATE, cache, NOW)
        self.assertEqual(result, (2, 0, 4000, 7200, False))
        self.assertEqual([p["title_zh"] for p in papers], ["摘要 40111111", "摘要 40222222"])
        self.assertEqual(set(cache), {"40111111", "40222222"})
        self.assertFalse(self.fp.pending_path().exists())

    def test_downloads_output_and_error_files_and_caches_only_valid_summaries(self):
        ids = [str(40111111 + i) for i in range(5)]
        refusal = response_body()
        refusal["output"][1]["content"] = [{"type": "refusal", "refusal": "Cannot comply"}]
        api = FakeOpenAI(output_rows=[
            result_row(ids[2], response_body(status="incomplete", incomplete_details={"reason": "max_output_tokens"})),
            result_row(ids[0]), result_row(ids[3], refusal),
            result_row(ids[1], response_body({**GOOD, "novelty": "與前作矛盾"})),
        ], error_rows=[result_row(ids[4], error={"code": "batch_expired", "message": "Expired"})])
        papers, cache = [paper(pmid) for pmid in ids], {}
        with patch.object(self.fp.requests, "request", side_effect=api):
            result = self.fp.summarize_batch(papers, DATE, cache, NOW)
        self.assertEqual(result[:2], (1, 4))
        # Failed parsing/refusal still consumed billable response tokens.
        self.assertEqual(result[2:4], (8000, 14400))
        self.assertEqual(len(self.fp.USAGE_LEDGER), 4)
        self.assertEqual(set(cache), {ids[0]})
        self.assertTrue(all(p.get("added_at") for p in papers))
        self.assertTrue(all("abstract_zh" not in p for p in papers[1:]))
        downloads = [url for method, url, _ in api.calls if method == "GET" and url.endswith("/content")]
        self.assertCountEqual(downloads, ["https://api.openai.com/v1/files/file_output/content",
                                          "https://api.openai.com/v1/files/file_errors/content"])

    def test_http_error_rows_are_not_accepted_as_successful_responses(self):
        api = FakeOpenAI(output_rows=[result_row("40111111", status_code=400)])
        status = {"status": "completed", "output_file_id": "file_output"}
        with patch.object(self.fp.requests, "request", side_effect=api):
            messages, _ = self.fp.batch_fetch(status, provider="openai")
        self.assertNotIn("40111111", messages)

    def test_terminal_statuses_stop_waiting_even_without_output(self):
        for status in ("completed", "failed", "expired", "cancelled"):
            with self.subTest(status=status):
                api = FakeOpenAI(status=status)
                with patch.object(self.fp.requests, "request", side_effect=api):
                    result = self.fp.batch_wait("batch_test", max_wait_min=0, provider="openai")
                self.assertEqual(result["status"], status)
                self.assertEqual(len(api.calls), 1)

    def test_timeout_persists_metadata_and_resume_never_resubmits(self):
        api = FakeOpenAI(status="in_progress")
        cache = {}
        with patch.object(self.fp.requests, "request", side_effect=api):
            result = self.fp.summarize_batch([paper("40111111")], DATE, cache, NOW)
            self.assertEqual(result, (0, 0, 0, 0, True))
            pending = self.fp.load_pending()
            self.assertEqual(pending["provider"], "openai")
            self.assertEqual(pending["model"], "gpt-6.1-sol")
            self.assertTrue(pending["submission_id"])
            self.assertEqual(pending["batch_id"], "batch_test")
            self.assertEqual(pending["papers"][0]["pmid"], "40111111")
            api.status = "completed"
            self.assertTrue(self.fp.resume_pending(cache))
        self.assertEqual((api.uploaded, api.created), (1, 1))
        self.assertEqual(set(cache), {"40111111"})
        self.assertTrue(self.fp.CACHE_FILE.exists())
        self.assertTrue((self.data_dir / (DATE + ".json")).exists())
        self.assertFalse(self.fp.pending_path().exists())

    def test_existing_pending_file_cannot_be_overwritten_by_new_batch(self):
        self.pending()
        before = self.fp.pending_path().read_bytes()
        with patch.object(self.fp.requests, "request") as request:
            with self.assertRaises(RuntimeError):
                self.fp.summarize_batch([paper("40222222")], DATE, {}, NOW)
        request.assert_not_called()
        self.assertEqual(self.fp.pending_path().read_bytes(), before)

    def test_main_stops_before_pubmed_or_new_batch_while_pending_remains(self):
        self.pending()
        with patch.object(self.fp, "resume_pending", return_value=False), \
                patch.object(self.fp, "search_pubmed") as search, \
                patch.object(self.fp, "summarize_batch") as submit:
            self.fp.main()
        search.assert_not_called()
        submit.assert_not_called()
        self.assertTrue(self.fp.pending_path().exists())

    def test_resume_status_failure_preserves_pending_for_later(self):
        self.pending()
        before = self.fp.pending_path().read_bytes()
        with patch.object(self.fp, "batch_status", side_effect=requests.Timeout("temporary")):
            self.assertFalse(self.fp.resume_pending({}))
        self.assertEqual(self.fp.pending_path().read_bytes(), before)

    def test_legacy_anthropic_pending_resumes_with_original_provider_and_key(self):
        pending = self.pending(batch_id="msgbatch_legacy", model="claude-sonnet-5")
        pending.pop("provider")
        pending.pop("model")
        pending.pop("submission_id")
        self.fp.pending_path().write_text(json.dumps(pending), encoding="utf-8")
        legacy_body = {"content": [{"type": "text", "text": json.dumps(GOOD)}],
                       "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 200}}
        calls = []

        def legacy_request(method, url, **kw):
            calls.append((method, url, kw))
            self.assertEqual(method, "GET")
            self.assertEqual(kw["headers"]["x-api-key"], "test-anthropic-not-a-real-key")
            self.assertNotIn("Authorization", kw["headers"])
            if url == "https://api.anthropic.com/v1/messages/batches/msgbatch_legacy":
                return FakeResponse({"processing_status": "ended", "request_counts": {"succeeded": 1},
                                     "results_url": "https://api.anthropic.com/legacy-results"})
            if url == "https://api.anthropic.com/legacy-results":
                return FakeResponse(text=json.dumps({"custom_id": "40111111", "result": {
                    "type": "succeeded", "message": legacy_body}}))
            raise AssertionError(f"Unexpected legacy request: {method} {url}")

        cache = {}
        with patch.object(self.fp.requests, "request", side_effect=legacy_request):
            self.assertTrue(self.fp.resume_pending(cache))
        self.assertEqual(len(calls), 2)
        self.assertEqual(cache["40111111"]["summary_provider"], "anthropic")
        self.assertEqual(cache["40111111"]["summary_model"], "claude-sonnet-5")
        self.assertFalse(self.fp.pending_path().exists())

    def test_ambiguous_batch_creation_timeout_does_not_create_twice(self):
        api = FakeOpenAI()
        api.create_error = requests.Timeout("Server may already have accepted this batch")
        with patch.object(self.fp.requests, "request", side_effect=api):
            with self.assertRaises(requests.RequestException):
                self.fp.summarize_batch([paper("40111111")], DATE, {}, NOW)
        self.assertEqual(api.created, 1)
        pending = self.fp.load_pending()
        self.assertIsNone(pending["batch_id"])
        self.assertEqual(pending["state"], "submitting")
        self.assertEqual(pending["input_file_id"], "file_input")
        self.assertEqual(pending["submission_id"], api.create_payload["metadata"]["submission_id"])

    def test_duplicate_custom_ids_fail_before_upload(self):
        with patch.object(self.fp.requests, "request") as request:
            with self.assertRaises(ValueError):
                self.fp.batch_submit([("40111111", "Prompt"), ("40111111", "Prompt again")])
        request.assert_not_called()

    def test_completed_resume_keeps_other_papers_already_published_that_day(self):
        existing = {**paper("40999999"), **GOOD, "added_at": NOW}
        self.fp.save([existing], DATE)
        self.pending()
        api = FakeOpenAI(output_rows=[result_row("40111111")])
        with patch.object(self.fp.requests, "request", side_effect=api):
            self.assertTrue(self.fp.resume_pending({}))
        day = json.loads((self.data_dir / (DATE + ".json")).read_text(encoding="utf-8"))
        self.assertEqual({p["pmid"] for p in day["papers"]}, {"40111111", "40999999"})

    def test_merge_day_never_downgrades_existing_valid_same_pmid_after_failure(self):
        existing = {**paper("40111111"), **GOOD, "added_at": NOW}
        self.fp.save([existing], DATE)
        failed_retry = {**paper("40111111", "Retry has no summary"), "added_at": NOW}
        self.fp.merge_day([failed_retry], DATE)
        day = json.loads((self.data_dir / (DATE + ".json")).read_text(encoding="utf-8"))
        self.assertEqual(len(day["papers"]), 1)
        self.assertEqual(day["papers"][0], existing)

    def test_invalid_config_fails_before_journal_or_network(self):
        with patch.object(self.fp, "OPENAI_REASONING_EFFORT", "unlimited"), \
                patch.object(self.fp.requests, "request") as request:
            with self.assertRaises(ValueError):
                self.fp.summarize_batch([paper("40111111")], DATE, {}, NOW)
        request.assert_not_called()
        self.assertFalse(self.fp.pending_path().exists())

    def test_resume_uses_saved_provider_and_model_after_config_changes(self):
        self.pending(model="gpt-6.1-sol-original")
        body = response_body()
        body.pop("model")
        api = FakeOpenAI(output_rows=[result_row("40111111", body)])
        cache = {}
        with patch.object(self.fp, "PROVIDER", "anthropic"), \
                patch.object(self.fp, "MODEL", "claude-sonnet-5"), \
                patch.object(self.fp.requests, "request", side_effect=api):
            self.assertTrue(self.fp.resume_pending(cache))
        self.assertEqual(cache["40111111"]["summary_provider"], "openai")
        self.assertEqual(cache["40111111"]["summary_model"], "gpt-6.1-sol-original")
        self.assertTrue(all("api.openai.com" in url for _, url, _ in api.calls))
        self.assertTrue(all(kw["headers"]["Authorization"] == "Bearer test-openai-not-a-real-key"
                            for _, _, kw in api.calls))

    def test_missing_original_provider_key_keeps_pending_and_sends_nothing(self):
        self.pending()
        with patch.object(self.fp, "OPENAI_API_KEY", ""), \
                patch.object(self.fp.requests, "request") as request:
            self.assertFalse(self.fp.resume_pending({}))
        request.assert_not_called()
        self.assertTrue(self.fp.pending_path().exists())

    def test_corrupt_pending_stops_main_without_replacing_journal(self):
        self.fp.pending_path().write_text("{interrupted journal", encoding="utf-8")
        with patch.object(self.fp, "search_pubmed") as search, \
                patch.object(self.fp.requests, "request") as request:
            self.fp.main()
        search.assert_not_called()
        request.assert_not_called()
        self.assertEqual(self.fp.pending_path().read_text(encoding="utf-8"), "{interrupted journal")

    def test_preparing_journal_can_clear_before_any_paid_submission(self):
        self.pending(batch_id=None, state="preparing")
        with patch.object(self.fp.requests, "request") as request:
            self.assertFalse(self.fp.resume_pending({}))
        request.assert_not_called()
        self.assertFalse(self.fp.pending_path().exists())

    def test_rejected_creation_clears_journal_but_server_failure_retains_it(self):
        for status in (400, 401, 422, 500, 503):
            with self.subTest(status=status):
                self.fp.clear_pending()
                api = FakeOpenAI()
                response = FakeResponse(status_code=status)
                api.create_error = requests.HTTPError("Rejected or uncertain", response=response)
                with patch.object(self.fp.requests, "request", side_effect=api):
                    with self.assertRaises(requests.HTTPError):
                        self.fp.summarize_batch([paper("40111111")], DATE, {}, NOW)
                self.assertEqual(api.created, 1)
                self.assertEqual(self.fp.pending_path().exists(), status >= 500)

    def test_http_408_creation_keeps_ambiguous_journal_and_does_not_retry(self):
        api = FakeOpenAI()
        api.create_error = requests.HTTPError("Request timeout", response=FakeResponse(status_code=408))
        with patch.object(self.fp.requests, "request", side_effect=api):
            with self.assertRaises(requests.HTTPError):
                self.fp.summarize_batch([paper("40111111")], DATE, {}, NOW)
        self.assertEqual(api.created, 1)
        self.assertEqual(self.fp.load_pending()["state"], "submitting")
        self.assertIsNone(self.fp.load_pending()["batch_id"])

    def test_cancelled_batch_with_zero_executed_requests_clears_pending(self):
        self.pending(papers=[paper("40111111"), paper("40222222")])
        status = {"id": "batch_test", "status": "cancelled",
                  "request_counts": {"total": 2, "completed": 0, "failed": 0}}
        cache = {}
        with patch.object(self.fp, "batch_status", return_value=status), \
                patch.object(self.fp.requests, "request") as request:
            self.assertFalse(self.fp.resume_pending(cache))
        request.assert_not_called()
        self.assertEqual(cache, {})
        self.assertFalse(self.fp.pending_path().exists())

    def test_cancelled_partial_results_match_counts_and_preserve_valid_summary(self):
        self.pending(papers=[paper("40111111"), paper("40222222"), paper("40333333")])
        status = {"id": "batch_test", "status": "cancelled", "output_file_id": "file_output",
                  "error_file_id": "file_errors",
                  "request_counts": {"total": 3, "completed": 1, "failed": 1}}
        api = FakeOpenAI(output_rows=[result_row("40111111")], error_rows=[
            result_row("40222222", error={"code": "request_error", "message": "Failed"})])
        cache = {}
        with patch.object(self.fp, "batch_status", return_value=status), \
                patch.object(self.fp.requests, "request", side_effect=api):
            self.assertTrue(self.fp.resume_pending(cache))
        self.assertEqual(set(cache), {"40111111"})
        self.assertFalse(self.fp.pending_path().exists())
        self.assertEqual((api.created, api.uploaded), (0, 0))

    def test_cancelled_truncated_results_keep_pending(self):
        self.pending(papers=[paper("40111111"), paper("40222222")])
        status = {"id": "batch_test", "status": "cancelled", "output_file_id": "file_output",
                  "request_counts": {"total": 2, "completed": 2, "failed": 0}}
        api = FakeOpenAI(output_rows=[result_row("40111111")])
        cache = {}
        with patch.object(self.fp, "batch_status", return_value=status), \
                patch.object(self.fp.requests, "request", side_effect=api):
            with self.assertRaises(ValueError):
                self.fp.resume_pending(cache)
        self.assertTrue(self.fp.pending_path().exists())
        self.assertEqual(cache, {})

    def test_missing_duplicate_and_unexpected_results_preserve_pending(self):
        variants = {
            "missing": [],
            "duplicate": [result_row("40111111"), result_row("40111111")],
            "unexpected": [result_row("40111111"), result_row("40999999")],
            "missing custom_id": [{"response": {"status_code": 200, "body": response_body()}}],
        }
        for name, rows in variants.items():
            with self.subTest(name=name):
                self.pending()
                api = FakeOpenAI(output_rows=rows)
                cache = {}
                with patch.object(self.fp.requests, "request", side_effect=api):
                    with self.assertRaises(ValueError):
                        self.fp.resume_pending(cache)
                self.assertTrue(self.fp.pending_path().exists())
                self.assertEqual(cache, {})

    def test_malformed_result_file_keeps_pending_for_recovery(self):
        self.pending()
        api = FakeOpenAI()

        def malformed_download(method, url, **kw):
            if url.endswith("/content"):
                return FakeResponse(text="{truncated JSONL")
            return api(method, url, **kw)

        with patch.object(self.fp.requests, "request", side_effect=malformed_download):
            with self.assertRaises(ValueError):
                self.fp.resume_pending({})
        self.assertTrue(self.fp.pending_path().exists())

    def test_cache_write_failure_does_not_discard_paid_batch_reference(self):
        self.pending()
        api = FakeOpenAI(output_rows=[result_row("40111111")])
        with patch.object(self.fp.requests, "request", side_effect=api), \
                patch.object(self.fp, "save_cache", side_effect=OSError("Disk full")):
            with self.assertRaises(OSError):
                self.fp.resume_pending({})
        self.assertTrue(self.fp.pending_path().exists())


class RecoveryTests(IsolatedTestCase):
    def test_lost_creation_response_is_recovered_via_paginated_metadata_search(self):
        self.pending(batch_id=None, state="submitting", input_file_id="file_input")
        api = FakeOpenAI(output_rows=[result_row("40111111")])
        pages = iter([
            {"data": [{"id": "batch_other", "metadata": {"submission_id": "other"}}],
             "has_more": True, "last_id": "batch_other"},
            {"data": [{"id": "batch_test", "metadata": {"submission_id": "submission-test"}}],
             "has_more": False},
        ])
        search_params = []

        def request(method, url, **kw):
            if method == "GET" and url == "https://api.openai.com/v1/batches":
                search_params.append(kw["params"])
                return FakeResponse(next(pages))
            return api(method, url, **kw)

        cache = {}
        with patch.object(self.fp.requests, "request", side_effect=request):
            self.assertTrue(self.fp.resume_pending(cache))
        self.assertEqual(search_params, [{"limit": 100}, {"limit": 100, "after": "batch_other"}])
        self.assertEqual((api.uploaded, api.created), (0, 0))
        self.assertEqual(set(cache), {"40111111"})
        self.assertFalse(self.fp.pending_path().exists())

    def test_ambiguous_submission_with_no_match_is_never_resubmitted(self):
        self.pending(batch_id=None, state="submitting")
        with patch.object(self.fp.requests, "request", return_value=FakeResponse({
                "data": [], "has_more": False})) as request:
            self.assertFalse(self.fp.resume_pending({}))
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args, ("GET", "https://api.openai.com/v1/batches"))
        self.assertTrue(self.fp.pending_path().exists())

    def test_multiple_matching_submissions_require_review_and_keep_journal(self):
        self.pending(batch_id=None, state="submitting")
        matching = [{"id": "batch_1", "metadata": {"submission_id": "submission-test"}},
                    {"id": "batch_2", "metadata": {"submission_id": "submission-test"}}]
        with patch.object(self.fp.requests, "request", return_value=FakeResponse({
                "data": matching, "has_more": False})) as request:
            with self.assertRaises(RuntimeError):
                self.fp.resume_pending({})
        self.assertEqual(request.call_count, 1)
        self.assertTrue(self.fp.pending_path().exists())

    def test_invalid_pagination_stops_without_losing_ambiguous_journal(self):
        self.pending(batch_id=None, state="submitting")
        with patch.object(self.fp.requests, "request", return_value=FakeResponse({
                "data": [], "has_more": True, "last_id": None})) as request:
            with self.assertRaises(ValueError):
                self.fp.resume_pending({})
        self.assertEqual(request.call_count, 1)
        self.assertTrue(self.fp.pending_path().exists())


class DataSafetyTests(IsolatedTestCase):
    def test_corrupt_cache_raises_instead_of_triggering_paid_regeneration(self):
        for content in ("{truncated", "[]", "null", "42", '{"40111111": null}',
                        '{"40111111": "not a summary"}'):
            with self.subTest(content=content):
                self.fp.CACHE_FILE.write_text(content, encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.fp.load_cache()
                self.assertEqual(self.fp.CACHE_FILE.read_text(encoding="utf-8"), content)

    def test_daily_rerun_with_smaller_selection_keeps_already_published_rows(self):
        existing = [{**paper(pmid), **GOOD, "added_at": NOW} for pmid in ("40111111", "40222222")]
        self.fp.save(existing, DATE)
        self.fp.save_cache({"40111111": {**GOOD, "added_at": NOW}})
        selected = [paper("40111111")]
        with patch.object(self.fp, "today_tw", return_value=self.fp.datetime.date.fromisoformat(DATE)), \
                patch.object(self.fp, "search_pubmed", return_value=["40111111"]), \
                patch.object(self.fp, "fetch_details", return_value=selected), \
                patch.object(self.fp, "select", return_value=(selected, "Test selection")), \
                patch.object(self.fp, "find_related", return_value=[]), \
                patch.object(self.fp.requests, "request") as request:
            self.fp.main()
        request.assert_not_called()
        day = json.loads((self.data_dir / (DATE + ".json")).read_text(encoding="utf-8"))
        self.assertEqual({p["pmid"] for p in day["papers"]}, {"40111111", "40222222"})
        self.assertTrue(all(p["abstract_zh"] == GOOD["abstract_zh"] for p in day["papers"]))


class CheckpointTests(IsolatedTestCase):
    def test_checkpoints_journal_before_paid_create_and_after_receiving_batch_id(self):
        os.environ["BATCH_CHECKPOINT"] = "1"
        api = FakeOpenAI(status="in_progress")
        events = []

        def checkpoint(*args, **kw):
            self.assertEqual(args, (["bash", str(ROOT / ".github/scripts/checkpoint_batch.sh")],))
            self.assertEqual(kw, {"cwd": ROOT, "check": True})
            events.append(("checkpoint", copy.deepcopy(self.fp.load_pending())))

        def request(method, url, **kw):
            events.append(("request", (method, url)))
            return api(method, url, **kw)

        with patch.object(self.fp.subprocess, "run", side_effect=checkpoint), \
                patch.object(self.fp.requests, "request", side_effect=request):
            result = self.fp.summarize_batch([paper("40111111")], DATE, {}, NOW)
        self.assertTrue(result[-1])
        checkpoints = [(i, value) for i, (kind, value) in enumerate(events) if kind == "checkpoint"]
        self.assertEqual([value["state"] for _, value in checkpoints],
                         ["preparing", "submitting", "submitted"])
        create_index = events.index(("request", ("POST", "https://api.openai.com/v1/batches")))
        self.assertLess(checkpoints[0][0], create_index)
        self.assertLess(checkpoints[1][0], create_index)
        self.assertEqual(checkpoints[1][1]["input_file_id"], "file_input")
        self.assertIsNone(checkpoints[1][1]["batch_id"])
        self.assertGreater(checkpoints[2][0], create_index)
        self.assertEqual(checkpoints[2][1]["batch_id"], "batch_test")
        poll_index = events.index(("request", ("GET", "https://api.openai.com/v1/batches/batch_test")))
        self.assertLess(checkpoints[2][0], poll_index)
        self.assertEqual(len({value["submission_id"] for _, value in checkpoints}), 1)

    def test_failed_checkpoint_prevents_paid_batch_creation(self):
        os.environ["BATCH_CHECKPOINT"] = "1"
        api = FakeOpenAI(status="in_progress")
        failure = self.fp.subprocess.CalledProcessError(1, "checkpoint_batch.sh")
        with patch.object(self.fp.subprocess, "run", side_effect=[None, failure]), \
                patch.object(self.fp.requests, "request", side_effect=api):
            with self.assertRaises(self.fp.subprocess.CalledProcessError):
                self.fp.summarize_batch([paper("40111111")], DATE, {}, NOW)
        self.assertEqual(api.created, 0)
        self.assertTrue(self.fp.pending_path().exists())


class UsageTests(IsolatedTestCase):
    def test_usage_ledger_prices_cached_input_and_includes_reasoning_once(self):
        body = response_body(usage={"input_tokens": 2000, "output_tokens": 3600,
                                   "input_tokens_details": {"cached_tokens": 500},
                                   "output_tokens_details": {"reasoning_tokens": 1000}})
        entry = self.fp.record_usage(body, batch=True)
        input_price, output_price = self.fp.price_of("gpt-6.1-sol", batch=True)
        expected = (1500 * input_price + 500 * input_price * 0.05 + 3600 * output_price) / 1e6
        self.assertAlmostEqual(entry["estimated_cost_usd"], expected)
        self.assertEqual(entry["output_tokens"], 3600)
        self.assertEqual(entry["reasoning_tokens"], 1000)
        self.assertEqual(entry["cached_tokens"], 500)
        self.assertTrue(entry["batch"])

    def test_batch_pricing_is_half_synchronous_for_sol(self):
        sync = self.fp.price_of("gpt-6.1-sol", batch=False)
        self.assertEqual(self.fp.price_of("gpt-6.1-sol", batch=True), tuple(price / 2 for price in sync))

    def test_sync_invalid_output_attempts_all_remain_in_usage_ledger(self):
        invalid = response_body({**GOOD, "novelty": "與前作矛盾"})
        with patch.object(self.fp.requests, "post", side_effect=[FakeResponse(invalid), FakeResponse(response_body())]):
            result = self.fp.summarize(paper("40111111"))
        self.assertIsNotNone(result)
        self.assertEqual(len(self.fp.USAGE_LEDGER), 2)
        self.assertEqual(sum(row["output_tokens"] for row in self.fp.USAGE_LEDGER), 7200)
        self.assertTrue(all(not row["batch"] for row in self.fp.USAGE_LEDGER))


class RetryTests(IsolatedTestCase):
    def test_nonretryable_client_errors_are_not_retried(self):
        for status in (400, 401, 403, 404, 422):
            with self.subTest(status=status):
                with patch.object(self.fp.requests, "request", return_value=FakeResponse(status_code=status)) as request:
                    with self.assertRaises(requests.HTTPError):
                        self.fp._api("GET", "https://api.openai.com/v1/batches/batch_test")
                self.assertEqual(request.call_count, 1)

    def test_transient_statuses_are_retried_for_reads(self):
        for status in (429, 500, 503):
            with self.subTest(status=status):
                with patch.object(self.fp.requests, "request", side_effect=[
                        FakeResponse(status_code=status), FakeResponse({"status": "completed"})]) as request:
                    result = self.fp._api("GET", "https://api.openai.com/v1/batches/batch_test")
                self.assertEqual(result.json()["status"], "completed")
                self.assertEqual(request.call_count, 2)

    def test_explicit_no_retry_applies_to_server_errors_and_timeouts(self):
        for failure in (FakeResponse(status_code=429), FakeResponse(status_code=503), requests.Timeout("ambiguous")):
            with self.subTest(failure=failure):
                kw = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(self.fp.requests, "request", **kw) as request:
                    with self.assertRaises(requests.RequestException):
                        self.fp._api("POST", "https://api.openai.com/v1/batches", retries=False,
                                     json={"input_file_id": "file_input"})
                self.assertEqual(request.call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
