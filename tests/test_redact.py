"""Tests for lore.utils.redact."""

from __future__ import annotations

import random
import time

from lore.utils.redact import RedactionResult, redact, redact_many

MARKER = "[REDACTED:"


def test_private_key_pem_block():
    body = "A" * 40
    pem = (
        "Before chat log.\n"
        "-----BEGIN RSA PRIVATE KEY-----\n"
        f"{body}\n"
        "-----END RSA PRIVATE KEY-----\n"
        "After."
    )
    result = redact(pem)
    assert "-----BEGIN" not in result.text
    assert result.text.startswith("Before chat log.\n")
    assert f"{MARKER}private_key]" in result.text
    assert result.kinds.get("private_key") == 1
    assert result.count == 1


def test_aws_access_key_in_prose():
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    text = f"Deploy uses access key {key} in the old config file."
    result = redact(text)
    assert key not in result.text
    assert f"{MARKER}aws_access_key]" in result.text
    assert result.kinds["aws_access_key"] == 1


def test_aws_secret_access_key():
    secret = "A" * 20 + "/" + "B" * 19  # 40 base64-ish
    text = f"aws_secret_access_key={secret} trailing"
    result = redact(text)
    assert secret not in result.text
    assert "aws_secret_access_key=" in result.text
    assert f"{MARKER}aws_secret]" in result.text
    assert result.kinds["aws_secret"] == 1


def test_github_classic_token():
    tok = "ghp_" + "a" * 36
    text = f"curl -H 'Authorization: token {tok}' https://api.github.com"
    result = redact(text)
    assert tok not in result.text
    assert result.kinds["github_token"] == 1


def test_github_pat():
    pat = "github_pat_" + ("x" * 50)
    result = redact(f"token={pat}")
    assert pat not in result.text
    assert result.kinds["github_token"] == 1


def test_slack_token():
    tok = "xoxb-" + ("A" * 12)
    result = redact(f"Slack bot uses {tok} here.")
    assert tok not in result.text
    assert result.kinds["slack_token"] == 1


def test_openai_key():
    key = "sk-proj-" + ("x" * 20)
    result = redact(f"OPENAI_API_KEY={key}")
    assert key not in result.text
    assert result.kinds["openai_key"] == 1


def test_anthropic_key():
    key = "sk-ant-" + ("a" * 20)
    result = redact(f"key {key} end")
    assert key not in result.text
    assert result.kinds["anthropic_key"] == 1


def test_google_api_key():
    key = "AIza" + ("x" * 35)
    result = redact(f"maps?key={key}")
    assert key not in result.text
    assert result.kinds["google_api_key"] == 1


def test_stripe_key():
    key = "sk_live_" + ("A" * 16)
    result = redact(f"stripe {key}")
    assert key not in result.text
    assert result.kinds["stripe_key"] == 1


def test_jwt():
    seg = "A" * 12
    jwt = f"eyJ{seg}.eyJ{seg}.eyJ{seg}"
    result = redact(f"Cookie: session={jwt};")
    assert jwt not in result.text
    assert result.kinds["jwt"] == 1


def test_bearer_authorization_header():
    tok = "z" * 24
    text = f"Authorization: Bearer {tok}"
    result = redact(text)
    assert tok not in result.text
    assert "Authorization: Bearer " in result.text
    assert f"{MARKER}bearer_token]" in result.text
    assert result.kinds["bearer_token"] == 1


def test_bearer_lowercase_word():
    tok = "y" * 22
    result = redact(f"send bearer {tok} please")
    assert tok not in result.text
    assert "bearer " in result.text
    assert result.kinds["bearer_token"] == 1


def test_url_password_only():
    text = "https://user:supersecret12@api.example.com/v1"
    result = redact(text)
    assert "supersecret12" not in result.text
    assert "https://user:" in result.text
    assert "@api.example.com" in result.text
    assert result.kinds["url_password"] == 1


def test_secret_assignment_keeps_key():
    result = redact('password="hunter2!!"')
    assert 'password="[REDACTED:secret_assignment]"' in result.text
    assert "hunter2" not in result.text
    assert result.kinds["secret_assignment"] == 1


def test_ordinary_code_untouched():
    sample = """
def f(x):
    return x + 1
token_count = 12
password_field = form.password
api_key = os.environ["API_KEY"]
Authorization: Bearer ${TOKEN}
"""
    result = redact(sample)
    assert result.text == sample
    assert result.count == 0


def test_hashes_uuid_git_sha_untouched():
    git_sha = "a" * 40
    sha256 = "b" * 64
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    b64_header = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    text = f"commit {git_sha} digest {sha256} id {uuid} img {b64_header}"
    result = redact(text)
    assert result.text == text
    assert result.count == 0


def test_assignment_placeholders_untouched():
    text = "password=changeme\nsecret=<insert-here>\napi_key=${API_KEY}\npwd=your_password\n"
    result = redact(text)
    assert result.text == text
    assert result.count == 0


def test_counts_match_kinds():
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    tok = "xoxb-" + ("z" * 12)
    text = f"{key} and {tok}"
    result = redact(text)
    assert result.count == sum(result.kinds.values())
    assert result.count == 2


def test_idempotent_mixed_sample():
    key = "sk-ant-" + ("k" * 20)
    mixed = f"note {key} and password=realvalue9"
    once = redact(mixed)
    twice = redact(once.text)
    assert twice.text == once.text
    assert twice.count == 0


def test_idempotent_random_strings_property():
    rng = random.Random(197)
    for _ in range(200):
        length = rng.randint(0, 400)
        chars = "abcdefghijklmnopqrstuvwxyz0123456789 sk-AKIApassword=:./@\n"
        s = "".join(rng.choice(chars) for _ in range(length))
        once = redact(s)
        assert redact(once.text).text == once.text
        assert once.count == sum(once.kinds.values())


def test_non_str_input():
    result = redact(42)  # type: ignore[arg-type]
    assert result == RedactionResult("42", 0, {})


def test_empty_and_nul_surrogates():
    assert redact("").count == 0
    nul = "before\x00after"
    r = redact(nul)
    assert "\x00" in r.text


def test_redact_many():
    key = "AKIA" + "ABCDEFGHIJKLMNOP"
    texts, total = redact_many(["clean", f"key {key}"])
    assert texts[0] == "clean"
    assert key not in texts[1]
    assert total == 1


def test_linear_time_large_inputs():
    limit = 2.0
    cases = [
        "a" * 2_000_000,
        "sk-" * 600_000,
        "AKIA" * 500_000,
        "password=" * 200_000,
        "eyJ" + "." * 1_000_000,
    ]
    for payload in cases:
        start = time.perf_counter()
        redact(payload)
        elapsed = time.perf_counter() - start
        assert elapsed < limit, f"too slow ({elapsed:.2f}s) for len={len(payload)}"


def test_jwt_prefix_run_is_linear_time():
    payload = "eyJ" * 400_000 + "." * 10
    start = time.perf_counter()
    result = redact(payload)
    assert time.perf_counter() - start < 3.0
    assert result.count == 0


def test_jwt_after_separator_is_still_redacted():
    token = "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20
    assert redact(f"token: {token}").kinds == {"jwt": 1}


def _kinds(text: str) -> dict[str, int]:
    return redact(text).kinds


def test_basic_auth_and_short_bearer_tokens_are_redacted():
    basic = redact("Authorization: Basic dXNlcjpwYXNzd29yZA==")
    assert "dXNlcjpwYXNzd29yZA==" not in basic.text
    assert "Authorization: Basic [REDACTED:bearer_token]" in basic.text
    short = redact("Authorization: Bearer abc12345")
    assert "abc12345" not in short.text


def test_auth_header_placeholders_are_left_alone():
    assert redact("Authorization: Bearer ${TOKEN}").count == 0
    assert redact("Authorization: Bearer <your-token>").count == 0
    assert redact("Authorization: Bearer $API_TOKEN").count == 0


def test_dotenv_style_names_are_covered():
    text = (
        "OPENAI_API_KEY=notarealkey1234\nDB_PASSWORD=correcthorsebattery\nGITHUB_TOKEN=abcd1234efgh"
    )
    result = redact(text)
    assert result.kinds == {"secret_assignment": 3}
    assert "OPENAI_API_KEY=[REDACTED:secret_assignment]" in result.text
    assert "correcthorsebattery" not in result.text


def test_quoted_values_with_spaces_are_replaced_whole():
    result = redact('api_key="hunter2 with spaces"')
    assert result.text == 'api_key="[REDACTED:secret_assignment]"'


def test_json_delimiters_survive():
    result = redact('{"api_key": "hunter2!!x", "ok": true}')
    assert result.text == '{"api_key": "[REDACTED:secret_assignment]", "ok": true}'


def test_code_references_and_numbers_are_not_secrets():
    assert redact("password = request.password").count == 0
    assert redact("password = self.config.password").count == 0
    assert redact("max_tokens: 100000000").count == 0
    assert redact("token_count = 123456789").count == 0
    assert redact("password = get_password(user)").count == 0


def test_npm_pypi_gitlab_huggingface_tokens():
    npm = "npm_" + "A" * 36
    pypi = "pypi-" + "B" * 60
    gitlab = "glpat-" + "C" * 20
    hf = "hf_" + "D" * 34
    result = redact(f"a {npm} b {pypi} c {gitlab} d {hf} e")
    assert result.text == (
        "a [REDACTED:npm_token] b [REDACTED:pypi_token] c [REDACTED:gitlab_token] "
        "d [REDACTED:huggingface_token] e"
    )


def test_cookie_header_value_is_redacted():
    result = redact("Cookie: sessionid=abcdefghijklmnop; csrftoken=zzzzzzzzzzzz")
    assert result.text == "Cookie: [REDACTED:cookie]"


def test_long_unclosed_pem_is_redacted_to_the_end():
    body = "A" * 50_000
    result = redact(f"before\n-----BEGIN RSA PRIVATE KEY-----\n{body}\ntail-secret")
    assert result.text == "before\n[REDACTED:private_key]"


def test_many_unmatched_pem_headers_are_linear_time():
    payload = "-----BEGIN RSA PRIVATE KEY-----\n" * 100_000
    start = time.perf_counter()
    result = redact(payload)
    assert time.perf_counter() - start < 3.0
    assert result.text == "[REDACTED:private_key]"


def test_markers_are_idempotent_for_every_supported_kind():
    samples = [
        "AKIA" + "ABCDEFGHIJKLMNOP",
        "ghp_" + "a" * 36,
        "sk-" + "a" * 24,
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Cookie: sessionid=abcdefghijklmnop",
        "postgres://user:s3cretpassword@db/x",
        'DB_PASSWORD="correct horse battery"',
        "npm_" + "A" * 36,
        "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
        "--password hunter2hunter2",
    ]
    for sample in samples:
        once = redact(sample)
        twice = redact(once.text)
        assert twice.text == once.text, sample
        assert twice.count == 0, sample


def test_brackets_parentheses_and_dots_inside_literal_secrets_are_redacted():
    for value in ("safe[looking]value", "safe(value)text", "abcd.efghijkl.mn"):
        result = redact(f'password="{value}"')
        assert value not in result.text, value
    assert redact("password=abcd.efghijkl.mn").count == 1


def test_unlisted_authorization_schemes_are_redacted():
    result = redact("Authorization: MAC abcdef123456")
    assert result.text == "Authorization: MAC [REDACTED:bearer_token]"
    assert redact("authorization: someOpaqueCredentialValue").count == 1


def test_azure_connection_string_keys_are_redacted():
    text = "DefaultEndpointsProtocol=https;AccountName=acct;AccountKey=abc123def456ghi789=="
    result = redact(text)
    assert "abc123def456ghi789" not in result.text
    assert "AccountName=acct" in result.text


def test_quotes_inside_quoted_values_and_escapes():
    assert "value-123" not in redact('{"api_key":"secret\'s-value-123"}').text
    assert "value-123" not in redact("""api_key = 'say "hi" value-123'""").text
    assert "tail-123" not in redact('password="a\\"b tail-123"').text


def test_mismatched_pem_end_marker_does_not_leave_the_key_exposed():
    text = "-----BEGIN RSA PRIVATE KEY----- AAAA -----END CERTIFICATE----- remaining-key-data"
    result = redact(text)
    assert "remaining-key-data" not in result.text


def test_json_quoted_authorization_header():
    result = redact('{"Authorization":"Bearer abcdefghijklmnopqrstuvwxyz", "x": 1}')
    assert "abcdefghijklmnopqrstuvwxyz" not in result.text
    assert '"x": 1' in result.text


def test_sas_signature_in_query_string():
    url = "https://acct.blob.core.windows.net/c?sv=2023-01-03&se=2030&sig=abcdefghijklmnop%2Bq%3D"
    result = redact(url)
    assert "abcdefghijklmnop" not in result.text
    assert "sv=2023-01-03&se=2030" in result.text


def test_cli_flag_prose_is_not_redacted_but_credential_values_are():
    assert redact("Use --password when prompted").count == 0
    assert (
        redact("run --token=abcd1234efgh now").text
        == "run --token=[REDACTED:secret_assignment] now"
    )
    assert redact("mysql --password hunter2hunter2!").count == 1


def test_non_string_inputs_never_raise():
    class Boom:
        def __str__(self) -> str:
            raise RuntimeError("no")

    assert redact(Boom()).count == 0  # type: ignore[arg-type]
    assert redact(None).text == "None"  # type: ignore[arg-type]


def test_redact_value_uses_the_field_name_as_context():
    from lore.utils.redact import redact_value

    assert redact_value("password", "hunter2!x").text == "[REDACTED:secret_assignment]"
    assert redact_value("DB_PASSWORD", "x1y2z3w4").count == 1
    assert redact_value("username", "alice").text == "alice"
    assert redact_value("token", "${TOKEN}").count == 0
    assert redact_value("note", "sk-" + "a" * 24).kinds == {"openai_key": 1}
