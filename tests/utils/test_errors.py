from agent.utils.errors import classify_exception, exception_fields


class _GatewayError(Exception):
    def __init__(self, message="", *, status_code=None, body=None, request_id=None):
        super().__init__(message)
        self.status_code = status_code
        self.body = body
        self.request_id = request_id


class _WrappedError(Exception):
    pass


def test_classify_exception_uses_dict_message_and_string_body():
    assert classify_exception(_GatewayError(body={"error": {"message": "Rate_limit exceeded"}})) == "provider_rate_limited"
    assert classify_exception(_GatewayError(body={"detail": "Upstream overloaded"})) == "provider_overloaded"
    assert classify_exception(_GatewayError(body=b'{"error":"context_length_exceeded"}')) == "context_too_long"
    assert classify_exception(_GatewayError(body='{"code":"model_not_available"}')) == "model_unavailable"


def test_classify_exception_handles_string_status_and_cause_chain():
    assert classify_exception(_GatewayError(status_code="429")) == "provider_rate_limited"
    assert classify_exception(_GatewayError(status_code="503")) == "provider_unavailable"
    assert classify_exception(_GatewayError(status_code=True)) is None

    try:
        try:
            raise TimeoutError("timed out")
        except TimeoutError as inner:
            inner.status_code = 529
            raise _WrappedError("outer") from inner
    except _WrappedError as outer:
        assert classify_exception(outer) == "provider_overloaded"
        fields = exception_fields(outer)
        assert fields["error_type"] == "_WrappedError"
        assert "TimeoutError" in fields["cause"]
