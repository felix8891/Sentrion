from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

from app.services import llm_extraction
from app.services.processing import record_extraction_trace
from app.db import get_db
from .conftest import login_as, csrf_from

ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode('utf-8')

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload


def _completion(content: str):
    return {'choices': [{'message': {'content': content}}]}


def _configure_gemini(monkeypatch):
    monkeypatch.setenv('SENTRION_ENABLE_EXTERNAL_AI', 'true')
    monkeypatch.setenv('OPENAI_API_KEY', 'unit-test-gemini-api-key-do-not-log')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai')
    monkeypatch.setenv('OPENAI_MODEL', 'gemini-3.8-flash')
    monkeypatch.setenv('SENTRION_AI_TIMEOUT_SECONDS', '3')


def test_optional_llm_success_records_gemini_provider_model_and_safe_metadata(monkeypatch):
    _configure_gemini(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout):
        seen['url'] = request.full_url
        seen['authorization'] = request.get_header('Authorization')
        seen['body'] = json.loads(request.data.decode('utf-8'))
        seen['timeout'] = timeout
        return FakeResponse(_completion(json.dumps({
            'invoice_number': 'TRACE-SUCCESS-001',
            'total': 123.45,
            'items': [],
            'unapproved_secret_field': 'must be ignored',
        })))

    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', fake_urlopen)
    fields, source, trace = llm_extraction.optional_llm_extract(
        'synthetic invoice text, must never be copied to trace details',
        {'items': [{'item_name': 'Local item'}], 'currency': 'USD'},
    )

    assert seen['url'] == 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
    assert seen['authorization'] == 'Bearer unit-test-gemini-api-key-do-not-log'
    assert seen['body']['model'] == 'gemini-3.8-flash'
    assert seen['timeout'] == 3
    assert source == 'external_llm_structured_extraction'
    assert trace['extraction_source'] == source
    assert trace['provider'] == 'Google Gemini'
    assert trace['model'] == 'gemini-3.8-flash'
    assert trace['request_attempted'] is True
    assert trace['request_status'] == 'succeeded'
    assert trace['validated_json_object'] is True
    assert trace['fallback_used'] is False
    assert 'unit-test-gemini-api-key-do-not-log' not in json.dumps(trace)
    assert 'synthetic invoice text' not in json.dumps(trace)
    assert 'unapproved_secret_field' not in fields
    assert fields['invoice_number'] == 'TRACE-SUCCESS-001'
    assert fields['total'] == 123.45
    assert fields['items'] == [{'item_name': 'Local item'}]


def test_optional_llm_http_error_uses_standardized_local_fallback(monkeypatch):
    _configure_gemini(monkeypatch)

    def fail_urlopen(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 429, 'rate limit', {}, io.BytesIO(b'private response body')
        )

    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', fail_urlopen)
    original = {'invoice_number': 'LOCAL-001', 'items': []}
    fields, source, trace = llm_extraction.optional_llm_extract('private invoice text', original.copy())

    assert source == 'fallback_after_llm_error'
    assert trace['extraction_source'] == source
    assert trace['provider'] == 'Google Gemini'
    assert trace['request_attempted'] is True
    assert trace['request_status'] == 'failed'
    assert trace['fallback_used'] is True
    assert trace['error_type'] == 'HTTPError'
    assert fields['invoice_number'] == 'LOCAL-001'
    serialized = json.dumps(trace)
    assert 'unit-test-gemini-api-key-do-not-log' not in serialized
    assert 'private invoice text' not in serialized
    assert 'private response body' not in serialized


def test_optional_llm_disabled_does_not_call_provider(monkeypatch):
    monkeypatch.setenv('SENTRION_ENABLE_EXTERNAL_AI', 'false')
    monkeypatch.setenv('OPENAI_API_KEY', 'should-not-be-used')
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai')
    monkeypatch.setenv('OPENAI_MODEL', 'gemini-3.8-flash')

    def unexpected_call(*args, **kwargs):
        raise AssertionError('The provider must not be called when external AI is disabled.')

    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', unexpected_call)
    fields, source, trace = llm_extraction.optional_llm_extract('synthetic invoice text', {'invoice_number': 'LOCAL-002'})

    assert source == 'local_ocr_regex_demo'
    assert trace['request_status'] == 'disabled'
    assert trace['request_attempted'] is False
    assert trace['fallback_used'] is False
    assert fields['invoice_number'] == 'LOCAL-002'
    assert 'should-not-be-used' not in json.dumps(trace)


def test_optional_llm_missing_key_is_not_misreported_as_an_api_fallback(monkeypatch):
    monkeypatch.setenv('SENTRION_ENABLE_EXTERNAL_AI', 'true')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai')
    monkeypatch.setenv('OPENAI_MODEL', 'gemini-3.8-flash')
    fields, source, trace = llm_extraction.optional_llm_extract('synthetic invoice text', {'total': 50})
    assert source == 'local_ocr_regex_demo'
    assert trace['request_status'] == 'api_key_missing'
    assert trace['request_attempted'] is False
    assert trace['fallback_used'] is False
    assert fields['total'] == 50


def test_extraction_trace_audit_is_idempotent_and_renders_without_secrets(client):
    login_as(client)
    trace = {
        'provider': 'Google Gemini', 'provider_host': 'generativelanguage.googleapis.com',
        'model': 'gemini-3.8-flash', 'request_attempted': True, 'request_status': 'failed',
        'extraction_source': 'fallback_after_llm_error', 'fallback_used': True,
        'error_type': 'HTTPError', 'validated_json_object': False,
        'provided_field_names': [], 'merge_strategy': 'non_null_llm_values_overwrite_local_fields',
        'timestamp': '2026-10-17T10:00:00+00:00',
    }
    run_id = 'trace-idempotency-test-run'
    record_extraction_trace('inv-demo-0002', trace, run_id)
    record_extraction_trace('inv-demo-0002', trace, run_id)

    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_events WHERE invoice_id=? AND event_type='LLM_EXTRACTION_FALLBACK' AND instr(details_json, ?) > 0",
            ('inv-demo-0002', run_id),
        ).fetchall()
        assert len(rows) == 1
        details = json.loads(rows[0]['details_json'])
        assert details['extraction_source'] == 'fallback_after_llm_error'
        assert details['request_status'] == 'failed'
        assert details['fallback_used'] is True
        assert 'api_key' not in details and 'authorization' not in details

    page = client.get('/invoices/inv-demo-0002')
    audit_page = client.get('/audits?q=inv-demo-0002')
    assert page.status_code == 200 and audit_page.status_code == 200
    assert 'Local fallback after LLM error' in page.text
    assert 'Google Gemini' in page.text and 'gemini-3.8-flash' in page.text
    assert 'Fallback used (HTTPError)' in page.text
    assert 'Local fallback after LLM error' in audit_page.text
    assert 'unit-test-gemini-api-key-do-not-log' not in page.text
    client.cookies.clear()


def test_upload_logs_successful_gemini_extraction_in_action_history(client, monkeypatch):
    _configure_gemini(monkeypatch)
    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', lambda request, timeout: FakeResponse(
        _completion(json.dumps({'invoice_number': 'LLM-TRACE-001', 'total': 100, 'items': []}))
    ))
    login_as(client)
    csrf = csrf_from(client, '/invoices')
    import fitz
    document = fitz.open()
    page = document.new_page()
    page.insert_text((50, 50), 'Invoice No: LLM-TRACE-001\nInvoice Date: 2026-10-17\nDue Date: 2026-11-16\nSupplier: Traceability Test Supplier\nCurrency: USD\nSubtotal: USD 100.00\nTax: USD 0.00\nGrand Total: USD 100.00', fontsize=10)
    pdf_bytes = document.tobytes()
    document.close()
    response = client.post(
        '/invoices/upload', data={'csrf_token': csrf},
        files=[('files', ('llm-traceability-test.pdf', pdf_bytes, 'application/pdf'))],
        follow_redirects=False,
    )
    assert response.status_code == 303
    with get_db() as conn:
        invoice = conn.execute(
            "SELECT * FROM invoices WHERE source_filename='llm-traceability-test.pdf' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert invoice is not None
        audit_row = conn.execute(
            "SELECT * FROM audit_events WHERE invoice_id=? AND event_type='LLM_EXTRACTION_COMPLETED' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (invoice['id'],),
        ).fetchone()
        assert audit_row is not None
        details = json.loads(audit_row['details_json'])
        assert details['extraction_source'] == 'external_llm_structured_extraction'
        assert details['provider'] == 'Google Gemini'
        assert details['model'] == 'gemini-3.8-flash'
        assert details['request_status'] == 'succeeded'
        assert details['request_attempted'] is True
        assert details['fallback_used'] is False
        assert 'unit-test-gemini-api-key-do-not-log' not in audit_row['details_json']
        invoice_id = invoice['id']

    detail = client.get(f'/invoices/{invoice_id}')
    assert detail.status_code == 200
    assert 'External LLM structured extraction' in detail.text
    assert 'Google Gemini' in detail.text
    assert 'gemini-3.8-flash' in detail.text
    assert 'Request: Succeeded' in detail.text
    assert 'unit-test-gemini-api-key-do-not-log' not in detail.text
    client.cookies.clear()


def test_invalid_llm_json_records_fallback_without_partial_model_mutation(monkeypatch):
    _configure_gemini(monkeypatch)
    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', lambda request, timeout: FakeResponse(
        _completion('{this is not json')
    ))
    original = {'invoice_number': 'LOCAL-INVALID-JSON', 'total': 99, 'items': [{'item_name': 'Local row'}]}
    fields, source, trace = llm_extraction.optional_llm_extract('synthetic document', original.copy())
    assert source == 'fallback_after_llm_error'
    assert trace['request_status'] == 'failed'
    assert trace['error_type'] == 'JSONDecodeError'
    assert trace['fallback_used'] is True
    assert fields == original


def test_optional_llm_purchase_order_uses_po_specific_prompt_and_field_mapping(monkeypatch):
    _configure_gemini(monkeypatch)
    seen = {}

    def fake_urlopen(request, timeout):
        seen['body'] = json.loads(request.data.decode('utf-8'))
        return FakeResponse(_completion(json.dumps({
            'purchase_order_number': 'PO-LLM-1001',
            'invoice_issue_date': '2026-10-20',
            'vendor_company_name': 'PO Test Supplier LLC',
            'total': 108.00,
        })))

    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', fake_urlopen)
    fields, source, trace = llm_extraction.optional_llm_extract(
        'PURCHASE ORDER\nPO-LLM-1001\nSupplier: PO Test Supplier LLC\nTotal: USD 108.00',
        {'purchase_order_number': 'PO-LLM-1001', 'items': []},
        document_type='purchase_order',
    )
    system_prompt = seen['body']['messages'][0]['content']
    user_prompt = seen['body']['messages'][1]['content']
    assert trace['document_type'] == 'purchase_order'
    assert 'purchase order' in system_prompt.lower()
    assert 'do not invent an invoice number' in system_prompt.lower()
    assert 'untrusted purchase order text' in user_prompt.lower()
    assert fields['purchase_order_number'] == 'PO-LLM-1001'
    assert fields['vendor_company_name'] == 'PO Test Supplier LLC'
    assert fields['total'] == 108.0
    assert source == 'external_llm_structured_extraction'


def test_optional_llm_retries_retryable_http_error_once(monkeypatch):
    _configure_gemini(monkeypatch)
    monkeypatch.setenv('SENTRION_AI_MAX_RETRIES', '1')
    calls = {'count': 0}

    def retry_once(request, timeout):
        calls['count'] += 1
        if calls['count'] == 1:
            raise urllib.error.HTTPError(request.full_url, 429, 'rate limit', {}, io.BytesIO(b'private body'))
        return FakeResponse(_completion(json.dumps({'invoice_number': 'RETRY-OK-01'})))

    monkeypatch.setattr(llm_extraction.urllib.request, 'urlopen', retry_once)
    fields, source, trace = llm_extraction.optional_llm_extract('document evidence', {'items': []})
    assert calls['count'] == 2
    assert trace['retry_count'] == 1
    assert trace['request_status'] == 'succeeded'
    assert fields['invoice_number'] == 'RETRY-OK-01'
    assert source == 'external_llm_structured_extraction'
