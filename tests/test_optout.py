import pytest

from app.services.optout import NOT_INTERESTED, UNSUBSCRIBE, detect_optout


@pytest.mark.parametrize("text", [
    "STOP", "Please unsubscribe me", "remove me from your list", "Don't contact us again",
    "dont email me", "do not message again", "opt out", "Please take me off this list",
    "message mat karo", "msg mat bhejo bhai", "dobara mat bhejna", "band karo ye sab",
    "कृपया मैसेज मत भेजो", "darse de baja",
])
def test_unsubscribe_phrases(text):
    r = detect_optout(text)
    assert r is not None and r.kind == UNSUBSCRIBE, text


@pytest.mark.parametrize("text", [
    "Not interested, thanks", "no thanks", "No, thank you.", "we're not looking right now",
    "interest nahi hai", "humein nahi chahiye", "abhi zarurat nahi", "rehne do bhai",
    "मुझे रुचि नहीं है", "नको", "no me interesa", "not for us",
])
def test_not_interested_phrases(text):
    r = detect_optout(text)
    assert r is not None and r.kind == NOT_INTERESTED, text


@pytest.mark.parametrize("text", [
    "Sounds great, what are your rates?", "Can you send more work?", "Interested! Let's talk tomorrow",
    "Haan bhejo portfolio", "kitna charge karte ho?", "We need 8 reels a month",
])
def test_positive_replies_not_flagged(text):
    assert detect_optout(text) is None, text


def test_quoted_history_ignored():
    reply = ("Yes please share pricing.\n\nOn Mon, 9 Nov 2026, Asha wrote:\n"
             "> Reply 'stop' and I won't contact you again.")
    assert detect_optout(reply) is None
    assert detect_optout("Hi\n> some quoted stop\nlooks good") is None
