"""Wire contract of Message, as producers publish it.

The JSON payload on the receipt queue must not change: ``content`` is the
UTF-8 string form of the bytes. Producers send receipt text as
ASCII, so in practice the bytes are ASCII text plus ESC/POS control codes.
"""

import json

import pytest
from pydantic_core import PydanticSerializationError

from croupier.printing import Message

ESC_POS = b"\x1bt\x00DEMO RECEIPT\n\x1bd\x06\x1dV\x00"


def test_parses_producer_payload_byte_for_byte() -> None:
    payload = json.dumps(
        {
            "content": ESC_POS.decode("utf-8"),
            "network_host": "192.168.1.50",
            "network_timeout": 60,
        },
    )

    message = Message.model_validate_json(payload)

    assert message.content == ESC_POS
    assert message.network_host == "192.168.1.50"
    assert message.network_timeout == 60


def test_ignores_extra_fields_from_producers() -> None:
    payload = '{"content": "x", "network_host": "h", "network_timeout": 1, "extra": 1}'

    assert Message.model_validate_json(payload).content == b"x"


def test_serialises_content_as_utf8_string() -> None:
    message = Message(content=ESC_POS, network_host="h", network_timeout=1)

    assert json.loads(message.model_dump_json())["content"] == ESC_POS.decode("utf-8")


def test_content_that_is_not_utf8_cannot_be_serialised() -> None:
    # Locks the contract: do not change the wire format; producers must send
    # UTF-8 (producers send ASCII). See context/queue-topology.md.
    message = Message(content=b"\xe9", network_host="h", network_timeout=1)

    with pytest.raises(PydanticSerializationError):
        message.model_dump_json()
