from datetime import datetime

from annuli.core.models import Role, Tool, UnifiedMessage, UnifiedSession
from annuli.interfaces import web, web_utils
from annuli.interfaces.web_services import normalize_session_messages_for_display


def _session(messages):
    return UnifiedSession(
        tool=Tool.OPENCODE,
        session_id="ses-noise",
        created_at=datetime(2026, 3, 5, 11, 0, 0),
        last_updated=datetime(2026, 3, 5, 11, 5, 0),
        messages=messages,
    )


def test_api_noise_rules_get_and_post(monkeypatch, tmp_path):
    noise_path = tmp_path / "noise_rules.json"
    monkeypatch.setattr(web_utils, "NOISE_RULES_PATH", noise_path)

    with web.app.test_client() as client:
        response = client.get("/api/noise-rules")
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["opencode"]["opencode_summarize_file_operations"] is True
        assert payload["default"]["strip_ansi_sequences"] is True
        assert payload["claude-code"]["claude_summarize_file_operations"] is True

        post = client.post(
            "/api/noise-rules",
            json={"opencode": {"opencode_summarize_file_operations": False}},
        )
        assert post.status_code == 200

        response2 = client.get("/api/noise-rules")
        payload2 = response2.get_json()
        assert payload2["opencode"]["opencode_summarize_file_operations"] is False


def test_noise_rules_page_renders(monkeypatch, tmp_path):
    monkeypatch.setattr(web_utils, "NOISE_RULES_PATH", tmp_path / "noise_rules.json")

    with web.app.test_client() as client:
        response = client.get("/noise-rules")

    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Noise Rules" in body
    assert "noiseRulesSaveBtn" in body
    assert "noiseRulesResetBtn" in body
    assert "noiseRulesPreviewBtn" in body
    assert "noisePreviewInput" in body
    assert "Balanced" in body
    assert "Aggressive Cleanup" in body
    assert "Minimal Cleanup" in body
    assert 'data-preset="balanced"' in body


def test_opencode_file_summary_rule_can_be_disabled():
    session = _session(
        [
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="\n".join(
                    [
                        "The file /tmp/a.py has been updated.",
                        "The file /tmp/b.py has been updated.",
                        "The file /tmp/c.py has been created.",
                    ]
                ),
                timestamp=datetime(2026, 3, 5, 11, 1, 0),
            )
        ]
    )

    normalize_session_messages_for_display(
        session,
        noise_rules={
            "opencode": {
                "opencode_summarize_file_operations": False,
                "opencode_strip_user_caveat": True,
            }
        },
    )

    assert "File operations summary" not in session.messages[0].content
    assert "The file /tmp/a.py has been updated." in session.messages[0].content


def test_disable_default_strip_ansi_rule():
    session = _session(
        [
            UnifiedMessage(
                role=Role.ASSISTANT,
                content="\u001b[31mERROR\u001b[0m",
                timestamp=datetime(2026, 3, 5, 11, 2, 0),
            )
        ]
    )

    normalize_session_messages_for_display(
        session,
        noise_rules={
            "default": {"strip_ansi_sequences": False},
            "opencode": {
                "opencode_summarize_file_operations": True,
                "opencode_strip_user_caveat": True,
                "opencode_summarize_unset_env_vars": True,
            },
        },
    )

    assert "\u001b[31m" in session.messages[0].content


def test_api_noise_rules_preview_applies_transient_rules(monkeypatch, tmp_path):
    noise_path = tmp_path / "noise_rules.json"
    monkeypatch.setattr(web_utils, "NOISE_RULES_PATH", noise_path)

    with web.app.test_client() as client:
        response = client.post(
            "/api/noise-rules/preview",
            json={
                "tool": "opencode",
                "role": "assistant",
                "content": "The file /tmp/a.py has been updated.\nThe file /tmp/b.py has been updated.\nThe file /tmp/c.py has been created.",
                "noise_rules": {
                    "opencode": {
                        "opencode_summarize_file_operations": False,
                    }
                },
            },
        )

    assert response.status_code == 200
    payload = response.get_json()
    assert "File operations summary" not in payload["content"]
    assert "The file /tmp/a.py has been updated." in payload["content"]
